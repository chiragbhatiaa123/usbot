#!/usr/bin/env python3
"""
Simple interactive CLI "frontend" for Templatea.

Usage:
    python cli/frontend_cli.py

Behavior:
 - Ask for reel URL
 - List templates from templates/*.json and let user pick one
 - Launch orchestrator to download & start processing (passes --template-id if supported)
 - Poll until OCR step completes
 - Show user menu: manual / OCR (cleaned) / AI options (cleaned)
 - Write choice to 03_choice/choice.txt and run orchestrator to render
 - Wait for render and print final file path
"""

import json
import time
import subprocess
from pathlib import Path
import sys
import re
import os
from typing import List, Optional

ROOT = Path.cwd()
WORKSPACE_BASE = ROOT / "workspace"
TEMPLATES_DIR = ROOT / "templates"
ORCHESTRATOR = ["python", "orchestrator.py"]

POLL_INTERVAL = 1.0  # seconds
DECISION_WAIT_SECONDS = int(os.getenv("CHOICE_DECISION_TIMEOUT", "30"))

# emoji remover (same as orchestrator)
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

def clean_text_for_display(text: str) -> str:
    if text is None:
        return ""
    # remove emojis, trim whitespace, collapse newlines to single space
    t = _EMOJI_RE.sub("", text)
    t = " ".join(t.splitlines())
    t = " ".join(t.split())
    return t.strip()

def run_cmd(cmd, timeout=600):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        return 999, "", str(e)

def list_templates():
    if not TEMPLATES_DIR.exists():
        return []

    def _load_manifest(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if "id" not in data:
            data["id"] = path.parent.name or path.stem
        if "name" not in data:
            data["name"] = data["id"]
        data["_manifest_path"] = str(path)
        data["_template_root"] = str(path.parent)
        return data

    manifests: List[dict] = []

    # Prefer folder-based manifests (templates/<id>/template.json)
    for child in sorted(TEMPLATES_DIR.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "template.json"
        if not manifest_path.exists():
            alt = child / "manifest.json"
            if alt.exists():
                manifest_path = alt
        if manifest_path.exists():
            meta = _load_manifest(manifest_path)
            if meta:
                manifests.append(meta)

    # Legacy flat manifests at templates/*.json
    for file in sorted(TEMPLATES_DIR.glob("*.json")):
        if file.name.lower() == "template.json":
            continue
        meta = _load_manifest(file)
        if meta:
            manifests.append(meta)

    return manifests

def find_workspace_by_url(url: str, timeout=30):
    """Poll workspace/ for a meta.json with matching source_url."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ws in sorted(WORKSPACE_BASE.iterdir()) if WORKSPACE_BASE.exists() else []:
            try:
                meta_p = ws / "meta.json"
                if not meta_p.exists():
                    continue
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
                if meta.get("source_url") == url or meta.get("source_url", "").startswith(url):
                    return ws
            except Exception:
                continue
        time.sleep(0.5)
    return None

def wait_for_status(ws: Path, step: str, timeout=300):
    """Wait until <step>.status exists and reports success/fallback/failed; return status dict or None."""
    status_p = ws / f"{step}.status"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if status_p.exists():
            try:
                s = json.loads(status_p.read_text(encoding="utf-8"))
                # if status is one of completion states, return
                st = s.get("status")
                if st in ("success", "fallback_caption", "failed", "manual_selected", "auto_selected"):
                    return s
                # else keep waiting
            except Exception:
                pass
        time.sleep(POLL_INTERVAL)
    return None

def pick_from_menu(prompt, options):
    """Simple numbered menu. options is list of strings. Returns selected index (0-based)."""
    print(prompt)
    for i, opt in enumerate(options, start=1):
        print(f"  {i}) {opt}")
    while True:
        choice = input(f"Choose 1-{len(options)} (or 'q' to quit): ").strip()
        if choice.lower() == "q":
            print("Aborted.")
            sys.exit(0)
        if not choice:
            continue
        if not choice.isdigit():
            print("Please enter a number.")
            continue
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            return idx
        print("Out of range.")

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def main():
    print("=== Templatea CLI frontend ===")
    print(f"DEBUG: CWD is {Path.cwd()}")
    print(f"DEBUG: TEMPLATES_DIR is {TEMPLATES_DIR}")
    url = input("Enter Instagram reel URL: ").strip()
    if not url:
        print("No URL provided; exiting.")
        return

    # list templates
    templates = list_templates()
    if not templates:
        print("No templates found in templates/*.json. Create templates first.")
        return

    template_options = [f"{t.get('name','<no-name>')} ({t.get('id')})" for t in templates]
    tidx = pick_from_menu("Select a template:", template_options)
    chosen_template = templates[tidx]
    template_id = chosen_template.get("id")
    print(f"Selected template: {chosen_template.get('name')} ({template_id})")

    # start orchestrator download + processing (pass template id if orchestrator supports flag)
    print("Starting download + processing (this may take a bit)...")
    # try passing --template-id; orchestrator may ignore it if not implemented
    cmd = ORCHESTRATOR + ["--url", url, "--template-id", template_id]
    code, out, err = run_cmd(cmd, timeout=600)
    if code != 0:
        # fallback: try without --template-id
        print("Orchestrator returned non-zero; retrying download-only call (no template flag)...")
        code2, out2, err2 = run_cmd(ORCHESTRATOR + ["--url", url], timeout=600)
        if code2 != 0:
            print("Downloader/orchestrator failed to start. stderr:")
            print(err2 or err)
            return

    # find the workspace for this URL
    print("Locating workspace for URL...")
    ws = find_workspace_by_url(url, timeout=20)
    if not ws:
        # maybe orchestrator uses slightly different source_url pattern, try last created folder
        wlist = sorted(WORKSPACE_BASE.iterdir()) if WORKSPACE_BASE.exists() else []
        if wlist:
            ws = wlist[-1]
            print("Using latest workspace:", ws.name)
        else:
            print("No workspace found. Exiting.")
            return
    else:
        print("Workspace found:", ws.name)

    # ensure template_id stored in meta.json (if orchestrator didn't set it)
    meta_p = ws / "meta.json"
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    if meta.get("template_id") != template_id:
        meta["template_id"] = template_id
        meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Wrote template_id to workspace meta.json")

    # ensure orchestrator processes this workspace (run scan)
    print("Triggering processing for workspace...")
    run_cmd(ORCHESTRATOR + ["--scan-workspace"], timeout=600)

    # Wait for OCR step
    print("Waiting for OCR to complete (this can take a while)...")
    status = wait_for_status(ws, "02_ocr", timeout=300)
    if not status:
        print("Timeout waiting for OCR step. Check logs in workspace and try again.")
        return
    if status.get("status") in ("failed",):
        print("OCR step failed:", status)
        return
    print("OCR step completed with status:", status.get("status"))

    # Load OCR and AI outputs
    ocr_txt_p = ws / "02_ocr" / "ocr.txt"
    ai_json_p = ws / "02_ocr" / "ai_copies.json"
    ocr_text = ocr_txt_p.read_text(encoding="utf-8").strip() if ocr_txt_p.exists() else ""
    ai_list = []
    if ai_json_p.exists():
        try:
            ai_list = json.loads(ai_json_p.read_text(encoding="utf-8"))
        except Exception:
            ai_list = []

    # Build menu: Manual, OCR(cleaned), AI options (cleaned)
    menu_options = []
    menu_values = []

    menu_options.append("Type manual copy now (enter text)")  # index 0
    menu_values.append({"type": "manual"})

    if ocr_text:
        cleaned_ocr = clean_text_for_display(ocr_text)
        menu_options.append(f"OCR extracted copy (cleaned): \"{cleaned_ocr[:120]}\"")
        menu_values.append({"type": "ocr", "text": cleaned_ocr})

    # Add AI options
    for ai in ai_list:
        # ai may be dict with text or raw string
        text = ai.get("text") if isinstance(ai, dict) else str(ai)
        cleaned = clean_text_for_display(text)
        menu_options.append(f"AI suggestion: \"{cleaned[:120]}\"")
        menu_values.append({"type": "ai", "text": cleaned})

    print(f"\nSelect the copy to use (auto mode will default to the first AI suggestion after {DECISION_WAIT_SECONDS} seconds).")

    # Present menu
    idx = pick_from_menu("Choose final copy option:", menu_options)

    selected = menu_values[idx]
    if selected["type"] == "manual":
        manual_text = input("Enter manual copy (will be cleaned of emojis before render):\n").strip()
        final_text = clean_text_for_display(manual_text)
    else:
        final_text = selected.get("text", "").strip()

    if not final_text:
        print("Empty final text; aborting.")
        return

    # ensure 03_choice folder exists and write choice.txt
    choice_dir = ws / "03_choice"
    choice_dir.mkdir(parents=True, exist_ok=True)
    (choice_dir / "choice.txt").write_text(final_text, encoding="utf-8")
    print("Wrote choice.txt with selected copy (emoji-cleaned).")

    # trigger render
    print("Triggering render...")
    run_cmd(ORCHESTRATOR + ["--scan-workspace"], timeout=600)

    # wait for render
    print("Waiting for render to finish...")
    rstatus = wait_for_status(ws, "04_render", timeout=600)
    if not rstatus:
        print("Timeout waiting for render. Check logs.")
        return
    if rstatus.get("status") != "success":
        print("Render failed:", rstatus)
        return

    final_p = ws / "04_render" / "final_1080x1920.mp4"
    if final_p.exists():
        print("Render complete! Final video at:", str(final_p))
    else:
        print("Render reported success but final file missing. Check workspace:", ws)

if __name__ == "__main__":
    main()
