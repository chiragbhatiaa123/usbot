import sys
import os
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Setup logging
logging.basicConfig(level=logging.INFO)

SCOPES = ['https://www.googleapis.com/auth/drive.file']
FOLDER_ID = "1vHYh69vfHHpqKKFtE0f70xj0v8tEWB91"

def check_quota():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        sa_path = os.path.join(base_dir, 'service-account.json')
        
        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=SCOPES)
        service = build('drive', 'v3', credentials=creds)
        
        # Check About info for quota
        about = service.about().get(fields="storageQuota").execute()
        print(f"QUOTA: {about.get('storageQuota')}")
        
        # List files in folder
        # results = service.files().list(
        #     q=f"'{FOLDER_ID}' in parents",
        #     fields="files(id, name)"
        # ).execute()
        # files = results.get('files', [])
        # print(f"Files in folder {FOLDER_ID}:")
        # for file in files:
        #     print(f"  {file['name']} ({file['id']})")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_quota()
