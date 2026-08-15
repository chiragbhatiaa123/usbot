#!/usr/bin/env python
"""
Queue Client CLI - Add URLs to the processing queue from another terminal.

Usage:
    python queue_client.py add <url>              # Add single URL
    python queue_client.py add-file <file>        # Add URLs from file (one per line)
    python queue_client.py list [--status=X]      # Show queue entries
    python queue_client.py stats                  # Show queue statistics
    python queue_client.py clear                  # Clear completed entries
    python queue_client.py clear-all              # Clear ALL entries (careful!)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add parent directory to path so we can import api.queue
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api.queue import (
    add_url,
    add_urls,
    list_queue,
    get_queue_stats,
    clear_completed,
    clear_all,
)


def cmd_add(args: argparse.Namespace) -> None:
    """Add a single URL to the queue."""
    url = args.url.strip()
    if not url:
        print("Error: URL cannot be empty")
        sys.exit(1)
    
    auto = not args.no_auto  # Default is auto=True unless --no-auto specified
    queue_id = add_url(url, template_id=args.template, auto=auto)
    print(f"✓ Added to queue (ID: {queue_id})")
    print(f"  URL: {url}")
    print(f"  Auto: {'Yes' if auto else 'No (manual copy selection required)'}")
    if args.template:
        print(f"  Template: {args.template}")


def cmd_add_file(args: argparse.Namespace) -> None:
    """Add multiple URLs from a file."""
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File not found: {file_path}")
        sys.exit(1)
    
    urls = [line.strip() for line in file_path.read_text().splitlines() if line.strip()]
    if not urls:
        print("Error: No URLs found in file")
        sys.exit(1)
    
    auto = not args.no_auto
    ids = add_urls(urls, template_id=args.template, auto=auto)
    print(f"✓ Added {len(ids)} URLs to queue (auto={'Yes' if auto else 'No'})")
    for url, qid in zip(urls, ids):
        print(f"  [{qid}] {url[:60]}{'...' if len(url) > 60 else ''}")


def cmd_list(args: argparse.Namespace) -> None:
    """List queue entries."""
    entries = list_queue(status_filter=args.status, limit=args.limit)
    
    if not entries:
        print("Queue is empty" + (f" (status={args.status})" if args.status else ""))
        return
    
    print(f"{'ID':<6} {'Status':<12} {'URL':<50} {'Workspace'}")
    print("-" * 90)
    
    for entry in entries:
        url = entry["url"]
        url_display = url[:47] + "..." if len(url) > 50 else url
        ws = entry.get("workspace_id") or "-"
        print(f"{entry['id']:<6} {entry['status']:<12} {url_display:<50} {ws}")
    
    if len(entries) == args.limit:
        print(f"\n(Showing first {args.limit} entries, use --limit to see more)")


def cmd_stats(args: argparse.Namespace) -> None:
    """Show queue statistics."""
    stats = get_queue_stats()
    
    if not stats:
        print("Queue is empty")
        return
    
    total = sum(stats.values())
    print("Queue Statistics:")
    print("-" * 30)
    for status, count in sorted(stats.items()):
        print(f"  {status:<15} {count:>5}")
    print("-" * 30)
    print(f"  {'Total':<15} {total:>5}")


def cmd_clear(args: argparse.Namespace) -> None:
    """Clear completed entries."""
    count = clear_completed()
    print(f"✓ Cleared {count} completed entries")


def cmd_clear_all(args: argparse.Namespace) -> None:
    """Clear ALL entries."""
    confirm = input("Are you sure you want to clear ALL queue entries? (yes/no): ")
    if confirm.lower() != "yes":
        print("Cancelled")
        return
    
    count = clear_all()
    print(f"✓ Cleared {count} entries")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Queue Client - Manage URL processing queue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # add command
    add_parser = subparsers.add_parser("add", help="Add a single URL to the queue")
    add_parser.add_argument("url", help="Instagram reel URL to add")
    add_parser.add_argument("--template", "-t", required=True, help="Template ID to use for rendering")
    add_parser.add_argument("--no-auto", action="store_true", help="Disable auto copy selection (require manual)")
    add_parser.set_defaults(func=cmd_add)
    
    # add-file command
    add_file_parser = subparsers.add_parser("add-file", help="Add URLs from a file")
    add_file_parser.add_argument("file", help="File containing URLs (one per line)")
    add_file_parser.add_argument("--template", "-t", required=True, help="Template ID to use for rendering")
    add_file_parser.add_argument("--no-auto", action="store_true", help="Disable auto copy selection (require manual)")
    add_file_parser.set_defaults(func=cmd_add_file)
    
    # list command
    list_parser = subparsers.add_parser("list", help="List queue entries")
    list_parser.add_argument("--status", "-s", help="Filter by status (pending/processing/completed/failed)")
    list_parser.add_argument("--limit", "-l", type=int, default=20, help="Max entries to show (default: 20)")
    list_parser.set_defaults(func=cmd_list)
    
    # stats command
    stats_parser = subparsers.add_parser("stats", help="Show queue statistics")
    stats_parser.set_defaults(func=cmd_stats)
    
    # clear command
    clear_parser = subparsers.add_parser("clear", help="Clear completed entries")
    clear_parser.set_defaults(func=cmd_clear)
    
    # clear-all command
    clear_all_parser = subparsers.add_parser("clear-all", help="Clear ALL entries (dangerous!)")
    clear_all_parser.set_defaults(func=cmd_clear_all)
    
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
