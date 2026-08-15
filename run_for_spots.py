"""
run_for_spots.py - CLI-compatible batch/single runner

Usage:
python run_for_spots.py <input_video> <output_video> [choice_txt_or_template_root]
"""

import sys
import os
from pathlib import Path
from marketingspots_template import process_marketingspots_template

def main():
    if len(sys.argv) < 3:
        print("Usage: run_for_spots.py <input> <output> [choice_txt_or_template_root]")
        sys.exit(1)

    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    choice = sys.argv[3] if len(sys.argv) > 3 else None

    if inp.is_dir():
        # batch mode
        out.mkdir(parents=True, exist_ok=True)
        for f in sorted(os.listdir(inp)):
            if not f.lower().endswith(".mp4"):
                continue
            in_path = inp / f
            base = in_path.stem
            out_path = out / f"{base}_final.mp4"
            text = "Auto title"
            template_config = None
            
            if choice and Path(choice).exists():
                choice_path = Path(choice)
                if choice_path.is_file() and choice_path.suffix == '.txt':
                    try:
                        text = choice_path.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
                elif choice_path.is_dir():
                    template_config = choice
            
            print(f"[run_for_spots] {in_path} -> {out_path} (text: {text[:60]})")
            process_marketingspots_template(str(in_path), str(out_path), text, config=template_config)
    else:
        # single file case
        text = "Auto title"
        template_config = None
        
        if choice and Path(choice).exists():
            choice_path = Path(choice)
            if choice_path.is_file() and choice_path.suffix == '.txt':
                try:
                    text = choice_path.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
            elif choice_path.is_dir():
                template_config = choice
        
        print(f"[run_for_spots] {inp} -> {out} (text: {text[:60]})")
        process_marketingspots_template(str(inp), str(out), text, config=template_config)

if __name__ == "__main__":
    main()
