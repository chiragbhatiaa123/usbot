import sys
import os
import logging
import firebase_admin
from firebase_admin import credentials, firestore

# Setup logging
logging.basicConfig(level=logging.INFO)

# Add parent dir to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

def check_queue():
    try:
        # Init Firebase
        base_dir = os.path.dirname(os.path.abspath(__file__))
        sa_path = os.path.join(base_dir, 'service-account.json')
        cred = credentials.Certificate(sa_path)
        
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
            
        db = firestore.client()
        
        # Get last 5 items
        docs = db.collection('queue').order_by('created_at', direction=firestore.Query.DESCENDING).limit(5).stream()
        
        print("\n--- Recent Queue Items ---")
        for doc in docs:
            data = doc.to_dict()
            status = data.get('status')
            link = data.get('output_link')
            url = data.get('url')
            print(f"ID: {doc.id} | Status: {status} | Link: {link if link else 'N/A'}")
            if status == 'completed' and not link:
                print(f"  WARNING: Completed but no link! (URL: {url})")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_queue()
