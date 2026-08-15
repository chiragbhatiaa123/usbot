"""
Storage helpers for Templatea backend.

Provides helpers to read workspace metadata, enumerate artifacts, and
maintain a lightweight SQLite index for fast queries.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

WORKSPACE_BASE = Path(os.getenv("WORKSPACE_BASE", "workspace"))
DB_PATH = Path(os.getenv("WORKSPACE_INDEX_DB", "db/workspaces.sqlite"))

# Map the API file keys to relative paths inside a workspace.
FILE_KEY_MAP: Dict[str, Path] = {
    "raw": Path("00_raw/raw_source.mp4"),
    "raw_caption": Path("00_raw/raw_caption.txt"),
    "raw_meta": Path("00_raw/raw_meta.json.xz"),
    "thumb": Path("00_raw/raw_thumb.jpg"),
    "cropped": Path("01_detector/cropped.mp4"),
    "detector_status": Path("01_detector.status"),
    "ocr": Path("02_ocr/ocr.txt"),
    "caption": Path("02_ocr/caption.txt"),
    "ai_copies": Path("02_ocr/ai_copies.json"),
    "gemini_report": Path("02_ocr/gemini_report.json"),
    "ocr_status": Path("02_ocr.status"),
    "choice": Path("03_choice/choice.txt"),
    "manual": Path("03_choice/manual.txt"),
    "choice_status": Path("03_choice.status"),
    # "final": Path("04_render/final_1080x1920.mp4"), No longer used by API
    # "render_status": Path("04_render.status"),
    "render_status": Path("04_render.status"),
    "logs": Path("logs"),
}

# Status files live at the workspace root with suffix ".status"
STATUS_SUFFIX = ".status"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def resolve_workspace(workspace_id: str) -> Path:
    return WORKSPACE_BASE / workspace_id


def read_meta(workspace_id: str) -> Optional[dict]:
    ws_path = resolve_workspace(workspace_id)
    return _read_json(ws_path / "meta.json")


def write_meta(workspace_id: str, meta: dict) -> None:
    ws_path = resolve_workspace(workspace_id)
    _atomic_write_json(ws_path / "meta.json", meta)


def read_status_file(ws_path: Path, step_name: str) -> Optional[dict]:
    path = ws_path / f"{step_name}{STATUS_SUFFIX}"
    return _read_json(path)


def collect_status(ws_path: Path) -> Dict[str, dict]:
    statuses: Dict[str, dict] = {}
    if not ws_path.exists():
        return statuses
    for item in ws_path.glob(f"*{STATUS_SUFFIX}"):
        step = item.stem
        data = _read_json(item)
        if data is not None:
            statuses[step] = data
    return statuses


def list_workspace_files(ws_path: Path) -> Dict[str, dict]:
    """
    Return a mapping of file_key -> {"exists": bool, "path": Path}
    The API layer will convert paths to URLs where appropriate.
    """
    files: Dict[str, dict] = {}
    for key, rel_path in FILE_KEY_MAP.items():
        abs_path = ws_path / rel_path
        files[key] = {"exists": abs_path.exists(), "path": abs_path}
    return files


def ensure_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            created_at TEXT,
            meta_json TEXT,
            status_json TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def _get_conn() -> sqlite3.Connection:
    return ensure_db()


def index_workspace(ws_path: Path) -> None:
    if not ws_path.exists():
        return
    workspace_id = ws_path.name
    meta = _read_json(ws_path / "meta.json") or {}
    statuses = collect_status(ws_path)

    created_at = meta.get("created_at")
    if not created_at:
        try:
            created_at = datetime.utcfromtimestamp(ws_path.stat().st_ctime).replace(microsecond=0).isoformat() + "Z"
        except Exception:
            created_at = _utc_now_iso()

    payload = {
        "id": workspace_id,
        "created_at": created_at,
        "meta_json": json.dumps(meta, ensure_ascii=False),
        "status_json": json.dumps(statuses, ensure_ascii=False),
        "updated_at": _utc_now_iso(),
    }

    conn = _get_conn()
    with conn:
        conn.execute(
            """
            INSERT INTO workspaces (id, created_at, meta_json, status_json, updated_at)
            VALUES (:id, :created_at, :meta_json, :status_json, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
                created_at = COALESCE(workspaces.created_at, excluded.created_at),
                meta_json = excluded.meta_json,
                status_json = excluded.status_json,
                updated_at = excluded.updated_at
            """,
            payload,
        )


def list_workspaces(limit: int = 20, offset: int = 0, status_filter: Optional[str] = None) -> List[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM workspaces ORDER BY created_at DESC").fetchall()

    items: List[dict] = []
    for row in rows:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
        status = json.loads(row["status_json"]) if row["status_json"] else {}
        if status_filter and status.get("04_render", {}).get("status") != status_filter:
            continue
        items.append(
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "meta": meta,
                "status": status,
                "updated_at": row["updated_at"],
            }
        )

    return items[offset : offset + limit]


def get_workspace(workspace_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "meta": json.loads(row["meta_json"]) if row["meta_json"] else {},
        "status": json.loads(row["status_json"]) if row["status_json"] else {},
    }


def delete_workspace(workspace_id: str) -> None:
    conn = _get_conn()
    with conn:
        conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))


def refresh_index_from_disk() -> None:
    if not WORKSPACE_BASE.exists():
        return
    for ws_path in WORKSPACE_BASE.iterdir():
        if ws_path.is_dir():
            index_workspace(ws_path)


__all__ = [
    "FILE_KEY_MAP",
    "collect_status",
    "delete_workspace",
    "ensure_db",
    "get_workspace",
    "index_workspace",
    "list_workspaces",
    "list_workspace_files",
    "read_meta",
    "read_status_file",
    "resolve_workspace",
    "write_meta",
    "refresh_index_from_disk",
]
