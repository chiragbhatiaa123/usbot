"""
Task helpers to invoke the orchestrator and broadcast workspace events.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional

from . import storage

BASE_DIR = Path(__file__).resolve().parent.parent
ORCHESTRATOR_PATH = BASE_DIR / "orchestrator.py"

LOG = logging.getLogger(__name__)


class WorkspaceEventBus:
    """Minimal in-memory event broadcaster for workspace updates."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, workspace_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.setdefault(workspace_id, []).append(q)
        return q

    def unsubscribe(self, workspace_id: str, q: queue.Queue) -> None:
        with self._lock:
            queues = self._subscribers.get(workspace_id)
            if not queues:
                return
            try:
                queues.remove(q)
            except ValueError:
                return
            if not queues:
                self._subscribers.pop(workspace_id, None)

    def publish(self, workspace_id: str, event: dict) -> None:
        with self._lock:
            queues = list(self._subscribers.get(workspace_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Drop the oldest event to make space, then re-queue.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    LOG.debug("Dropping workspace event for %s", workspace_id)


events = WorkspaceEventBus()


def _run_command(cmd: List[str]) -> subprocess.CompletedProcess:
    LOG.info("Running command: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        text=True,
        capture_output=True,
        check=False,
    )


def _read_workspace_from_emit(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("id")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Failed to parse workspace emit file %s: %s", path, exc)
        return None


def _publish_status_snapshot(ws_id: str) -> None:
    ws_path = storage.resolve_workspace(ws_id)
    statuses = storage.collect_status(ws_path)
    for step, data in statuses.items():
        events.publish(
            ws_id,
            {
                "type": "status",
                "workspace": ws_id,
                "step": step,
                "status": data.get("status"),
                "payload": data,
            },
        )


def run_orchestrator_for_url(
    url: str,
    template_id: Optional[str] = None,
    auto: bool = False,
) -> dict:
    """
    Invoke orchestrator.py for a new URL and return the resulting workspace metadata.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
        emit_path = Path(tmp.name)

    cmd = ["python", str(ORCHESTRATOR_PATH), "--url", url]
    if auto:
        cmd.append("--auto")
    if template_id:
        cmd.extend(["--template-id", template_id])
    cmd.extend(["--emit-json-workspace", str(emit_path)])

    proc = _run_command(cmd)

    workspace_id = _read_workspace_from_emit(emit_path)
    if workspace_id:
        ws_path = storage.resolve_workspace(workspace_id)
        storage.index_workspace(ws_path)
        _publish_status_snapshot(workspace_id)

    try:
        emit_path.unlink()
    except FileNotFoundError:
        pass
    except PermissionError:
        LOG.debug("Emit file still open: %s", emit_path)

    return {
        "workspace_id": workspace_id,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def process_workspace(
    workspace_id: str,
    template_id: Optional[str] = None,
    auto: bool = False,
) -> dict:
    """
    Re-run pipeline steps on an existing workspace.
    """
    import orchestrator  # local import to avoid circulars during startup

    ws_path = storage.resolve_workspace(workspace_id)
    if not ws_path.exists():
        raise FileNotFoundError(f"Workspace {workspace_id} not found at {ws_path}")

    result = orchestrator.process_single_workspace(
        ws_path,
        auto=auto,
        template_id=template_id,
    )
    storage.index_workspace(ws_path)
    _publish_status_snapshot(workspace_id)
    return result

"""
Add this function to api/tasks.py after the process_workspace function
"""

def clear_render_cache(workspace_id: str) -> None:
    """
    Clear cached renders for a workspace to force fresh rendering.
    Call this when new copy is submitted.
    """
    ws_path = storage.resolve_workspace(workspace_id)
    if not ws_path.exists():
        return

    render_dir = ws_path / "04_render"
    if not render_dir.exists():
        return

    templates_dir = render_dir / "templates"
    if templates_dir.exists():
        try:
            import shutil
            shutil.rmtree(templates_dir)
            LOG.info("Cleared render cache for workspace %s", workspace_id)
        except Exception as exc:
            LOG.warning("Failed to clear render cache for %s: %s", workspace_id, exc)


# Update the __all__ export at the bottom:
__all__ = ["events", "process_workspace", "run_orchestrator_for_url", "clear_render_cache"]


# __all__ = ["events", "process_workspace", "run_orchestrator_for_url"]
