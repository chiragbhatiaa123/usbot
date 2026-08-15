import sys
import os
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    filename='drive_debug.log',
    filemode='w',
    format='%(name)s - %(levelname)s - %(message)s'
)

# Add parent dir to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    from api import drive_upload
    
    # Create a dummy file
    test_file = "test_upload.txt"
    with open(test_file, "w") as f:
        f.write("This is a test upload from Templatea Queue.")
        
    print(f"Uploading {test_file}...")
    link = drive_upload.upload_file(test_file, mime_type="text/plain")
    
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
    with open("upload_error.log", "w") as log:
        log.write(str(e))
