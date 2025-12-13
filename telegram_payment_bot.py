"""
Telegram Payment Bot (single-file)
- UPI: Auto-approve via clean Dynamic QR (No Razorpay Branding).
- Crypto/Remitly: Manual Admin Approval.
- Full Admin Suite: Price, Links, Broadcast, Income, Insights.
"""

import os
import json
import time
import threading
import asyncio
from pathlib import Path
from io import BytesIO
from datetime import datetime

import requests
import qrcode
from flask import Flask, request, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- Configuration --------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "payments.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "admin_chat_id": int(os.environ.get("ADMIN_CHAT_ID", "7202040199")),
    "prices": {
        "vip": {"upi": 499, "crypto_usd": 6, "remitly": 499},
        "dark": {"upi": 1999, "crypto_usd": 24, "remitly": 1999},
        "both": {"upi": 1749, "crypto_usd": 20, "remitly": 1749},
    },
    "links": {"vip": "", "dark": "", "both": ""},
    "payment_info": {
        "crypto": "Address: 0x... | Network: BEP20",
        "remitly": "Recipient: Govind Mahto | UPI: govindmahto21@axl",
    }
}

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# -------------------- Data Helpers --------------------
def load_db():
    if DB_FILE.exists(): return json.loads(DB_FILE.read_text())
    return {"payments": []}

def save_db(db):
    DB_FILE.write_text(json.dumps(db, indent=2))

def load_settings():
    if SETTINGS_FILE.exists(): return json.loads(SETTINGS_FILE.read_text())
    SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
    return DEFAULT_SETTINGS

DB = load_db()
SETTINGS = load_settings()
BOT_LOOP = None

# -------------------- QR Logic --------------------
def generate_clean_qr(upi_url: str):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

def create_rzp_qr(amount, user_id, package):
    url = "https://api.razorpay.com/v1/payments/qr_codes"
    payload = {
        "type": "upi_qr",
        "name": f"User_{user_id}",
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": amount * 100,
        "notes": {"user_id": str(user_id), "package": package}
    }
    try:
        r = requests.post(url, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), json=payload, timeout=20)
        data = r.json()
        return {"id": data['id'], "upi_url": data['payment_data']['upi_qr_url']}
    except: return None

# -------------------- Bot Handlers --------------------
app = Flask(__name__)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("VIP", callback_data="choose_vip")],
          [InlineKeyboardButton("DARK", callback_data="choose_dark")],
          [InlineKeyboardButton("BOTH", callback_data="choose_both")]]
    await update.message.reply_text("Select a package:", reply_markup=InlineKeyboardMarkup(kb))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data, user = query.data, query.from_user

    if data.startswith("choose_"):
        pkg = data.split("_")[1]
        kb = [[InlineKeyboardButton(f"UPI (Auto) - ₹{SETTINGS['prices'][pkg]['upi']}", callback_data=f"pay_upi:{pkg}")],
              [InlineKeyboardButton(f"Crypto - ${SETTINGS['prices'][pkg]['crypto_usd']}", callback_data=f"pay_crypto:{pkg}")],
              [InlineKeyboardButton(f"Remitly - ₹{SETTINGS['prices'][pkg]['remitly']}", callback_data=f"pay_remitly:{pkg}")]]
        await query.message.reply_text(f"Payment for {pkg.upper()}:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("pay_upi:"):
        pkg = data.split(":")[1]
        amount = SETTINGS['prices'][pkg]['upi']
        qr_data = create_rzp_qr(amount, user.id, pkg)
        if not qr_data: return await query.message.reply_text("❌ Error. Try again.")
        
        DB["payments"].append({"user_id": user.id, "package": pkg, "method": "upi", "status": "pending", "rzp_id": qr_data['id'], "amount": amount, "date": str(datetime.now().date())})
        save_db(DB)
        await query.message.reply_photo(photo=generate_clean_qr(qr_data['upi_url']), caption=f"✅ Pay ₹{amount}\nAccess sent instantly after payment.")

    elif data.startswith("pay_"):
        method, pkg = data.split(":")[0].replace("pay_",""), data.split(":")[1]
        DB["payments"].append({"user_id": user.id, "package": pkg, "method": method, "status": "pending", "date": str(datetime.now().date())})
        save_db(DB)
        await query.message.reply_text(f"Manual Payment: {SETTINGS['payment_info'].get(method)}\n\nReply with screenshot proof.")

# -------------------- Admin Suite --------------------
async def setlink(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    SETTINGS['links'][context.args[0]] = context.args[1]
    save_settings(SETTINGS); await update.message.reply_text("Link Saved.")

async def broadcast(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    msg = " ".join(context.args)
    users = {p['user_id'] for p in DB['payments']}
    for u in users:
        try: await context.bot.send_message(u, msg)
        except: pass
    await update.message.reply_text(f"Broadcast sent to {len(users)} users.")

async def income(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    total = sum(p.get('amount', 0) for p in DB['payments'] if p['status'] == 'verified')
    await update.message.reply_text(f"💰 Total Verified Income: ₹{total}")

async def insights(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    stats = f"Total Payments: {len(DB['payments'])}\n"
    stats += f"Verified: {len([p for p in DB['payments'] if p['status']=='verified'])}"
    await update.message.reply_text(stats)

async def setcrypto(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    SETTINGS['payment_info']['crypto'] = " ".join(context.args)
    save_settings(SETTINGS); await update.message.reply_text("Crypto Info Updated.")

async def setremitly(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    SETTINGS['payment_info']['remitly'] = " ".join(context.args)
    save_settings(SETTINGS); await update.message.reply_text("Remitly Info Updated.")

# -------------------- Webhook --------------------
@app.route('/razorpay_webhook', methods=['POST'])
def razorpay_webhook():
    data = request.json
    if data.get('event') == 'qr_code.credited':
        qr_id = data['payload']['qr_code']['entity']['id']
        for p in DB["payments"]:
            if p.get("rzp_id") == qr_id and p["status"] == "pending":
                p["status"] = "verified"
                save_db(DB)
                if BOT_LOOP: asyncio.run_coroutine_threadsafe(app_instance.bot.send_message(p['user_id'], f"✅ Access Granted:\n{SETTINGS['links'][p['package']]}"), BOT_LOOP)
    return jsonify({"status": "ok"}), 200

# -------------------- Startup --------------------
async def post_init(application):
    global BOT_LOOP
    BOT_LOOP = asyncio.get_running_loop()

if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=8080), daemon=True).start()
    application = ApplicationBuilder().token(os.environ.get("BOT_TOKEN")).post_init(post_init).build()
    app_instance = application
    
    # User Handlers
    application.add_handler(CommandHandler('start', start_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    
    # Admin Handlers
    application.add_handler(CommandHandler('setlink', setlink))
    application.add_handler(CommandHandler('broadcast', broadcast))
    application.add_handler(CommandHandler('income', income))
    application.add_handler(CommandHandler('insights', insights))
    application.add_handler(CommandHandler('setcrypto', setcrypto))
    application.add_handler(CommandHandler('setremitly', setremitly))
    
    application.run_polling()
