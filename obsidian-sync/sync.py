#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
取り込みBOX に置かれたファイルを Markdown に変換し、
Obsidian の保管庫（OneDrive 同期フォルダ）へ保存する。

対応形式: .md / .markdown / .txt / .docx / .zip / .pdf(任意)
標準ライブラリのみで動作する（.pdf のみ pypdf があれば対応）。

使い方:
    python sync.py                 通常実行
    python sync.py --dry-run       書き込まずに動作確認
    python sync.py --config 別のconfig.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Windows のコンソールでも日本語が化けないようにする
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / "state.json"
LOG_FILE = SCRIPT_DIR / "sync.log"

TEXT_EXT = {".md", ".markdown", ".txt"}
SUPPORTED_EXT = TEXT_EXT | {".docx", ".zip", ".pdf"}

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# --------------------------------------------------------------------------
# ログ
# --------------------------------------------------------------------------

def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# 設定・状態
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"設定ファイルが見つかりません: {path}\n"
            "config.example.json をコピーして config.json を作り、"
            "フォルダのパスを書き換えてください。"
        )
    with path.open(encoding="utf-8") as fh:
        config = json.load(fh)

    for key in ("inbox_dir", "vault_dir"):
        if not config.get(key):
            raise SystemExit(f"config.json の {key} が空です。")

    config["inbox_dir"] = Path(os.path.expandvars(config["inbox_dir"])).expanduser()
    config["vault_dir"] = Path(os.path.expandvars(config["vault_dir"])).expanduser()
    config.setdefault("subfolder", "Inbox/NotebookLM")
    config.setdefault("tags", ["notebooklm", "inbox"])
    config.setdefault("archive_inputs", True)
    config.setdefault("filename_template", "{date}_{title}")
    config.setdefault("max_title_length", 80)
    return config


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"version": 1, "hashes": {}}
    try:
        with STATE_FILE.open(encoding="utf-8") as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        log("state.json を読めなかったので作り直します。")
        return {"version": 1, "hashes": {}}
    state.setdefault("hashes", {})
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------
# テキスト抽出
# --------------------------------------------------------------------------

def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _para_text(node: ET.Element) -> str:
    parts = []
    for child in node.iter():
        tag = child.tag
        if tag == f"{{{W_NS}}}t":
            parts.append(child.text or "")
        elif tag in (f"{{{W_NS}}}tab",):
            parts.append("\t")
        elif tag in (f"{{{W_NS}}}br", f"{{{W_NS}}}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _table_markdown(tbl: ET.Element) -> str:
    rows = []
    for tr in tbl.findall(f"{{{W_NS}}}tr"):
        cells = []
        for tc in tr.findall(f"{{{W_NS}}}tc"):
            texts = [_para_text(p) for p in tc.findall(f"{{{W_NS}}}p")]
            cells.append(" ".join(t for t in texts if t).replace("|", "\\|"))
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def docx_to_markdown(path: Path) -> str:
    """python-docx を使わずに .docx から本文を取り出す。"""
    with zipfile.ZipFile(path) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    body = root.find(f"{{{W_NS}}}body")
    if body is None:
        return ""

    blocks: list[str] = []
    for child in body:
        if child.tag == f"{{{W_NS}}}p":
            text = _para_text(child)
            if not text:
                continue
            style = child.find(f"{{{W_NS}}}pPr/{{{W_NS}}}pStyle")
            style_val = style.get(f"{{{W_NS}}}val", "") if style is not None else ""
            match = re.match(r"(?:Heading|見出し)\s*(\d)", style_val)
            if match:
                level = min(int(match.group(1)), 6)
                blocks.append("#" * level + " " + text)
            else:
                blocks.append(text)
        elif child.tag == f"{{{W_NS}}}tbl":
            table = _table_markdown(child)
            if table:
                blocks.append(table)
    return "\n\n".join(blocks)


def pdf_to_markdown(path: Path) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(p for p in pages if p)


# --------------------------------------------------------------------------
# Markdown 組み立て
# --------------------------------------------------------------------------

def strip_existing_frontmatter(text: str) -> tuple[str, dict]:
    """すでに YAML frontmatter がある場合は取り除き、中身を辞書で返す。"""
    if not text.startswith("---"):
        return text, {}
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", text, re.DOTALL)
    if not match:
        return text, {}
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip("\"'")
    return text[match.end():], meta


def find_h1(text: str, scan_lines: int = 10) -> str | None:
    """先頭付近から見出し (# ...) を探す。

    NotebookLM の一括出力は先頭に引用形式の URL が入るため、
    1 行目だけでなく数行分を見る必要がある。
    """
    seen = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        seen += 1
        if seen > scan_lines:
            break
        if re.match(r"^#{1,2}\s+\S", stripped):
            return stripped.lstrip("#").strip()
    return None


def guess_title(text: str, meta_title: str, fallback: str) -> str:
    heading = find_h1(text)
    if heading:
        return heading
    if meta_title:
        return meta_title
    # NotebookLM の一括出力は "01-タイトル.md" の形になる
    cleaned = re.sub(r"^\d{1,3}[-_\s]+", "", fallback).strip()
    return cleaned or fallback


def yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_note(title: str, body: str, source_name: str,
               created: dt.date, tags: list[str]) -> str:
    lines = [
        "---",
        f"title: {yaml_quote(title)}",
        f"created: {created.isoformat()}",
        f"imported: {dt.date.today().isoformat()}",
        "source: NotebookLM",
        f"source_file: {yaml_quote(source_name)}",
        "tags:",
    ]
    lines += [f"  - {tag}" for tag in tags]
    lines.append("---")
    lines.append("")
    if find_h1(body) is None:
        lines.append(f"# {title}")
        lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# ファイル名
# --------------------------------------------------------------------------

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_filename(name: str, max_length: int) -> str:
    name = unicodedata.normalize("NFC", name)
    name = INVALID_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Obsidian のリンク記法と衝突する文字を避ける
    name = name.replace("[", "(").replace("]", ")").replace("#", "＃").replace("^", "＾")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or "untitled"


def unique_path(directory: Path, stem: str) -> Path:
    candidate = directory / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}.md"
        counter += 1
    return candidate


# --------------------------------------------------------------------------
# 入力の展開
# --------------------------------------------------------------------------

def extract_documents(path: Path, tmp_root: Path) -> list[tuple[str, str, dt.date]]:
    """(表示名, 本文, 日付) のリストを返す。zip は中身を展開して複数返す。"""
    suffix = path.suffix.lower()
    created = dt.date.fromtimestamp(path.stat().st_mtime)

    if suffix in TEXT_EXT:
        return [(path.name, read_text_file(path), created)]

    if suffix == ".docx":
        return [(path.name, docx_to_markdown(path), created)]

    if suffix == ".pdf":
        body = pdf_to_markdown(path)
        if body is None:
            log(f"  スキップ: {path.name} … PDF を読むには pypdf が必要です "
                "（コマンド: pip install pypdf）")
            return []
        return [(path.name, body, created)]

    if suffix == ".zip":
        results: list[tuple[str, str, dt.date]] = []
        work = Path(tempfile.mkdtemp(dir=tmp_root))
        try:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    inner = Path(info.filename)
                    if inner.suffix.lower() not in TEXT_EXT | {".docx"}:
                        continue
                    # zip 内の相対パスを無害化してから展開する
                    target = work / safe_filename(inner.name, 120)
                    with zf.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if target.suffix.lower() == ".docx":
                        body = docx_to_markdown(target)
                    else:
                        body = read_text_file(target)
                    results.append((inner.name, body, created))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return results

    return []


# --------------------------------------------------------------------------
# メイン処理
# --------------------------------------------------------------------------

def process(config: dict, dry_run: bool) -> int:
    inbox: Path = config["inbox_dir"]
    vault: Path = config["vault_dir"]
    out_dir = vault / config["subfolder"]

    if not inbox.exists():
        inbox.mkdir(parents=True, exist_ok=True)
        log(f"取り込みBOX を作成しました: {inbox}")

    if not vault.exists():
        log(f"エラー: Obsidian の保管庫が見つかりません → {vault}")
        log("  OneDrive の同期が完了しているか、config.json のパスが正しいか確認してください。")
        return 2

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    candidates = sorted(
        p for p in inbox.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT
    )
    if not candidates:
        log("取り込むファイルはありませんでした。")
        return 0

    state = load_state()
    hashes: dict = state["hashes"]
    written = skipped = 0

    tmp_root = Path(tempfile.mkdtemp(prefix="obsidian-sync-"))
    try:
        for src in candidates:
            log(f"処理中: {src.name}")
            try:
                documents = extract_documents(src, tmp_root)
            except (zipfile.BadZipFile, ET.ParseError, OSError) as exc:
                log(f"  読み取り失敗: {exc}")
                continue

            if not documents:
                continue

            produced_any = False
            for display_name, raw_body, created in documents:
                body, meta = strip_existing_frontmatter(raw_body)
                body = body.strip()
                if not body:
                    log(f"  スキップ: {display_name} … 中身が空でした。")
                    continue

                digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
                if digest in hashes:
                    log(f"  スキップ: {display_name} … 同じ内容が既にあります "
                        f"({hashes[digest]})")
                    skipped += 1
                    produced_any = True
                    continue

                title = guess_title(body, meta.get("title", ""),
                                    Path(display_name).stem)
                stem = config["filename_template"].format(
                    date=created.isoformat(),
                    title=safe_filename(title, config["max_title_length"]),
                )
                stem = safe_filename(stem, config["max_title_length"] + 20)
                target = unique_path(out_dir, stem)
                note = build_note(title, body, display_name, created, config["tags"])

                if dry_run:
                    log(f"  [確認のみ] 保存予定: {target}")
                else:
                    target.write_text(note, encoding="utf-8", newline="\n")
                    hashes[digest] = str(target.relative_to(vault))
                    log(f"  保存しました: {target.relative_to(vault)}")
                written += 1
                produced_any = True

            if produced_any and config["archive_inputs"] and not dry_run:
                archive = inbox / "_処理済み" / dt.date.today().strftime("%Y-%m")
                archive.mkdir(parents=True, exist_ok=True)
                moved = archive / src.name
                counter = 2
                while moved.exists():
                    moved = archive / f"{src.stem}-{counter}{src.suffix}"
                    counter += 1
                shutil.move(str(src), str(moved))
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if not dry_run:
        save_state(state)

    log(f"完了: {written} 件を保存 / {skipped} 件は重複のためスキップ")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="取り込みBOX のファイルを Obsidian 保管庫へ Markdown で保存します。"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="設定ファイルのパス（既定: config.json）")
    parser.add_argument("--dry-run", action="store_true",
                        help="実際には書き込まず、何が起きるかだけ表示する")
    args = parser.parse_args()

    config = load_config(args.config)
    return process(config, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
