#!/usr/bin/env python3
"""
Video-in-Video Batch Processor
Processes multiple videos to detect and extract inner video content.

Usage:
    python batch_process.py <input_dir> <output_dir> [options]
    python batch_process.py <input_file1> <input_file2> ... -o <output_dir> [options]

Examples:
    # Process all videos in a directory
    python batch_process.py ./videos ./output

    # Process specific files
    python batch_process.py video1.mp4 video2.mp4 video3.mp4 -o ./output

    # With custom settings
    python batch_process.py ./videos ./output --threshold 70 --workers 4

    # Recursive directory processing
    python batch_process.py ./videos ./output --recursive

    # Filter by extension
    python batch_process.py ./videos ./output --extensions mp4 mov avi
"""

import argparse
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from datetime import datetime
import traceback

# Import the detection algorithm
try:
    from video_detector import detect_and_crop_video
except ImportError:
    print("Error: video_detector.py not found in the same directory")
    print("Please ensure the detection algorithm file is present")
    sys.exit(1)


def get_video_files(paths, extensions, recursive=False):
    """
    Collect all video files from given paths.
    
    Args:
        paths: List of file or directory paths
        extensions: List of video extensions to include
        recursive: Whether to search directories recursively
    
    Returns:
        List of Path objects for video files
    """
    video_files = []
    extensions = [ext.lower().strip('.') for ext in extensions]
    
    for path_str in paths:
        path = Path(path_str)
        
        if not path.exists():
            print(f"Warning: Path does not exist: {path}")
            continue
        
        if path.is_file():
            if path.suffix.lower().strip('.') in extensions:
                video_files.append(path)
            else:
                print(f"Warning: Skipping non-video file: {path}")
        
        elif path.is_dir():
            if recursive:
                for ext in extensions:
                    video_files.extend(path.rglob(f"*.{ext}"))
            else:
                for ext in extensions:
                    video_files.extend(path.glob(f"*.{ext}"))
    
    return sorted(set(video_files))


def process_single_video(args_tuple):
    """
    Process a single video file.
    
    Args:
        args_tuple: Tuple of (input_path, output_path, threshold, preserve_structure, base_input_dir)
    
    Returns:
        dict: Processing result
    """
    input_path, output_path, threshold, preserve_structure, base_input_dir = args_tuple
    
    try:
        start_time = datetime.now()
        
        # Create output directory if needed
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Run detection
        result = detect_and_crop_video(
            str(input_path),
            str(output_path),
            confidence_threshold=threshold
        )
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        # Add metadata
        result['input_file'] = str(input_path)
        result['output_file'] = str(output_path)
        result['processing_time'] = duration
        result['status'] = 'success'
        result['error'] = None
        
        return result
    
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        traceback_str = traceback.format_exc()
        
        return {
            'input_file': str(input_path),
            'output_file': str(output_path),
            'status': 'error',
            'error': error_msg,
            'traceback': traceback_str,
            'detected': False,
            'confidence': 0.0
        }


def generate_output_path(input_path, output_dir, preserve_structure, base_input_dir, suffix):
    """
    Generate output path for a video file.
    
    Args:
        input_path: Path to input video
        output_dir: Output directory
        preserve_structure: Whether to preserve directory structure
        base_input_dir: Base directory for structure preservation
        suffix: Suffix to add to filename
    
    Returns:
        Path: Output file path
    """
    output_dir = Path(output_dir)
    
    if preserve_structure and base_input_dir:
        # Preserve directory structure
        relative_path = input_path.relative_to(base_input_dir)
        output_path = output_dir / relative_path.parent / f"{input_path.stem}{suffix}{input_path.suffix}"
    else:
        # Flat structure
        output_path = output_dir / f"{input_path.stem}{suffix}{input_path.suffix}"
    
    return output_path


def print_progress(completed, total, current_file=""):
    """Print progress bar."""
    percent = (completed / total) * 100 if total > 0 else 0
    bar_length = 40
    filled = int(bar_length * completed / total) if total > 0 else 0
    bar = '█' * filled + '░' * (bar_length - filled)
    
    print(f"\r[{bar}] {completed}/{total} ({percent:.1f}%) - {current_file[:50]}", end='', flush=True)


def save_report(results, output_dir, report_name="processing_report.json"):
    """Save processing report to JSON file."""
    report_path = Path(output_dir) / report_name
    
    # Create summary
    total = len(results)
    successful = sum(1 for r in results if r['status'] == 'success')
    detected = sum(1 for r in results if r.get('detected', False))
    errors = sum(1 for r in results if r['status'] == 'error')
    
    avg_confidence = sum(r.get('confidence', 0) for r in results if r.get('detected', False)) / detected if detected > 0 else 0
    total_time = sum(r.get('processing_time', 0) for r in results if 'processing_time' in r)
    
    report = {
        'summary': {
            'total_videos': total,
            'successful': successful,
            'detected': detected,
            'errors': errors,
            'average_confidence': round(avg_confidence, 2),
            'total_processing_time': round(total_time, 2),
            'timestamp': datetime.now().isoformat()
        },
        'results': results
    }
    
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)
    
    return report_path


def main():
    parser = argparse.ArgumentParser(
        description='Batch process videos to detect and extract inner video content',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        'inputs',
        nargs='+',
        help='Input video files or directories'
    )
    
    parser.add_argument(
        '-o', '--output',
        required=True,
        help='Output directory for processed videos'
    )
    
    parser.add_argument(
        '-t', '--threshold',
        type=float,
        default=60.0,
        help='Confidence threshold for detection (0-100, default: 60.0)'
    )
    
    parser.add_argument(
        '-w', '--workers',
        type=int,
        default=1,
        help='Number of parallel workers (default: 1)'
    )
    
    parser.add_argument(
        '-r', '--recursive',
        action='store_true',
        help='Process directories recursively'
    )
    
    parser.add_argument(
        '-e', '--extensions',
        nargs='+',
        default=['mp4', 'mov', 'avi', 'mkv', 'webm'],
        help='Video file extensions to process (default: mp4 mov avi mkv webm)'
    )
    
    parser.add_argument(
        '-s', '--suffix',
        default='_cropped',
        help='Suffix to add to output filenames (default: _cropped)'
    )
    
    parser.add_argument(
        '--preserve-structure',
        action='store_true',
        help='Preserve input directory structure in output'
    )
    
    parser.add_argument(
        '--report',
        default='processing_report.json',
        help='Name of the processing report file (default: processing_report.json)'
    )
    
    parser.add_argument(
        '--skip-existing',
        action='store_true',
        help='Skip files that already exist in output directory'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='List files that would be processed without actually processing them'
    )
    
    args = parser.parse_args()
    
    # Validate threshold
    if not 0 <= args.threshold <= 100:
        print(f"Error: Threshold must be between 0 and 100 (got {args.threshold})")
        sys.exit(1)
    
    # Validate workers
    if args.workers < 1:
        print(f"Error: Workers must be at least 1 (got {args.workers})")
        sys.exit(1)
    
    # Collect video files
    print("Scanning for video files...")
    video_files = get_video_files(args.inputs, args.extensions, args.recursive)
    
    if not video_files:
        print("No video files found matching criteria")
        sys.exit(0)
    
    print(f"Found {len(video_files)} video file(s)")
    
    # Determine base input directory for structure preservation
    base_input_dir = None
    if args.preserve_structure:
        input_paths = [Path(p) for p in args.inputs]
        dirs = [p if p.is_dir() else p.parent for p in input_paths]
        if len(dirs) == 1:
            base_input_dir = dirs[0]
        else:
            base_input_dir = Path.cwd()
    
    # Prepare processing tasks
    tasks = []
    for video_file in video_files:
        output_path = generate_output_path(
            video_file,
            args.output,
            args.preserve_structure,
            base_input_dir,
            args.suffix
        )
        
        # Skip if file exists and skip-existing is set
        if args.skip_existing and output_path.exists():
            print(f"Skipping existing: {output_path}")
            continue
        
        tasks.append((video_file, output_path, args.threshold, args.preserve_structure, base_input_dir))
    
    if not tasks:
        print("No files to process (all outputs already exist)")
        sys.exit(0)
    
    print(f"\nWill process {len(tasks)} video(s)")
    print(f"Output directory: {args.output}")
    print(f"Confidence threshold: {args.threshold}")
    print(f"Parallel workers: {args.workers}")
    
    # Dry run mode
    if args.dry_run:
        print("\n=== DRY RUN MODE ===")
        print("\nFiles that would be processed:")
        for input_path, output_path, _, _, _ in tasks:
            print(f"  {input_path} -> {output_path}")
        print(f"\nTotal: {len(tasks)} files")
        sys.exit(0)
    
    # Create output directory
    Path(args.output).mkdir(parents=True, exist_ok=True)
    
    # Process videos
    print("\nProcessing videos...")
    results = []
    
    if args.workers == 1:
        # Sequential processing
        for i, task in enumerate(tasks):
            print_progress(i, len(tasks), task[0].name)
            result = process_single_video(task)
            results.append(result)
        print_progress(len(tasks), len(tasks), "Complete")
    else:
        # Parallel processing
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_single_video, task): task for task in tasks}
            
            completed = 0
            for future in as_completed(futures):
                task = futures[future]
                result = future.result()
                results.append(result)
                completed += 1
                print_progress(completed, len(tasks), task[0].name)
    
    print()  # New line after progress bar
    
    # Generate report
    print("\nGenerating report...")
    report_path = save_report(results, args.output, args.report)
    
    # Print summary
    successful = sum(1 for r in results if r['status'] == 'success')
    detected = sum(1 for r in results if r.get('detected', False))
    errors = sum(1 for r in results if r['status'] == 'error')
    
    print("\n" + "="*60)
    print("PROCESSING COMPLETE")
    print("="*60)
    print(f"Total videos: {len(results)}")
    print(f"Successful: {successful}")
    print(f"Video-in-video detected: {detected}")
    print(f"Errors: {errors}")
    
    if detected > 0:
        avg_conf = sum(r.get('confidence', 0) for r in results if r.get('detected', False)) / detected
        print(f"Average confidence: {avg_conf:.1f}%")
    
    print(f"\nReport saved to: {report_path}")
    
    # Print errors if any
    if errors > 0:
        print("\n" + "="*60)
        print("ERRORS")
        print("="*60)
        for result in results:
            if result['status'] == 'error':
                print(f"\n{result['input_file']}:")
                print(f"  {result['error']}")
    
    print()


if __name__ == "__main__":
    main()