import sys
import os
import logging
import time

# Setup logging
logging.basicConfig(level=logging.INFO)

# Add parent dir to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    from api import firebase_queue
    
    # Create a dummy file
    test_file = "test_video.mp4"
    with open(test_file, "w") as f:
        f.write("This is a test video content.")
        
    print(f"Uploading {test_file}...")
    destination = f"tests/test_{int(time.time())}.mp4"
    link = firebase_queue.upload_video(test_file, destination)
    
    if link:
        print(f"SUCCESS! File uploaded.")
        print(f"Link: {link}")
    else:
        print("FAILURE: Upload returned no link.")
        
    # Cleanup
    if os.path.exists(test_file):
        os.remove(test_file)
        
except Exception as e:
    print(f"Error: {e}")
