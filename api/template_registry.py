# api/template_registry.py
from __future__ import annotations
import importlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "templates"

class TemplateNotFound(Exception): ...
class TemplateManifestError(Exception): ...

_cache: Dict[str, Dict[str, Any]] = {}

def _manifest_path_from_dir(d: Path) -> Path:
    return d / "template.json"

def _validate_manifest(m: Dict[str, Any], folder: Path) -> Dict[str, Any]:
    required_keys = ["id", "name", "renderer"]
    for k in required_keys:
        if k not in m:
            raise TemplateManifestError(f"{folder.name}: missing key '{k}'")
    r = m["renderer"]
    if not isinstance(r, dict) or "module" not in r or "entrypoint" not in r:
        raise TemplateManifestError(f"{folder.name}: renderer must have 'module' and 'entrypoint'")
    m["_folder"] = str(folder)
    return m

def _discover() -> None:
    _cache.clear()
    if not TEMPLATES_ROOT.exists():
        return
    for d in sorted(TEMPLATES_ROOT.iterdir()):
        if not d.is_dir():
            continue
        manifest_path = _manifest_path_from_dir(d)
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = _validate_manifest(manifest, d)
            tid = manifest["id"]
            _cache[tid] = manifest
        except Exception as exc:
            # Skip bad templates; keep the rest usable
            print(f"[template_registry] skip {d.name}: {exc}")

def refresh() -> None:
    _discover()

def list_templates() -> List[Dict[str, Any]]:
    if not _cache:
        _discover()
    # Safe public view (no absolute paths)
    out = []
    for m in _cache.values():
        out.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "canvas": m.get("canvas"),
            "defaults": m.get("defaults", {}),
            "assets_base": f"/api/v1/template-assets/{m['id']}"
        })
    return out

def get_template(template_id: str) -> Dict[str, Any]:
    if not _cache:
        _discover()
    if template_id not in _cache:
        raise TemplateNotFound(f"template '{template_id}' not found")
    m = _cache[template_id]
    return {
        "id": m["id"],
        "name": m.get("name", m["id"]),
        "canvas": m.get("canvas"),
        "defaults": m.get("defaults", {}),
        "assets_base": f"/api/v1/template-assets/{m['id']}"
    }

def _resolve_renderer(template_id: str) -> Tuple[Any, str]:
    m = _cache.get(template_id)
    if not m:
        _discover()
        m = _cache.get(template_id)
        if not m:
            raise TemplateNotFound(template_id)
    mod = importlib.import_module(m["renderer"]["module"])
    func = getattr(mod, m["renderer"]["entrypoint"], None)
    if not callable(func):
        raise TemplateManifestError(f"{template_id}: renderer entrypoint not callable")
    return func, m["_folder"]

# public for orchestrator
def get_renderer_func(template_id: str):
    func, _ = _resolve_renderer(template_id)
    return func

def get_template_folder(template_id: str) -> Path:
    _, folder = _resolve_renderer(template_id)
    return Path(folder)

def asset_path(template_id: str, rel: str) -> Path:
    folder = get_template_folder(template_id)
    return (folder / rel).resolve()
