#!/usr/bin/env python3
"""Irodori-TTS をジョブ定義ファイルからまとめて実行し、音声ファイルを書き出す。

GitHub Actions のランナー上で動かすことを想定しているが、ローカルでも同じように動く。

    .venv/bin/python scripts/synthesize.py --spec jobs/example.yaml --outdir outputs

ジョブ定義（YAML または JSON）:

    defaults:
      checkpoint: Aratako/Irodori-TTS-v4.1-Small-MF
      voice: null          # voices/ 配下のファイル名。null なら参照音声なし
      caption: null        # VoiceDesign 対応チェックポイント用の声の説明
      formats: [wav, mp3]
      max_chars: 120       # 1 チャンクあたりの最大文字数
      gap_ms: 300          # チャンク間に挟む無音の長さ
    items:
      - id: greeting
        text: |
          こんにちは。今日もおつかれさまです。
      - id: news
        text_file: texts/sample.txt
        voice: sample
        seed: 42

同じチェックポイントを使う項目はモデルを 1 回だけロードして連続で合成する。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 型チェック時だけ。実行時は下の _import_backend() で読み込む。
    import torch
    from irodori_tts.inference_runtime import InferenceRuntime, SamplingRequest

# torch と irodori_tts は数秒〜数十秒かかるうえ重い依存を引き込むので、
# 実際に合成するときだけ読み込む（--dry-run はジョブ定義の検証しかしない）。
_BACKEND_NAMES = (
    "InferenceRuntime",
    "RuntimeKey",
    "SamplingRequest",
    "default_runtime_device",
    "download_hf_checkpoint",
    "resolve_cfg_scales",
    "save_wav",
)


def _import_backend() -> None:
    import torch as torch_module
    import irodori_tts.inference_runtime as runtime_module

    globals()["torch"] = torch_module
    for name in _BACKEND_NAMES:
        globals()[name] = getattr(runtime_module, name)


# voices/ 配下で参照音声として受け付ける拡張子（上流の対応形式に合わせる）
REF_WAV_EXTS = (".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".webm")
REF_LATENT_EXTS = (".pt", ".pth")
REF_EMBED_EXTS = (".speaker.safetensors",)

# 文の切れ目。句点・感嘆符・疑問符は直後で切り、改行も区切りとして扱う。
SENTENCE_END = re.compile(r"(?<=[。．！？!?])|(?<=\n)")

DEFAULTS: dict[str, Any] = {
    "checkpoint": "Aratako/Irodori-TTS-v4.1-Small-MF",
    "codec_repo": "Aratako/Semantic-DACVAE-Japanese-32dim",
    "voice": None,
    "caption": None,
    "num_steps": None,
    "seed": None,
    "cfg_scale_text": 3.0,
    "cfg_scale_caption": 3.0,
    "cfg_scale_speaker": 5.0,
    "cfg_guidance_mode": "independent",
    "duration_scale": 1.0,
    "t_schedule_mode": "linear",
    "formats": ["wav"],
    "max_chars": 120,
    "gap_ms": 300,
    "mp3_quality": "2",
}

ITEM_ONLY_KEYS = {"id", "text", "text_file"}


class SpecError(RuntimeError):
    """ジョブ定義が不正なときに投げる。"""


# ---------------------------------------------------------------- spec 読み込み


def load_spec(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # irodori-tts の依存に含まれる

        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise SpecError(f"{path}: トップレベルはマッピングである必要がある。")
    return data


def normalize_items(spec: dict[str, Any], spec_dir: Path) -> list[dict[str, Any]]:
    defaults = dict(DEFAULTS)
    for key, value in (spec.get("defaults") or {}).items():
        if key in ITEM_ONLY_KEYS:
            raise SpecError(f"defaults に {key} は指定できない。")
        if key not in DEFAULTS:
            raise SpecError(f"defaults の未知のキー: {key}")
        defaults[key] = value

    items = spec.get("items")
    if not isinstance(items, list) or not items:
        raise SpecError("items に 1 件以上の項目が必要。")

    normalized: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise SpecError(f"items[{index}] はマッピングである必要がある。")
        for key in item:
            if key not in DEFAULTS and key not in ITEM_ONLY_KEYS:
                raise SpecError(f"items[{index}] の未知のキー: {key}")

        merged = dict(defaults)
        merged.update(item)

        if ("text" in item) == ("text_file" in item):
            raise SpecError(f"items[{index}] は text か text_file のどちらか一方を指定する。")
        if "text_file" in item:
            # ジョブ定義からの相対パスを優先し、無ければカレントディレクトリ基準で探す。
            raw_path = str(item["text_file"])
            candidates = [(spec_dir / raw_path).resolve(), Path(raw_path).resolve()]
            text_path = next((path for path in candidates if path.is_file()), None)
            if text_path is None:
                raise SpecError(f"items[{index}] のテキストファイルが無い: {raw_path}")
            merged["text"] = text_path.read_text(encoding="utf-8")

        merged["text"] = str(merged["text"]).strip()
        if not merged["text"]:
            raise SpecError(f"items[{index}] のテキストが空。")

        item_id = slugify(str(item.get("id") or f"item-{index:03d}"))
        if item_id in used_ids:
            raise SpecError(f"id が重複している: {item_id}")
        used_ids.add(item_id)
        merged["id"] = item_id

        formats = merged["formats"]
        if isinstance(formats, str):
            formats = [part.strip() for part in formats.split(",")]
        formats = [fmt.lower() for fmt in formats if fmt]
        unknown = set(formats) - {"wav", "mp3"}
        if unknown:
            raise SpecError(f"items[{index}] の未対応の形式: {', '.join(sorted(unknown))}")
        merged["formats"] = formats or ["wav"]

        normalized.append(merged)
    return normalized


def slugify(value: str) -> str:
    """ファイル名に使える形に落とす（日本語はそのまま残す）。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "-", value.strip())
    cleaned = cleaned.strip("-.")
    return cleaned or "item"


# ------------------------------------------------------------------ 参照音声


@dataclass
class Reference:
    ref_wavs: list[str] | None = None
    ref_latent: str | None = None
    ref_embed: str | None = None

    @property
    def no_ref(self) -> bool:
        return self.ref_wavs is None and self.ref_latent is None and self.ref_embed is None


def resolve_reference(voice: Any, voices_dir: Path) -> Reference:
    """voice 指定（None / 名前 / 相対パス / それらのリスト）を参照音声に解決する。"""
    if voice is None or voice == "" or voice == []:
        return Reference()

    names = voice if isinstance(voice, list) else [voice]
    paths = [resolve_voice_path(str(name), voices_dir) for name in names]

    if any(str(p).endswith(REF_EMBED_EXTS) for p in paths):
        if len(paths) > 1:
            raise SpecError("Speaker Inversion 埋め込みは 1 件だけ指定できる。")
        return Reference(ref_embed=str(paths[0]))
    if any(p.suffix.lower() in REF_LATENT_EXTS for p in paths):
        if len(paths) > 1:
            raise SpecError("参照 latent は 1 件だけ指定できる。")
        return Reference(ref_latent=str(paths[0]))
    return Reference(ref_wavs=[str(p) for p in paths])


def resolve_voice_path(name: str, voices_dir: Path) -> Path:
    direct = Path(name)
    if direct.is_file():
        return direct.resolve()

    candidate = voices_dir / name
    if candidate.is_file():
        return candidate.resolve()
    for ext in (*REF_WAV_EXTS, *REF_LATENT_EXTS, *REF_EMBED_EXTS):
        with_ext = voices_dir / f"{name}{ext}"
        if with_ext.is_file():
            return with_ext.resolve()

    available = sorted(p.name for p in voices_dir.glob("*") if p.is_file() and p.name != "README.md")
    hint = "、".join(available) if available else "（voices/ が空）"
    raise SpecError(f"参照音声が見つからない: {name} / 利用できるファイル: {hint}")


# -------------------------------------------------------------------- 合成本体


def split_text(text: str, max_chars: int) -> list[str]:
    """長文を文の切れ目でチャンクに分ける。max_chars<=0 なら分割しない。"""
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for sentence in (s.strip() for s in SENTENCE_END.split(text)):
        if not sentence:
            continue
        # 1 文だけで上限を超える場合は、その文をさらに機械的に割る。
        if len(sentence) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for start in range(0, len(sentence), max_chars):
                chunks.append(sentence[start : start + max_chars])
            continue
        if buffer and len(buffer) + len(sentence) > max_chars:
            chunks.append(buffer)
            buffer = sentence
        else:
            buffer = f"{buffer}{sentence}"
    if buffer:
        chunks.append(buffer)
    return chunks


def build_request(item: dict[str, Any], text: str, reference: Reference, runtime: InferenceRuntime) -> SamplingRequest:
    caption = item["caption"]
    caption = None if caption is None or str(caption).strip() == "" else str(caption)
    use_speaker = bool(runtime.model_cfg.use_speaker_condition_resolved and not reference.no_ref)
    use_caption = bool(runtime.model_cfg.use_caption_condition and caption is not None)

    if runtime.model_cfg.use_speaker_condition_resolved and reference.no_ref:
        print(f"[{item['id']}] 参照音声なしで合成する（話者はランダムになる）。", flush=True)
    if caption is not None and not runtime.model_cfg.use_caption_condition:
        print(f"[{item['id']}] このチェックポイントは caption 非対応のため無視する。", flush=True)

    cfg_text, cfg_caption, cfg_speaker, messages = resolve_cfg_scales(
        cfg_guidance_mode=str(item["cfg_guidance_mode"]),
        cfg_scale_text=float(item["cfg_scale_text"]),
        cfg_scale_caption=float(item["cfg_scale_caption"]),
        cfg_scale_speaker=float(item["cfg_scale_speaker"]),
        cfg_scale=None,
        use_caption_condition=use_caption,
        use_speaker_condition=use_speaker,
    )
    for message in messages:
        print(message, flush=True)

    return SamplingRequest(
        text=text,
        caption=caption,
        ref_wavs=reference.ref_wavs,
        ref_latent=reference.ref_latent,
        ref_embed=reference.ref_embed,
        no_ref=reference.no_ref,
        num_steps=None if item["num_steps"] is None else int(item["num_steps"]),
        seed=None if item["seed"] is None else int(item["seed"]),
        duration_scale=float(item["duration_scale"]),
        t_schedule_mode=str(item["t_schedule_mode"]),
        cfg_scale_text=cfg_text,
        cfg_scale_caption=cfg_caption,
        cfg_scale_speaker=cfg_speaker,
        cfg_guidance_mode=str(item["cfg_guidance_mode"]),
    )


def synthesize_item(item: dict[str, Any], runtime: InferenceRuntime, voices_dir: Path) -> tuple[torch.Tensor, int, list[int]]:
    reference = resolve_reference(item["voice"], voices_dir)
    chunks = split_text(str(item["text"]), int(item["max_chars"]))
    gap_ms = max(0, int(item["gap_ms"]))

    segments: list[torch.Tensor] = []
    seeds: list[int] = []
    sample_rate = 0
    for index, chunk in enumerate(chunks, start=1):
        print(f"[{item['id']}] {index}/{len(chunks)}: {chunk[:40]}...", flush=True)
        result = runtime.synthesize(build_request(item, chunk, reference, runtime), log_fn=None)
        audio = result.audio.detach().to(device="cpu", dtype=torch.float32)
        if audio.ndim == 1:
            audio = audio.unsqueeze(0)
        sample_rate = int(result.sample_rate)
        seeds.append(int(result.used_seed))
        if segments and gap_ms:
            gap = torch.zeros(audio.shape[0], int(sample_rate * gap_ms / 1000), dtype=audio.dtype)
            segments.append(gap)
        segments.append(audio)

    return torch.cat(segments, dim=-1), sample_rate, seeds


# -------------------------------------------------------------------- 出力処理


def to_mp3(wav_path: Path, quality: str) -> Path:
    mp3_path = wav_path.with_suffix(".mp3")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("mp3 出力には ffmpeg が必要。")
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(wav_path), "-codec:a", "libmp3lame", "-q:a", str(quality), str(mp3_path)],
        check=True,
    )
    return mp3_path


def write_summary(entries: list[dict[str, Any]]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["## 生成した音声", "", "| ID | 秒数 | 参照音声 | シード | ファイル |", "| --- | ---: | --- | --- | --- |"]
    for entry in entries:
        files = ", ".join(Path(f).name for f in entry["files"])
        voice = entry["voice"] or "（なし）"
        seeds = ", ".join(str(s) for s in entry["seeds"])
        lines.append(f"| {entry['id']} | {entry['duration_sec']} | {voice} | {seeds} | {files} |")
    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


# ------------------------------------------------------------------------ main


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Irodori-TTS でジョブ定義をまとめて音声化する。")
    parser.add_argument("--spec", required=True, help="ジョブ定義ファイル（.yaml / .yml / .json）")
    parser.add_argument("--outdir", default="outputs", help="出力先ディレクトリ")
    parser.add_argument("--voices-dir", default="voices", help="参照音声の置き場")
    parser.add_argument("--device", default=None, help="推論デバイス（既定は自動判定）")
    parser.add_argument("--precision", choices=["fp32", "bf16"], default="fp32", help="モデル・コーデックの精度")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ジョブ定義と参照音声の存在だけを検証し、合成はしない（モデルも torch も不要）。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec_path = Path(args.spec).resolve()
    if not spec_path.is_file():
        print(f"ジョブ定義が見つからない: {spec_path}", file=sys.stderr)
        return 1

    voices_dir = Path(args.voices_dir).resolve()

    try:
        items = normalize_items(load_spec(spec_path), spec_path.parent)
        if args.dry_run:
            for item in items:
                reference = resolve_reference(item["voice"], voices_dir)
                chunks = split_text(str(item["text"]), int(item["max_chars"]))
                ref_label = "参照なし" if reference.no_ref else str(item["voice"])
                print(
                    f"[{item['id']}] OK: {len(item['text'])} 文字 / {len(chunks)} チャンク"
                    f" / {ref_label} / {item['checkpoint']} / {','.join(item['formats'])}"
                )
            print(f"{len(items)} 件のジョブ定義を検証した。")
            return 0
    except SpecError as exc:
        print(f"ジョブ定義エラー: {exc}", file=sys.stderr)
        return 1

    _import_backend()
    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    device = args.device or default_runtime_device()

    # 同じチェックポイントの項目をまとめ、モデルのロードを 1 回で済ませる。
    items.sort(key=lambda item: (str(item["checkpoint"]), str(item["codec_repo"])))

    entries: list[dict[str, Any]] = []
    runtime: InferenceRuntime | None = None
    loaded_key: tuple[str, str] | None = None

    for item in items:
        key = (str(item["checkpoint"]), str(item["codec_repo"]))
        if runtime is None or key != loaded_key:
            checkpoint_path = download_hf_checkpoint(key[0])
            print(f"[model] {key[0]} -> {checkpoint_path} (device={device})", flush=True)
            runtime = InferenceRuntime.from_key(
                RuntimeKey(
                    checkpoint=str(checkpoint_path),
                    model_device=device,
                    codec_repo=key[1],
                    model_precision=args.precision,
                    codec_device=device,
                    codec_precision=args.precision,
                )
            )
            loaded_key = key

        try:
            audio, sample_rate, seeds = synthesize_item(item, runtime, voices_dir)
        except SpecError as exc:
            print(f"ジョブ定義エラー: {exc}", file=sys.stderr)
            return 1

        wav_path = save_wav(outdir / f"{item['id']}.wav", audio, sample_rate)
        files = [wav_path]
        if "mp3" in item["formats"]:
            files.append(to_mp3(wav_path, item["mp3_quality"]))
        if "wav" not in item["formats"]:
            wav_path.unlink()
            files.remove(wav_path)

        entry = {
            "id": item["id"],
            "text": item["text"],
            "checkpoint": item["checkpoint"],
            "voice": item["voice"],
            "caption": item["caption"],
            "seeds": seeds,
            "sample_rate": sample_rate,
            "duration_sec": round(audio.shape[-1] / sample_rate, 3),
            "files": [str(path.relative_to(outdir)) for path in files],
        }
        entries.append(entry)
        print(f"[{item['id']}] 完了: {', '.join(entry['files'])} ({entry['duration_sec']}s)", flush=True)

    manifest = outdir / "manifest.json"
    manifest.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"マニフェスト: {manifest}", flush=True)
    write_summary(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
