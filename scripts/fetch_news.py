"""RSS からIT・世界情勢ニュースを集め、芸能系を除外して docs/data.json を作る。

Python 標準ライブラリだけで動く（GitHub Actions で1時間ごとに実行）。
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "scripts" / "config.json").read_text(encoding="utf-8"))
OUT = ROOT / "docs" / "data.json"

JST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
KEEP_HOURS = CONFIG.get("keep_hours", 48)
TREND_HOURS = CONFIG.get("trend_hours", 24)
UA = "Mozilla/5.0 (trend-news; +https://github.com/novice1248/trend-news)"


# ---------- 取得・パース ----------

def fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception as e:  # 1つ失敗しても他は続ける
        print(f"  ! fetch failed: {url} ({e})", file=sys.stderr)
        return None


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(el: ET.Element, *names: str) -> str:
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def parse_feed(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        name = local(el.tag)
        if name not in ("item", "entry"):
            continue
        title = clean(child_text(el, "title"))
        link = child_text(el, "link")
        if not link:  # Atom
            for c in el:
                if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
        if not link and el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"):
            link = el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
        date = parse_date(child_text(el, "pubDate", "date", "published", "updated"))
        summary = clean(child_text(el, "description", "summary"))[:160]
        bookmarks = child_text(el, "bookmarkcount")  # はてブ
        if title and link:
            items.append({
                "title": title,
                "link": link.strip(),
                "date": date,
                "summary": summary,
                "score": int(bookmarks) if bookmarks.isdigit() else 0,
            })
    return items


# ---------- フィルタ ----------

BLOCK = [w.lower() for w in CONFIG["block_words"]]
BLOCK_RE = re.compile("|".join(re.escape(w) for w in BLOCK), re.I) if BLOCK else None


def is_blocked(text: str) -> bool:
    return bool(BLOCK_RE and BLOCK_RE.search(text))


def google_news_title(title: str) -> tuple[str, str]:
    """Google ニュースは「見出し - 媒体名」形式なので分ける。"""
    m = re.match(r"^(.*)\s+-\s+([^-]{1,40})$", title)
    return (m.group(1).strip(), m.group(2).strip()) if m else (title, "")


# ---------- トレンドワード抽出 ----------

STOP = set(CONFIG["stop_words"])
TOKEN_RE = re.compile(
    r"[ァ-ヴー]{3,}"                     # カタカナ語
    r"|[一-龥々]{2,6}"                    # 漢字の熟語
    r"|[A-Za-z][A-Za-z0-9\.\+\-]{1,}"   # 英数字（AI, iPhone, GPT-5 など）
)


CHUNK_RE = re.compile(r"[一-龥々ァ-ヴーA-Za-z0-9\.\+\-]+")
SEG_RE = re.compile(r"[一-龥々]+|[ァ-ヴー]+|[A-Za-z0-9\.\+\-]+")


def mixed_words(title: str) -> list[str]:
    """「生成AI規制」→「生成AI」「AI規制」のように、文字種の切れ目で隣り合う2つをつなぐ。"""
    out = []
    for chunk in CHUNK_RE.findall(title):
        segs = SEG_RE.findall(chunk)
        out += [a + b for a, b in zip(segs, segs[1:]) if len(a + b) <= 12]
    return out


def tokens(title: str) -> set[str]:
    out = set()
    for t in TOKEN_RE.findall(title) + mixed_words(title):
        t = t.strip(".-")
        if len(t) < 2 or t in STOP or t.lower() in STOP:
            continue
        if re.fullmatch(r"[a-z][a-z\-]*", t):  # 小文字だけの英単語（一般語）は除外
            continue
        if re.search(r"[0-9]", t) and re.search(r"[日月年時分週]", t):  # 「26日」などの日付
            continue
        if re.fullmatch(r"[0-9\.\+\-]+", t):  # 数字だけは除外
            continue
        out.add(t)
    return out


def trend_words(articles: list[dict], limit: int = 20) -> list[dict]:
    since = NOW - timedelta(hours=TREND_HOURS)
    df: Counter = Counter()
    srcs: dict[str, set] = defaultdict(set)
    cats: dict[str, Counter] = defaultdict(Counter)
    for a in articles:
        if datetime.fromisoformat(a["date"]) < since:
            continue
        for t in tokens(a["title"]):
            df[t] += 1
            srcs[t].add(a["source"])
            cats[t][a["category"]] += 1
    scored = []
    for t, n in df.items():
        if n < 2:
            continue
        score = n * (1 + 0.5 * (len(srcs[t]) - 1))  # 複数の媒体で出てるほど上位
        scored.append((score, n, t))
    scored.sort(key=lambda x: (x[0], len(x[2])), reverse=True)  # 同点なら長い語を優先

    picked: list[tuple] = []
    for s, n, t in scored:
        # 「生成AI」と「AI」のような包含関係は上位だけ残す
        if any(t in p[2] or p[2] in t for p in picked):
            continue
        picked.append((s, n, t))
        if len(picked) >= limit:
            break
    return [
        {"word": t, "count": n, "category": cats[t].most_common(1)[0][0]}
        for s, n, t in picked
    ]


# ---------- メイン ----------

def load_previous() -> list[dict]:
    if OUT.exists():
        try:
            return json.loads(OUT.read_text(encoding="utf-8")).get("articles", [])
        except Exception:
            pass
    return []


def main() -> None:
    cutoff = NOW - timedelta(hours=KEEP_HOURS)
    articles: dict[str, dict] = {}

    for a in load_previous():  # 前回分を引き継いで48時間ぶん保持
        if datetime.fromisoformat(a["date"]) >= cutoff:
            articles[a["link"]] = a

    blocked = 0
    for feed in CONFIG["feeds"]:
        print(f"- {feed['name']}")
        data = fetch(feed["url"])
        if not data:
            continue
        try:
            items = parse_feed(data)
        except ET.ParseError as e:
            print(f"  ! parse failed ({e})", file=sys.stderr)
            continue
        for it in items[: feed.get("max", 40)]:
            date = it["date"] or NOW
            if date < cutoff:
                continue
            title, source = it["title"], feed["name"]
            if feed.get("google"):
                title, media = google_news_title(title)
                if media:
                    source = media
                it["summary"] = ""  # Google ニュースの概要は見出しの繰り返しなので省く
            if is_blocked(" ".join([title, source, it["summary"], it["link"]])):
                blocked += 1
                continue
            old = articles.get(it["link"])
            articles[it["link"]] = {
                "title": title,
                "link": it["link"],
                "source": source,
                "category": (old["category"] if old else feed["category"]),
                "lang": feed.get("lang", "ja"),
                "date": (old["date"] if old else date.isoformat()),
                "summary": it["summary"],
                "score": max(it["score"], old.get("score", 0) if old else 0),
            }

    # 同じ見出しの重複を除く
    seen, uniq = set(), []
    for a in sorted(articles.values(), key=lambda x: x["date"], reverse=True):
        key = re.sub(r"\W", "", a["title"])[:40]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)

    result = {
        "updated": NOW.astimezone(JST).isoformat(timespec="minutes"),
        "categories": CONFIG["categories"],
        "trends": trend_words(uniq),
        "articles": uniq[: CONFIG.get("max_articles", 600)],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"articles={len(result['articles'])} trends={len(result['trends'])} blocked={blocked}")


if __name__ == "__main__":
    main()
