import os
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

LOG = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive.file']
FOLDER_ID = "1vHYh69vfHHpqKKFtE0f70xj0v8tEWB91"

def get_drive_service():
    """Authenticate and return Drive service."""
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sa_path = os.path.join(base_dir, 'service-account.json')
        
        if not os.path.exists(sa_path):
            LOG.error(f"Service account file not found at {sa_path}")
            return None
            
        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=SCOPES)
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        LOG.error(f"Failed to create Drive service: {e}")
        return None

def upload_file(file_path: str, mime_type: str = 'video/mp4') -> str:
    """
    Upload a file to Google Drive and return the webViewLink.
    Returns None if upload fails.
    """
    service = get_drive_service()
    if not service:
        return None

    try:
        file_name = os.path.basename(file_path)
        file_metadata = {
            'name': file_name,
            'parents': [FOLDER_ID]
        }
        
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
        
        LOG.info(f"Uploading {file_name} to Drive folder {FOLDER_ID}...")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink, webContentLink'
        ).execute()
        
        # Make the file readable by anyone with the link (optional, but good for sharing)
        # Or rely on folder permissions if the folder is already shared.
        # For now, we assume folder permissions are sufficient or we just return the link.
        
        link = file.get('webViewLink')
        LOG.info(f"Upload complete. File ID: {file.get('id')}, Link: {link}")
        return link
        
    except Exception as e:
        LOG.error(f"Drive upload failed: {e}")
        return None
