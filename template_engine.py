"""
template_engine.py - ONE-PASS VERSION + FRAGMENTED TEXT SUPPORT
- Single FFmpeg pass for canvas + logo overlays
- Canvas never written to disk (piped via stdin)
- NEW: Fragmented text coloring via fragatext_system integration
"""

from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import io
import json
import string
import tempfile
import requests
import re
import subprocess
import shlex
import os
import math
import logging
import sys
from typing import Tuple, List, Optional, Dict, Any
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Import fragatext if available
try:
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    from fragatext_system import FermentExtractor
    FRAGATEXT_AVAILABLE = True
except ImportError:
    FRAGATEXT_AVAILABLE = False
    FermentExtractor = None

api_key = os.getenv("GROQ_API_KEY", "").strip()

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
HIGHLIGHT_SYSTEM_PROMPT = (
    "You are a text analyzer. Given a hook copy and a number n, identify the n consecutive words "
    "that would be most impactful when highlighted. Follow the user's rules exactly."
)

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[template_engine] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    
    # Debug file handler (commented out for production)
    # file_handler = logging.FileHandler("debug_template_engine.log", mode='w')
    # file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    # logger.addHandler(file_handler)

logger.setLevel(logging.DEBUG)
logger.propagate = False

# Encoding settings
PRODUCTION_VIDEO_ENCODING = (
    "-c:v libx264 "
    "-crf 18 "
    "-preset veryfast "
    "-pix_fmt yuv420p "
    "-movflags +faststart"
)
PRODUCTION_AUDIO_ENCODING = "-c:a aac"

# ---------- Utilities ----------

def probe_video_size(path: str) -> Tuple[int, int]:
    try:
        cmd = f"ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 {shlex.quote(path)}"
        out = subprocess.check_output(shlex.split(cmd), stderr=subprocess.DEVNULL).decode().strip()
        w, h = out.split(",")
        return int(w), int(h)
    except Exception:
        return 1080, 1920

def probe_duration(path):
    try:
        out = subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration",
                                       "-of","default=noprint_wrappers=1:nokey=1", path])
        return float(out.decode().strip())
    except Exception:
        return None

def load_template_cfg(template_root: Path):
    cfg_path = template_root / "template.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Missing template.json at {cfg_path}")
    return json.loads(cfg_path.read_text(encoding="utf-8"))

def get_line_height(font: ImageFont.FreeTypeFont) -> int:
    try:
        ascent, descent = font.getmetrics()
        line_height = ascent + descent
        if line_height > 0:
            return line_height
    except Exception:
        pass
    try:
        bbox = font.getbbox("Ag")
        if bbox:
            return max(1, bbox[3] - bbox[1])
    except Exception:
        pass
    return max(1, getattr(font, "size", 12))

def measure_wrapped_lines(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    draw: ImageDraw.Draw,
    line_spacing_factor: float = 1.2,
    line_height: Optional[int] = None,
):
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if not lines:
        return [], 0
    line_height = line_height or get_line_height(font)
    line_step = max(1, int(line_height * line_spacing_factor))
    total_height = line_height + (len(lines) - 1) * line_step
    return lines, total_height

def choose_font(draw, font_path: Path, requested_size: int, min_size: int, max_lines: int, max_width: int, text: str):
    lo = min_size
    hi = requested_size
    best_size = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            font = ImageFont.truetype(str(font_path), mid)
        except Exception:
            font = ImageFont.load_default()
        lines, _ = measure_wrapped_lines(text, font, max_width, draw)
        if len(lines) <= max_lines:
            best_size = mid
            lo = mid + 1
        else:
            hi = mid - 1
    try:
        return ImageFont.truetype(str(font_path), best_size)
    except Exception:
        return ImageFont.load_default()

def apply_text_casing(text: str, mode: Optional[str]) -> str:
    if not text:
        return text
    normalized = (mode or "original").lower()
    if normalized == "none":
        return text
    if normalized in {"upper", "uppercase", "all_caps", "caps"}:
        return text.upper()
    if normalized in {"lower", "lowercase", "all_lower"}:
        return text.lower()
    if normalized in {"title", "titlecase", "title_case"}:
        return string.capwords(text)
    if normalized in {"sentence", "sentencecase", "sentence_case", "capitalize"}:
        idx = None
        for i, ch in enumerate(text):
            if ch.isalpha():
                idx = i
                break
        if idx is None:
            return text
        prefix = text[:idx]
        first = text[idx].upper()
        rest = text[idx + 1:].lower()
        return f"{prefix}{first}{rest}"
    return text

def draw_highlight_rect(draw: ImageDraw.Draw, rect: Tuple[int,int,int,int], radius: int, fill):
    try:
        draw.rounded_rectangle(rect, radius=radius, fill=fill)
    except Exception:
        draw.rectangle(rect, fill=fill)

def parse_highlight_words(manual_words: List) -> List[List[str]]:
    result = []
    for item in manual_words:
        if isinstance(item, str):
            words = item.strip().split()
            result.append(words)
        else:
            result.append([str(item)])
    return result

def find_phrase_positions(lines: List[str], phrase: List[str]) -> List[Tuple[int, int, int]]:
    positions = []
    all_words = []
    for line_idx, line in enumerate(lines):
        words = line.split()
        for word_idx, word in enumerate(words):
            cleaned = word.strip(".,!?:;\"'()[]{}").lower()
            all_words.append((line_idx, word_idx, word, cleaned))
    phrase_clean = [w.strip(".,!?:;\"'()[]{}").lower() for w in phrase]
    phrase_len = len(phrase_clean)
    i = 0
    while i <= len(all_words) - phrase_len:
        match = True
        for j in range(phrase_len):
            if all_words[i + j][3] != phrase_clean[j]:
                match = False
                break
        if match:
            start_line = all_words[i][0]
            end_line = all_words[i + phrase_len - 1][0]
            if start_line == end_line:
                positions.append((start_line, all_words[i][1], all_words[i + phrase_len - 1][1]))
            else:
                current_line = start_line
                for k in range(phrase_len):
                    word_line = all_words[i + k][0]
                    word_pos = all_words[i + k][1]
                    if word_line != current_line:
                        current_line = word_line
                    positions.append((word_line, word_pos, word_pos))
            i += phrase_len
        else:
            i += 1
    return positions

def draw_highlights(
    draw: ImageDraw.Draw,
    lines: List[str],
    font: ImageFont.FreeTypeFont,
    x_origin: int,
    y_origin: int,
    highlight_phrases: List[List[str]],
    highlight_color: str,
    line_origins: Optional[List[int]] = None,
    line_positions: Optional[List[int]] = None,
    line_height: Optional[int] = None,
    line_step: Optional[int] = None,
):
    if not highlight_phrases:
        return
    radius = max(4, int(font.size * 0.2))
    line_height = line_height or get_line_height(font)
    line_step = line_step or max(1, line_height)
    if line_positions is None:
        line_positions = []
        current_y = y_origin
        for idx in range(len(lines)):
            line_positions.append(current_y)
            if idx < len(lines) - 1:
                current_y += line_step
    highlight_positions = set()
    for phrase in highlight_phrases:
        positions = find_phrase_positions(lines, phrase)
        for line_idx, start_word, end_word in positions:
            for word_idx in range(start_word, end_word + 1):
                highlight_positions.add((line_idx, word_idx))
    for line_idx, line in enumerate(lines):
        line_top = line_positions[line_idx] if line_idx < len(line_positions) else y_origin
        words = line.split()
        cursor_x = (
            line_origins[line_idx]
            if line_origins and line_idx < len(line_origins)
            else x_origin
        )
        for word_idx, word in enumerate(words):
            bbox = draw.textbbox((cursor_x, line_top), word, font=font)
            highlight_rect = (
                bbox[0] - 7,
                bbox[1] - 7,
                bbox[2] + 7,
                bbox[3] + 7,
            )
            if (line_idx, word_idx) in highlight_positions:
                try:
                    draw.rounded_rectangle(highlight_rect, radius=radius, fill=highlight_color)
                except Exception:
                    draw.rectangle(highlight_rect, fill=highlight_color)
            word_width = font.getlength(word)
            space_width = font.getlength(' ') if word_idx < len(words) - 1 else 0
            cursor_x += word_width + space_width

def _fallback_highlight_phrases(text: str, top_k: int) -> List[List[str]]:
    cleaned = [w.strip(".,!?:;\"'()[]{}") for w in text.split()]
    cleaned = [w for w in cleaned if w]
    if not cleaned:
        logger.debug("highlight fallback: no tokens available")
    cleaned.sort(key=lambda w: -len(w))
    limit = max(1, top_k)
    phrases = [[w] for w in cleaned[:limit]]
    logger.debug("highlight fallback selected tokens=%s", phrases)
    return phrases

def _call_groq_highlight_phrase(text: str, n: int, api_key: str, model: str = "llama-3.3-70b-versatile", timeout: int = 10) -> Optional[List[str]]:
    if not api_key or not text or n <= 0:
        return None
    n = 1
    logger.debug("highlight ai preparing Groq call key_present=%s key_length=%d", bool(api_key), len(api_key))
    logger.debug("highlight ai request: text_len=%d top_k=%d model=%s", len(text), n, model)
    user_prompt = (
        "You are a text analyzer. Given a hook copy, identify word "
        "that would be most impactful when highlighted.\n\n"
        f"INPUT:\nHook Copy: {text}\n"
        "RULES:\n"
        "1. Select exactly 1 consecutive words from the hook copy\n"
        "2. Choose word that is most attention-grabbing or emotionally impactful\n"
        "4. Return ONLY the selected word as it appears in the text\n"
        "5. No explanations, no formatting, no quotation marks, no additional text\n\n"
        "OUTPUT:\n"
        "[return only and only the selected word]"
    )
    logger.debug("highlight ai prompt preview=%r", user_prompt[:200])
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": HIGHLIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
    }
    try:
        resp = requests.post(GROQ_CHAT_COMPLETIONS_URL, headers=headers, json=payload, timeout=timeout)
        logger.debug("highlight ai response http_status=%s", resp.status_code)
        resp.raise_for_status()
        logger.debug("highlight ai response status=%s body=%r", resp.status_code, resp.text[:200])
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 'unknown'
        body = exc.response.text[:200] if exc.response is not None and getattr(exc.response, 'text', None) else ''
        logger.warning("highlight ai request failed http status=%s body=%r", status, body)
        return None
    except Exception as exc:
        logger.warning("highlight ai request failed: %s", exc)
        return None
    candidate = re.sub(r"\s+", " ", content.strip(" \"'"))
    logger.debug("highlight ai raw candidate=%r", candidate)
    words = candidate.split()
    if len(words) != n:
        logger.info("highlight ai rejected: expected %d words, got %d", n, len(words))
        return None
    pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        logger.info("highlight ai rejected: candidate not found in original text")
        return None
    original_span = text[match.start():match.end()]
    original_words = original_span.split()
    if len(original_words) != n:
        return None
    logger.info("highlight ai accepted phrase=%s", original_words)
    return original_words

def select_highlight_words_via_ai(text: str, top_k: int = 3) -> List[List[str]]:
    fallback = _fallback_highlight_phrases(text, top_k)
    if not text:
        logger.info("highlight ai skipped: empty text")
        logger.info("highlight fallback tokens=%s", fallback)
        return fallback
    if top_k <= 0:
        logger.info("highlight ai skipped: non-positive top_k=%s", top_k)
        logger.info("highlight fallback tokens=%s", fallback)
        return fallback
    logger.debug(
        "highlight ai env check: key_present=%s key_length=%d",
        bool(api_key),
        len(api_key),
    )
    if not api_key:
        logger.info("highlight ai skipped: GROQ_API_KEY missing")
        logger.info("highlight fallback tokens=%s", fallback)
        return fallback
    phrase = _call_groq_highlight_phrase(text, top_k, api_key)
    if phrase:
        logger.info("highlight ai using Groq phrase=%s", phrase)
        return [phrase]
    logger.info("highlight ai falling back to heuristic selection")
    logger.info("highlight fallback tokens=%s", fallback)
    return fallback

def draw_text_with_color_highlight(
    draw: ImageDraw.Draw,
    lines: List[str],
    font: ImageFont.FreeTypeFont,
    x_origin: int,
    y_origin: int,
    highlight_phrases: List[List[str]],
    highlight_color: str,
    base_color: str,
    line_origins: Optional[List[int]] = None,
    line_positions: Optional[List[int]] = None,
    line_height: Optional[int] = None,
    line_step: Optional[int] = None,
):
    line_height = line_height or get_line_height(font)
    line_step = line_step or max(1, line_height)
    if line_positions is None:
        line_positions = []
        current_y = y_origin
        for idx in range(len(lines)):
            line_positions.append(current_y)
            if idx < len(lines) - 1:
                current_y += line_step
    highlight_positions = set()
    for phrase in highlight_phrases:
        positions = find_phrase_positions(lines, phrase)
        for line_idx, start_word, end_word in positions:
            for word_idx in range(start_word, end_word + 1):
                highlight_positions.add((line_idx, word_idx))
    for line_idx, line in enumerate(lines):
        line_top = line_positions[line_idx] if line_idx < len(line_positions) else y_origin
        words = line.split()
        cursor_x = (
            line_origins[line_idx]
            if line_origins and line_idx < len(line_origins)
            else x_origin
        )
        for word_idx, word in enumerate(words):
            color = highlight_color if (line_idx, word_idx) in highlight_positions else base_color
            draw.text((cursor_x, line_top), word, font=font, fill=color)
            word_width = font.getlength(word)
            space_width = font.getlength(' ') if word_idx < len(words) - 1 else 0
            cursor_x += word_width + space_width

# ---------- NEW: Fragmented Text Support ----------

def call_fragatext(text: str, api_key: str) -> Optional[Dict[str, Any]]:
    """Call fragatext_system to get highlighted fragments."""
    if not FRAGATEXT_AVAILABLE:
        logger.warning("fragatext_system not available - falling back to standard highlighting")
        return None
    
    if not api_key:
        logger.warning("GROQ_API_KEY missing for fragatext")
        return None
    
    try:
        extractor = FermentExtractor(api_key=api_key)
        result = extractor.extract(text)
        logger.info(f"fragatext: extracted {len(result.get('fragments', []))} fragments")
        return result
    except Exception as exc:
        logger.warning(f"fragatext extraction failed: {exc}")
        return None

def map_fragments_to_words(
    text: str,
    fragments: List[Dict[str, Any]],
    lines: List[str]
) -> List[Tuple[int, int, float]]:
    """
    Map fragment character positions to (line_idx, word_idx, importance) tuples.
    
    Returns list of (line_idx, word_idx, importance) for highlighting.
    """
    # Build word position map
    word_positions = []
    char_pos = 0
    for line_idx, line in enumerate(lines):
        words = line.split()
        for word_idx, word in enumerate(words):
            start = text.find(word, char_pos)
            if start != -1:
                end = start + len(word)
                word_positions.append((line_idx, word_idx, start, end, word))
                char_pos = end
    
    # Map fragments to words
    highlighted_words = []
    for frag in fragments:
        frag_start = frag.get("start", 0)
        frag_end = frag.get("end", 0)
        importance = frag.get("importance", 0.5)
        
        for line_idx, word_idx, word_start, word_end, word_text in word_positions:
            # Check if word overlaps with fragment
            if not (word_end <= frag_start or word_start >= frag_end):
                highlighted_words.append((line_idx, word_idx, importance))
    
    return highlighted_words

def get_importance_color(importance: float, base_color: str, highlight_color: str) -> str:
    """Interpolate color based on importance (0.0 = base, 1.0 = highlight)."""
    if importance >= 0.9:
        return highlight_color
    elif importance >= 0.7:
        # High importance - full highlight
        return highlight_color
    elif importance >= 0.5:
        # Medium importance - blend
        return highlight_color
    else:
        # Low importance - base color
        return base_color

def draw_fragmented_text(
    draw: ImageDraw.Draw,
    lines: List[str],
    font: ImageFont.FreeTypeFont,
    x_origin: int,
    y_origin: int,
    highlighted_words: List[Tuple[int, int, float]],
    base_color: str,
    highlight_color: str,
    line_origins: Optional[List[int]] = None,
    line_positions: Optional[List[int]] = None,
    line_height: Optional[int] = None,
    line_step: Optional[int] = None,
):
    """Draw text with fragmented highlighting based on importance."""
    line_height = line_height or get_line_height(font)
    line_step = line_step or max(1, line_height)
    
    if line_positions is None:
        line_positions = []
        current_y = y_origin
        for idx in range(len(lines)):
            line_positions.append(current_y)
            if idx < len(lines) - 1:
                current_y += line_step
    
    # Build highlight map
    highlight_map = {}
    for line_idx, word_idx, importance in highlighted_words:
        key = (line_idx, word_idx)
        # Keep highest importance if word appears in multiple fragments
        if key not in highlight_map or importance > highlight_map[key]:
            highlight_map[key] = importance
    
    # Draw text with colors
    for line_idx, line in enumerate(lines):
        line_top = line_positions[line_idx] if line_idx < len(line_positions) else y_origin
        words = line.split()
        cursor_x = (
            line_origins[line_idx]
            if line_origins and line_idx < len(line_origins)
            else x_origin
        )
        
        for word_idx, word in enumerate(words):
            key = (line_idx, word_idx)
            if key in highlight_map:
                importance = highlight_map[key]
                color = get_importance_color(importance, base_color, highlight_color)
            else:
                color = base_color
            
            draw.text((cursor_x, line_top), word, font=font, fill=color)
            word_width = font.getlength(word)
            space_width = font.getlength(' ') if word_idx < len(words) - 1 else 0
            cursor_x += word_width + space_width

# ---------- Heuristic fragmented highlighting ----------

def select_fragmented_highlights(
    lines: List[str],
    target_ratio: float = 0.45,
) -> List[Tuple[int, int, float]]:
    """
    Heuristically choose words to highlight, targeting roughly 40-50% coverage.
    Returns list of (line_idx, word_idx, importance).
    """
    target_ratio = max(0.3, min(0.5, target_ratio))
    scored_words: List[Tuple[float, int, int]] = []

    for line_idx, line in enumerate(lines):
        words = line.split()
        for word_idx, word in enumerate(words):
            clean = re.sub(r"[^\w]", "", word)
            if not clean:
                continue
            score = len(clean)
            if word.isupper():
                score += 2
            if clean[0].isupper():
                score += 1
            if any(ch.isdigit() for ch in clean):
                score += 1.5
            if any(ch in "!?:;" for ch in word):
                score += 0.5
            scored_words.append((score, line_idx, word_idx))

    total_words = len(scored_words)
    if total_words == 0:
        return []

    target_count = max(1, int(round(total_words * target_ratio)))
    if target_count >= total_words:
        target_count = total_words

    scored_words.sort(key=lambda x: x[0], reverse=True)
    selected = scored_words[:target_count]

    max_score = selected[0][0]
    min_score = selected[-1][0]
    denom = max(0.001, max_score - min_score)

    highlights: List[Tuple[int, int, float]] = []
    for score, line_idx, word_idx in selected:
        importance = 0.5 + 0.5 * ((score - min_score) / denom)
        importance = max(0.35, min(importance, 1.0))
        highlights.append((line_idx, word_idx, importance))

    return highlights

def _ffmpeg_quote(path: Path) -> str:
    posix = path.as_posix()
    posix = posix.replace(":", r"\:")
    posix = posix.replace("'", r"\'")
    return f"'{posix}'"

# ---------- Core engine ----------

@dataclass
class TemplateRenderRequest:
    input_video_path: str
    output_video_path: str
    text: str
    template_root: str
    overrides: Optional[Dict] = None
    top_text: Optional[str] = None
    bottom_text: Optional[str] = None

class TemplateEngine:
    def __init__(self, request: TemplateRenderRequest):
        self.request = request

    def render(self) -> bool:
        root = Path(self.request.template_root)
        cfg = load_template_cfg(root)
        if self.request.overrides:
            cfg.update(self.request.overrides)

        logger.info(f"Starting render - input: {self.request.input_video_path}")

        # Validate input
        input_path = Path(self.request.input_video_path)
        if not input_path.exists():
            logger.error(f"Input video not found: {input_path}")
            return False

        input_size = input_path.stat().st_size
        logger.info(f"Input video size: {input_size / (1024*1024):.2f} MB")

        # Build canvas (PIL in memory)
        canvas_w = int(cfg.get("canvas", {}).get("width", 1080))
        canvas_h = int(cfg.get("canvas", {}).get("height", 1920))
        image = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)

        # Background
        bg = cfg.get("background", {})
        if bg.get("type") == "image":
            bg_path = root / bg.get("value", "")
            logger.info(f"Background image path: {bg_path} (Absolute: {bg_path.resolve()})")
            if bg_path.exists():
                logger.info("Background image found, loading...")
                bgi = Image.open(bg_path).convert("RGBA")
                
                # Cover logic
                img_w, img_h = bgi.size
                scale = max(canvas_w / img_w, canvas_h / img_h)
                new_w = int(img_w * scale)
                new_h = int(img_h * scale)
                bgi = bgi.resize((new_w, new_h), Image.LANCZOS)
                
                # Center crop
                left = (new_w - canvas_w) // 2
                top = (new_h - canvas_h) // 2
                bgi = bgi.crop((left, top, left + canvas_w, top + canvas_h))
                
                image.paste(bgi, (0, 0))
            else:
                logger.error(f"Background image NOT found at {bg_path}")
                draw.rectangle([0, 0, canvas_w, canvas_h], fill=bg.get("value", "#000000"))
        else:
            draw.rectangle([0, 0, canvas_w, canvas_h], fill=bg.get("value", "#000000"))

        # Probe video and compute video box
        vid_w, vid_h = probe_video_size(self.request.input_video_path)
        logger.info(f"Input video dimensions: {vid_w}x{vid_h}")

        video_cfg = cfg.get("video", {})
        fit_mode = video_cfg.get("fit_mode", "width_pct").lower()
        width_pct = float(video_cfg.get("width_pct", 100))
        if fit_mode == "cover":
            scale = max(canvas_w / vid_w, canvas_h / vid_h)
            raw_vw = vid_w * scale
            raw_vh = vid_h * scale
        elif fit_mode == "contain":
            scale = min(canvas_w / vid_w, canvas_h / vid_h)
            raw_vw = vid_w * scale
            raw_vh = vid_h * scale
        else:
            raw_vw = canvas_w * (width_pct / 100.0)
            aspect = vid_h and (vid_w / vid_h) or (16 / 9)
            raw_vh = raw_vw / aspect

        def to_even(value: float) -> int:
            value = max(2.0, value)
            even = int(math.floor(value / 2.0) * 2)
            return max(2, even if even >= 2 else 2)

        vw = to_even(raw_vw)
        vh = to_even(raw_vh)
        vx = int((canvas_w - vw) // 2)
        pos = video_cfg.get("position", {})
        video_style = video_cfg.get("style", {})
        rounded_radius = int(video_cfg.get("rounded_radius", video_style.get("rounded_radius", 0)))
        stroke_width = int(video_cfg.get("stroke_width", video_style.get("stroke_width", 0)))
        stroke_color = video_cfg.get("stroke_color", video_style.get("stroke_color", "#ffffff"))
        stroke_color = video_style.get("stroke_color", stroke_color)
        mask_path = self._prepare_video_mask_asset(vw, vh, rounded_radius)

        # Pre-compute text layouts
        top_layout = None
        bottom_layout = None
        top_placement = None
        bottom_placement = None
        top_text_cfg = cfg.get("top_text", {})
        if top_text_cfg.get("enabled", False):
            text_content = self.request.top_text if self.request.top_text else self.request.text
            top_layout = self._compute_text_layout(draw, root, top_text_cfg, canvas_w, text_content)
            top_placement = (top_text_cfg.get("placement_mode") or "stack").lower()
        bottom_text_cfg = cfg.get("bottom_text", {})
        if bottom_text_cfg.get("enabled", False):
            text_content = self.request.bottom_text if self.request.bottom_text else self.request.text
            bottom_layout = self._compute_text_layout(draw, root, bottom_text_cfg, canvas_w, text_content)
            bottom_placement = (bottom_text_cfg.get("placement_mode") or "stack").lower()

        # Legacy single text fallback
        if not top_layout and not bottom_layout:
            legacy_text_cfg = cfg.get("text", {})
            if legacy_text_cfg:
                top_text_cfg = {
                    "enabled": True,
                    "font": legacy_text_cfg.get("font", ""),
                    "font_size": legacy_text_cfg.get("font_size", 120),
                    "min_font_size": legacy_text_cfg.get("min_font_size", 40),
                    "max_lines": legacy_text_cfg.get("max_lines", 2),
                    "line_width_pct": legacy_text_cfg.get("line_width_pct", 90),
                    "line_spacing_factor": cfg.get("text.line_spacing_factor", legacy_text_cfg.get("line_spacing_factor", 1.35)),
                    "color": legacy_text_cfg.get("color", "#ffffff"),
                    "casing_mode": cfg.get("text.casing_mode", legacy_text_cfg.get("casing_mode", "original")),
                    "align": legacy_text_cfg.get("align", "center"),
                    "gap_px": legacy_text_cfg.get("position_relative_to_video", {}).get("gap_px", 20),
                    "highlight": legacy_text_cfg.get("highlight", {})
                }
                text_content = self.request.top_text if self.request.top_text else self.request.text
                top_layout = self._compute_text_layout(draw, root, top_text_cfg, canvas_w, text_content)

        # Determine stacked layout positions and center the group
        stack_height = vh
        if top_layout and top_placement == "stack":
            stack_height += top_layout["text_height"] + top_layout["gap_px"]
        if bottom_layout and bottom_placement == "stack":
            stack_height += bottom_layout["gap_px"] + bottom_layout["text_height"]

        target_center = int(pos.get("y", canvas_h // 2))
        stack_top = int(target_center - stack_height / 2)

        top_text_top = None
        bottom_text_top = None
        current_y = stack_top

        if top_layout and top_placement == "stack":
            top_text_top = current_y
            current_y += top_layout["text_height"]
            current_y += top_layout["gap_px"]

        vy = current_y
        current_y += vh

        if stroke_width > 0:
            inset = stroke_width / 2.0
            bbox = [
                vx - inset,
                vy - inset,
                vx + vw - 1 + inset,
                vy + vh - 1 + inset,
            ]
            stroke_radius = max(0, rounded_radius + int(inset))
            draw.rounded_rectangle(
                bbox,
                radius=stroke_radius,
                outline=stroke_color,
                width=max(1, stroke_width),
            )

        if bottom_layout and bottom_placement == "stack":
            current_y += bottom_layout["gap_px"]
            bottom_text_top = current_y
            current_y += bottom_layout["text_height"]

        if top_layout and top_placement == "absolute":
            top_text_top = self._compute_absolute_text_top(top_layout, top_text_cfg, canvas_h)
        if bottom_layout and bottom_placement == "absolute":
            bottom_text_top = self._compute_absolute_text_top(bottom_layout, bottom_text_cfg, canvas_h)

        logger.debug(f"Video box: vx={vx}, vy={vy}, vw={vw}, vh={vh}")

        # Render text blocks using computed positions
        if top_layout and top_text_top is not None:
            self._render_text_block(draw, image, top_text_cfg, top_layout, top_text_top, "top")

        if bottom_layout and bottom_text_top is not None:
            try:
                self._render_text_block(draw, image, bottom_text_cfg, bottom_layout, bottom_text_top, "bottom")
            except Exception as exc:
                logger.error(f"bottom_text render failed: {exc}", exc_info=True)
                raise

        # Optional header above top_text
        if top_text_cfg.get("enabled", False) and top_layout:
            header_cfg = cfg.get("header", {})
            if header_cfg.get("enabled", False):
                self._render_header(image, root, header_cfg, canvas_w, top_text_cfg, vx, vy)

        # ------ ONE-PASS COMPOSITION ------
        # Prepare in-memory PNG for canvas
        canvas_bytes = io.BytesIO()
        image.save(canvas_bytes, format="PNG")
        canvas_bytes.seek(0)

        # Logo settings & positions
        logo_cfg = cfg.get("logo", {})
        logo_enabled = bool(logo_cfg.get("enabled", True))
        logo_input_path = None
        lx = ly = 0
        target_w = target_h = None
        rotation_deg = 0

        if logo_enabled:
            logo_path = root / logo_cfg.get("path", "")
            if not logo_path.exists():
                logger.warning(f"Logo file not found at {logo_path}, proceeding without logo.")
                logo_enabled = False
            else:
                sz = logo_cfg.get("size", {})
                target_w = sz.get("width")
                target_h = sz.get("height")
                if target_w is not None:
                    target_w = int(target_w)
                if target_h is not None:
                    target_h = int(target_h)

                rotation_deg = int(logo_cfg.get("rotation", 0))
                position_mode = logo_cfg.get("position_mode", "canvas")
                if position_mode == "video":
                    video_relative = logo_cfg.get("video_relative", {})
                    offset_x = int(video_relative.get("x", 0))
                    offset_y = int(video_relative.get("y", 0))
                    lx = vx + offset_x
        # Build FFmpeg filter graph
        base_opts = "-hide_banner -loglevel warning -loop 1"
        filter_steps = ["[0:v]loop=-1:size=1:start=0,setpts=N/FRAME_RATE/TB[bg]"]

        label_counter = 0
        def new_label(prefix="tmp"):
            nonlocal label_counter
            label = f"{prefix}{label_counter}"
            label_counter += 1
            return label

        current_base = "bg"
        video_label = new_label("vid")
        filter_steps.append(f"[1:v]scale={vw}:{vh},format=rgba[{video_label}]")

        if mask_path:
            mask_label = new_label("mask")
            filter_steps.append(f"movie={_ffmpeg_quote(mask_path)},format=rgba[{mask_label}]")
            masked_label = new_label("vid")
            filter_steps.append(f"[{video_label}][{mask_label}]alphamerge[{masked_label}]")
            video_label = masked_label

        stroke_label = None
        next_base = new_label("base")
        filter_steps.append(f"[{current_base}][{video_label}]overlay={vx}:{vy}:format=yuv420:shortest=1[{next_base}]")
        current_base = next_base

        if logo_enabled:
            lg_in = "[2:v]"
            lg_cur = "lg0"

            lg_chain = []
            if target_w and target_h:
                lg_chain.append(f"{lg_in}scale={target_w}:{target_h}[{lg_cur}]")
                lg_in = f"[{lg_cur}]"
                lg_cur = "lg1"
            elif target_w and not target_h:
                lg_chain.append(f"{lg_in}scale={target_w}:-1[{lg_cur}]")
                lg_in = f"[{lg_cur}]"
                lg_cur = "lg1"
            elif target_h and not target_w:
                lg_chain.append(f"{lg_in}scale=-1:{target_h}[{lg_cur}]")
                lg_in = f"[{lg_cur}]"
                lg_cur = "lg1"

            if rotation_deg != 0:
                radians = rotation_deg * math.pi / 180.0
                lg_chain.append(f"{lg_in}rotate=a={radians}:c=none:ow=rotw(iw):oh=roth(ih)[{lg_cur}]")
                lg_in = f"[{lg_cur}]"
                lg_cur = "lg2"

            if not lg_chain:
                lg_chain.append(f"{lg_in}copy[lg]")
                lg_label = "[lg]"
            else:
                lg_label = f"[{lg_in.strip('[]')}]"
            if lg_label != "[lg]":
                lg_chain.append(f"{lg_label}copy[lg]")

            filter_steps.extend(lg_chain)
            next_base = new_label("base")
            filter_steps.append(f"[{current_base}][lg]overlay={lx}:{ly}:format=auto[{next_base}]")
            current_base = next_base

        filter_steps.append(f"[{current_base}]copy[outv]")

        filter_complex = ";".join(filter_steps)

        cmd = [
            "ffmpeg", "-y"
        ] + shlex.split(base_opts) + [
            "-f", "image2pipe", "-vcodec", "png", "-i", "pipe:0",
            "-i", self.request.input_video_path
        ]

        if logo_enabled and logo_input_path:
            cmd += ["-i", logo_input_path]

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "1:a?",
        ] + shlex.split(PRODUCTION_VIDEO_ENCODING) + [
        ] + shlex.split(PRODUCTION_AUDIO_ENCODING) + [
            self.request.output_video_path
        ]

        logger.debug("FFmpeg command:\n" + " ".join(shlex.quote(x) for x in cmd))
        try:
            proc = subprocess.run(cmd, input=canvas_bytes.getvalue(), capture_output=True)
            if proc.returncode != 0:
                logger.error("FFmpeg failed:\nSTDERR:\n%s", proc.stderr.decode("utf-8", errors="ignore"))
                return False
        except Exception as exc:
            logger.error(f"FFmpeg execution failed: {exc}", exc_info=True)
            return False

        output_path = Path(self.request.output_video_path)
        if not output_path.exists():
            logger.error("Output file not created")
            return False
        if output_path.stat().st_size < 1000:
            logger.error(f"Output file too small: {output_path.stat().st_size} bytes")
            return False

        logger.info("Render completed successfully (single-pass, final video saved).")
        return True

    def _compute_text_layout(self, draw, root, txt_cfg, canvas_w, text_content):
        """Pre-compute font, wrapping, and geometry for a text block."""
        font_rel = txt_cfg.get("font", "")
        font_path = root / font_rel if font_rel else None

        requested_size = int(txt_cfg.get("font_size", 120))
        min_size = int(txt_cfg.get("min_font_size", 40))
        max_lines = int(txt_cfg.get("max_lines", 2))
        line_width_pct = float(txt_cfg.get("line_width_pct", 90))
        max_width = int(canvas_w * (line_width_pct / 100.0))

        casing_mode = txt_cfg.get("casing_mode", "original")
        render_text = apply_text_casing(text_content, casing_mode)

        line_spacing_factor = float(txt_cfg.get("line_spacing_factor", 1.35))

        if font_path and font_path.exists():
            font = choose_font(draw, font_path, requested_size, min_size, max_lines, max_width, render_text)
        else:
            font = ImageFont.load_default()

        line_height = get_line_height(font)
        line_step = max(1, int(line_height * line_spacing_factor))
        lines, text_h = measure_wrapped_lines(
            render_text,
            font,
            max_width,
            draw,
            line_spacing_factor=line_spacing_factor,
            line_height=line_height,
        )

        gap_px = int(txt_cfg.get("gap_px", 20))
        align = txt_cfg.get("align", "center")
        align_lower = align.lower() if isinstance(align, str) else "center"
        margin = int(canvas_w * 0.05)
        fallback_origin = (canvas_w - max_width) // 2 if align_lower == "center" else (canvas_w - margin - max_width if align_lower == "right" else margin)

        line_origins: List[int] = []
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=font)
            line_width = bbox[2] - bbox[0]
            if align_lower == "center":
                origin = int((canvas_w - line_width) // 2)
            elif align_lower == "right":
                origin = canvas_w - margin - line_width
            else:
                origin = margin
            line_origins.append(origin)

        return {
            "render_text": render_text,
            "font": font,
            "lines": lines,
            "text_height": text_h,
            "line_height": line_height,
            "line_step": line_step,
            "gap_px": gap_px,
            "line_origins": line_origins,
            "fallback_origin": fallback_origin,
        }

    def _render_text_block(self, draw, image, txt_cfg, layout, text_top, position_type):
        """Render a text block (top or bottom) using a pre-computed layout."""
        lines: List[str] = layout["lines"]
        if not lines:
            return

        render_text = layout["render_text"]
        font = layout["font"]
        text_h = layout["text_height"]
        line_height = layout["line_height"]
        line_step = layout["line_step"]
        line_origins = layout["line_origins"]
        x_origin = layout["fallback_origin"]

        logger.debug(f"_render_text_block: position={position_type}, text='{render_text[:50]}'")

        line_positions: List[int] = []
        current_line_top = text_top
        for idx in range(len(lines)):
            line_positions.append(current_line_top)
            if idx < len(lines) - 1:
                current_line_top += line_step

        hl_cfg = txt_cfg.get("highlight", {})
        highlight_phrases = []

        if hl_cfg.get("enabled", False):
            highlight_type = hl_cfg.get("type", "background")
            
            if highlight_type == "fragmented":
                target_pct = float(hl_cfg.get("target_pct", 0.45))
                highlighted_words = select_fragmented_highlights(lines, target_pct)

                if not highlighted_words:
                    logger.info("Heuristic fragmented highlighting yielded no result, trying fragatext fallback")
                    fragatext_result = call_fragatext(render_text, api_key)
                    if fragatext_result and fragatext_result.get("fragments"):
                        fragments = fragatext_result["fragments"]
                        highlighted_words = map_fragments_to_words(render_text, fragments, lines)

                if highlighted_words:
                    text_color = txt_cfg.get("color", "#ffffff")
                    highlight_color = hl_cfg.get("color", "#ffde59")
                    draw_fragmented_text(
                        draw,
                        lines,
                        font,
                        x_origin,
                        text_top,
                        highlighted_words,
                        text_color,
                        highlight_color,
                        line_origins=line_origins,
                        line_positions=line_positions,
                        line_height=line_height,
                        line_step=line_step,
                    )
                else:
                    logger.warning("Fragmented highlighting produced no words; falling back to standard rendering")
                    text_color = txt_cfg.get("color", "#ffffff")
                    for idx, (ln, line_top) in enumerate(zip(lines, line_positions)):
                        line_x = line_origins[idx] if idx < len(line_origins) else x_origin
                        draw.text((line_x, line_top), ln, font=font, fill=text_color)
            else:
                # Existing highlight logic for "background" and "color_word"
                if hl_cfg.get("mode", "ai") == "manual":
                    manual_words = hl_cfg.get("manual_words", [])
                    highlight_phrases = parse_highlight_words(manual_words)
                else:
                    top_k = int(hl_cfg.get("highlight_count", 3))
                    highlight_phrases = select_highlight_words_via_ai(render_text, top_k=top_k)

                text_color = txt_cfg.get("color", "#ffffff")

                if highlight_phrases and highlight_type == "background":
                    draw_highlights(
                        draw,
                        lines,
                        font,
                        x_origin,
                        text_top,
                        highlight_phrases,
                        hl_cfg.get("color", "#ffde59"),
                        line_origins=line_origins,
                        line_positions=line_positions,
                        line_height=line_height,
                        line_step=line_step,
                    )
                    for idx, (ln, line_top) in enumerate(zip(lines, line_positions)):
                        line_x = line_origins[idx] if idx < len(line_origins) else x_origin
                        draw.text((line_x, line_top), ln, font=font, fill=text_color)
                elif highlight_phrases and highlight_type == "color_word":
                    draw_text_with_color_highlight(
                        draw,
                        lines,
                        font,
                        x_origin,
                        text_top,
                        highlight_phrases,
                        hl_cfg.get("color", "#ffde59"),
                        text_color,
                        line_origins=line_origins,
                        line_positions=line_positions,
                        line_height=line_height,
                        line_step=line_step,
                    )
                else:
                    for idx, (ln, line_top) in enumerate(zip(lines, line_positions)):
                        line_x = line_origins[idx] if idx < len(line_origins) else x_origin
                        draw.text((line_x, line_top), ln, font=font, fill=text_color)
        else:
            # No highlighting - standard rendering
            text_color = txt_cfg.get("color", "#ffffff")
            for idx, (ln, line_top) in enumerate(zip(lines, line_positions)):
                line_x = line_origins[idx] if idx < len(line_origins) else x_origin
                draw.text((line_x, line_top), ln, font=font, fill=text_color)

        if position_type == "top":
            txt_cfg["_computed_top"] = text_top
            txt_cfg["_computed_height"] = text_h

    def _compute_absolute_text_top(self, layout, txt_cfg, canvas_h):
        anchor = (txt_cfg.get("anchor") or "top").lower()
        offset = int(txt_cfg.get("offset", 0))
        text_h = layout["text_height"]
        if anchor == "bottom":
            top = canvas_h - offset - text_h
        elif anchor == "center":
            top = int((canvas_h - text_h) / 2) + offset
        else:
            top = offset
        max_top = canvas_h - text_h
        return max(0, min(max_top, top))

    def _prepare_video_mask_asset(self, vw: int, vh: int, radius: int):
        if radius <= 0:
            return None
        asset_root = Path(self.request.output_video_path).resolve().parent / "__video_assets"
        asset_root.mkdir(parents=True, exist_ok=True)
        mask_path = asset_root / f"mask_{vw}x{vh}_r{radius}.png"
        if not mask_path.exists():
            mask = Image.new("L", (vw, vh), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle([(0, 0), (vw - 1, vh - 1)], radius=radius, fill=255)
            mask.save(mask_path)
        return mask_path

    def _render_header(self, image, root, header_cfg, canvas_w, top_text_cfg, vx, vy):
        """Render header image above top_text."""
        header_path = root / header_cfg.get("path", "")
        if not header_path.exists():
            return
        try:
            header = Image.open(header_path).convert("RGBA")
            target_w = header_cfg.get("width", 400)
            aspect_ratio = header.height / header.width if header.width else 1
            target_h = int(target_w * aspect_ratio)
            header = header.resize((target_w, target_h), Image.LANCZOS)
            gap_px = int(header_cfg.get("gap_px", 20))
            text_top = top_text_cfg.get("_computed_top", vy)
            hx = (canvas_w - target_w) // 2
            hy = text_top - target_h - gap_px
            image.paste(header, (hx, hy), header)
        except Exception as e:
            logger.warning(f"Failed to render header: {e}")

# ---------- Public API ----------

def render_with_template(input_video_path: str, output_video_path: str, text: str, template_root: str, overrides: dict = None):
    """Main engine entrypoint."""
    request = TemplateRenderRequest(
        input_video_path=input_video_path,
        output_video_path=output_video_path,
        text=text,
        template_root=template_root,
        overrides=overrides,
    )
    engine = TemplateEngine(request)
    return engine.render()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run marketing spot template renderer (one-pass)")
    parser.add_argument("--input-video", type=str, required=True)
    parser.add_argument("--output-video", type=str, required=True)
    parser.add_argument("--text", type=str, required=True)
    parser.add_argument("--template-root", type=str, required=True)
    parser.add_argument("--overrides", type=str)
    args = parser.parse_args()

    overrides = None
    if args.overrides:
        overrides = json.loads(args.overrides)

    ok = render_with_template(
        input_video_path=args.input_video,
        output_video_path=args.output_video,
        text=args.text,
        template_root=args.template_root,
        overrides=overrides
    )
    if not ok:
        raise SystemExit(1)
