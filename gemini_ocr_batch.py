#!/usr/bin/env python3
"""
Gemini OCR batch tester with Groq validation layer - FIXED VERSION
Key fixes:
1. Proper HTTP header casing (Content-Type, Authorization)
2. Better error handling for Groq validator error objects
3. Enhanced debug logging
"""

from __future__ import annotations
import traceback
import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List

from builtins import print as _builtin_print
import unicodedata
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)
from ai_hook_orchestrator_perplexity import (
    generate_ai_one_liners_browsing,
    generate_caption_with_hashtags,
)

perplexity_api_key = (os.getenv("PERPLEXITY_API_KEY") or "").strip()
groq_api_key = os.getenv("GROQ_API_KEY") 
api_key = os.getenv("GEMINI_API_KEY")

def _safe_print(*args, **kwargs):
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_args = []
    for arg in args:
        if isinstance(arg, str):
            safe_args.append(arg.encode(encoding, errors="backslashreplace").decode(encoding, errors="ignore"))
        else:
            safe_args.append(arg)
    _builtin_print(*safe_args, **kwargs)


print = _safe_print  # type: ignore

try:
    import cv2  # type: ignore
except ImportError as exc:
    raise SystemExit("OpenCV (cv2) is required to run this script") from exc

import requests


def _clean_text(text: str) -> str:
    """Normalize captions by replacing dodgy punctuation and stripping zero-width chars."""
    if not text:
        return ""
    normalized = text.replace("\u2014", ", ").replace("\u2013", ", ").replace("\u2212", "-")
    cleaned_chars = []
    for char in normalized:
        if unicodedata.category(char) == "Cf":
            continue
        cleaned_chars.append(char)
    return "".join(cleaned_chars)

DEFAULT_PROMPT = (
    "Look at the image and extract only the main body text or commentary of the post "
    "that relates to the advertisement. Do not include the social media account name, "
    "handle, date/series information, or any header/branding text. Please provide only "
    "the extracted text as the response."
)

GROQ_JSON_EXTRACTOR_PROMPT = '''You are a strict **JSON extractor & repairer**.
Input: a single arbitrary string (may contain valid JSON, broken JSON, or text around JSON).
Output: **exactly one** JSON value (object or array) – nothing else. No markdown, no code fences, no explanation, no extra characters before or after the JSON (no leading/trailing bytes, not even a newline). The JSON must be 100% RFC-validated (double quotes for strings, no comments, no trailing commas).

Rules – follow them exactly:

1. If the input contains one valid JSON value, output it (canonicalize: double quotes, valid booleans/null).
2. If the input contains multiple JSON values/objects, output a JSON array of those values in the order found.
3. If the input is messy, attempt to **repair** only obvious, local issues:
   * Convert single-quoted strings to double-quoted strings.
   * Quote unquoted object keys.
   * Remove JavaScript-style comments (`//` and `/* */`).
   * Remove trailing commas.
   * Fix obvious missing commas between members/array items when unambiguous.
   * Fix common escape issues inside strings.
   * Convert `True`/`False`/`None` (Python-style) to `true`/`false`/`null`.
   * Preserve numeric formats (integers/floats), but do not invent numbers.
   * Preserve the original text of string values except for necessary quote/escape fixes.
   * Do not attempt speculative structural changes (no adding nested objects/keys that are not implied). Be conservative.
4. If you can confidently produce repaired JSON, output it.
5. If you cannot extract or repair any JSON, output exactly this JSON object (replace the value of `original` with the full original input string, escaped as a JSON string):
   {"error":"no json could be extracted","original": "<full original input here>"}
6. If you must report partial success (some pieces parseable, others not), return an object:
   {"extracted": <value-or-array>, "unparsed":"<remaining unparseable text if any>"}
   (Only use this when necessary; prefer returning the parsed value or the `error` object above.)
7. Never output any extra keys except `error`, `original`, `extracted`, or `unparsed` when following rule 5–6.
8. ALWAYS ensure the output is valid JSON (no trailing commas, proper quoting, proper true/false/null).
9. Output must be the **raw JSON text only** – nothing else, not even a single extra space or newline outside the JSON.
10.Under no circumstances should you use em dashes (—) or en dashes (–) or any similar kind of dash. no dashes at all in your output.
11.Review and revise your response to ensure em dashes never appear in the final output for any reason.

Now parse and respond with the single, valid JSON value for the following input string (do not repeat the input, do not add commentary):

{input_text}'''

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def extract_json_from_string(text: str) -> dict | list | None:
    """
    Sophisticated JSON extractor that handles markdown code blocks and malformed JSON.
    Returns parsed JSON object/array or None if extraction fails.
    """
    text = text.strip()
    
    # Remove markdown code fences
    patterns = [
        r'```json\s*(.*?)\s*```',
        r'```\s*(.*?)\s*```',
        r'`(.*?)`',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            text = match.group(1).strip()
            break
    
    # Try direct parsing first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # Try to find JSON object or array boundaries
    json_patterns = [
        (r'\{.*\}', re.DOTALL),  # Find outermost object
        (r'\[.*\]', re.DOTALL),  # Find outermost array
    ]
    
    for pattern, flags in json_patterns:
        match = re.search(pattern, text, flags)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
    
    return None


def generate_ai_one_liners(
    ocr_caption: str,
    perplexity_api_key: str,
    groq_api_key: str,
    *,
    downloader_caption: str | None = None,
    timeout: int = 60,
    perplexity_model: str = "sonar-pro",
    groq_model: str = "OpenAI/gpt-oss-120b",
) -> dict:
    """
    Wrapper around the orchestrated Perplexity + Groq pipeline.
    Returns dict with 'one_liners', 'source', and 'validation_notes'.
    """
    if not groq_api_key:
        raise RuntimeError("GROQ_API_KEY required for AI generation")

    orchestrator_kwargs = dict(
        groq_api_key=groq_api_key,
        perplexity_api_key=perplexity_api_key or None,
        downloader_caption=downloader_caption,
        use_perplexity=bool(perplexity_api_key),
        timeout=timeout,
        pplx_model=perplexity_model,
        drafts_model="llama-3.1-8b-instant",  # Add this
        critic_model="openai/gpt-oss-20b"
    )

    if groq_model and groq_model != "OpenAI/gpt-oss-120b":
        orchestrator_kwargs["drafts_model"] = groq_model

    result = generate_ai_one_liners_browsing(
        ocr_caption or "",
        **orchestrator_kwargs,
    )

    sanitized = [
        _clean_text(line)
        for line in result.get("one_liners", [])
        if isinstance(line, str) and line.strip()
    ]

    if len(sanitized) < 3:
        sanitized.extend([
            "Someone promote this intern.",
            "Peak ad. No notes.",
            "POV: you're already sold.",
        ])

    sanitized = sanitized[:3]
    result["one_liners"] = sanitized
    result.setdefault("source", "groq_orchestrated")
    result.setdefault("validation_notes", [])

    if not perplexity_api_key:
        result["validation_notes"].append("Perplexity browsing disabled (no API key).")

    return result


def detect_workspace_root(video_path: Path) -> Path | None:
    """
    Try to locate the canonical workspace folder (contains 00_raw) for a given artifact.
    """
    try:
        resolved = video_path.resolve()
    except Exception:
        resolved = video_path

    search_order = [resolved.parent] + list(resolved.parents)
    for candidate in search_order:
        if candidate.name == "00_raw" and candidate.parent.exists():
            ws = candidate.parent
            if (ws / "00_raw").is_dir():
                return ws
        if (candidate / "00_raw").is_dir():
            return candidate
    return None


def gather_video_files(paths: Iterable[str], extensions: Iterable[str]) -> List[Path]:
    exts = {ext.lower().lstrip(".") for ext in extensions}
    files: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            if path.suffix.lower().lstrip(".") in exts:
                files.append(path)
        elif path.is_dir():
            for ext in exts:
                files.extend(path.rglob(f"*.{ext}"))
    return sorted({f.resolve() for f in files})


def extract_frame(video_path: Path, fraction: float) -> bytes:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        raise RuntimeError(f"Video has no frames: {video_path}")

    fraction = min(max(fraction, 0.0), 1.0)
    target_idx = int(total_frames * fraction)
    cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)

    success, frame = cap.read()
    cap.release()
    if not success or frame is None:
        raise RuntimeError(f"Failed to capture frame at index {target_idx} in {video_path}")

    ok, buffer = cv2.imencode(".jpg", frame)
    if not ok:
        raise RuntimeError(f"Could not encode frame from {video_path} to JPEG")

    return buffer.tobytes()


def call_gemini(
    api_key: str,
    image_bytes: bytes,
    prompt: str,
    model: str = "gemini-2.5-flash",
    timeout: int = 90,
) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    encoded_image = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": encoded_image,
                        }
                    },
                ]
            }
        ]
    }

    response = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    try:
        candidates = data["candidates"]
        first = candidates[0]
        parts = first["content"]["parts"]
        text = parts[0]["text"]
        return text.strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data, indent=2)}") from exc


def process_video(path: Path, api_key: str, frame_fraction: float, prompt: str, model: str) -> dict:
    frame_bytes = extract_frame(path, frame_fraction)
    text = call_gemini(api_key, frame_bytes, prompt=prompt, model=model)
    return {
        "video": str(path),
        "text": text,
        "status": "success",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch test Gemini OCR on video stills with Groq validation.")
    parser.add_argument("inputs", nargs="+", help="Video files or directories to process")
    parser.add_argument(
        "--extensions",
        nargs="+",
        default=["mp4", "mov", "mkv", "avi"],
        help="File extensions to include when scanning directories",
    )
    parser.add_argument(
        "--frame-fraction",
        type=float,
        default=0.5,
        help="Fraction through the video to sample (0=start, 1=end)",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-flash",
        help="Gemini model to use",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Override prompt text",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="Optional path to save a JSON summary",
    )
    parser.add_argument(
        "--api-key",
        help="Override GEMINI_API_KEY environment variable",
    )
    parser.add_argument(
        "--perplexity-key",
        help="Override PERPLEXITY_API_KEY environment variable",
    )
    parser.add_argument(
        "--perplexity-model",
        default="sonar-pro",
        help="Perplexity model to use for AI one-liner generation",
    )
    parser.add_argument(
        "--groq-key",
        help="Override GROQ_API_KEY environment variable",
    )
    # parser.add_argument(
    #     "--groq-model",
    #     default="OpenAI/gpt-oss-120b",
    #     help="Groq model to use for validation and fallback generation",
    # )
    parser.add_argument(
        "--perplexity-timeout",
        type=int,
        default=60,
        help="Timeout (seconds) for Perplexity requests",
    )
    parser.add_argument(
        "--groq-timeout",
        type=int,
        default=60,
        help="Timeout (seconds) for Groq requests",
    )
    parser.add_argument(
        "--disable-ai-copy",
        action="store_true",
        help="Skip generating AI recommended one-liner copies",
    )
    parser.add_argument(
        "--groq-model",
        default="llama-3.1-8b-instant",  # Match orchestrator default
        help="Groq model to use for drafts generation",
)
    parser.add_argument(
        "--groq-critic-model",
        default="openai/gpt-oss-20b",  # Add critic model option
        help="Groq model to use for critic/selection stage",
)
    parser.add_argument(
        "--caption-dir",
        type=Path,
        help="Optional directory containing manual caption text files (one per video)",
    )
    parser.add_argument(
        "--caption-extension",
        default=".txt",
        help="Extension to look for when loading manual caption files (default: .txt)",
    )

    args = parser.parse_args()
    load_env_file()
    global api_key, perplexity_api_key, groq_api_key
    api_key = os.getenv("GEMINI_API_KEY")
    perplexity_api_key = (os.getenv("PERPLEXITY_API_KEY") or "").strip()
    groq_api_key = os.getenv("GROQ_API_KEY")
    
    if not api_key:
        raise SystemExit("Set GEMINI_API_KEY (environment or .env) or use --api-key to provide a Gemini API key")

    
    
    ai_copy_enabled = not args.disable_ai_copy
    if ai_copy_enabled and not perplexity_api_key:
        print("[INFO] PERPLEXITY_API_KEY not provided. Skipping Perplexity browsing.")
    if ai_copy_enabled and not groq_api_key:
        print("[ERROR] GROQ_API_KEY is required for AI copy generation.")
        ai_copy_enabled = False

    caption_dir = args.caption_dir.resolve() if args.caption_dir else None
    caption_extension = args.caption_extension.strip()
    if caption_extension and not caption_extension.startswith("."):
        caption_extension = f".{caption_extension}"
    elif not caption_extension:
        caption_extension = ".txt"

    videos = gather_video_files(args.inputs, args.extensions)
    if not videos:
        raise SystemExit("No video files found to process")

    results = []
    for video in videos:
        print(f"[INFO] Processing {video}")
        try:
            result = process_video(video, api_key, args.frame_fraction, args.prompt, args.model)
            print(f"[OK] Extracted text:\n{result['text']}\n")

            ocr_caption = _clean_text(result["text"].strip())
            caption_text = ocr_caption
            caption_source = "ocr"
            manual_caption_file: Path | None = None
            manual_caption_text = ""
            manual_candidates = []

            if caption_dir:
                manual_candidates.append(caption_dir / f"{video.stem}{caption_extension}")
            manual_candidates.append(video.with_suffix(caption_extension))

            for candidate in manual_candidates:
                try:
                    if candidate.exists() and candidate.is_file():
                        manual_caption_file = candidate
                        manual_caption_text = _clean_text(candidate.read_text(encoding="utf-8").strip())
                        break
                except OSError as os_err:
                    print(f"[WARN] Unable to read manual caption file {candidate}: {os_err}")

            if manual_caption_file:
                if manual_caption_text:
                    caption_text = manual_caption_text
                    caption_source = "manual_file"
                    print(f"[INFO] Using manual caption from {manual_caption_file}")
                else:
                    print(f"[WARN] Manual caption file {manual_caption_file} is empty. Falling back to OCR text.")

            caption_text = _clean_text(caption_text)
            result["caption_source"] = caption_source
            result["manual_caption_file"] = str(manual_caption_file) if manual_caption_file else None
            result["manual_caption"] = manual_caption_text if manual_caption_text else None
            result["ocr_caption"] = ocr_caption if ocr_caption else None
            result["effective_caption"] = caption_text
            workspace_root = detect_workspace_root(video)
            result["workspace_root"] = str(workspace_root) if workspace_root else None

            # Replace the entire try/except block (around line 389-420) with:

            if ai_copy_enabled:
                if not (ocr_caption or manual_caption_text):
                    result["ai_copy_status"] = "skipped"
                    result.setdefault("ai_recommended_copies", [])
                    result.setdefault("ai_copy_source", None)
                    result.setdefault("ai_validation_notes", [])
                    result["ai_caption"] = {"status": "skipped", "notes": ["no caption text available"]}
                    print("[WARN] No caption text available. Skipping AI recommended copies.\n")
                else:
                    # No try/except needed - orchestrator never raises
                    ai_result = generate_ai_one_liners(
                        ocr_caption,
                        perplexity_api_key,
                        groq_api_key,
                        downloader_caption=manual_caption_text if manual_caption_text else None,
                        timeout=args.perplexity_timeout,
                        perplexity_model=args.perplexity_model,
                        groq_model=args.groq_model,
                    )
                    
                    # Detect problems by checking the 'source' field
                    if ai_result["source"] == "local_fallback":
                        result["ai_copy_status"] = "fallback"
                        print("[WARN] AI generation fell back to generic one-liners")
                    else:
                        result["ai_copy_status"] = "success"

                    validation_notes = ai_result.get("validation_notes", [])
        
                    # Check for errors in validation notes
                    has_errors = any(
                        "error" in note.lower() or "fail" in note.lower() 
                        for note in validation_notes
                    )
                    
                    if ai_result["source"] == "local_fallback":
                        result["ai_copy_status"] = "fallback"
                        print("[WARN] Using fallback one-liners (API calls failed)")
                    elif has_errors:
                        result["ai_copy_status"] = "degraded"
                        print("[WARN] AI generation succeeded with some errors (check validation_notes)")
                    elif ai_result["source"] == "groq_orchestrated":
                        result["ai_copy_status"] = "success_basic"
                        print("[OK] AI generation succeeded (no web search)")
                    elif ai_result["source"] == "groq_orchestrated_web":
                        result["ai_copy_status"] = "success_enhanced"
                        print("[OK] AI generation succeeded with Perplexity browsing")
                    else:
                        result["ai_copy_status"] = "success"        
                    
                    sanitized_copies = [_clean_text(line) for line in ai_result["one_liners"]]
                    result["ai_recommended_copies"] = sanitized_copies
                    result["ai_copy_source"] = ai_result["source"]
                    result["ai_validation_notes"] = ai_result["validation_notes"]
                    
                    print(f"[OK] AI recommended copies (source: {ai_result['source']}):")
                    for idx, line in enumerate(sanitized_copies, start=1):
                        print(f"  {idx}. {line}")
                    if ai_result["validation_notes"]:
                        print(f"[INFO] Validation notes:")
                        for note in ai_result["validation_notes"]:
                            print(f"  - {note}")
                    print()
                    caption_input_for_ai = manual_caption_text if manual_caption_text else caption_text
                    caption_payload = generate_caption_with_hashtags(
                        ocr_text=ocr_caption,
                        downloader_caption=caption_input_for_ai,
                        workspace_dir=workspace_root,
                        groq_api_key=groq_api_key,
                        perplexity_api_key=perplexity_api_key or None,
                        use_perplexity=bool(perplexity_api_key),
                        existing_perplexity=ai_result.get("perplexity"),
                        timeout=args.groq_timeout,
                    )
                    result["ai_caption"] = caption_payload
                    caption_notes = caption_payload.get("notes") or []
                    if caption_payload.get("status") == "success":
                        location = caption_payload.get("file_path") or "(not saved)"
                        print(f"[OK] AI caption saved to {location}")
                    else:
                        if caption_notes:
                            print(f"[WARN] AI caption skipped: {'; '.join(caption_notes)}")
                        else:
                            print("[WARN] AI caption generation skipped (no output).")
                    if caption_notes and caption_payload.get("status") == "success":
                        print("[INFO] Caption generation notes:")
                        for note in caption_notes:
                            print(f"  - {note}")
                    print()


            else:
                result.setdefault("ai_copy_status", "skipped")
                result.setdefault("ai_recommended_copies", [])
                result.setdefault("ai_copy_source", None)
                result.setdefault("ai_validation_notes", [])
                result.setdefault("ai_caption", {"status": "skipped", "notes": ["ai_copy_disabled"]})

        except Exception as exc:
            result = {
                "video": str(video),
                "status": "error",
                "error": str(exc),
            }
            print(f"[ERROR] {video}: {exc}\n")
        results.append(result)

    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        with open(args.json_report, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Saved report to {args.json_report}")


if __name__ == "__main__":
    main()
