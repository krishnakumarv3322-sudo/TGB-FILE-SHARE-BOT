
import os
import logging
import threading
import asyncio

import psycopg2
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]

JOIN_LINK = None
web = Flask(__name__)


def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def setup_database():
    con = db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shared_files (
            id SERIAL PRIMARY KEY,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_name TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    cur.close()
    con.close()


async def post_init(application):
    global JOIN_LINK
    try:
        link = await application.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            creates_join_request=True
        )
        JOIN_LINK = link.invite_link
        log.info("Join request link created")
    except Exception:
        log.exception("Could not create join request link")


async def is_approved(user_id, context):
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return (
            member.status in ("member", "administrator", "creator")
            or (member.status == "restricted" and member.is_member)
        )
    except Exception:
        log.exception("Membership check failed")
        return False


def home_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Join Channel", url=JOIN_LINK or "https://t.me/")],
        [InlineKeyboardButton("🔄 Try Again", callback_data="try_again")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Ari File Share Bot!\n\n"
        "Join our private channel first, then press Try Again.",
        reply_markup=home_buttons()
    )


async def try_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if await is_approved(query.from_user.id, context):
        await show_files(query, context)
    else:
        await query.message.reply_text(
            "⏳ Your channel join request is not approved yet.\n"
            "Please wait for admin approval, then press Try Again.",
            reply_markup=home_buttons()
        )


async def show_files(query, context):
    con = db()
    cur = con.cursor()
    cur.execute(
        "SELECT id, file_name FROM shared_files ORDER BY id DESC"
    )
    files = cur.fetchall()
    cur.close()
    con.close()

    if not files:
        await query.message.reply_text("📂 No files uploaded yet.")
        return

    buttons = []
    for file_id, name in files:
        buttons.append([
            InlineKeyboardButton(
                name or f"File {file_id}",
                callback_data=f"file_{file_id}"
            )
        ])

    await query.message.reply_text(
        "📂 Available Files:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def file_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_approved(query.from_user.id, context):
        await query.message.reply_text(
            "❌ Access denied. Join and get approved first.",
            reply_markup=home_buttons()
        )
        return

    try:
        file_number = int(query.data.split("_", 1)[1])
        con = db()
        cur = con.cursor()
        cur.execute(
            "SELECT file_id, file_type, file_name "
            "FROM shared_files WHERE id = %s",
            (file_number,)
        )
        item = cur.fetchone()
        cur.close()
        con.close()

        if not item:
            await query.message.reply_text("File not found.")
            return

        file_id, file_type, name = item

        if file_type == "photo":
            await context.bot.send_photo(query.from_user.id, file_id)
        elif file_type == "video":
            await context.bot.send_video(query.from_user.id, file_id)
        elif file_type == "audio":
            await context.bot.send_audio(query.from_user.id, file_id)
        else:
            await context.bot.send_document(
                query.from_user.id, file_id,
                filename=name or "shared_file"
            )

    except Exception:
        log.exception("File sending failed")
        await query.message.reply_text("❌ Could not send this file.")


async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    message = update.message
    file_id = None
    file_type = None
    file_name = None

    if message.document:
        file_id = message.document.file_id
        file_type = "document"
        file_name = message.document.file_name
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
        file_name = message.caption or "Video"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
        file_name = message.caption or "Photo"
    elif message.audio:
        file_id = message.audio.file_id
        file_type = "audio"
        file_name = message.audio.file_name or "Audio"

    if not file_id:
        await message.reply_text("Please upload a document, video, photo or audio.")
        return

    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO shared_files (file_id, file_type, file_name) "
        "VALUES (%s, %s, %s)",
        (file_id, file_type, file_name)
    )
    con.commit()
    cur.close()
    con.close()

    await message.reply_text("✅ File saved successfully!")


async def myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    con = db()
    cur = con.cursor()
    cur.execute("SELECT id, file_name FROM shared_files ORDER BY id DESC")
    files = cur.fetchall()
    cur.close()
    con.close()

    if not files:
        await update.message.reply_text("No files uploaded.")
        return

    result = "\n".join(
        f"{file_id}. {name or 'Unnamed file'}"
        for file_id, name in files
    )
    await update.message.reply_text("📂 Uploaded files:\n" + result)


async def delete_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delete FILE_ID")
        return

    con = db()
    cur = con.cursor()
    cur.execute(
        "DELETE FROM shared_files WHERE id = %s",
        (int(context.args[0]),)
    )
    deleted = cur.rowcount
    con.commit()
    cur.close()
    con.close()

    await update.message.reply_text(
        "✅ File deleted." if deleted else "File ID not found."
    )


async def make_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global JOIN_LINK
    if update.effective_user.id != ADMIN_ID:
        return

    if JOIN_LINK:
        await update.message.reply_text(
            "Current Join Request Link:\n" + JOIN_LINK
        )
    else:
        await update.message.reply_text("Join link is not available.")


def run_bot():
    setup_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myfiles", myfiles))
    application.add_handler(CommandHandler("delete", delete_file))
    application.add_handler(CommandHandler("joinlink", make_link))
    application.add_handler(CallbackQueryHandler(
        try_again, pattern="^try_again$"
    ))
    application.add_handler(CallbackQueryHandler(
        file_button, pattern="^file_"
    ))
    application.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND, upload_file
    ))

    application.run_polling(
    drop_pending_updates=True,
    stop_signals=None
    )


@web.route("/")
def home():
    return "Ari File Share Bot is running!"


@web.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
