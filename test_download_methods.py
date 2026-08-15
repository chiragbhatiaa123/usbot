import subprocess
import requests

def method1_instaloader(url, shortcode, output_dir="."):
    """Test Instaloader for a reel"""
    print("Testing Method 1: Instaloader...")
    try:
        import instaloader
        from instaloader import Post

        L = instaloader.Instaloader(
            dirname_pattern=output_dir,
            filename_pattern="{shortcode}",
            download_comments=False,
            download_geotags=False,
            save_metadata=False
        )
        post = Post.from_shortcode(L.context, shortcode)
        L.download_post(post, target=shortcode)
        print("Method 1: SUCCESS")
    except Exception as e:
        print(f"Method 1: FAILED - {e}")

def method2_ytdlp(url, output_dir="."):
    """Test yt-dlp for a reel"""
    print("Testing Method 2: yt-dlp...")
    try:
        output_template = "%(id)s.%(ext)s"
        cmd = ["yt-dlp", "-o", output_template, url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            print("Method 2: SUCCESS")
        else:
            print(f"Method 2: FAILED - {result.stderr}")
    except Exception as e:
        print(f"Method 2: FAILED - {e}")

def method3_instagrapi(shortcode, output_dir="."):
    """Test instagrapi for a reel (public reel only)"""
    print("Testing Method 3: instagrapi...")
    try:
        from instagrapi import Client
        cl = Client()
        media_pk = cl.media_pk_from_code(shortcode)
        media_info = cl.media_info(media_pk)
        if media_info.video_url:
            video_url = str(media_info.video_url)
            response = requests.get(video_url, stream=True)
            if response.status_code == 200:
                filepath = f"{shortcode}.mp4"
                with open(filepath, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
                print("Method 3: SUCCESS")
            else:
                print("Method 3: FAILED - Couldn't fetch video")
        else:
            print("Method 3: FAILED - No video URL found")
    except Exception as e:
        print(f"Method 3: FAILED - {e}")

def method4_gallery_dl(url, output_dir="."):
    """Test gallery-dl for a reel"""
    print("Testing Method 4: gallery-dl...")
    try:
        cmd = ["gallery-dl", "-d", output_dir, url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            print("Method 4: SUCCESS")
        else:
            print(f"Method 4: FAILED - {result.stderr}")
    except Exception as e:
        print(f"Method 4: FAILED - {e}")

def extract_shortcode(url):
    """Extract shortcode from Instagram URL"""
    import re
    patterns = [
        r'/reel/([A-Za-z0-9_-]+)',
        r'/p/([A-Za-z0-9_-]+)',
        r'/tv/([A-Za-z0-9_-]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError("Could not extract shortcode from URL")

# =====================
# USAGE:
if __name__ == "__main__":
    # Replace with your Reel URL:
    reel_url = "https://www.instagram.com/reel/DPimQwekRkq/?utm_source=ig_web_copy_link"
    shortcode = extract_shortcode(reel_url)

    # Call each method individually, comment/uncomment to test:
    method1_instaloader(reel_url, shortcode)
    method2_ytdlp(reel_url)
    method3_instagrapi(shortcode)
    method4_gallery_dl(reel_url)
