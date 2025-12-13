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
COUNTDOWN_TASKS = {}
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
    await update.message.reply_text(
        "Choose a package to continue:",
        reply_markup=main_keyboard()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # ----- HELP -----
    if data == "help":
        await query.message.reply_text("Contact help: @Dark123222_bot")
        return

    # ----- PACKAGE SELECTION -----
    if data.startswith("choose_"):
        package = data.split("_")[1]
        kb = [
            [InlineKeyboardButton(f"UPI (Fast/Auto) - ₹{SETTINGS['prices'][package]['upi']}",
                                  callback_data=f"pay_upi:{package}")],
            [InlineKeyboardButton(f"Crypto - ${SETTINGS['prices'][package]['crypto_usd']}",
                                  callback_data=f"pay_crypto:{package}")],
            [InlineKeyboardButton(f"Remitly - ₹{SETTINGS['prices'][package]['remitly']}",
                                  callback_data=f"pay_remitly:{package}")],
            [InlineKeyboardButton("Cancel", callback_data="cancel")],
        ]
        await query.message.reply_text(
            f"Select Payment Method for {package.upper()}",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # ----- CANCEL -----
    if data == "cancel":
        await query.message.reply_text("Menu closed. Use /start to reopen.")
        return

    # ----- PAYMENT METHOD SELECTED -----
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

        # ---------- UPI (WITH 10 MIN COUNTDOWN) ----------
        if method == "upi":
            amount = SETTINGS["prices"][package]["upi"]

            msg1 = await query.message.reply_text("⏳ Creating QR code...")

            qr_resp = create_razorpay_smart_qr(amount, user.id, package)
            if not qr_resp:
                await msg1.edit_text("❌ System Busy. Try again later.")
                return

            entry["razorpay_qr_id"] = qr_resp["id"]
            DB["payments"].append(entry)
            save_db(DB)

            await msg1.edit_text("📤 Sending QR code...")

            caption_text = (
                f"✅ **SCAN & PAY ₹{amount}**\n"
                f"• Auto-detect payment\n"
                f"• Do NOT send screenshot\n"
            )

            qr_msg = await query.message.reply_photo(
                photo=qr_resp["image_url"],
                caption=caption_text,
                parse_mode="Markdown"
            )

            entry["caption_text"] = caption_text
            entry["chat_id"] = qr_msg.chat.id
            entry["message_id"] = qr_msg.message_id
            save_db(DB)

            # Start 10-minute countdown
            COUNTDOWN_TASKS[entry["payment_id"]] = asyncio.create_task(
                start_countdown(entry["payment_id"], qr_msg.chat.id, qr_msg.message_id, 600)
            )

            return

        # ---------- MANUAL PAYMENTS (CRYPTO / REMITLY — 30 MIN COUNTDOWN) ----------
        DB["payments"].append(entry)
        save_db(DB)

        caption_text = build_manual_payment_text(package, method)

        msg2 = await query.message.reply_text(
            caption_text,
            parse_mode="Markdown"
        )

        entry["caption_text"] = caption_text
        entry["chat_id"] = msg2.chat.id
        entry["message_id"] = msg2.message_id
        save_db(DB)

        # Start 30-min countdown
        COUNTDOWN_TASKS[entry["payment_id"]] = asyncio.create_task(
            start_countdown(entry["payment_id"], msg2.chat.id, msg2.message_id, 1800)
        )

        return




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

    # Stop countdown if exists
    task = COUNTDOWN_TASKS.get(pay_id)
    if task:
        task.cancel()
        COUNTDOWN_TASKS.pop(pay_id, None)

    # Find payment record
    for p in DB["payments"]:
        if p["payment_id"] == pay_id:

            user_id = p["user_id"]
            package = p["package"]

            # Detect amount
            if p["method"] == "crypto":
                amount = f"${SETTINGS['prices'][package]['crypto_usd']}"
            else:
                amount = f"₹{SETTINGS['prices'][package]['upi']}"

            # ------- APPROVE -------
            if action == "approve":
                p["status"] = "verified"
                save_db(DB)

                # Update admin message
                try:
                    await query.edit_message_caption(
                        caption=f"✅ Approved payment (ID: {pay_id}) for user: {user_id} | amount: {amount}",
                        reply_markup=None
                    )
                except:
                    await query.edit_message_text(
                        f"✅ Approved payment (ID: {pay_id}) for user: {user_id} | amount: {amount}",
                        reply_markup=None
                    )

                # Send access to user
                await send_link_to_user(user_id, package)

                # Notify admin
                await context.bot.send_message(
                    SETTINGS["admin_chat_id"],
                    f"✅ Approved payment (ID: {pay_id}) for user: {user_id} | amount: {amount}"
                )
                return

            # ------- DECLINE -------
            if action == "decline":
                p["status"] = "declined"
                save_db(DB)

                # Update admin message
                try:
                    await query.edit_message_caption(
                        caption=f"❌ Declined payment (ID: {pay_id}) for user: {user_id} | amount: {amount}",
                        reply_markup=None
                    )
                except:
                    await query.edit_message_text(
                        f"❌ Declined payment (ID: {pay_id}) for user: {user_id} | amount: {amount}",
                        reply_markup=None
                    )

                # Notify user
                await context.bot.send_message(
                    user_id,
                    "❌ Payment declined. Please try again."
                )

                # Notify admin
                await context.bot.send_message(
                    SETTINGS["admin_chat_id"],
                    f"❌ Declined payment (ID: {pay_id}) for user: {user_id} | amount: {amount}"
                )
                return



async def send_link_to_user(user_id: int, package: str):
    if package == "both":
        vip_link = SETTINGS["links"].get("vip", "VIP link not set.")
        dark_link = SETTINGS["links"].get("dark", "DARK link not set.")

        text = (
            "🎉 **Access Granted: BOTH Package**\n\n"
            "Here are your links:\n"
            f"🔹 **VIP Access:**\n{vip_link}\n\n"
            f"🔹 **DARK Access:**\n{dark_link}\n"
        )

        await app_instance.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown"
        )
        return

    # Normal single package case
    link = SETTINGS["links"].get(package, "Link not set. Contact admin.")
    await app_instance.bot.send_message(
        chat_id=user_id,
        text=f"✅ Access Granted ({package.upper()}):\n{link}"
    )


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

                # STOP countdown if running
                task = COUNTDOWN_TASKS.get(p["payment_id"])
                if task:
                    task.cancel()
                    COUNTDOWN_TASKS.pop(p["payment_id"], None)

                # SEND ACCESS LINK
                if BOT_LOOP:
                    asyncio.run_coroutine_threadsafe(
                        send_link_to_user(user_id, package),
                        BOT_LOOP
                    )

                # DELETE QR MESSAGE
                try:
                    chat_id = p.get("chat_id")
                    msg_id = p.get("message_id")
                    if chat_id and msg_id:
                        asyncio.run_coroutine_threadsafe(
                            app_instance.bot.delete_message(chat_id, msg_id),
                            BOT_LOOP
                        )
                except Exception as e:
                    print("QR delete error:", e)

                break

    return jsonify({"status": "ok"}), 200

# -------------------- Startup --------------------
async def start_countdown(payment_id: str, chat_id: int, message_id: int, seconds: int):
    global COUNTDOWN_TASKS

    for p in DB["payments"]:
        if p["payment_id"] == payment_id:
            break
    else:
        return
    
    while seconds > 0:
        # Stop countdown if payment is no longer pending
        if p["status"] != "pending":
            return

        minutes = seconds // 60
        sec = seconds % 60
        timer_text = f"{minutes:02d}:{sec:02d}"

        try:
            await app_instance.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=p.get("caption_text", "") + f"\n\n⏳ **Time Left:** {timer_text}",
                parse_mode="Markdown"
            )
        except:
            pass
        
        await asyncio.sleep(1)
        seconds -= 1

    # TIME EXPIRED → FORCE EXPIRE
    if p["status"] == "pending":
        p["status"] = "expired"
        save_db(DB)

        # Delete message
        try:
            await app_instance.bot.delete_message(chat_id, message_id)
        except:
            pass

        # Notify user
        try:
            await app_instance.bot.send_message(
                chat_id=p["user_id"],
                text="⌛ **Payment session expired. Please try again.**"
            )
        except:
            pass

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
