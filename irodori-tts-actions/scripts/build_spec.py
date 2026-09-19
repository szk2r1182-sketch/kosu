#!/usr/bin/env python3
"""workflow_dispatch の入力（環境変数）から 1 件分のジョブ定義 JSON を組み立てる。

入力を ${{ }} でシェルに埋め込まずに環境変数として受け取るため、テキストに
引用符やバッククォートが含まれていても安全に扱える。標準ライブラリだけで動く
ので、Irodori-TTS の環境を作る前のランナーの Python でも実行できる。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# 単一行しか入力できない workflow_dispatch のテキスト欄で改行を表すエスケープ。
NEWLINE_ESCAPES = (("\\n", "\n"), ("\\t", "\t"))


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def optional_int(name: str) -> int | None:
    raw = env(name)
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} には整数を指定する: {raw!r}")


def unescape(text: str) -> str:
    for escaped, actual in NEWLINE_ESCAPES:
        text = text.replace(escaped, actual)
    return text


def main() -> None:
    text = unescape(os.environ.get("TTS_TEXT") or "").strip()
    if not text:
        sys.exit("TTS_TEXT が空。読み上げるテキストを指定する。")

    formats = [part.strip() for part in env("TTS_FORMATS", "wav").split(",") if part.strip()]
    voices = [part.strip() for part in env("TTS_VOICE").split(",") if part.strip()]

    item: dict[str, Any] = {
        "id": env("TTS_NAME", "speech"),
        "text": text,
        "voice": voices[0] if len(voices) == 1 else (voices or None),
        "caption": env("TTS_CAPTION") or None,
        "num_steps": optional_int("TTS_NUM_STEPS"),
        "seed": optional_int("TTS_SEED"),
        "formats": formats or ["wav"],
    }
    max_chars = optional_int("TTS_MAX_CHARS")
    if max_chars is not None:
        item["max_chars"] = max_chars

    spec = {
        "defaults": {"checkpoint": env("TTS_CHECKPOINT", "Aratako/Irodori-TTS-v4.1-Small-MF")},
        "items": [item],
    }

    out_path = env("TTS_SPEC_OUT", "job.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(spec, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"ジョブ定義を書き出した: {out_path}")
    print(json.dumps(spec, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
