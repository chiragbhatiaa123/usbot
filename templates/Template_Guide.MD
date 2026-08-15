# Template Engine - New Features Guide

## Overview
This guide covers all the new features added to the template engine without breaking existing functionality.

---

## 1. Text Casing Modes

### Configuration
```json
{
  "top_text": {
    "casing_mode": "none"
  }
}
```

### Available Modes
- **`"none"`** - NEW! Preserves original text exactly as provided
- `"original"` - Same as "none" (default behavior)
- `"upper"`, `"uppercase"`, `"all_caps"`, `"caps"` - CONVERTS TO UPPERCASE
- `"lower"`, `"lowercase"`, `"all_lower"` - converts to lowercase
- `"title"`, `"titlecase"`, `"title_case"` - Converts To Title Case
- `"sentence"`, `"sentencecase"`, `"capitalize"` - Converts to sentence case

---

## 2. Dual Text System

### Top Text
Text positioned ABOVE the video with bottom edge at specified gap.

```json
{
  "top_text": {
    "enabled": true,
    "font": "assets/font-bold.ttf",
    "font_size": 120,
    "min_font_size": 40,
    "max_lines": 2,
    "line_width_pct": 90,
    "line_spacing_factor": 1.35,
    "color": "#ffffff",
    "casing_mode": "uppercase",
    "align": "center",
    "gap_px": 30
  }
}
```

### Bottom Text
Text positioned BELOW the video with top edge at specified gap.

```json
{
  "bottom_text": {
    "enabled": false,
    "font": "assets/font-regular.ttf",
    "font_size": 80,
    "min_font_size": 30,
    "max_lines": 3,
    "line_width_pct": 85,
    "line_spacing_factor": 1.3,
    "color": "#ffffff",
    "casing_mode": "none",
    "align": "left",
    "gap_px": 40
  }
}
```

### Text Alignment Options
- `"center"` - Centered horizontally
- `"left"` - Left-aligned with margin
- `"right"` - Right-aligned with margin

### Key Properties
- **`enabled`**: `true` or `false` to show/hide text block
- **`gap_px`**: Distance in pixels from video edge
- **`align`**: Horizontal alignment
- All standard text properties (font, size, color, etc.)

---

## 3. Video Positioning

### NEW: X,Y Coordinate Positioning
Position video center at specific coordinates.

```json
{
  "video": {
    "width_pct": 85,
    "position": {
      "x": 540,
      "y": 960
    }
  }
}
```

### Default Behavior
If `x` and `y` are not specified, video is centered:
```json
{
  "video": {
    "width_pct": 85
  }
}
```

---

## 4. Header Image

### Configuration
Header appears above `top_text` (only when `top_text.enabled = true`).

```json
{
  "header": {
    "enabled": true,
    "path": "assets/header.png",
    "width": 600,
    "gap_px": 25
  }
}
```

### Properties
- **`enabled`**: Show/hide header
- **`path`**: Path to PNG file (relative to template folder)
- **`width`**: Target width in pixels (height auto-calculated to preserve aspect ratio)
- **`gap_px`**: Distance in pixels above top_text

---

## 5. Highlight Features

### Background Highlighting (Original)
Draws colored rectangle behind words.

```json
{
  "top_text": {
    "highlight": {
      "enabled": true,
      "type": "background",
      "mode": "ai",
      "highlight_count": 1,
      "color": "#ffde59"
    }
  }
}
```

### NEW: Color Word Highlighting
Colors the text itself instead of background.

```json
{
  "bottom_text": {
    "highlight": {
      "enabled": true,
      "type": "color_word",
      "mode": "manual",
      "manual_words": ["Samsung", "Ballie"],
      "color": "#ff0000"
    }
  }
}
```

### Highlight Types
- **`"background"`** - Rectangle behind words (original)
- **`"color_word"`** - NEW! Change text color of selected words

### Highlight Modes
- **`"ai"`** - Use AI to select impactful words
  - `highlight_count`: Number of words to highlight (default: 3)
- **`"manual"`** - Manually specify words/phrases
  - `manual_words`: Array of words or phrases to highlight

### Examples
```json
// Single words
"manual_words": ["Samsung", "incredible", "revolutionary"]

// Phrases (multiple words together)
"manual_words": ["AI-powered home helper", "game changer"]

// Mixed
"manual_words": ["Samsung", "AI-powered assistant", "incredible"]
```

---

## 6. Logo Positioning

### Canvas-Relative Positioning (Original)
Position relative to canvas edges using presets or coordinates.

```json
{
  "logo": {
    "enabled": true,
    "path": "assets/logo.png",
    "size": {
      "width": 150
    },
    "position_mode": "canvas",
    "position": "top-right",
    "margin": 40,
    "rotation": 0
  }
}
```

#### Position Presets
- `"top-right"` - Top right corner
- `"top-left"` - Top left corner
- `"bottom-right"` - Bottom right corner
- `"bottom-left"` - Bottom left corner

#### Custom X,Y Positioning
```json
{
  "logo": {
    "position_mode": "canvas",
    "position": {
      "x": 100,
      "y": 100
    }
  }
}
```

### NEW: Video-Relative Positioning
Position logo relative to video edges.

```json
{
  "logo": {
    "enabled": true,
    "path": "assets/logo.png",
    "size": {
      "width": 120
    },
    "position_mode": "video",
    "video_relative": {
      "x": 20,
      "y": 20
    },
    "rotation": 15
  }
}
```

#### Video-Relative Properties
- **`position_mode`**: Set to `"video"`
- **`video_relative.x`**: Pixels from left edge of video
- **`video_relative.y`**: Pixels from top edge of video

### NEW: Logo Rotation
Rotate logo by degrees (clockwise).

```json
{
  "logo": {
    "rotation": 15
  }
}
```

- **`rotation`**: Degrees to rotate (default: 0)
- Positive values = clockwise
- Negative values = counter-clockwise

---

## Complete Example Template

```json
{
  "id": "dual_text_advanced",
  "name": "Dual Text with All Features",
  "canvas": {
    "width": 1080,
    "height": 1920
  },
  "background": {
    "type": "color",
    "value": "#1a1a1a"
  },
  "video": {
    "width_pct": 80,
    "position": {
      "x": 540,
      "y": 960
    }
  },
  "top_text": {
    "enabled": true,
    "font": "assets/font-bold.ttf",
    "font_size": 110,
    "min_font_size": 40,
    "max_lines": 2,
    "line_width_pct": 90,
    "line_spacing_factor": 1.4,
    "color": "#ffffff",
    "casing_mode": "none",
    "align": "center",
    "gap_px": 40,
    "highlight": {
      "enabled": true,
      "type": "background",
      "mode": "ai",
      "highlight_count": 1,
      "color": "#ffde59"
    }
  },
  "bottom_text": {
    "enabled": true,
    "font": "assets/font-regular.ttf",
    "font_size": 70,
    "min_font_size": 30,
    "max_lines": 3,
    "line_width_pct": 85,
    "line_spacing_factor": 1.3,
    "color": "#cccccc",
    "casing_mode": "uppercase",
    "align": "center",
    "gap_px": 35,
    "highlight": {
      "enabled": true,
      "type": "color_word",
      "mode": "manual",
      "manual_words": ["revolutionary", "game changer"],
      "color": "#00ff88"
    }
  },
  "header": {
    "enabled": true,
    "path": "assets/header.png",
    "width": 550,
    "gap_px": 30
  },
  "logo": {
    "enabled": true,
    "path": "assets/logo.png",
    "size": {
      "width": 140
    },
    "position_mode": "video",
    "video_relative": {
      "x": 15,
      "y": 15
    },
    "rotation": -5
  },
  "renderer": {
    "module": "template_engine",
    "entrypoint": "render_with_template"
  }
}
```

---

## Backward Compatibility

All changes are **backward compatible**:

1. **Old templates** without `top_text`/`bottom_text` still work (uses legacy `text` config)
2. **Default values** ensure missing properties don't break rendering
3. **Original features** (background highlighting, preset positioning) remain unchanged
4. **Line spacing calculations** preserved exactly

---

## Usage with CLI

```bash
# Basic usage
python cli/template_renderer.py \
  --input input.mp4 \
  --output output.mp4 \
  --text "Your amazing text here" \
  --template-root templates/advanced_template

# With overrides
python cli/template_renderer.py \
  --input input.mp4 \
  --output output.mp4 \
  --text "Your text" \
  --template-root templates/advanced_template \
  --override top_text.color="#ff0000" \
  --override bottom_text.enabled=false \
  --override logo.rotation=10
```

---

## Migration Guide

### Converting Old Templates

**Old (single text):**
```json
{
  "text": {
    "font": "assets/font.ttf",
    "font_size": 120,
    "color": "#ffffff",
    "position_relative_to_video": {
      "gap_px": 20
    }
  }
}
```

**New (dual text system):**
```json
{
  "top_text": {
    "enabled": true,
    "font": "assets/font.ttf",
    "font_size": 120,
    "color": "#ffffff",
    "gap_px": 20
  },
  "bottom_text": {
    "enabled": false
  }
}
```