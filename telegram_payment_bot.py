"""
Telegram Payment Bot (single-file)
UPGRADED: UPI uses Smart Dynamic QR (Auto-Approve).
MANUAL: Crypto & Remitly still require Admin Approval.
"""

import os
from PIL import Image, ImageDraw, ImageFont, ImageChops
import io
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

def crop_qr_from_razorpay(qr_bytes):
    """Automatically extract only the QR code from Razorpay QR images."""

    try:
        img = Image.open(io.BytesIO(qr_bytes)).convert("RGB")
        w, h = img.size

        # Convert to grayscale
        gray = img.convert("L")

        # Auto-detect the QR region by scanning darkest pixels
        threshold = 180
        mask = gray.point(lambda p: p < threshold and 255)

        # Convert mask to image
        mask = mask.convert("1")

        # Find bounding box of QR code
        bbox = mask.getbbox()

        if not bbox:
            print("QR Crop Failed: No bbox detected")
            return img  # fallback: return original

        # Expand crop box slightly (remove Razorpay text & logo)
        left, top, right, bottom = bbox

        pad = 20
        left = max(0, left - pad)
        top = max(0, top - pad)
        right = min(w, right + pad)
        bottom = min(h, bottom + pad)

        cropped = img.crop((left, top, right, bottom))

        # Remove extra white border around QR
        bg = Image.new(cropped.mode, cropped.size, (255, 255, 255))
        diff = ImageChops.difference(cropped, bg)
        bbox2 = diff.getbbox()

        if bbox2:
            cropped = cropped.crop(bbox2)

        return cropped

    except Exception as e:
        print("Crop QR Error:", e)
        return None

def generate_branded_qr(qr_bytes):
    """Creates a branded Technova QR template with the cropped QR in center."""
    try:
        qr_img = crop_qr_from_razorpay(qr_bytes)
        if qr_img is None:
           qr_img = Image.open(io.BytesIO(qr_bytes)).convert("RGB")

        # Template size
        width = 900
        height = qr_img.height + 350
        template = Image.new("RGB", (width, height), "#FFFFFF")
        draw = ImageDraw.Draw(template)

        # Colors
        brand_color = "#1D4ED8"  # Technova Blue
        text_color = "#000000"

        # Header Background
        draw.rectangle([0, 0, width, 150], fill=brand_color)

        # Load default fonts
        try:
            font_title = ImageFont.truetype("arial.ttf", 60)
            font_sub = ImageFont.truetype("arial.ttf", 36)
        except:
            font_title = ImageFont.load_default()
            font_sub = ImageFont.load_default()

        # Header Text
        draw.text((width/2, 40), "TECHNOVA STORE", fill="white", font=font_title, anchor="mm")
        draw.text((width/2, 110), "Secure UPI Payment", fill="white", font=font_sub, anchor="mm")

        # Place QR
        qr_x = (width - qr_img.width) // 2
        qr_y = 160
        template.paste(qr_img, (qr_x, qr_y))

        # Footer Section
        footer_y = qr_y + qr_img.height + 40
        draw.text((width/2, footer_y), "GPay • PhonePe • Paytm UPI", fill=text_color, font=font_sub, anchor="mm")
        draw.text((width/2, footer_y + 60), "Auto-payment verification enabled", fill="#4B5563", font=font_sub, anchor="mm")

        # Save output
        output = io.BytesIO()
        template.save(output, format="PNG")
        output.seek(0)
        return output

    except Exception as e:
        print("Branded QR Error:", e)
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
    await query.answer(cache_time=1)
    data = query.data
    user = query.from_user

    # HELP
    if data == "help":
        await query.message.reply_text("Contact help: @Dark123222_bot")
        return

    # PACKAGE SELECTION
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

    # CANCEL
    if data == "cancel":
        await query.message.reply_text("Menu closed. Use /start to reopen.")
        return

    # PAYMENT METHOD SELECTED (FIXED INDENTATION)
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

        # -------------------- UPI QR BLOCK --------------------
        if method == "upi":
            amount = SETTINGS["prices"][package]["upi"]

            # Step 1: Loading message
            msg1 = await query.message.reply_text("⏳ Creating QR code...")
            entry["loading_msg_ids"] = [msg1.message_id]

            # Step 2: Create Razorpay QR
            qr_resp = create_razorpay_smart_qr(amount, user.id, package)
            if not qr_resp:
                await msg1.edit_text("❌ System Busy. Try again later.")
                return

            entry["razorpay_qr_id"] = qr_resp["id"]
            DB["payments"].append(entry)
            save_db(DB)

            # Step 3: Update loading msg
            await msg1.edit_text("📤 Preparing QR...")
            save_db(DB)

            # Step 4: Download QR
            r = requests.get(qr_resp["image_url"], timeout=10)
            qr_bytes = r.content

            # Step 5: Branded QR
            branded_qr = generate_branded_qr(qr_bytes)
            if branded_qr:
                branded_qr.seek(0)

            caption_text = (
                f"✅ **SCAN & PAY ₹{amount}**\n"
                f"• Auto-detect payment\n"
                f"• Do NOT send screenshot\n"
            )

            # Step 6: Send QR
            qr_msg = await query.message.reply_photo(
                photo=branded_qr,
                caption=caption_text,
                parse_mode="Markdown"
            )

            entry["caption_text"] = caption_text
            entry["chat_id"] = qr_msg.chat.id
            entry["message_id"] = qr_msg.message_id
            save_db(DB)

            # Step 7: Countdown (10 min)
            COUNTDOWN_TASKS[entry["payment_id"]] = asyncio.create_task(
                start_countdown(entry["payment_id"], qr_msg.chat.id, qr_msg.message_id, 600)
            )

            return

        # -------------------- MANUAL PAYMENTS (Crypto / Remitly) --------------------
        DB["payments"].append(entry)
        save_db(DB)

        caption_text = build_manual_payment_text(package, method)
        msg2 = await query.message.reply_text(caption_text, parse_mode="Markdown")

        entry["caption_text"] = caption_text
        entry["chat_id"] = msg2.chat.id
        entry["message_id"] = msg2.message_id
        save_db(DB)

        # 30-min countdown
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
async def adminpanel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # Always answer once at the start
    await query.answer(cache_time=1)

    # Only admin access
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        await query.answer("Not allowed.", show_alert=True)
        return

    # ——————————— BUTTON ACTIONS ———————————

    if data == "admin_broadcast":
        await query.message.reply_text(
            "📢 **Broadcast Instructions**\n\n"
            "To send a broadcast message:\n"
            "• Text → `/broadcast your message`\n"
            "• Photo → Send a photo with caption `/broadcast`\n"
            "• Document → Send a document with caption `/broadcast`\n",
            parse_mode="Markdown"
        )
        return

    if data.startswith("admin_setlink_"):
        pkg = data.replace("admin_setlink_", "")
        await query.message.reply_text(
            f"Send new link for: **{pkg.upper()}**\n\n"
            "Format:\n"
            f"`/setlink {pkg} <link>`",
            parse_mode="Markdown"
        )
        return

    if data == "admin_pending":
        pendings = [p for p in DB["payments"] if p["status"] == "pending"]

        if not pendings:
            await query.answer("No pending payments.", show_alert=True)
            return

        msg = "🟡 *Pending Payments:*\n\n"
        for p in pendings:
            msg += (
                f"🆔 ID: `{p['payment_id']}`\n"
                f"👤 User: `{p['user_id']}`\n"
                f"📦 Package: *{p['package']}*\n"
                f"💳 Method: `{p['method']}`\n"
                f"———————————————\n"
            )

        await query.message.reply_text(msg, parse_mode="Markdown")
        return

    if data == "admin_close":
        await query.message.delete()
        return



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

    if method == "crypto":
        usd = SETTINGS['prices'][package]['crypto_usd']
        return (
            f"💱 **Crypto Payment Instructions**\n\n"
            f"Amount: **${usd} USDT**\n"
            f"Network: **{pi['crypto_network']}**\n\n"
            f"🔐 **Wallet Address:**\n`{pi['crypto_address']}`\n\n"
            f"📸 After payment, send a *payment screenshot* here.\n"
            f"⏳ Your payment session is active. Complete it before the timer ends."
        )

    # REMITLY
    amount_inr = SETTINGS['prices'][package]['remitly']
    return (
        f"🌍 **Remitly Payment Instructions**\n\n"
        f"Amount to Send: **₹{amount_inr} INR**\n\n"
        f"1️⃣ Select *India* as destination.\n"
        f"2️⃣ Recipient Name: **Govind Mahto**\n"
        f"3️⃣ UPI ID: **{pi['upi_id']}**\n"
        f"4️⃣ Reason: *Family Support*\n\n"
        f"📸 After sending, upload a *payment screenshot* here.\n"
        f"⏳ Your payment session is active. Complete it before the timer ends."
    )


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

                # DELETE QR MESSAGE (main QR)
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

                # DELETE loading messages ("Creating QR...", "Sending QR...")
                try:
                    if p.get("loading_msg_ids"):
                        for mid in p["loading_msg_ids"]:
                            asyncio.run_coroutine_threadsafe(
                                app_instance.bot.delete_message(p["user_id"], mid),
                                BOT_LOOP
                            )
                except Exception as e:
                    print("Loading delete error:", e)

                break

    return jsonify({"status": "ok"}), 200

# -------------------- Startup --------------------
async def start_countdown(payment_id: str, chat_id: int, message_id: int, seconds: int):
    global COUNTDOWN_TASKS

    # Find payment entry
    for p in DB["payments"]:
        if p["payment_id"] == payment_id:
            break
    else:
        return

    while seconds > 0:
        if p["status"] != "pending":
            return

        timer_text = f"{seconds//60:02d}:{seconds%60:02d}"
        new_text = p["caption_text"] + f"\n\n⏳ **Time Left:** {timer_text}"

        try:
            if p["method"] == "upi":
                # UPI → edit caption of QR photo
                await app_instance.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=new_text,
                    parse_mode="Markdown"
                )
            else:
                # Crypto & Remitly → edit text message
                await app_instance.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=new_text,
                    parse_mode="Markdown"
                )
        except:
            pass

        await asyncio.sleep(1)
        seconds -= 1

    # TIMEOUT HANDLING
    if p["status"] == "pending":
        p["status"] = "expired"
        save_db(DB)

        # Delete payment message
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
async def adminpanel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return  # Block non-admins

    text = (
        "🛠 **ADMIN PANEL**\n"
        "Manage prices, links, and payments.\n\n"
        "Available Commands:\n"
        "——————————————\n"
        "🔗 `/setlink <package> <link>`\n"
        "– Set access link for VIP / DARK / BOTH\n\n"
        "💰 `/setprice <package> <upi/crypto_usd> <value>`\n"
        "– Change prices instantly\n\n"
        "📄 `/pending`  (optional, I can add)\n"
        "– View all pending payments\n\n"
        "📊 `/stats` (optional)\n"
        "– Overview of sales\n\n"
        "⚙️ More features can be added anytime.\n"
    )

    keyboard = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Set VIP Link", callback_data="admin_setlink_vip"),
        InlineKeyboardButton("Set DARK Link", callback_data="admin_setlink_dark")
    ],
    [
        InlineKeyboardButton("Set BOTH Link", callback_data="admin_setlink_both"),
    ],
    [
        InlineKeyboardButton("Pending Payments", callback_data="admin_pending"),
    ],
    [
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
    ],
    [
        InlineKeyboardButton("Close", callback_data="admin_close")
    ]
])


    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
# -------------------- ADMIN EXTRA COMMANDS --------------------

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return

    pendings = [p for p in DB["payments"] if p["status"] == "pending"]

    if not pendings:
        await update.message.reply_text("🟡 No pending payments.")
        return

    text = "🟡 *Pending Payments:*\n\n"
    for p in pendings:
        text += (
            f"🆔 ID: `{p['payment_id']}`\n"
            f"👤 User: `{p['user_id']}`\n"
            f"📦 Package: *{p['package']}*\n"
            f"💳 Method: `{p['method']}`\n"
            f"⏱ Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p['created_at']))}\n"
            f"———————————————\n"
        )

    await update.message.reply_text(text, parse_mode="Markdown")
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return

    delivered = 0
    failed = 0

    users = set()
    for p in DB["payments"]:
        if p["status"] == "verified":
            users.add(p["user_id"])

    # CASE 1: PHOTO OR DOCUMENT BROADCAST
    if update.message.photo or update.message.document:
        caption = update.message.caption or ""
        file_obj = update.message.photo[-1] if update.message.photo else update.message.document
        file = await file_obj.get_file()

        for uid in users:
            try:
                await app_instance.bot.send_message(uid, "📢 *New Broadcast Message:*", parse_mode="Markdown")

                if update.message.photo:
                    await app_instance.bot.send_photo(uid, file.file_id, caption=caption)
                else:
                    await app_instance.bot.send_document(uid, file.file_id, caption=caption)

                delivered += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1

        return await update.message.reply_text(
            f"📢 **Broadcast Completed**\nDelivered: {delivered}\nFailed: {failed}"
        )

    # CASE 2: TEXT BROADCAST
    if context.args:
        text_to_send = " ".join(context.args)

        for uid in users:
            try:
                await app_instance.bot.send_message(uid, text_to_send, parse_mode="Markdown")
                delivered += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1

        return await update.message.reply_text(
            f"📢 **Broadcast Completed**\nDelivered: {delivered}\nFailed: {failed}"
        )

    # NO CONTENT PROVIDED
    return await update.message.reply_text("Usage:\n\n"
                                          "📌 Text → `/broadcast your message`\n"
                                          "📌 Photo → Send photo with caption `/broadcast`\n"
                                          "📌 Document → Send document with caption `/broadcast`")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return

    total_sales = len([p for p in DB["payments"] if p["status"] == "verified"])
    total_pending = len([p for p in DB["payments"] if p["status"] == "pending"])
    total_expired = len([p for p in DB["payments"] if p["status"] == "expired"])
    total_declined = len([p for p in DB["payments"] if p["status"] == "declined"])

    # INCOME
    income = 0
    for p in DB["payments"]:
        if p["status"] == "verified":
            if p["package"] == "both":
                income += SETTINGS["prices"]["both"]["upi"]
            else:
                income += SETTINGS["prices"][p["package"]]["upi"]

    text = (
        "📊 **BOT SALES STATISTICS**\n\n"
        f"✅ Verified Payments: *{total_sales}*\n"
        f"🟡 Pending Payments: *{total_pending}*\n"
        f"⛔ Declined: *{total_declined}*\n"
        f"⌛ Expired: *{total_expired}*\n\n"
        f"💰 **Total Income:** ₹{income}\n"
        "———————————————\n"
        "Use /pending to view open payments."
    )

    await update.message.reply_text(text, parse_mode="Markdown")



if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .allowed_updates(["message", "callback_query"])
        .build()
    )

    app_instance = application


    # USER COMMANDS
    application.add_handler(CommandHandler('start', start_handler))

    # ADMIN COMMANDS
    application.add_handler(CommandHandler('setlink', setlink))
    application.add_handler(CommandHandler('setprice', setprice))
    application.add_handler(CommandHandler('adminpanel', adminpanel))   # <-- ADDED

    # CALLBACK BUTTON HANDLERS
    
    application.add_handler(
    CallbackQueryHandler(callback_handler, pattern="^(choose_.*|pay_.*|cancel|help)$")
)



    application.add_handler(
    CallbackQueryHandler(admin_review_handler, pattern="^(approve|decline):")
)
    application.add_handler(
    CallbackQueryHandler(adminpanel_buttons, pattern="^admin_")
)

    application.add_handler(CommandHandler('broadcast', broadcast_cmd))
    application.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex("^/broadcast"), broadcast_cmd))
    application.add_handler(MessageHandler(filters.Document.ALL & filters.CaptionRegex("^/broadcast"), broadcast_cmd))


    # ADMIN EXTRA COMMANDS
    application.add_handler(CommandHandler('pending', pending_cmd))
    application.add_handler(CommandHandler('stats', stats_cmd))

    # MEDIA HANDLER
    application.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.ALL) & ~filters.CaptionRegex("^/broadcast"),
            message_handler
        )
    )

    application.run_polling()
