"""
Batch video processor without AI - processes pre-existing videos with pre-written copies.

Usage:
  python batch-orchestrator-noai.py --assets-root <path> --template-id <template> [--workers 4] [--limit 10]

Structure expected:
  assets/
    video_assets/
      folder1/
        1.mp4
        2.mp4
        3.mp4
      folder2/
        1.mp4
        2.mp4
    copies/
      folder1.hook.json
      folder2.hook.json

Output structure:
  assets/
    video_assets/
      folder1/
        <template_id>/
          1.mp4
          2.mp4
          3.mp4
"""

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

# Import necessary functions from orchestrator
try:
    from orchestrator import (
        run_cmd,
        clean_text_for_render,
        normalize_dashes,
        _resolve_render_text_context,
        _load_template_config,
        _template_text_flags,
        read_meta,
        write_meta,
        ts,
    )
except ImportError:
    print("ERROR: Could not import from orchestrator.py. Ensure it's in the same directory.")
    sys.exit(1)

# Try to import template registry
try:
    from api.template_registry import (
        get_renderer_func,
        TemplateNotFound as TemplateRegistryError,
    )
except Exception:
    get_renderer_func = None

    class TemplateRegistryError(Exception):
        pass

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("batch_orchestrator_noai.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Disable Unicode characters for Windows console compatibility
USE_UNICODE = False

# Constants
DETECTOR_THRESHOLD = "10.0"
DEFAULT_WORKERS = 4


class VideoTask:
    """Represents a single video processing task."""

    def __init__(
        self,
        video_path: Path,
        output_dir: Path,
        copy_text: str,
        video_index: int,
        folder_name: str,
        template_id: str,
    ):
        self.video_path = video_path
        self.output_dir = output_dir
        self.copy_text = copy_text
        self.video_index = video_index
        self.folder_name = folder_name
        self.template_id = template_id
        self.task_id = f"{folder_name}_{video_index}"

    def __repr__(self):
        return f"VideoTask({self.task_id}, template={self.template_id})"


def load_hook_json(hook_file: Path) -> Dict[int, List[str]]:
    """Load hook.json and return mapping of video index to copy options."""
    try:
        with open(hook_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        hook_map = {}
        
        # Handle different JSON structures
        if isinstance(data, dict):
            # Structure: {"reels": [{"index": 1, "hook_options": [...]}]}
            reels_list = data.get("reels", [])
        elif isinstance(data, list):
            # Structure: [{"index": 1, "hook_options": [...]}]
            reels_list = data
        else:
            logger.error(f"Unexpected JSON structure in {hook_file}")
            return hook_map

        for reel in reels_list:
            index = reel.get("index")
            hook_options = reel.get("hook_options", [])
            if index is not None and hook_options:
                hook_map[index] = hook_options

        return hook_map

    except Exception as e:
        logger.error(f"Error loading {hook_file}: {e}")
        return {}


def run_video_detector(input_video: Path, output_video: Path) -> bool:
    """Run the video detector on the input video."""
    try:
        cmd = [
            "python",
            "video_detector.py",
            str(input_video),
            str(output_video),
            DETECTOR_THRESHOLD,
        ]
        result = run_cmd(cmd, timeout=120)
        
        if output_video.exists() and output_video.stat().st_size > 0:
            logger.info(f"Detector success: {output_video.name}")
            return True
        else:
            logger.error(f"Detector failed: output file missing or empty {output_video.name}")
            return False

    except Exception as e:
        logger.error(f"Detector exception for {input_video.name}: {e}")
        return False


def render_video(
    cropped_video: Path,
    output_video: Path,
    copy_text: str,
    template_id: str,
    template_options: Optional[Dict] = None,
) -> bool:
    """Render the video with the given copy text using the specified template."""
    if template_options is None:
        template_options = {}

    try:
        # Clean the text
        text_value = clean_text_for_render(normalize_dashes(copy_text))

        # Check template capabilities
        top_enabled, bottom_enabled = _template_text_flags(template_id)

        # Check for fixed bottom text
        cfg = _load_template_config(template_id)
        fixed_bottom = None
        if cfg and bottom_enabled:
            fixed_bottom = cfg.get("bottom_text", {}).get("fixed_value")
            if fixed_bottom:
                fixed_bottom = clean_text_for_render(normalize_dashes(fixed_bottom))

        # Prepare render context
        meta = {"template_id": template_id, "template_options": template_options}
        
        # Handle dual text if needed
        if top_enabled and bottom_enabled:
            if fixed_bottom:
                # Use fixed bottom text
                meta["dual_text"] = {
                    "top_text": text_value,
                    "bottom_text": fixed_bottom,
                    "source": "batch_fixed",
                }
            else:
                # Both texts are the same in batch mode
                meta["dual_text"] = {
                    "top_text": text_value,
                    "bottom_text": text_value,
                    "source": "batch_simple",
                }

        render_ctx = _resolve_render_text_context(meta, template_id, text_value)
        top_text_for_renderer = render_ctx["top_text"]
        bottom_text_for_renderer = render_ctx["bottom_text"]

        # Get renderer - try registry first, then fallback
        renderer = None
        if get_renderer_func:
            try:
                renderer = get_renderer_func(template_id)
            except Exception as e:
                logger.warning(f"Template registry lookup failed: {e}, trying fallback methods")
                renderer = None

        if renderer:
            # Call renderer with appropriate arguments
            render_kwargs: Dict[str, Any] = {}
            if top_text_for_renderer is not None:
                render_kwargs["top_text"] = top_text_for_renderer
            if bottom_text_for_renderer is not None:
                render_kwargs["bottom_text"] = bottom_text_for_renderer

            try:
                if render_kwargs:
                    ok = renderer(
                        str(cropped_video),
                        str(output_video),
                        text_value,
                        template_options,
                        **render_kwargs,
                    )
                else:
                    ok = renderer(
                        str(cropped_video),
                        str(output_video),
                        text_value,
                        template_options,
                    )
            except TypeError:
                # Fallback if renderer doesn't accept extra kwargs
                ok = renderer(
                    str(cropped_video),
                    str(output_video),
                    text_value,
                    template_options,
                )

            if ok is None or ok is True:
                if output_video.exists():
                    logger.info(f"Render success: {output_video.name}")
                    return True
                else:
                    logger.error(f"Render failed: output file missing {output_video.name}")
                    return False
            else:
                logger.error(f"Renderer returned False for {output_video.name}")
                return False
        else:
            # Try legacy fallback methods
            logger.info("Trying legacy render methods")
            
            # Try importing the template module directly
            try:
                # Try to import as a module
                import importlib
                template_module = importlib.import_module(f"templates.{template_id}.renderer")
                if hasattr(template_module, "render"):
                    render_func = template_module.render
                    ok = render_func(
                        str(cropped_video),
                        str(output_video),
                        text_value,
                        template_options,
                    )
                    if ok is None or ok is True:
                        if output_video.exists():
                            logger.info(f"Render success (module): {output_video.name}")
                            return True
                    logger.error(f"Module render failed for {output_video.name}")
                    return False
            except Exception as e:
                logger.warning(f"Could not import template module: {e}")
            
            # Try legacy marketingspots_template
            try:
                from marketingspots_template import process_marketingspots_template
                ok = process_marketingspots_template(str(cropped_video), str(output_video), text_value)
                if ok is None or ok is True:
                    if output_video.exists():
                        logger.info(f"Render success (legacy): {output_video.name}")
                        return True
                logger.error(f"Legacy render failed for {output_video.name}")
                return False
            except Exception as e:
                logger.warning(f"Legacy render not available: {e}")
            
            logger.error(f"No renderer found for template: {template_id}")
            return False

    except Exception as e:
        logger.error(f"Render exception for {output_video.name}: {e}", exc_info=True)
        return False


def process_task(task: VideoTask) -> Dict[str, Any]:
    """Process a single video task."""
    result = {
        "task_id": task.task_id,
        "folder": task.folder_name,
        "index": task.video_index,
        "input": str(task.video_path),
        "copy": task.copy_text,
        "success": False,
        "error": None,
        "detector_success": False,
        "render_success": False,
        "output": None,
    }

    try:
        # Create temp directory for this task
        temp_dir = task.output_dir / ".temp"
        temp_dir.mkdir(exist_ok=True, parents=True)

        # Step 1: Run detector
        cropped_video = temp_dir / f"{task.video_index}_cropped.mp4"
        detector_success = run_video_detector(task.video_path, cropped_video)
        result["detector_success"] = detector_success

        if not detector_success:
            result["error"] = "Detector failed"
            return result

        # Step 2: Render video
        output_video = task.output_dir / f"{task.video_index}.mp4"
        render_success = render_video(
            cropped_video, output_video, task.copy_text, task.template_id
        )
        result["render_success"] = render_success

        if render_success:
            result["success"] = True
            result["output"] = str(output_video)
        else:
            result["error"] = "Render failed"

        # Cleanup temp files
        try:
            if cropped_video.exists():
                cropped_video.unlink()
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Task {task.task_id} failed: {e}", exc_info=True)

    return result


def collect_tasks(
    assets_root: Path,
    template_id: str,
    limit: Optional[int] = None,
) -> List[VideoTask]:
    """Collect all video tasks from the assets directory."""
    tasks = []
    
    video_assets_dir = assets_root / "video_assets"
    copies_dir = assets_root / "copies"
    
    if not video_assets_dir.exists():
        logger.error(f"Video assets directory not found: {video_assets_dir}")
        return tasks
    
    if not copies_dir.exists():
        logger.error(f"Copies directory not found: {copies_dir}")
        return tasks
    
    # Iterate through each folder in video_assets
    for folder in sorted(video_assets_dir.iterdir()):
        if not folder.is_dir():
            continue
        
        folder_name = folder.name
        hook_file = copies_dir / f"{folder_name}.hook.json"
        
        if not hook_file.exists():
            logger.warning(f"No hook file found for folder: {folder_name}")
            continue
        
        # Load copy options
        hook_map = load_hook_json(hook_file)
        if not hook_map:
            logger.warning(f"No valid copies in hook file: {hook_file}")
            continue
        
        # Find all video files in the folder
        video_files = sorted(folder.glob("*.mp4"))
        
        for video_file in video_files:
            # Extract video index from filename (e.g., "1.mp4" -> 1)
            try:
                video_index = int(video_file.stem)
            except ValueError:
                logger.warning(f"Skipping non-numeric video file: {video_file}")
                continue
            
            # Get copy text for this video
            copy_options = hook_map.get(video_index)
            if not copy_options:
                logger.warning(f"No copy found for {folder_name}/{video_index}.mp4")
                continue
            
            # Select a random copy from options
            copy_text = random.choice(copy_options)
            
            # Create output directory
            output_dir = folder / template_id
            output_dir.mkdir(exist_ok=True, parents=True)
            
            # Create task
            task = VideoTask(
                video_path=video_file,
                output_dir=output_dir,
                copy_text=copy_text,
                video_index=video_index,
                folder_name=folder_name,
                template_id=template_id,
            )
            tasks.append(task)
            
            # Check limit
            if limit and len(tasks) >= limit:
                logger.info(f"Reached task limit: {limit}")
                return tasks
    
    return tasks


def main():
    parser = argparse.ArgumentParser(
        description="Batch process videos without AI copy generation"
    )
    parser.add_argument(
        "--assets-root",
        required=True,
        type=Path,
        help="Root assets directory containing video_assets and copies folders",
    )
    parser.add_argument(
        "--template-id",
        required=True,
        help="Template ID to use for rendering",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of concurrent workers (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of videos to process (for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without actually processing",
    )

    args = parser.parse_args()

    # Validate paths
    if not args.assets_root.exists():
        logger.error(f"Assets root does not exist: {args.assets_root}")
        sys.exit(1)

    # Collect tasks
    logger.info(f"Collecting tasks from {args.assets_root}...")
    tasks = collect_tasks(args.assets_root, args.template_id, args.limit)

    if not tasks:
        logger.error("No tasks to process!")
        sys.exit(1)

    logger.info(f"Collected {len(tasks)} tasks")

    if args.dry_run:
        logger.info("\n=== DRY RUN - Tasks that would be processed ===")
        for task in tasks:
            logger.info(
                f"  {task.task_id}: {task.video_path.name} -> {task.output_dir}/{task.video_index}.mp4"
            )
            logger.info(f"    Copy: {task.copy_text[:60]}...")
        logger.info("=== End of dry run ===")
        return

    # Process tasks concurrently
    logger.info(f"\nStarting processing with {args.workers} workers...")
    start_time = time.time()
    results = []
    success_count = 0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        # Submit all tasks
        future_to_task = {executor.submit(process_task, task): task for task in tasks}

        # Process completed tasks
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                result = future.result()
                results.append(result)

                if result["success"]:
                    success_count += 1
                    symbol = "OK" if not USE_UNICODE else "✓"
                    logger.info(
                        f"{symbol} [{success_count + failed_count}/{len(tasks)}] {result['task_id']} SUCCESS"
                    )
                else:
                    failed_count += 1
                    symbol = "FAIL" if not USE_UNICODE else "✗"
                    logger.error(
                        f"{symbol} [{success_count + failed_count}/{len(tasks)}] {result['task_id']} FAILED: {result.get('error')}"
                    )

            except Exception as e:
                failed_count += 1
                logger.error(f"Task {task.task_id} raised exception: {e}")
                results.append(
                    {
                        "task_id": task.task_id,
                        "success": False,
                        "error": str(e),
                    }
                )

    # Calculate statistics
    elapsed_time = time.time() - start_time
    total = len(tasks)

    # Write report
    report = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "template_id": args.template_id,
        "assets_root": str(args.assets_root),
        "workers": args.workers,
        "limit": args.limit,
        "total_tasks": total,
        "successful": success_count,
        "failed": failed_count,
        "elapsed_seconds": round(elapsed_time, 2),
        "results": results,
    }

    report_file = Path("batch_orchestrator_report.json")
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Print summary
    logger.info("\n" + "=" * 80)
    logger.info("PROCESSING COMPLETE")
    logger.info("=" * 80)
    logger.info(f"Total tasks: {total}")
    logger.info(f"Successful: {success_count}")
    logger.info(f"Failed: {failed_count}")
    logger.info(f"Success rate: {(success_count/total*100):.1f}%")
    logger.info(f"Time elapsed: {elapsed_time:.1f}s")
    logger.info(f"Average time per video: {(elapsed_time/total):.1f}s")
    logger.info(f"Report saved to: {report_file}")
    logger.info("=" * 80)

    # Exit with error code if any failures
    if failed_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()