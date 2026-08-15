# downloader_mapper.py
"""
Move/normalize downloader outputs into a canonical workspace.

Usage (from instagram_downloader.py after a downloader run):
    from downloader_mapper import map_and_move_downloaded_files
    ws = map_and_move_downloaded_files(
            downloaded_dir="/tmp/instaloader_out_123",
            dest_base="workspace",
            source_url=url,
            downloader_name="instaloader",
            keep_originals=False
         )
    # ws is a pathlib.Path to the created workspace

Behavior:
- Picks canonical id (prefers shortcode from URL, then shortcode-like token in filenames,
  then instaloader timestamp format, then uuid4()).
- Creates workspace/<id>/00_raw/ and moves files there with deterministic names:
    raw_source.mp4, raw_caption.txt, raw_meta.json.xz, raw_thumb.jpg, ...
- Writes workspace/<id>/meta.json recording original filenames, downloader, sha256.
- Returns Path(workspace/<id>).
"""

import re
import json
import os
import shutil
import hashlib
from pathlib import Path
from uuid import uuid4
from datetime import datetime
from typing import Optional, Dict, Any

SHORTCODE_RE = re.compile(r"([A-Za-z0-9_-]{6,})")
INSTALOADER_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_UTC$")

# mapping of known suffix combos to our canonical names
EXT_MAP = {
    (".mp4",): "raw_source.mp4",
    (".mov",): "raw_source.mov",
    (".jpg",): "raw_thumb.jpg",
    (".jpeg",): "raw_thumb.jpg",
    (".png",): "raw_thumb.png",
    (".txt",): "raw_caption.txt",
    (".json",): "raw_meta.json",
    (".xz",): "raw_meta.json.xz",
    (".json", ".xz"): "raw_meta.json.xz",
    (".json.xz",): "raw_meta.json.xz",
}

def _sha256(path: Path, chunk_size: int = 65536) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

def _normalize_suffixes(p: Path):
    # return suffix tuple in order, e.g. ('.json', '.xz') or ('.mp4',)
    suffs = tuple([s.lower() for s in p.suffixes]) or (p.suffix.lower(),)
    # handle case where suffixes empty: return ('.',) but we avoid that
    return suffs

def parse_shortcode_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"(?:/p/|/reel/|/tv/|/reels/|/video/)([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)
    # fallback: any alnum token in URL path
    m2 = SHORTCODE_RE.search(url)
    return m2.group(1) if m2 else None

def parse_downloader_id_from_filename(fn: str) -> Optional[str]:
    name = Path(fn).stem
    if INSTALOADER_TS_RE.match(name):
        return name
    m = SHORTCODE_RE.search(name)
    if m:
        return m.group(1)
    return None

def make_canonical_id(source_url: Optional[str], filenames: Optional[list]) -> str:
    # 1) shortcode from URL
    sc = parse_shortcode_from_url(source_url)
    if sc:
        return sc
    # 2) any shortcode-like token from filenames
    if filenames:
        for fn in filenames:
            pid = parse_downloader_id_from_filename(fn)
            if pid:
                return pid
    # 3) instaloader-ish timestamp fallback
    ts = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S_UTC")
    return ts

def _find_next_ws(base: Path, canonical_id: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    candidate = base / canonical_id
    if not candidate.exists():
        return candidate
    # add _v2, _v3 ...
    i = 2
    while True:
        c = base / f"{canonical_id}_v{i}"
        if not c.exists():
            return c
        i += 1

def atomic_write_json(path: Path, data: Dict[str, Any]):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def map_and_move_downloaded_files(
    downloaded_dir: str,
    dest_base: str = "workspace",
    source_url: Optional[str] = None,
    downloader_name: Optional[str] = None,
    keep_originals: bool = False,
) -> Path:
    """
    Move files from `downloaded_dir` into a namespaced workspace.
    Returns the Path to the workspace (e.g. workspace/<id>).
    """
    downloaded = Path(downloaded_dir)
    if not downloaded.exists():
        raise FileNotFoundError(f"downloaded_dir not found: {downloaded_dir}")

    found = [p for p in downloaded.iterdir() if p.is_file()]
    filenames = [p.name for p in found]

    canonical_id = make_canonical_id(source_url, filenames)
    ws = _find_next_ws(Path(dest_base), canonical_id)
    raw_dir = ws / "00_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "canonical_id": canonical_id,
        "source_url": source_url,
        "downloader_hint": downloader_name,
        "original_files": {},
        "created_at": datetime.utcnow().isoformat() + "Z",
        "steps": {},
    }

    # move & map files
    for p in found:
        suffs = _normalize_suffixes(p)
        mapped_name = None
        # try exact match on suffix tuple
        for key, val in EXT_MAP.items():
            # match when suffixes exactly equal or join matches
            if tuple(key) == tuple(suffs) or "".join(suffs) == "".join(key):
                mapped_name = val
                break
        # fallback heuristics
        if mapped_name is None:
            low = p.suffix.lower()
            if low in (".mp4", ".mov"):
                mapped_name = f"raw_source{low}"
            elif low == ".txt":
                mapped_name = "raw_caption.txt"
            elif low in (".jpg", ".jpeg", ".png"):
                mapped_name = f"raw_thumb{low}"
            else:
                mapped_name = f"raw_other{''.join(suffs) or low}"

        dest_path = raw_dir / mapped_name
        # avoid overwrites: version existing destination
        if dest_path.exists():
            i = 2
            stem = dest_path.stem
            suffix = dest_path.suffix
            while True:
                cand = raw_dir / f"{stem}_v{i}{suffix}"
                if not cand.exists():
                    dest_path = cand
                    break
                i += 1

        # move or copy to dest
        if keep_originals:
            shutil.copy2(p, dest_path)
        else:
            # move keeps file timestamps etc
            shutil.move(str(p), str(dest_path))

        file_info = {"original_filename": p.name, "downloader": downloader_name}
        sha = _sha256(dest_path)
        if sha:
            file_info["sha256"] = sha
        try:
            file_info["size"] = dest_path.stat().st_size
        except Exception:
            file_info["size"] = None

        meta["original_files"][dest_path.name] = file_info

    # if keep_originals and downloaded dir still has files, optionally move them into raw/original_downloader_output
    if keep_originals:
        orig_bucket = raw_dir / "original_downloader_output"
        orig_bucket.mkdir(exist_ok=True)
        for p in downloaded.iterdir():
            if p.is_file():
                # don't overwrite originals if exist
                dest = orig_bucket / p.name
                if not dest.exists():
                    shutil.move(str(p), str(dest))

    # write meta.json atomically
    atomic_write_json(ws / "meta.json", meta)
    return ws

# Quick CLI for debugging
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Map downloader outputs into standardized workspace.")
    ap.add_argument("downloaded_dir", help="Directory containing downloader output files")
    ap.add_argument("--dest", default="workspace", help="Workspace base folder")
    ap.add_argument("--url", default=None, help="Source URL (optional)")
    ap.add_argument("--downloader", default=None, help="Downloader name (instaloader|yt-dlp)")
    ap.add_argument("--keep-originals", action="store_true", help="Copy originals into workspace instead of moving")
    args = ap.parse_args()
    ws_path = map_and_move_downloaded_files(
        downloaded_dir=args.downloaded_dir,
        dest_base=args.dest,
        source_url=args.url,
        downloader_name=args.downloader,
        keep_originals=args.keep_originals,
    )
    print(f"Workspace created at: {ws_path}")
