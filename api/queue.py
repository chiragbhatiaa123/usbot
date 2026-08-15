"""
SQLite-based URL Queue for sequential processing.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# Database path - in db/ folder alongside existing db files
DB_PATH = Path(__file__).resolve().parent.parent / "db" / "url_queue.db"


def _get_connection() -> sqlite3.Connection:
    """Get a thread-local database connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# Thread-local storage for connections
_local = threading.local()


def get_db() -> sqlite3.Connection:
    """Get or create a thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _get_connection()
    return _local.conn


def init_db() -> None:
    """Initialize the queue database table."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS url_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            template_id TEXT,
            auto INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',
            workspace_id TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_queue_status ON url_queue(status)
    """)
    # Add auto column if missing (for existing databases)
    try:
        conn.execute("ALTER TABLE url_queue ADD COLUMN auto INTEGER DEFAULT 1")
    except Exception:
        pass  # Column already exists
    conn.commit()


def add_url(url: str, template_id: Optional[str] = None, auto: bool = True) -> int:
    """Add a URL to the queue. Returns the queue entry ID."""
    conn = get_db()
    cursor = conn.execute(
        """
        INSERT INTO url_queue (url, template_id, auto, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (url.strip(), template_id, 1 if auto else 0, datetime.utcnow().isoformat() + "Z"),
    )
    conn.commit()
    return cursor.lastrowid


def add_urls(urls: List[str], template_id: Optional[str] = None, auto: bool = True) -> List[int]:
    """Add multiple URLs to the queue. Returns list of queue entry IDs."""
    ids = []
    for url in urls:
        url = url.strip()
        if url:
            ids.append(add_url(url, template_id, auto))
    return ids


def get_next_pending() -> Optional[Dict[str, Any]]:
    """Get the next pending URL from the queue (FIFO)."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT id, url, template_id, auto, status, created_at
        FROM url_queue
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    if row:
        return dict(row)
    return None


def mark_processing(queue_id: int) -> bool:
    """Mark a queue entry as currently processing."""
    conn = get_db()
    cursor = conn.execute(
        """
        UPDATE url_queue
        SET status = 'processing', started_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (datetime.utcnow().isoformat() + "Z", queue_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_completed(queue_id: int, workspace_id: Optional[str] = None) -> bool:
    """Mark a queue entry as completed."""
    conn = get_db()
    cursor = conn.execute(
        """
        UPDATE url_queue
        SET status = 'completed', completed_at = ?, workspace_id = ?
        WHERE id = ?
        """,
        (datetime.utcnow().isoformat() + "Z", workspace_id, queue_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_failed(queue_id: int, error: str) -> bool:
    """Mark a queue entry as failed."""
    conn = get_db()
    cursor = conn.execute(
        """
        UPDATE url_queue
        SET status = 'failed', completed_at = ?, error = ?
        WHERE id = ?
        """,
        (datetime.utcnow().isoformat() + "Z", error, queue_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def list_queue(status_filter: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """List queue entries, optionally filtered by status."""
    conn = get_db()
    if status_filter:
        rows = conn.execute(
            """
            SELECT id, url, template_id, status, workspace_id, created_at, started_at, completed_at, error
            FROM url_queue
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (status_filter, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, url, template_id, status, workspace_id, created_at, started_at, completed_at, error
            FROM url_queue
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_queue_stats() -> Dict[str, int]:
    """Get count of entries by status."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT status, COUNT(*) as count
        FROM url_queue
        GROUP BY status
        """
    ).fetchall()
    return {row["status"]: row["count"] for row in rows}


def clear_completed() -> int:
    """Remove all completed entries from the queue. Returns count deleted."""
    conn = get_db()
    cursor = conn.execute("DELETE FROM url_queue WHERE status = 'completed'")
    conn.commit()
    return cursor.rowcount


def clear_all() -> int:
    """Remove ALL entries from the queue. Returns count deleted."""
    conn = get_db()
    cursor = conn.execute("DELETE FROM url_queue")
    conn.commit()
    return cursor.rowcount


# Initialize database on module import
init_db()

__all__ = [
    "add_url",
    "add_urls",
    "get_next_pending",
    "mark_processing",
    "mark_completed",
    "mark_failed",
    "list_queue",
    "get_queue_stats",
    "clear_completed",
    "clear_all",
]
