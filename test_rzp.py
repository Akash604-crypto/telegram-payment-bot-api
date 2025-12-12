import requests
import os

key = os.environ.get("RAZORPAY_KEY_ID")
secret = os.environ.get("RAZORPAY_KEY_SECRET")

if not key or not secret:
    print("❌ Missing Razorpay environment variables.")
    print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in Render.")
    exit()

auth = (key, secret)

payload = {
    "amount": 49900,
    "currency": "INR",
    "description": "Standard Link Test"
}


print("➡️ Sending request to Razorpay...")

try:
    r = requests.post(
        "https://api.razorpay.com/v1/payment_links",
        auth=auth,
        json=payload,
        timeout=15
    )
    print("📌 Status Code:", r.status_code)
    print("📌 Response Body:", r.text)

except Exception as e:
    print("❌ Request Failed:", str(e))
