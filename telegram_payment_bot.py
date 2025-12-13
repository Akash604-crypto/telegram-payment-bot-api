"""
Telegram Payment Bot (single-file)
FIXED VERSION: Concurrency & Webhook Loop Capture Included.
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

# Python version check
if sys.version_info.major == 3 and sys.version_info.minor >= 12:
    raise RuntimeError(
        "This bot must run on Python 3.11.x. Please set runtime.txt to python-3.11.x "
        "or change the Render service runtime."
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

# GLOBAL VAR TO HOLD THE EVENT LOOP
BOT_LOOP = None

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

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass

    data = query.data
    user = query.from_user

    if data == "help":
        await query.message.reply_text("Contact help: @Dark123222_bot")
        return

    if data.startswith("choose_"):
        package = data.split("_")[1]
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

        DB["payments"].append(entry)
        save_db(DB)

        if method == "upi":
            amount = SETTINGS["prices"][package]["upi"] * 100 
            link = create_razorpay_payment_link(amount, f"{package.upper()} bundle for {user.id}")

            if not link:
                return await query.message.reply_text(
                    "Failed to create payment link. Please try again later or contact admin."
                )

            entry["payment_link"] = link.get("short_url")
            entry["razorpay_id"] = link.get("id")
            save_db(DB)

            await query.message.reply_text(
                f"UPI payment link created.\nPay here:\n{entry['payment_link']}\n\n"
                "After payment is successful, bot will auto-deliver your access link."
            )
            await notify_admin_of_pending(entry)
            return

        if method in ("crypto", "remitly"):
            text = build_manual_payment_text(package, method)
            await query.message.reply_text(text)
            await notify_admin_of_pending(entry)
            return
        
async def admin_review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass

    data = query.data
    admin_id = query.from_user.id

    if admin_id != SETTINGS["admin_chat_id"]:
        return await query.message.reply_text("❌ You are not authorized.")

    action, pay_id = data.split(":")

    for p in DB["payments"]:
        if p["payment_id"] == pay_id:
            if action == "approve":
                p["status"] = "verified"
                save_db(DB)
                try:
                    await query.edit_message_reply_markup(None)
                    await query.edit_message_text("✅ Payment Approved")
                except:
                    pass
                await send_link_to_user(p["user_id"], p["package"])
                return await query.message.reply_text(f"✅ Approved. Link sent to {p['user_id']}.")

            if action == "decline":
                p["status"] = "declined"
                save_db(DB)
                try:
                    await query.edit_message_reply_markup(None)
                    await query.edit_message_text("❌ Payment Declined")
                except:
                    pass
                
                # Using application instance if available, otherwise context.bot
                await context.bot.send_message(
                    chat_id=p["user_id"],
                    text="❌ Payment declined.\nInvalid or incomplete proof.\nContact @Dark123222_bot."
                )
                return await query.message.reply_text("❌ Payment declined.")

    return await query.message.reply_text("Payment not found.")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id

    if msg.text and not msg.photo and not msg.document:
        return await msg.reply_text("❌ Text-only proof not accepted. Please upload a screenshot/document.")

    if msg.photo or msg.document:
        # Find latest pending manual payment
        for p in reversed(DB["payments"]):
            if (p["user_id"] == user_id and p["status"] == "pending" and p["method"] in ("crypto", "remitly")):
                
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

                caption = f"📩 *Proof Received*\nUser: {user_id}\nPackage: {p['package']}\nMethod: {p['method']}"

                if msg.photo:
                    await context.bot.send_photo(chat_id=SETTINGS["admin_chat_id"], photo=open(save_path, "rb"), caption=caption, reply_markup=buttons, parse_mode="Markdown")
                elif msg.document:
                    await context.bot.send_document(chat_id=SETTINGS["admin_chat_id"], document=open(save_path, "rb"), caption=caption, reply_markup=buttons, parse_mode="Markdown")

                return await msg.reply_text("📸 Proof received. Admin will verify shortly.")
    return


# -------------------- Razorpay helpers --------------------
RAZORPAY_API_BASE = "https://api.razorpay.com/v1"

def create_razorpay_payment_link(amount_paise: int, description: str):
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        print("❌ Razorpay keys missing")
        return None
    payload = {"amount": amount_paise, "currency": "INR", "description": description}
    try:
        r = requests.post(f"{RAZORPAY_API_BASE}/payment_links", auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), json=payload, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("❌ create_razorpay_payment_link FAILED:", e)
        return None

# -------------------- Admin helpers --------------------
async def notify_admin_of_pending(entry):
    # This might need context.bot if app_instance isn't ready, but inside handlers context is safer
    # However, since this is called from handlers, we need to pass context or use the global app instance if loop is running
    # To be safe, we rely on the loop being active.
    text = f"New payment pending:\nUser: {entry['user_id']}\nPackage: {entry['package']}\nMethod: {entry['method']}\nStatus: pending"
    if entry.get("payment_link"):
        text += f"\nLink: {entry['payment_link']}"
    
    if BOT_LOOP:
        # If we are in the main loop context, we can just print or try sending
        # But this function is usually called FROM an async handler, so we can just use app_instance
        try:
             await app_instance.bot.send_message(chat_id=SETTINGS["admin_chat_id"], text=text)
        except:
             pass

async def send_link_to_user(user_id: int, package: str):
    if package == "both":
        vip = SETTINGS["links"].get("vip", "")
        dark = SETTINGS["links"].get("dark", "")
        if not vip or not dark:
            await app_instance.bot.send_message(chat_id=user_id, text="❌ VIP or DARK link not set. Contact admin.")
            return
        await app_instance.bot.send_message(
            chat_id=user_id,
            text=f"✅ Your BOTH ACCESS:\n\n🔹 VIP Link:\n{vip}\n\n🔹 DARK Link:\n{dark}"
        )
        return

    link = SETTINGS["links"].get(package, "")
    if not link:
        await app_instance.bot.send_message(chat_id=user_id, text="Sorry, access link is not set. Contact admin.")
        return

    await app_instance.bot.send_message(chat_id=user_id, text=f"✅ Your {package.upper()} access link:\n{link}")


# Admin command implementations
async def helpadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    await update.message.reply_text("🔐 ADMIN PANEL\n/sales, /listpayments, /verify <id>, /announce <msg>, /setprice, /setlink, /setpaymentinfo, /stats")

async def listpayments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    lines = [f"{p['user_id']} | {p['package']} | {p['method']} | {p['status']}" for p in DB["payments"][-20:][::-1]]
    await update.message.reply_text("\n".join(lines) or "No payments stored")

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    args = context.args
    if not args: return await update.message.reply_text("Usage: /verify <user_id>")
    user_id = int(args[0])
    for p in DB["payments"]:
        if p["user_id"] == user_id and p["status"] == "pending":
            p["status"] = "verified"
            save_db(DB)
            await send_link_to_user(user_id, p["package"])
            return await update.message.reply_text("Verified and link sent.")
    await update.message.reply_text("No matching pending payment found.")

async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    text = " ".join(context.args)
    if not text: return
    users = {p['user_id'] for p in DB['payments']}
    for u in users:
        try: await context.bot.send_message(chat_id=u, text=text)
        except: pass
    await update.message.reply_text("Announcement sent.")

async def setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    if len(context.args) < 3: return await update.message.reply_text("Usage: /setprice <pkg> <method> <val>")
    SETTINGS['prices'].setdefault(context.args[0], {})[context.args[1]] = int(context.args[2])
    save_settings(SETTINGS)
    await update.message.reply_text("Price updated")

async def setlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    if len(context.args) < 2: return await update.message.reply_text("Usage: /setlink <pkg> <link>")
    SETTINGS['links'][context.args[0]] = " ".join(context.args[1:])
    save_settings(SETTINGS)
    await update.message.reply_text("Link saved")

async def setpaymentinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    text = " ".join(context.args)
    for p in text.split():
        if "=" in p:
            k, v = p.split("=", 1)
            SETTINGS['payment_info'][k] = v
    save_settings(SETTINGS)
    await update.message.reply_text("Payment info updated")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]: return
    await update.message.reply_text(f"Total: {len(DB['payments'])}")

# -------------------- Flask webhook for Razorpay --------------------
@app.route("/")
def home():
    return "OK", 200

@app.route('/razorpay_webhook', methods=['POST'])
def razorpay_webhook():
    # 1. Verify Signature
    raw_body = request.data
    received_signature = request.headers.get("X-Razorpay-Signature", "")
    if RAZORPAY_WEBHOOK_SECRET:
        computed_signature = hmac.new(
            key=RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256
        ).hexdigest()
        if computed_signature != received_signature:
            print("❌ Signature Mismatch")
            return jsonify({"status": "invalid signature"}), 400

    # 2. Process Event
    event = request.json or {}
    payload = event.get("payload", {})
    pid = None
    if "payment_link" in payload:
        pid = payload["payment_link"]["entity"]["id"]
    elif "payment" in payload:
        pid = payload["payment"]["entity"].get("payment_link_id")

    if not pid:
        return jsonify({"status": "ignored"}), 200

    print(f"🔔 Webhook received: {pid}")

    # 3. Find User & Verify
    for p in DB["payments"]:
        if p.get("razorpay_id") == pid:
            if p["status"] == "verified":
                return jsonify({"status": "already_processed"}), 200

            p["status"] = "verified"
            p["razorpay_payload"] = event
            save_db(DB)
            print(f"✅ Verified User {p['user_id']}")

            if BOT_LOOP:
                asyncio.run_coroutine_threadsafe(
                    send_link_to_user(p["user_id"], p["package"]),
                    BOT_LOOP
                )
            else:
                print("❌ CRITICAL: BOT_LOOP is None. Telegram message failed.")
            break
    
    return jsonify({"status": "ok"})

def build_manual_payment_text(package, method):
    pi = SETTINGS['payment_info']
    if method == 'crypto':
        return f"Send {SETTINGS['prices'][package]['crypto_usd']}$ via {pi.get('crypto_network')} to {pi.get('crypto_address')}. Reply with screenshot."
    if method == 'remitly':
        return f"{pi.get('remitly_info')}\nHow to: {pi.get('remitly_how_to')}\nReply with screenshot."
    return "Manual payment."

# -------------------- Startup --------------------

# This function captures the running event loop
async def post_init(application):
    global BOT_LOOP
    BOT_LOOP = asyncio.get_running_loop()
    print("✅ Bot Event Loop captured successfully!")

# Define global app instance
app_instance = None

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    print("🚀 Starting Flask webhook server...")
    threading.Thread(target=run_flask, daemon=True).start()

    print("🤖 Building Telegram bot...")
    # FIXED: Added .post_init(post_init) to the builder
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    app_instance = application

    # Register handlers
    application.add_handler(CommandHandler('start', start_handler))
    application.add_handler(CallbackQueryHandler(callback_handler, pattern="^(choose_|pay_|cancel)"))
    application.add_handler(CallbackQueryHandler(admin_review_handler, pattern="^(approve|decline):"))
    application.add_handler(CommandHandler('helpadmin', helpadmin))
    application.add_handler(CommandHandler('listpayments', listpayments))
    application.add_handler(CommandHandler('verify', verify))
    application.add_handler(CommandHandler('announce', announce))
    application.add_handler(CommandHandler('setprice', setprice))
    application.add_handler(CommandHandler('setlink', setlink))
    application.add_handler(CommandHandler('setpaymentinfo', setpaymentinfo))
    application.add_handler(CommandHandler('stats', stats))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, message_handler))

    print("✅ Polling started...")
    application.run_polling()
