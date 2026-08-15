# templates/LOM/__init__.py
from pathlib import Path
from typing import Optional, Dict, Any
from template_engine import render_with_template

def process_lom_template(input_video_path: str,
                         output_video_path: str,
                         text: str,
                         options: Optional[Dict[str, Any]] = None):
    """
    Registry-compatible renderer entrypoint for LOM.
    Delegates to the generic engine with this folder as template_root.
    """
    template_root = Path(__file__).parent
    overrides = options or {}
    return render_with_template(
        input_video_path=input_video_path,
        output_video_path=output_video_path,
        text=text,
        template_root=str(template_root),
        overrides=overrides
    )
