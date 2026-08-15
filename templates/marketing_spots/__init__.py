# Templatea/templates/marketing_spots/__init__.py
from pathlib import Path
from template_engine import TemplateEngine, TemplateRenderRequest

TEMPLATE_ROOT = Path(__file__).parent

def render(input_video, output_video, text, options=None, top_text=None, bottom_text=None):
    request = TemplateRenderRequest(
        input_video_path=input_video,
        output_video_path=output_video,
        text=text,
        template_root=str(TEMPLATE_ROOT),
        overrides=options,
        top_text=top_text,  # NEW
        bottom_text=bottom_text,  # NEW
    )
    engine = TemplateEngine(request)
    return engine.render()

