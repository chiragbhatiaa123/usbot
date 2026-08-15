import requests
import sys

LINK = "https://firebasestorage.googleapis.com/v0/b/templatea-queue.firebasestorage.app/o/outputs%2FySMQsvK2Et2hb2NuhbLR.mp4?alt=media"

try:
    print(f"Checking link: {LINK}")
    response = requests.head(LINK)
    print(f"Status Code: {response.status_code}")
    
    if response.status_code == 200:
        print("SUCCESS: File is publicly accessible.")
    elif response.status_code == 403:
        print("FAILURE: Still getting 403 Permission Denied.")
    else:
        print(f"FAILURE: Unexpected status code {response.status_code}")
        
except Exception as e:
    print(f"Error: {e}")
