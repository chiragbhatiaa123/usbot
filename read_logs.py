
import os

def read_last_lines(filename, n=50):
    if not os.path.exists(filename):
        print(f"{filename} does not exist")
        return
    try:
        with open(filename, 'rb') as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            pos = end
            lines = []
            while pos > 0 and len(lines) < n:
                pos -= 1024
                if pos < 0: pos = 0
                f.seek(pos)
                chunk = f.read(end - pos)
                chunk_lines = chunk.split(b'\n')
                if len(lines) == 0:
                    lines = chunk_lines
                else:
                    lines = chunk_lines[:-1] + [chunk_lines[-1] + lines[0]] + lines[1:]
                end = pos
            
            print(f"--- {filename} (last {n} lines) ---")
            for line in lines[-n:]:
                print(line.decode('utf-8', errors='ignore'))
    except Exception as e:
        print(f"Error reading {filename}: {e}")

read_last_lines('orchestrator.log')
read_last_lines('debug_template_engine.log')
