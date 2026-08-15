"""
marketingspots_template.py (compatibility shim)
This file keeps the original public function signature:
process_marketingspots_template(input_video_path, output_video_path, text_content, config=None, logo_path_override=None)

Internally it delegates to template_engine.render_with_template while maintaining backwards-compatibility.
"""

from pathlib import Path
import tempfile
import os
import json
from template_engine import TemplateEngine, TemplateRenderRequest, render_with_template

DEFAULT_TEMPLATE_DIR = Path(__file__).parent / "templates" / "default"

def _resolve_template_root(config):
    # config can be None, a dict, or a path str
    if not config:
        return DEFAULT_TEMPLATE_DIR
    # if config is a dict containing 'template_root', use it
    if isinstance(config, dict) and config.get("template_root"):
        return Path(config.get("template_root"))
    # if config is a string path:
    if isinstance(config, str):
        return Path(config)
    # else fallback to default
    return DEFAULT_TEMPLATE_DIR

def _legacy_runner(input_video_path, output_video_path, text_content, config, logo_path_override):
    """
    If config is None and you want to keep legacy behaviour: call render_with_template
    with a minimal default template that mimics old rendering (so visuals unchanged).
    """
    template_root = _resolve_template_root(config)
    overrides = {}
    if logo_path_override is not None:
        # create an override dict to disable logo or set path
        overrides["logo"] = overrides.get("logo", {})
        if logo_path_override == "":
            overrides["logo"]["enabled"] = False
        else:
            overrides["logo"]["path"] = logo_path_override

    request = TemplateRenderRequest(
        input_video_path=input_video_path,
        output_video_path=output_video_path,
        text=text_content,
        template_root=str(template_root),
        overrides=overrides or None,
    )
    engine = TemplateEngine(request)
    return engine.render()

def process_marketingspots_template(input_video_path, output_video_path, text_content, config=None, logo_path_override=None):
    """
    Backwards-compatible entrypoint.
    - input_video_path, output_video_path, text_content stay the same.
    - config can be:
        - None (use templates/default)
        - path string to a template folder
        - dict with {"template_root": "...", ...}
    - logo_path_override: str path or empty string to disable
    """
    # ensure output dir exists
    out_dir = Path(output_video_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    return _legacy_runner(input_video_path, output_video_path, text_content, config, logo_path_override)
