# highlight_api.py
def select_highlight_words_via_ai(text: str, top_k: int = 3) -> list[str]:
    """
    Default simple heuristic: return k longest words (placeholder).
    Replace with your Groq/Perplexity/Gemini call that returns best words to highlight.
    """
    cleaned = [w.strip(".,!?:;\"'()") for w in text.split()]
    cleaned = [w for w in cleaned if len(w) > 2]
    cleaned.sort(key=lambda w: -len(w))
    return cleaned[:top_k]
