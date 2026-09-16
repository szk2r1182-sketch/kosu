# 工数タイムレコーダー

日々の工数を記録・集計するシングルファイルの Web アプリです（`index.html` のみで動作します）。

## 公開URL

https://szk2r1182-sketch.github.io/kosu/

`main` ブランチに push すると GitHub Actions（`.github/workflows/pages.yml`）が自動で GitHub Pages へデプロイします。

## 初回だけ必要な設定

1. GitHub のリポジトリ画面で **Settings → Pages** を開く
2. **Build and deployment → Source** を **GitHub Actions** に変更する
3. `main` に push（またはワークフローを手動実行）するとデプロイが走る

デプロイの状況は **Actions** タブで確認できます。

## 使い方のメモ

- 記録はブラウザの `localStorage` に保存されます。**端末・ブラウザごとに別データ**で、サーバーには送信されません。
- 端末を移行するときは、アプリ内の「データの引き継ぎ（バックアップ）」からバックアップを書き出し、移行先で読み込んでください。
- スマホではブラウザの「ホーム画面に追加」をしておくとアプリのように起動できます。

## 開発

`index.html` を直接編集します。画面右下のバージョン表記（`ver.X.Y.Z`）とファイル冒頭の変更履歴コメントも合わせて更新してください。

動作確認はファイルをブラウザで開くだけで可能です。ローカルサーバーで確認する場合:

```bash
python3 -m http.server 8000
# http://localhost:8000/ を開く
```
