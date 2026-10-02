#!/usr/bin/env python3
"""
p2v_bot.py
Telegram bot for P2V (Post to Video) pipeline.
Overlays Instagram posts (images or videos) onto a custom 5-second background video
along with a logo video/image at the top.
"""

import os
import sys
import re
import logging
import asyncio
import subprocess
import shutil
import json
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Ensure virtual env binaries are in the path (e.g., yt-dlp, gallery-dl)
venv_bin = Path(sys.executable).parent
os.environ["PATH"] = os.path.pathsep.join([str(venv_bin), os.environ.get("PATH", "")])

# Load environment variables
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("p2v_bot")

# Configuration
P2V_TOKEN = os.getenv("P2V_TELEGRAM_BOT_TOKEN")
WORKSPACE_BASE = Path("workspace/p2v_bot")
SESSIONS_FILE = WORKSPACE_BASE / "sessions.json"

# State machine constants
STATE_WAITING_BG = "WAITING_BG"
STATE_WAITING_LOGO = "WAITING_LOGO"

# Ensure directories exist
WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)

# Helper: Load and save persistent user settings
def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load sessions: {e}")
    return {}

def save_sessions(sessions: dict):
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save sessions: {e}")

# Helper: Resolve ffmpeg path (checks system PATH first, falls back to imageio-ffmpeg)
def get_ffmpeg_command() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(exe):
            return exe
    except ImportError:
        pass
    return "ffmpeg"

# Helper: Get media metadata via OpenCV & Pillow (removes ffprobe dependency)
def get_media_info(path: Path) -> dict:
    suffix = path.suffix.lower()
    is_image = suffix in (".jpg", ".jpeg", ".png", ".webp")
    
    if is_image:
        try:
            from PIL import Image
            with Image.open(path) as img:
                width, height = img.size
            return {
                "width": width,
                "height": height,
                "duration": 0.0,
                "has_audio": False,
                "is_video": False
            }
        except Exception as e:
            logger.error(f"Pillow failed to read image {path}: {e}")
            return {"width": 0, "height": 0, "duration": 0.0, "has_audio": False, "is_video": False}
    else:
        try:
            import cv2
            cap = cv2.VideoCapture(str(path))
            if cap.isOpened():
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                duration = frame_count / fps if fps else 0.0
                cap.release()
                
                # Check for audio: use ffprobe if available, else fallback
                has_audio = False
                if shutil.which("ffprobe"):
                    audio_cmd = [
                        "ffprobe", "-v", "error",
                        "-select_streams", "a:0",
                        "-show_entries", "stream=index",
                        "-of", "json", str(path)
                    ]
                    try:
                        res_a = subprocess.run(audio_cmd, capture_output=True, text=True, check=True)
                        data_a = json.loads(res_a.stdout)
                        if data_a.get("streams"):
                            has_audio = True
                    except Exception:
                        pass
                else:
                    # Fallback heuristic: assume videos have audio, except background videos
                    if "bg_" in path.name or "bg video" in path.name.lower():
                        has_audio = False
                    else:
                        has_audio = True
                        
                return {
                    "width": width,
                    "height": height,
                    "duration": duration,
                    "has_audio": has_audio,
                    "is_video": True
                }
        except Exception as e:
            logger.error(f"OpenCV failed to read video {path}: {e}")
            
    return {"width": 0, "height": 0, "duration": 0.0, "has_audio": False, "is_video": False}

def remove_watermark_from_image(img_path: Path, watermark_templates_dir: Path) -> tuple[bool, list[list[int]]]:
    """
    Attempts to remove watermarks from an image.
    1. Tries Gemini API to detect watermark bounding box.
    2. Falls back to Multi-scale Template Matching if Gemini fails or is not configured.
    Returns: (success_bool, list_of_bbox_coordinates)
    """
    import cv2
    import numpy as np
    import requests
    import base64
    import json
    import re
    
    img = cv2.imread(str(img_path))
    if img is None:
        return False, []
        
    H_px, W_px = img.shape[:2]
    bbox_list = [] # List of [ymin, xmin, ymax, xmax] normalized 0-1000
    
    # 1. Try Gemini API
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key and api_key != "YOUR_GEMINI_API_KEY":
        try:
            # Read image bytes
            with open(img_path, "rb") as f:
                img_bytes = f.read()
                
            prompt = """
            Locate all watermarks, logo overlays, or handle watermarks in this image (e.g. circular profile overlays, username watermarks, etc.).
            Return the bounding box coordinates normalized as [ymin, xmin, ymax, xmax] in the range 0 to 1000 (relative to the image height and width).
            Your response MUST be just a JSON list of lists of 4 integers, for example: [[500, 700, 600, 800], [100, 100, 200, 200]]
            If no watermark is found, return: []
            """
            
            # Request
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
            encoded_image = base64.b64encode(img_bytes).decode("utf-8")
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {
                                "inline_data": {
                                    "mime_type": "image/jpeg",
                                    "data": encoded_image,
                                }
                            },
                        ]
                    }
                ]
            }
            response = requests.post(url, params={"key": api_key}, json=payload, timeout=20)
            if response.status_code == 200:
                data = response.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                # Parse list of lists of 4 integers
                matches = re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]", text)
                if matches:
                    for m in matches:
                        bbox_list.append([int(x) for x in m])
                    logger.info(f"Gemini detected watermarks at normalized coordinates: {bbox_list}")
        except Exception as e:
            logger.warning(f"Gemini watermark detection failed/skipped: {e}")

    # 2. Fall back to local template matching
    if not bbox_list and watermark_templates_dir.exists():
        logger.info("Watermark not found by Gemini or API skipped. Trying local template matching fallback...")
        
        # Downscale working image for matching search to speed up calculation by 100x+
        max_search_dim = 1000
        scale_factor = max_search_dim / float(max(H_px, W_px))
        search_img = cv2.resize(img, (int(W_px * scale_factor), int(H_px * scale_factor)))
        H_search, W_search = search_img.shape[:2]
        
        # We will draw black rectangles on this working search image to isolate candidates
        working_search = search_img.copy()
            
        # Scan templates
        for t_path in watermark_templates_dir.glob("*"):
            if t_path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                continue
            
            # Load template with IMREAD_UNCHANGED to get alpha channel (mask)
            template_raw = cv2.imread(str(t_path), cv2.IMREAD_UNCHANGED)
            if template_raw is None:
                continue
                
            if template_raw.shape[2] == 4:
                template_bgr = template_raw[:, :, :3]
                template_alpha = template_raw[:, :, 3]
                has_mask = True
            else:
                template_bgr = template_raw
                template_alpha = None
                has_mask = False
                
            th, tw = template_bgr.shape[:2]
            
            # Downscale template proportionally by scale_factor
            t_w_search = int(tw * scale_factor)
            t_h_search = int(th * scale_factor)
            
            # Parse threshold from filename (e.g. template_thresh0.60.png)
            threshold = 0.70
            thresh_match = re.search(r"_thresh(\d+\.\d+)", t_path.name)
            if thresh_match:
                threshold = float(thresh_match.group(1))
                logger.info(f"Using template-specific threshold {threshold} for {t_path.name}")
                
            # Keep searching for this template until no more matches are found
            while True:
                best_match = None # (max_val, max_loc, rw, rh)
                
                # 1. Coarse search (0.2 to 6.0 scale with 40 steps)
                best_coarse = None # (max_val, scale, max_loc)
                for scale in np.linspace(0.2, 6.0, 40):
                    rw = int(t_w_search * scale)
                    rh = int(t_h_search * scale)
                    if rw > W_search or rh > H_search or rw < 10 or rh < 10:
                        continue
                        
                    resized_template = cv2.resize(template_bgr, (rw, rh))
                    if has_mask:
                        resized_mask = cv2.resize(template_alpha, (rw, rh))
                        if np.sum(resized_mask) == 0:
                            continue
                    else:
                        resized_mask = None
                    
                    # Channel-by-channel template matching
                    res_b = cv2.matchTemplate(working_search[:, :, 0], resized_template[:, :, 0], cv2.TM_CCORR_NORMED, mask=resized_mask)
                    res_g = cv2.matchTemplate(working_search[:, :, 1], resized_template[:, :, 1], cv2.TM_CCORR_NORMED, mask=resized_mask)
                    res_r = cv2.matchTemplate(working_search[:, :, 2], resized_template[:, :, 2], cv2.TM_CCORR_NORMED, mask=resized_mask)
                    res = (res_b + res_g + res_r) / 3.0
                    res = np.nan_to_num(res, nan=0.0)
                    
                    # Vectorized margin check
                    h_res, w_res = res.shape
                    ys = np.arange(h_res)
                    xs = np.arange(w_res)
                    
                    ymin_norms = ((ys / scale_factor) * 1000 / H_px).astype(int)
                    ymax_norms = (((ys + rh) / scale_factor) * 1000 / H_px).astype(int)
                    xmin_norms = ((xs / scale_factor) * 1000 / W_px).astype(int)
                    xmax_norms = (((xs + rw) / scale_factor) * 1000 / W_px).astype(int)
                    
                    top_left_mask = (ymax_norms[:, np.newaxis] <= 380) & (xmax_norms[np.newaxis, :] <= 380)
                    top_right_mask = (ymax_norms[:, np.newaxis] <= 380) & (xmin_norms[np.newaxis, :] >= 620)
                    bottom_left_mask = (ymin_norms[:, np.newaxis] >= 620) & (xmax_norms[np.newaxis, :] <= 380)
                    right_side_mask = np.broadcast_to(xmin_norms >= 600, (h_res, w_res))
                    
                    valid_map = top_left_mask | top_right_mask | bottom_left_mask | right_side_mask
                    res[~valid_map] = 0.0
                    
                    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
                    if best_coarse is None or max_val > best_coarse[0]:
                        best_coarse = (max_val, scale, max_loc)
                        
                # 2. Fine search around the best coarse scale
                if best_coarse:
                    coarse_val, coarse_scale, coarse_loc = best_coarse
                    min_scale = max(0.2, coarse_scale - 0.15)
                    max_scale = min(6.0, coarse_scale + 0.15)
                    
                    for scale in np.linspace(min_scale, max_scale, 15):
                        rw = int(t_w_search * scale)
                        rh = int(t_h_search * scale)
                        if rw > W_search or rh > H_search or rw < 10 or rh < 10:
                            continue
                            
                        resized_template = cv2.resize(template_bgr, (rw, rh))
                        if has_mask:
                            resized_mask = cv2.resize(template_alpha, (rw, rh))
                            if np.sum(resized_mask) == 0:
                                continue
                        else:
                            resized_mask = None
                        
                        res_b = cv2.matchTemplate(working_search[:, :, 0], resized_template[:, :, 0], cv2.TM_CCORR_NORMED, mask=resized_mask)
                        res_g = cv2.matchTemplate(working_search[:, :, 1], resized_template[:, :, 1], cv2.TM_CCORR_NORMED, mask=resized_mask)
                        res_r = cv2.matchTemplate(working_search[:, :, 2], resized_template[:, :, 2], cv2.TM_CCORR_NORMED, mask=resized_mask)
                        res = (res_b + res_g + res_r) / 3.0
                        res = np.nan_to_num(res, nan=0.0)
                        
                        h_res, w_res = res.shape
                        ys = np.arange(h_res)
                        xs = np.arange(w_res)
                        
                        ymin_norms = ((ys / scale_factor) * 1000 / H_px).astype(int)
                        ymax_norms = (((ys + rh) / scale_factor) * 1000 / H_px).astype(int)
                        xmin_norms = ((xs / scale_factor) * 1000 / W_px).astype(int)
                        xmax_norms = (((xs + rw) / scale_factor) * 1000 / W_px).astype(int)
                        
                        top_left_mask = (ymax_norms[:, np.newaxis] <= 380) & (xmax_norms[np.newaxis, :] <= 380)
                        top_right_mask = (ymax_norms[:, np.newaxis] <= 380) & (xmin_norms[np.newaxis, :] >= 620)
                        bottom_left_mask = (ymin_norms[:, np.newaxis] >= 620) & (xmax_norms[np.newaxis, :] <= 380)
                        right_side_mask = np.broadcast_to(xmin_norms >= 600, (h_res, w_res))
                        
                        valid_map = top_left_mask | top_right_mask | bottom_left_mask | right_side_mask
                        res[~valid_map] = 0.0
                        
                        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
                        if best_match is None or max_val > best_match[0]:
                            best_match = (max_val, max_loc, rw, rh)
                        
                if best_match and best_match[0] >= threshold:
                    max_val, max_loc, rw, rh = best_match
                    xmin_search = max_loc[0]
                    ymin_search = max_loc[1]
                    xmax_search = xmin_search + rw
                    ymax_search = ymin_search + rh
                    
                    # Verify candidate crop color properties
                    crop = working_search[ymin_search:ymax_search, xmin_search:xmax_search]
                    is_valid = True
                    if crop.size > 0:
                        b_mean = float(np.mean(crop[:, :, 0]))
                        g_mean = float(np.mean(crop[:, :, 1]))
                        r_mean = float(np.mean(crop[:, :, 2]))
                        
                        if "maga" in t_path.name.lower():
                            r_b_ratio = r_mean / (b_mean + 1.0)
                            if r_b_ratio < 1.02:
                                logger.info(f"Skipping MAGA match candidate at ({xmin_search},{ymin_search}) due to low R/B ratio ({r_b_ratio:.2f})")
                                is_valid = False
                        elif "vdn" in t_path.name.lower():
                            brightness = (r_mean + g_mean + b_mean) / 3.0
                            if brightness < 120:
                                logger.info(f"Skipping VDN match candidate at ({xmin_search},{ymin_search}) due to low brightness ({brightness:.2f})")
                                is_valid = False
                                
                    # Black-rectangle masking on working search image to prevent matching again
                    cv2.rectangle(working_search, (xmin_search, ymin_search), (xmax_search, ymax_search), (0, 0, 0), -1)
                    
                    if not is_valid:
                        continue
                        
                    # Project back to original coordinates
                    xmin = int(xmin_search / scale_factor)
                    ymin = int(ymin_search / scale_factor)
                    xmax = int(xmax_search / scale_factor)
                    ymax = int(ymax_search / scale_factor)
                    
                    # Convert to normalized coordinates and store
                    bbox_list.append([
                        int(ymin * 1000 / H_px),
                        int(xmin * 1000 / W_px),
                        int(ymax * 1000 / H_px),
                        int(xmax * 1000 / W_px)
                    ])
                    
                    logger.info(f"Local template matched watermark {t_path.name} (corr={max_val:.4f}, threshold={threshold}) at pixel: xmin={xmin}, ymin={ymin}, xmax={xmax}, ymax={ymax}")
                else:
                    break
                    
    if not bbox_list:
        logger.info("No watermarks detected in the image.")
        return False, []
        
    # Re-read original image to do a single final high-quality inpainting of all bounding boxes combined
    original_img = cv2.imread(str(img_path))
    if original_img is None:
        return False, []
        
    mask = np.zeros((H_px, W_px), dtype=np.uint8)
    for bbox in bbox_list:
        ymin_px = int(bbox[0] * H_px / 1000)
        xmin_px = int(bbox[1] * W_px / 1000)
        ymax_px = int(bbox[2] * H_px / 1000)
        xmax_px = int(bbox[3] * W_px / 1000)
        
        padding = 15
        ymin_px = max(0, ymin_px - padding)
        xmin_px = max(0, xmin_px - padding)
        ymax_px = min(H_px, ymax_px + padding)
        xmax_px = min(W_px, xmax_px + padding)
        
        cv2.rectangle(mask, (xmin_px, ymin_px), (xmax_px, ymax_px), 255, -1)
        
    inpainted = cv2.inpaint(original_img, mask, inpaintRadius=15, flags=cv2.INPAINT_TELEA)
    cv2.imwrite(str(img_path), inpainted)
    logger.info(f"Successfully inpainted and removed {len(bbox_list)} watermarks from image {img_path}")
    return True, bbox_list

def remove_watermark_from_video(video_path: Path, watermark_templates_dir: Path) -> bool:
    """
    Detects watermark coordinates from the middle frame of the video,
    and then in-paints all frames of the video.
    """
    import cv2
    import numpy as np
    import shutil
    import subprocess
    
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return False
        
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    
    # Extract middle frame to detect watermark
    middle_frame_idx = frame_count // 2
    cap.set(cv2.CAP_PROP_POS_FRAMES, middle_frame_idx)
    ret, middle_frame = cap.read()
    cap.release()
    
    if not ret or middle_frame is None:
        return False
        
    # Save middle frame to a temp file to run detection
    temp_frame_path = video_path.parent / "temp_frame.jpg"
    cv2.imwrite(str(temp_frame_path), middle_frame)
    
    # Detect watermarks on the temp frame
    success, bbox_list = remove_watermark_from_image(temp_frame_path, watermark_templates_dir)
    
    if temp_frame_path.exists():
        temp_frame_path.unlink()
        
    if not success or not bbox_list:
        logger.info("No watermark detected in video stills.")
        return False
        
    # Re-open video for reading
    cap = cv2.VideoCapture(str(video_path))
    
    # Open a temporary output video
    temp_out_path = video_path.parent / "temp_clean_vid.mp4"
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(str(temp_out_path), fourcc, fps, (width, height))
    
    # Create mask combining all detected watermarks
    mask = np.zeros((height, width), dtype=np.uint8)
    for bbox in bbox_list:
        ymin_px = int(bbox[0] * height / 1000)
        xmin_px = int(bbox[1] * width / 1000)
        ymax_px = int(bbox[2] * height / 1000)
        xmax_px = int(bbox[3] * width / 1000)
        
        padding = 15
        ymin_px = max(0, ymin_px - padding)
        xmin_px = max(0, xmin_px - padding)
        ymax_px = min(height, ymax_px + padding)
        xmax_px = min(width, xmax_px + padding)
        
        cv2.rectangle(mask, (xmin_px, ymin_px), (xmax_px, ymax_px), 255, -1)
    
    logger.info(f"Applying {len(bbox_list)} watermark removal masks to all video frames...")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        # Inpaint frame
        clean_frame = cv2.inpaint(frame, mask, inpaintRadius=15, flags=cv2.INPAINT_TELEA)
        out.write(clean_frame)
        
    cap.release()
    out.release()
    
    # Merge video and copy original audio if any using FFmpeg
    ffmpeg_cmd = get_ffmpeg_command()
    final_out_path = video_path.parent / f"clean_{video_path.name}"
    cmd = [
        ffmpeg_cmd, "-y",
        "-i", str(temp_out_path),
        "-i", str(video_path),
        "-map", "0:v",
        "-map", "1:a?",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(final_out_path)
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        # Overwrite the original video file
        if final_out_path.exists():
            shutil.move(str(final_out_path), str(video_path))
        if temp_out_path.exists():
            temp_out_path.unlink()
        logger.info(f"Successfully removed watermarks from video {video_path}")
        return True
    except Exception as e:
        logger.error(f"FFmpeg audio copy for cleaned video failed: {e}")
        if temp_out_path.exists():
            shutil.move(str(temp_out_path), str(video_path))
        return False

def calculate_composition_coords(bg_info: dict, post_info: dict, logo_info: dict) -> dict:
    """
    Calculates scaled dimensions and overlay coordinates for the Instagram post
    and logo to match the background video dimensions beautifully.
    """
    W, H = bg_info["width"], bg_info["height"]
    
    # 1. Instagram Post Scaling: Aspect-ratio aware scaling to avoid "zoomed in" vertical posts.
    post_ratio = post_info["height"] / post_info["width"]
    
    # Configurable padding parameters to adjust gap from canvas border
    side_gap = 35            # Increase gap on the sides (left/right) from 20px to 35px
    max_height_pct = 0.55    # Decrease max height limit from 65% to 55% of background height
    border_padding = 4       # 2px white border on all sides -> total 4px width/height padding
    
    if post_ratio > 1.2:
        # Tall vertical post (e.g. 4:5 or 9:16)
        # Limit padded height to max_height_pct of background height
        h_post_target = H * max_height_pct - border_padding
        w_post_target = h_post_target / post_ratio
    else:
        # Square or landscape post
        # Span width to fill background width minus side_gap on both sides
        w_post_target = W - (2 * side_gap + border_padding)
        h_post_target = w_post_target * post_ratio
        
        # Cap height just in case it exceeds max_height_pct of H
        if h_post_target + border_padding > H * max_height_pct:
            h_post_target = H * max_height_pct - border_padding
            w_post_target = h_post_target / post_ratio
            
    w_post = int(w_post_target) // 2 * 2
    h_post = int(h_post_target) // 2 * 2
    
    # Padded dimensions (post content + 2px white border on all sides)
    w_padded = w_post + border_padding
    h_padded = h_post + border_padding
    
    x_post = int((W - w_padded) / 2)
    y_post = int((H - h_padded) / 2)
    
    # 2. Logo Scaling: Forced to square of 16% of background width (reduced size for elegance)
    w_logo = int(W * 0.16) // 2 * 2
    h_logo = w_logo
    
    # Position logo at the top right corner of the Instagram post with 20px padding inside
    x_logo = x_post + w_padded - w_logo - 20
    y_logo = y_post + 20
        
    return {
        "w_post": w_post,
        "h_post": h_post,
        "x_post": x_post,
        "y_post": y_post,
        "w_logo": w_logo,
        "h_logo": h_logo,
        "x_logo": x_logo,
        "y_logo": y_logo
    }

def create_composition_assets(size: int, solid_path: Path, mask_path: Path):
    """
    Generates a solid #f6f6f6 square and a high-quality circular mask using Pillow.
    """
    from PIL import Image, ImageDraw
    # 1. Solid color square background
    solid = Image.new("RGBA", (size, size), "#f6f6f6")
    solid.save(solid_path)
    
    # 2. Circular mask with antialiasing
    scale = 4
    large_size = size * scale
    mask = Image.new("L", (large_size, large_size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, large_size, large_size), fill=255)
    
    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:
        resample = Image.LANCZOS
        
    mask_resized = mask.resize((size, size), resample=resample)
    mask_resized.save(mask_path)

# Core: Video composition with FFmpeg
def render_composition(bg_path: Path, post_path: Path, logo_path: Path, output_path: Path) -> bool:
    # Check and remove watermark from the post media before probing info and composition
    try:
        watermarks_dir = WORKSPACE_BASE / "watermarks"
        if post_path.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv"):
            remove_watermark_from_video(post_path, watermarks_dir)
        else:
            remove_watermark_from_image(post_path, watermarks_dir)
    except Exception as e:
        logger.error(f"Watermark removal processing failed: {e}")

    bg_info = get_media_info(bg_path)
    post_info = get_media_info(post_path)
    logo_info = get_media_info(logo_path)
    
    if bg_info["width"] == 0 or post_info["width"] == 0 or logo_info["width"] == 0:
        logger.error(f"Failed to probe dimensions for files: bg={bg_path}, post={post_path}, logo={logo_path}")
        return False
        
    coords = calculate_composition_coords(bg_info, post_info, logo_info)
    w_post, h_post = coords["w_post"], coords["h_post"]
    x_post, y_post = coords["x_post"], coords["y_post"]
    w_logo, h_logo = coords["w_logo"], coords["h_logo"]
    x_logo, y_logo = coords["x_logo"], coords["y_logo"]
    
    # Generate circular mask and solid background assets
    timestamp = int(datetime.now().timestamp())
    solid_path = WORKSPACE_BASE / f"temp_solid_{timestamp}.png"
    mask_path = WORKSPACE_BASE / f"temp_mask_{timestamp}.png"
    try:
        create_composition_assets(w_logo, solid_path, mask_path)
    except Exception as e:
        logger.error(f"Failed to create composition assets: {e}")
        return False
        
    # Build filter complex
    filter_complex = (
        f"[0:v]trim=end=5,setpts=PTS-STARTPTS[bg_v]; "
    )
    # Apply a 2-pixel wide white border around the post content
    if post_info["is_video"]:
        filter_complex += f"[1:v]scale={w_post}:{h_post},pad=iw+4:ih+4:2:2:white,trim=end=5,setpts=PTS-STARTPTS[post_padded]; "
    else:
        filter_complex += f"[1:v]scale={w_post}:{h_post},pad=iw+4:ih+4:2:2:white[post_padded]; "
        
    # Crop logo to square and scale
    if logo_info["is_video"]:
        filter_complex += f"[2:v]crop=min(iw\\,ih):min(iw\\,ih),scale={w_logo}:{h_logo},trim=end=5,setpts=PTS-STARTPTS[logo_sq]; "
    else:
        filter_complex += f"[2:v]crop=min(iw\\,ih):min(iw\\,ih),scale={w_logo}:{h_logo}[logo_sq]; "
        
    # Overlay logo on top of solid #f6f6f6 background, then apply circular mask
    filter_complex += (
        f"[3:v][logo_sq]overlay[logo_with_bg]; "
        f"[logo_with_bg][4:v]alphamerge[logo_circular]; "
        f"[bg_v][post_padded]overlay={x_post}:{y_post}:shortest=1[tmp]; "
        f"[tmp][logo_circular]overlay={x_logo}:{y_logo}:shortest=1[outv]"
    )
    
    # Assemble FFmpeg Command
    cmd = [get_ffmpeg_command(), "-y"]
    
    # Inputs: loop background, post, and logo so they are guaranteed to fill the 5s timeline
    cmd += ["-stream_loop", "-1", "-i", str(bg_path)]
    
    if post_info["is_video"]:
        cmd += ["-stream_loop", "-1", "-i", str(post_path)]
    else:
        cmd += ["-loop", "1", "-i", str(post_path)]
        
    if logo_info["is_video"]:
        cmd += ["-stream_loop", "-1", "-i", str(logo_path)]
    else:
        cmd += ["-loop", "1", "-i", str(logo_path)]
        
    # Input 3: Solid background square
    cmd += ["-i", str(solid_path)]
    
    # Input 4: Circle mask image
    cmd += ["-i", str(mask_path)]
    
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]"
    ]
    
    # Audio routing: Prefer IG video audio, fallback to BG video audio, else silent
    if post_info["is_video"] and post_info["has_audio"]:
        cmd += ["-map", "1:a"]
    elif bg_info["has_audio"]:
        cmd += ["-map", "0:a"]
        
    cmd += [
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-t", "5"
    ]
    
    if (post_info["is_video"] and post_info["has_audio"]) or bg_info["has_audio"]:
        cmd += ["-c:a", "aac"]
        
    cmd.append(str(output_path))
    
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("FFmpeg composition succeeded!")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg failed with exit code {e.returncode}")
        logger.error(f"FFmpeg Stderr:\n{e.stderr}")
        return False
    except Exception as ex:
        logger.error(f"Failed to spawn FFmpeg process: {ex}")
        return False
    finally:
        # Clean up temporary background assets
        for path in (solid_path, mask_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

# Import local Instagram downloader
try:
    from instagram_downloader import download_instagram_reel_efficient
except ImportError:
    logger.error("Failed to import download_instagram_reel_efficient from instagram_downloader.py. Please verify path.")
    download_instagram_reel_efficient = None

# Telegram Bot handlers
async def command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    await update.message.reply_text(
        f"🎬 **Welcome to P2V (Post to Video) Bot, {user_name}!**\n\n"
        "I will take Instagram posts (images or reels) and overlay them in the center of your background video "
        "along with your custom logo at the top.\n\n"
        "🔧 **Setup Checklist:**\n"
        "1️⃣ Send your **Background Video** (5s long) directly or click `/setbg`.\n"
        "2️⃣ Send your **Logo** (video or image) directly or click `/setlogo`.\n\n"
        "💡 *Tip: You can drag and drop videos or photos directly into this chat! I'll ask you what you want to set them as.*\n\n"
        "Use `/status` to check your configuration at any time."
    )

async def command_setbg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    sessions = load_sessions()
    if user_id not in sessions:
        sessions[user_id] = {}
    sessions[user_id]["state"] = STATE_WAITING_BG
    save_sessions(sessions)
    await update.message.reply_text("📹 Please send or upload the **Background Video** (5 seconds duration recommended).")

async def command_setlogo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    sessions = load_sessions()
    if user_id not in sessions:
        sessions[user_id] = {}
    sessions[user_id]["state"] = STATE_WAITING_LOGO
    save_sessions(sessions)
    await update.message.reply_text("🎨 Please send or upload the **Logo** (can be a video or a static image).")

async def command_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    sessions = load_sessions()
    user_conf = sessions.get(user_id, {})
    
    bg = user_conf.get("bg_video") or os.getenv("P2V_DEFAULT_BG_VIDEO")
    logo = user_conf.get("logo_media") or os.getenv("P2V_DEFAULT_LOGO_MEDIA")
    
    bg_status = f"✅ Configured (`{Path(bg).name}`)" if bg and os.path.exists(bg) else "❌ Not set"
    logo_status = f"✅ Configured (`{Path(logo).name}`)" if logo and os.path.exists(logo) else "❌ Not set"
    
    status_text = (
        "📝 **Your P2V Configuration Status:**\n\n"
        f"• **Background Video**: {bg_status}\n"
        f"• **Logo Media**: {logo_status}\n\n"
    )
    if bg and logo:
        status_text += "🚀 **Ready to go!** Just send me any Instagram post link (image post or reel) to render it."
    else:
        status_text += "⚠️ Please configure both background video and logo to start generating videos."
        
    await update.message.reply_text(status_text)

async def command_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    sessions = load_sessions()
    
    # Delete folder and contents
    user_dir = WORKSPACE_BASE / f"user_{user_id}"
    if user_dir.exists():
        try:
            shutil.rmtree(user_dir)
        except Exception as e:
            logger.error(f"Failed to delete user directory: {e}")
            
    if user_id in sessions:
        del sessions[user_id]
        save_sessions(sessions)
        
    await update.message.reply_text("🧹 Configuration and stored assets have been successfully reset.")

# Extraction and downloader task wrapper
async def process_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, link_index: int = None, total_links: int = None):
    user_id = str(update.effective_user.id)
    sessions = load_sessions()
    user_conf = sessions.get(user_id, {})
    
    bg_video = user_conf.get("bg_video") or os.getenv("P2V_DEFAULT_BG_VIDEO")
    logo_media = user_conf.get("logo_media") or os.getenv("P2V_DEFAULT_LOGO_MEDIA")
    
    prefix = f"[{link_index}/{total_links}] " if link_index is not None and total_links is not None else ""
    
    if not bg_video or not logo_media:
        await update.message.reply_text(
            f"⚠️ **Configuration Missing**\n\n"
            f"Please upload your Background Video and Logo first.\n"
            f"Use `/status` to check your setup."
        )
        return
        
    if not os.path.exists(bg_video):
        await update.message.reply_text("⚠️ Background video not found on disk. Please set it again using `/setbg`.")
        return
    if not os.path.exists(logo_media):
        await update.message.reply_text("⚠️ Logo media not found on disk. Please set it again using `/setlogo`.")
        return
        
    status_msg = await update.message.reply_text(f"📥 {prefix}**Downloading Instagram post (full quality)...**")
    
    async def safe_edit_status(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception as se:
            logger.warning(f"Failed to update status message: {se}")

    async def safe_delete_status():
        try:
            await status_msg.delete()
        except Exception as se:
            logger.warning(f"Failed to delete status message: {se}")
            
    try:
        if not download_instagram_reel_efficient:
            await safe_edit_status(f"❌ {prefix}Downloader pipeline import error. Check server logs.")
            return
            
        # Download IG reel/post in an executor to keep the bot responsive
        loop = asyncio.get_running_loop()
        ws_path = await loop.run_in_executor(
            None,
            download_instagram_reel_efficient,
            url,
            "workspace/p2v_downloads"
        )
        
        if not ws_path:
            await safe_edit_status(f"❌ {prefix}**Download Failed**\n\nCould not fetch Instagram post. Make sure it is public and try again.")
            return
            
        raw_dir = Path(ws_path) / "00_raw"
        video_candidates = list(raw_dir.glob("raw_source.*"))
        image_candidates = list(raw_dir.glob("raw_thumb.*"))
        
        post_path = None
        if video_candidates:
            post_path = video_candidates[0]
        elif image_candidates:
            post_path = image_candidates[0]
            
        if not post_path:
            await safe_edit_status(f"❌ {prefix}**Media Error**\n\nNo media file could be located in the downloaded content.")
            return
            
        await safe_edit_status(f"🎬 {prefix}**Checking watermarks and overlaying media...**")
        
        # Prepare output path
        output_dir = WORKSPACE_BASE / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"output_{user_id}_{timestamp}.mp4"
        
        # Render composition in thread pool executor
        success = await loop.run_in_executor(
            None,
            render_composition,
            Path(bg_video),
            post_path,
            Path(logo_media),
            output_path
        )
        
        if not success:
            await safe_edit_status(f"❌ {prefix}**Render Failed**\n\nFFmpeg processing failed. Check bot console logs.")
            return
            
        await safe_edit_status(f"📤 {prefix}**Uploading result...**")
        
        # Upload video back as document (highest quality file)
        with open(output_path, "rb") as vf:
            await update.message.reply_document(
                document=vf,
                filename=output_path.name,
                caption=f"✅ {prefix}**Composition Complete! (Highest Quality)**\n\n*Source*: {url}",
                write_timeout=120,
                read_timeout=120
            )
            
        await safe_delete_status()
        
        # Clean up output and downloads
        try:
            output_path.unlink()
        except Exception:
            pass
            
        try:
            shutil.rmtree(ws_path)
        except Exception:
            pass
            
    except Exception as e:
        logger.exception("Exception occurred during link processing:")
        try:
            await status_msg.edit_text(f"❌ {prefix}**Unexpected Error**: {str(e)}")
        except Exception:
            try:
                await update.message.reply_text(f"❌ {prefix}**Unexpected Error**: {str(e)}")
            except Exception:
                pass

# Helper: Save attachments to disk
async def save_user_file(bot, file_id: str, original_filename: str, user_id: str, file_role: str) -> str:
    user_dir = WORKSPACE_BASE / f"user_{user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    
    # Remove existing files of the same role to save space
    prefix = f"{file_role}_"
    for old_file in user_dir.glob(f"{prefix}*"):
        try:
            old_file.unlink()
        except Exception:
            pass
            
    safe_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', original_filename)
    dest_path = user_dir / f"{prefix}{safe_name}"
    
    # Download file via telegram API
    file = await bot.get_file(file_id)
    await file.download_to_drive(dest_path)
    
    return str(dest_path)

# Handle text messages
async def handle_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    
    # Scan for Instagram URLs (split lines or extract via regex)
    urls = re.findall(r'(https?://[^\s]+)', text)
    ig_urls = [u for u in urls if "instagram.com" in u]
    
    if ig_urls:
        total_links = len(ig_urls)
        if total_links > 1:
            await update.message.reply_text(f"📚 **Found {total_links} Instagram links!** Processing in bulk...")
            
        for idx, url in enumerate(ig_urls, start=1):
            try:
                await process_instagram_link(update, context, url, link_index=idx, total_links=total_links)
            except Exception as e:
                logger.error(f"Failed to process bulk link {idx} ({url}): {e}")
    else:
        # Fallback response for unhandled text
        await update.message.reply_text(
            "❓ Send me a public Instagram post / reel URL, or upload your background/logo assets."
        )

# Handle media messages
def extract_attachment_info(message) -> tuple:
    if message.video:
        return message.video.file_id, message.video.file_name or "video.mp4"
    elif message.photo:
        return message.photo[-1].file_id, "photo.jpg"
    elif message.document:
        mime = message.document.mime_type or ""
        fn = message.document.file_name or "file.bin"
        ext = Path(fn).suffix.lower()
        if mime.startswith("video/") or ext in (".mp4", ".mov", ".avi", ".mkv"):
            return message.document.file_id, fn
        elif mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp"):
            return message.document.file_id, fn
    return None, None

async def handle_incoming_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    sessions = load_sessions()
    user_conf = sessions.get(user_id, {})
    state = user_conf.get("state")
    
    file_id, file_name = extract_attachment_info(update.message)
    
    if not file_id:
        await update.message.reply_text("❌ Unsupported file type. Please send a video or photo.")
        return
        
    # State-based immediate configuration
    if state == STATE_WAITING_BG:
        # Background must be a video
        if file_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            await update.message.reply_text("❌ The background must be a video asset. Please upload a video.")
            return
            
        status_msg = await update.message.reply_text("📥 Saving background video...")
        saved_path = await save_user_file(context.bot, file_id, file_name, user_id, "bg")
        
        user_conf["bg_video"] = saved_path
        user_conf["state"] = None
        sessions[user_id] = user_conf
        save_sessions(sessions)
        
        await status_msg.edit_text("✅ **Background video configured successfully!**")
        return
        
    elif state == STATE_WAITING_LOGO:
        status_msg = await update.message.reply_text("📥 Saving logo asset...")
        saved_path = await save_user_file(context.bot, file_id, file_name, user_id, "logo")
        
        user_conf["logo_media"] = saved_path
        user_conf["state"] = None
        sessions[user_id] = user_conf
        save_sessions(sessions)
        
        await status_msg.edit_text("✅ **Logo configured successfully!**")
        return
        
    # Drag and Drop workflow (no active state)
    # Store file details in temp context state
    context.user_data["temp_file_id"] = file_id
    context.user_data["temp_file_name"] = file_name
    
    # Determine options based on attachment type
    keyboard = []
    is_img = file_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    
    if not is_img:
        keyboard.append([InlineKeyboardButton("Set as Background Video 📹", callback_data="btn_setbg")])
    keyboard.append([InlineKeyboardButton("Set as Logo Media 🎨", callback_data="btn_setlogo")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📦 I received your file! What would you like to configure this asset as?",
        reply_markup=reply_markup
    )

# Inline button click callback handler
async def handle_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    choice = query.data
    
    file_id = context.user_data.get("temp_file_id")
    file_name = context.user_data.get("temp_file_name")
    
    if not file_id or not file_name:
        await query.edit_message_text("❌ Session expired or file reference not found. Please upload it again.")
        return
        
    sessions = load_sessions()
    user_conf = sessions.get(user_id, {})
    
    if choice == "btn_setbg":
        await query.edit_message_text("📥 Downloading background video...")
        saved_path = await save_user_file(context.bot, file_id, file_name, user_id, "bg")
        user_conf["bg_video"] = saved_path
        await query.edit_message_text("✅ **Background video set successfully!**")
        
    elif choice == "btn_setlogo":
        await query.edit_message_text("📥 Downloading logo asset...")
        saved_path = await save_user_file(context.bot, file_id, file_name, user_id, "logo")
        user_conf["logo_media"] = saved_path
        await query.edit_message_text("✅ **Logo set successfully!**")
        
    # Clear temp state
    context.user_data.pop("temp_file_id", None)
    context.user_data.pop("temp_file_name", None)
    
    user_conf["state"] = None
    sessions[user_id] = user_conf
    save_sessions(sessions)

# Startup verification
def verify_system_dependencies():
    logger.info("Verifying system dependencies...")
    ffmpeg_found = shutil.which("ffmpeg") is not None
    ffprobe_found = shutil.which("ffprobe") is not None
    
    if not ffmpeg_found:
        logger.warning("⚠️ FFMPEG not found on system PATH. Please ensure FFmpeg is installed.")
    else:
        logger.info("✓ FFmpeg found.")
        
    if not ffprobe_found:
        logger.warning("⚠️ FFPROBE not found on system PATH. Dimensions query will fail.")
    else:
        logger.info("✓ FFprobe found.")

def main():
    verify_system_dependencies()
    
    if not P2V_TOKEN or P2V_TOKEN == "YOUR_P2V_TELEGRAM_BOT_TOKEN":
        logger.error("❌ P2V_TELEGRAM_BOT_TOKEN environment variable is not configured in .env!")
        sys.exit(1)
        
    # Build bot application with resilient HTTP request timeouts
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    application = Application.builder().token(P2V_TOKEN).request(request).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", command_start))
    application.add_handler(CommandHandler("help", command_start))
    application.add_handler(CommandHandler("setbg", command_setbg))
    application.add_handler(CommandHandler("setlogo", command_setlogo))
    application.add_handler(CommandHandler("status", command_status))
    application.add_handler(CommandHandler("reset", command_reset))
    
    # Callback query (Inline keyboard buttons)
    application.add_handler(CallbackQueryHandler(handle_button_callback))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_text))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.PHOTO, handle_incoming_media))
    
    logger.info("Starting P2V Telegram Bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
