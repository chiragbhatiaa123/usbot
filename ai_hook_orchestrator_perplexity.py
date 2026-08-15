"""
ai_hook_orchestrator_perplexity.py
----------------------------------
Production-safe, *sophisticated* orchestration for ad-reel one-liners with optional
Perplexity browsing (sonar / sonar-pro) and Groq generation.

Contract preserved:
  Returns dict with shape:
    {
      "one_liners": ["...", "...", "..."],
      "source": "groq_orchestrated" | "groq_orchestrated_web" | "local_fallback",
      "validation_notes": ["..."]
    }

Key traits:
- Stage 0: Normalize OCR + caption, detect language, extract entities, score caption quality.
- Stage 0.5 (optional): Perplexity browsing to derive *soft trend hints* + citations without
  introducing brittle factual claims. If confidence is low, we simply don’t use hints.
- Stage 1 (Drafts): Fast Groq model generates 24 JSON-only candidates with style coverage.
- Stage 2 (Critic): Strong Groq model picks the best 3 with style diversity + brevity + safety.
- Post: lexical relevance scoring to prefer context-fit; de-dup; guaranteed 3 outputs.
- JSON-only to/from LLMs; robust parsing; never raises in production (safe fallbacks).

Environment:
- GROQ:   https://api.groq.com/openai/v1/chat/completions  (Bearer GROQ_API_KEY)
- PPLX:   https://api.perplexity.ai/chat/completions        (Bearer PPLX_API_KEY)

Minimal use:
    res = generate_ai_one_liners_browsing(
        ocr_caption, groq_api_key=..., perplexity_api_key=..., downloader_caption=..., use_perplexity=True
    )

Notes:
- We DO NOT change your output JSON shape. Keep your extraction as-is.
- Perplexity results (titles/urls/dates) are summarized into non-assertive hints ("X era",
  "Y energy"). If hints are weak, they are dropped. Citations are surfaced in validation_notes only.
"""

from __future__ import annotations
import logging
import os, json, re, unicodedata, requests
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[ai_hook] %(levelname)s %(message)s"))
    logger.addHandler(handler)
if not logger.level or logger.level > logging.DEBUG:
    logger.setLevel(logging.INFO)
logger.propagate = False

# ------------------ Constants ------------------
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
PPLX_API_URL = "https://api.perplexity.ai/chat/completions"

DRAFTS_MODEL_PREF = "gpt-oss-120b"     # fast, cheap, explores breadth
CRITIC_MODEL_PREF  = "llama-3.3-70b-versatile"         # stronger selector for quality

# ------------------ Text Utilities ------------------

def _clean_text(text: str) -> str:
    if not text:
        return ""
    normalized = (
        text.replace("\u2014", ", ")
            .replace("\u2013", ", ")
            .replace("\u2212", "-")
    )
    out = []
    for ch in normalized:
        if unicodedata.category(ch) == "Cf":  # zero-width & format chars
            continue
        out.append(ch)
    return "".join(out).strip()


def _count_words(s: str) -> int:
    return len([w for w in re.split(r"\s+", s.strip()) if w])


def _is_valid_line(s: str, *, allow_emoji: bool = True, max_words: int = 10) -> bool:
    s = s.strip()
    if not s:
        return False
    if _count_words(s) > max_words:
        return False
    if "#" in s or "@" in s:  # no hashtags/mentions
        return False
    if not allow_emoji and re.search(r"[\U0001F300-\U0001FAFF]", s):
        return False
    low = s.lower()
    banned = ["take my money", "shut up and take", "here for it", "literal chills"]
    if any(b in low for b in banned):
        return False
    risky = ["guaranteed", "cure", "instant results", "100% off"]
    if any(r in low for r in risky):
        return False
    return True


def _dedup(seq: List[str], thresh: float = 0.90) -> List[str]:
    out: List[str] = []
    def jaccard(a: str, b: str) -> float:
        A, B = set(a.lower().split()), set(b.lower().split())
        if not A or not B:
            return 0.0
        inter = len(A & B)
        union = len(A | B)
        return inter / union if union else 0.0
    for s in seq:
        if not out or all(jaccard(s, t) < thresh for t in out):
            out.append(s)
    return out


def _extract_json(text: str) -> Optional[Any]:
    """Tolerant JSON extractor from LLM output."""
    text = (text or "").strip()
    for p in [r"```json\s*(.*?)\s*```", r"```\s*(.*?)\s*```", r"`(.*?)`"]:
        m = re.search(p, text, re.DOTALL)
        if m:
            text = m.group(1).strip()
            break
    try:
        return json.loads(text)
    except Exception:
        pass
    for p, flags in [(r"\{.*\}", re.DOTALL), (r"\[.*\]", re.DOTALL)]:
        m = re.search(p, text, flags)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    return None

# ------------------ Light NLP ------------------

def _detect_language(ocr: str, dl: str) -> str:
    blob = (ocr or "") + "\n" + (dl or "")
    return "hi" if re.search("[\u0900-\u097F]", blob) else "en"


def _caption_quality(caption: str, ocr: str) -> Tuple[str, float]:
    """classify caption as good/ok/garbage with a cheap lexical heuristic."""
    if not caption:
        return ("empty", 0.0)
    cap = caption.lower()
    # garbage-y signals: long bloggy, many links/hashtags, mismatched tokens
    links = cap.count("http://") + cap.count("https://")
    hashtags = cap.count("#")
    longish = len(cap) > 300
    # overlap with OCR tokens
    src = (ocr or "").lower()
    cap_tokens = set(re.findall(r"[a-zA-Z\u0900-\u097F]+", cap))
    ocr_tokens = set(re.findall(r"[a-zA-Z\u0900-\u097F]+", src))
    overlap = len(cap_tokens & ocr_tokens) / max(1, len(cap_tokens))
    if links > 2 or hashtags > 8 or longish and overlap < 0.05:
        return ("garbage_like", 0.15)
    if overlap < 0.10:
        return ("weak", 0.35)
    if overlap < 0.25:
        return ("ok", 0.55)
    return ("good", 0.75)


def _extract_entities(text: str) -> List[str]:
    # ultra-light entity hints: Capitalized tokens and common brand-ish tokens
    ents = set()
    for tok in re.findall(r"[A-Z][a-zA-Z]{2,}\b", text or ""):
        ents.add(tok)
    # add product-ish keywords
    for kw in re.findall(r"(Pro|Ultra|MAX|Plus|Limited|Edition|Beta)\b", text or ""):
        ents.add(kw)
    return sorted(ents)[:8]


def _relevance(line: str, ocr: str, dl: str) -> float:
    src = (ocr + " " + dl).lower()
    src_tokens = {t for t in re.findall(r"[a-zA-Z\u0900-\u097F]+", src)}
    cand_tokens = {t for t in re.findall(r"[a-zA-Z\u0900-\u097F]+", line.lower())}
    if not cand_tokens:
        return 0.0
    inter = len(src_tokens & cand_tokens)
    return inter / max(1, len(cand_tokens))

# ------------------ External Calls ------------------

def _groq_chat(
    api_key: str,
    model: str,
    system: str,
    user: str,
    temperature: float,
    timeout: int,
    seed: Optional[int] = None,
) -> str:
    logger.debug(
        "groq_chat call model=%s temperature=%.2f seed=%s key_present=%s",
        model,
        temperature,
        seed,
        bool(api_key),
    )
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
    }
    if seed is not None:
        payload["seed"] = seed
    try:
        response = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:  # pragma: no cover - network issue
        logger.error("groq_chat request failure: %s", exc, exc_info=True)
        raise

    if response.status_code >= 400:
        logger.warning(
            "groq_chat non-success status=%s body=%r",
            response.status_code,
            response.text[:400],
        )
    response.raise_for_status()
    logger.debug(
        "groq_chat status=%s latency=%.3fs",
        response.status_code,
        response.elapsed.total_seconds() if response.elapsed else -1,
    )

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        logger.error("groq_chat JSON decode error: %s body=%r", exc, response.text[:400])
        raise

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.error("groq_chat unexpected payload structure: %s data=%r", exc, data)
        raise
    return str(content).strip()


def _pplx_browse(perplexity_api_key: str, query: str, model: str = "sonar-pro", timeout: int = 45) -> Tuple[List[Dict[str, str]], str]:
    """Queries Perplexity and returns (search_results, assistant_text). Safe on failure."""
    try:
        headers = {"authorization": f"Bearer {perplexity_api_key}", "content-type": "application/json"}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": query}],
            "enable_search_classifier": True,
        }
        resp = requests.post(PPLX_API_URL, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        sr = data.get("search_results") or []
        msg = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        out = []
        for item in sr:
            title = str(item.get("title", "")).strip()
            url   = str(item.get("url", "")).strip()
            date  = str(item.get("date", "")).strip()
            if url:
                out.append({"title": title, "url": url, "date": date})
        return out[:5], msg
    except Exception as e:
        return [], f"browse_fail: {e}"

# ------------------ Prompt Templates ------------------
DRAFTS_SYS = (
    "You generate on-video hook one-liners for short ad reels.\n"
    "Return JSON ONLY with key \"drafts\" as an array of strings.\n"
    "Rules: 8 to 12 words (hard cap 18), no hashtags/@, no unverifiable facts."
)

DRAFTS_USER_TMPL = (
    "OCR_CAPTION:\n{ocr}\n\n"
    "DOWNLOADER_CAPTION_QUALITY: {cap_quality} ({cap_score:.2f})\n"
    "DOWNLOADER_CAPTION:\n{dl}\n\n"
    "SOFT_TREND_HINTS:\n{hints}\n\n"
    "Produce exactly 24 diverse one-liners across styles: \n"
    "trophy, peak, POV, absurd, buy-now-irony, chefkiss, setup-punch, hyperliteral.\n"
    "Language: {language}.\n"
    "JSON schema: {{\"drafts\":[...]}}"
)

CRITIC_SYS = (
    "You are a selector that chooses the best 3 one-liners for an ad reel.\n"
    "Rules: 8 to 12 words (max 18), no hashtags/@, no unverifiable facts.\n"
    "Ensure style diversity (not 3 of the same). Return JSON ONLY:\n"
    "{\"one_liners\":[\"...\", \"...\", \"...\"], \"reasons\":[\"...\", \"...\", \"...\"]}"
)

CRITIC_USER_TMPL = (
    "OCR_CAPTION:\n{ocr}\n\n"
    "DOWNLOADER_CAPTION_QUALITY: {cap_quality} ({cap_score:.2f})\n"
    "DOWNLOADER_CAPTION:\n{dl}\n\n"
    "SOFT_TREND_HINTS:\n{hints}\n\n"
    "CANDIDATES_JSON:\n{cands}\n\n"
    "Pick the strongest 3 lines that are relevant to the context and universal enough if context is weak.\n"
    "Prefer punchy phrasing and social-native tone.\n"
    "Language: {language}."
)

# ------------------ Context Builder ------------------

def _build_browse_query(ocr: str, dl: str, entities: List[str]) -> str:
    # Compose a concise, high-signal query. We avoid dates/claims; we want *vibe-level* hints.
    core = (ocr or "")[:280]
    cap  = (dl or "")[:280]
    ents = ", ".join(entities)
    return (
        "Given this ad-reel OCR and caption, identify any public trend, meme, brand moment, "
        "or cultural reference likely connected to it. Summarize in 3 short bullets. Do NOT "
        "invent facts; focus on recognizable phrases for wink-level references.\n\n"
        f"OCR: {core}\nCAPTION: {cap}\nENTITIES: {ents}"
    )


def _hints_from_pplx_text(text: str) -> List[str]:
    # Extract 1-liner hints from Perplexity assistant text (keep neutral, no claims)
    if not text:
        return []
    bullets = re.split(r"\n\s*[-•]\s*", text)
    if len(bullets) == 1:
        bullets = re.split(r"\n+", text)
    out: List[str] = []
    for b in bullets:
        b = _clean_text(b)
        if not b:
            continue
        # soften: remove dates, numbers-heavy claims
        b = re.sub(r"\b\d{4}\b", "", b)
        b = re.sub(r"\b\d+%\b", "", b)
        if len(b) > 140:
            b = b[:140].rstrip() + "…"
        out.append(b)
    return out[:3]



DUAL_SYS = """You are an advertising copy chief for short-form video overlays.
Produce two complementary lines for on-screen text.
Constraints: respond with valid JSON only.
TOP_TEXT: 6-12 words, hooks attention, punctuation allowed but no emojis, hashtags, or @mentions.
BOTTOM_TEXT: 10-18 words, supportive detail and gentle CTA, same safety rules.
Stay truthful to the supplied context; keep tone clever, modern, and brand-safe.
"""

DUAL_USER_TMPL = """CONTEXT (JSON):
{context_json}

Tasks:
- TOP_TEXT is the hero hook above the video.
- BOTTOM_TEXT reinforces value, adds nuance, or nudges action.
- Avoid duplicating sentences; make bottom feel like a continuation.
- Word limits: TOP_TEXT <= {top_max_words} words, BOTTOM_TEXT <= {bottom_max_words} words.
- No hashtags, no @mentions, no emoji, no quotation marks.

Return JSON exactly as {{"top_text": "...", "bottom_text": "..."}}.
"""

TOP_TEXT_MIN_WORDS = 5
TOP_TEXT_MAX_WORDS = 12
BOTTOM_TEXT_MIN_WORDS = 8
BOTTOM_TEXT_MAX_WORDS = 18


def generate_dual_text_pair(
    *,
    groq_api_key: str,
    ocr_text: str,
    downloader_caption: Optional[str] = None,
    ai_one_liners: Optional[List[Any]] = None,
    perplexity_payload: Optional[Dict[str, Any]] = None,
    temperature: float = 0.65,
    timeout: int = 60,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate paired top/bottom overlay copy with Groq and safe fallbacks."""
    notes: List[str] = []
    model_name = "llama-3.3-70b-versatile"
    result: Dict[str, Any] = {
        "status": "fallback",
        "top_text": "",
        "bottom_text": "",
        "notes": notes,
        "source": "fallback",
        "raw_output": None,
        "model": model_name,
        "perplexity_hints": None,
    }
    logger.info(
        "dual_text: start (ocr_len=%d downloader_len=%d ai_candidates=%d key_present=%s)",
        len(ocr_text or ""),
        len(downloader_caption or ""),
        len(ai_one_liners or []) if ai_one_liners else 0,
        bool(groq_api_key),
    )

    def _strip_noise(value: str) -> str:
        cleaned = _clean_text(value or "")
        cleaned = cleaned.replace("#", " ").replace("@", " ")
        return " ".join(cleaned.split())

    def _fallback_pair() -> Tuple[str, str]:
        candidates: List[str] = []
        for raw in (ocr_text, downloader_caption):
            if raw:
                cleaned = _strip_noise(raw)
                if cleaned:
                    candidates.append(cleaned)
        if ai_one_liners:
            for item in ai_one_liners:
                if isinstance(item, dict):
                    value = item.get("text") or item.get("caption") or item.get("one_liner")
                else:
                    value = item
                if value:
                    cleaned = _strip_noise(str(value))
                    if cleaned:
                        candidates.append(cleaned)
        if not candidates:
            candidates = ["This deserves your attention"]
        top_seed = candidates[0]
        bottom_seed = candidates[1] if len(candidates) > 1 else f"{candidates[0]} Explore what happens next."
        return top_seed, bottom_seed

    fallback_top_raw, fallback_bottom_raw = _fallback_pair()
    fallback_top_base = _strip_noise(fallback_top_raw) or "This deserves your attention"
    fallback_bottom_base = _strip_noise(fallback_bottom_raw) or "Stay tuned for the reveal."

    def _finalize_slot(value: str, fallback: str, label: str, min_words: int, max_words: int) -> str:
        candidate = _strip_noise(value)
        if not candidate:
            notes.append(f"{label}: empty after cleaning, fallback used.")
            candidate = fallback
        words = candidate.split()
        if len(words) > max_words:
            candidate = " ".join(words[:max_words])
            notes.append(f"{label}: trimmed to {max_words} words.")
            words = candidate.split()
        if len(words) < min_words and fallback:
            candidate = fallback
            notes.append(f"{label}: shorter than {min_words} words, fallback applied.")
        return candidate

    def _apply_fallback(status: str) -> Dict[str, Any]:
        top_text = _finalize_slot(fallback_top_base, fallback_top_base, "TOP_TEXT fallback", TOP_TEXT_MIN_WORDS, TOP_TEXT_MAX_WORDS)
        bottom_seed = fallback_bottom_base if fallback_bottom_base != fallback_top_base else f"{fallback_bottom_base} Stay with us."
        bottom_text = _finalize_slot(bottom_seed, fallback_top_base, "BOTTOM_TEXT fallback", BOTTOM_TEXT_MIN_WORDS, BOTTOM_TEXT_MAX_WORDS)
        if top_text == bottom_text:
            alt = f"{bottom_text} Discover more." if len(bottom_text.split()) < BOTTOM_TEXT_MAX_WORDS else fallback_top_base
            bottom_text = _finalize_slot(alt, fallback_top_base, "BOTTOM_TEXT dedupe", BOTTOM_TEXT_MIN_WORDS, BOTTOM_TEXT_MAX_WORDS)
        result.update({
            "status": status,
            "top_text": top_text,
            "bottom_text": bottom_text,
            "source": status,
        })
        logger.warning(
            "dual_text: returning fallback status=%s top=%r bottom=%r notes=%s",
            status,
            top_text,
            bottom_text,
            notes,
        )
        return result

    if not groq_api_key:
        notes.append("Missing GROQ_API_KEY; using fallback copy.")
        logger.warning("dual_text: GROQ_API_KEY missing; skipping Groq call.")
        return _apply_fallback("fallback_missing_key")

    ai_lines: List[str] = []
    if ai_one_liners:
        for item in ai_one_liners:
            if isinstance(item, dict):
                value = item.get("text") or item.get("caption") or item.get("one_liner")
            else:
                value = item
            if value:
                cleaned = _strip_noise(str(value))
                if cleaned:
                    ai_lines.append(cleaned)
    ai_lines = ai_lines[:8]

    pplx_hints: List[str] = []
    pplx_raw = None
    if isinstance(perplexity_payload, dict):
        hints = perplexity_payload.get("hints")
        if isinstance(hints, list):
            pplx_hints = [str(h).strip() for h in hints if str(h).strip()]
        pplx_raw = perplexity_payload.get("raw_text")
        result["perplexity_hints"] = pplx_hints

    context = {
        "ocr_text": _strip_noise(ocr_text),
        "downloader_caption": _strip_noise(downloader_caption or ""),
        "ai_one_liners": ai_lines,
    }
    if pplx_hints:
        context["perplexity_hints"] = pplx_hints
    if pplx_raw:
        context["perplexity_summary"] = str(pplx_raw)

    context_json = json.dumps(context, ensure_ascii=False, indent=2)
    user_prompt = DUAL_USER_TMPL.format(
        context_json=context_json,
        top_max_words=TOP_TEXT_MAX_WORDS,
        bottom_max_words=BOTTOM_TEXT_MAX_WORDS,
    )

    try:
        raw_output = _groq_chat(
            groq_api_key,
            model_name,
            DUAL_SYS,
            user_prompt,
            temperature=temperature,
            timeout=timeout,
            seed=seed,
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"groq_request_error: {exc}")
        return _apply_fallback("fallback_groq_error")

    result["raw_output"] = raw_output
    parsed = _extract_json(raw_output)
    if not isinstance(parsed, dict):
        notes.append("groq_response_parse_error: missing top/bottom in JSON")
        return _apply_fallback("fallback_parse_error")

    top_candidate = str(parsed.get("top_text") or "").strip()
    bottom_candidate = str(parsed.get("bottom_text") or "").strip()
    if not top_candidate and not bottom_candidate:
        notes.append("groq_response_empty: falling back")
        return _apply_fallback("fallback_empty")

    top_text = _finalize_slot(top_candidate, fallback_top_base, "TOP_TEXT", TOP_TEXT_MIN_WORDS, TOP_TEXT_MAX_WORDS)
    bottom_seed = fallback_bottom_base if fallback_bottom_base != fallback_top_base else fallback_top_base
    bottom_text = _finalize_slot(bottom_candidate, bottom_seed, "BOTTOM_TEXT", BOTTOM_TEXT_MIN_WORDS, BOTTOM_TEXT_MAX_WORDS)

    if top_text == bottom_text:
        notes.append("BOTTOM_TEXT matched TOP_TEXT; adjusted with fallback blend.")
        alt = bottom_candidate if bottom_candidate and bottom_candidate != top_text else f"{fallback_bottom_base} Discover why it matters."
        bottom_text = _finalize_slot(alt, fallback_bottom_base, "BOTTOM_TEXT dedupe", BOTTOM_TEXT_MIN_WORDS, BOTTOM_TEXT_MAX_WORDS)

    result.update({
        "status": "success",
        "top_text": top_text,
        "bottom_text": bottom_text,
        "source": "groq_dual_text",
    })
    logger.info(
        "dual_text: success source=%s top=%r bottom=%r notes=%s",
        result["source"],
        top_text,
        bottom_text,
        notes,
    )
    return result

# ------------------ Public API ------------------

def generate_caption_with_hashtags(
    *,
    ocr_text: Optional[str],
    downloader_caption: Optional[str],
    workspace_dir: Optional[Path] = None,
    groq_api_key: str,
    perplexity_api_key: Optional[str] = None,
    use_perplexity: bool = True,
    existing_perplexity: Optional[Dict[str, Any]] = None,
    brand: Optional[str] = None,
    topic: Optional[str] = None,
    target: Optional[str] = None,
    objective: Optional[str] = "engagement",
    cta_hint: Optional[str] = None,
    tone: str = "clever, modern, culturally aware, human",
    word_limit_max: int = 100,
    preferred_min: int = 50,
    preferred_max: int = 70,
    model: str = "llama-3.3-70b-versatile",
    temperature: float = 0.7,
    seed: Optional[int] = None,
    caption_filename: str = "caption.txt",
    timeout: int = 60,
) -> Dict[str, Any]:
    """
    Generate a full Instagram caption + hashtag block and persist it inside the active workspace.
    Returns a dict describing the outcome, but never raises.
    """

    notes: List[str] = []
    result: Dict[str, Any] = {
        "status": "skipped",
        "caption": None,
        "hashtags": None,
        "output": None,
        "file_path": None,
        "notes": notes,
        "perplexity": None,
    }

    ocr_clean = _clean_text(ocr_text or "")
    downloader_clean = _clean_text(downloader_caption or "")
    primary_text = downloader_clean or ocr_clean
    if not primary_text:
        notes.append("No caption or OCR text provided; skipping caption generation.")
        return result

    if not groq_api_key:
        notes.append("Missing GROQ_API_KEY; skipping caption generation.")
        return result

    ws_path: Optional[Path] = None
    if workspace_dir:
        try:
            ws_path = Path(workspace_dir).resolve()
        except Exception:
            ws_path = Path(workspace_dir)

    entities = _extract_entities(f"{ocr_clean}\n{downloader_clean}")

    hints: List[str] = []
    citations: List[Dict[str, str]] = []
    pplx_text = ""

    if existing_perplexity:
        hints = list(existing_perplexity.get("hints") or [])
        citations = list(existing_perplexity.get("citations") or [])
        pplx_text = str(existing_perplexity.get("raw_text") or "")

    if use_perplexity and not hints and perplexity_api_key:
        try:
            query = _build_browse_query(ocr_clean, downloader_clean, entities)
            citations, pplx_text = _pplx_browse(perplexity_api_key, query, timeout=timeout)
            hints = _hints_from_pplx_text(pplx_text)
        except Exception as exc:
            notes.append(f"perplexity_error: {exc}")
    elif use_perplexity and not perplexity_api_key:
        notes.append("Perplexity browsing requested but PERPLEXITY_API_KEY missing.")
    elif not use_perplexity:
        notes.append("Perplexity browsing disabled for caption generation.")

    system_prompt = (
        "You craft Instagram captions with hashtags. "
        "Output MUST be only caption and hashtags with a single blank line between. "
        "Do not include any extra commentary. Do not include hashtags inside the caption sentences. "
        "No apostrophes and no em dashes. Use simple punctuation. "
        f"Total words including hashtags must be <= {word_limit_max}. "
        f"Prefer {preferred_min}-{preferred_max} words. "
        "Aim for 10 to 12 relevant hashtags. "
        "Keep tone clever and modern. Add a clean CTA that matches the objective. "
        "If information conflicts, prioritize brand copy over notes over web results. "
        "Never mention uncertainty. Never cite sources."
    )

    payload: Dict[str, Optional[str]] = {
        "brand": brand,
        "topic": topic,
        "target": target,
        "objective": objective,
        "cta_hint": cta_hint,
        "tone": tone,
        "copy_text": downloader_clean or None,
        "ocr_text": ocr_clean or None,
        "web_results_text": pplx_text or None,
        "hints": "; ".join(hints) if hints else None,
    }

    user_prompt = (
        "TASK: Produce caption + hashtags exactly in the format below.\n"
        "FORMAT:\n"
        "CAPTION LINES\n"
        "\n"
        "#tag1 #tag2 ...\n"
        "RULES: No apostrophes. No em dashes. No quotes. No leading or trailing spaces. "
        "Do not exceed word limit. 10 to 12 hashtags. CTA included in caption. "
        "PRIORITY OF TRUTH: copy_text > ocr_text > web_results_text.\n"
        f"INPUTS:\n{json.dumps(payload, ensure_ascii=False, separators=(',', ': '))}"
    )

    try:
        raw_output = _groq_chat(
            groq_api_key,
            model,
            system_prompt,
            user_prompt,
            temperature=temperature,
            timeout=timeout,
            seed=seed,
        )
    except Exception as exc:
        notes.append(f"groq_error: {exc}")
        result["perplexity"] = {"hints": hints, "citations": citations, "raw_text": pplx_text}
        return result

    sanitized_output = raw_output.strip()
    for dash in ("\u2014", "\u2013"):
        sanitized_output = sanitized_output.replace(dash, " ")
    for apostrophe in ("'", "\u2018", "\u2019"):
        sanitized_output = sanitized_output.replace(apostrophe, "")

    parts = sanitized_output.split("\n\n", 1)
    if len(parts) == 2:
        caption_block, hashtags_block = parts
    else:
        lines = [line.strip() for line in sanitized_output.splitlines() if line.strip()]
        if not lines:
            notes.append("groq_empty_output")
            result["perplexity"] = {"hints": hints, "citations": citations, "raw_text": pplx_text}
            return result
        caption_block = " ".join(lines[:-1]) if len(lines) > 1 else lines[0]
        hashtags_block = lines[-1]

    caption_block = "\n".join([ln.strip() for ln in caption_block.splitlines() if ln.strip()])
    hashtags_block = " ".join(hashtags_block.split())

    if "#" not in hashtags_block:
        notes.append("hashtags_missing")
    hashtags_tokens = [tok for tok in hashtags_block.split() if tok.startswith("#")]
    if not 9 <= len(hashtags_tokens) <= 13:
        notes.append(f"hashtags_count={len(hashtags_tokens)}")

    total_words = _count_words(f"{caption_block} {hashtags_block}")
    if total_words > word_limit_max:
        notes.append(f"word_count_exceeds({total_words}>{word_limit_max})")

    combined_output = f"{caption_block}\n\n{hashtags_block}".strip()

    file_path: Optional[Path] = None
    if ws_path:
        try:
            target_dir = ws_path / "02_ocr"
            target_dir.mkdir(parents=True, exist_ok=True)
            file_path = target_dir / caption_filename
            file_path.write_text(combined_output + "\n", encoding="utf-8")
        except Exception as exc:
            notes.append(f"write_error: {exc}")
            file_path = None

    has_output = bool(combined_output)
    status_value = "success" if has_output else "skipped"
    result.update(
        {
            "status": status_value,
            "caption": caption_block,
            "hashtags": hashtags_block,
            "output": combined_output if has_output else None,
            "file_path": str(file_path) if file_path else None,
        }
    )
    result["perplexity"] = {"hints": hints, "citations": citations, "raw_text": pplx_text}
    if ws_path and file_path is None:
        notes.append("caption_not_written")
    return result

def generate_ai_one_liners_browsing(
    ocr_caption: str,
    *,
    groq_api_key: str,
    perplexity_api_key: Optional[str] = None,
    downloader_caption: Optional[str] = None,
    use_perplexity: bool = True,
    drafts_model: str = "llama-3.3-70b-versatile",
    critic_model: str = "openai/gpt-oss-20b",
    timeout: int = 60,
    allow_emoji: bool = True,
    language: str = "auto",
    pplx_model: str = "sonar-pro",
) -> Dict[str, Any]:
    """Sophisticated orchestration with optional Perplexity context.
    Never raises; returns safe fallbacks on failure.
    """
    notes: List[str] = []
    result = {"one_liners": [], "source": "groq_orchestrated", "validation_notes": notes}

    if not groq_api_key:
        result["one_liners"] = [
            "Someone promote this intern.",
            "Peak ad. No notes.",
            "POV: you’re already sold.",
        ]
        result["source"] = "local_fallback"
        notes.append("Missing GROQ_API_KEY; used local fallback.")
        return result

    ocr = _clean_text(ocr_caption or "")
    dl  = _clean_text(downloader_caption or "")
    if language == "auto":
        language = _detect_language(ocr, dl)

    cap_q, cap_score = _caption_quality(dl, ocr)
    entities = _extract_entities(ocr + "\n" + dl)

    # Optional browse
    hints: List[str] = []
    citations: List[Dict[str, str]] = []
    pplx_text: str = ""
    if use_perplexity and perplexity_api_key:
        try:
            q = _build_browse_query(ocr, dl, entities)
            citations, pplx_text = _pplx_browse(perplexity_api_key, q, model=pplx_model, timeout=timeout)
            hints = _hints_from_pplx_text(pplx_text)
            if hints:
                result["source"] = "groq_orchestrated_web"
            if citations:
                notes.append("pplx_citations=" + json.dumps(citations, ensure_ascii=False))
        except Exception as e:
            notes.append(f"perplexity_error: {e}")
    else:
        notes.append("perplexity_disabled_or_missing_key")

    # Stage 1: Drafts
    try:
        drafts_user = DRAFTS_USER_TMPL.format(
            ocr=ocr or "[EMPTY]",
            dl=dl or "[NOT AVAILABLE]",
            cap_quality=cap_q,
            cap_score=cap_score,
            hints="; ".join(hints) if hints else "[NONE]",
            language=language,
        )
        drafts_raw = _groq_chat(groq_api_key, drafts_model, DRAFTS_SYS, drafts_user, temperature=0.5, timeout=timeout)
        drafts_json = _extract_json(drafts_raw) or {"drafts": []}
        drafts = drafts_json.get("drafts", [])
        if not isinstance(drafts, list):
            drafts = []
    except Exception as e:
        drafts = []
        notes.append(f"drafts_stage_fail: {e}")

    # Filter drafts
    filtered: List[str] = []
    for s in drafts:
        if not isinstance(s, str):
            continue
        t = re.sub(r"\s+", " ", _clean_text(s)).strip()
        if _is_valid_line(t, allow_emoji=allow_emoji, max_words=10):
            filtered.append(t)
    filtered = _dedup(filtered)

    if not filtered:
        filtered = [
            "Someone promote this intern.",
            "Peak ad. No notes.",
            "POV: you’re already sold.",
            "I don’t need it. I need two.",
            "Art. Simply art.",
        ]
        notes.append("seeded_generics_due_to_no_valid_drafts")

    # Stage 2: Critic
    try:
        cands_json = json.dumps(filtered, ensure_ascii=False)
        critic_user = CRITIC_USER_TMPL.format(
            ocr=ocr or "[EMPTY]",
            dl=dl or "[NOT AVAILABLE]",
            cap_quality=cap_q,
            cap_score=cap_score,
            hints="; ".join(hints) if hints else "[NONE]",
            cands=cands_json,
            language=language,
        )
        critic_raw = _groq_chat(groq_api_key, critic_model, CRITIC_SYS, critic_user, temperature=0.2, timeout=timeout)
        critic_json = _extract_json(critic_raw) or {}
        chosen = critic_json.get("one_liners", [])
        if not isinstance(chosen, list):
            chosen = []
    except Exception as e:
        chosen = []
        notes.append(f"critic_stage_fail: {e}")

    # Finalize: filter, dedup, relevance preference, top-up
    final: List[str] = []
    rel_map: Dict[str, float] = {}
    for s in chosen:
        if not isinstance(s, str):
            continue
        t = re.sub(r"\s+", " ", _clean_text(s)).strip()
        if _is_valid_line(t, allow_emoji=allow_emoji, max_words=10):
            final.append(t)
            rel_map[t] = _relevance(t, ocr, dl)

    final = _dedup(final)

    if len(final) < 3:
        scored = sorted(filtered, key=lambda x: _relevance(x, ocr, dl), reverse=True)
        for cand in scored:
            if len(final) >= 3:
                break
            if cand not in final and _is_valid_line(cand):
                final.append(cand)

    if len(final) < 3:
        backups = [
            "Someone promote this intern.",
            "Peak ad. No notes.",
            "POV: you’re already sold.",
        ]
        for b in backups:
            if len(final) >= 3:
                break
            if b not in final:
                final.append(b)

    # Diagnostics
    try:
        if rel_map:
            top_rel = ", ".join([f"{k} (r={rel_map.get(k, 0):.2f})" for k in final[:3]])
            notes.append(f"relevance(top3): {top_rel}")
        if entities:
            notes.append("entities=" + ", ".join(entities))
        notes.append(f"caption_quality={cap_q}:{cap_score:.2f}")
    except Exception:
        pass

    result["perplexity"] = {
        "hints": hints,
        "citations": citations,
        "raw_text": pplx_text,
    }
    result["one_liners"] = final[:3]
    return result
