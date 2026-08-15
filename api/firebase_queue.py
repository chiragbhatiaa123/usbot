import os
import logging
import firebase_admin
from firebase_admin import credentials, firestore, storage
from datetime import datetime
from typing import Optional, Dict, Any

LOG = logging.getLogger(__name__)

# Initialize Firebase Admin
try:
    # Check for local service account file in project root
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sa_path = os.path.join(base_dir, 'service-account.json')
    
    if os.path.exists(sa_path):
        cred = credentials.Certificate(sa_path)
        LOG.info(f"Using service account from {sa_path}")
    else:
        # Fallback to default credentials (gcloud auth application-default login)
        cred = credentials.ApplicationDefault()
        LOG.info("Using Application Default Credentials")

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred, {
            'projectId': 'templatea-queue',
            'storageBucket': 'templatea-queue.firebasestorage.app'
        })
    db = firestore.client()
    bucket = storage.bucket()
    LOG.info("Firebase Admin initialized successfully")
except Exception as e:
    LOG.error(f"Failed to initialize Firebase Admin: {e}")
    db = None
    bucket = None

def get_next_pending() -> Optional[Dict[str, Any]]:
    """Get the next pending URL from Firestore queue (FIFO)."""
    if not db:
        return None

    try:
        # Query for oldest pending item
        docs = db.collection('queue')\
            .where('status', '==', 'pending')\
            .order_by('created_at', direction=firestore.Query.ASCENDING)\
            .limit(1)\
            .get()

        if not docs:
            return None

        doc = docs[0]
        return {
            "id": doc.id,
            **doc.to_dict()
        }
    except Exception as e:
        LOG.error(f"Error fetching from Firebase queue: {e}")
        return None

def mark_processing(queue_id: str) -> bool:
    """Mark a queue entry as processing."""
    if not db:
        return False
    
    try:
        db.collection('queue').document(queue_id).update({
            'status': 'processing',
            'started_at': firestore.SERVER_TIMESTAMP
        })
        return True
    except Exception as e:
        LOG.error(f"Error marking processing {queue_id}: {e}")
        return False

def mark_completed(queue_id: str, workspace_id: str) -> bool:
    """Mark a queue entry as completed."""
    if not db:
        return False
    
    try:
        db.collection('queue').document(queue_id).update({
            'status': 'completed',
            'workspace_id': workspace_id,
            'completed_at': firestore.SERVER_TIMESTAMP
        })
        return True
    except Exception as e:
        LOG.error(f"Error marking completed {queue_id}: {e}")
        return False

def mark_failed(queue_id: str, error: str) -> bool:
    """Mark a queue entry as failed."""
    if not db:
        return False
    
    try:
        db.collection('queue').document(queue_id).update({
            'status': 'failed',
            'error': error,
            'completed_at': firestore.SERVER_TIMESTAMP
        })
        return True
    except Exception as e:
        LOG.error(f"Error marking failed {queue_id}: {e}")
        return False

def set_output_link(queue_id: str, link: str) -> bool:
    """Set the output link for a completed job."""
    if not db:
        return False
    
    try:
        db.collection('queue').document(queue_id).update({
            'output_link': link
        })
        return True
    except Exception as e:
        LOG.error(f"Error setting output link {queue_id}: {e}")
        return False

def upload_video(file_path: str, destination_blob_name: str) -> str:
    """
    Uploads a file to the bucket and returns the public URL.
    """
    if not bucket:
        LOG.error("Storage bucket not initialized")
        return None

    try:
        blob = bucket.blob(destination_blob_name)
        LOG.info(f"Uploading {file_path} to {destination_blob_name}...")
        blob.upload_from_filename(file_path)
        
        # Make public
        blob.make_public()
        
        LOG.info(f"Upload complete. Public URL: {blob.public_url}")
        return blob.public_url
    except Exception as e:
        LOG.error(f"Failed to upload video: {e}")
        return None
