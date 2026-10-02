# instagram_downloader.py
"""
Efficient Instagram reel downloader for the pipeline.

Behavior:
- Creates workspace/<canonical_id>/00_raw/ before downloading.
- Tries Instaloader first (writes directly into raw dir).
- Falls back to yt-dlp (writes directly into raw dir).
- Normalizes filenames to:
    raw_source.mp4 (or raw_source.mov)
    raw_caption.txt
    raw_meta.json.xz (or raw_meta.json)
    raw_thumb.jpg / raw_thumb.png
- Writes workspace/<canonical_id>/meta.json with original filenames and basic metadata.
- Returns the workspace Path on success, None on failure.

Usage:
    python instagram_downloader.py "https://www.instagram.com/reel/SHORTCODE/"
"""

import re
import json
import subprocess
import sys
import os
from pathlib import Path

# Ensure virtual env binaries (like yt-dlp) are in PATH
venv_bin = Path(sys.executable).parent
os.environ["PATH"] = os.path.pathsep.join([str(venv_bin), os.environ.get("PATH", "")])
from datetime import datetime
from typing import Optional

# optional: keep mapping helper available if you want to reuse it elsewhere
try:
    from downloader_mapper import map_and_move_downloaded_files  # noqa: F401
except Exception:
    # it's okay if mapper isn't present; this script writes minimal meta itself
    map_and_move_downloaded_files = None  # type: ignore

# attempt lazy import for instaloader; not fatal if missing
try:
    import instaloader
except Exception:
    instaloader = None  # type: ignore

SHORTCODE_PATTERNS = [
    r'/reel/([A-Za-z0-9_-]+)',
    r'/p/([A-Za-z0-9_-]+)',
    r'/tv/([A-Za-z0-9_-]+)'
]


def extract_shortcode(url: str) -> Optional[str]:
    if not url:
        return None
    for pat in SHORTCODE_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    # fallback: any alnum token (6+ chars)
    m2 = re.search(r'([A-Za-z0-9_-]{6,})', url)
    if m2:
        return m2.group(1)
    return None


def ensure_workspace(base: str, canonical_id: str):
    basep = Path(base)
    ws = basep / canonical_id
    raw = ws / "00_raw"
    raw.mkdir(parents=True, exist_ok=True)
    return ws, raw


def _write_meta_atomic(ws: Path, meta: dict):
    tmp = ws / "meta.json.tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    tmp.replace(ws / "meta.json")


def normalize_and_write_meta(raw_dir: Path, source_url: str, downloader_name: str):
    """
    Scan raw_dir for files and rename them to standard names if needed.
    Writes meta.json in the workspace folder (parent of raw_dir).
    Does not copy large files — only renames within the same filesystem.
    """
    files = [p for p in raw_dir.iterdir() if p.is_file()]
    meta = {
        "canonical_id": raw_dir.parent.name,
        "source_url": source_url,
        "downloader": downloader_name,
        "original_files": {},
        "created_at": datetime.utcnow().isoformat() + "Z",
        "steps": {}
    }

    # pick main video (mp4 or mov)
    video = next((p for p in files if p.suffix.lower() in (".mp4", ".mov")), None)
    if video:
        target = raw_dir / ("raw_source" + video.suffix.lower())
        if video.name != target.name:
            # rename in-place
            try:
                video.rename(target)
                video = target
            except Exception:
                # if rename fails, keep original name as-is
                target = video
        meta["original_files"][target.name] = {"original_filename": video.name, "downloader": downloader_name, "size": target.stat().st_size}

    # caption / txt
    txt = next((p for p in files if p.suffix.lower() == ".txt"), None)
    if txt:
        target = raw_dir / "raw_caption.txt"
        if txt.name != target.name:
            try:
                txt.rename(target)
                txt = target
            except Exception:
                target = txt
        meta["original_files"][target.name] = {"original_filename": txt.name, "downloader": downloader_name, "size": target.stat().st_size}

    # json.xz or json
    jxz = next((p for p in files if "".join(p.suffixes).lower() in (".json.xz", ".json", ".xz")), None)
    if jxz:
        # prefer .json.xz naming if there's an .xz suffix
        joined = "".join(jxz.suffixes).lower()
        if joined.endswith(".xz"):
            target_name = "raw_meta.json.xz"
        else:
            target_name = "raw_meta.json"
        target = raw_dir / target_name
        if jxz.name != target.name:
            try:
                jxz.rename(target)
                jxz = target
            except Exception:
                target = jxz
        try:
            size = jxz.stat().st_size
        except Exception:
            size = None
        meta["original_files"][target.name] = {"original_filename": jxz.name, "downloader": downloader_name, "size": size}

    # thumbnail / jpg / png
    thumb = next((p for p in files if p.suffix.lower() in (".jpg", ".jpeg", ".png")), None)
    if thumb:
        target = raw_dir / ("raw_thumb" + thumb.suffix.lower())
        if thumb.name != target.name:
            try:
                thumb.rename(target)
                thumb = target
            except Exception:
                target = thumb
        meta["original_files"][target.name] = {"original_filename": thumb.name, "downloader": downloader_name, "size": target.stat().st_size}

    # write meta.json in workspace
    _write_meta_atomic(raw_dir.parent, meta)
    return raw_dir.parent


def try_instaloader_to_dir(shortcode: Optional[str], raw_dir: Path, url: str) -> bool:
    """
    Attempt to download using Instaloader writing directly into raw_dir.
    Returns True on success, False on failure.
    """
    if instaloader is None:
        print("Instaloader not installed / import failed; skipping.")
        return False

    # Instaloader likes to create targets by shortcode; pass dirname_pattern as parent dir.
    # We'll attempt to download the specific shortcode post if available.
    try:
        print("Instaloader: preparing to download into", str(raw_dir))
        # Use a dedicated Instaloader instance per download
        L = instaloader.Instaloader(dirname_pattern=str(raw_dir))
        if shortcode:
            # Post.from_shortcode requires the context of our Instaloader
            try:
                Post = instaloader.Post
                post = Post.from_shortcode(L.context, shortcode)
                L.download_post(post, target=shortcode)  # this would create a <raw_dir>/shortcode/... structure
                # Move nested shortcode folder contents (if any) up one level into raw_dir
                nested = raw_dir / shortcode
                if nested.exists() and nested.is_dir():
                    for child in nested.iterdir():
                        dest = raw_dir / child.name
                        if not dest.exists():
                            child.replace(dest)
                    # remove empty nested folder if possible
                    try:
                        nested.rmdir()
                    except Exception:
                        pass
                print("Instaloader: download complete")
                return True
            except Exception as e:
                print("Instaloader: failed to fetch by shortcode:", e)
                return False
        else:
            # If we don't have a shortcode, instaloader doesn't support direct URL download here reliably
            print("Instaloader: shortcode not available; skipping instaloader URL attempt")
            return False
    except Exception as e:
        print("Instaloader: error while attempting download:", e)
        return False


def try_ytdlp_to_dir(url: str, raw_dir: Path) -> bool:
    """
    Attempt to download using yt-dlp writing directly into raw_dir.
    Returns True on success, False on failure.
    """
    # ensure yt-dlp binary is callable
    output_template = str(raw_dir / "%(id)s.%(ext)s")
    cmd = ["yt-dlp", "-o", output_template, url]
    print("Running yt-dlp:", " ".join(cmd))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if res.returncode == 0:
            print("yt-dlp: success")
            return True
        else:
            print("yt-dlp: failed:", res.stderr.strip()[:400])
            return False
    except FileNotFoundError:
        print("yt-dlp binary not found. Install yt-dlp or ensure it's on PATH.")
        return False
    except Exception as e:
        print("yt-dlp: exception:", e)
        return False


def try_vxinstagram_to_dir(shortcode: str, raw_dir: Path) -> bool:
    """
    Attempt to download using vxinstagram or instagram7 proxies when standard downloaders are rate-limited or fail.
    Returns True on success, False on failure.
    """
    import urllib.request
    import urllib.error
    import html as html_lib
    
    # Custom redirect handler that preserves headers (User-Agent) to prevent 403 on CDN redirects
    class BindHeadersRedirectHandler(urllib.request.HTTPRedirectHandler):
        def __init__(self, original_req):
            self.original_req = original_req
            super().__init__()
            
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new_req is not None:
                for k, v in self.original_req.headers.items():
                    new_req.add_header(k, v)
            return new_req
            
    def open_url_preserving_headers(req, timeout=15):
        opener = urllib.request.build_opener(BindHeadersRedirectHandler(req))
        return opener.open(req, timeout=timeout)
    
    # Try multiple proxy domains for redundancy (prioritize instagram7 for its uncropped offload endpoint)
    proxy_domains = ["instagram7.com", "vxinstagram.com"]
    
    for domain in proxy_domains:
        # First: Try the direct offload URL to get the uncropped original media (useful for photo posts)
        offload_url = f"https://{domain}/offload/{shortcode}/1"
        print(f"Trying direct offload URL for uncropped media: {offload_url}")
        try:
            req = urllib.request.Request(
                offload_url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with open_url_preserving_headers(req, timeout=15) as response:
                content_type = response.headers.get("Content-Type", "")
                if "image" in content_type or "video" in content_type:
                    data = response.read()
                    if len(data) < 1000:
                        print(f"Downloaded media too small ({len(data)} bytes), likely placeholder. Skipping offload.")
                    else:
                        ext = ".mp4" if "video" in content_type else ".jpg"
                        dest = raw_dir / f"{shortcode}{ext}"
                        with open(dest, "wb") as f:
                            f.write(data)
                        print(f"Direct offload download succeeded ({domain})! Ext: {ext}, Size: {len(data)} bytes")
                        return True
        except Exception as e:
            print(f"Direct offload failed for {domain}: {e}")
            
        # Second: Fallback to HTML scraping
        for prefix in ("p", "reel"):
            proxy_url = f"https://{domain}/{prefix}/{shortcode}/"
            print(f"Querying proxy HTML: {proxy_url}")
            try:
                req = urllib.request.Request(
                    proxy_url, 
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                )
                with open_url_preserving_headers(req, timeout=15) as response:
                    html = response.read().decode('utf-8', errors='ignore')
                    
                # Search for video first
                video_match = re.search(r'property="og:video" content="([^"]+)"', html)
                if video_match:
                    video_url = html_lib.unescape(video_match.group(1))
                    print(f"Found video URL via proxy ({domain}): {video_url}")
                    dest = raw_dir / f"{shortcode}.mp4"
                    urllib.request.urlretrieve(video_url, dest)
                    return True
                    
                # Fallback to image
                image_match = re.search(r'property="og:image" content="([^"]+)"', html)
                if image_match:
                    image_url = html_lib.unescape(image_match.group(1))
                    print(f"Found image URL via proxy ({domain}): {image_url}")
                    dest = raw_dir / f"{shortcode}.jpg"
                    
                    # Follow and download from offload CDN
                    req_img = urllib.request.Request(
                        image_url,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                    )
                    with open_url_preserving_headers(req_img, timeout=15) as img_resp:
                        with open(dest, "wb") as f:
                            f.write(img_resp.read())
                    return True
                    
            except Exception as e:
                print(f"Proxy HTML scrape failed for {domain} with prefix {prefix}: {e}")
                
    return False


def download_instagram_reel_efficient(url: str, dest_base: str = "workspace") -> Optional[Path]:
    """
    High-level function:
    - picks canonical id (shortcode preferred, else timestamp)
    - ensures workspace/raw exists
    - tries instaloader -> normalize meta OR tries yt-dlp -> normalize meta OR tries vxinstagram -> normalize meta
    - returns workspace Path on success, None on failure
    """
    shortcode = extract_shortcode(url)
    canonical_id = shortcode or datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S_UTC")
    ws, raw = ensure_workspace(dest_base, canonical_id)

    # Attempt Instaloader first (preferred)
    if shortcode:
        ok = try_instaloader_to_dir(shortcode, raw, url)
        if ok:
            workspace = normalize_and_write_meta(raw, url, "instaloader")
            print("Workspace ready (instaloader):", workspace)
            return workspace
        else:
            print("Instaloader attempt failed or incomplete; falling back to yt-dlp.")

    # Fall back to yt-dlp
    ok2 = try_ytdlp_to_dir(url, raw)
    if ok2:
        workspace = normalize_and_write_meta(raw, url, "yt-dlp")
        print("Workspace ready (yt-dlp):", workspace)
        return workspace

    # Fall back to vxinstagram proxy
    if shortcode:
        ok3 = try_vxinstagram_to_dir(shortcode, raw)
        if ok3:
            workspace = normalize_and_write_meta(raw, url, "vxinstagram")
            print("Workspace ready (vxinstagram):", workspace)
            return workspace

    # all failed
    print("All downloaders failed for URL:", url)
    return None


if __name__ == "__main__":
    # Accept URL from command-line, else use example
    if len(sys.argv) >= 2:
        reel_url = sys.argv[1]
    else:
        reel_url = "https://www.instagram.com/reel/DPLeVCZk7wP/?igsh=MWRkN2h0MGt5NXR3OA==k"

    print("Downloading:", reel_url)
    ws = download_instagram_reel_efficient(reel_url, dest_base="workspace")
    if ws:
        print("Download + normalization finished. Workspace:", ws)
    else:
        print("Download failed. Check logs above for details.")
