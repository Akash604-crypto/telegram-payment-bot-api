"""
Telegram Payment Bot (single-file)
UPGRADED: UPI uses Smart Dynamic QR (Auto-Approve).
MANUAL: Crypto & Remitly still require Admin Approval.
"""

import os
import base64
import json
import time
import hmac
import hashlib
import threading
import asyncio
from typing import Dict, Any
from pathlib import Path
import sys

import requests
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

# -------------------- Configuration & storage --------------------
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
        "upi_id": os.environ.get("UPI_ID", "govindmahto21@axl"),
        "crypto_address": os.environ.get("CRYPTO_ADDRESS", "0xfc14846229f375124d8fed5cd9a789a271a303f5"),
        "crypto_network": os.environ.get("CRYPTO_NETWORK", "BEP20"),
        "remitly_info": os.environ.get("REMITLY_INFO", "Recipient: Govind Mahto. UPI: govindmahto21@axl"),
        "remitly_how_to": os.environ.get("REMITLY_HOW_TO_PAY_LINK", "https://t.me/+8jECICY--sU2MjIx"),
    }
}

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

BOT_LOOP = None

def load_db():
    if DB_FILE.exists(): return json.loads(DB_FILE.read_text())
    return {"payments": []}

def save_db(db):
    DB_FILE.write_text(json.dumps(db, indent=2))

def load_settings():
    if SETTINGS_FILE.exists(): return json.loads(SETTINGS_FILE.read_text())
    SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
    return DEFAULT_SETTINGS

def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

DB = load_db()
SETTINGS = load_settings()

# -------------------- Razorpay Smart QR Helper --------------------
def create_razorpay_smart_qr(amount_in_rupees, user_id, package):
    url = "https://api.razorpay.com/v1/payments/qr_codes"
    payload = {
        "type": "upi_qr",
        "name": f"User_{user_id}",
        "usage": "single_use",
        "fixed_amount": True,
        "payment_amount": amount_in_rupees * 100, 
        "description": f"Auto-pay {package}",
        "notes": {
            "user_id": str(user_id),
            "package": package
        }
    }
    try:
        r = requests.post(url, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), json=payload, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"QR Error: {e}")
        return None

# -------------------- Bot Handlers --------------------
app = Flask(__name__)
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")

def main_keyboard():
    kb = [
        [InlineKeyboardButton("VIP", callback_data="choose_vip")],
        [InlineKeyboardButton("DARK", callback_data="choose_dark")],
        [InlineKeyboardButton("BOTH (30% off)", callback_data="choose_both")],
        [InlineKeyboardButton("HELP", callback_data="help")],
    ]
    return InlineKeyboardMarkup(kb)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Choose a package to continue:", reply_markup=main_keyboard())

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
        kb = [
            [InlineKeyboardButton(f"UPI (Fast/Auto) - ₹{SETTINGS['prices'][package]['upi']}", callback_data=f"pay_upi:{package}")],
            [InlineKeyboardButton(f"Crypto - ${SETTINGS['prices'][package]['crypto_usd']}", callback_data=f"pay_crypto:{package}")],
            [InlineKeyboardButton(f"Remitly - ₹{SETTINGS['prices'][package]['remitly']}", callback_data=f"pay_remitly:{package}")],
            [InlineKeyboardButton("Cancel", callback_data="cancel")],
        ]
        await query.message.reply_text(f"Select Payment Method for {package.upper()}:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "cancel":
        await query.message.reply_text("Menu closed. Use /start to reopen.")
        return

    if data.startswith("pay_"):
        method, package = data.split(":")
        method = method.replace("pay_", "")
        
        entry = {
            "payment_id": f"p_{int(time.time()*1000)}",
            "user_id": user.id,
            "username": user.username or "",
            "package": package,
            "method": method,
            "status": "pending",
            "created_at": int(time.time()),
        }

if method == "upi":
    amount = SETTINGS["prices"][package]["upi"]

    # Step 1 → Inform user
    msg1 = await query.message.reply_text("⏳ Creating QR code...")

    # Step 2 → Create QR
    qr_resp = create_razorpay_smart_qr(amount, user.id, package)
    if not qr_resp:
        await msg1.edit_text("❌ System Busy. Try again later.")
        return
    
    # Step 3 → Update DB
    entry["razorpay_qr_id"] = qr_resp['id']
    DB["payments"].append(entry)
    save_db(DB)

    # Step 4 → Inform before sending
    await msg1.edit_text("📤 Sending QR code...")

    # Step 5 → Send actual QR
    await query.message.reply_photo(
        photo=qr_resp['image_url'],
        caption=(
            f"✅ **SCAN & PAY ₹{amount}**\n\n"
            f"• Auto-detect payment\n"
            f"• No need to send screenshot\n"
            f"• Access link will arrive instantly after payment"
        )
    )

    return


        # Crypto/Remitly Flow (Manual)
        DB["payments"].append(entry)
        save_db(DB)
        text = build_manual_payment_text(package, method)
        await query.message.reply_text(text)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    if msg.photo or msg.document:
        for p in reversed(DB["payments"]):
            if p["user_id"] == user_id and p["status"] == "pending" and p["method"] in ("crypto", "remitly"):
                file_obj = msg.photo[-1] if msg.photo else msg.document
                file = await file_obj.get_file()
                save_path = DATA_DIR / f"proof_{user_id}_{int(time.time())}.jpg"
                await file.download_to_drive(str(save_path))
                p.setdefault("proof_files", []).append(str(save_path))
                save_db(DB)

                buttons = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve:{p['payment_id']}"),
                     InlineKeyboardButton("❌ DECLINE", callback_data=f"decline:{p['payment_id']}")]
                ])
                await context.bot.send_photo(chat_id=SETTINGS["admin_chat_id"], photo=open(save_path, "rb"), 
                                            caption=f"Manual Proof: {user_id}\nPkg: {p['package']}", reply_markup=buttons)
                return await msg.reply_text("📸 Proof sent to Admin for verification.")

# -------------------- Admin Command Functions (Preserved) --------------------
async def setlink(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    if len(context.args) < 2: return await update.message.reply_text("/setlink <pkg> <link>")
    SETTINGS['links'][context.args[0]] = context.args[1]
    save_settings(SETTINGS)
    await update.message.reply_text(f"Link updated for {context.args[0]}")

async def setprice(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    if len(context.args) < 3: return await update.message.reply_text("/setprice <pkg> <upi/crypto_usd> <val>")
    pkg, method, val = context.args[0], context.args[1], int(context.args[2])
    SETTINGS['prices'][pkg][method] = val
    save_settings(SETTINGS)
    await update.message.reply_text("Price updated.")

async def admin_review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, pay_id = query.data.split(":")
    for p in DB["payments"]:
        if p["payment_id"] == pay_id:
            if action == "approve":
                p["status"] = "verified"
                save_db(DB)
                await send_link_to_user(p["user_id"], p["package"])
                await query.edit_message_text("✅ Approved")
            elif action == "decline":
                p["status"] = "declined"
                save_db(DB)
                await context.bot.send_message(p["user_id"], "❌ Payment declined.")
                await query.edit_message_text("❌ Declined")
            break

async def send_link_to_user(user_id: int, package: str):
    link = SETTINGS["links"].get(package, "Link not set. Contact admin.")
    await app_instance.bot.send_message(chat_id=user_id, text=f"✅ Access Granted ({package}):\n{link}")

def build_manual_payment_text(package, method):
    pi = SETTINGS['payment_info']
    if method == 'crypto':
        return f"Send ${SETTINGS['prices'][package]['crypto_usd']} ({pi['crypto_network']}) to:\n`{pi['crypto_address']}`\nReply with screenshot."
    return f"{pi['remitly_info']}\nReply with screenshot."

# -------------------- Webhook (Auto-Approve UPI) --------------------
@app.route('/razorpay_webhook', methods=['POST'])
def razorpay_webhook():
    data = request.json
    if data.get('event') == 'qr_code.credited':
        qr_entity = data['payload']['qr_code']['entity']
        qr_id = qr_entity['id']
        user_id = int(qr_entity['notes']['user_id'])
        package = qr_entity['notes']['package']

        for p in DB["payments"]:
            if p.get("razorpay_qr_id") == qr_id and p["status"] == "pending":
                p["status"] = "verified"
                save_db(DB)
                if BOT_LOOP:
                    asyncio.run_coroutine_threadsafe(send_link_to_user(user_id, package), BOT_LOOP)
                break
    return jsonify({"status": "ok"}), 200

# -------------------- Startup --------------------
async def post_init(application):
    global BOT_LOOP
    BOT_LOOP = asyncio.get_running_loop()

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app_instance = application

    application.add_handler(CommandHandler('start', start_handler))
    application.add_handler(CommandHandler('setlink', setlink))
    application.add_handler(CommandHandler('setprice', setprice))
    application.add_handler(CallbackQueryHandler(callback_handler, pattern="^(choose_|pay_|cancel)"))
    application.add_handler(CallbackQueryHandler(admin_review_handler, pattern="^(approve|decline):"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, message_handler))

    application.run_polling()
