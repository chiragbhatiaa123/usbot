#!/usr/bin/env python3
"""
Quick test to verify cache clearing is implemented correctly.

Run this in your project root:
    python test_cache_clearing.py DPnX-1IErws
"""

import sys
from pathlib import Path

def test_cache_clearing(workspace_id: str):
    """Test if cache clearing function exists and works."""
    
    print(f"Testing cache clearing for workspace: {workspace_id}")
    print("-" * 60)
    
    # Check if workspace exists
    workspace_path = Path("workspace") / workspace_id
    if not workspace_path.exists():
        print(f"❌ Workspace not found: {workspace_path}")
        return False
    
    print(f"✅ Workspace found: {workspace_path}")
    
    # Check render directory
    render_dir = workspace_path / "04_render"
    if not render_dir.exists():
        print("⚠️  No render directory found (might not have rendered yet)")
        return True
    
    print(f"✅ Render directory exists: {render_dir}")
    
    # Check for cached files
    templates_dir = render_dir / "templates"
    final_video = render_dir / "final_1080x1920.mp4"
    
    print(f"\nCached files before clearing:")
    print(f"  - Templates dir: {'EXISTS' if templates_dir.exists() else 'NOT FOUND'}")
    print(f"  - Final video: {'EXISTS' if final_video.exists() else 'NOT FOUND'}")
    
    # Try to import and use the clear_render_cache function
    try:
        sys.path.insert(0, str(Path.cwd()))
        from api.tasks import clear_render_cache
        print("\n✅ Successfully imported clear_render_cache from api.tasks")
    except ImportError as e:
        print(f"\n❌ Failed to import clear_render_cache: {e}")
        print("\n⚠️  YOU NEED TO IMPLEMENT THE CACHE CLEARING!")
        print("    See the implementation guide artifact for instructions.")
        return False
    
    # Call the function
    print(f"\nCalling clear_render_cache('{workspace_id}')...")
    try:
        clear_render_cache(workspace_id)
        print("✅ Function executed successfully")
    except Exception as e:
        print(f"❌ Function failed: {e}")
        return False
    
    # Check if files were deleted
    print(f"\nCached files after clearing:")
    print(f"  - Templates dir: {'EXISTS' if templates_dir.exists() else 'DELETED ✅'}")
    print(f"  - Final video: {'EXISTS' if final_video.exists() else 'DELETED ✅'}")
    
    if not templates_dir.exists() and not final_video.exists():
        print("\n✅ SUCCESS! Cache clearing is working correctly.")
        return True
    else:
        print("\n⚠️  Some files still exist. Cache clearing may not be working properly.")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_cache_clearing.py <workspace_id>")
        print("Example: python test_cache_clearing.py DPnX-1IErws")
        sys.exit(1)
    
    workspace_id = sys.argv[1]
    success = test_cache_clearing(workspace_id)
    sys.exit(0 if success else 1)