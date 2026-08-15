import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import os
import subprocess
import shutil

# --- Configuration ---
CANVAS_COLOR = (0, 0, 0)  # Black canvas
FONT_PATH = 'Anton.ttf'
FONT_SIZE = 48
TEXT_COLOR = (255, 255, 255)  # White text
TEXT_TOP_MARGIN = 20  # 20px above video
HORIZONTAL_MARGIN_PERCENT = 10  # For text wrapping
PORTRAIT_VIDEO_WIDTH_PERCENT = 70  # Portrait/square videos use 70% canvas width

def get_wrapped_lines(text, font, max_width):
    """Wrap text to fit within max_width."""
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    words = text.split()
    if not words:
        return []
    
    lines = []
    current_line = words[0]
    
    for word in words[1:]:
        test_line = f"{current_line} {word}"
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if bbox[2] <= max_width:
            current_line = test_line
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)
    return lines

def draw_centered_text(draw, lines, font, text_color, canvas_width, text_bottom_y):
    """Draw wrapped text centered horizontally, ending at text_bottom_y."""
    line_height = font.size + 5
    total_text_height = len(lines) * line_height
    current_y = text_bottom_y - total_text_height
    
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2]
        current_x = (canvas_width - line_width) / 2
        draw.text((current_x, current_y), line, font=font, fill=text_color)
        current_y += line_height

def has_audio_stream(video_path):
    """Check if video has audio using ffprobe."""
    if not shutil.which("ffprobe"):
        print("Warning: ffprobe not found. Assuming video has audio.")
        return True
    
    command = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_type", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        video_path
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return len(result.stdout.strip()) > 0
    except subprocess.CalledProcessError:
        return False

def process_video(input_path, output_path, sample_text, canvas_width=1080, canvas_height=1920):
    """
    Process video: place on black canvas with text above.
    
    Args:
        input_path: Path to input video file
        output_path: Path to save output video
        sample_text: Text to display above video
        canvas_width: Width of output canvas (default 1080)
        canvas_height: Height of output canvas (default 1920)
    """
    
    # Check for FFmpeg
    if not shutil.which("ffmpeg"):
        print("\n" + "="*50)
        print("ERROR: FFmpeg not found.")
        print("Please install FFmpeg and add it to your system's PATH.")
        print("="*50)
        return False
    
    # Check font file
    if not os.path.exists(FONT_PATH):
        print(f"Error: Font file not found: {FONT_PATH}")
        return False
    
    # Open input video
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        print(f"Error: Could not open video: {input_path}")
        return False
    
    video_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    video_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    print(f"Input video: {video_width}x{video_height}, {fps} fps, {total_frames} frames")
    
    # Calculate video placement
    video_aspect = video_width / video_height
    
    if video_aspect <= 1.0:
        # Portrait or square video: use PORTRAIT_VIDEO_WIDTH_PERCENT, center vertically
        target_width = int(canvas_width * (PORTRAIT_VIDEO_WIDTH_PERCENT / 100))
        scale = target_width / video_width
        scaled_width = target_width
        scaled_height = int(video_height * scale)
        video_x = (canvas_width - scaled_width) // 2  # Center horizontally
        video_y = (canvas_height - scaled_height) // 2  # Center vertically
    else:
        # Landscape video: width 100%
        scale = canvas_width / video_width
        scaled_width = canvas_width
        scaled_height = int(video_height * scale)
        video_x = 0
        video_y = (canvas_height - scaled_height) // 2
    
    print(f"Scaled video: {scaled_width}x{scaled_height} at position ({video_x}, {video_y})")
    
    # Prepare text rendering
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    margin_px = int(canvas_width * (HORIZONTAL_MARGIN_PERCENT / 100))
    max_text_width = canvas_width - (2 * margin_px)
    wrapped_lines = get_wrapped_lines(sample_text, font, max_text_width)
    text_bottom_y = video_y - TEXT_TOP_MARGIN
    
    print(f"Text: {len(wrapped_lines)} line(s)")
    
    # Process video frames
    temp_video_path = "temp_silent_video.mp4"
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(temp_video_path, fourcc, fps, (canvas_width, canvas_height))
    
    for frame_count in range(1, total_frames + 1):
        ret, frame = cap.read()
        if not ret:
            break
        
        # Resize video frame
        resized_frame = cv2.resize(frame, (scaled_width, scaled_height))
        
        # Create black canvas
        canvas_np = np.full((canvas_height, canvas_width, 3), 
                           tuple(reversed(CANVAS_COLOR)), dtype=np.uint8)
        
        # Place video on canvas (handle overflow)
        if video_y < 0:
            # Video taller than canvas, crop top/bottom
            crop_top = abs(video_y)
            crop_bottom = scaled_height - crop_top
            if crop_bottom > canvas_height:
                crop_bottom = canvas_height
            canvas_np[0:crop_bottom, video_x:video_x+scaled_width] = \
                resized_frame[crop_top:crop_top+crop_bottom, :]
        else:
            # Video fits or is smaller than canvas
            end_y = min(video_y + scaled_height, canvas_height)
            canvas_np[video_y:end_y, video_x:video_x+scaled_width] = \
                resized_frame[0:end_y-video_y, :]
        
        # Add text
        canvas_pil = Image.fromarray(cv2.cvtColor(canvas_np, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(canvas_pil)
        draw_centered_text(draw, wrapped_lines, font, TEXT_COLOR, 
                          canvas_width, text_bottom_y)
        
        # Convert back and write
        final_frame = cv2.cvtColor(np.array(canvas_pil), cv2.COLOR_RGB2BGR)
        out.write(final_frame)
        
        print(f"Processing: {frame_count}/{total_frames}", end='\r')
    
    print("\nVideo processing complete. Adding audio...")
    cap.release()
    out.release()
    
    # Merge audio with FFmpeg
    try:
        if has_audio_stream(input_path):
            print("Merging audio from original video...")
            command = [
                "ffmpeg", "-i", temp_video_path, "-i", input_path,
                "-c:v", "copy", "-c:a", "aac",
                "-map", "0:v:0", "-map", "1:a:0",
                "-y", output_path
            ]
            subprocess.run(command, check=True, capture_output=True)
        else:
            print("No audio found. Saving silent video.")
            shutil.move(temp_video_path, output_path)
    except subprocess.CalledProcessError as e:
        print(f"\nError during audio merge: {e.stderr.decode()}")
        shutil.move(temp_video_path, output_path)
        return False
    finally:
        if os.path.exists(temp_video_path):
            os.remove(temp_video_path)
    
    print(f"\nComplete! Output saved to: {output_path}")
    return True


# --- Example Usage ---
if __name__ == "__main__":
    input_video = "Samay_cropped.mp4"  # Your cropped video
    output_video = "output.mp4"
    text = "This is sample text that will appear above the video"
    
    # For portrait/square videos on 9:16 canvas
    process_video(input_video, output_video, text, 
                  canvas_width=1080, canvas_height=1920)
    
    # For other canvas sizes, adjust parameters:
    # process_video(input_video, output_video, text, 
    #               canvas_width=1920, canvas_height=1080)