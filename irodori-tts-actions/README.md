# irodori-tts-actions

[Irodori-TTS](https://github.com/Aratako/Irodori-TTS)（日本語の音声合成モデル）を **GitHub Actions 上で動かして音声ファイルを作る**ためのリポジトリ。

GPU つきの PC を用意しなくても、GitHub の画面からテキストを入れてワークフローを回せば、
生成された WAV / MP3 が **Artifacts**（必要なら **Release**）に出てくる。

---

## できること

| ワークフロー | 用途 |
| --- | --- |
| **音声生成（単発）** `tts.yml` | 画面にテキストを打ち込んで 1 ファイル作る |
| **音声生成（一括）** `tts-batch.yml` | `jobs/*.yaml` に書いた複数のテキストをまとめて作る |
| **検証** `ci.yml` | モデルを落とさずにスクリプトとジョブ定義だけをチェックする |

- 参照音声（`voices/`）を置けば、その声を複製して読み上げる（ゼロショット音声クローン）
- テキストに絵文字を混ぜると、話し方・感情・笑い声などが変わる（Irodori-TTS の特徴）
- VoiceDesign 対応チェックポイントなら、`caption` に声の説明を書いて声そのものを指定できる
- 長文は文の切れ目で自動分割して合成し、1 本につなぎ直す

---

## 使い方（単発）

1. リポジトリの **Actions** タブ → **音声生成（単発）** を開く
2. **Run workflow** を押し、入力を埋める
3. 実行が終わったら、実行ページ下部の **Artifacts** から `audio-<番号>.zip` をダウンロード

### 入力

| 入力 | 説明 |
| --- | --- |
| `text` | 読み上げるテキスト（必須）。**改行は `\n` と書く**（入力欄が 1 行しかないため） |
| `voice` | `voices/` 配下のファイル名（拡張子なし）。カンマ区切りで複数可。空なら参照音声なし |
| `caption` | 声の説明。VoiceDesign 対応チェックポイントでのみ有効 |
| `name` | 出力ファイル名（拡張子なし） |
| `formats` | `wav` / `mp3` / `wav,mp3` |
| `checkpoint` | Hugging Face のモデル。既定は `Aratako/Irodori-TTS-v4.1-Small-MF` |
| `num_steps` | サンプリングステップ数。空ならモデルの既定値 |
| `seed` | 乱数シード。**同じ声・同じ読み方を再現したいときに固定する** |
| `upstream_ref` | Irodori-TTS のブランチ／タグ／コミット SHA |
| `release` | オンにすると Release にも添付する |

---

## 使い方（一括）

`jobs/example.yaml` のようなジョブ定義を書いて、**音声生成（一括）** の `spec` に渡す。

```yaml
defaults:
  checkpoint: Aratako/Irodori-TTS-v4.1-Small-MF
  voice: null # voices/ のファイル名。null なら参照なし
  formats: [wav, mp3]
  max_chars: 120 # 1 回に渡す最大文字数
  gap_ms: 300 # 分割したチャンクの間に挟む無音

items:
  - id: greeting
    text: |
      おはようございます。今日もよろしくお願いします。
    seed: 20260919

  - id: from-file
    text_file: ../texts/sample.txt
    voice: narrator
```

- `id` がそのまま出力ファイル名になる
- `text` と `text_file` はどちらか一方
- `defaults` に書けるキーは `items` にも書ける（項目側が優先）
- 同じ `checkpoint` の項目はモデルを 1 回だけ読み込んで連続で処理する

出力と一緒に `manifest.json`（使ったシード・秒数・設定の一覧）も入る。

### 指定できるキー

`checkpoint` / `codec_repo` / `voice` / `caption` / `num_steps` / `seed` /
`cfg_scale_text` / `cfg_scale_caption` / `cfg_scale_speaker` / `cfg_guidance_mode` /
`duration_scale` / `t_schedule_mode` / `formats` / `max_chars` / `gap_ms` / `mp3_quality`

意味は [Irodori-TTS 本体の `infer.py`](https://github.com/Aratako/Irodori-TTS/blob/main/infer.py) と同じ。

---

## 声を登録する

`voices/` に音声ファイルを置いてコミットするだけ。詳細は [`voices/README.md`](voices/README.md)。

参照音声は**本人の同意があるもの、または自分に権利があるものだけ**を使うこと。

---

## 速度とコストの注意

**GitHub ホストのランナーは CPU のみ**なので、GPU で動かす場合に比べてかなり遅い。

- 既定の `Aratako/Irodori-TTS-v4.1-Small-MF` は MeanFlow 蒸留版で **サンプリング 4 ステップ**。
  CPU で回すならこれを使う。品質優先なら `Aratako/Irodori-TTS-v4.1-Small`（既定 40 ステップ）。
- 初回はモデルのダウンロードと PyTorch のインストールで数分かかる。
  2 回目以降は `~/.cache/huggingface` と uv のキャッシュが効いて短くなる。
- private リポジトリでは Actions の実行時間が課金対象。長文の一括生成を回す前に、
  まず短いテキストで所要時間を測っておく。
- もっと速くしたいなら **GPU のセルフホストランナー**を登録し、ワークフローの
  `runs-on: ubuntu-latest` を自分のランナーのラベルに変え、
  `.github/actions/setup-irodori` の `backend` を `cu128` にする。

---

## ローカルで動かす

```bash
git clone https://github.com/Aratako/Irodori-TTS.git vendor/Irodori-TTS
cd vendor/Irodori-TTS && uv sync --extra cu128 && cd ../..   # CPU なら --extra cpu

vendor/Irodori-TTS/.venv/bin/python scripts/synthesize.py \
  --spec jobs/example.yaml --outdir outputs --voices-dir voices
```

ジョブ定義だけ確かめたいときは、torch なしで検証できる。

```bash
python3 scripts/synthesize.py --spec jobs/example.yaml --dry-run
```

---

## 構成

```
.github/
  actions/setup-irodori/   Irodori-TTS のクローン・依存インストール・キャッシュ
  workflows/tts.yml        単発生成
  workflows/tts-batch.yml  一括生成
  workflows/ci.yml         軽い検証
scripts/
  synthesize.py            ジョブ定義を読んでまとめて合成する本体
  build_spec.py            ワークフロー入力から 1 件分のジョブ定義を作る
jobs/example.yaml          一括生成のサンプル
texts/sample.txt           text_file のサンプル
voices/                    参照音声の置き場
```

`vendor/Irodori-TTS` は実行のたびに取得するので、このリポジトリには含めない。
再現性が必要なら `upstream_ref` にコミット SHA を指定する。

---

## ライセンス

- このリポジトリのコード: MIT（[LICENSE](LICENSE)）
- Irodori-TTS 本体のコード: MIT
- **モデル重み**: ライセンスは Hugging Face のモデルカードに従う。
  生成した音声を配布・商用利用する前に必ず確認すること。
