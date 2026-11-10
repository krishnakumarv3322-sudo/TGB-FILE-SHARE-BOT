
import os
import logging
import threading
import asyncio
import json

import psycopg2
from flask import Flask
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, InputMediaPhoto
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]

BOT_USERNAME = None
JOIN_LINK = None
web = Flask(__name__)
ALBUM_BUFFER = {}

IMPORTANT_TEXT = (
    "⚠️ Important:\n\n"
    "This file message will be deleted after 30 minutes. "
    "Content protection is enabled where Telegram supports it."
)


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
            caption TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Safe migration for databases created by earlier bot versions.
    cur.execute("ALTER TABLE shared_files ADD COLUMN IF NOT EXISTS caption TEXT")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS delivered_messages (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            file_message_id BIGINT NOT NULL,
            warning_message_id BIGINT,
            expires_at TIMESTAMP NOT NULL,
            deleted BOOLEAN DEFAULT FALSE
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL
        )
    """)

    cur.execute("""
        INSERT INTO bot_settings (setting_key, setting_value)
        VALUES ('protect_content', 'true')
        ON CONFLICT (setting_key) DO NOTHING
    """)

    con.commit()
    cur.close()
    con.close()


def get_protection_setting():
    con = db()
    cur = con.cursor()

    cur.execute("""
        SELECT setting_value
        FROM bot_settings
        WHERE setting_key = 'protect_content'
    """)

    row = cur.fetchone()
    cur.close()
    con.close()

    return row is None or row[0].lower() == "true"


def set_protection_setting(enabled):
    con = db()
    cur = con.cursor()

    cur.execute("""
        INSERT INTO bot_settings (setting_key, setting_value)
        VALUES ('protect_content', %s)
        ON CONFLICT (setting_key)
        DO UPDATE SET setting_value = EXCLUDED.setting_value
    """, ("true" if enabled else "false",))

    con.commit()
    cur.close()
    con.close()


async def post_init(application):
    global JOIN_LINK, BOT_USERNAME

    try:
        me = await application.bot.get_me()
        BOT_USERNAME = me.username

        link = await application.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            creates_join_request=True
        )

        JOIN_LINK = link.invite_link
        log.info("Bot setup completed")

    except Exception:
        log.exception("Bot setup failed")

    application.create_task(cleanup_loop(application))


async def is_approved(user_id, context):
    try:
        member = await context.bot.get_chat_member(
            CHANNEL_ID, user_id
        )

        return (
            member.status in ("member", "administrator", "creator")
            or (
                member.status == "restricted"
                and member.is_member
            )
        )

    except Exception:
        log.exception("Membership check failed")
        return False


def home_buttons(file_number=None):
    retry_data = (
        f"try_again_{file_number}"
        if file_number
        else "try_again"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔄 Try Again",
                callback_data=retry_data
            ),
            InlineKeyboardButton(
                "📢 Official Channel",
                url="https://t.me/+g2gbuV62fFkzZDU1"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 My Profile",
                callback_data="my_profile"
            ),
            InlineKeyboardButton(
                "💬 Contact Developer",
                url="https://t.me/Arixp20king"
            )
        ]
    ])


def welcome_text(user):
    first_name = user.first_name or "User"

    return (
        f"Hello 👋 {first_name}\n"
        "Welcome to ARI X P20KING bot ❣️\n\n"
        "🔐 Secure Access • Fast response!\n"
        "📁 Easy to access files 📢\n\n"
        "‼️ JOIN OUR PRIVATE CHANNEL FIRST\n"
        "THEN CLICK TRY AGAIN 🚨"
    )


async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    username = f"@{user.username}" if user.username else "Not set"
    first_name = user.first_name or "Not set"

    await query.message.reply_text(
        "👤 MY PROFILE\n\n"
        f"🧑 Name : {first_name}\n"
        f"🔖 Username : {username}\n"
        f"🆔 User ID : {user.id}\n\n"
        "💎 ARI X P20KING"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = context.args[0] if context.args else None
    file_number = None

    if payload and payload.startswith("file_"):
        value = payload.replace("file_", "", 1)

        if value.isdigit():
            file_number = int(value)

    if file_number:
        if await is_approved(update.effective_user.id, context):
            await deliver_file(
                update.effective_user.id,
                file_number,
                context
            )
            return

        await update.message.reply_text(
            welcome_text(update.effective_user),
            reply_markup=home_buttons(file_number)
        )
        return

    await update.message.reply_text(
        welcome_text(update.effective_user),
        reply_markup=home_buttons()
    )


async def try_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    file_number = None
    if query.data.startswith("try_again_"):
        value = query.data.replace("try_again_", "", 1)
        if value.isdigit():
            file_number = int(value)

    if not await is_approved(query.from_user.id, context):
        await query.message.reply_text(
            "⏳ Your channel join request is not approved yet.\n"
            "Please wait for admin approval, then press Try Again.",
            reply_markup=home_buttons(file_number)
        )
        return

    if file_number:
        await deliver_file(query.from_user.id, file_number, context)
    else:
        await query.message.reply_text(
            "✅ Channel membership verified!\n"
            "Please open the specific file link to access that file."
        )


async def send_saved_file(
    chat_id,
    file_id,
    file_type,
    name,
    context,
    protection,
    caption=None
):
    common = {
        "chat_id": chat_id,
        "protect_content": protection
    }

    if file_type == "photo":
        return await context.bot.send_photo(
            **common,
            photo=file_id,
            caption=caption
        )

    if file_type == "video":
        return await context.bot.send_video(
            **common,
            video=file_id,
            caption=caption
        )

    if file_type == "audio":
        return await context.bot.send_audio(
            **common,
            audio=file_id,
            caption=caption
        )

    if file_type == "animation":
        return await context.bot.send_animation(
            **common,
            animation=file_id,
            caption=caption
        )

    if file_type == "voice":
        return await context.bot.send_voice(
            **common,
            voice=file_id,
            caption=caption
        )

    if file_type == "video_note":
        return await context.bot.send_video_note(
            **common,
            video_note=file_id
        )

    if file_type == "sticker":
        return await context.bot.send_sticker(
            **common,
            sticker=file_id
        )

    return await context.bot.send_document(
        **common,
        document=file_id,
        filename=name or "shared_file",
        caption=caption
    )


async def deliver_file(chat_id, file_number, context):
    con = db()
    cur = con.cursor()

    cur.execute(
        """
        SELECT file_id, file_type, file_name, caption
        FROM shared_files
        WHERE id = %s
        """,
        (file_number,)
    )

    item = cur.fetchone()
    cur.close()
    con.close()

    if not item:
        await context.bot.send_message(
            chat_id,
            "❌ File not found."
        )
        return

    file_id, file_type, name, caption = item

    try:
        protection = get_protection_setting()

        if file_type == "photo_album":
            photo_ids = json.loads(file_id)
            media = [InputMediaPhoto(media=photo_id) for photo_id in photo_ids]
            if caption:
                media[0].caption = caption
            sent_messages = await context.bot.send_media_group(
                chat_id=chat_id,
                media=media,
                protect_content=protection
            )
        elif file_type == "text":
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=file_id,
                protect_content=protection
            )
            sent_messages = [sent]
        else:
            sent = await send_saved_file(
                chat_id,
                file_id,
                file_type,
                name,
                context,
                protection,
                caption
            )
            sent_messages = [sent]

        warning = await context.bot.send_message(
            chat_id=chat_id,
            text=IMPORTANT_TEXT,
            protect_content=protection
        )

        con = db()
        cur = con.cursor()

        for index, sent_message in enumerate(sent_messages):
            cur.execute(
                """
                INSERT INTO delivered_messages
                (chat_id, file_message_id, warning_message_id, expires_at)
                VALUES (%s, %s, %s, NOW() + INTERVAL '30 minutes')
                """,
                (
                    chat_id,
                    sent_message.message_id,
                    warning.message_id if index == 0 else None
                )
            )

        con.commit()
        cur.close()
        con.close()

    except Exception:
        log.exception("File delivery failed")

        await context.bot.send_message(
            chat_id,
            "❌ Could not send this file. Please try again."
        )




async def cleanup_loop(application):
    while True:
        try:
            con = db()
            cur = con.cursor()

            cur.execute("""
                SELECT id, chat_id, file_message_id, warning_message_id
                FROM delivered_messages
                WHERE expires_at <= NOW()
                AND deleted = FALSE
                ORDER BY id
                LIMIT 100
            """)

            rows = cur.fetchall()
            cur.close()
            con.close()

            for row_id, chat_id, file_msg, warning_msg in rows:
                try:
                    await application.bot.delete_message(
                        chat_id=chat_id,
                        message_id=file_msg
                    )
                except Exception:
                    log.info("File message unavailable for deletion")

                if warning_msg:
                    try:
                        await application.bot.delete_message(
                            chat_id=chat_id,
                            message_id=warning_msg
                        )
                    except Exception:
                        log.info("Warning message unavailable for deletion")

                con = db()
                cur = con.cursor()

                cur.execute(
                    """
                    UPDATE delivered_messages
                    SET deleted = TRUE
                    WHERE id = %s
                    """,
                    (row_id,)
                )

                con.commit()
                cur.close()
                con.close()

        except Exception:
            log.exception("Cleanup loop error")

        await asyncio.sleep(30)


async def save_photo_album(chat_id, media_group_id, context):
    key = (chat_id, media_group_id)
    await asyncio.sleep(1.5)  # wait for all messages in this Telegram album
    photos = ALBUM_BUFFER.pop(key, [])
    if len(photos) < 2:
        # A single item is handled as a normal photo below; this is a safety fallback.
        if photos:
            photo_id, caption = photos[0]
            con = db()
            cur = con.cursor()
            cur.execute(
                "INSERT INTO shared_files (file_id, file_type, file_name, caption) VALUES (%s, %s, %s, %s) RETURNING id",
                (photo_id, "photo", caption or "Photo", caption)
            )
            file_number = cur.fetchone()[0]
            con.commit()
            cur.close()
            con.close()
        else:
            return
    else:
        photo_ids = [photo_id for photo_id, _ in photos]
        album_caption = next((caption for _, caption in photos if caption), None)
        con = db()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO shared_files (file_id, file_type, file_name, caption) VALUES (%s, %s, %s, %s) RETURNING id",
            (json.dumps(photo_ids), "photo_album", f"Photo album ({len(photo_ids)} photos)", album_caption)
        )
        file_number = cur.fetchone()[0]
        con.commit()
        cur.close()
        con.close()

    bot_name = BOT_USERNAME or "Ari_Exe_files_bot"
    deep_link = f"https://t.me/{bot_name}?start=file_{file_number}"
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ Photo album saved successfully! ({len(photos)} photos)\n\n"
            "📎 Your unique album link:\n"
            f"{deep_link}\n\n"
            "Post this link in your channel using the text CLICK HERE."
        )
    )


async def upload_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    message = update.message

    # Telegram sends each item in an album as a separate update with the same media_group_id.
    if message.media_group_id and message.photo:
        key = (message.chat_id, message.media_group_id)
        ALBUM_BUFFER.setdefault(key, []).append(
            (message.photo[-1].file_id, message.caption)
        )
        if len(ALBUM_BUFFER[key]) == 1:
            asyncio.create_task(
                save_photo_album(message.chat_id, message.media_group_id, context)
            )
        return

    file_id = None
    file_type = None
    file_name = None
    caption = message.caption

    if message.text is not None:
        file_id = message.text
        file_type = "text"
        file_name = "Text message"

    elif message.document:
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

    elif message.animation:
        file_id = message.animation.file_id
        file_type = "animation"
        file_name = message.animation.file_name or "Animation"

    elif message.voice:
        file_id = message.voice.file_id
        file_type = "voice"
        file_name = "Voice"

    elif message.video_note:
        file_id = message.video_note.file_id
        file_type = "video_note"
        file_name = "Video note"

    elif message.sticker:
        file_id = message.sticker.file_id
        file_type = "sticker"
        file_name = "Sticker"

    if not file_id:
        await message.reply_text(
            "Please send text or upload a supported document, video, photo, "
            "audio, animation, voice, video note or sticker."
        )
        return

    con = db()
    cur = con.cursor()

    cur.execute(
        """
        INSERT INTO shared_files (file_id, file_type, file_name, caption)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (file_id, file_type, file_name, caption)
    )

    file_number = cur.fetchone()[0]

    con.commit()
    cur.close()
    con.close()

    bot_name = BOT_USERNAME or "Ari_Exe_files_bot"
    deep_link = f"https://t.me/{bot_name}?start=file_{file_number}"

    await message.reply_text(
        ("✅ Text saved successfully!\n\n" if file_type == "text" else "✅ File saved successfully!\n\n")
        + "📎 Your unique link:\n"
        f"{deep_link}\n\n"
        "Post this link in your channel using the text CLICK HERE."
    )


async def myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    con = db()
    cur = con.cursor()

    cur.execute(
        "SELECT id, file_name FROM shared_files ORDER BY id DESC"
    )

    files = cur.fetchall()
    cur.close()
    con.close()

    if not files:
        await update.message.reply_text("No files uploaded.")
        return

    result = "\n".join(
        f"{file_number}. {name or 'Unnamed file'}"
        for file_number, name in files
    )

    await update.message.reply_text(
        "📂 Uploaded files:\n" + result
    )


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


async def delete_all_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args or context.args[0].lower() != "confirm":
        await update.message.reply_text(
            "⚠️ This will delete ALL saved file records.\n\n"
            "To confirm, send:\n"
            "/deleteallfiles confirm"
        )
        return

    con = db()
    cur = con.cursor()

    cur.execute("DELETE FROM shared_files")
    deleted_count = cur.rowcount

    con.commit()
    cur.close()
    con.close()

    await update.message.reply_text(
        f"✅ All file records deleted.\nTotal: {deleted_count}"
    )


async def stop_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    set_protection_setting(True)

    await update.message.reply_text(
        "🔒 Save Protection is now ON!\n\n"
        "New file deliveries will use Telegram content protection. "
        "Telegram clients may still show a download control; complete "
        "prevention of every copy is not guaranteed."
    )


async def unlock_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    set_protection_setting(False)

    await update.message.reply_text(
        "🔓 Save Protection is now OFF!\n\n"
        "New file deliveries will not request Telegram content protection."
    )


async def make_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if JOIN_LINK:
        await update.message.reply_text(
            "Current Join Request Link:\n" + JOIN_LINK
        )
    else:
        await update.message.reply_text(
            "Join link is not available."
        )


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
    application.add_handler(
        CommandHandler("deleteallfiles", delete_all_files)
    )
    application.add_handler(CommandHandler("joinlink", make_link))
    application.add_handler(CommandHandler("stopsave", stop_save))
    application.add_handler(CommandHandler("unlocksave", unlock_save))

    application.add_handler(
        CallbackQueryHandler(
            try_again,
            pattern=r"^try_again(?:_\d+)?$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            my_profile,
            pattern=r"^my_profile$"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            upload_file
        )
    )

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
    threading.Thread(
        target=run_bot,
        daemon=True
    ).start()

    web.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
    
