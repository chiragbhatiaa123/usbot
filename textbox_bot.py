#!/usr/bin/env python3
"""
textbox_bot.py
Telegram bot for generating premium text-box images.
Features:
- Select style font: "faith" or "maga_charlie"
- Enter text: renders text centered on a dark gradient background inside a semi-transparent glassmorphic card.
- Dynamic line fitting: scales each line to span the box width (touching the borders).
- All text is auto-capitalized.
- Conditional line coloring:
  - Default (faith): last 2/3 lines colored.
  - Maga/Charlie: middle lines colored (4 lines → middle 2, 5 lines → middle 3, etc.).
- Persistent session: user stays on the same page after generating, can send more text or change color.
"""

import os
import sys
import logging
import urllib.request
import argparse
from io import BytesIO
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageEnhance
import cv2
import numpy as np
from rembg import remove
import requests
import instaloader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes
# Load environment variables
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import json

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger("textbox_bot")

def save_user_session(user_id, chosen_font, chosen_color=None, doge_highlight_lines=None):
    try:
        session_file = os.path.join(BASE_DIR, "scratch", f"session_{user_id}.json")
        os.makedirs(os.path.dirname(session_file), exist_ok=True)
        data = {
            "chosen_font": chosen_font,
            "chosen_color": chosen_color,
            "doge_highlight_lines": doge_highlight_lines
        }
        with open(session_file, "w") as f:
            json.dump(data, f)
        logger.info(f"Saved session for user {user_id}: {data}")
    except Exception as e:
        logger.error(f"Failed to save user session: {e}")

def load_user_session(user_id):
    try:
        session_file = os.path.join(BASE_DIR, "scratch", f"session_{user_id}.json")
        if os.path.exists(session_file):
            with open(session_file, "r") as f:
                data = json.load(f)
                logger.info(f"Loaded session for user {user_id}: {data}")
                return data
    except Exception as e:
        logger.error(f"Failed to load user session: {e}")
    return {}

def restore_session_if_needed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    if not context.user_data.get("chosen_font"):
        session = load_user_session(user_id)
        if session:
            context.user_data["chosen_font"] = session.get("chosen_font")
            context.user_data["chosen_color"] = session.get("chosen_color")
            context.user_data["doge_highlight_lines"] = session.get("doge_highlight_lines")

def fetch_instagram_post(url: str, user_id: int):
    """
    Fetches the caption and downloads the primary image/video of an Instagram post.
    Returns: (caption, media_path, is_video)
    """
    parts = url.strip("/").split("/")
    shortcode = None
    for i, p in enumerate(parts):
        if p in ("p", "reel", "tv") and i + 1 < len(parts):
            shortcode = parts[i + 1].split("?")[0]
            break
            
    if not shortcode:
        raise ValueError("Could not extract Instagram shortcode from URL.")
        
    L = instaloader.Instaloader()
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    caption = post.caption or ""
    
    media_path = None
    is_video = post.is_video
    
    if is_video and post.video_url:
        response = requests.get(post.video_url, stream=True)
        if response.status_code == 200:
            media_path = os.path.join(BASE_DIR, f"user_bg_vid_{user_id}.mp4")
            with open(media_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk)
    elif post.url:
        response = requests.get(post.url)
        if response.status_code == 200:
            media_path = os.path.join(BASE_DIR, f"user_bg_img_{user_id}.jpg")
            with open(media_path, "wb") as f:
                f.write(response.content)
                
    return caption, media_path, is_video

# Font Download Links (official Google Fonts)
FONT_LINKS = {
    "faith": [
        "https://github.com/google/fonts/raw/10a708073179c32928eb894e53465fca8106772f/ofl/playfairdisplay/static/PlayfairDisplay-Italic.ttf",
        "https://github.com/google/fonts/raw/10a708073179c32928eb894e53465fca8106772f/ofl/playfairdisplay/PlayfairDisplay-Italic.ttf"
    ],
    "maga_charlie": []  # Maga/Charlie uses system Impact font, no download needed
}

def download_fonts():
    """Ensure fonts are downloaded and available."""
    fonts_dir = os.path.join(BASE_DIR, "fonts")
    os.makedirs(fonts_dir, exist_ok=True)
    
    for font_name, urls in FONT_LINKS.items():
        font_path = os.path.join(fonts_dir, f"{font_name}.ttf")
        if os.path.exists(font_path) and os.path.getsize(font_path) > 1024:
            logger.info(f"Font '{font_name}' already exists.")
            continue
            
        success = False
        for url in urls:
            try:
                logger.info(f"Downloading {font_name} from {url}...")
                urllib.request.urlretrieve(url, font_path)
                if os.path.exists(font_path) and os.path.getsize(font_path) > 1024:
                    logger.info(f"Successfully downloaded {font_name}.")
                    success = True
                    break
            except Exception as e:
                logger.warning(f"Failed to download {font_name} from {url}: {e}")
                if os.path.exists(font_path):
                    os.remove(font_path)
                    
        if not success:
            logger.error(f"Could not download font '{font_name}'. Fallbacks will be used.")

def get_line_height(font: ImageFont.FreeTypeFont) -> int:
    """Helper to calculate vertical height of font line."""
    try:
        ascent, descent = font.getmetrics()
        line_height = ascent + descent
        if line_height > 0:
            return line_height
    except Exception:
        pass
    try:
        bbox = font.getbbox("Ag")
        if bbox:
            return max(1, bbox[3] - bbox[1])
    except Exception:
        pass
    return max(1, getattr(font, "size", 12))

def draw_gradient_background(width, height, start_color, end_color):
    """Draw a smooth vertical color gradient on an RGB image."""
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / height
        r = int(start_color[0] + (end_color[0] - start_color[0]) * ratio)
        g = int(start_color[1] + (end_color[1] - start_color[1]) * ratio)
        b = int(start_color[2] + (end_color[2] - start_color[2]) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    return img

def get_highlight_color(color_choice: str) -> tuple:
    """Helper to resolve highlight color choices to RGB tuples."""
    color_choice = (color_choice or "").lower().strip()
    if color_choice == "red":
        return (255, 49, 49)   # #ff3131
    elif color_choice == "orange":
        return (255, 145, 77)  # #ff914d
    elif color_choice == "magenta":
        return (203, 108, 230) # #cb6ce6
    elif color_choice == "green":
        return (0, 230, 118)   # #00e676
    elif color_choice == "blue":
        return (56, 182, 255)  # #38b6ff
    elif color_choice == "purple":
        return (140, 82, 255)  # #8c52ff
    else:
        return (255, 222, 89)  # #ffde59

def proofread_text_with_ai(text: str) -> str:
    """Proofreads text for spelling/grammar using Groq (llama-3.3-70b-versatile) or Gemini."""
    # Try Groq first since we have a valid GROQ_API_KEY in .env
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {groq_key}",
            "Content-Type": "application/json"
        }
        prompt = (
            "You are an expert copyeditor. Fix any spelling, punctuation, or grammatical errors in the text below. "
            "Do NOT change the meaning or style. Keep the original text structure and line breaks where appropriate. "
            "Do NOT add any introductory or concluding comments. Return ONLY the final corrected text.\n\n"
            f"Text to correct:\n{text}"
        )
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=8)
            if response.status_code == 200:
                res_data = response.json()
                content = res_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if content:
                    return content
        except Exception as e:
            logger.error(f"Error during Groq proofreading: {e}")

    # Fallback to Gemini if Groq fails or is not configured
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        model = "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        prompt = (
            "You are an expert copyeditor. Fix any spelling, punctuation, or grammatical errors in the text below. "
            "Do NOT change the meaning or style. Keep the original text structure and line breaks where appropriate. "
            "Do NOT add any introductory or concluding comments. Return ONLY the final corrected text.\n\n"
            f"Text to correct:\n{text}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ]
        }
        headers = {"Content-Type": "application/json"}
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=8)
            if response.status_code == 200:
                res_data = response.json()
                parts = res_data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    corrected = parts[0]["text"].strip()
                    if corrected.startswith("```"):
                        lines = corrected.split("\n")
                        if lines[0].startswith("```"):
                            lines = lines[1:]
                        if lines and lines[-1].startswith("```"):
                            lines = lines[:-1]
                        corrected = "\n".join(lines).strip()
                    return corrected
        except Exception as e:
            logger.error(f"Error during Gemini proofreading: {e}")

    return text

def call_ocr_space(image_bytes: bytes) -> str:
    """Extract text from image bytes using OCR.space API."""
    url = "https://api.ocr.space/parse/image"
    payload = {
        "apikey": "helloworld",
        "language": "eng",
        "isOverlayRequired": False
    }
    files = {"file": ("image.jpg", image_bytes, "image/jpeg")}
    try:
        response = requests.post(url, files=files, data=payload, timeout=25)
        if response.status_code == 200:
            res_json = response.json()
            results = res_json.get("ParsedResults")
            if results:
                return results[0].get("ParsedText", "").strip()
    except Exception as e:
        logger.error(f"OCR.space request failed: {e}")
    return ""

def generate_textbox_image(text: str, font_type: str, highlight_choice: str = "yellow", user_support_image_path: str = None, doge_highlight_lines: int = None) -> BytesIO:
    """Generates the premium text box image as a PNG bytes buffer."""
    # Image Canvas Dimensions
    canvas_w = 1080
    
    # Aesthetic styling colors
    bg_start = (20, 24, 33)      # Charcoal blue gradient top
    bg_end = (10, 12, 16)        # Dark midnight background bottom
    box_bg = (0, 0, 0, 175)      # Semi-transparent sleek dark box
    box_outline = (255, 255, 255, 45) # Thin glass outline
    
    highlight_color = get_highlight_color(highlight_choice)
        
    default_text_color = (255, 255, 255) # Premium white
    
    # Parse lines and capitalize all text
    lines = [line.strip().upper() for line in text.split('\n') if line.strip()]
    if not lines:
        lines = ["TEXT BOX"]
        
    num_lines = len(lines)
    is_maga = font_type.lower() in ("maga", "charlie", "maga_charlie", "doge")
    
    # Assign colors:
    # For doge: color the last k lines based on user choice
    # For maga/charlie: middle lines are colored (5 lines → middle 3, 4 lines → middle 2)
    # For faith: last lines are colored (<=4 → last 2, >=5 → last 3)
    line_colors = []
    if font_type.lower() == "doge":
        k = doge_highlight_lines if doge_highlight_lines is not None else 2
        k = min(num_lines, max(0, k))
        for idx in range(num_lines):
            if idx >= num_lines - k:
                line_colors.append(highlight_color)
            else:
                line_colors.append(default_text_color)
    elif font_type.lower() in ("maga", "charlie", "maga_charlie"):
        # Maga: color the middle lines
        if num_lines <= 4:
            num_colored = 2
        else:
            num_colored = 3
        # Center the colored block
        start_colored = (num_lines - num_colored) // 2
        end_colored = start_colored + num_colored
        for idx in range(num_lines):
            if start_colored <= idx < end_colored:
                line_colors.append(highlight_color)
            else:
                line_colors.append(default_text_color)
    else:
        # Faith: color the last lines
        if num_lines <= 4:
            for idx in range(num_lines):
                if idx >= num_lines - 2:
                    line_colors.append(highlight_color)
                else:
                    line_colors.append(default_text_color)
        else:
            for idx in range(num_lines):
                if idx >= num_lines - 3:
                    line_colors.append(highlight_color)
                else:
                    line_colors.append(default_text_color)
                
    # Find matching font
    fonts_dir = os.path.join(BASE_DIR, "fonts")
    if font_type.lower() == "doge":
        font_path = os.path.join(fonts_dir, "league_gothic.ttf")
    elif font_type.lower() in ("maga", "charlie", "maga_charlie"):
        # Both Maga and Charlie styles use the Impact font (impact.ttf)
        font_path = None
        for name in ("impact.ttf", "maga.ttf"):
            local_impact = os.path.join(fonts_dir, name)
            if os.path.exists(local_impact):
                font_path = local_impact
                break
        if font_path is None:
            # Fallback to system paths if local is missing
            impact_paths = [
                "/System/Library/Fonts/Supplemental/Impact.ttf",
                "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
                "/usr/share/fonts/Impact.ttf",
                "C:\\Windows\\Fonts\\impact.ttf",
            ]
            for ip in impact_paths:
                if os.path.exists(ip):
                    font_path = ip
                    break
    else:
        font_path = os.path.join(fonts_dir, f"{font_type.lower()}.ttf")
    
    if font_path is None or not os.path.exists(font_path):
        # Local system fallbacks
        fallbacks = [
            "/System/Library/Fonts/Supplemental/Georgia.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "Arial.ttf"
        ]
        for fb in fallbacks:
            if os.path.exists(fb):
                font_path = fb
                break
        else:
            font_path = None
            
    # Text box geometry constraints
    box_width = 1080
    padding = 14
    target_text_width = box_width - (padding * 2)
    
    temp_img = Image.new("RGBA", (canvas_w, 1350))
    temp_draw = ImageDraw.Draw(temp_img)
    
    fonts = []
    line_widths = []
    line_heights = []
    line_bboxes = []
    
    # Font sizing constraints to prevent excessive scaling
    max_font_size = 9999
    min_font_size = 15
    
    for line in lines:
        # Calculate dynamic font size using ratio-based resizing
        base_size = 100
        try:
            if font_path is not None:
                base_font = ImageFont.truetype(font_path, base_size)
            else:
                base_font = ImageFont.load_default(size=base_size)
            bbox = temp_draw.textbbox((0, 0), line, font=base_font)
            base_w = bbox[2] - bbox[0]
            if base_w > 0:
                estimated_size = int(base_size * (target_text_width / base_w))
            else:
                estimated_size = 50
        except Exception:
            estimated_size = 50
            
        font_size = max(min_font_size, min(max_font_size, estimated_size))
        
        try:
            if font_path is not None:
                line_font = ImageFont.truetype(font_path, font_size)
            else:
                line_font = ImageFont.load_default(size=font_size)
        except Exception:
            try:
                line_font = ImageFont.load_default(size=font_size)
            except Exception:
                line_font = ImageFont.load_default()
            
        bbox = temp_draw.textbbox((0, 0), line, font=line_font)
        line_w = bbox[2] - bbox[0]
        line_h = get_line_height(line_font)
        
        fonts.append(line_font)
        line_widths.append(line_w)
        line_heights.append(line_h)
        line_bboxes.append(bbox)
        
    # Calculate box height
    if font_type.lower() in ("maga", "charlie", "maga_charlie"):
        line_spacing_multiplier = 0.75
    else:
        line_spacing_multiplier = 0.70
    total_text_height = 0
    first_line_top_offset = 0
    if num_lines > 0:
        current_offset = 0
        for i in range(num_lines - 1):
            h_curr = line_heights[i]
            h_next = line_heights[i+1]
            step = 0.5 * (1 + line_spacing_multiplier) * h_curr - 0.5 * (1 - line_spacing_multiplier) * h_next
            current_offset += step
        
        first_line_top_offset = line_bboxes[0][1]
        last_line_bottom_offset = line_bboxes[-1][3]
        total_text_height = current_offset + last_line_bottom_offset - first_line_top_offset
        
    if is_maga:
        box_top_padding = 7
        box_bottom_padding = 18
    else:
        box_top_padding = padding
        box_bottom_padding = padding
        
    box_height = int(total_text_height + box_top_padding + box_bottom_padding)
    
    start_y = 17
    # Load banner if style is faith
    banner_img = None
    banner_height = 0
    if font_type.lower() == "faith":
        banner_path = os.path.join(BASE_DIR, "resources", "faith banner.png")
        if not os.path.exists(banner_path):
            banner_path = "/Users/dhawansevkani/Downloads/faith banner.png"
        if os.path.exists(banner_path):
            try:
                banner_img = Image.open(banner_path)
                banner_height = banner_img.height
            except Exception as e:
                logger.error(f"Failed to load banner: {e}")
                
    if banner_img is not None:
        box_y = start_y + banner_height - 5
    else:
        box_y = start_y
        
    if font_type.lower() in ("maga", "charlie"):
        line_bottom_val = box_y + box_height + 20
    else:
        line_bottom_val = box_y + box_height

    # Load supporting PNG if style is maga or charlie
    support_img = None
    support_height = 0
    if font_type.lower() in ("maga", "charlie", "faith"):
        if user_support_image_path is not None and os.path.exists(user_support_image_path):
            try:
                support_img = Image.open(user_support_image_path)
                support_w = 1080
                # Calculate remaining space to make it fill from the line to the bottom (1350px)
                support_height = max(10, 1350 - line_bottom_val)
                
                # Crop to the exact aspect ratio of (1080 x support_height)
                img_aspect = support_img.width / support_img.height
                target_aspect = support_w / support_height
                if img_aspect > target_aspect:
                    new_width = int(support_img.height * target_aspect)
                    left = (support_img.width - new_width) // 2
                    support_img = support_img.crop((left, 0, left + new_width, support_img.height))
                else:
                    new_height = int(support_img.width / target_aspect)
                    top = (support_img.height - new_height) // 2
                    support_img = support_img.crop((0, top, support_img.width, top + new_height))
                
                support_img = support_img.resize((support_w, support_height), Image.Resampling.LANCZOS)
                
                # Enhance brightness to make it look bright and clear
                enhancer = ImageEnhance.Brightness(support_img)
                support_img = enhancer.enhance(1.3)
            except Exception as e:
                logger.error(f"Failed to load/resize supporting image: {e}")

    # Calculate total height of elements
    if banner_img is not None:
        total_height = banner_height + 5 + box_height
    elif support_img is not None:
        if user_support_image_path is not None:
            total_height = 1350
        else:
            total_height = box_height + 20 + support_height
    else:
        total_height = box_height

    # Final output dimensions (4:5 ratio)
    canvas_h = 1350
    
    # Generate background (transparent for maga/charlie/faith/doge, solid black for others)
    if font_type.lower() in ("maga", "charlie", "faith", "doge"):
        bg_img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    else:
        bg_img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 255))
    box_overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    box_draw = ImageDraw.Draw(box_overlay)
    
    # Start elements from the top (Y = 17px)
    start_y = 17
    
    # Paste banner if present
    if banner_img is not None:
        banner_x = 14
        banner_y = start_y
        bg_img.paste(banner_img, (banner_x, banner_y), banner_img if banner_img.mode in ('RGBA', 'LA') else None)
        
    box_x = (canvas_w - box_width) // 2
    box_coords = [box_x, box_y, box_x + box_width, box_y + box_height]
    
    box_draw.rectangle(
        box_coords,
        fill=box_bg
    )
    
    # Draw each line of text
    current_y = box_y + box_top_padding - first_line_top_offset
    for i in range(num_lines):
        line = lines[i]
        font = fonts[i]
        color = line_colors[i]
        
        # Use precise bounding box to avoid cutting off the edges
        bbox = box_draw.textbbox((0, 0), line, font=font)
        left, top, right, bottom = bbox
        w = right - left
        
        # Center text horizontally using exact bounds
        x = box_x + padding - left + (target_text_width - w) // 2
        
        box_draw.text((x, current_y), line, font=font, fill=color)
        if i < num_lines - 1:
            h_curr = line_heights[i]
            h_next = line_heights[i+1]
            step = 0.5 * (1 + line_spacing_multiplier) * h_curr - 0.5 * (1 - line_spacing_multiplier) * h_next
            current_y += step
        
    # Draw horizontal line and paste supporting PNG if present
    if font_type.lower() in ("maga", "charlie", "faith"):
        # Draw a horizontal line of thickness 20px, directly below the text box (0px gap)
        # Spans edge-to-edge (0 to 1080px)
        line_top = box_y + box_height
        if font_type.lower() in ("maga", "charlie"):
            line_bottom = line_top + 20
            box_draw.rectangle([0, line_top, 1080, line_bottom], fill=highlight_color)
        else:
            line_bottom = line_top
        
        # Paste supporting PNG directly below the line
        if support_img is not None:
            bg_img.paste(support_img, (0, line_bottom), support_img if support_img.mode in ('RGBA', 'LA') else None)
        
        # Draw watermarks for Maga and Charlie styles
        if font_type.lower() in ("maga", "charlie"):
            watermark_path = None
            if font_type.lower() == "maga":
                watermark_path = os.path.join(BASE_DIR, "resources", "Supporting Maga.png")
                if not os.path.exists(watermark_path):
                    watermark_path = "/Users/dhawansevkani/Downloads/Supporting Maga.png"
            elif font_type.lower() == "charlie":
                watermark_path = os.path.join(BASE_DIR, "resources", "charlie banner.png")
                if not os.path.exists(watermark_path):
                    watermark_path = "/Users/dhawansevkani/Downloads/charlie banner.png"
            
            if watermark_path and os.path.exists(watermark_path):
                try:
                    watermark_img = Image.open(watermark_path)
                    watermark_w = 1080
                    watermark_h = int(watermark_img.height * (watermark_w / watermark_img.width))
                    watermark_img = watermark_img.resize((watermark_w, watermark_h), Image.Resampling.LANCZOS)
                    bg_img.paste(watermark_img, (0, line_bottom), watermark_img if watermark_img.mode in ('RGBA', 'LA') else None)
                except Exception as e:
                    logger.error(f"Failed to load/paste watermark: {e}")
        
    # Combine layers
    final_img = Image.alpha_composite(bg_img, box_overlay)
    
    if font_type.lower() == "doge":
        final_img = final_img.crop((0, box_y, canvas_w, box_y + box_height))
        
    out_buf = BytesIO()
    final_img.save(out_buf, format="PNG")
    out_buf.seek(0)
    return out_buf, line_bottom_val

def create_subject_shape(img, size: int, shape_type: str, border_color: tuple, border_width: int = 4) -> Image.Image:
    """
    Creates a square or circular masked subject image with a colored border.
    """
    if isinstance(img, str):
        img = Image.open(img)
    img_w, img_h = img.size
    crop_size = min(img_w, img_h)
    left = (img_w - crop_size) // 2
    top = (img_h - crop_size) // 2
    img = img.crop((left, top, left + crop_size, top + crop_size))
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    
    outer_size = size + border_width * 2
    canvas = Image.new("RGBA", (outer_size, outer_size), (0, 0, 0, 0))
    canvas_draw = ImageDraw.Draw(canvas)
    
    if shape_type.lower() == "circle":
        mask_draw.ellipse((0, 0, size, size), fill=255)
        canvas_draw.ellipse((0, 0, outer_size, outer_size), fill=border_color)
    else: # square
        mask_draw.rectangle((0, 0, size, size), fill=255)
        canvas_draw.rectangle((0, 0, outer_size, outer_size), fill=border_color)
        
    masked_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    masked_img.paste(img, (0, 0), mask)
    
    canvas.paste(masked_img, (border_width, border_width), masked_img)
    return canvas

def overlay_png_on_image(
    bg_image_path: str, 
    png_bytes: bytes, 
    shape_image_path: str = None,
    shape_type: str = "square", 
    border_color: tuple = (255, 222, 89),
    shape_offset_x: int = 0,
    shape_offset_y: int = 0,
    shape_scale: float = 1.0,
    simple_overlay: bool = False,
    line_bottom: int = None,
    # Second shape parameters
    shape2_image_path: str = None,
    shape2_offset_x: int = 0,
    shape2_offset_y: int = 0,
    shape2_scale: float = 1.0
) -> BytesIO:
    """
    Overlays a transparent PNG image on top of a background image.
    The top part (till line_bottom) is solid black.
    The bottom part contains:
      - Bottom layer: The background image
      - Middle layer: The background image or custom shape images cropped to a shape (square/circle) with colored border, offsets, and scale
      - Top layer: The cutout subject (foreground person with background removed)
    """
    overlay = Image.open(BytesIO(png_bytes)).convert("RGBA")
    
    # Find line_bottom (last row with non-transparent content)
    if line_bottom is None:
        line_bottom = 0
        for y in range(overlay.height):
            if overlay.getpixel((0, y))[3] > 0:
                line_bottom = y + 1
            
    # Create solid black canvas
    canvas = Image.new("RGBA", (1080, 1350), (0, 0, 0, 255))
    
    if line_bottom > 0 and line_bottom < 1350:
        target_h = 1350 - line_bottom
        bg_img = Image.open(bg_image_path).convert("RGBA")
        
        # Calculate cropping coordinates to fill 1080 x target_h
        bg_aspect = bg_img.width / bg_img.height
        target_aspect = 1080 / target_h
        
        if bg_aspect > target_aspect:
            new_w = int(bg_img.height * target_aspect)
            left = (bg_img.width - new_w) // 2
            crop_box = (left, 0, left + new_w, bg_img.height)
        else:
            new_h = int(bg_img.width / target_aspect)
            top = (bg_img.height - new_h) // 2
            crop_box = (0, top, bg_img.width, top + new_h)
            
        bg_cropped = bg_img.crop(crop_box).resize((1080, target_h), Image.Resampling.LANCZOS)
        
        # 1. Paste background image (bottom layer)
        canvas.paste(bg_cropped, (0, line_bottom))
        
        if not simple_overlay:
            # 2. Generate and paste subject shapes (middle layer)
            # Paste Shape 1
            if shape_image_path is not None and os.path.exists(shape_image_path):
                shape_img = Image.open(shape_image_path)
                base_size = min(360, target_h - 40)
                size = int(base_size * shape_scale)
                if size > 20:
                    subject_shape = create_subject_shape(shape_img, size, shape_type, border_color)
                    center_x = 1080 // 2
                    center_y = line_bottom + target_h // 2
                    paste_x = center_x - subject_shape.width // 2 + shape_offset_x
                    paste_y = center_y - subject_shape.height // 2 + shape_offset_y
                    canvas.paste(subject_shape, (paste_x, paste_y), subject_shape)
                    
            # Paste Shape 2 (if present)
            if shape2_image_path is not None and os.path.exists(shape2_image_path):
                shape2_img = Image.open(shape2_image_path)
                base_size = min(360, target_h - 40)
                size2 = int(base_size * shape2_scale)
                if size2 > 20:
                    subject_shape2 = create_subject_shape(shape2_img, size2, shape_type, border_color)
                    center_x = 1080 // 2
                    center_y = line_bottom + target_h // 2
                    paste2_x = center_x - subject_shape2.width // 2 + shape2_offset_x
                    paste2_y = center_y - subject_shape2.height // 2 + shape2_offset_y
                    canvas.paste(subject_shape2, (paste2_x, paste2_y), subject_shape2)
                
            # 3. Generate cutout (top layer) and crop/resize it identically
            try:
                cutout = remove(bg_img)
                cutout_cropped = cutout.crop(crop_box).resize((1080, target_h), Image.Resampling.LANCZOS)
                canvas.paste(cutout_cropped, (0, line_bottom), cutout_cropped)
            except Exception as e:
                logger.error(f"Failed to remove background for 3D effect: {e}")
        
    combined = Image.alpha_composite(canvas, overlay)
    
    out_buf = BytesIO()
    combined.convert("RGB").save(out_buf, format="JPEG", quality=95)
    out_buf.seek(0)
    return out_buf

def overlay_png_on_video(video_path: str, png_bytes: bytes, output_path: str, subject_image_path: str = None, shape_type: str = "square", border_color: tuple = (255, 222, 89), line_bottom: int = None):
    """
    Overlays a transparent PNG image on top of a video.
    The top part (till line_bottom) is solid black.
    The bottom part contains the video frame.
    """
    png_data = np.frombuffer(png_bytes, dtype=np.uint8)
    overlay_img = cv2.imdecode(png_data, cv2.IMREAD_UNCHANGED)
    if overlay_img is None:
        raise ValueError("Could not decode PNG overlay image.")
    overlay_img = cv2.resize(overlay_img, (1080, 1350))
    
    # Find line_bottom (last row where alpha channel at column 0 is > 0)
    if line_bottom is None:
        line_bottom = 0
        alpha_col = overlay_img[:, 0, 3]
        non_zero_indices = np.where(alpha_col > 0)[0]
        if len(non_zero_indices) > 0:
            line_bottom = int(non_zero_indices[-1] + 1)
        
    overlay_bgr = overlay_img[:, :, :3]
    overlay_mask = overlay_img[:, :, 3] / 255.0
    overlay_mask = np.expand_dims(overlay_mask, axis=2)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError("Could not open video file.")
        
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0

    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (1080, 1350))
    
    target_h = 1350 - line_bottom
    
    # Prepare static subject shape if provided
    subject_bgr = None
    subject_mask = None
    subject_coords = None
    
    if subject_image_path is not None and os.path.exists(subject_image_path):
        size = min(360, target_h - 40)
        if size > 20:
            subject_img = create_subject_shape(subject_image_path, size, shape_type, border_color)
            subject_np = np.array(subject_img)
            subject_bgr = cv2.cvtColor(subject_np[:, :, :3], cv2.COLOR_RGBA2BGRA)[:, :, :3]
            subject_mask = subject_np[:, :, 3] / 255.0
            subject_mask = np.expand_dims(subject_mask, axis=2)
            
            center_x = 1080 // 2
            center_y = line_bottom + target_h // 2
            paste_x = center_x - subject_img.width // 2
            paste_y = center_y - subject_img.height // 2
            subject_coords = (paste_x, paste_y, paste_x + subject_img.width, paste_y + subject_img.height)
            
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
                
            # Create a solid black canvas frame
            canvas_frame = np.zeros((1350, 1080, 3), dtype=np.uint8)
            
            if target_h > 0:
                h, w = frame.shape[:2]
                frame_aspect = w / h
                target_aspect = 1080 / target_h
                
                if frame_aspect > target_aspect:
                    new_w = int(h * target_aspect)
                    left = (w - new_w) // 2
                    cropped = frame[:, left:left+new_w]
                else:
                    new_h = int(w / target_aspect)
                    top = (h - new_h) // 2
                    cropped = frame[top:top+new_h, :]
                    
                resized_vid_frame = cv2.resize(cropped, (1080, target_h))
                
                # Place resized video frame in the bottom part of the black canvas frame
                canvas_frame[line_bottom:1350, :] = resized_vid_frame
                
                # Blend subject shape on top of the canvas frame
                if subject_bgr is not None:
                    x1, y1, x2, y2 = subject_coords
                    canvas_roi = canvas_frame[y1:y2, x1:x2]
                    blended_roi = (canvas_roi * (1.0 - subject_mask) + subject_bgr * subject_mask).astype(np.uint8)
                    canvas_frame[y1:y2, x1:x2] = blended_roi
                
            blended = (canvas_frame * (1.0 - overlay_mask) + overlay_bgr * overlay_mask).astype(np.uint8)
            out.write(blended)
    finally:
        cap.release()
        out.release()

# ----------- Telegram Bot Flow -----------

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

# States
STATE_CHOOSING_FONT = 1
STATE_CHOOSING_COLOR = 2
STATE_WAITING_TEXT = 3
STATE_WAITING_BACKGROUND = 4
STATE_WAITING_SHAPE_IMAGE = 5
STATE_POSITIONING_SHAPE = 6
STATE_CONFIRM_IG_OCR = 7
STATE_WAITING_SHAPE2_IMAGE = 8
STATE_WAITING_DOGE_HIGHLIGHT_LINES = 9

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: Greet user and offer font styles."""
    user = update.effective_user
    keyboard = [
        [
            InlineKeyboardButton("✨ Faith (Lilita One)", callback_data="font:faith")
        ],
        [
            InlineKeyboardButton("🇺🇸 Maga (Impact)", callback_data="font:maga"),
            InlineKeyboardButton("🇺🇸 Charlie (Impact)", callback_data="font:charlie")
        ],
        [
            InlineKeyboardButton("🐕 Doge (League Gothic)", callback_data="font:doge")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = (
        f"👋 Welcome {user.first_name} to the Text Box Generator!\n\n"
        "Please choose a font style to start:"
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg, reply_markup=reply_markup)
        
    return STATE_CHOOSING_FONT

async def font_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle font selection callback."""
    query = update.callback_query
    await query.answer()
    
    font_choice = query.data.split(":")[1]
    context.user_data["chosen_font"] = font_choice
    save_user_session(update.effective_user.id, font_choice, context.user_data.get("chosen_color"), context.user_data.get("doge_highlight_lines"))
    
    display_names = {
        "faith": "Faith (Lilita One)",
        "maga": "Maga (Impact)",
        "charlie": "Charlie (Impact)",
        "maga_charlie": "Maga/Charlie (Impact)",
        "doge": "Doge (League Gothic)"
    }
    
    name = display_names.get(font_choice, font_choice)
    
    if font_choice in ("maga", "charlie", "maga_charlie", "faith", "doge"):
        keyboard = [
            [
                InlineKeyboardButton("🟡 Yellow (#ffde59)", callback_data="color:yellow"),
                InlineKeyboardButton("🔴 Red (#ff3131)", callback_data="color:red")
            ],
            [
                InlineKeyboardButton("🟠 Orange (#ff914d)", callback_data="color:orange"),
                InlineKeyboardButton("🟣 Magenta (#cb6ce6)", callback_data="color:magenta")
            ],
            [
                InlineKeyboardButton("🟢 Green (#00e676)", callback_data="color:green"),
                InlineKeyboardButton("🔵 Blue (#38b6ff)", callback_data="color:blue")
            ],
            [
                InlineKeyboardButton("🟪 Purple (#8c52ff)", callback_data="color:purple")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("🟡 Yellow (#ffde59)", callback_data="color:yellow"),
                InlineKeyboardButton("🔴 Red (#ff3131)", callback_data="color:red")
            ],
            [
                InlineKeyboardButton("🟢 Green (#00e676)", callback_data="color:green"),
                InlineKeyboardButton("🔵 Blue (#38b6ff)", callback_data="color:blue")
            ],
            [
                InlineKeyboardButton("🟪 Purple (#8c52ff)", callback_data="color:purple")
            ]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"Selected Style: **{name}**\n\n🎨 Now, please select the highlight color:",
        reply_markup=reply_markup
    )
    return STATE_CHOOSING_COLOR

async def color_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle color selection callback."""
    query = update.callback_query
    await query.answer()
    
    color_choice = query.data.split(":")[1]
    context.user_data["chosen_color"] = color_choice
    save_user_session(update.effective_user.id, context.user_data.get("chosen_font"), color_choice, context.user_data.get("doge_highlight_lines"))
    
    color_names = {
        "yellow": "🟡 Yellow",
        "red": "🔴 Red",
        "orange": "🟠 Orange",
        "magenta": "🟣 Magenta",
        "green": "🟢 Green",
        "blue": "🔵 Blue",
        "purple": "🟪 Purple"
    }
    color_name = color_names.get(color_choice, color_choice.capitalize())
    font_choice = context.user_data.get("chosen_font", "faith")
    
    await query.edit_message_text(
        text=f"🎨 Font: **{font_choice.capitalize()}** | Color: **{color_name}**\n\n"
             "💬 Send me the text you want in your box.\n"
             "Use line breaks (Shift+Enter) to define separate lines."
    )
    return STATE_WAITING_TEXT

async def change_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle color change after output — re-render with new color if last text exists."""
    restore_session_if_needed(update, context)
    query = update.callback_query
    await query.answer()
    
    color_choice = query.data.split(":")[1]
    context.user_data["chosen_color"] = color_choice
    save_user_session(update.effective_user.id, context.user_data.get("chosen_font"), color_choice, context.user_data.get("doge_highlight_lines"))
    
    color_names = {
        "yellow": "🟡 Yellow",
        "red": "🔴 Red",
        "orange": "🟠 Orange",
        "magenta": "🟣 Magenta",
        "green": "🟢 Green",
        "blue": "🔵 Blue",
        "purple": "🟪 Purple"
    }
    color_name = color_names.get(color_choice, color_choice.capitalize())
    font_choice = context.user_data.get("chosen_font", "faith")
    last_text = context.user_data.get("last_text", None)
    
    if last_text:
        # Re-render with the new color
        status_msg = await query.message.reply_text("🎨 Re-rendering with new color...")
        try:
            user_support_image_path = context.user_data.get("user_support_image_path")
            image_buffer, line_bottom = generate_textbox_image(
                last_text, font_choice, color_choice,
                user_support_image_path=user_support_image_path,
                doge_highlight_lines=context.user_data.get("doge_highlight_lines")
            )
            
            # Save transparent PNG bytes in session
            png_bytes = image_buffer.getvalue()
            context.user_data["transparent_png_bytes"] = png_bytes
            context.user_data["line_bottom"] = line_bottom
            
            # Build post-render keyboard with color change + font change options
            if font_choice in ("maga", "charlie", "maga_charlie", "faith", "doge"):
                keyboard = [
                    [
                        InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                        InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                    ],
                    [
                        InlineKeyboardButton("🟠 Orange", callback_data="recolor:orange"),
                        InlineKeyboardButton("🟣 Magenta", callback_data="recolor:magenta")
                    ],
                    [
                        InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                        InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                    ],
                    [
                        InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                    ],
                    [
                        InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                    ]
                ]
            else:
                keyboard = [
                    [
                        InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                        InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                    ],
                    [
                        InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                        InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                    ],
                    [
                        InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                    ],
                    [
                        InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                    ]
                ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            caption_text = (
                f"✅ **{font_choice.capitalize()}** text box with **{color_name}** highlight!\n\n"
                "📝 Send more text to generate another, or use buttons below."
            )
            if font_choice in ("maga", "charlie", "faith"):
                image_buffer.name = f"{font_choice}_textbox.png"
                await query.message.reply_document(
                    document=image_buffer,
                    caption=caption_text,
                    reply_markup=reply_markup
                )
            else:
                await query.message.reply_photo(
                    photo=image_buffer,
                    caption=caption_text,
                    reply_markup=reply_markup
                )
            await status_msg.delete()
            
            # Re-prompt for background image/video
            await query.message.reply_text(
                "🖼️ **Would you like to overlay this text box on top of a background image or video?**\n\n"
                "📥 **Send an image or a video now** to blend it,\n"
                "➡️ Or click/type /skip to keep the transparent PNG layout and send new text."
            )
            return STATE_WAITING_BACKGROUND
            
        except Exception as e:
            logger.error(f"Failed to re-render: {e}")
            await status_msg.edit_text(f"❌ Error re-rendering: {str(e)}")
            return STATE_WAITING_BACKGROUND
    else:
        await query.message.reply_text(
            f"Color changed to **{color_name}**. Send text to generate an image."
        )
        return STATE_WAITING_TEXT

async def change_font(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Let user change font without restarting."""
    restore_session_if_needed(update, context)
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [
            InlineKeyboardButton("✨ Faith", callback_data="font:faith")
        ],
        [
            InlineKeyboardButton("🇺🇸 Maga", callback_data="font:maga"),
            InlineKeyboardButton("🇺🇸 Charlie", callback_data="font:charlie")
        ],
        [
            InlineKeyboardButton("🐕 Doge (League Gothic)", callback_data="font:doge")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.reply_text(
        "🔄 Choose a new font style:",
        reply_markup=reply_markup
    )
    return STATE_CHOOSING_FONT

async def image_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download and store user-provided custom supporting image."""
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("📥 Downloading your image, please wait...")
    
    try:
        # Get the photo or document file
        if update.message.photo:
            file_obj = await update.message.photo[-1].get_file()
        elif update.message.document:
            file_obj = await update.message.document.get_file()
        else:
            await status_msg.edit_text("❌ No valid image found.")
            return STATE_WAITING_TEXT
            
        file_path = os.path.join(BASE_DIR, f"user_support_{user_id}.png")
        await file_obj.download_to_drive(file_path)
        
        context.user_data["user_support_image_path"] = file_path
        
        await status_msg.edit_text(
            "📸 Custom supporting image saved successfully!\n\n"
            "📝 Now send the text to generate the transparent box on top of it.\n"
            "🧹 To clear this image and go back to the default layout, send /clear_image."
        )
    except Exception as e:
        logger.error(f"Failed to download user image: {e}")
        await status_msg.edit_text(f"❌ Failed to process image: {str(e)}")
        
    return STATE_WAITING_TEXT

async def clear_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Clear custom supporting image from user data."""
    context.user_data.pop("user_support_image_path", None)
    await update.message.reply_text(
        "🧹 Custom supporting image cleared!"
    )
    return STATE_WAITING_TEXT

async def text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Generate and send image upon text reception. User stays on the same page."""
    restore_session_if_needed(update, context)
    text = update.message.text.strip()
    user_id = update.effective_user.id
    font_choice = context.user_data.get("chosen_font", "faith")
    if font_choice == "charlie":
        charlie_colors = ["yellow", "red", "orange", "magenta", "green", "blue", "purple"]
        current_index = context.user_data.get("charlie_color_index", 0)
        color_choice = charlie_colors[current_index]
        context.user_data["chosen_color"] = color_choice
        context.user_data["charlie_color_index"] = (current_index + 1) % len(charlie_colors)
    else:
        color_choice = context.user_data.get("chosen_color", "yellow")
    
    is_instagram = "instagram.com" in text.lower() or "instagr.am" in text.lower()
    
    if is_instagram:
        status_msg = await update.message.reply_text("🔍 Downloading Instagram post image & performing OCR...")
        try:
            # 1. Extract shortcode
            parts = text.strip("/").split("/")
            shortcode = None
            for i, p in enumerate(parts):
                if p in ("p", "reel", "tv") and i + 1 < len(parts):
                    shortcode = parts[i + 1].split("?")[0]
                    break
            
            if not shortcode:
                raise ValueError("Could not extract Instagram shortcode from URL.")
                
            # 2. Download high-res image via the public media redirection link
            media_url = f"https://www.instagram.com/p/{shortcode}/media/?size=l"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            }
            img_response = requests.get(media_url, headers=headers, allow_redirects=True, timeout=15)
            if img_response.status_code != 200 or "image" not in img_response.headers.get("Content-Type", ""):
                raise ValueError(f"Failed to download image (HTTP {img_response.status_code})")
                
            # 3. Call OCR.space with the downloaded image bytes
            await status_msg.edit_text("🤖 Image downloaded! Extracting text via OCR...")
            extracted_text = call_ocr_space(img_response.content)
            
            # If OCR failed, try fallback to caption
            if not extracted_text:
                try:
                    caption, _, _ = fetch_instagram_post(text, user_id)
                    extracted_text = caption
                except Exception:
                    pass
            
            if not extracted_text:
                raise ValueError("Could not extract text via OCR or caption.")
                
            # 4. Save extracted text in context
            context.user_data["ocr_extracted_text"] = extracted_text
            
            # AI Proofreading check on extracted OCR text
            corrected_text = proofread_text_with_ai(extracted_text)
            has_corrections = (corrected_text.upper().strip() != extracted_text.upper().strip())
            context.user_data["suggested_corrected_text"] = corrected_text
            
            if has_corrections:
                kb = [
                    [
                        InlineKeyboardButton("✨ Use Corrected Version", callback_data="ig_ocr:proceed_corrected"),
                        InlineKeyboardButton("📄 Use Original OCR Text", callback_data="ig_ocr:proceed_original")
                    ],
                    [
                        InlineKeyboardButton("✏️ Edit Text", callback_data="ig_ocr:edit")
                    ]
                ]
            else:
                kb = [
                    [
                        InlineKeyboardButton("✅ Proceed (Text is Good)", callback_data="ig_ocr:proceed_original"),
                        InlineKeyboardButton("✏️ Edit Text", callback_data="ig_ocr:edit")
                    ]
                ]
                
            markup = InlineKeyboardMarkup(kb)
            await status_msg.delete()
            
            if has_corrections:
                msg_body = (
                    f"📝 **Text extracted from Instagram post:**\n"
                    f"`{extracted_text}`\n\n"
                    f"✨ **AI Grammatical Correction:**\n"
                    f"`{corrected_text}`\n\n"
                    f"Which version would you like to proceed with?"
                )
            else:
                msg_body = (
                    f"📝 **Here is the text I extracted from that Instagram post:**\n\n"
                    f"`{extracted_text}`\n\n"
                    f"Does this look good, or would you like to edit it?"
                )
                
            await update.message.reply_text(
                msg_body,
                reply_markup=markup,
                parse_mode="Markdown"
            )
            return STATE_CONFIRM_IG_OCR
            
        except Exception as e:
            logger.error(f"Instagram OCR workflow failed: {e}")
            await status_msg.edit_text(
                f"⚠️ **Could not download or OCR the Instagram post:** {str(e)}.\n\n"
                "Please type/send your text box text directly, or upload the image post file here!"
            )
            return STATE_WAITING_TEXT
            
    # Non-Instagram regular rendering path
    if font_choice == "doge":
        context.user_data["doge_highlight_lines"] = None
        context.user_data["doge_pending_text"] = text
        await update.message.reply_text(
            "🖍️ **How many lines would you like to highlight/color?**\n\n"
            "Send a number (e.g. 1, 2, 3, etc.):"
        )
        return STATE_WAITING_DOGE_HIGHLIGHT_LINES
        
    status_msg = await update.message.reply_text("🎨 Checking grammar & rendering, please wait...")
    
    # AI Proofreading check
    corrected_text = proofread_text_with_ai(text)
    has_corrections = (corrected_text.upper().strip() != text.upper().strip())
    
    # Store the last text so color change can re-render
    context.user_data["last_text"] = text
    
    try:
        user_support_image_path = context.user_data.get("user_support_image_path")
        image_buffer, line_bottom = generate_textbox_image(text, font_choice, color_choice, user_support_image_path=user_support_image_path)
        
        # Save transparent PNG bytes in session
        png_bytes = image_buffer.getvalue()
        context.user_data["transparent_png_bytes"] = png_bytes
        context.user_data["line_bottom"] = line_bottom
        
        color_names = {
            "yellow": "🟡 Yellow",
            "red": "🔴 Red",
            "orange": "🟠 Orange",
            "magenta": "🟣 Magenta",
            "green": "🟢 Green",
            "blue": "🔵 Blue",
            "purple": "🟪 Purple"
        }
        color_name = color_names.get(color_choice, color_choice.capitalize())
        
        # Build post-render keyboard
        if font_choice in ("maga", "charlie", "maga_charlie", "faith"):
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟠 Orange", callback_data="recolor:orange"),
                    InlineKeyboardButton("🟣 Magenta", callback_data="recolor:magenta")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
        else:
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        caption_text = f"✅ **{font_choice.capitalize()}** transparent text box generated!"
        if font_choice in ("maga", "charlie", "faith"):
            image_buffer.name = f"{font_choice}_textbox.png"
            await update.message.reply_document(
                document=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_photo(
                photo=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
            
        await status_msg.delete()
        
        # Send proofreading suggestion if corrections found
        if has_corrections:
            context.user_data["suggested_corrected_text"] = corrected_text
            kb = [
                [
                    InlineKeyboardButton("✅ Use Corrected Text", callback_data="proofread:accept"),
                    InlineKeyboardButton("❌ Keep Original", callback_data="proofread:ignore")
                ]
            ]
            markup = InlineKeyboardMarkup(kb)
            await update.message.reply_text(
                f"📝 **AI Proofreading Suggestion:**\n"
                f"I detected some spelling or grammar errors.\n\n"
                f"**Suggested Correction:**\n"
                f"`{corrected_text}`\n\n"
                f"Would you like to switch to this version?",
                reply_markup=markup,
                parse_mode="Markdown"
            )
        await update.message.reply_text(
            "🖼️ **Would you like to overlay this text box on top of a background image or video?**\n\n"
            "📥 **Send an image or a video now** to blend it,\n"
            "➡️ Or click/type /skip to keep the transparent PNG layout and send new text."
        )
        return STATE_WAITING_BACKGROUND
        
    except Exception as e:
        logger.error(f"Failed to generate/send image: {e}")
        await status_msg.edit_text(f"❌ Sorry, an error occurred during rendering: {str(e)}")
        return STATE_WAITING_TEXT

async def doge_highlight_lines_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle receiving number of lines to color for Doge style."""
    restore_session_if_needed(update, context)
    num_lines_text = update.message.text.strip()
    try:
        num_lines = int(num_lines_text)
        if num_lines < 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("⚠️ Please send a valid positive number (e.g. 1, 2, 3):")
        return STATE_WAITING_DOGE_HIGHLIGHT_LINES

    context.user_data["doge_highlight_lines"] = num_lines
    save_user_session(update.effective_user.id, "doge", context.user_data.get("chosen_color"), num_lines)
    text = context.user_data.get("doge_pending_text", "")
    
    # Clear the pending text
    context.user_data.pop("doge_pending_text", None)
    
    status_msg = await update.message.reply_text("🎨 Checking grammar & rendering, please wait...")
    
    # AI Proofreading check
    corrected_text = proofread_text_with_ai(text)
    has_corrections = (corrected_text.upper().strip() != text.upper().strip())
    
    # Store the last text so color change can re-render
    context.user_data["last_text"] = text
    font_choice = "doge"
    color_choice = context.user_data.get("chosen_color", "yellow")
    
    try:
        user_support_image_path = context.user_data.get("user_support_image_path")
        image_buffer, line_bottom = generate_textbox_image(
            text, font_choice, color_choice,
            user_support_image_path=user_support_image_path,
            doge_highlight_lines=num_lines
        )
        
        # Save transparent PNG bytes in session
        png_bytes = image_buffer.getvalue()
        context.user_data["transparent_png_bytes"] = png_bytes
        context.user_data["line_bottom"] = line_bottom
        
        color_names = {
            "yellow": "🟡 Yellow",
            "red": "🔴 Red",
            "orange": "🟠 Orange",
            "magenta": "🟣 Magenta",
            "green": "🟢 Green",
            "blue": "🔵 Blue",
            "purple": "🟪 Purple"
        }
        color_name = color_names.get(color_choice, color_choice.capitalize())
        
        # Build post-render keyboard
        keyboard = [
            [
                InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
            ],
            [
                InlineKeyboardButton("🟠 Orange", callback_data="recolor:orange"),
                InlineKeyboardButton("🟣 Magenta", callback_data="recolor:magenta")
            ],
            [
                InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
            ],
            [
                InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
            ],
            [
                InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        caption_text = f"✅ **Doge** transparent text box generated!"
        image_buffer.name = "doge_textbox.png"
        await update.message.reply_document(
            document=image_buffer,
            caption=caption_text,
            reply_markup=reply_markup
        )
            
        await status_msg.delete()
        
        # Send proofreading suggestion if corrections found
        if has_corrections:
            context.user_data["suggested_corrected_text"] = corrected_text
            kb = [
                [
                    InlineKeyboardButton("✅ Use Corrected Text", callback_data="proofread:accept"),
                    InlineKeyboardButton("❌ Keep Original", callback_data="proofread:ignore")
                ]
            ]
            markup = InlineKeyboardMarkup(kb)
            await update.message.reply_text(
                f"📝 **AI Proofreading Suggestion:**\n"
                f"I detected some spelling or grammar errors.\n\n"
                f"**Suggested Correction:**\n"
                f"`{corrected_text}`\n\n"
                f"Would you like to switch to this version?",
                reply_markup=markup,
                parse_mode="Markdown"
            )
        await update.message.reply_text(
            "🖼️ **Would you like to overlay this text box on top of a background image or video?**\n\n"
            "📥 **Send an image or a video now** to blend it,\n"
            "➡️ Or click/type /skip to keep the transparent PNG layout and send new text."
        )
        return STATE_WAITING_BACKGROUND
        
    except Exception as e:
        logger.error(f"Failed to generate/send image: {e}")
        await status_msg.edit_text(f"❌ Sorry, an error occurred during rendering: {str(e)}")
        return STATE_WAITING_TEXT

async def ig_ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle callback queries in the Instagram OCR confirmation screen."""
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(":")[1]
    if action == "edit":
        text_to_edit = context.user_data.get("suggested_corrected_text") or context.user_data.get("ocr_extracted_text", "")
        await query.message.reply_text(
            "💬 **Please copy the text below, edit it, and reply to this message with your updated version:**\n\n"
            f"`{text_to_edit}`",
            parse_mode="Markdown"
        )
        return STATE_CONFIRM_IG_OCR
        
    if action == "proceed_corrected":
        chosen_text = context.user_data.get("suggested_corrected_text", "").strip()
    else:  # proceed_original
        chosen_text = context.user_data.get("ocr_extracted_text", "").strip()
        
    if not chosen_text:
        await query.message.reply_text("❌ No text found. Please send the link or text again.")
        return STATE_WAITING_TEXT
        
    context.user_data["last_text"] = chosen_text
    
    # Ask for color!
    font_choice = context.user_data.get("chosen_font", "faith")
    if font_choice == "charlie":
        charlie_colors = ["yellow", "red", "orange", "magenta", "green", "blue", "purple"]
        current_index = context.user_data.get("charlie_color_index", 0)
        color_choice = charlie_colors[current_index]
        context.user_data["chosen_color"] = color_choice
        context.user_data["charlie_color_index"] = (current_index + 1) % len(charlie_colors)
        
        status_msg = await query.message.reply_text("🎨 Rendering your text box, please wait...")
        try:
            user_support_image_path = context.user_data.get("user_support_image_path")
            image_buffer, line_bottom = generate_textbox_image(
                chosen_text, "charlie", color_choice, user_support_image_path=user_support_image_path
            )
            
            # Save transparent PNG bytes in session
            png_bytes = image_buffer.getvalue()
            context.user_data["transparent_png_bytes"] = png_bytes
            context.user_data["line_bottom"] = line_bottom
            
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟠 Orange", callback_data="recolor:orange"),
                    InlineKeyboardButton("🟣 Magenta", callback_data="recolor:magenta")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            caption_text = f"✅ **Charlie** transparent text box generated with auto-cycled color!"
            image_buffer.name = "charlie_textbox.png"
            await query.message.reply_document(
                document=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
            await status_msg.delete()
            
            # Ask user for background image/video
            await query.message.reply_text(
                "🖼️ **Would you like to overlay this text box on top of a background image or video?**\n\n"
                "📥 **Send an image or a video now** to blend it,\n"
                "➡️ Or click/type /skip to keep the transparent PNG layout and send new text."
            )
            return STATE_WAITING_BACKGROUND
            
        except Exception as e:
            logger.error(f"Failed to generate/send image in OCR flow for Charlie: {e}")
            await status_msg.edit_text(f"❌ Sorry, an error occurred during rendering: {str(e)}")
            return STATE_WAITING_TEXT
            
    if font_choice in ("maga", "charlie", "maga_charlie", "faith"):
        keyboard = [
            [
                InlineKeyboardButton("🟡 Yellow (#ffde59)", callback_data="color_ocr:yellow"),
                InlineKeyboardButton("🔴 Red (#ff3131)", callback_data="color_ocr:red")
            ],
            [
                InlineKeyboardButton("🟠 Orange (#ff914d)", callback_data="color_ocr:orange"),
                InlineKeyboardButton("🟣 Magenta (#cb6ce6)", callback_data="color_ocr:magenta")
            ],
            [
                InlineKeyboardButton("🟢 Green (#00e676)", callback_data="color_ocr:green"),
                InlineKeyboardButton("🔵 Blue (#38b6ff)", callback_data="color_ocr:blue")
            ],
            [
                InlineKeyboardButton("🟪 Purple (#8c52ff)", callback_data="color_ocr:purple")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("🟡 Yellow (#ffde59)", callback_data="color_ocr:yellow"),
                InlineKeyboardButton("🔴 Red (#ff3131)", callback_data="color_ocr:red")
            ],
            [
                InlineKeyboardButton("🟢 Green (#00e676)", callback_data="color_ocr:green"),
                InlineKeyboardButton("🔵 Blue (#38b6ff)", callback_data="color_ocr:blue")
            ],
            [
                InlineKeyboardButton("🟪 Purple (#8c52ff)", callback_data="color_ocr:purple")
            ]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "🎨 **Please select the highlight color to generate the text box:**",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return STATE_CONFIRM_IG_OCR

async def ig_ocr_text_edited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle text message input when the user edits the OCR text."""
    text = update.message.text.strip()
    
    # Run AI proofreading on edited text too!
    corrected_text = proofread_text_with_ai(text)
    has_corrections = (corrected_text.upper().strip() != text.upper().strip())
    
    context.user_data["ocr_extracted_text"] = text
    context.user_data["suggested_corrected_text"] = corrected_text
    
    if has_corrections:
        kb = [
            [
                InlineKeyboardButton("✨ Use Corrected Version", callback_data="ig_ocr:proceed_corrected"),
                InlineKeyboardButton("📄 Use My Original", callback_data="ig_ocr:proceed_original")
            ],
            [
                InlineKeyboardButton("✏️ Edit Again", callback_data="ig_ocr:edit")
            ]
        ]
        markup = InlineKeyboardMarkup(kb)
        await update.message.reply_text(
            f"📝 **Your edited text:**\n`{text}`\n\n"
            f"✨ **AI Grammatical Correction:**\n`{corrected_text}`\n\n"
            "Which version would you like to use?",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return STATE_CONFIRM_IG_OCR
        
    context.user_data["last_text"] = text
    
    font_choice = context.user_data.get("chosen_font", "faith")
    if font_choice == "charlie":
        charlie_colors = ["yellow", "red", "orange", "magenta", "green", "blue", "purple"]
        current_index = context.user_data.get("charlie_color_index", 0)
        color_choice = charlie_colors[current_index]
        context.user_data["chosen_color"] = color_choice
        context.user_data["charlie_color_index"] = (current_index + 1) % len(charlie_colors)
        
        status_msg = await update.message.reply_text("🎨 Rendering your text box, please wait...")
        try:
            user_support_image_path = context.user_data.get("user_support_image_path")
            image_buffer, line_bottom = generate_textbox_image(
                text, "charlie", color_choice, user_support_image_path=user_support_image_path
            )
            
            # Save transparent PNG bytes in session
            png_bytes = image_buffer.getvalue()
            context.user_data["transparent_png_bytes"] = png_bytes
            context.user_data["line_bottom"] = line_bottom
            
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟠 Orange", callback_data="recolor:orange"),
                    InlineKeyboardButton("🟣 Magenta", callback_data="recolor:magenta")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            caption_text = f"✅ **Charlie** transparent text box generated with auto-cycled color!"
            image_buffer.name = "charlie_textbox.png"
            await update.message.reply_document(
                document=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
            await status_msg.delete()
            
            # Ask user for background image/video
            await update.message.reply_text(
                "🖼️ **Would you like to overlay this text box on top of a background image or video?**\n\n"
                "📥 **Send an image or a video now** to blend it,\n"
                "➡️ Or click/type /skip to keep the transparent PNG layout and send new text."
            )
            return STATE_WAITING_BACKGROUND
            
        except Exception as e:
            logger.error(f"Failed to generate/send image in OCR flow for Charlie: {e}")
            await status_msg.edit_text(f"❌ Sorry, an error occurred during rendering: {str(e)}")
            return STATE_WAITING_TEXT
            
    if font_choice in ("maga", "charlie", "maga_charlie", "faith"):
        keyboard = [
            [
                InlineKeyboardButton("🟡 Yellow (#ffde59)", callback_data="color_ocr:yellow"),
                InlineKeyboardButton("🔴 Red (#ff3131)", callback_data="color_ocr:red")
            ],
            [
                InlineKeyboardButton("🟠 Orange (#ff914d)", callback_data="color_ocr:orange"),
                InlineKeyboardButton("🟣 Magenta (#cb6ce6)", callback_data="color_ocr:magenta")
            ],
            [
                InlineKeyboardButton("🟢 Green (#00e676)", callback_data="color_ocr:green"),
                InlineKeyboardButton("🔵 Blue (#38b6ff)", callback_data="color_ocr:blue")
            ],
            [
                InlineKeyboardButton("🟪 Purple (#8c52ff)", callback_data="color_ocr:purple")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("🟡 Yellow (#ffde59)", callback_data="color_ocr:yellow"),
                InlineKeyboardButton("🔴 Red (#ff3131)", callback_data="color_ocr:red")
            ],
            [
                InlineKeyboardButton("🟢 Green (#00e676)", callback_data="color_ocr:green"),
                InlineKeyboardButton("🔵 Blue (#38b6ff)", callback_data="color_ocr:blue")
            ],
            [
                InlineKeyboardButton("🟪 Purple (#8c52ff)", callback_data="color_ocr:purple")
            ]
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📝 **Text confirmed:** `{text}`\n\n"
        "🎨 **Please select the highlight color to generate the text box:**",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return STATE_CONFIRM_IG_OCR

async def color_ocr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Generate image based on OCR text and selected color."""
    query = update.callback_query
    await query.answer()
    
    color_choice = query.data.split(":")[1]
    context.user_data["chosen_color"] = color_choice
    
    text = context.user_data.get("last_text")
    font_choice = context.user_data.get("chosen_font", "faith")
    user_id = update.effective_user.id
    
    if not text:
        await query.message.reply_text("❌ No text found. Please start over by sending your text.")
        return STATE_WAITING_TEXT
        
    status_msg = await query.message.reply_text("🎨 Rendering your text box, please wait...")
    try:
        user_support_image_path = context.user_data.get("user_support_image_path")
        image_buffer, line_bottom = generate_textbox_image(
            text, font_choice, color_choice,
            user_support_image_path=user_support_image_path,
            doge_highlight_lines=context.user_data.get("doge_highlight_lines")
        )
        
        # Save transparent PNG bytes in session
        png_bytes = image_buffer.getvalue()
        context.user_data["transparent_png_bytes"] = png_bytes
        context.user_data["line_bottom"] = line_bottom
        
        color_names = {
            "yellow": "🟡 Yellow",
            "red": "🔴 Red",
            "orange": "🟠 Orange",
            "magenta": "🟣 Magenta",
            "green": "🟢 Green",
            "blue": "🔵 Blue",
            "purple": "🟪 Purple"
        }
        color_name = color_names.get(color_choice, color_choice.capitalize())
        
        if font_choice in ("maga", "charlie", "maga_charlie", "faith"):
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟠 Orange", callback_data="recolor:orange"),
                    InlineKeyboardButton("🟣 Magenta", callback_data="recolor:magenta")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
        else:
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        caption_text = f"✅ **{font_choice.capitalize()}** transparent text box generated!"
        if font_choice in ("maga", "charlie", "faith"):
            image_buffer.name = f"{font_choice}_textbox.png"
            await query.message.reply_document(
                document=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
        else:
            await query.message.reply_photo(
                photo=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
            
        await status_msg.delete()
        
        # Ask user for background image/video
        await query.message.reply_text(
            "🖼️ **Would you like to overlay this text box on top of a background image or video?**\n\n"
            "📥 **Send an image or a video now** to blend it,\n"
            "➡️ Or click/type /skip to keep the transparent PNG layout and send new text."
        )
        return STATE_WAITING_BACKGROUND
        
    except Exception as e:
        logger.error(f"Failed to generate/send image in OCR flow: {e}")
        await status_msg.edit_text(f"❌ Sorry, an error occurred during rendering: {str(e)}")
        return STATE_WAITING_TEXT

async def proofread_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the AI proofread accept/ignore actions."""
    restore_session_if_needed(update, context)
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(":")[1]
    if action == "ignore":
        await query.message.edit_text("❌ Keeping your original text.")
        return STATE_WAITING_TEXT
        
    # Else action == "accept"
    corrected_text = context.user_data.get("suggested_corrected_text")
    if not corrected_text:
        await query.message.edit_text("⚠️ No suggested text found. Please send your text again.")
        return STATE_WAITING_TEXT
        
    # Re-render with corrected text!
    font_choice = context.user_data.get("chosen_font", "faith")
    color_choice = context.user_data.get("chosen_color", "yellow")
    user_support_image_path = context.user_data.get("user_support_image_path")
    
    # Store corrected text as the last text
    context.user_data["last_text"] = corrected_text
    
    status_msg = await query.message.reply_text("🎨 Re-rendering with corrected text...")
    try:
        image_buffer, line_bottom = generate_textbox_image(
            corrected_text, font_choice, color_choice,
            user_support_image_path=user_support_image_path,
            doge_highlight_lines=context.user_data.get("doge_highlight_lines")
        )
        
        # Save transparent PNG bytes in session
        png_bytes = image_buffer.getvalue()
        context.user_data["transparent_png_bytes"] = png_bytes
        context.user_data["line_bottom"] = line_bottom
        
        # Build standard post-render keyboard
        color_names = {
            "yellow": "🟡 Yellow",
            "red": "🔴 Red",
            "orange": "🟠 Orange",
            "magenta": "🟣 Magenta",
            "green": "🟢 Green",
            "blue": "🔵 Blue",
            "purple": "🟪 Purple"
        }
        color_name = color_names.get(color_choice, color_choice.capitalize())
        
        if font_choice in ("maga", "charlie", "maga_charlie", "faith", "doge"):
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟠 Orange", callback_data="recolor:orange"),
                    InlineKeyboardButton("🟣 Magenta", callback_data="recolor:magenta")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
        else:
            keyboard = [
                [
                    InlineKeyboardButton("🟡 Yellow", callback_data="recolor:yellow"),
                    InlineKeyboardButton("🔴 Red", callback_data="recolor:red")
                ],
                [
                    InlineKeyboardButton("🟢 Green", callback_data="recolor:green"),
                    InlineKeyboardButton("🔵 Blue", callback_data="recolor:blue")
                ],
                [
                    InlineKeyboardButton("🟪 Purple", callback_data="recolor:purple")
                ],
                [
                    InlineKeyboardButton("🔄 Change Font", callback_data="changefont")
                ]
            ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        caption_text = f"✅ **{font_choice.capitalize()}** transparent text box generated with corrected text!"
        
        if font_choice in ("maga", "charlie", "faith", "doge"):
            image_buffer.name = f"{font_choice}_textbox.png"
            await query.message.reply_document(
                document=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
        else:
            await query.message.reply_photo(
                photo=image_buffer,
                caption=caption_text,
                reply_markup=reply_markup
            )
            
        await status_msg.delete()
        await query.message.edit_text("✅ Switched to corrected text box successfully!")
        
    except Exception as e:
        logger.error(f"Failed to generate/send image after proofread: {e}")
        await status_msg.edit_text(f"❌ Error during rendering: {str(e)}")
        
    return STATE_WAITING_TEXT

async def background_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download the background image or video and overlay the transparent PNG."""
    restore_session_if_needed(update, context)
    user_id = update.effective_user.id
    png_bytes = context.user_data.get("transparent_png_bytes")
    font_choice = context.user_data.get("chosen_font", "faith")
    color_choice = context.user_data.get("chosen_color", "yellow")
    
    if not png_bytes:
        await update.message.reply_text("❌ No text box generated yet. Please send some text first.")
        return STATE_WAITING_TEXT
        
    status_msg = await update.message.reply_text("📥 Processing media and rendering 3D effect...")
    
    highlight_color = get_highlight_color(color_choice)
        
    shape_type = "square" if font_choice == "maga" else "circle"
    
    try:
        if update.message.photo:
            file_obj = await update.message.photo[-1].get_file()
            bg_path = os.path.join(BASE_DIR, f"user_bg_img_{user_id}.jpg")
            await file_obj.download_to_drive(bg_path)
            context.user_data["temp_bg_path"] = bg_path
            context.user_data["temp_bg_type"] = "image"
            
            # Generate standard preview
            preview_buffer = overlay_png_on_image(
                bg_path, png_bytes, 
                shape_image_path=None, 
                shape_type=shape_type, 
                border_color=highlight_color,
                shape_offset_x=0,
                shape_offset_y=0,
                shape_scale=1.0,
                simple_overlay=True,
                line_bottom=context.user_data.get("line_bottom")
            )
            
            await status_msg.delete()
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Done", callback_data="bg_done")
                ]
            ]
            await update.message.reply_photo(
                photo=preview_buffer,
                caption=(
                    "🖼️ **Standard overlay generated!**\n\n"
                    f"👤 Send the **shape image** (different from the background) to place inside the **{shape_type}** shape behind the subject for a 3D pop-out layout.\n"
                    "➡️ Or click **Done** below to save this standard overlay."
                ),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return STATE_WAITING_SHAPE_IMAGE
            
        elif update.message.video or (update.message.document and update.message.document.mime_type and update.message.document.mime_type.startswith("video/")):
            doc_or_vid = update.message.video or update.message.document
            file_obj = await doc_or_vid.get_file()
            video_ext = "mp4"
            if update.message.document and update.message.document.file_name:
                video_ext = update.message.document.file_name.split(".")[-1]
            elif update.message.video and update.message.video.file_name:
                video_ext = update.message.video.file_name.split(".")[-1]
                
            bg_path = os.path.join(BASE_DIR, f"user_bg_vid_{user_id}.{video_ext}")
            out_path = os.path.join(BASE_DIR, f"user_output_{user_id}.mp4")
            await file_obj.download_to_drive(bg_path)
            
            overlay_png_on_video(bg_path, png_bytes, out_path, line_bottom=context.user_data.get("line_bottom"))
            
            if os.path.exists(bg_path):
                os.remove(bg_path)
                
            with open(out_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption="✅ Done! Standard video overlay generated. Send more text to generate another one!"
                )
            if os.path.exists(out_path):
                os.remove(out_path)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Please send a valid image or video background.")
            return STATE_WAITING_BACKGROUND
            
    except Exception as e:
        logger.error(f"Error rendering background: {e}")
        await status_msg.edit_text(f"❌ Failed to overlay background: {str(e)}")
        
    return STATE_WAITING_TEXT

async def skip_background(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip background overlay and return to text waiting state."""
    restore_session_if_needed(update, context)
    await update.message.reply_text("✅ Transparent PNG layout kept. Send new text to generate another!")
    return STATE_WAITING_TEXT

def shape_position_keyboard(move_step: int = 25, scale_step: float = 0.1, active_shape: int = 1) -> InlineKeyboardMarkup:
    """Returns the inline keyboard for shape position and scale adjustment."""
    keyboard = [
        [
            InlineKeyboardButton("⬆️ Up", callback_data="pos:up")
        ],
        [
            InlineKeyboardButton("⬅️ Left", callback_data="pos:left"),
            InlineKeyboardButton("➡️ Right", callback_data="pos:right")
        ],
        [
            InlineKeyboardButton("⬇️ Down", callback_data="pos:down")
        ],
        [
            InlineKeyboardButton("📍 Snap Left (-275px)", callback_data="pos:snap_left"),
            InlineKeyboardButton("📍 Snap Right (275px)", callback_data="pos:snap_right")
        ],
        [
            InlineKeyboardButton(f"🔄 Move Step: {move_step}px", callback_data="pos:toggle_move_step")
        ],
        [
            InlineKeyboardButton("➖ Scale Down", callback_data="pos:scale_down"),
            InlineKeyboardButton("➕ Scale Up", callback_data="pos:scale_up")
        ],
        [
            InlineKeyboardButton(f"🔄 Scale Step: {scale_step:.2f}x", callback_data="pos:toggle_scale_step")
        ]
    ]
    if active_shape == 1:
        keyboard.append([
            InlineKeyboardButton("➕ Add Second Shape", callback_data="pos:add_second_shape")
        ])
    keyboard.append([
        InlineKeyboardButton("✅ Done", callback_data="pos:done")
    ])
    return InlineKeyboardMarkup(keyboard)

async def update_positioning_ui(query, context: ContextTypes.DEFAULT_TYPE):
    """Re-renders the composite image with current shape parameters and edits the message photo."""
    png_bytes = context.user_data.get("transparent_png_bytes")
    bg_path = context.user_data.get("temp_bg_path")
    shape_path = context.user_data.get("temp_shape_img_path")
    shape2_path = context.user_data.get("temp_shape2_img_path")
    font_choice = context.user_data.get("chosen_font", "faith")
    color_choice = context.user_data.get("chosen_color", "yellow")
    
    offset_x = context.user_data.get("shape_offset_x", -275)
    offset_y = context.user_data.get("shape_offset_y", 0)
    scale = context.user_data.get("shape_scale", 1.0)
    
    offset_x2 = context.user_data.get("shape2_offset_x", 275)
    offset_y2 = context.user_data.get("shape2_offset_y", 0)
    scale2 = context.user_data.get("shape2_scale", 1.0)
    
    move_step = context.user_data.get("shape_move_step", 25)
    scale_step = context.user_data.get("shape_scale_step", 0.1)
    
    highlight_color = get_highlight_color(color_choice)
    shape_type = "square" if font_choice == "maga" else "circle"
    
    combined_buffer = overlay_png_on_image(
        bg_image_path=bg_path,
        png_bytes=png_bytes,
        shape_image_path=shape_path,
        shape_type=shape_type,
        border_color=highlight_color,
        shape_offset_x=offset_x,
        shape_offset_y=offset_y,
        shape_scale=scale,
        shape2_image_path=shape2_path,
        shape2_offset_x=offset_x2,
        shape2_offset_y=offset_y2,
        shape2_scale=scale2,
        line_bottom=context.user_data.get("line_bottom")
    )
    
    active_shape = context.user_data.get("active_shape_editing", 1)
    curr_offset_x = offset_x2 if active_shape == 2 else offset_x
    curr_offset_y = offset_y2 if active_shape == 2 else offset_y
    curr_scale = scale2 if active_shape == 2 else scale
    
    # Send as input media photo to edit in-place
    combined_buffer.name = "repositioning.jpg"
    await query.message.edit_media(
        media=InputMediaPhoto(
            media=combined_buffer,
            caption=(
                f"🎮 **Use the controls below to position Shape {active_shape}:**\n\n"
                f"📍 Offset: ({curr_offset_x}px, {curr_offset_y}px) [Step: {move_step}px]\n"
                f"🔎 Scale: {curr_scale:.2f}x [Step: {scale_step:.2f}x]\n\n"
                "When satisfied, click **Done**!"
            )
        ),
        reply_markup=shape_position_keyboard(move_step=move_step, scale_step=scale_step, active_shape=active_shape)
    )

async def shape_image_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download shape image and show interactive positioning controls."""
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("📥 Processing shape image...")
    
    try:
        file_obj = await update.message.photo[-1].get_file()
        shape_path = os.path.join(BASE_DIR, f"user_shape_img_{user_id}.jpg")
        await file_obj.download_to_drive(shape_path)
        context.user_data["temp_shape_img_path"] = shape_path
        
        # Initialize shape transform parameters
        context.user_data["active_shape_editing"] = 1
        context.user_data["shape_offset_x"] = -275
        context.user_data["shape_offset_y"] = 0
        context.user_data["shape_scale"] = 1.0
        context.user_data["shape_move_step"] = 25
        context.user_data["shape_scale_step"] = 0.1
        
        png_bytes = context.user_data.get("transparent_png_bytes")
        bg_path = context.user_data.get("temp_bg_path")
        font_choice = context.user_data.get("chosen_font", "faith")
        color_choice = context.user_data.get("chosen_color", "yellow")
        
        highlight_color = get_highlight_color(color_choice)
            
        shape_type = "square" if font_choice == "maga" else "circle"
        
        combined_buffer = overlay_png_on_image(
            bg_path, png_bytes, 
            shape_image_path=shape_path, 
            shape_type=shape_type, 
            border_color=highlight_color,
            shape_offset_x=-275,
            shape_offset_y=0,
            shape_scale=1.0,
            line_bottom=context.user_data.get("line_bottom")
        )
        
        await status_msg.delete()
        await update.message.reply_photo(
            photo=combined_buffer,
            caption="🎮 **Use the controls below to position the shape behind the person:**\n\n📍 Offset: (-275px, 0px) [Step: 25px]\n🔎 Scale: 1.00x [Step: 0.10x]",
            reply_markup=shape_position_keyboard(move_step=25, scale_step=0.1, active_shape=1)
        )
        return STATE_POSITIONING_SHAPE
        
    except Exception as e:
        logger.error(f"Failed to process shape image: {e}")
        await status_msg.edit_text(f"❌ Failed to process shape image: {str(e)}")
        return STATE_WAITING_SHAPE_IMAGE

async def shape2_image_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download shape image 2 and show interactive positioning controls opposite to shape 1."""
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("📥 Processing second shape image...")
    
    try:
        file_obj = await update.message.photo[-1].get_file()
        shape2_path = os.path.join(BASE_DIR, f"user_shape2_img_{user_id}.jpg")
        await file_obj.download_to_drive(shape2_path)
        context.user_data["temp_shape2_img_path"] = shape2_path
        
        # Initialize shape 2 editing mode
        context.user_data["active_shape_editing"] = 2
        
        # Set shape 2 opposite to shape 1
        offset_x1 = context.user_data.get("shape_offset_x", -275)
        if offset_x1 < 0:
            context.user_data["shape2_offset_x"] = 275
        else:
            context.user_data["shape2_offset_x"] = -275
            
        context.user_data["shape2_offset_y"] = 0
        context.user_data["shape2_scale"] = 1.0
        
        png_bytes = context.user_data.get("transparent_png_bytes")
        bg_path = context.user_data.get("temp_bg_path")
        shape_path = context.user_data.get("temp_shape_img_path")
        font_choice = context.user_data.get("chosen_font", "faith")
        color_choice = context.user_data.get("chosen_color", "yellow")
        
        highlight_color = get_highlight_color(color_choice)
        shape_type = "square" if font_choice == "maga" else "circle"
        
        combined_buffer = overlay_png_on_image(
            bg_image_path=bg_path,
            png_bytes=png_bytes,
            shape_image_path=shape_path,
            shape_type=shape_type,
            border_color=highlight_color,
            shape_offset_x=offset_x1,
            shape_offset_y=context.user_data.get("shape_offset_y", 0),
            shape_scale=context.user_data.get("shape_scale", 1.0),
            shape2_image_path=shape2_path,
            shape2_offset_x=context.user_data["shape2_offset_x"],
            shape2_offset_y=0,
            shape2_scale=1.0,
            line_bottom=context.user_data.get("line_bottom")
        )
        
        await status_msg.delete()
        
        move_step = context.user_data.get("shape_move_step", 25)
        scale_step = context.user_data.get("shape_scale_step", 0.1)
        
        await update.message.reply_photo(
            photo=combined_buffer,
            caption=(
                "🎮 **Use the controls below to position Shape 2:**\n\n"
                f"📍 Offset: ({context.user_data['shape2_offset_x']}px, 0px) [Step: {move_step}px]\n"
                f"🔎 Scale: 1.00x [Step: {scale_step:.2f}x]\n\n"
                "When satisfied, click **Done**!"
            ),
            reply_markup=shape_position_keyboard(move_step=move_step, scale_step=scale_step, active_shape=2)
        )
        return STATE_POSITIONING_SHAPE
        
    except Exception as e:
        logger.error(f"Failed to process second shape image: {e}")
        await status_msg.edit_text(f"❌ Failed to process second shape image: {str(e)}")
        return STATE_POSITIONING_SHAPE

async def skip_shape_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip shape/cutout (standard overlay)."""
    restore_session_if_needed(update, context)
    user_id = update.effective_user.id
    png_bytes = context.user_data.get("transparent_png_bytes")
    bg_path = context.user_data.get("temp_bg_path")
    font_choice = context.user_data.get("chosen_font", "faith")
    color_choice = context.user_data.get("chosen_color", "yellow")
    
    highlight_color = get_highlight_color(color_choice)
        
    shape_type = "square" if font_choice == "maga" else "circle"
    
    query = update.callback_query
    if query:
        await query.answer()
        # Clean up files
        context.user_data.pop("temp_bg_path", None)
        context.user_data.pop("temp_shape_img_path", None)
        if bg_path and os.path.exists(bg_path):
            os.remove(bg_path)
            
        await query.message.edit_caption(
            caption="✅ Done! Standard image overlay generated. Send new text to generate another one!",
            reply_markup=None
        )
        return STATE_WAITING_TEXT
        
    # If they typed /skip or /done
    status_msg = await update.message.reply_text("📥 Rendering standard image overlay...")
    
    try:
        combined_buffer = overlay_png_on_image(
            bg_path, png_bytes, 
            shape_image_path=None, 
            shape_type=shape_type, 
            border_color=highlight_color,
            shape_offset_x=0,
            shape_offset_y=0,
            shape_scale=1.0,
            simple_overlay=True,
            line_bottom=context.user_data.get("line_bottom")
        )
        
        # Clean up files
        context.user_data.pop("temp_bg_path", None)
        context.user_data.pop("temp_shape_img_path", None)
        if bg_path and os.path.exists(bg_path):
            os.remove(bg_path)
            
        await status_msg.delete()
        await update.message.reply_photo(
            photo=combined_buffer,
            caption="✅ Done! Standard image overlay generated. Send new text to generate another one!"
        )
        return STATE_WAITING_TEXT
        
    except Exception as e:
        logger.error(f"Error rendering standard image overlay: {e}")
        await status_msg.edit_text(f"❌ Failed to generate overlay: {str(e)}")
        return STATE_WAITING_TEXT

async def shape_position_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle callback queries for positioning/scaling buttons."""
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(":")[-1]
    
    active_shape = context.user_data.get("active_shape_editing", 1)
    
    if active_shape == 2:
        offset_x = context.user_data.get("shape2_offset_x", 275)
        offset_y = context.user_data.get("shape2_offset_y", 0)
        scale = context.user_data.get("shape2_scale", 1.0)
        prefix = "shape2_"
    else:
        offset_x = context.user_data.get("shape_offset_x", -275)
        offset_y = context.user_data.get("shape_offset_y", 0)
        scale = context.user_data.get("shape_scale", 1.0)
        prefix = "shape_"
        
    move_step = context.user_data.get("shape_move_step", 25)
    scale_step = context.user_data.get("shape_scale_step", 0.1)
    
    if action == "up":
        context.user_data[prefix + "offset_y"] = offset_y - move_step
    elif action == "down":
        context.user_data[prefix + "offset_y"] = offset_y + move_step
    elif action == "left":
        context.user_data[prefix + "offset_x"] = offset_x - move_step
    elif action == "right":
        context.user_data[prefix + "offset_x"] = offset_x + move_step
    elif action == "snap_left":
        context.user_data[prefix + "offset_x"] = -275
    elif action == "snap_right":
        context.user_data[prefix + "offset_x"] = 275
    elif action == "scale_up":
        context.user_data[prefix + "scale"] = min(2.5, scale + scale_step)
    elif action == "scale_down":
        context.user_data[prefix + "scale"] = max(0.4, scale - scale_step)
    elif action == "toggle_move_step":
        steps = [5, 10, 25, 50, 100]
        next_idx = (steps.index(move_step) + 1) % len(steps)
        context.user_data["shape_move_step"] = steps[next_idx]
    elif action == "toggle_scale_step":
        steps = [0.02, 0.05, 0.1, 0.2, 0.5]
        next_idx = (steps.index(scale_step) + 1) % len(steps)
        context.user_data["shape_scale_step"] = steps[next_idx]
    elif action == "add_second_shape":
        await query.message.reply_text(
            "📥 **Please send/upload the second shape image now.**\n\n"
            "The bot will automatically place it on the opposite side of your first shape."
        )
        return STATE_WAITING_SHAPE2_IMAGE
    elif action == "done":
        bg_path = context.user_data.pop("temp_bg_path", None)
        shape_path = context.user_data.pop("temp_shape_img_path", None)
        shape2_path = context.user_data.pop("temp_shape2_img_path", None)
        context.user_data.pop("shape_move_step", None)
        context.user_data.pop("shape_scale_step", None)
        context.user_data.pop("active_shape_editing", None)
        context.user_data.pop("shape_offset_x", None)
        context.user_data.pop("shape_offset_y", None)
        context.user_data.pop("shape_scale", None)
        context.user_data.pop("shape2_offset_x", None)
        context.user_data.pop("shape2_offset_y", None)
        context.user_data.pop("shape2_scale", None)
        
        if bg_path and os.path.exists(bg_path):
            os.remove(bg_path)
        if shape_path and os.path.exists(shape_path):
            os.remove(shape_path)
        if shape2_path and os.path.exists(shape2_path):
            os.remove(shape2_path)
            
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Done! Final pop-out layout completed. Send new text to generate another one!")
        return STATE_WAITING_TEXT
        
    await update_positioning_ui(query, context)
    return STATE_POSITIONING_SHAPE

async def instagram_background_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle choice of using the fetched Instagram media as background."""
    restore_session_if_needed(update, context)
    query = update.callback_query
    await query.answer()
    
    action = query.data.split(":")[-1]
    user_id = update.effective_user.id
    png_bytes = context.user_data.get("transparent_png_bytes")
    font_choice = context.user_data.get("chosen_font", "faith")
    color_choice = context.user_data.get("chosen_color", "yellow")
    
    if action == "skip":
        await query.message.edit_text("✅ Transparent PNG layout kept. Send new text to generate another!")
        context.user_data.pop("instagram_media_path", None)
        context.user_data.pop("instagram_is_video", None)
        return STATE_WAITING_TEXT
        
    if action == "upload":
        await query.message.edit_text(
            "📥 Please send a new background image or video now."
        )
        return STATE_WAITING_BACKGROUND
        
    # action == "use"
    media_path = context.user_data.get("instagram_media_path")
    is_video = context.user_data.get("instagram_is_video", False)
    
    if not media_path or not os.path.exists(media_path):
        await query.message.edit_text("❌ Instagram background media not found. Please upload a new background.")
        return STATE_WAITING_BACKGROUND
        
    status_msg = await query.message.reply_text("📥 Processing background and rendering...")
    
    highlight_color = get_highlight_color(color_choice)
        
    shape_type = "square" if font_choice == "maga" else "circle"
    
    try:
        if not is_video:
            # It's an image. Setup temp_bg_path and transition to waiting for shape image state!
            context.user_data["temp_bg_path"] = media_path
            context.user_data["temp_bg_type"] = "image"
            
            # Generate standard preview
            preview_buffer = overlay_png_on_image(
                media_path, png_bytes, 
                shape_image_path=None, 
                shape_type=shape_type, 
                border_color=highlight_color,
                shape_offset_x=0,
                shape_offset_y=0,
                shape_scale=1.0,
                simple_overlay=True,
                line_bottom=context.user_data.get("line_bottom")
            )
            
            await status_msg.delete()
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Done", callback_data="bg_done")
                ]
            ]
            await query.message.reply_photo(
                photo=preview_buffer,
                caption=(
                    "🖼️ **Standard overlay generated!**\n\n"
                    f"👤 Send the **shape image** (different from the background) to place inside the **{shape_type}** shape behind the subject for a 3D pop-out layout.\n"
                    "➡️ Or click **Done** below to save this standard overlay."
                ),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return STATE_WAITING_SHAPE_IMAGE
        else:
            # Video overlay (directly process and return to text waiting state)
            out_path = os.path.join(BASE_DIR, f"user_output_{user_id}.mp4")
            overlay_png_on_video(media_path, png_bytes, out_path, line_bottom=context.user_data.get("line_bottom"))
            
            if os.path.exists(media_path):
                os.remove(media_path)
                
            with open(out_path, "rb") as f:
                await query.message.reply_video(
                    video=f,
                    caption="✅ Done! Standard video overlay generated. Send more text to generate another one!"
                )
            if os.path.exists(out_path):
                os.remove(out_path)
                
            context.user_data.pop("instagram_media_path", None)
            context.user_data.pop("instagram_is_video", None)
            await status_msg.delete()
            
    except Exception as e:
        logger.error(f"Error rendering Instagram background: {e}")
        await status_msg.edit_text(f"❌ Failed to overlay Instagram background: {str(e)}")
        
    return STATE_WAITING_TEXT

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Restarts the bot process."""
    await update.message.reply_text("🔄 Restarting the bot, please wait...")
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel conversation flow."""
    await update.message.reply_text("Cancelled. Send /start to begin again.")
    context.user_data.clear()
    return ConversationHandler.END

def run_test_renders():
    """Generates standard test renders for visual validation."""
    logger.info("Starting test renders...")
    download_fonts()
    
    test_cases = [
        {
            "name": "test_faith_6line.png",
            "font": "faith",
            "color": "yellow",
            "text": "MY FAMILY AND EVERYONE I\nKNOW IN PAKISTAN NO\nLONGER WANTS TO COME TO\nAMERICA THANKS TO TRUMP.\nPAKISTANIS ARE STAYING\nHOME!'- WAJAHAT ALI"
        },
        {
            "name": "test_charlie_6line.png",
            "font": "charlie",
            "color": "orange",
            "text": "MY FAMILY AND EVERYONE I\nKNOW IN PAKISTAN NO\nLONGER WANTS TO COME TO\nAMERICA THANKS TO TRUMP.\nPAKISTANIS ARE STAYING\nHOME!'- WAJAHAT ALI"
        },
        {
            "name": "test_maga_6line.png",
            "font": "maga",
            "color": "orange",
            "text": "MY FAMILY AND EVERYONE I\nKNOW IN PAKISTAN NO\nLONGER WANTS TO COME TO\nAMERICA THANKS TO TRUMP.\nPAKISTANIS ARE STAYING\nHOME!'- WAJAHAT ALI"
        },
        {
            "name": "test_doge_6line.png",
            "font": "doge",
            "color": "yellow",
            "text": "MY FAMILY AND EVERYONE I\nKNOW IN PAKISTAN NO\nLONGER WANTS TO COME TO\nAMERICA THANKS TO TRUMP.\nPAKISTANIS ARE STAYING\nHOME!'- WAJAHAT ALI"
        }
    ]
    
    for tc in test_cases:
        buf, _ = generate_textbox_image(tc["text"], tc["font"], tc["color"])
        out_path = os.path.join(BASE_DIR, tc["name"])
        with open(out_path, "wb") as f:
            f.write(buf.read())
        logger.info(f"Saved test render to: {out_path}")
        
    logger.info("Test renders complete! You can view the saved images.")

def main():
    parser = argparse.ArgumentParser(description="TextBox Telegram Bot")
    parser.add_argument("--test-render", action="store_true", help="Generate test rendering images and exit")
    args = parser.parse_args()
    
    if args.test_render:
        run_test_renders()
        sys.exit(0)
        
    # Standard bot run
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set in .env")
        sys.exit(1)
        
    # Download fonts at startup
    download_fonts()
    
    application = Application.builder().token(token).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("clear_image", clear_image),
            MessageHandler(filters.TEXT & ~filters.COMMAND, text_received)
        ],
        states={
            STATE_CHOOSING_FONT: [
                CallbackQueryHandler(font_selected, pattern="^font:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_received)
            ],
            STATE_CHOOSING_COLOR: [
                CallbackQueryHandler(color_selected, pattern="^color:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_received)
            ],
            STATE_WAITING_TEXT: [
                CommandHandler("clear_image", clear_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_received),
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, image_received),
                CallbackQueryHandler(change_color, pattern="^recolor:"),
                CallbackQueryHandler(change_font, pattern="^changefont$"),
                CallbackQueryHandler(proofread_callback, pattern="^proofread:"),
            ],
            STATE_WAITING_BACKGROUND: [
                CommandHandler("skip", skip_background),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex("^/skip$"), skip_background),
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, background_received),
                CallbackQueryHandler(instagram_background_callback, pattern="^ig_bg:"),
                CallbackQueryHandler(change_color, pattern="^recolor:"),
                CallbackQueryHandler(change_font, pattern="^changefont$"),
                CallbackQueryHandler(proofread_callback, pattern="^proofread:"),
            ],
            STATE_WAITING_SHAPE_IMAGE: [
                CommandHandler("skip", skip_shape_image),
                CommandHandler("done", skip_shape_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex("^(?:/skip|/done|Done|done|Skip)$"), skip_shape_image),
                CallbackQueryHandler(skip_shape_image, pattern="^bg_done$"),
                MessageHandler(filters.PHOTO, shape_image_received),
                CallbackQueryHandler(change_color, pattern="^recolor:"),
                CallbackQueryHandler(change_font, pattern="^changefont$"),
                CallbackQueryHandler(proofread_callback, pattern="^proofread:"),
            ],
            STATE_POSITIONING_SHAPE: [
                CallbackQueryHandler(shape_position_callback, pattern="^pos:")
            ],
            STATE_CONFIRM_IG_OCR: [
                CallbackQueryHandler(ig_ocr_callback, pattern="^ig_ocr:"),
                CallbackQueryHandler(color_ocr_callback, pattern="^color_ocr:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ig_ocr_text_edited)
            ],
            STATE_WAITING_SHAPE2_IMAGE: [
                MessageHandler(filters.PHOTO, shape2_image_received),
                CallbackQueryHandler(shape_position_callback, pattern="^pos:")
            ],
            STATE_WAITING_DOGE_HIGHLIGHT_LINES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, doge_highlight_lines_received),
                CallbackQueryHandler(change_color, pattern="^recolor:"),
                CallbackQueryHandler(change_font, pattern="^changefont$"),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("restart", restart)],
        per_message=False,
    )
    
    application.add_handler(conv_handler)
    
    # Start a dummy web server on port 7860/PORT for Render/Hugging Face startup requirements
    import threading
    from http.server import SimpleHTTPRequestHandler, HTTPServer
    
    def run_web_server():
        port = int(os.environ.get("PORT", 7860))
        class StatusHandler(SimpleHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Textbox Telegram Bot is running!</h1></body></html>")
            def log_message(self, format, *args):
                # Silence standard GET request logging to keep Render logs clean
                pass
        try:
            server = HTTPServer(("0.0.0.0", port), StatusHandler)
            logger.info(f"Starting health status HTTP web server on port {port}...")
            server.serve_forever()
        except Exception as ex:
            logger.error(f"Web server failed to start: {ex}")
            
    threading.Thread(target=run_web_server, daemon=True).start()
    
    logger.info("Starting Textbox Bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
