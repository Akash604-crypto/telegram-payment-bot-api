
                             
"""
Telegram Payment Bot (single-file)
UPGRADED: UPI uses Smart Dynamic QR (Auto-Approve).
MANUAL: Crypto & Remitly still require Admin Approval.
"""

import os
import aiohttp
import json
import requests
from PIL import Image
import time
import hmac
import hashlib
import threading
import asyncio
from io import BytesIO
import signal
from typing import Dict, Any
from pathlib import Path
import sys
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
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "payments.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
USERS_FILE = DATA_DIR / "users.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
AIOHTTP_SESSION = None
DB_LOCK = threading.Lock()





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
        "remitly_info": os.environ.get(
            "REMITLY_INFO",
            "Recipient: Govind Mahto | Bank Transfer | A/C: 002020391365887 | IFSC: JIOP0000001 | Reason: Family Support"
        ),

        "remitly_how_to": os.environ.get("REMITLY_HOW_TO_PAY_LINK", "https://t.me/+8jECICY--sU2MjIx"),
    }
}

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

BOT_LOOP = None


def load_users():
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return []

def load_reminders():
    if REMINDERS_FILE.exists():
        return json.loads(REMINDERS_FILE.read_text())
    return []

def save_reminders(data):
    REMINDERS_FILE.write_text(json.dumps(data, indent=2))

REMINDERS = load_reminders()


def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2))

USERS = list(set(load_users()))


def load_db():
    with DB_LOCK:
        if DB_FILE.exists():
            return json.loads(DB_FILE.read_text())
    return {"payments": []}


def save_db(db):
    with DB_LOCK:
        DB_FILE.write_text(json.dumps(db, indent=2))


def load_settings():
    if SETTINGS_FILE.exists(): return json.loads(SETTINGS_FILE.read_text())
    SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
    return DEFAULT_SETTINGS

def now_ms():
    return int(time.perf_counter() * 1000)

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



async def fetch_qr_image_bytes(url: str) -> bytes:
    if not AIOHTTP_SESSION:
        raise RuntimeError("HTTP session not ready")
    async with AIOHTTP_SESSION.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()



def crop_qr_bytes(data: bytes) -> BytesIO:
    img = Image.open(BytesIO(data)).convert("RGB")
    w, h = img.size

    # 🔐 MANDATORY SAFE CROP (UNCHANGED)
    SAFE_TOP_CROP = int(h * 0.20)
    SAFE_BOTTOM_CROP = int(h * 0.20)

    cropped = img.crop((0, SAFE_TOP_CROP, w, h - SAFE_BOTTOM_CROP))

    bio = BytesIO()
    cropped.save(bio, format="JPEG", quality=88)
    bio.seek(0)
    return bio

# -------------------- Bot Handlers --------------------
def conversion_stats(days=None):
    """
    days = None  -> all time
    days = 0     -> today
    days = 7     -> last 7 days
    days = 30    -> last 30 days
    """
    now = int(time.time())

    def in_range(p):
        if p["status"] != "verified":
            return False
        # ✅ COUNT ONLY REMINDER-BASED PURCHASES
        if not p.get("from_reminder"):
            return False      
        if days is None:
            return True
        if days == 0:
            today_start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
            return p["created_at"] >= today_start

        return p["created_at"] >= now - days * 86400

    stats = {
        "upi": {"vip": 0, "dark": 0, "both": 0},
        "manual": {"vip": 0, "dark": 0, "both": 0},
    }

    for p in DB["payments"]:
        if not in_range(p):
            continue

        method = "upi" if p["method"] == "upi" else "manual"
        pkg = p["package"]

        if pkg in stats[method]:
            stats[method][pkg] += 1

    return stats

def clear_user_reminders(user_id):
    global REMINDERS
    REMINDERS = [r for r in REMINDERS if r["user_id"] != user_id]
    save_reminders(REMINDERS)

def get_buyer_ids():
    return {p["user_id"] for p in DB["payments"] if p["status"] == "verified"}

def get_nonbuyer_ids():
    buyers = get_buyer_ids()
    return {uid for uid in USERS if uid not in buyers}

app = Flask(__name__)
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")

def main_keyboard():
    kb = [
        [InlineKeyboardButton("🔥 VIP", callback_data="choose_vip")],
        [InlineKeyboardButton("🌑 DARK", callback_data="choose_dark")],
        [InlineKeyboardButton("💥 BOTH (30% off)", callback_data="choose_both")],
        [InlineKeyboardButton("📊 Check Payment Status", callback_data="status_btn")],
        [InlineKeyboardButton("🆘 HELP", callback_data="help")],
    ]
    return InlineKeyboardMarkup(kb)

    
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id not in USERS:
        USERS.append(user.id)
        USERS[:] = list(set(USERS))
        save_users(USERS)



    name = user.first_name or "there"

    text = (
    f"Welcome {name} 👋\n\n"
    "Select a package below to proceed with secure access."
    )


    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(),
    )
async def cleanup_previous_pending_payments(user_id, context):
    for p in DB["payments"]:
        if p["user_id"] == user_id and p["status"] == "pending":

            # stop countdown
            task = COUNTDOWN_TASKS.get(p["payment_id"])
            if task:
                task.cancel()
                COUNTDOWN_TASKS.pop(p["payment_id"], None)

            # delete payment message
            try:
                if p.get("chat_id") and p.get("message_id"):
                    await context.bot.delete_message(
                        p["chat_id"], p["message_id"]
                    )
            except Exception as e:
                print("Ignored error:", e)


            # delete loading messages
            try:
                for mid in p.get("loading_msg_ids", []):
                    await context.bot.delete_message(user_id, mid)
            except Exception as e:
                print("Ignored error:", e)

            # expire payment
            p["status"] = "expired"
            break
    save_db(DB)
    
async def ux_progress(bot, chat_id, msg_id):
    try:
        await bot.edit_message_text(
            "🔄 Connecting to UPI gateway…",
            chat_id=chat_id,
            message_id=msg_id
        )
        await asyncio.sleep(1)

        await bot.edit_message_text(
            "📡 Verifying payment channel…",
            chat_id=chat_id,
            message_id=msg_id
        )
        await asyncio.sleep(1)

        await bot.edit_message_text(
            "🖼 Preparing QR image…",
            chat_id=chat_id,
            message_id=msg_id
        )
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def handle_payment(method, package, query, context, from_reminder=False):
    user = query.from_user
    
    # 🔥 CLEAN ALL PREVIOUS PENDING PAYMENTS (QR / MANUAL / COUNTDOWN)
    await cleanup_previous_pending_payments(user.id, context)

    entry = {
        "payment_id": f"p_{int(time.time()*1000)}",
        "user_id": user.id,
        "username": user.username or "",
        "package": package,
        "method": method,
        "status": "pending",
        "created_at": int(time.time()),
        "from_reminder": from_reminder,
    }
    


    # ---------- UPI ----------
    if method == "upi":
        amount = SETTINGS["prices"][package]["upi"]

        msg1 = await query.message.reply_text("⚡ Generating secure UPI QR…")
        entry["loading_msg_ids"] = [msg1.message_id]
        ux_task = asyncio.create_task(
            ux_progress(
                context.bot,
                msg1.chat.id,
                msg1.message_id
            )
        )



        caption_text = (
            f"✅ **SCAN & PAY ₹{amount}**\n"
            f"• Auto-detect payment\n"
            f"• Do NOT send screenshot\n"
        )
        loop = asyncio.get_running_loop()
        t0 = now_ms()
        print(f"[TIMING] total_start               +0 ms")

        # 1️⃣ Razorpay QR creation
        t1 = now_ms()
        qr_resp = await loop.run_in_executor(
            None,
            create_razorpay_smart_qr,
            amount,
            user.id,
            package
        )
        if ux_task and not ux_task.done():
            ux_task.cancel()
        t2 = now_ms()
        print(f"[TIMING] razorpay_qr_created       +{t2 - t1} ms")
        
        if not qr_resp or "image_url" not in qr_resp:
            if ux_task and not ux_task.done():
                ux_task.cancel()
            entry["status"] = "expired"
            DB["payments"].append(entry)
            save_db(DB)
            await query.message.reply_text("❌ Failed to generate UPI QR. Please try again.")
            return
        
        # 2️⃣ QR image download
        t3 = now_ms()
        # 🔥 async download (non-blocking)
        qr_raw = await fetch_qr_image_bytes(qr_resp["image_url"])
        t4 = now_ms()
        print(f"[TIMING] qr_image_downloaded       +{t4 - t3} ms")
        
        # 3️⃣ QR crop
        t5 = now_ms()
        # 🧠 mandatory crop (executor)
        qr_bytes = await loop.run_in_executor(
           None,
           crop_qr_bytes,
           qr_raw
        )
        t6 = now_ms()
        print(f"[TIMING] qr_image_cropped          +{t6 - t5} ms")
        
        # 4️⃣ Telegram upload
        t7 = now_ms()
        qr_msg = await query.message.reply_photo(
            photo=qr_bytes,
            caption=caption_text,
            parse_mode="Markdown"
        )
        
        # 🧹 Delete loading / UX message immediately (optional UX polish)
        try:
            await context.bot.delete_message(
                chat_id=msg1.chat.id,
                message_id=msg1.message_id
            )
        except:
            pass


        t8 = now_ms()
        print(f"[TIMING] telegram_photo_sent       +{t8 - t7} ms")

        print(
            f"[TIMING][user={user.id}][{package}] TOTAL = {t8 - t0} ms"
        )


        entry["razorpay_qr_id"] = qr_resp["id"]   # REQUIRED for webhook match
        DB["payments"].append(entry)  
        entry["caption_text"] = caption_text
        entry["chat_id"] = qr_msg.chat.id
        entry["message_id"] = qr_msg.message_id
        save_db(DB)

        COUNTDOWN_TASKS[entry["payment_id"]] = asyncio.create_task(
            start_countdown(entry["payment_id"], qr_msg.chat.id, qr_msg.message_id, 600)
        )
        return

    # ---------- MANUAL ----------
    DB["payments"].append(entry)
    save_db(DB)

    caption_text = build_manual_payment_text(package, method)

    msg = await query.message.reply_text(caption_text, parse_mode="Markdown")
    entry["caption_text"] = caption_text
    entry["chat_id"] = msg.chat.id
    entry["message_id"] = msg.message_id
    save_db(DB)

    COUNTDOWN_TASKS[entry["payment_id"]] = asyncio.create_task(
        start_countdown(entry["payment_id"], msg.chat.id, msg.message_id, 1800)
    )
    return

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    # ----- HELP -----
    if data == "help":
        await query.message.reply_text("Contact help: @Dark123222_bot")
        return

    # ----- STATUS BUTTON -----
    if data == "status_btn":
        return await status_handler(update, context)




    # ----- PACKAGE SELECTION -----
    if data.startswith("choose_"):
        # 🔥 CLEAN OLD PENDING PAYMENTS WHEN SWITCHING PACKAGE
        await cleanup_previous_pending_payments(user.id, context)

        # 🚫 BLOCK IF USER ALREADY PAID FOR THIS PACKAGE
        package = data.split("_")[1]

        if any(
            p["user_id"] == user.id and
            p["package"] == package and
            p["status"] == "verified"
            for p in DB["payments"]
        ):
            # ✅ SAFE to clear reminders here
            clear_user_reminders(user.id)
  
            await send_link_to_user(user.id, package)
            return

        
        clear_user_reminders(user.id)
        REMINDERS.append({
            "user_id": user.id,
            "package": data.split("_")[1],
            "intent": "package_clicked",
            "created_at": int(time.time()),
            "sent": [],
            "touched": False,   # ✅ ADD THIS
            "clicked_from_reminder": False
        })
        save_reminders(REMINDERS)

        package = data.split("_")[1]
        kb = [
            [InlineKeyboardButton(f"💸 UPI (Fast/Auto) - ₹{SETTINGS['prices'][package]['upi']}",
                                  callback_data=f"pay_upi:{package}")],
            [InlineKeyboardButton(f"🪙 Crypto - ${SETTINGS['prices'][package]['crypto_usd']}",
                                  callback_data=f"pay_crypto:{package}")],
            [InlineKeyboardButton(f"🌍 Remitly - ₹{SETTINGS['prices'][package]['remitly']}",
                                  callback_data=f"pay_remitly:{package}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
        await query.message.reply_text(
            f"Select Payment Method for {package.upper()}",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    # ----- CANCEL -----
    if data == "cancel":
        clear_user_reminders(user.id)
        # stop countdowns & clean messages
        for p in DB["payments"]:
            if p["user_id"] == user.id and p["status"] == "pending":
            
                # stop countdown
                task = COUNTDOWN_TASKS.get(p["payment_id"])
                if task:
                    task.cancel()
                    COUNTDOWN_TASKS.pop(p["payment_id"], None)

                # delete payment message (QR or manual)
                try:
                    if p.get("chat_id") and p.get("message_id"):
                        await context.bot.delete_message(
                            p["chat_id"], p["message_id"]
                        )
                except:
                    pass
                # delete loading messages (Creating QR / Sending QR)
                try:
                    for mid in p.get("loading_msg_ids", []):
                        await context.bot.delete_message(user.id, mid)
                except:
                    pass


                # mark payment as cancelled
                p["status"] = "expired"

        save_db(DB)

        await query.message.reply_text(
            "❌ Payment cancelled.\n\nUse /start to begin again."
        )
        return


    
    # ----- REMINDER PAYMENT BUTTON -----
    if data.startswith("reminder_pay_"):
        part, package = data.split(":")
        method = part.replace("reminder_pay_", "")

        for r in REMINDERS:
            if r["user_id"] == user.id:
                r["clicked_from_reminder"] = True
                r["intent"] = "upi_clicked" if method == "upi" else "manual_clicked"
                save_reminders(REMINDERS)
                break

        return await handle_payment(
            method=method,
            package=package,
            query=query,
            context=context,
            from_reminder=True
        )


    # ----- PAYMENT METHOD SELECTED -----
    if data.startswith("pay_"):
        method, package = data.split(":")
        method = method.replace("pay_", "")
        for r in REMINDERS:
            if r["user_id"] == user.id:
                r["intent"] = "upi_clicked" if method == "upi" else "manual_clicked"
                save_reminders(REMINDERS)
                break

        return await handle_payment(
            method=method,
            package=package,
            query=query,
            context=context,
            from_reminder=False
        )




async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id

    # USER SENT PHOTO OR DOCUMENT
    if msg.photo or msg.document:

        for p in reversed(DB["payments"]):

            if p["user_id"] == user_id and p["status"] == "pending" and p["method"] in ("crypto", "remitly"):

                # -------- DELETE OLD PAYMENT INSTRUCTION MESSAGE ----------
                try:
                    old_chat = p.get("chat_id")
                    old_msg = p.get("message_id")
                    if old_chat and old_msg:
                        await context.bot.delete_message(old_chat, old_msg)
                except Exception as e:
                    print("Failed to delete old instruction message:", e)

                # -------- STOP COUNTDOWN ----------
                task = COUNTDOWN_TASKS.get(p["payment_id"])
                if task:
                    task.cancel()
                    COUNTDOWN_TASKS.pop(p["payment_id"], None)

                # -------- UPDATE STATUS TO UNDER REVIEW ----------
                p["status"] = "review"
                save_db(DB)

                # -------- SAVE PROOF FILE ----------
                file_obj = msg.photo[-1] if msg.photo else msg.document
                file = await file_obj.get_file()
                save_path = DATA_DIR / f"proof_{user_id}_{int(time.time())}.jpg"
                await file.download_to_drive(str(save_path))
                p.setdefault("proof_files", []).append(str(save_path))
                save_db(DB)

                # -------- FORWARD TO ADMIN ----------
                buttons = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ APPROVE", callback_data=f"approve:{p['payment_id']}"),
                        InlineKeyboardButton("❌ DECLINE", callback_data=f"decline:{p['payment_id']}")
                    ]
                ])

                await context.bot.send_photo(
                    chat_id=SETTINGS["admin_chat_id"],
                    photo=open(save_path, "rb"),
                    caption=f"🔎 UNDER REVIEW\nUser: {user_id}\nPackage: {p['package']}",
                    reply_markup=buttons
                )

                # -------- AUTO-DELETE USER'S UPLOADED SCREENSHOT ----------
                try:
                    await context.bot.delete_message(chat_id=user_id, message_id=msg.message_id)
                except:
                    pass

                # -------- SEND UNDER REVIEW MESSAGE TO USER ----------
                return await context.bot.send_message(
                    chat_id=user_id,
                    text="⏳ **Payment Under Review**\nAdmin is verifying your proof..."
                )



def is_admin(update):
    if update.effective_user:
        return update.effective_user.id == SETTINGS["admin_chat_id"]
    return False

def shutdown():
    if AIOHTTP_SESSION and BOT_LOOP:
        asyncio.run_coroutine_threadsafe(
            AIOHTTP_SESSION.close(),
            BOT_LOOP
        )

# -------------------- Admin Command Functions (Preserved) --------------------

# reminder_analytics_from_button
async def reminder_analytics_from_button(query):
    # ---- OLD REMINDER METRICS ----
    total_users = len(set(r["user_id"] for r in REMINDERS))
    package_clicked = sum(1 for r in REMINDERS if r["intent"] == "package_clicked")
    upi_clicked = sum(1 for r in REMINDERS if r["intent"] == "upi_clicked")
    manual_clicked = sum(1 for r in REMINDERS if r["intent"] == "manual_clicked")
    reminders_sent = sum(len(r["sent"]) for r in REMINDERS)

    # ---- NEW CONVERSION METRICS ----
    today = conversion_stats(days=0)
    week = conversion_stats(days=7)
    month = conversion_stats(days=30)
    total = conversion_stats(days=None)

    def block(title, data):
        return (
            f"**{title}**\n"
            f"UPI → VIP: {data['upi']['vip']} | DARK: {data['upi']['dark']} | BOTH: {data['upi']['both']}\n"
            f"MANUAL → VIP: {data['manual']['vip']} | DARK: {data['manual']['dark']} | BOTH: {data['manual']['both']}\n"
        )

    text = (
        "📊 **REMINDER & CONVERSION ANALYTICS**\n\n"
        "🔔 **Reminder Funnel**\n"
        f"👥 Active Users: *{total_users}*\n"
        f"📦 Package Clicked: *{package_clicked}*\n"
        f"💸 UPI Clicked: *{upi_clicked}*\n"
        f"🪙 Manual Clicked: *{manual_clicked}*\n"
        f"📨 Reminders Sent: *{reminders_sent}*\n\n"
        "———————————————\n\n"
        "💰 **Conversions**\n\n"
        + block("📅 Today", today) + "\n"
        + block("📆 Last 7 Days", week) + "\n"
        + block("🗓 Last 30 Days", month) + "\n"
        + block("📦 Total (All Time)", total)
    )

    await query.message.reply_text(text, parse_mode="Markdown")



# Stats (button-safe)
async def stats_cmd_from_button(query, context):
    total_users = len(USERS)
    total_sales = len([p for p in DB["payments"] if p["status"] == "verified"])
    total_pending = len([p for p in DB["payments"] if p["status"] == "pending"])
    total_expired = len([p for p in DB["payments"] if p["status"] == "expired"])
    total_declined = len([p for p in DB["payments"] if p["status"] == "declined"])

    income = 0
    for p in DB["payments"]:
        if p["status"] == "verified":
            price = SETTINGS["prices"].get(p["package"], {}).get("upi")
            if price:
                income += price


    text = (
        "📊 **BOT SALES STATISTICS**\n\n"
        f"👥 Users: {total_users}\n"
        f"✅ Sales: {total_sales}\n"
        f"🟡 Pending: {total_pending}\n"
        f"⛔ Declined: {total_declined}\n"
        f"⌛ Expired: {total_expired}\n\n"
        f"💰 Income: ₹{income}"
    )

    await query.message.reply_text(text, parse_mode="Markdown")

async def adminpanel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # Only admin access
    if query.from_user.id != SETTINGS["admin_chat_id"]:
        await query.answer("Not allowed.", show_alert=True)
        return

    # ——————————— BUTTON ACTIONS ———————————

    # Broadcast Instructions
    if data == "admin_broadcast":
        await query.message.reply_text(
            "📢 **Broadcast Instructions**\n\n"
            "Use the following commands:\n\n"
            "🌍 `/broadcast_all your message`\n"
            "– Send to ALL users\n\n"
            "💰 `/broadcast_buyers your message`\n"
            "– Send to VERIFIED buyers only\n\n"
            "❌ `/broadcast_nonbuyers your message`\n"
            "– Send to NON-buyers\n\n"
            "📸 For photo/document:\n"
            "Send media with caption:\n"
            "`/broadcast_all`\n"
            "`/broadcast_buyers`\n"
            "`/broadcast_nonbuyers`\n",
            parse_mode="Markdown"
        )
        await query.answer()
        return
    
    # stats button
    if data == "admin_stats":
        if not is_admin(update):
            return
        await stats_cmd_from_button(query, context)
        return


    # Reminder analytics button
    if data == "admin_reminder_analytics":
        if not is_admin(update):
            return
        await reminder_analytics_from_button(query)
        return

    
    # Stop ALL reminders (admin only)
    if data == "admin_stop_all_reminders":
       global REMINDERS
       REMINDERS.clear()
       save_reminders(REMINDERS)
       await query.message.reply_text("🔕 All reminders stopped successfully.")
       await query.answer()
       return
    
    # Restart reminders (admin)
    if data == "admin_restart_reminders":
       await query.message.reply_text(
           "🔔 Reminder system is active.\n"
           "New reminders will start when users click packages again."
       )
       await query.answer()
       return

    # Set Link Buttons
    if data.startswith("admin_setlink_"):
        pkg = data.replace("admin_setlink_", "")
        await query.message.reply_text(
            f"Send new link for: **{pkg.upper()}**\n\n"
            "Format:\n"
            f"`/setlink {pkg} <link>`",
            parse_mode="Markdown"
        )
        await query.answer()
        return

    # Show Pending Payments
    if data == "admin_pending":
        pendings = [p for p in DB["payments"] if p["status"] == "pending"]

        if not pendings:
            await query.message.reply_text("🟡 No pending payments.")
            await query.answer()

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
        await query.answer()
        return

    # Close Admin Panel
    if data == "admin_close":
        await query.message.delete()
        await query.answer()
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

    # STOP countdown if exists
    task = COUNTDOWN_TASKS.get(pay_id)
    if task:
        task.cancel()
        COUNTDOWN_TASKS.pop(pay_id, None)

    # FIND PAYMENT RECORD
    for p in DB["payments"]:
        if p["payment_id"] == pay_id:

            user_id = p["user_id"]
            package = p["package"]

            # Detect amount
            if p["method"] == "crypto":
                amount = f"${SETTINGS['prices'][package]['crypto_usd']}"
            else:
                amount = f"₹{SETTINGS['prices'][package]['upi']}"

            # -------------------- APPROVE --------------------
            if action == "approve":

                # Must be under review
                if p["status"] != "review":
                    await query.answer("Payment is not under review.", show_alert=True)
                    return

                p["status"] = "verified"
                

                save_db(DB)

                # Update admin message
                try:
                    await query.edit_message_caption(
                        caption=(
                            f"✅ Approved Payment\n"
                            f"User: {user_id}\n"
                            f"Package: {package.upper()}\n"
                            f"Amount: {amount}"
                        ),
                        reply_markup=None
                    )
                except:
                    await query.edit_message_text(
                        f"✅ Approved Payment\nUser: {user_id}\nPackage: {package.upper()}\nAmount: {amount}",
                        reply_markup=None
                    )

                # SEND ACCESS LINK
                await send_link_to_user(user_id, package)

                # Notify admin
                await context.bot.send_message(
                    SETTINGS["admin_chat_id"],
                    f"✅ Payment Approved (ID: {pay_id}) | User: {user_id} | Amount: {amount}"
                )
                return


            # -------------------- DECLINE --------------------
            if action == "decline":

                # Must be under review
                if p["status"] != "review":
                    await query.answer("Payment is not under review.", show_alert=True)
                    return

                p["status"] = "declined"
                save_db(DB)

                # Update admin message
                try:
                    await query.edit_message_caption(
                        caption=(
                            f"❌ Declined Payment\n"
                            f"User: {user_id}\n"
                            f"Package: {package.upper()}\n"
                            f"Amount: {amount}"
                        ),
                        reply_markup=None
                    )
                except:
                    await query.edit_message_text(
                        f"❌ Declined Payment\nUser: {user_id}\nPackage: {package.upper()}\nAmount: {amount}",
                        reply_markup=None
                    )

                # Notify user
                await context.bot.send_message(
                    user_id,
                    "❌ Payment Declined.\nPlease send correct proof or try again."
                )

                # Notify admin
                await context.bot.send_message(
                    SETTINGS["admin_chat_id"],
                    f"❌ Payment Declined (ID: {pay_id}) | User: {user_id} | Amount: {amount}"
                )
                return




async def send_link_to_user(user_id: int, package: str):
    if package == "both":
        vip_link = SETTINGS["links"].get("vip")
        dark_link = SETTINGS["links"].get("dark")

        # ✅ handle empty or missing links safely
        if not vip_link:
            vip_link = "VIP link not set. Contact admin."
        if not dark_link:
            dark_link = "DARK link not set. Contact admin."

        text = (
            "🎉 **Access Granted: BOTH Package**\n\n"
            "Here are your links:\n\n"
            f"🔹 **VIP Access:**\n{vip_link}\n\n"
            f"🔹 **DARK Access:**\n{dark_link}"
        )

        await app_instance.bot.send_message(
            chat_id=user_id,
            text=text,
        )
        return

    # ✅ Normal single package (VIP or DARK)
    link = SETTINGS["links"].get(package)
    if not link:
        link = "Link not set. Contact admin."

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

    # ✅ UPDATED REMITLY INSTRUCTIONS
    amount_inr = SETTINGS['prices'][package]['remitly']
    return (
        f"🌍 **Remitly Payment Instructions**\n\n"
        f"Amount to Send: **₹{amount_inr} INR**\n\n"
        f"📘 How to Pay Guide:\n{pi['remitly_how_to']}\n\n"
        f"1️⃣ Create an account on *Remitly* (App / Website)\n"
        f"2️⃣ Select destination: **India** and enter amount\n"
        f"3️⃣ Choose Delivery Method: **Bank**\n"
        f"4️⃣ Recipient Name: **Govind Mahto**\n"
        f"5️⃣ Bank Account No: **002020391365887**\n"
        f"   IFSC Code: **JIOP0000001**\n"
        f"6️⃣ Reason for Payment: **Family Support**\n\n"
        f"📸 After sending payment, upload a *payment screenshot* here.\n"
        f"⏳ Your payment session is active. Complete it before the timer ends."
    )



# -------------------- Webhook (Auto-Approve UPI) --------------------
@app.route('/razorpay_webhook', methods=['POST'])
def razorpay_webhook():

    # ---------------- SIGNATURE VERIFICATION ----------------
    received_sig = request.headers.get("X-Razorpay-Signature", "")
    body = request.data

    calc_sig = hmac.new(
        bytes(RAZORPAY_WEBHOOK_SECRET, 'utf-8'),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(received_sig, calc_sig):
        print("❌ Invalid Razorpay Signature")
        return jsonify({"status": "invalid signature"}), 400

    # ---------------- VALIDATED PAYLOAD ----------------
    data = request.json

    if data.get('event') == 'qr_code.credited':
        qr_entity = data['payload']['qr_code']['entity']
        qr_id = qr_entity['id']
        user_id = int(qr_entity['notes']['user_id'])
        package = qr_entity['notes']['package']

        for p in DB["payments"]:
            if p.get("razorpay_qr_id") == qr_id and p["status"] == "pending":
                
                p["status"] = "verified"
               

                clear_user_reminders(user_id)
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
                    text=new_text
                )
        except Exception as e:
            print("Ignored error:", e)

        await asyncio.sleep(30)
        seconds -= 30


    # TIMEOUT HANDLING
    if p["status"] == "pending":
        p["status"] = "expired"
        save_db(DB)

        # Delete payment message
        try:
            await app_instance.bot.delete_message(chat_id, message_id)
        except Exception as e:
            print("Ignored error:", e)

        # Notify user
        try:
            await app_instance.bot.send_message(
                chat_id=p["user_id"],
                text="⌛ **Payment session expired. Please try again.**"
            )
        except Exception as e:
            print("Ignored error:", e)



async def post_init(application):
    global BOT_LOOP, AIOHTTP_SESSION
    BOT_LOOP = asyncio.get_running_loop()
    AIOHTTP_SESSION = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        connector=aiohttp.TCPConnector(
            limit=10,
            ttl_dns_cache=300,
        )
    )
    asyncio.create_task(reminder_loop())


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
        InlineKeyboardButton("🔗 Set VIP Link", callback_data="admin_setlink_vip"),
        InlineKeyboardButton("🔗 Set DARK Link", callback_data="admin_setlink_dark"),
    ],
    [
        InlineKeyboardButton("🔗 Set BOTH Link", callback_data="admin_setlink_both"),
    ],
    [
        InlineKeyboardButton("🟡 Pending Payments", callback_data="admin_pending"),
        InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
    ],
    [
        InlineKeyboardButton("📊 Reminder Analytics", callback_data="admin_reminder_analytics"),
    ],
    [
        InlineKeyboardButton("🔕 Stop All Reminders", callback_data="admin_stop_all_reminders"),
        InlineKeyboardButton("🔔 Restart Reminders", callback_data="admin_restart_reminders"),
    ],
    [
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
    ],
    [
        InlineKeyboardButton("❌ Close", callback_data="admin_close"),
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



async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return
    total_users = len(USERS) 
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
        f"👥 **Total Users Started Bot:** {total_users}\n\n"
        f"✅ Verified Payments: *{total_sales}*\n"
        f"🟡 Pending Payments: *{total_pending}*\n"
        f"⛔ Declined: *{total_declined}*\n"
        f"⌛ Expired: *{total_expired}*\n\n"
        f"💰 **Total Income:** ₹{income}\n"
        "———————————————\n"
        "Use /pending to view open payments."
    )

    await update.message.reply_text(text, parse_mode="Markdown")

    
async def reminder_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    before = len(REMINDERS)

    clear_user_reminders(user_id)

    after = len(REMINDERS)

    if before == after:
        await update.message.reply_text(
            "ℹ️ You don’t have any active reminders."
        )
    else:
        await update.message.reply_text(
            "🔕 **Reminders stopped successfully.**\n\n"
            "You won’t receive payment reminders anymore.\n"
            "You can restart anytime using /reminder_start",
            parse_mode="Markdown"
        )

async def reminder_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # If already paid, no reminders
    if any(p["user_id"] == user_id and p["status"] == "verified" for p in DB["payments"]):
        return await update.message.reply_text(
            "✅ You already have access. No reminders needed."
        )

    clear_user_reminders(user_id)

    await update.message.reply_text(
        "🔔 Reminders enabled.\n\nUse /start and select a package to continue."
    )
    return



async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Determine where to reply
    if update.callback_query:
        user_id = update.callback_query.from_user.id
        reply_func = update.callback_query.message.reply_text
    else:
        user_id = update.effective_user.id
        reply_func = update.message.reply_text

    # Find latest payment
    user_payments = [p for p in DB["payments"] if p["user_id"] == user_id]
    if not user_payments:
        return await reply_func("❌ No payment found. Start with /start")

    p = user_payments[-1]

    status_map = {
        "pending": "🟡 Pending (Waiting for your payment)",
        "review": "🟠 Under Review by Admin",
        "verified": "🟢 Verified — Access Granted",
        "declined": "🔴 Declined — Submit correct proof",
        "expired": "⚫ Expired — Start again",
    }

    text = (
        "📄 **Your Payment Status**\n\n"
        f"📦 Package: *{p['package'].upper()}*\n"
        f"💳 Method: *{p['method']}*\n"
        f"🧾 Status: {status_map.get(p['status'], 'Unknown')}\n"
        f"⏱ Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p['created_at']))}"
    )

    await reply_func(text, parse_mode="Markdown")
    
async def broadcast_to_users(bot, user_ids, update, context):
    delivered = 0
    failed = 0

    for uid in user_ids:
        try:
            # PHOTO
            if update.message.photo:
                await bot.send_photo(
                    uid,
                    update.message.photo[-1].file_id,
                    caption=(update.message.caption or "").replace("/broadcast_all", "")
                    .replace("/broadcast_buyers", "")
                    .replace("/broadcast_nonbuyers", "").strip()

                )

            # DOCUMENT
            elif update.message.document:
                await bot.send_document(
                    uid,
                    update.message.document.file_id,
                    caption=(
                        (update.message.caption or "")
                        .replace("/broadcast_all", "")
                        .replace("/broadcast_buyers", "")
                        .replace("/broadcast_nonbuyers", "")
                        .strip()
                    )
                )



            
            # TEXT (preserve new lines)
            else:
                text = update.message.text
                if not text:
                    continue

                # remove only the broadcast command
                text = (
                    text.replace("/broadcast_all", "")
                        .replace("/broadcast_buyers", "")
                        .replace("/broadcast_nonbuyers", "")
                        .strip()
                )

                await bot.send_message(uid, text)


            delivered += 1
            await asyncio.sleep(0.05)

        except Exception as e:
            failed += 1
            print(f"Broadcast failed to {uid}: {e}")

    await update.message.reply_text(
        f"📢 **Broadcast Completed**\n"
        f"Delivered: {delivered}\n"
        f"Failed: {failed}",
        parse_mode="Markdown"
    )


async def broadcast_all(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return
    await broadcast_to_users(app_instance.bot, USERS, update, context)
    
    
async def broadcast_buyers(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return
    await broadcast_to_users(app_instance.bot, get_buyer_ids(), update, context)
    
    
async def broadcast_nonbuyers(update, context):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return
    await broadcast_to_users(app_instance.bot, get_nonbuyer_ids(), update, context)

async def setremitlyhowto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SETTINGS["admin_chat_id"]:
        return

    if not context.args:
        return await update.message.reply_text(
            "Usage:\n/setremitlyhowto <link>"
        )

    SETTINGS["payment_info"]["remitly_how_to"] = context.args[0]
    save_settings(SETTINGS)

    await update.message.reply_text("✅ Remitly how-to-pay link updated successfully.")
def get_due_reminders(r):
    now = int(time.time())
    base = r["created_at"]

    schedules = {
        "package_clicked": [1800, 14400],   # 30 min, 4 hr
        "upi_clicked": [600, 7200],          # 10 min, 2 hr
        "manual_clicked": [3600, 14400],     # 1 hr, 4 hr
    }

    due = []

    for i, sec in enumerate(schedules.get(r["intent"], []), start=1):
        if i not in r["sent"] and now >= base + sec:
            due.append(i)

    # Next day 11 PM
    if 3 not in r["sent"]:
        t = time.localtime(base)
        next_day_11 = int(time.mktime((
            t.tm_year, t.tm_mon, t.tm_mday + 1,
            23, 0, 0, 0, 0, -1
        )))
        if now >= next_day_11:
            due.append(3)

    return due
REMINDER_MESSAGES = {
    "package_clicked": {
        1: "👋 Hey!\n\nYou opened *{pkg}* a little while ago.\nMost users finish this step in under a minute.\n\nTap the button below to continue 👇",
        2: "🧠 Quick check-in\n\nYou explored *{pkg}* earlier.\nAccess is still open.\n\nTap the button below to continue 👇",
        3: "🌙 Good evening\n\nYou checked *{pkg}* earlier.\nNo pressure at all.\n\nTap the button below to continue 👇",
    },
    "upi_clicked": {
        1: "⚡ Just a reminder\n\nYou chose UPI for *{pkg}*.\nUPI is instant & auto-approved.\n\nTap the button below to continue 👇",
        2: "🔔 Still interested?\n\nUPI is the fastest way to unlock *{pkg}*.\nNo screenshots needed.\n\nTap the button below to continue 👇",
        3: "🌙 Ending the day note\n\nYour UPI option for *{pkg}* is still available.\n\nTap the button below to continue 👇",
    },
    "manual_clicked": {
        1: "📌 Heads up\n\nYour *{pkg}* payment session is active.\nAfter payment, upload the screenshot here.\n\nTap the button below to continue 👇",
        2: "🛠 Need assistance?\n\nManual payments take a little longer.\nWe’re here if you need help.\n\nTap the button below to continue 👇",
        3: "🌙 Final check\n\nIf now isn’t the right time, that’s okay.\n\nYou can return anytime → Tap the button below to continue 👇",
    }
}

async def reminder_loop():
    global REMINDERS
    while True:
        for r in REMINDERS[:]:

            # Stop if user already paid or under review
            if any(
                p["user_id"] == r["user_id"] and p["status"] in ("review", "verified")
                for p in DB["payments"]
            ):
                clear_user_reminders(r["user_id"])
                continue

            due = get_due_reminders(r)

            for step in due:
                try:
                    msg = REMINDER_MESSAGES.get(r["intent"], {}).get(step)

                    buttons = []

                    # PACKAGE CLICKED → show all
                    if r["intent"] == "package_clicked":
                        buttons = [
                            [InlineKeyboardButton("💸 Pay via UPI", callback_data=f"reminder_pay_upi:{r['package']}")],
                            [InlineKeyboardButton("🪙 Crypto", callback_data=f"reminder_pay_crypto:{r['package']}")],
                            [InlineKeyboardButton("🌍 Remitly", callback_data=f"reminder_pay_remitly:{r['package']}")]
                        ]

                    # UPI CLICKED → UPI only
                    elif r["intent"] == "upi_clicked":
                        buttons = [
                            [InlineKeyboardButton("💸 Pay via UPI", callback_data=f"reminder_pay_upi:{r['package']}")]
                        ]

                    # MANUAL CLICKED → Crypto + Remitly
                    elif r["intent"] == "manual_clicked":
                        buttons = [
                            [InlineKeyboardButton("🪙 Crypto", callback_data=f"reminder_pay_crypto:{r['package']}")],
                            [InlineKeyboardButton("🌍 Remitly", callback_data=f"reminder_pay_remitly:{r['package']}")]
                        ]

                    await app_instance.bot.send_message(
                        r["user_id"],
                        msg.format(pkg=r["package"].upper()),
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode="Markdown"
                    )

                    r["touched"] = True



                    r["sent"].append(step)
                    save_reminders(REMINDERS)
                except Exception as e:
                    print("Ignored error:", e)


        await asyncio.sleep(300)  # check every 5 minutes


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app_instance = application

    # USER COMMANDS
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("status", status_handler))
    application.add_handler(CommandHandler("reminder_cancel", reminder_cancel))
    application.add_handler(CommandHandler("reminder_start", reminder_start))

    # ADMIN COMMANDS
    application.add_handler(CommandHandler("setlink", setlink))
    application.add_handler(CommandHandler("setprice", setprice))
    application.add_handler(CommandHandler("adminpanel", adminpanel))
    application.add_handler(CommandHandler("pending", pending_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("broadcast_all", broadcast_all))
    application.add_handler(CommandHandler("broadcast_buyers", broadcast_buyers))
    application.add_handler(CommandHandler("broadcast_nonbuyers", broadcast_nonbuyers))
    application.add_handler(CommandHandler("setremitlyhowto", setremitlyhowto))
    signal.signal(signal.SIGTERM, lambda s, f: shutdown())
    signal.signal(signal.SIGINT, lambda s, f: shutdown())

    # CALLBACKS
    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
            pattern="^(choose_.*|pay_.*|reminder_pay_.*|cancel|help|status_btn)$"
        )
    )
    application.add_handler(CallbackQueryHandler(admin_review_handler, pattern="^(approve|decline):"))
    application.add_handler(CallbackQueryHandler(adminpanel_buttons, pattern="^admin_"))

    # MEDIA HANDLERS
    application.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.ALL) & ~filters.CaptionRegex("^/broadcast_"),
            message_handler
        )
    )

    application.add_handler(
        MessageHandler(filters.PHOTO & filters.CaptionRegex("^/broadcast_all"), broadcast_all)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO & filters.CaptionRegex("^/broadcast_buyers"), broadcast_buyers)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO & filters.CaptionRegex("^/broadcast_nonbuyers"), broadcast_nonbuyers)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & filters.CaptionRegex("^/broadcast_all"), broadcast_all)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & filters.CaptionRegex("^/broadcast_buyers"), broadcast_buyers)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & filters.CaptionRegex("^/broadcast_nonbuyers"), broadcast_nonbuyers)
    )

    # 🔥 IMPORTANT
    application.run_polling(drop_pending_updates=True)
