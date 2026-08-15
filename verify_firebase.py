import sys
import os
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)

# Add parent dir to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    from api import firebase_queue
    print("Import successful")
    
    # Test connection (this will try to initialize app)
    # Note: This might fail if no credentials are found, which is what we want to test
    if firebase_queue.db:
        print("Firebase Admin initialized successfully")
        print("Checking for pending items...")
        item = firebase_queue.get_next_pending()
        print(f"Next pending: {item}")
    else:
        print("Firebase Admin failed to initialize (check logs)")
        
except Exception as e:
    print(f"Error: {e}")
