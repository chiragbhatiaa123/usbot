# orchestrator.py
"""
Minimal orchestrator — idempotent, file-specific, writes step.status JSON files.

Usage:
  python orchestrator.py --url "<REEL_URL>" [--auto]
  python orchestrator.py --scan-workspace [--auto]

Notes:
- Expects workspace/<id>/meta.json (or instagram_downloader.py to create it).
- Calls existing CLIs/scripts:
    - python instagram_downloader.py "<URL>"   (creates workspace)
    - python video_detector.py <in> <out> <threshold>
    - python gemini_ocr_batch.py <in> --out-dir <outdir>
    - uses marketingspots_template.process_marketingspots_template if importable else runs run_for_spots.py
- Writes workspace/<id>/<step>.status JSON with {"status","ts","error","retries"}
"""

import argparse, subprocess, json, time, os, hashlib, shutil, sys, threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("orchestrator.log"),
        logging.StreamHandler(),
    ],
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logger = logging.getLogger(__name__)
if not logger.handlers:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("[orchestrator] %(levelname)s %(message)s"))
    log_file = BASE_DIR / "orchestrator.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
if not logger.level or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)
logger.propagate = False
try:
    from api.template_registry import (
        get_renderer_func,
        TemplateNotFound as TemplateRegistryError,
        get_template_folder,
    )
except Exception:  # pragma: no cover - fallback for standalone usage
    get_renderer_func = None  # type: ignore

    class TemplateRegistryError(Exception):
        pass

    def get_template_folder(template_id: str):
        raise TemplateRegistryError("template registry unavailable")
from gemini_ocr_batch import generate_ai_one_liners
from ai_hook_orchestrator_perplexity import generate_dual_text_pair

import re
_EMOJI_RE = re.compile(
    "["                                    # character ranges
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map symbols
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002700-\U000027BF"  # dingbats
    "\U000024C2-\U0001F251"  # enclosed chars
    "]+",
    flags=re.UNICODE
)

_URL_SHORTCODE_RE = re.compile(r"(?:/p/|/reel/|/reels/|/tv/|/video/)([^/?#]+)")

_TEMPLATE_CFG_CACHE: Dict[str, Dict[str, Any]] = {}


def _load_template_config(template_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Read template.json for the given template id, with a tiny in-process cache."""
    if not template_id:
        return None
    if template_id in _TEMPLATE_CFG_CACHE:
        return _TEMPLATE_CFG_CACHE[template_id]
    if get_template_folder is None:
        return None
    try:
        folder = get_template_folder(template_id)
    except Exception:
        return None
    cfg_path = folder / "template.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        _TEMPLATE_CFG_CACHE[template_id] = cfg
        return cfg
    except Exception:
        return None


def _template_text_flags(template_id: Optional[str]) -> Tuple[bool, bool]:
    cfg = _load_template_config(template_id)
    if not cfg:
        return False, False
    top_enabled = bool(cfg.get("top_text", {}).get("enabled"))
    bottom_enabled = bool(cfg.get("bottom_text", {}).get("enabled"))
    return top_enabled, bottom_enabled


def _resolve_render_text_context(meta: Dict[str, Any], template_id: Optional[str], base_text: str) -> Dict[str, Any]:
    """
    Compute the effective texts and hash signature for rendering, given template capabilities.
    base_text is assumed to be normalized for rendering.
    """
    top_enabled, bottom_enabled = _template_text_flags(template_id)
    
    # NEW: Check for fixed bottom text in template config
    cfg = _load_template_config(template_id)
    fixed_bottom = None
    if cfg and bottom_enabled:
        fixed_bottom = cfg.get("bottom_text", {}).get("fixed_value")
        if fixed_bottom:
            fixed_bottom = clean_text_for_render(normalize_dashes(fixed_bottom))
    
    dual_meta = meta.get("dual_text") if isinstance(meta, dict) else {}

    def _clean(value: Optional[str]) -> str:
        if not value:
            return ""
        return clean_text_for_render(normalize_dashes(value))

    if top_enabled and bottom_enabled:
        top_text = ""
        bottom_text = ""
        
        # NEW: Use fixed bottom if available
        if fixed_bottom:
            bottom_text = fixed_bottom
            top_text = base_text  # User's choice goes to top
        else:
            # Original dual text logic
            if isinstance(dual_meta, dict):
                top_text = _clean(dual_meta.get("top_text"))
                bottom_text = _clean(dual_meta.get("bottom_text"))
            if not top_text:
                top_text = base_text
            if not bottom_text:
                bottom_text = base_text
    elif top_enabled:
        top_text = base_text
        bottom_text = ""
    elif bottom_enabled:
        top_text = ""
        bottom_text = base_text
    else:
        top_text = ""
        bottom_text = ""

    if top_enabled or bottom_enabled:
        hash_payload = json.dumps(
            {
                "text": base_text,
                "top": top_text if top_enabled else "",
                "bottom": bottom_text if bottom_enabled else "",
                "fixed_bottom": fixed_bottom or "",  # NEW: Include in hash
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        text_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()
    else:
        text_hash = hashlib.sha256(base_text.encode("utf-8")).hexdigest() if base_text else "empty"

    return {
        "top_enabled": top_enabled,
        "bottom_enabled": bottom_enabled,
        "top_text": top_text if top_enabled else None,
        "bottom_text": bottom_text if bottom_enabled else None,
        "hash": text_hash,
    }

def clean_text_for_render(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return _EMOJI_RE.sub("", text).strip()


def normalize_dashes(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return text.replace("\u2014", ", ").replace("\u2013", ", ")

def derive_canonical_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = _URL_SHORTCODE_RE.search(url)
    if match:
        return match.group(1)
    # Fallback only if URL has path segments (not just domain)
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    if not path:  # No path means just domain, return None
        return None
    fallback = re.search(r"([A-Za-z0-9_-]{6,})", url)
    return fallback.group(1) if fallback else None

# Config
WORKSPACE_BASE = Path("workspace")
DETECTOR_THRESHOLD = "10.0"  # float as string
RETRY_MAX = 99999
RETRY_BACKOFF = 2  # multiplier
CHOICE_DECISION_TIMEOUT = int(os.getenv("CHOICE_DECISION_TIMEOUT", "30"))

def ts():
    return datetime.utcnow().isoformat() + "Z"

def write_status(ws: Path, step: str, status: str, error=None, retries=0, extra: Optional[dict] = None):
    s = {
        "status": status,
        "ts": ts(),
        "error": None if error is None else str(error),
        "retries": retries,
    }
    if extra:
        try:
            # ensure we don't mutate caller dict
            for key, value in extra.items():
                s[key] = value
        except Exception:
            # best-effort, ignore extra on failure
            pass
    p = ws / f"{step}.status"
    tmp = ws / f"{step}.status.tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(s, f)
    os.replace(str(tmp), str(p))


def read_meta(ws: Path) -> dict:
    meta_path = ws / "meta.json"
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8-sig") as handle:
            try:
                return json.load(handle)
            except json.JSONDecodeError:
                return {}
    return {}


def write_meta(ws: Path, meta: dict) -> None:
    meta_path = ws / "meta.json"
    tmp = ws / "meta.json.tmp"
    with open(tmp, "w", encoding="utf8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(meta_path))

def run_cmd(cmd, cwd=None, timeout=600):
    """Run cmd (list). Return (retcode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return 999, "", str(e)

def acquire_lock(ws: Path):
    lock = ws / ".lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False

def release_lock(ws: Path):
    lock = ws / ".lock"
    try:
        lock.unlink()
    except Exception:
        pass

def find_workspaces(scan_dir=WORKSPACE_BASE):
    if not scan_dir.exists():
        return []
    return sorted([p for p in scan_dir.iterdir() if p.is_dir()])

def ensure_detector(ws: Path):
    out_dir = ws / "01_detector"
    out_dir.mkdir(exist_ok=True)
    cropped = out_dir / "cropped.mp4"
    if cropped.exists():
        write_status(ws, "01_detector", "success", retries=0)
        return True
    src = ws / "00_raw" / "raw_source.mp4"
    if not src.exists():
        write_status(ws, "01_detector", "failed", error="raw_source.mp4 missing")
        return False

    # try with retries
    retries = 0
    while retries < RETRY_MAX:
        cmd = ["python", "video_detector.py", str(src), str(cropped), DETECTOR_THRESHOLD]
        code, out, err = run_cmd(cmd)
        if code == 0 and cropped.exists():
            write_status(ws, "01_detector", "success", retries=retries)
            return True
        retries += 1
        write_status(ws, "01_detector", "retrying", error=err, retries=retries)
        time.sleep((RETRY_BACKOFF ** retries))
    write_status(ws, "01_detector", "failed", error="max retries")
    return False

def ensure_ocr(ws: Path, regen_ai_only: bool = False):
    ocr_dir = ws / "02_ocr"
    ocr_dir.mkdir(exist_ok=True)
    ocr_txt = ocr_dir / "ocr.txt"
    ai_json = ocr_dir / "ai_copies.json"

    meta = read_meta(ws) or {}
    template_id = meta.get("template_id")
    top_enabled, bottom_enabled = _template_text_flags(template_id)

    if regen_ai_only:
        if ocr_txt.exists():
            try:
                caption_text = normalize_dashes(ocr_txt.read_text(encoding="utf-8").strip())
            except Exception as exc:
                logger.warning("Failed to read cached OCR text: %s", exc)
                caption_text = ""
        else:
            caption_text = ""

        if caption_text:
            raw_caption = ws / "00_raw" / "raw_caption.txt"
            downloader_caption = ""
            if raw_caption.exists():
                try:
                    downloader_caption = normalize_dashes(raw_caption.read_text(encoding="utf-8-sig").strip())
                except Exception as exc:
                    logger.warning("Failed to read raw caption %s: %s", raw_caption, exc)

            history_dir = ocr_dir / "history"
            try:
                history_dir.mkdir(exist_ok=True)
            except Exception:
                pass
            if ai_json.exists():
                timestamp_label = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
                try:
                    shutil.move(str(ai_json), str(history_dir / f"ai_copies_refresh_{timestamp_label}.json"))
                except Exception:
                    pass

            perplexity_key = os.getenv("PERPLEXITY_API_KEY", "")
            groq_key = os.getenv("GROQ_API_KEY", "")
            if not groq_key:
                logger.warning("GROQ_API_KEY missing; falling back to full OCR run.")
            else:
                try:
                    ai_result = generate_ai_one_liners(
                        caption_text,
                        perplexity_key,
                        groq_key,
                        downloader_caption=downloader_caption or None,
                    )
                    sanitized = []
                    for idx, line in enumerate(ai_result.get("one_liners", []), start=1):
                        clean_line = normalize_dashes(str(line).strip())
                        if not clean_line:
                            continue
                        sanitized.append(
                            {
                                "id": f"ai-copy-{idx}",
                                "text": clean_line,
                                "source": ai_result.get("source") or "groq_direct",
                            }
                        )
                    if not sanitized and caption_text:
                        sanitized.append(
                            {"id": "fallback-1", "text": caption_text[:140], "source": "ocr_cached"}
                        )
                    with open(ai_json, "w", encoding="utf8") as f:
                        json.dump(sanitized, f, indent=2)
                    write_status(ws, "02_ocr", "success", extra={"mode": "ai_refresh"})
                    return True
                except Exception as exc:
                    logger.warning("AI refresh failed, falling back to full OCR run: %s", exc, exc_info=True)
        else:
            logger.warning("No cached OCR text found; running full OCR.")
        regen_ai_only = False

    if ocr_txt.exists() or ai_json.exists():
        history_dir = ocr_dir / "history"
        archived: List[str] = []
        timestamp_label = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        try:
            history_dir.mkdir(exist_ok=True)
        except Exception:
            pass

        def _archive(path: Path, label: str) -> None:
            if not path.exists():
                return
            suffix = "".join(path.suffixes) if path.suffixes else path.suffix
            destination = history_dir / f"{label}_{timestamp_label}{suffix}"
            try:
                shutil.move(str(path), str(destination))
                archived.append(destination.name)
            except Exception:
                pass

        _archive(ocr_txt, "ocr")
        _archive(ai_json, "ai_copies")
        _archive(ocr_dir / "gemini_report.json", "gemini_report")

        if archived:
            write_status(
                ws,
                "02_ocr",
                "refreshing",
                extra={"archived": archived},
            )

    # Prefer the full raw video for OCR+cross-checking (per your note)
    raw_video = ws / "00_raw" / "raw_source.mp4"
    cropped = ws / "01_detector" / "cropped.mp4"
    if raw_video.exists():
        video_input = raw_video
    elif cropped.exists():
        video_input = cropped
    else:
        write_status(ws, "02_ocr", "failed", error="no raw_source.mp4 or cropped.mp4 found")
        return False

    # caption file available for Perplexity cross-check
    caption_file = ws / "00_raw" / "raw_caption.txt"

    retries = 0
    while retries < RETRY_MAX:
        json_report = ocr_dir / "gemini_report.json"
        # Build correct gemini_ocr_batch invocation (no --out-dir)
        cmd = ["python", "gemini_ocr_batch.py", str(video_input), "--json-report", str(json_report)]

        # If downloader caption exists, tell gemini_ocr_batch where to look
        if caption_file.exists():
            cmd.extend(["--caption-dir", str(ws / "00_raw")])
            cmd.extend(["--caption-extension", ".txt"])

        code, out, err = run_cmd(cmd, timeout=300)

        # write logs for debugging
        logs_dir = ws / "logs"
        logs_dir.mkdir(exist_ok=True)
        with open(logs_dir / "ocr.stdout.txt", "w", encoding="utf8") as f:
            f.write(out or "")
        with open(logs_dir / "ocr.stderr.txt", "w", encoding="utf8") as f:
            f.write(err or "")
        with open(logs_dir / "ocr.log", "w", encoding="utf8") as f:
            f.write(f"CMD: {' '.join(cmd)}\nRETURN: {code}\n\nSTDOUT:\n{out}\n\nSTDERR:\n{err}\n")

        # If gemini wrote the JSON report, parse it
        if code == 0 and json_report.exists():
            try:
                with open(json_report, "r", encoding="utf-8-sig") as f:
                    report = json.load(f)
                if isinstance(report, list) and len(report) > 0:
                    result = report[0]
                    # effective_caption set by gemini script; fallback to 'text'
                    caption_text = normalize_dashes(result.get("effective_caption") or result.get("text") or "")
                    # write OCR text (effective caption)
                    with open(ocr_txt, "w", encoding="utf8") as f:
                        f.write(caption_text)

                    # ai recommended copies (gemini script produces ai_recommended_copies as list of strings)
                    # ai recommended copies (expect list of either strings or dicts)
                    ai_copies_raw = result.get("ai_recommended_copies", []) or []
                    ai_copies_data = []

                    # Try to read a global reported source from the report if present
                    reported_source = result.get("ai_copy_source") or result.get("ai_copy_provider") or None

                    for idx, item in enumerate(ai_copies_raw, start=1):
                        if isinstance(item, dict):
                            text = item.get("text") or item.get("caption") or item.get("one_liner") or ""
                            source = item.get("source") or reported_source or "groq_direct"
                        else:
                            text = str(item)
                            source = reported_source or "groq_direct"
                        text = text.strip()
                        if not text:
                            continue
                        ai_copies_data.append({
                            "id": f"ai-copy-{idx}",
                            "text": text,
                            "source": source
                        })

                                        # NEW: Check if bottom text is fixed in template config
                    cfg = _load_template_config(template_id)
                    skip_dual_generation = False
                    if cfg and top_enabled and bottom_enabled:
                        fixed_bottom = cfg.get("bottom_text", {}).get("fixed_value")
                        if fixed_bottom:
                            skip_dual_generation = True
                            logger.info("dual_text: skipping generation in OCR (bottom_text is fixed)")

                    if top_enabled and bottom_enabled and template_id and not skip_dual_generation:
                        dual_downloader_caption = (
                            result.get("manual_caption")
                            or result.get("ocr_caption")
                            or ""
                        )
                        logger.info(
                            "dual_text: generating overlay copy workspace=%s template=%s key_present=%s",
                            ws.name,
                            template_id,
                            bool(os.getenv("GROQ_API_KEY")),
                        )
                        groq_key = os.getenv("GROQ_API_KEY", "")
                        if groq_key:
                            try:
                                dual_result = generate_dual_text_pair(
                                    groq_api_key=groq_key,
                                    ocr_text=caption_text,
                                    downloader_caption=dual_downloader_caption,
                                    ai_one_liners=ai_copies_data,
                                    temperature=0.6,
                                    timeout=int(os.getenv("DUAL_TEXT_TIMEOUT", "60")),
                                )
                            except Exception as exc:
                                logger.warning("dual_text: generation exception %s", exc, exc_info=True)
                                dual_result = {
                                    "status": "fallback_exception",
                                    "top_text": caption_text,
                                    "bottom_text": caption_text,
                                    "source": "fallback_exception",
                                    "notes": [f"error: {exc}"],
                                }
                        else:
                            dual_result = {
                                "status": "fallback_missing_key",
                                "top_text": caption_text,
                                "bottom_text": caption_text,
                                "source": "fallback_missing_key",
                                "notes": ["missing GROQ_API_KEY"],
                            }
                            logger.warning("dual_text: missing GROQ_API_KEY; using fallback payload")

                        top_text_clean = clean_text_for_render(normalize_dashes(str(dual_result.get("top_text") or caption_text)))
                        bottom_text_clean = clean_text_for_render(normalize_dashes(str(dual_result.get("bottom_text") or caption_text)))
                        if not top_text_clean:
                            top_text_clean = clean_text_for_render(normalize_dashes(caption_text))
                        if not bottom_text_clean:
                            bottom_text_clean = top_text_clean
                        if top_text_clean == bottom_text_clean and bottom_text_clean:
                            bottom_text_clean = clean_text_for_render(f"{bottom_text_clean} Stay tuned.")
                        dual_notes = dual_result.get("notes")
                        if isinstance(dual_notes, list):
                            dual_notes_clean = [str(n) for n in dual_notes if str(n).strip()]
                        elif dual_notes:
                            dual_notes_clean = [str(dual_notes)]
                        else:
                            dual_notes_clean = []
                        dual_hints = dual_result.get("perplexity_hints")
                        if isinstance(dual_hints, list):
                            dual_hints_clean = [str(h) for h in dual_hints if str(h).strip()]
                        else:
                            dual_hints_clean = None
                        dual_payload = {
                            "top_text": top_text_clean,
                            "bottom_text": bottom_text_clean,
                            "source": dual_result.get("source") or "groq_dual_text",
                            "status": dual_result.get("status") or "success",
                            "notes": dual_notes_clean,
                            "perplexity_hints": dual_hints_clean,
                            "ts": ts(),
                        }
                        logger.info(
                            "dual_text: result status=%s source=%s top=%r bottom=%r notes=%s",
                            dual_payload["status"],
                            dual_payload["source"],
                            dual_payload["top_text"],
                            dual_payload["bottom_text"],
                            dual_payload["notes"],
                        )
                        meta = read_meta(ws) or {}
                        meta["dual_text"] = dual_payload
                        write_meta(ws, meta)

                    # If no AI copies but we have caption, create a minimal fallback
                    if not ai_copies_data and caption_text:
                        ai_copies_data.append({"id": "fallback-1", "text": caption_text[:140], "source": "ocr_fallback"})
                    with open(ai_json, "w", encoding="utf8") as f:
                        json.dump(ai_copies_data, f, indent=2)
                    write_status(ws, "02_ocr", "success", retries=retries)
                    return True
            except Exception as e:
                write_status(ws, "02_ocr", "failed", error=f"Failed to parse report: {e}")

        # fallback: use downloader caption if available
        raw_caption = ws / "00_raw" / "raw_caption.txt"
        if raw_caption.exists():
            try:
                text = normalize_dashes(raw_caption.read_text(encoding="utf-8-sig").strip())
                with open(ocr_txt, "w", encoding="utf8") as f:
                    f.write(text)
                ac = [{"id": "fallback-1", "text": text[:140] if len(text) > 140 else text, "source": "raw_caption_fallback"}]
                if top_enabled and bottom_enabled and template_id:
                    fallback_text = clean_text_for_render(text)
                    dual_payload = {
                        "top_text": fallback_text,
                        "bottom_text": fallback_text,
                        "source": "raw_caption_fallback",
                        "ts": ts(),
                    }
                    meta = read_meta(ws) or {}
                    meta["dual_text"] = dual_payload
                    write_meta(ws, meta)
                with open(ai_json, "w", encoding="utf8") as f:
                    json.dump(ac, f, indent=2)
                write_status(ws, "02_ocr", "fallback_caption", retries=retries)
                return True
            except Exception as e:
                write_status(ws, "02_ocr", "failed", error=str(e))
                return False

        retries += 1
        write_status(ws, "02_ocr", "retrying", error=err, retries=retries)
        time.sleep((RETRY_BACKOFF ** retries))

    write_status(ws, "02_ocr", "failed", error="max retries")
    return False


def ensure_choice(ws: Path, auto=False, force_refresh: bool = False):
    choice_dir = ws / "03_choice"
    choice_dir.mkdir(exist_ok=True)
    choice_file = choice_dir / "choice.txt"
    manual_file = choice_dir / "manual.txt"

    if force_refresh:
        for stale in (choice_file, manual_file):
            try:
                if stale.exists():
                    stale.unlink()
            except Exception:
                pass

    def finalize_choice(text: str, status_value: str, source: str) -> bool:
        if not text:
            return False
        normalized = normalize_dashes(text or "")
        cleaned = clean_text_for_render(normalized)
        if not cleaned:
            return False
        with open(choice_file, "w", encoding="utf8") as f:
            f.write(cleaned)
        write_status(
            ws,
            "03_choice",
            status_value,
            extra={
                "selected_source": source,
                "selected_text": cleaned,
            },
        )
        return True

    def try_existing_choice() -> bool:
        if choice_file.exists():
            try:
                text = choice_file.read_text(encoding="utf-8-sig")
            except Exception:
                text = ""
            normalized = normalize_dashes(text)
            cleaned = clean_text_for_render(normalized)
            if cleaned and cleaned != text:
                try:
                    choice_file.write_text(cleaned, encoding="utf8")
                    text = cleaned
                except Exception:
                    pass
            write_status(
                ws,
                "03_choice",
                "success",
                extra={
                    "selected_source": "existing",
                    "selected_text": text.strip(),
                },
            )
            return True
        return False

    def try_manual_choice() -> bool:
        if not manual_file.exists():
            return False
        try:
            text = manual_file.read_text(encoding="utf-8-sig")
        except Exception as exc:
            write_status(ws, "03_choice", "failed", error=str(exc))
            return False
        if finalize_choice(text, "manual_selected", "manual"):
            return True
        return False

    if try_existing_choice():
        return True
    if try_manual_choice():
        return True

    ocr_txt_path = ws / "02_ocr" / "ocr.txt"
    try:
        ocr_text = clean_text_for_render(normalize_dashes(ocr_txt_path.read_text(encoding="utf-8-sig").strip()))
    except Exception:
        ocr_text = ""

    ai_json = ws / "02_ocr" / "ai_copies.json"
    ai_candidates: List[dict] = []
    if ai_json.exists():
        try:
            with ai_json.open("r", encoding="utf-8-sig") as f:
                raw_ai = json.load(f)
            if isinstance(raw_ai, list):
                for idx, item in enumerate(raw_ai, start=1):
                    if isinstance(item, dict):
                        text_value = (
                            item.get("text")
                            or item.get("caption")
                            or item.get("one_liner")
                        )
                        if text_value:
                            sanitized = clean_text_for_render(normalize_dashes(str(text_value)))
                            if sanitized:
                                ai_candidates.append(
                                    {
                                        "id": item.get("id") or f"ai-{idx}",
                                        "text": sanitized,
                                        "source": item.get("source") or "ai",
                                    }
                                )
                    else:
                        sanitized = clean_text_for_render(normalize_dashes(str(item)))
                        if sanitized:
                            ai_candidates.append(
                                {
                                    "id": f"ai-{idx}",
                                    "text": sanitized,
                                    "source": "ai",
                                }
                            )
        except Exception:
            ai_candidates = []

    options_payload = {
        "options": {
            "ocr_text": ocr_text,
            "ai_copies": ai_candidates,
            "timeout_seconds": CHOICE_DECISION_TIMEOUT if auto else None,
        }
    }

    if auto:
        write_status(
            ws,
            "03_choice",
            "pending_choice",
            extra={"state": "waiting_user", **options_payload},
        )
        deadline = time.time() + CHOICE_DECISION_TIMEOUT
        while time.time() < deadline:
            if try_existing_choice():
                return True
            if try_manual_choice():
                return True
            time.sleep(1)
        # one last check in case user wrote near timeout
        if try_existing_choice():
            return True
        if try_manual_choice():
            return True

        fallback_text = None
        fallback_source = None
        if ai_candidates:
            fallback_text = ai_candidates[0].get("text", "")
            fallback_source = ai_candidates[0].get("id") or "ai"
        elif ocr_text:
            fallback_text = ocr_text
            fallback_source = "ocr"

        if fallback_text:
            if finalize_choice(fallback_text, "auto_selected", fallback_source or "auto"):
                return True
            write_status(
                ws,
                "03_choice",
                "failed",
                error="Auto-selected text invalid",
                extra=options_payload,
            )
            return False
        write_status(
            ws,
            "03_choice",
            "failed",
            error="No copy available for auto selection",
            extra=options_payload,
        )
        return False

    # auto is False -> inform caller and wait for external input
    write_status(
        ws,
        "03_choice",
        "pending_choice",
        extra={"state": "awaiting_manual", **options_payload},
    )
    return False

# --- in orchestrator.py ---


def _load_dual_text_sources(ws: Path, base_text: str) -> tuple[str, str, list]:
    """Fetch OCR text, downloader caption, and AI one-liners for dual text generation."""
    ocr_text = base_text
    downloader_caption = ""
    ai_candidates: List[str | Dict[str, Any]] = []
    try:
        ocr_path = ws / "02_ocr" / "ocr.txt"
        if ocr_path.exists():
            ocr_text = normalize_dashes(ocr_path.read_text(encoding="utf-8-sig").strip()) or base_text
    except Exception as exc:
        logger.warning("dual_text: failed reading ocr.txt: %s", exc)

    try:
        caption_path = ws / "00_raw" / "raw_caption.txt"
        if caption_path.exists():
            downloader_caption = normalize_dashes(caption_path.read_text(encoding="utf-8-sig").strip())
    except Exception as exc:
        logger.warning("dual_text: failed reading raw_caption.txt: %s", exc)

    try:
        ai_path = ws / "02_ocr" / "ai_copies.json"
        if ai_path.exists():
            data = json.loads(ai_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                ai_candidates = data
    except Exception as exc:
        logger.warning("dual_text: failed reading ai_copies.json: %s", exc)

    return ocr_text, downloader_caption, ai_candidates
def ensure_render(ws: Path, template_id: Optional[str] = None, force_refresh: bool = False):
    render_dir = ws / "04_render"
    render_dir.mkdir(exist_ok=True)

    # REMOVE canonical copy usage
    # canonical_final = render_dir / "final_1080x1920.mp4"

    choice = ws / "03_choice" / "choice.txt"
    cropped = ws / "01_detector" / "cropped.mp4"
    if not cropped.exists():
        write_status(ws, "04_render", "failed", error="cropped missing")
        return False
    if not choice.exists():
        write_status(ws, "04_render", "failed", error="choice missing")
        return False

    meta = read_meta(ws) or {}
    effective_template_id = template_id or meta.get("template_id")
    top_enabled, bottom_enabled = _template_text_flags(effective_template_id)
    template_options = dict(meta.get("template_options", {}))
    raw_text = normalize_dashes(choice.read_text(encoding="utf-8-sig"))

    text_value = clean_text_for_render(raw_text)
    
    # dual_meta = meta.get("dual_text") if isinstance(meta, dict) else {}
    dual_meta = meta.get("dual_text") if isinstance(meta, dict) else {}

    # NEW: Check if bottom text is fixed in template config
    cfg = _load_template_config(effective_template_id)
    skip_dual_generation = False
    if cfg and top_enabled and bottom_enabled:
        fixed_bottom = cfg.get("bottom_text", {}).get("fixed_value")
        if fixed_bottom:
            skip_dual_generation = True
            logger.info("dual_text: skipping generation in render (bottom_text is fixed)")

    if top_enabled and bottom_enabled and not skip_dual_generation:
        need_refresh = True
        if isinstance(dual_meta, dict):
            top_ready = bool(dual_meta.get("top_text"))
            bottom_ready = bool(dual_meta.get("bottom_text"))
            source_flag = (dual_meta.get("source") or "").lower()
            status_flag = (dual_meta.get("status") or "").lower()
            if top_ready and bottom_ready and not source_flag.startswith("fallback") and status_flag == "success":
                need_refresh = False
        if need_refresh:
            ocr_text, downloader_caption, ai_candidates_raw = _load_dual_text_sources(ws, text_value)
            dual_result = generate_dual_text_pair(
                groq_api_key=os.getenv("GROQ_API_KEY", ""),
                ocr_text=ocr_text,
                downloader_caption=downloader_caption,
                ai_one_liners=ai_candidates_raw,
                perplexity_payload=None,
                temperature=0.6,
                timeout=int(os.getenv("DUAL_TEXT_TIMEOUT", "60")),
            )
            dual_top = clean_text_for_render(normalize_dashes(dual_result.get("top_text") or text_value))
            dual_bottom = clean_text_for_render(normalize_dashes(dual_result.get("bottom_text") or text_value))
            dual_notes = dual_result.get("notes") or []
            if not isinstance(dual_notes, list):
                dual_notes = [str(dual_notes)]
            if dual_top == dual_bottom and dual_bottom:
                dual_bottom = clean_text_for_render(f"{dual_bottom} Stay tuned.")
                dual_notes.append("bottom text adjusted to avoid duplication")
            dual_meta = {
                "top_text": dual_top,
                "bottom_text": dual_bottom,
                "source": dual_result.get("source"),
                "status": dual_result.get("status"),
                "notes": dual_notes,
                "ts": ts(),
            }
            meta["dual_text"] = dual_meta
            write_meta(ws, meta)
    else:
        dual_meta = {}
    
    render_ctx = _resolve_render_text_context(meta, effective_template_id, text_value)
    text_hash = render_ctx["hash"]
    top_text_for_renderer = render_ctx["top_text"]
    bottom_text_for_renderer = render_ctx["bottom_text"]

    if not effective_template_id:
        write_status(ws, "04_render", "failed", error="No template_id specified")
        return False
    
    template_key = effective_template_id

    def options_signature(options: dict) -> str:
        if not options:
            return "none"
        try:
            payload = json.dumps(options, sort_keys=True, separators=(",", ":"))
        except Exception:
            payload = str(options)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    signature = options_signature(template_options)
    renders_meta = meta.setdefault("renders", {})

    template_output_dir = render_dir / "templates" / template_key
    template_output_dir.mkdir(parents=True, exist_ok=True)
    template_final = template_output_dir / "final_1080x1920.mp4"

    existing_entry = renders_meta.get(template_key, {})
    existing_path = Path(existing_entry.get("path", "")) if existing_entry.get("path") else template_final

    if force_refresh:
        existing_entry = {}
        try:
            if template_final.exists():
                template_final.unlink()
        except Exception:
            pass

    # inside ensure_render(...) in orchestrator
    def record_success(source_path: Path, selected_text: str, selected_hash: str, top_text: Optional[str], bottom_text: Optional[str]) -> bool:  
        if not source_path.exists():
            return False
        rel = source_path.relative_to(ws)
        now_ts = ts()
        entry = {
            "path": str(rel),
            "options_signature": signature,
            "template_options": template_options,
            "text_hash": selected_hash,
            "text": selected_text,
            "ts": now_ts,
        }
        if top_text is not None:
            entry["top_text"] = top_text
        if bottom_text is not None:
            entry["bottom_text"] = bottom_text
        renders_meta[template_key] = entry
        meta_render = {
            "template_id": effective_template_id,
            "template_options": template_options,
            "path": str(rel),
            "text_hash": selected_hash,
            "text": selected_text,
            "ts": now_ts,
        }
        if top_text is not None:
            meta_render["top_text"] = top_text
        if bottom_text is not None:
            meta_render["bottom_text"] = bottom_text
        meta["render"] = meta_render
        write_meta(ws, meta)
        write_status(ws, "04_render", "success")
        return True


    if (
        not force_refresh
        and existing_path.exists()
        and existing_entry.get("options_signature") == signature
        and existing_entry.get("text_hash") == text_hash
    ):
        return record_success(existing_path, text_value, text_hash, top_text_for_renderer, bottom_text_for_renderer)

    registry_renderer_factory = None
    if effective_template_id:
        registry_renderer_factory = get_renderer_func
        if registry_renderer_factory is None:
            try:
                from api.template_registry import get_renderer_func as _grf  # type: ignore
                registry_renderer_factory = _grf
            except Exception:
                registry_renderer_factory = None

    # A fresh render is required; mark status accordingly so downstream consumers wait for completion.
    write_status(ws, "04_render", "rendering", extra={"template": template_key})

    if effective_template_id and registry_renderer_factory:
        try:
            renderer = registry_renderer_factory(effective_template_id)
            logger.info("render call workspace=%s template=%s top=%r bottom=%r", ws.name, effective_template_id, top_text_for_renderer, bottom_text_for_renderer)
            render_kwargs: Dict[str, Any] = {}
            if top_text_for_renderer is not None:
                render_kwargs["top_text"] = top_text_for_renderer
            if bottom_text_for_renderer is not None:
                render_kwargs["bottom_text"] = bottom_text_for_renderer
            try:
                if render_kwargs:
                    ok = renderer(
                        str(cropped),
                        str(template_final),
                        text_value,
                        template_options,
                        **render_kwargs,
                    )
                else:
                    ok = renderer(str(cropped), str(template_final), text_value, template_options)
            except TypeError:
                ok = renderer(str(cropped), str(template_final), text_value, template_options)
            except Exception as exc:
                logger.error("Renderer raised exception: %s", exc, exc_info=True)
                write_status(ws, "04_render", "failed", error=str(exc))
                return False
            if ok is None or ok is True:
                if record_success(template_final, text_value, text_hash, top_text_for_renderer, bottom_text_for_renderer):
                    return True
                write_status(ws, "04_render", "failed", error="rendered file missing")
                return False
            write_status(ws, "04_render", "failed", error="renderer returned False")
            return False
        except TemplateRegistryError as tre:
            write_status(ws, "04_render", "failed", error=str(tre))
            return False
        except Exception as exc:
            write_status(ws, "04_render", "failed", error=str(exc))
            return False

    if effective_template_id and not registry_renderer_factory:
        write_status(
            ws,
            "04_render",
            "failed",
            error=f"Template registry unavailable for template '{effective_template_id}'",
        )
        return False

    # No legacy fallback - template_id is required
    write_status(ws, "04_render", "failed", error="Template-based rendering failed")
    return False

def process_single_workspace(ws: Path, auto: bool = False, template_id: Optional[str] = None, reuse_existing: bool = False):
    """Run all steps for single workspace path. Returns dict summary."""
    summary = {
        "id": ws.name,
        "downloaded": not reuse_existing,
        "detected": False,
        "ocr": False,
        "choice": False,
        "rendered": False,
    }
    meta = read_meta(ws)
    meta_changed = False
    if template_id and meta.get("template_id") != template_id:
        meta["template_id"] = template_id
        meta_changed = True
    if not meta.get("canonical_id"):
        meta["canonical_id"] = ws.name
        meta_changed = True
    meta["last_run"] = ts()
    meta["last_auto"] = auto
    meta["last_reuse"] = bool(reuse_existing)
    meta_changed = True
    if meta_changed:
        write_meta(ws, meta)
    effective_template = meta.get("template_id")
    # download already handled by instagram_downloader or creator; if meta missing and url provided, caller should have run downloader.
    # Acquire lock
    if not acquire_lock(ws):
        return {"id": ws.name, "error": "locked"}
    try:
        detector_result = {"value": False}
        detector_done = threading.Event()
        cropped_path = ws / "01_detector" / "cropped.mp4"

        detector_pre_completed = cropped_path.exists()
        if detector_pre_completed:
            status_file = ws / "01_detector.status"
            if status_file.exists():
                try:
                    info = json.loads(status_file.read_text(encoding="utf-8"))
                    detector_pre_completed = info.get("status") == "success"
                except Exception:
                    detector_pre_completed = False

        if detector_pre_completed:
            detector_result["value"] = True
            detector_done.set()
            detector_thread = None
        else:
            def _detector_runner():
                try:
                    detector_result["value"] = ensure_detector(ws)
                except Exception:
                    detector_result["value"] = False
                finally:
                    detector_done.set()

            detector_thread = threading.Thread(
                target=_detector_runner,
                name=f"detector-{ws.name}",
                daemon=True,
            )
            detector_thread.start()

        o = ensure_ocr(ws, regen_ai_only=reuse_existing)
        summary["ocr"] = o

        choice_path = ws / "03_choice" / "choice.txt"
        c = ensure_choice(ws, auto=auto, force_refresh=auto or reuse_existing)
        summary["choice"] = c

        choice_text_clean = ""
        if choice_path.exists():
            try:
                choice_text_clean = clean_text_for_render(
                    normalize_dashes(choice_path.read_text(encoding="utf-8"))
                )
            except Exception:
                choice_text_clean = ""

        meta = read_meta(ws) or meta
        if not effective_template:
            logger.warning("No template_id specified - skipping render")
            summary["rendered"] = False
            return summary
        template_key = effective_template
        previous_entry = (meta.get("renders") or {}).get(template_key, {})
        previous_hash = previous_entry.get("text_hash") if previous_entry else None
        render_ctx_preview = _resolve_render_text_context(meta, effective_template, choice_text_clean)
        desired_hash = render_ctx_preview["hash"]
        force_render = reuse_existing or previous_hash != desired_hash

        if detector_done.wait(0):
            d = detector_result["value"]
        else:
            d = False
        summary["detected"] = d

        if not detector_done.is_set():
            # If detector still running, wait briefly so we can continue in this pass.
            detector_done.wait(timeout=1)

        if detector_done.is_set() and detector_result["value"]:
            r = ensure_render(ws, template_id=effective_template, force_refresh=force_render) if c else False
            summary["rendered"] = r
        return summary
    finally:
        release_lock(ws)

def build_workspace_payload(ws_id: str) -> dict:
    ws_path = WORKSPACE_BASE / ws_id
    meta = read_meta(ws_path)
    payload = {"id": ws_id, "path": str(ws_path.resolve())}
    if meta.get("template_id"):
        payload["template_id"] = meta["template_id"]
    return payload


def emit_workspace_info(target: str, ws_id: str) -> None:
    payload = build_workspace_payload(ws_id)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    if target in ("-", "stdout"):
        print(data)
        return
    out_path = Path(target)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf8") as handle:
        handle.write(data)
    os.replace(tmp, out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="Instagram reel URL to download and process")
    ap.add_argument("--scan-workspace", action="store_true", help="Scan workspace directory for existing meta folders")
    ap.add_argument("--auto", action="store_true", help="Auto select AI copy when choice missing")
    ap.add_argument("--template-id", help="Template identifier to use for rendering")
    ap.add_argument("--emit-json-workspace", help="Path (or '-' for stdout) to emit workspace metadata JSON")
    args = ap.parse_args()

    summaries = []
    workspace_to_emit: Optional[str] = None
    failure = False

    existing_workspaces = find_workspaces()
    targets_map: Dict[Path, bool] = {}

    if args.scan_workspace:
        for ws in existing_workspaces:
            targets_map.setdefault(ws, True)

    if args.url:
        canonical = derive_canonical_from_url(args.url)
        matched_targets: List[Path] = []
        if canonical:
            canonical_lower = canonical.lower()
            for ws in existing_workspaces:
                ws_name_lower = ws.name.lower()
                if ws_name_lower == canonical_lower or ws_name_lower.startswith(f"{canonical_lower}_v"):
                    matched_targets.append(ws)
                    continue
                meta = read_meta(ws)
                cid = (meta.get("canonical_id") or "").lower()
                if cid == canonical_lower:
                    matched_targets.append(ws)

        if matched_targets:
            if workspace_to_emit is None:
                workspace_to_emit = matched_targets[0].name
            print(f"Reusing existing workspace(s): {', '.join(ws.name for ws in matched_targets)}")
            for ws in matched_targets:
                targets_map[ws] = True
        else:
            print("Calling downloader for URL...")
            pre_existing_ids = {p.name for p in existing_workspaces}
            code, out, err = run_cmd(["python", "instagram_downloader.py", args.url], timeout=600)
            if code != 0:
                print("Downloader failed:", err)
            time.sleep(1)
            existing_workspaces = find_workspaces()
            post_ids = {p.name for p in existing_workspaces}
            new_ids = sorted(post_ids - pre_existing_ids)
            if new_ids:
                if workspace_to_emit is None:
                    workspace_to_emit = new_ids[-1]
                ws_map = {ws.name: ws for ws in existing_workspaces}
                for nid in new_ids:
                    if nid in ws_map:
                        targets_map[ws_map[nid]] = False
            else:
                print("Downloader did not produce a new workspace for the requested URL.")
                failure = True

    if targets_map:
        for ws in sorted(targets_map.keys(), key=lambda p: p.name):
            reuse_flag = targets_map[ws]
            label = " (cached)" if reuse_flag else ""
            print(f"Processing {ws.name}{label}")
            res = process_single_workspace(ws, auto=args.auto, template_id=args.template_id, reuse_existing=reuse_flag)
            summaries.append(res)
            if workspace_to_emit is None:
                workspace_to_emit = ws.name
    else:
        if args.scan_workspace or args.url:
            print("No workspaces queued for processing.")

    # write report
    rep = {"ts": ts(), "workspaces": summaries}
    with open("orchestrator_report.json", "w", encoding="utf8") as f:
        json.dump(rep, f, indent=2)
    # compact print
    for s in summaries:
        print(f"{s.get('id')} | detected:{s.get('detected')} ocr:{s.get('ocr')} choice:{s.get('choice')} render:{s.get('rendered')}")
    print("Report written to orchestrator_report.json")
    if args.emit_json_workspace and workspace_to_emit:
        emit_workspace_info(args.emit_json_workspace, workspace_to_emit)
    if failure and not summaries and not workspace_to_emit:
        sys.exit(2)

if __name__ == "__main__":
    main()
