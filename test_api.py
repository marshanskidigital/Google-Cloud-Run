import urllib.request
import json
import urllib.error
import sys

# Testing the correct 5320 endpoint but with the exact conversation ID from screenshot
url = 'https://google-ads-api-44988665320.me-west1.run.app/ai-chat'
data = {
    "message": "מה הסטטוס ביצועים שלי",
    "conversation_id": "2e074a64-8001-41f5-bdb0-890c8cc6c379",
    "google_ads_customer_ids": ["4283707037"],
    "include_google_ads": True,
    "ga_property_id": "513346981",
    "gsc_property_url": "https://euroline.co.il/",
    "gtm_account_id": "47874966311",
    "gtm_public_id": "GTM-NCJ9SQYZ"
}

req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})

try:
    with urllib.request.urlopen(req) as resp:
        print("STATUS:", resp.status)
        print(resp.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print(f"FAILED WITH {e.code}")
    print("BODY:", e.read().decode('utf-8'))
except Exception as e:
    print("OTHER ERROR:", e)
