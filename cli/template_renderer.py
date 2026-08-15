#!/usr/bin/env python3
"""
Command-line entrypoint for the template rendering engine.

Examples:
    # Basic usage with single text
    python cli/template_renderer.py --input input.mp4 --output out.mp4 --text "Hello world"
    
    # With choice file
    python cli/template_renderer.py --input in.mp4 --output out.mp4 --choice-file workspace/03_choice/choice.txt --template-root templates/clean_ad
    
    # With overrides
    python cli/template_renderer.py --input in.mp4 --output out.mp4 --text "Ad copy" --override text.color="#ff00ff"
    
    # NEW: Dual text support
    python cli/template_renderer.py --input in.mp4 --output out.mp4 --top-text "HEADLINE" --bottom-text "Subtext" --template-root templates/dual_text
    
    # NEW: Mixed (top different, bottom from file)
    python cli/template_renderer.py --input in.mp4 --output out.mp4 --top-text "Custom Top" --choice-file choice.txt --template-root templates/dual_text
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from template_engine import TemplateEngine, TemplateRenderRequest  # noqa: E402

DEFAULT_TEMPLATE_DIR = ROOT / "templates" / "default"


def _parse_override(raw: str) -> Tuple[str, Any]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("Overrides must be provided as key=value")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError("Override key cannot be empty")
    value = value.strip()
    if not value:
        return key, ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    return key, parsed


def _collect_overrides(raw_items: Optional[Iterable[str]]) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    if not raw_items:
        return overrides
    for raw in raw_items:
        key, value = _parse_override(raw)
        overrides[key] = value
    return overrides


def _resolve_text(text: Optional[str], choice_file: Optional[Path]) -> str:
    if choice_file:
        try:
            return choice_file.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            raise SystemExit(f"Choice file not found: {choice_file}")
    if text is not None:
        return text
    raise SystemExit("Either --text or --choice-file must be provided.")


def _format_preview(request: TemplateRenderRequest, overrides: Dict[str, Any]) -> str:
    payload = {
        "input": request.input_video_path,
        "output": request.output_video_path,
        "template_root": request.template_root,
        "text": request.text,
        "top_text": request.top_text,
        "bottom_text": request.bottom_text,
        "overrides": overrides,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a video using a template.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single text (backward compatible)
  %(prog)s --input video.mp4 --output out.mp4 --text "Hello World"
  
  # Dual text with different content
  %(prog)s --input video.mp4 --output out.mp4 --top-text "HEADLINE" --bottom-text "Subtext"
  
  # Top text custom, bottom from file
  %(prog)s --input video.mp4 --output out.mp4 --top-text "SAMSUNG" --choice-file text.txt
  
  # Both from same file
  %(prog)s --input video.mp4 --output out.mp4 --choice-file text.txt
  
  # With template overrides
  %(prog)s --input video.mp4 --output out.mp4 --text "Hello" --override top_text.color="#ff0000"
        """
    )
    
    # Required arguments
    parser.add_argument("--input", required=True, dest="input_video", help="Source video to composite.")
    parser.add_argument("--output", required=True, dest="output_video", help="Destination MP4 path.")
    
    # Text input options (mutually exclusive groups handled in validation)
    text_group = parser.add_argument_group('text input options')
    text_group.add_argument("--text", help="Default text (used for both top/bottom if not specified separately).")
    text_group.add_argument("--choice-file", type=Path, help="Path to text file (used as fallback if top/bottom not specified).")
    text_group.add_argument("--top-text", dest="top_text", help="Specific text for top_text block.")
    text_group.add_argument("--bottom-text", dest="bottom_text", help="Specific text for bottom_text block.")
    text_group.add_argument("--top-choice-file", type=Path, dest="top_choice_file", help="Text file for top_text block.")
    text_group.add_argument("--bottom-choice-file", type=Path, dest="bottom_choice_file", help="Text file for bottom_text block.")
    
    # Template and overrides
    parser.add_argument(
        "--template-root",
        default=str(DEFAULT_TEMPLATE_DIR),
        help=f"Template folder (default: {DEFAULT_TEMPLATE_DIR}).",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        help="Template override as key=value (supports dotted keys). Repeat for multiple overrides.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve inputs and print payload without rendering.",
    )
    return parser


def run_cli(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input_video)
    output_path = Path(args.output_video)
    template_root = Path(args.template_root).resolve()
    overrides = _collect_overrides(args.override)

    # Resolve text inputs with priority system
    # Priority: specific text > specific choice file > general text > general choice file
    
    # Default/fallback text
    default_text = None
    if args.choice_file:
        try:
            default_text = args.choice_file.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            parser.error(f"Choice file not found: {args.choice_file}")
    elif args.text:
        default_text = args.text
    
    # Top text
    top_text = None
    if args.top_choice_file:
        try:
            top_text = args.top_choice_file.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            parser.error(f"Top choice file not found: {args.top_choice_file}")
    elif args.top_text:
        top_text = args.top_text
    
    # Bottom text
    bottom_text = None
    if args.bottom_choice_file:
        try:
            bottom_text = args.bottom_choice_file.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            parser.error(f"Bottom choice file not found: {args.bottom_choice_file}")
    elif args.bottom_text:
        bottom_text = args.bottom_text
    
    # Validation: At least one text source must be provided
    if not any([default_text, top_text, bottom_text]):
        parser.error("At least one text source required: --text, --choice-file, --top-text, --bottom-text, --top-choice-file, or --bottom-choice-file")
    
    # If no default text but have specific texts, use one as fallback
    if not default_text:
        default_text = top_text or bottom_text or ""

    if not input_path.exists() and not args.dry_run:
        parser.error(f"Input video not found: {input_path}")

    if not args.dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    if not template_root.exists():
        parser.error(f"Template root not found: {template_root}")

    request = TemplateRenderRequest(
        input_video_path=str(input_path),
        output_video_path=str(output_path),
        text=default_text,
        template_root=str(template_root),
        overrides=overrides or None,
        top_text=top_text,
        bottom_text=bottom_text,
    )

    if args.dry_run:
        print(_format_preview(request, overrides))
        return 0

    engine = TemplateEngine(request)
    success = engine.render()
    if not success:
        return 1
    print(f"[template_renderer] Rendered {output_path}")
    return 0


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()