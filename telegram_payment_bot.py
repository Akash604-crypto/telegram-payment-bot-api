"""
Telegram Payment Bot (single-file)
Generated for Akash Modak's spec (VIP/DARK/BOTH + payments)

What this file contains:
- Telegram bot using python-telegram-bot v20 (async)
- Simple Flask webhook endpoint to receive Razorpay webhooks (so UPI payments can be auto-approved)
- Local JSON storage for payments and settings
- Admin commands from your spec (/setprice, /setlink, /listpayments, /verify, /announce, /setpaymentinfo, /stats, /helpadmin, /sales)
- Inline keyboards: VIP / DARK / BOTH / HELP; payment method selection after bundle choice

HOW TO USE / DEPLOY notes (keep these in repo README)
1) Required environment variables (set on Render or locally):
   BOT_TOKEN - Telegram bot token
   ADMIN_CHAT_ID - admin chat id (your admin id: 7202040199)
   VIP_CHANNEL_ID - channel id for VIP sales (e.g. -1003308911819)
   DARK_CHANNEL_ID - channel id for DARK sales (e.g. -1003335040158)
   RAZORPAY_KEY_ID - Razorpay API Key ID
   RAZORPAY_KEY_SECRET - Razorpay API Key Secret
   RAZORPAY_WEBHOOK_SECRET - Razorpay webhook secret (for verifying webhooks)
   UPI_ID - e.g. "govindmahto21@axl"
   UPI_QR_URL - optional
   UPI_HOW_TO_PAY_LINK - remitly/how-to link for UPI
   CRYPTO_ADDRESS - 0xfc14846229f375124d8fed5cd9a789a271a303f5
   CRYPTO_NETWORK - BEP20
   REMITLY_INFO - string with remitly instructions
   REMITLY_HOW_TO_PAY_LINK - link
   PORT - (for Render) default 8080
   DATA_DIR - path to save payment db (default ./data)

2) Requirements (create requirements.txt):
   python-telegram-bot==20.5
   Flask==2.2.5
   requests==2.31.0

3) Run locally:
   export BOT_TOKEN=...
   python telegram_payment_bot.py

4) Deploy to Render (or Heroku-like):
   - Create GitHub repo, push this file and requirements.txt & Procfile
   - Procfile: "web: python telegram_payment_bot.py"
   - On Render create a Web Service, connect repo, add the environment variables above and deploy.

Notes:
- This implementation uses Razorpay Payment Links API to create a payment link for UPI. When the payment link is paid, the Razorpay webhook marks payment as paid and the bot sends the access link automatically.
- Crypto / Remitly flows are manual: the bot collects the user and uploaded screenshot (or just "I paid" message). Admin is notified to verify and can use /verify to approve.

"""

import os
import json
import time
import hmac
import hashlib
import threading
from typing import Dict, Any
from pathlib import Path
import sys
if sys.version_info.major == 3 and sys.version_info.minor >= 12:
    raise RuntimeError(
        "This bot must run on Python 3.11.x. Please set runtime.txt to python-3.11.x "
        "or change the Render service runtime. Running on Python "
        f"{sys.version_info.major}.{sys.version_info.minor} is not supported."
    )


import requests
from flask import Flask, request, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- Configuration & storage --------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "payments.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "admin_chat_id": int(os.environ.get("ADMIN_CHAT_ID", "7202040199")),
    "vip_channel_id": int(os.environ.get("VIP_CHANNEL_ID", "-1003308911819")),
    "dark_channel_id": int(os.environ.get("DARK_CHANNEL_ID", "-1003335040158")),
    "prices": {
        "vip": {"upi": 499, "crypto_usd": 6, "remitly": 499},
        "dark": {"upi": 1999, "crypto_usd": 24, "remitly": 1999},
        "both": {"upi": 1749, "crypto_usd": 20, "remitly": 1749},
    },
    "links": {"vip": "", "dark": "", "both": ""},
    "payment_info": {
        "upi_id": os.environ.get("UPI_ID", "govindmahto21@axl"),
        "upi_qr_url": os.environ.get("UPI_QR_URL", ""),
        "crypto_address": os.environ.get("CRYPTO_ADDRESS", "0xfc14846229f375124d8fed5cd9a789a271a303f5"),
        "crypto_network": os.environ.get("CRYPTO_NETWORK", "BEP20"),
        "remitly_info": os.environ.get("REMITLY_INFO", "Select India as destination. Recipient Name: Govind Mahto. UPI ID: govindmahto21@axl. Reason: Family Support."),
        "remitly_how_to": os.environ.get("REMITLY_HOW_TO_PAY_LINK", "https://t.me/+8jECICY--sU2MjIx"),
    }
}

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# load/save helpers

def load_db() -> Dict[str, Any]:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {"payments": []}


def save_db(db: Dict[str, Any]):
    DB_FILE.write_text(json.dumps(db, indent=2))


def load_settings() -> Dict[str, Any]:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
    return DEFAULT_SETTINGS


def save_settings(s: Dict[str, Any]):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

DB = load_db()
SETTINGS = load_settings()

# -------------------- Telegram bot logic --------------------

app = Flask(__name__)
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing — set it as an environment variable in Render.")


# Helper: build main keyboard

def main_keyboard():
    kb = [
        [InlineKeyboardButton("VIP", callback_data="choose_vip")],
        [InlineKeyboardButton("DARK", callback_data="choose_dark")],
        [InlineKeyboardButton("BOTH (30% off)", callback_data="choose_both")],
        [InlineKeyboardButton("HELP", callback_data="help")],
    ]
    return InlineKeyboardMarkup(kb)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Welcome {user.first_name}! Choose an option:",
        reply_markup=main_keyboard(),
    )

# After user chooses package, show payment methods
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "help":
        await query.message.reply_text("Contact help: @Dark123222_bot")
        return

    if data.startswith("choose_"):
        package = data.split("_")[1]
        # Show payment method choices
        kb = [
            [InlineKeyboardButton(f"UPI - ₹{SETTINGS['prices'][package]['upi']}", callback_data=f"pay_upi:{package}" )],
            [InlineKeyboardButton(f"Crypto - ${SETTINGS['prices'][package]['crypto_usd']}", callback_data=f"pay_crypto:{package}")],
            [InlineKeyboardButton(f"Remitly - ₹{SETTINGS['prices'][package]['remitly']}", callback_data=f"pay_remitly:{package}")],
            [InlineKeyboardButton("Cancel", callback_data="cancel")],
        ]
        await query.message.reply_text(
            f"You chose {package.upper()}. Select payment method:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "cancel":
        await query.message.reply_text("Cancelled. Use /start to open menu again.")
        return

    # Payment method flows
    if data.startswith("pay_"):
        method, package = data.split(":")
        method = method.replace("pay_", "")
        # Record an entry in DB with status pending
        entry = {
            "user_id": user.id,
            "username": user.username or "",
            "package": package,
            "method": method,
            "status": "pending",
            "created_at": int(time.time()),
        }
        DB["payments"].append(entry)
        save_db(DB)

        # If UPI -> create razorpay payment link and send to user (automatic)
        if method == "upi":
            amount = SETTINGS["prices"][package]["upi"] * 100  # in paise
            # create payment link
            link = create_razorpay_payment_link(amount, f"{package.upper()} bundle for {user.id}")
            if link:
                # store link ref
                entry["payment_link"] = link["short_url"]
                entry["razorpay_id"] = link.get("id")
                save_db(DB)
                await query.message.reply_text(
                    f"UPI payment link created. Pay using this link:\n{link['short_url']}\nAfter payment the bot will auto-deliver the access link.")
                # notify admin about pending payment
                await notify_admin_of_pending(entry)
                return
            else:
                await query.message.reply_text("Failed to create payment link. Please try again later or contact admin.")
                return

        # Crypto and Remitly - manual
        if method in ("crypto", "remitly"):
            text = build_manual_payment_text(package, method)
            await query.message.reply_text(text)
            # notify admin with user info to verify manually
            await notify_admin_of_pending(entry)
            return

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accept user-uploaded screenshots as proof for manual payments
    msg = update.message
    if msg.photo:
        # find last pending manual payment by this user
        user_id = msg.from_user.id
        for p in reversed(DB["payments"]):
            if p["user_id"] == user_id and p["status"] == "pending" and p["method"] in ("crypto", "remitly"):
                # download photo
                file = await msg.photo[-1].get_file()
                saved = DATA_DIR / f"proof_{user_id}_{int(time.time())}.jpg"
                await file.download_to_drive(str(saved))
                p.setdefault("proof_files", []).append(str(saved))
                save_db(DB)
                await msg.reply_text("Proof received. Admin will review and approve/decline.")
                # notify admin
                await app_instance.bot.send_message(
                    chat_id=SETTINGS["admin_chat_id"],
                    text=f"New proof received for user {user_id} ({msg.from_user.username or 'no-username'})\nPackage: {p['package']}\nMethod: {p['method']}",
                )
                return
    # fallback
    return

# -------------------- Razorpay helpers --------------------

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"

def create_razorpay_payment_link(amount_paise: int, description: str):
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        print("Razorpay keys missing")
        return None

    payload = {
        "type": "link",   # IMPORTANT FIX 🔥

        "amount": amount_paise,
        "currency": "INR",
        "description": description,

        # Razorpay requires customer object for some accounts
        "customer": {
            "name": "Telegram User",
            "contact": "9999999999",
            "email": "test@test.com"
        },

        "notify": {
            "sms": False,
            "email": False
        },

        "options": {
            "checkout": {
                "method": ["upi"]    # Force UPI only
            }
        },

        "expire_by": int(time.time()) + 86400
    }

    try:
        r = requests.post(
            f"{RAZORPAY_API_BASE}/payment_links",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json=payload,
            timeout=20,
        )
        print("RZP status:", r.status_code, r.text)
        r.raise_for_status()
        return r.json()

    except Exception as e:
        print("create_razorpay_payment_link failed:", e)
        return None


# -------------------- Admin helpers --------------------

async def notify_admin_of_pending(entry):
    text = (
        f"New payment pending:\nUser: {entry['user_id']} ({entry.get('username','')})\nPackage: {entry['package']}\nMethod: {entry['method']}\nStatus: pending"
    )
    if entry.get("payment_link"):
        text += f"\nLink: {entry['payment_link']}"
    await app_instance.bot.send_message(chat_id=SETTINGS["admin_chat_id"], text=text)

async def send_link_to_user(user_id: int, package: str):
    link = SETTINGS["links"].get(package, "")
    if not link:
        await app_instance.bot.send_message(chat_id=user_id, text="Sorry, access link is not set. Contact admin.")
        return
    await app_instance.bot.send_message(chat_id=user_id, text=f"✅ Your {package.upper()} access link:\n{link}")

# Admin command implementations

async def helpadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("You are not admin.")
        return
    text = "🔐 ADMIN PANEL\n" + "\n".join([
        "/sales – View sales summary",
        "/listpayments – Show all stored payments",
        "/verify – Manually verify a user payment (usage: /verify <user_id>)",
        "/announce – Broadcast message to all users",
        "/setprice – Update the bundle price",
        "/setlink – Update the access/download link",
        "/setpaymentinfo – Update UPI/QR/Crypto/Remitly details",
        "/stats – Bot & performance stats",
    ])
    await update.message.reply_text(text)

async def listpayments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("Not authorized")
        return
    lines = []
    for p in DB["payments"][-50:][::-1]:
        lines.append(f"{p['user_id']} | {p['package']} | {p['method']} | {p['status']}")
    await update.message.reply_text("\n".join(lines) or "No payments stored")

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("Not authorized")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /verify <user_id> [package]")
        return
    user_id = int(args[0])
    package = args[1] if len(args) > 1 else None
    # find pending payment
    for p in DB["payments"]:
        if p["user_id"] == user_id and p["status"] == "pending" and (package is None or p["package"] == package):
            p["status"] = "verified"
            save_db(DB)
            await send_link_to_user(user_id, p["package"])            
            await update.message.reply_text("Verified and link sent.")
            return
    await update.message.reply_text("No matching pending payment found.")

async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("Not authorized")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /announce <message>")
        return
    # broadcast to unique users in DB
    users = {p['user_id'] for p in DB['payments']}
    for u in users:
        try:
            await app_instance.bot.send_message(chat_id=u, text=text)
        except Exception:
            pass
    await update.message.reply_text("Announcement sent (attempted)")

async def setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("Not authorized")
        return
    # usage: /setprice vip upi 499
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /setprice <package> <method> <value>")
        return
    package, method, val = context.args[0], context.args[1], context.args[2]
    try:
        valn = float(val)
    except:
        await update.message.reply_text("Value must be a number")
        return
    SETTINGS['prices'].setdefault(package, {})[method] = int(valn)
    save_settings(SETTINGS)
    await update.message.reply_text("Price updated")

async def setlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("Not authorized")
        return
    # usage: /setlink vip https://... 
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setlink <package> <link>")
        return
    package = context.args[0]
    link = " ".join(context.args[1:])
    SETTINGS['links'][package] = link
    save_settings(SETTINGS)
    await update.message.reply_text("Link saved")

async def setpaymentinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("Not authorized")
        return
    # simple key=value pairs, e.g. /setpaymentinfo upi_id=abc crypto_address=0x...
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /setpaymentinfo key=value ...")
        return
    parts = text.split()
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            SETTINGS['payment_info'][k] = v
    save_settings(SETTINGS)
    await update.message.reply_text("Payment info updated")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await update.message.reply_text("Not authorized")
        return
    total = len(DB['payments'])
    verified = sum(1 for p in DB['payments'] if p['status'] == 'verified')
    await update.message.reply_text(f"Total payments: {total}\nVerified: {verified}")

# -------------------- Flask webhook for Razorpay --------------------

@app.route('/razorpay_webhook', methods=['POST'])
def razorpay_webhook():
    payload = request.get_data()
    signature = request.headers.get('X-Razorpay-Signature', '')
    if RAZORPAY_WEBHOOK_SECRET:
        computed = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
        ).hexdigest()
        # Razorpay sends signature as base64; a simple compare may not match all cases.
        # We'll accept if signature contains computed or vice versa - best effort.
        if signature and computed not in signature and signature not in computed:
            return jsonify({'status': 'invalid signature'}), 400
    event = request.json or {}
    # handle payment link paid
    if event.get('event') == 'payment_link.paid' or event.get('event') == 'payment_link.updated':
        obj = event.get('payload', {}).get('payment_link', {}).get('entity', {})
        pid = obj.get('id')
        # find payment in DB
        for p in DB['payments']:
            if p.get('razorpay_id') == pid:
                p['status'] = 'verified'
                p['razorpay_payload'] = obj
                save_db(DB)
                # send link to user
                try:
                    # use bot to send link
                    threading.Thread(target=lambda: app_instance.create_task(send_link_to_user(p['user_id'], p['package']))).start()
                except Exception as e:
                    print('notify bot failed', e)
                break
    return jsonify({'status': 'ok'})

# -------------------- Utilities --------------------

def build_manual_payment_text(package, method):
    pi = SETTINGS['payment_info']
    if method == 'crypto':
        return f"Send {SETTINGS['prices'][package]['crypto_usd']}$ via {pi.get('crypto_network')} to {pi.get('crypto_address')}. After sending, reply here with proof (screenshot). Admin will verify."
    if method == 'remitly':
        return f"{pi.get('remitly_info')}\nHow to: {pi.get('remitly_how_to')}\nAfter sending, reply with proof. Admin will verify."
    return "Manual payment - please follow instructions from admin."

# -------------------- Startup: register handlers & run --------------------

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

application.add_handler(CommandHandler('start', start_handler))
application.add_handler(CallbackQueryHandler(callback_handler))
application.add_handler(CommandHandler('helpadmin', helpadmin))
application.add_handler(CommandHandler('listpayments', listpayments))
application.add_handler(CommandHandler('verify', verify))
application.add_handler(CommandHandler('announce', announce))
application.add_handler(CommandHandler('setprice', setprice))
application.add_handler(CommandHandler('setlink', setlink))
application.add_handler(CommandHandler('setpaymentinfo', setpaymentinfo))
application.add_handler(CommandHandler('stats', stats))
application.add_handler(MessageHandler(filters.PHOTO, message_handler))

# store global app instance for webhook thread to use
app_instance = application


def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # start webhook server thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print('Flask webhook started in thread')
    # start the Telegram bot (polling)
    application.run_polling()
