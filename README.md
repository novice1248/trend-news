# Trend News

IT・世界情勢・経済・科学のニュースをRSSから1時間ごとに自動収集し、トレンドワードを抽出して表示するサイトです。芸能ニュースは除外しています。

公開URL: https://novice1248.github.io/trend-news/

## しくみ
- `scripts/fetch_news.py` … RSSを取得 → 芸能系を除外 → トレンドワード抽出 → `docs/data.json` を出力（Python標準ライブラリのみ）
- `.github/workflows/update.yml` … 上のスクリプトを毎時実行して `docs/data.json` をコミット
- `docs/index.html` … 表示ページ（GitHub Pages で `main` ブランチの `/docs` を公開）

## スマホからのカスタマイズ
`scripts/config.json` をGitHubアプリやブラウザで編集するだけで反映されます（保存すると自動で再取得）。
- `feeds` … 取得するRSS。`category` は `it` / `world` / `economy` / `science`
- `block_words` … タイトルや媒体名にこの語が含まれる記事を除外（芸能対策）
- `stop_words` … トレンドワードに出したくない一般的な語

すぐ更新したいときは Actions タブ →「Update news」→「Run workflow」。
