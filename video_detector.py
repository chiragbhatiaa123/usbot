import cv2
import numpy as np
from pathlib import Path
from skimage.metrics import structural_similarity as ssim
from scipy import ndimage
import subprocess
import tempfile
import shutil

def detect_corner_artifacts(frame, bbox, corner_sample_size=50):
    """
    Detect if corners have artifacts (curved white edges, rounded borders).
    Returns True if artifacts detected, along with recommended inset.
    """
    x, y, w, h = bbox['x'], bbox['y'], bbox['w'], bbox['h']
    crop = frame[y:y+h, x:x+w]
    
    if crop.size == 0:
        return False, 0
    
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    crop_h, crop_w = gray.shape
    
    # Sample corners
    sample_size = min(corner_sample_size, crop_h // 4, crop_w // 4)
    if sample_size < 10:
        return False, 0
    
    corners = {
        'tl': gray[0:sample_size, 0:sample_size],
        'tr': gray[0:sample_size, crop_w-sample_size:crop_w],
        'bl': gray[crop_h-sample_size:crop_h, 0:sample_size],
        'br': gray[crop_h-sample_size:crop_h, crop_w-sample_size:crop_w]
    }
    
    # Check if corners are brighter/darker than center
    center = gray[crop_h//4:3*crop_h//4, crop_w//4:3*crop_w//4]
    center_median = np.median(center)
    
    artifact_count = 0
    max_diff = 0
    
    for corner_name, corner in corners.items():
        corner_median = np.median(corner)
        diff = abs(corner_median - center_median)
        max_diff = max(max_diff, diff)
        
        # If corner is significantly different from center (likely artifact)
        if diff > 30:  # Threshold for brightness difference
            artifact_count += 1
    
    # If 2+ corners have artifacts, likely rounded template
    has_artifacts = artifact_count >= 2
    
    # Recommend inset based on severity
    if max_diff > 80:
        recommended_inset = 12
    elif max_diff > 50:
        recommended_inset = 10
    elif max_diff > 30:
        recommended_inset = 8
    else:
        recommended_inset = 5
    
    return has_artifacts, recommended_inset


def aggressive_trim_uniform_borders(frame, bbox, max_trim_px=0, uniformity_threshold=3):
    """
    Aggressively trim thin uniform borders (like 1-2px white edges).
    Now checks SUBSECTIONS of edges to catch curved borders.
    """
    x, y, w, h = bbox['x'], bbox['y'], bbox['w'], bbox['h']
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    
    # Crop to current bbox
    crop = gray[y:y+h, x:x+w]
    if crop.size == 0:
        return bbox
    
    crop_h, crop_w = crop.shape
    trim = {'top': 0, 'bottom': 0, 'left': 0, 'right': 0}
    
    # Helper function to check if edge segment is uniform
    def is_edge_uniform(pixels, threshold):
        return np.std(pixels) < threshold
    
    # Trim top - check multiple segments
    for i in range(min(max_trim_px, crop_h)):
        row = crop[i, :]
        # Check if at least 60% of the row is uniform
        segment_size = len(row) // 5
        uniform_segments = 0
        for j in range(5):
            start = j * segment_size
            end = (j + 1) * segment_size if j < 4 else len(row)
            if is_edge_uniform(row[start:end], uniformity_threshold):
                uniform_segments += 1
        
        if uniform_segments >= 3:  # At least 60% uniform
            trim['top'] = i + 1
        else:
            break
    
    # Trim bottom
    for i in range(min(max_trim_px, crop_h)):
        row = crop[crop_h - 1 - i, :]
        segment_size = len(row) // 5
        uniform_segments = 0
        for j in range(5):
            start = j * segment_size
            end = (j + 1) * segment_size if j < 4 else len(row)
            if is_edge_uniform(row[start:end], uniformity_threshold):
                uniform_segments += 1
        
        if uniform_segments >= 3:
            trim['bottom'] = i + 1
        else:
            break
    
    # Trim left
    for i in range(min(max_trim_px, crop_w)):
        col = crop[:, i]
        segment_size = len(col) // 5
        uniform_segments = 0
        for j in range(5):
            start = j * segment_size
            end = (j + 1) * segment_size if j < 4 else len(col)
            if is_edge_uniform(col[start:end], uniformity_threshold):
                uniform_segments += 1
        
        if uniform_segments >= 3:
            trim['left'] = i + 1
        else:
            break
    
    # Trim right
    for i in range(min(max_trim_px, crop_w)):
        col = crop[:, crop_w - 1 - i]
        segment_size = len(col) // 5
        uniform_segments = 0
        for j in range(5):
            start = j * segment_size
            end = (j + 1) * segment_size if j < 4 else len(col)
            if is_edge_uniform(col[start:end], uniformity_threshold):
                uniform_segments += 1
        
        if uniform_segments >= 3:
            trim['right'] = i + 1
        else:
            break
    
    # Apply trim
    new_x = x + trim['left']
    new_y = y + trim['top']
    new_w = w - trim['left'] - trim['right']
    new_h = h - trim['top'] - trim['bottom']
    
    # Ensure valid dimensions
    if new_w < 50 or new_h < 50:
        return bbox
    
    return {
        'x': int(new_x),
        'y': int(new_y),
        'w': int(new_w),
        'h': int(new_h)
    }


def apply_corner_inset(bbox, inset_px=8):
    """
    Inset bbox by specified pixels to avoid rounded corner artifacts.
    
    Args:
        bbox: Current bounding box
        inset_px: Pixels to inset from each edge
    
    Returns:
        Updated bbox with inset applied
    """
    new_w = bbox['w'] - (2 * inset_px)
    new_h = bbox['h'] - (2 * inset_px)
    
    # Ensure minimum dimensions
    if new_w < 50 or new_h < 50:
        print(f"Warning: Inset would make bbox too small, skipping corner inset")
        return bbox
    
    return {
        'x': bbox['x'] + inset_px,
        'y': bbox['y'] + inset_px,
        'w': new_w,
        'h': new_h
    }


def clamp_bbox(bbox, frame_w, frame_h, min_size=10):
    """Clamp bbox to valid frame coordinates and ensure non-zero size."""
    x = max(0, min(int(bbox['x']), frame_w - 1))
    y = max(0, min(int(bbox['y']), frame_h - 1))
    max_w = frame_w - x
    max_h = frame_h - y
    w = max(min_size, min(max_w, int(bbox['w'])))
    h = max(min_size, min(max_h, int(bbox['h'])))
    return {'x': x, 'y': y, 'w': w, 'h': h}


def apply_adaptive_corner_inset(bbox, inset_px, frame_w, frame_h):
    """
    Try increasingly less aggressive insets until bbox remains valid.
    Returns (bbox, applied_inset).
    """
    if inset_px <= 0:
        return clamp_bbox(bbox, frame_w, frame_h), 0

    attempts = [
        inset_px,
        int(round(inset_px * 0.75)),
        int(round(inset_px * 0.5)),
        int(round(inset_px * 0.25)),
        0,
    ]
    seen = set()
    for attempt in attempts:
        if attempt in seen:
            continue
        seen.add(attempt)
        cand = dict(bbox)
        if attempt > 0:
            cand = apply_corner_inset(cand, inset_px=attempt)
        cand = clamp_bbox(cand, frame_w, frame_h)
        if cand['w'] > 1 and cand['h'] > 1:
            if attempt != inset_px:
                print(f"[adaptive inset] initial {inset_px}px failed, using {attempt}px")
            return cand, attempt

    print("[adaptive inset] all inset attempts failed, falling back to clamped bbox")
    return clamp_bbox(bbox, frame_w, frame_h), 0


def detect_uniform_borders(frame, threshold=5):
    """
    Detect and return crop bounds to remove uniform (static) borders of any color.
    Returns (top, bottom, left, right) - the indices to crop to.
    """
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
    
    def is_uniform_row(row_data):
        """Check if a row is uniform (low variance)"""
        return np.std(row_data) < threshold
    
    def is_uniform_col(col_data):
        """Check if a column is uniform (low variance)"""
        return np.std(col_data) < threshold
    
    # Scan from top
    top = 0
    for i in range(h):
        if not is_uniform_row(gray[i, :]):
            top = i
            break
    
    # Scan from bottom
    bottom = h
    for i in range(h - 1, -1, -1):
        if not is_uniform_row(gray[i, :]):
            bottom = i + 1
            break
    
    # Scan from left
    left = 0
    for i in range(w):
        if not is_uniform_col(gray[:, i]):
            left = i
            break
    
    # Scan from right
    right = w
    for i in range(w - 1, -1, -1):
        if not is_uniform_col(gray[:, i]):
            right = i + 1
            break
    
    return top, bottom, left, right


def refine_bbox_with_content_analysis(sampled_frames, initial_bbox, original_width, original_height):
    """
    Refine the bounding box by analyzing actual content across multiple frames.
    Ensures the box contains ONLY dynamic content, no static borders.
    Returns a consistent rectangular bbox.
    """
    x, y, w, h = initial_bbox['x'], initial_bbox['y'], initial_bbox['w'], initial_bbox['h']
    
    # Extract the region from multiple frames
    crops = []
    for i in range(0, len(sampled_frames), max(1, len(sampled_frames) // 10)):
        frame = sampled_frames[i]
        crop = frame[y:y+h, x:x+w]
        crops.append(crop)
    
    if len(crops) == 0:
        return initial_bbox
    
    # Find the tightest consistent bounds across all sampled crops
    top_bounds = []
    bottom_bounds = []
    left_bounds = []
    right_bounds = []
    
    for crop in crops:
        if crop.size == 0:
            continue
        top, bottom, left, right = detect_uniform_borders(crop, threshold=10)
        top_bounds.append(top)
        bottom_bounds.append(bottom)
        left_bounds.append(left)
        right_bounds.append(right)
    
    if not top_bounds:
        return initial_bbox
    
    # Use conservative estimates (tightest crop that appears in most frames)
    # We want to ensure NO static border pixels are included
    final_top = int(np.percentile(top_bounds, 75))  # Be conservative
    final_bottom = int(np.percentile(bottom_bounds, 25))  # Be conservative
    final_left = int(np.percentile(left_bounds, 75))  # Be conservative
    final_right = int(np.percentile(right_bounds, 25))  # Be conservative
    
    # Apply to original bbox coordinates
    refined_x = x + final_left
    refined_y = y + final_top
    refined_w = final_right - final_left
    refined_h = final_bottom - final_top
    
    # Safety checks
    refined_x = max(0, refined_x)
    refined_y = max(0, refined_y)
    refined_w = min(refined_w, original_width - refined_x)
    refined_h = min(refined_h, original_height - refined_y)
    
    # Ensure minimum size
    if refined_w < 50 or refined_h < 50:
        print("Warning: Refined bbox too small, using initial bbox")
        return initial_bbox
    
    return {
        'x': int(refined_x),
        'y': int(refined_y),
        'w': int(refined_w),
        'h': int(refined_h)
    }


def remove_background_border(frame, bbox, background_thresh=250):
    """Remove light background borders by adjusting bbox"""
    x, y, w, h = bbox['x'], bbox['y'], bbox['w'], bbox['h']
    # Defend against out-of-bounds and minimum size
    while h > 1 and np.all(frame[y, x:x+w] > background_thresh):     # Top edge
        y += 1
        h -= 1
    while h > 1 and np.all(frame[y+h-1, x:x+w] > background_thresh): # Bottom edge
        h -= 1
    while w > 1 and np.all(frame[y:y+h, x] > background_thresh):     # Left edge
        x += 1
        w -= 1
    while w > 1 and np.all(frame[y:y+h, x+w-1] > background_thresh): # Right edge
        w -= 1
    return {'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h)}


def detect_and_crop_video(input_path, output_path, confidence_threshold=0.0, 
                         handle_rounded_corners=True, corner_inset_px=8,
                         aggressive_border_trim=True, border_trim_px=3):
    """
    Detects and extracts inner video from video-in-video format.
    
    Args:
        input_path: Path to input video file
        output_path: Path where cropped video will be saved
        confidence_threshold: Minimum confidence to perform cropping (0-100)
        handle_rounded_corners: If True, insets crop to avoid rounded corner artifacts
        corner_inset_px: Pixels to inset from each edge when handling rounded corners
        aggressive_border_trim: If True, performs aggressive trimming of uniform borders
        border_trim_px: Maximum pixels to trim from each edge during aggressive trim
    
    Returns:
        dict: Detection results with keys:
            - detected: bool
            - confidence: float
            - output: str (path to output file)
            - bbox: dict or None
            - original_dimensions: dict
            - cropped_dimensions: dict or None
    """
    
    # ========================
    # 1. LOAD VIDEO
    # ========================
    cap = cv2.VideoCapture(str(input_path))
    
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {input_path}")
    
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total_frames / fps
    
    print(f"Video: {width}x{height}, {fps:.2f}fps, {duration:.2f}s, {total_frames} frames")
    
    # ========================
    # 2. SAMPLE FRAMES
    # ========================
    # Calculate sampling interval
    if duration < 5:
        sample_interval = 0.25
    elif duration < 30:
        sample_interval = 0.5
    else:
        sample_interval = 1.0
    
    sample_frame_interval = max(1, int(sample_interval * fps))
    
    # Exclude first and last second
    start_frame = int(fps)
    end_frame = total_frames - int(fps)
    
    sampled_frames = []
    sample_indices = []
    
    for frame_idx in range(start_frame, end_frame, sample_frame_interval):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if ret:
            sampled_frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            sample_indices.append(frame_idx)
        
        # Limit to 100 samples max
        if len(sampled_frames) >= 100:
            break
    
    print(f"Sampled {len(sampled_frames)} frames for analysis")
    
    if len(sampled_frames) < 10:
        cap.release()
        print("Not enough frames sampled. Returning original video.")
        shutil.copy(input_path, output_path)
        return {
            'detected': False,
            'confidence': 0.0,
            'output': str(output_path),
            'bbox': None,
            'original_dimensions': {'width': width, 'height': height},
            'cropped_dimensions': None
        }
    
    # ========================
    # 3. TEMPORAL VARIANCE ANALYSIS
    # ========================
    print("Computing temporal variance...")
    
    # Convert to grayscale
    gray_frames = [cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY) for frame in sampled_frames]
    frame_stack = np.stack(gray_frames, axis=-1)
    
    # Compute variance across time
    variance_map = np.var(frame_stack, axis=-1)
    
    # Normalize
    variance_normalized = cv2.normalize(variance_map, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    
    # Smooth
    variance_smoothed = cv2.GaussianBlur(variance_normalized, (5, 5), 0)
    
    # ========================
    # 4. IDENTIFY DYNAMIC REGION
    # ========================
    print("Identifying dynamic region...")
    
    # Threshold at 30th percentile
    threshold = np.percentile(variance_smoothed.flatten(), 30)
    binary_mask = (variance_smoothed > threshold).astype(np.uint8)
    
    # Morphological operations
    kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    binary_mask = cv2.morphologyEx(binary_mask, cv2.MORPH_OPEN, kernel_open)
    binary_mask = cv2.morphologyEx(binary_mask, cv2.MORPH_CLOSE, kernel_close)
    
    # Find connected components
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(binary_mask, connectivity=8)
    
    if num_labels <= 1:  # Only background
        cap.release()
        print("No dynamic region detected. Returning original video.")
        subprocess.run(['cp', str(input_path), str(output_path)], check=True)
        return {
            'detected': False,
            'confidence': 0.0,
            'output': str(output_path),
            'bbox': None,
            'original_dimensions': {'width': width, 'height': height},
            'cropped_dimensions': None
        }
    
    # Analyze regions (skip label 0 which is background)
    regions = []
    total_pixels = height * width
    
    for label_id in range(1, num_labels):
        x, y, w, h, area = stats[label_id]
        
        aspect_ratio = w / h if h > 0 else 0
        coverage = area / total_pixels
        rectangularity = area / (w * h) if (w * h) > 0 else 0
        
        # Score the region
        score = 0.0
        
        # Size score (prefer 30-90% coverage)
        if 0.30 <= coverage <= 0.90:
            score += 30.0 * (1 - abs(coverage - 0.60) / 0.30)
        
        # Aspect ratio score (standard video ratios)
        standard_ratios = [16/9, 9/16, 4/3, 3/4, 1/1, 21/9, 9/21]
        min_ratio_diff = min([abs(aspect_ratio - r) for r in standard_ratios])
        if min_ratio_diff < 0.1:
            score += 30.0
        elif min_ratio_diff < 0.2:
            score += 15.0
        
        # Rectangularity score
        if rectangularity > 0.85:
            score += 20.0 * rectangularity
        
        # Centering score
        region_center_x = x + w / 2
        region_center_y = y + h / 2
        frame_center_x = width / 2
        frame_center_y = height / 2
        
        center_offset_x = abs(region_center_x - frame_center_x) / frame_center_x
        center_offset_y = abs(region_center_y - frame_center_y) / frame_center_y
        
        if center_offset_x < 0.1 and center_offset_y < 0.1:
            score += 20.0
        
        regions.append({
            'bbox': {'x': x, 'y': y, 'w': w, 'h': h},
            'area': area,
            'aspect_ratio': aspect_ratio,
            'coverage': coverage,
            'rectangularity': rectangularity,
            'score': score
        })
    
    if not regions:
        cap.release()
        print("No valid regions found. Returning original video.")
        subprocess.run(['cp', str(input_path), str(output_path)], check=True)
        return {
            'detected': False,
            'confidence': 0.0,
            'output': str(output_path),
            'bbox': None,
            'original_dimensions': {'width': width, 'height': height},
            'cropped_dimensions': None
        }
    
    best_region = max(regions, key=lambda r: r['score'])
    
    # Always use the best region found, no minimum score requirement
    initial_confidence = min(best_region['score'], 100.0)
    bbox = best_region['bbox']
    
    print(f"Initial detection - Confidence: {initial_confidence:.1f}%")
    print(f"Bbox: x={bbox['x']}, y={bbox['y']}, w={bbox['w']}, h={bbox['h']}")
    
    # ========================
    # 5. REFINE BOUNDARY
    # ========================
    print("Refining boundaries...")
    
    mid_frame = sampled_frames[len(sampled_frames) // 2]
    gray_mid = cv2.cvtColor(mid_frame, cv2.COLOR_RGB2GRAY)
    
    # Edge detection on multiple frames
    edge_maps = []
    for i in range(0, len(sampled_frames), 5):
        gray = cv2.cvtColor(sampled_frames[i], cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        edge_maps.append(edges)
    
    edge_persistence = np.mean(edge_maps, axis=0)
    edge_persistent_binary = (edge_persistence > 0.5).astype(np.uint8)
    
    # Refine boundaries by looking for strong edges
    margin = 20
    
    def find_edge_in_region(region, axis):
        density = np.sum(region, axis=axis)
        if len(density) == 0 or np.max(density) == 0:
            return None
        threshold = np.max(density) * 0.4
        peaks = np.where(density > threshold)[0]
        if len(peaks) > 0:
            return peaks[len(peaks) // 2]  # Take median peak
        return None
    
    # Refine top
    y_min = bbox['y']
    search_top = edge_persistent_binary[max(0, y_min - margin):y_min + margin, bbox['x']:bbox['x'] + bbox['w']]
    top_edge = find_edge_in_region(search_top, axis=1)
    if top_edge is not None:
        y_min = max(0, y_min - margin) + top_edge
    
    # Refine bottom
    y_max = bbox['y'] + bbox['h']
    search_bottom = edge_persistent_binary[y_max - margin:min(height, y_max + margin), bbox['x']:bbox['x'] + bbox['w']]
    bottom_edge = find_edge_in_region(search_bottom, axis=1)
    if bottom_edge is not None:
        y_max = (y_max - margin) + bottom_edge
    
    # Refine left
    x_min = bbox['x']
    search_left = edge_persistent_binary[y_min:y_max, max(0, x_min - margin):x_min + margin]
    left_edge = find_edge_in_region(search_left, axis=0)
    if left_edge is not None:
        x_min = max(0, x_min - margin) + left_edge
    
    # Refine right
    x_max = bbox['x'] + bbox['w']
    search_right = edge_persistent_binary[y_min:y_max, x_max - margin:min(width, x_max + margin)]
    right_edge = find_edge_in_region(search_right, axis=0)
    if right_edge is not None:
        x_max = (x_max - margin) + right_edge
    
    # Ensure bounds
    x_min = max(0, x_min)
    y_min = max(0, y_min)
    x_max = min(width, x_max)
    y_max = min(height, y_max)
    
    refined_bbox = {
        'x': int(x_min),
        'y': int(y_min),
        'w': int(x_max - x_min),
        'h': int(y_max - y_min)
    }
    
    print(f"Refined bbox: x={refined_bbox['x']}, y={refined_bbox['y']}, w={refined_bbox['w']}, h={refined_bbox['h']}")
    
    # Remove light background borders
    refined_bbox = remove_background_border(mid_frame, refined_bbox, background_thresh=250)
    print(f"After light border removal: x={refined_bbox['x']}, y={refined_bbox['y']}, w={refined_bbox['w']}, h={refined_bbox['h']}")
    
    # ========================
    # 5.5. CONTENT-BASED REFINEMENT (NEW)
    # ========================
    print("Performing content analysis to remove static borders...")
    refined_bbox = refine_bbox_with_content_analysis(sampled_frames, refined_bbox, width, height)
    print(f"After content analysis: x={refined_bbox['x']}, y={refined_bbox['y']}, w={refined_bbox['w']}, h={refined_bbox['h']}")
    
    # ========================
    # 5.6. FINAL EDGE REFINEMENTS
    # ========================
    
    # First, detect if we have corner artifacts
    has_corner_artifacts, recommended_inset = detect_corner_artifacts(mid_frame, refined_bbox)
    print(f"Corner artifact detection: {has_corner_artifacts}, recommended inset: {recommended_inset}px")
    
    if aggressive_border_trim:
        print(f"Applying aggressive border trim (max {border_trim_px}px)...")
        refined_bbox = aggressive_trim_uniform_borders(mid_frame, refined_bbox, 
                                                       max_trim_px=border_trim_px, 
                                                       uniformity_threshold=1)  # Very sensitive
        print(f"After aggressive trim: x={refined_bbox['x']}, y={refined_bbox['y']}, w={refined_bbox['w']}, h={refined_bbox['h']}")
    
    if handle_rounded_corners:
        inset_to_apply = recommended_inset if has_corner_artifacts else corner_inset_px
        refined_bbox, applied_inset = apply_adaptive_corner_inset(refined_bbox, inset_to_apply, width, height)
        print(f"Applying corner inset ({applied_inset}px)...")
        print(f"After corner inset: x={refined_bbox['x']}, y={refined_bbox['y']}, w={refined_bbox['w']}, h={refined_bbox['h']}")
    
    refined_bbox = clamp_bbox(refined_bbox, width, height)
    print(f"Final bbox: x={refined_bbox['x']}, y={refined_bbox['y']}, w={refined_bbox['w']}, h={refined_bbox['h']}")
    
    # ========================
    # 6. TEMPORAL CONSISTENCY CHECK
    # ========================
    print("Validating temporal consistency...")
    
    first_frame = sampled_frames[0]
    last_frame = sampled_frames[-1]
    mid_frame = sampled_frames[len(sampled_frames) // 2]
    
    ssim_scores = []
    
    # Check top region
    if refined_bbox['y'] > 10:
        first_top = first_frame[0:refined_bbox['y'], :]
        last_top = last_frame[0:refined_bbox['y'], :]
        mid_top = mid_frame[0:refined_bbox['y'], :]
        
        if first_top.size > 0 and last_top.size > 0:
            score1 = ssim(cv2.cvtColor(first_top, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(last_top, cv2.COLOR_RGB2GRAY))
            score2 = ssim(cv2.cvtColor(first_top, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(mid_top, cv2.COLOR_RGB2GRAY))
            ssim_scores.extend([score1, score2])
    
    # Check bottom region
    if refined_bbox['y'] + refined_bbox['h'] < height - 10:
        first_bottom = first_frame[refined_bbox['y'] + refined_bbox['h']:, :]
        last_bottom = last_frame[refined_bbox['y'] + refined_bbox['h']:, :]
        mid_bottom = mid_frame[refined_bbox['y'] + refined_bbox['h']:, :]
        
        if first_bottom.size > 0 and last_bottom.size > 0:
            score1 = ssim(cv2.cvtColor(first_bottom, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(last_bottom, cv2.COLOR_RGB2GRAY))
            score2 = ssim(cv2.cvtColor(first_bottom, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(mid_bottom, cv2.COLOR_RGB2GRAY))
            ssim_scores.extend([score1, score2])
    
    # Check left region
    if refined_bbox['x'] > 10:
        first_left = first_frame[refined_bbox['y']:refined_bbox['y'] + refined_bbox['h'], 0:refined_bbox['x']]
        last_left = last_frame[refined_bbox['y']:refined_bbox['y'] + refined_bbox['h'], 0:refined_bbox['x']]
        mid_left = mid_frame[refined_bbox['y']:refined_bbox['y'] + refined_bbox['h'], 0:refined_bbox['x']]
        
        if first_left.size > 0 and last_left.size > 0:
            score1 = ssim(cv2.cvtColor(first_left, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(last_left, cv2.COLOR_RGB2GRAY))
            score2 = ssim(cv2.cvtColor(first_left, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(mid_left, cv2.COLOR_RGB2GRAY))
            ssim_scores.extend([score1, score2])
    
    # Check right region
    if refined_bbox['x'] + refined_bbox['w'] < width - 10:
        first_right = first_frame[refined_bbox['y']:refined_bbox['y'] + refined_bbox['h'], 
                                  refined_bbox['x'] + refined_bbox['w']:]
        last_right = last_frame[refined_bbox['y']:refined_bbox['y'] + refined_bbox['h'], 
                               refined_bbox['x'] + refined_bbox['w']:]
        mid_right = mid_frame[refined_bbox['y']:refined_bbox['y'] + refined_bbox['h'], 
                             refined_bbox['x'] + refined_bbox['w']:]
        
        if first_right.size > 0 and last_right.size > 0:
            score1 = ssim(cv2.cvtColor(first_right, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(last_right, cv2.COLOR_RGB2GRAY))
            score2 = ssim(cv2.cvtColor(first_right, cv2.COLOR_RGB2GRAY), 
                         cv2.cvtColor(mid_right, cv2.COLOR_RGB2GRAY))
            ssim_scores.extend([score1, score2])
    
    # Calculate consistency boost
    if ssim_scores:
        avg_ssim = np.mean(ssim_scores)
        if avg_ssim > 0.98:
            consistency_boost = 30.0
        elif avg_ssim > 0.95:
            consistency_boost = 20.0
        elif avg_ssim > 0.90:
            consistency_boost = 10.0
        else:
            consistency_boost = 0.0
        print(f"Static region SSIM: {avg_ssim:.4f} -> Boost: {consistency_boost:.1f}%")
    else:
        consistency_boost = 0.0
        print("No static regions to validate")
    
    final_confidence = min(initial_confidence + consistency_boost, 100.0)
    
    print(f"\nFinal confidence: {final_confidence:.1f}%")
    print(f"Cropped dimensions: {refined_bbox['w']}x{refined_bbox['h']}")
    print(f"Aspect ratio: {refined_bbox['w']/refined_bbox['h']:.2f}:1")
    
    # ========================
    # 7. DECISION AND EXTRACTION
    # ========================
    if final_confidence < confidence_threshold:
        cap.release()
        print(f"Confidence ({final_confidence:.1f}%) below threshold ({confidence_threshold}%). Returning original video.")
        shutil.copy(input_path, output_path)
        return {
            'detected': False,
            'confidence': final_confidence,
            'output': str(output_path),
            'bbox': refined_bbox,
            'original_dimensions': {'width': width, 'height': height},
            'cropped_dimensions': None
        }
    
    # ========================
    # 8. EXTRACT VIDEO
    # ========================
    print("Extracting inner video...")
    
    # Reset video
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    
    # Get codec
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    
    # Create temporary output without audio
    temp_output = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
    temp_output_path = temp_output.name
    temp_output.close()
    
    out = cv2.VideoWriter(temp_output_path, fourcc, fps, 
                         (refined_bbox['w'], refined_bbox['h']))
    
    frame_count = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        # SIMPLE RECTANGULAR CROP - NO MASKING, NO PER-FRAME PROCESSING
        # This ensures consistent rectangular output across all frames
        cropped = frame[refined_bbox['y']:refined_bbox['y'] + refined_bbox['h'],
                       refined_bbox['x']:refined_bbox['x'] + refined_bbox['w']]
        
        out.write(cropped)
        frame_count += 1
        
        if frame_count % 100 == 0:
            print(f"Processed {frame_count}/{total_frames} frames")
    
    cap.release()
    out.release()
    
    print(f"Video extraction complete: {frame_count} frames written")
    
    # Final pass through ffmpeg to produce a streaming-friendly MP4
    print("Finalizing cropped video...")
    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-i', temp_output_path,
        '-i', str(input_path),
        '-map', '0:v:0',
        '-map', '1:a?',
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', '18',
        '-profile:v', 'high',
        '-level', '4.1',
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-shortest',
        '-map_metadata', '1',
        str(output_path)
    ]
    temp_path = Path(temp_output_path)
    try:
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        print("FFmpeg re-encode failed. Leaving temporary file for inspection:", temp_output_path)
        raise exc
    else:
        if temp_path.exists():
            temp_path.unlink()
    
    print(f"Successfully extracted video to: {output_path}")
    
    return {
        'detected': True,
        'confidence': final_confidence,
        'output': str(output_path),
        'bbox': refined_bbox,
        'original_dimensions': {'width': width, 'height': height},
        'cropped_dimensions': {'width': refined_bbox['w'], 'height': refined_bbox['h']}
    }


# Example usage
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python video_detector.py <input_video> <output_video> [confidence_threshold]")
        sys.exit(1)
    
    input_video = sys.argv[1]
    output_video = sys.argv[2]
    threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    
    result = detect_and_crop_video(input_video, output_video, threshold)
    
    print("\n" + "="*50)
    print("RESULTS:")
    print("="*50)
    print(f"Detected: {result['detected']}")
    print(f"Confidence: {result['confidence']:.1f}%")
    print(f"Output: {result['output']}")
    if result['bbox']:
        print(f"Bounding box: {result['bbox']}")
    print(f"Original: {result['original_dimensions']}")
    if result['cropped_dimensions']:
        print(f"Cropped: {result['cropped_dimensions']}")
