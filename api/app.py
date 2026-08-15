"""
FastAPI application exposing Templatea workspace APIs.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, AnyHttpUrl, Field

from . import storage, template_registry, firebase_queue as url_queue
from .tasks import events as task_events
from .tasks import process_workspace, run_orchestrator_for_url
from orchestrator import write_status  # type: ignore
import logging
import threading

LOG = logging.getLogger(__name__)


API_PREFIX = "/api/v1"
API_KEY = os.getenv("API_KEY")

router = APIRouter(prefix=API_PREFIX)


@router.get("/templates")
async def list_templates() -> JSONResponse:
    return JSONResponse(template_registry.list_templates())

@router.get("/templates/{template_id}")
async def get_template(template_id: str) -> JSONResponse:
    try:
        return JSONResponse(template_registry.get_template(template_id))
    except template_registry.TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@router.get("/template-assets/{template_id}/{path:path}")
async def get_template_asset(template_id: str, path: str) -> Response:
    try:
        p = template_registry.asset_path(template_id, path)
    except Exception:
        raise HTTPException(status_code=404, detail="Asset not found")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    media_type, _ = mimetypes.guess_type(p.name)
    return FileResponse(str(p), media_type=media_type or "application/octet-stream")



def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


class ReelRequest(BaseModel):
    url: AnyHttpUrl
    auto: bool = False
    template_id: Optional[str] = Field(default=None, alias="template_id")


class ReelResponse(BaseModel):
    ok: bool
    workspace: Dict[str, Any]
    message: str


class ChoiceRequest(BaseModel):
    type: str
    text: str

    def validate(self) -> None:
        if self.type not in {"manual", "ai", "ocr"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid choice type")
        if not self.text or not self.text.strip():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Text cannot be empty")


def summarize_status(statuses: Dict[str, Dict[str, Any]]) -> Dict[str, Optional[str]]:
    return {step: info.get("status") for step, info in statuses.items()}


def file_url(workspace_id: str, key: str) -> str:
    return f"{API_PREFIX}/workspaces/{workspace_id}/files/{key}"


# --- in app.py ---
def serialize_files(workspace_id: str, detail: bool = False) -> Dict[str, Dict[str, Any]]:
    ws_path = storage.resolve_workspace(workspace_id)
    file_map = storage.list_workspace_files(ws_path)

    # NEW: resolve final from meta.render.path, not FILE_KEY_MAP
    meta = storage.read_meta(workspace_id) or {}
    final_entry: Dict[str, Any] = {"exists": False, "url": None}

    rel_final = None
    if meta.get("render", {}) and meta["render"].get("path"):
        rel_final = meta["render"]["path"]
        target = ws_path / rel_final
        if target.exists():
            final_entry["exists"] = True
            final_entry["url"] = file_url(workspace_id, "final")
            if detail:
                try:
                    stat = target.stat()
                    final_entry["size"] = stat.st_size
                    final_entry["modified_at"] = datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z"
                except Exception:
                    pass

    result: Dict[str, Dict[str, Any]] = {}
    for key, file_info in file_map.items():
        entry: Dict[str, Any] = {"exists": file_info["exists"], "url": None}
        if file_info["exists"]:
            entry["url"] = file_url(workspace_id, key)
            if detail:
                try:
                    stat = file_info["path"].stat()
                    entry["size"] = stat.st_size
                    entry["modified_at"] = datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z"
                except Exception:
                    pass
        result[key] = entry

    # Always expose the final render entry, even if legacy FILE_KEY_MAP did not include it.
    result["final"] = final_entry

    return result



def build_workspace_response(workspace: Dict[str, Any]) -> Dict[str, Any]:
    ws_id = workspace["id"]
    statuses = workspace.get("status") or {}
    files = serialize_files(ws_id)
    return {
        "id": ws_id,
        "created_at": workspace.get("created_at"),
        "meta": workspace.get("meta") or {},
        "status": summarize_status(statuses),
        "status_details": statuses,
        "files": {
            "thumb": files.get("thumb"),
            "final": files.get("final"),
        },
    }


def build_workspace_detail(workspace_id: str) -> Dict[str, Any]:
    ws = storage.get_workspace(workspace_id)
    if ws is None:
        # Attempt to index on the fly
        ws_path = storage.resolve_workspace(workspace_id)
        if not ws_path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
        storage.index_workspace(ws_path)
        ws = storage.get_workspace(workspace_id)
        if ws is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not indexed")
    response = {
        "id": ws["id"],
        "meta": ws.get("meta") or {},
        "status": summarize_status(ws.get("status") or {}),
        "status_details": ws.get("status") or {},
        "files": serialize_files(workspace_id, detail=True),
    }
    return response


def parse_range(range_header: Optional[str], file_size: int) -> Optional[tuple[int, int]]:
    if not range_header or "=" not in range_header:
        return None
    units, _, range_spec = range_header.partition("=")
    if units.strip().lower() != "bytes":
        return None
    start_str, _, end_str = range_spec.partition("-")
    try:
        if start_str == "":
            # bytes=-500 (last 500 bytes)
            length = int(end_str)
            start = max(file_size - length, 0)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        if start > end or end >= file_size:
            return None
        return start, end
    except ValueError:
        return None


def range_stream(path: Path, start: int, end: int, chunk_size: int = 1024 * 1024):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


# --- Queue Worker ---
_queue_worker_stop = threading.Event()


def _queue_worker_loop() -> None:
    """Background worker that polls the queue and processes URLs sequentially."""
    LOG.info("Queue worker started")
    while not _queue_worker_stop.is_set():
        try:
            entry = url_queue.get_next_pending()
            if entry is None:
                # No pending items, wait before polling again
                _queue_worker_stop.wait(timeout=5.0)
                continue

            queue_id = entry["id"]
            url = entry["url"]
            template_id = entry.get("template_id")
            auto = bool(entry.get("auto", 1))  # Default to True for backwards compatibility

            LOG.info(f"Queue worker processing: [{queue_id}] {url} (auto={auto})")
            
            # Mark as processing
            if not url_queue.mark_processing(queue_id):
                LOG.warning(f"Queue entry {queue_id} already being processed")
                continue

            try:
                # Run orchestrator with auto flag from queue entry
                result = run_orchestrator_for_url(
                    url,
                    template_id=template_id,
                    auto=auto,
                )
                
                workspace_id = result.get("workspace_id")
                if workspace_id:
                    url_queue.mark_completed(queue_id, workspace_id)
                    LOG.info(f"Queue entry {queue_id} completed: workspace={workspace_id}")
                    
                    # Upload to Firebase Storage
                    try:
                        ws_path = storage.resolve_workspace(workspace_id)
                        render_dir = ws_path / "04_render"
                        output_file = render_dir / "output.mp4"
                        
                        # If default output doesn't exist, search for final_*.mp4 recursively
                        if not output_file.exists():
                            candidates = list(render_dir.rglob("final_*.mp4"))
                            if candidates:
                                output_file = candidates[0]
                                LOG.info(f"Found output file: {output_file}")
                        
                        if output_file.exists():
                            LOG.info(f"Uploading output for {queue_id}...")
                            # Destination: outputs/{queue_id}.mp4
                            destination = f"outputs/{queue_id}.mp4"
                            link = url_queue.upload_video(str(output_file), destination)
                            
                            if link:
                                url_queue.set_output_link(queue_id, link)
                                LOG.info(f"Output uploaded: {link}")
                            else:
                                LOG.error("Upload failed (no link returned)")
                        else:
                            LOG.warning(f"Output file not found in {render_dir}")
                    except Exception as upload_exc:
                        LOG.error(f"Upload error: {upload_exc}")
                        
                else:
                    error_msg = result.get("stderr", "")[:500] or "No workspace created"
                    url_queue.mark_failed(queue_id, error_msg)
                    LOG.error(f"Queue entry {queue_id} failed: {error_msg}")

            except Exception as exc:
                url_queue.mark_failed(queue_id, str(exc)[:500])
                LOG.exception(f"Queue entry {queue_id} failed with exception")

        except Exception as exc:
            LOG.exception("Queue worker error (will retry)")
            _queue_worker_stop.wait(timeout=10.0)

    LOG.info("Queue worker stopped")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.ensure_db()
    storage.refresh_index_from_disk()
    
    # Start queue worker background thread
    _queue_worker_stop.clear()
    worker_thread = threading.Thread(target=_queue_worker_loop, daemon=True)
    worker_thread.start()
    LOG.info("Queue worker thread started")
    
    yield
    
    # Stop queue worker on shutdown
    _queue_worker_stop.set()
    worker_thread.join(timeout=5.0)
    LOG.info("Queue worker thread stopped")


@router.get("/workspaces")
async def list_workspaces(limit: int = 20, offset: int = 0, status_filter: Optional[str] = None) -> JSONResponse:
    rows = storage.list_workspaces(limit=limit, offset=offset, status_filter=status_filter)
    items = []
    for row in rows:
        items.append(build_workspace_response(row))
    return JSONResponse(items)


@router.get("/workspaces/{workspace_id}")
async def get_workspace_detail(workspace_id: str) -> JSONResponse:
    detail = build_workspace_detail(workspace_id)
    return JSONResponse(detail)

@router.get("/workspaces/{workspace_id}/renders")
async def list_renders(workspace_id: str) -> JSONResponse:
    ws = storage.get_workspace(workspace_id)
    if ws is None:
        ws_path = storage.resolve_workspace(workspace_id)
        if not ws_path.exists():
            raise HTTPException(status_code=404, detail="Workspace not found")
        storage.index_workspace(ws_path)
        ws = storage.get_workspace(workspace_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="Workspace not indexed")
    meta = ws.get("meta") or {}
    renders = meta.get("renders") or {}
    out = []
    for tid, info in renders.items():
        rel = info.get("path")
        exists = bool(rel) and (storage.resolve_workspace(workspace_id) / rel).exists()
        out.append({
            "template_id": tid,
            "exists": exists,
            "url": f"{API_PREFIX}/workspaces/{workspace_id}/render/{tid}" if exists else None,
            "text": info.get("text"),
            "text_hash": info.get("text_hash"),
            "options_signature": info.get("options_signature"),
            "ts": info.get("ts"),
        })
    return JSONResponse(out)

@router.get("/workspaces/{workspace_id}/render/{template_id}")
async def get_render_variant(workspace_id: str, template_id: str, request: Request) -> Response:
    ws_path = storage.resolve_workspace(workspace_id)
    meta = storage.read_meta(workspace_id) or {}
    info = (meta.get("renders") or {}).get(template_id)
    if not info or not info.get("path"):
        raise HTTPException(status_code=404, detail="Render variant not found")
    target = ws_path / info["path"]
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Render file missing")
    file_size = target.stat().st_size
    rng = request.headers.get("range")
    rng_tuple = parse_range(rng, file_size)
    media_type, _ = mimetypes.guess_type(target.name)
    if rng_tuple:
        start, end = rng_tuple
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(
            range_stream(target, start, end),
            media_type=media_type or "application/octet-stream",
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
        )
    return FileResponse(str(target), media_type=media_type or "application/octet-stream", filename=target.name)


@router.get("/workspaces/{workspace_id}/files/{file_key}")
@router.get("/workspaces/{workspace_id}/files/{file_key}")
@router.get("/workspaces/{workspace_id}/files/{file_key}")
async def get_workspace_file(workspace_id: str, file_key: str, request: Request) -> Response:
    ws_path = storage.resolve_workspace(workspace_id)

    if file_key == "final":
        meta = storage.read_meta(workspace_id) or {}
        rel = (meta.get("render") or {}).get("path")
        if not rel:
            raise HTTPException(status_code=404, detail="Final not available")
        target = ws_path / rel
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        file_size = target.stat().st_size
        rng = request.headers.get("range")
        rng_tuple = parse_range(rng, file_size)
        media_type, _ = mimetypes.guess_type(target.name)
        if rng_tuple:
            start, end = rng_tuple
            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(end - start + 1),
            }
            return StreamingResponse(
                range_stream(target, start, end),
                media_type=media_type or "application/octet-stream",
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                headers=headers,
            )
        return FileResponse(str(target), media_type=media_type or "application/octet-stream", filename=target.name)

    # existing behavior for other keys…
    if file_key not in storage.FILE_KEY_MAP:
        raise HTTPException(status_code=404, detail="Unknown file key")
    # ... your current logic continues here


    # default behavior for other keys (unchanged)
    if file_key not in storage.FILE_KEY_MAP:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown file key")
    target = ws_path / storage.FILE_KEY_MAP[file_key]
    # original logic continues...

    if file_key not in storage.FILE_KEY_MAP:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown file key")

    ws_path = storage.resolve_workspace(workspace_id)
    target = ws_path / storage.FILE_KEY_MAP[file_key]

    if file_key == "logs":
        if not target.exists() or not target.is_dir():
            return JSONResponse([])
        entries = []
        for log in sorted(target.glob("*")):
            entries.append(
                {
                    "name": log.name,
                    "size": log.stat().st_size,
                    "modified_at": datetime.utcfromtimestamp(log.stat().st_mtime).isoformat() + "Z",
                }
            )
        return JSONResponse(entries)

    if not target.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if target.is_dir():
        entries = [child.name for child in sorted(target.iterdir())]
        return JSONResponse(entries)

    file_size = target.stat().st_size
    range_header = request.headers.get("range")
    range_tuple = parse_range(range_header, file_size)
    media_type, _ = mimetypes.guess_type(target.name)

    if range_tuple:
        start, end = range_tuple
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(
            range_stream(target, start, end),
            media_type=media_type or "application/octet-stream",
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
        )

    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        filename=target.name,
    )


@router.post("/reels", response_model=ReelResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(verify_api_key)])
async def create_reel(request: ReelRequest) -> ReelResponse:
    if request.template_id:
        try:
            template_registry.get_template(request.template_id)
        except template_registry.TemplateNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    result = await asyncio.to_thread(
        run_orchestrator_for_url,
        str(request.url),
        request.template_id,
        request.auto,
    )
    if result["workspace_id"] is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Orchestrator failed to create workspace",
                "returncode": result.get("returncode"),
                "stdout": result.get("stdout"),
                "stderr": result.get("stderr"),
            },
        )

    workspace_detail = build_workspace_detail(result["workspace_id"])

    return ReelResponse(
        ok=True,
        workspace=workspace_detail,
        message="orchestrator started",
    )


def _write_choice_file(workspace_id: str, body: ChoiceRequest) -> None:
    ws_path = storage.resolve_workspace(workspace_id)
    choice_dir = ws_path / "03_choice"
    choice_dir.mkdir(parents=True, exist_ok=True)
    choice_file = choice_dir / "choice.txt"
    manual_file = choice_dir / "manual.txt"

    if body.type == "manual":
        manual_file.write_text(body.text.strip(), encoding="utf-8")
    else:
        if manual_file.exists():
            manual_file.unlink()
    choice_file.write_text(body.text.strip(), encoding="utf-8")


async def _reprocess_workspace(workspace_id: str) -> None:
    try:
        await asyncio.to_thread(process_workspace, workspace_id, None, False)
    except Exception:
        # logging handled by tasks module
        pass


"""
Replace the update_choice function in api/app.py with this version
"""

@router.post(
    "/workspaces/{workspace_id}/choice",
    dependencies=[Depends(verify_api_key)],
)
async def update_choice(workspace_id: str, body: ChoiceRequest, background: BackgroundTasks) -> JSONResponse:
    body.validate()
    ws_path = storage.resolve_workspace(workspace_id)
    if not ws_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")

    _write_choice_file(workspace_id, body)
    # Immediately reflect that a fresh render is pending so clients do not pick up stale outputs.
    try:
        write_status(ws_path, "04_render", "pending", extra={"reason": "choice_submitted"})
    except Exception:
        # best-effort; downstream steps will still attempt rendering
        pass
    
    # IMPORTANT: Clear render cache to force fresh render with new copy
    from .tasks import clear_render_cache
    clear_render_cache(workspace_id)
    
    storage.index_workspace(ws_path)
    background.add_task(_reprocess_workspace, workspace_id)

    return JSONResponse({"ok": True, "queued": True})


@router.get("/templates")
async def list_templates() -> JSONResponse:
    manifests = template_registry.list_templates()
    return JSONResponse(manifests)

# List all renders (stable, URL-only, no paths leaked)
@router.get("/workspaces/{workspace_id}/renders")
async def list_renders(workspace_id: str) -> JSONResponse:
    ws = storage.get_workspace(workspace_id)
    if ws is None:
        ws_path = storage.resolve_workspace(workspace_id)
        if not ws_path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
        storage.index_workspace(ws_path)
        ws = storage.get_workspace(workspace_id)
        if ws is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not indexed")

    meta = ws.get("meta") or {}
    renders = meta.get("renders") or {}
    items = []
    for template_id, info in renders.items():
        rel = info.get("path")
        exists = False
        if rel:
            exists = (storage.resolve_workspace(workspace_id) / rel).exists()
        items.append({
            "template_id": template_id,
            "exists": bool(exists),
            "url": f"{API_PREFIX}/workspaces/{workspace_id}/render/{template_id}" if exists else None,
            "text": info.get("text"),
            "text_hash": info.get("text_hash"),
            "options_signature": info.get("options_signature"),
            "template_options": info.get("template_options"),
            "ts": info.get("ts"),
        })
    return JSONResponse(items)


# Stream a specific template's render
@router.get("/workspaces/{workspace_id}/render/{template_id}")
async def get_render_variant(workspace_id: str, template_id: str, request: Request) -> Response:
    ws_path = storage.resolve_workspace(workspace_id)
    meta = storage.read_meta(workspace_id) or {}
    info = (meta.get("renders") or {}).get(template_id)
    if not info or not info.get("path"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Render variant not found")
    target = ws_path / info["path"]
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Render file missing")
    file_size = target.stat().st_size
    range_header = request.headers.get("range")
    range_tuple = parse_range(range_header, file_size)
    media_type, _ = mimetypes.guess_type(target.name)
    if range_tuple:
        start, end = range_tuple
        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(
            range_stream(target, start, end),
            media_type=media_type or "application/octet-stream",
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
        )
    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        filename=target.name,
    )


@router.get("/templates/{template_id}")
async def get_template(template_id: str) -> JSONResponse:
    try:
        manifest = template_registry.get_template(template_id)
    except template_registry.TemplateNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return JSONResponse(manifest)


async def websocket_status_poller(ws: WebSocket, workspace_id: str) -> None:
    last_snapshot: Dict[str, Optional[str]] = {}
    try:
        while True:
            await asyncio.sleep(1)
            ws_path = storage.resolve_workspace(workspace_id)
            statuses = summarize_status(storage.collect_status(ws_path))
            if statuses != last_snapshot:
                await ws.send_json(
                    {
                        "type": "status_snapshot",
                        "workspace": workspace_id,
                        "status": statuses,
                    }
                )
                last_snapshot = statuses
    except asyncio.CancelledError:
        pass


async def websocket_event_forwarder(ws: WebSocket, workspace_id: str, q) -> None:
    try:
        while True:
            event = await asyncio.to_thread(q.get)
            await ws.send_json(event)
    except asyncio.CancelledError:
        pass


# Create APP instance BEFORE using @APP decorators
APP = FastAPI(title="Templatea Backend", version="1.0.0", lifespan=lifespan)

APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

APP.include_router(router)


@APP.websocket("/ws/workspace/{workspace_id}")
async def workspace_socket(websocket: WebSocket, workspace_id: str) -> None:
    provided_key = websocket.query_params.get("api_key")
    if API_KEY and provided_key != API_KEY:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    queue = task_events.subscribe(workspace_id)
    poller = asyncio.create_task(websocket_status_poller(websocket, workspace_id))
    forwarder = asyncio.create_task(websocket_event_forwarder(websocket, workspace_id, queue))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        poller.cancel()
        forwarder.cancel()
        task_events.unsubscribe(workspace_id, queue)
        await asyncio.gather(poller, forwarder, return_exceptions=True)


__all__ = ["APP"]

