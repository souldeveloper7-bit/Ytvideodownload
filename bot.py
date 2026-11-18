import asyncio
import base64
import html
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_NAME = os.getenv("BOT_NAME", "YT Downloader")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
STICKER_FILE_ID = os.getenv("STICKER_FILE_ID", "").strip()
PREMIUM_TEXT = os.getenv(
    "PREMIUM_TEXT",
    "⭐ <b>Premium</b>\n\nPremium membership is not enabled yet. Contact the bot owner for details."
)
MAX_UPLOAD_MB = max(5, min(int(os.getenv("MAX_UPLOAD_MB", "49")), 49))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_CONCURRENT = max(1, min(int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")), 4))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("advanced-ytdlp-bot")
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
active_jobs = {}
JOB_LOCK = threading.Lock()

def esc(value):
    return html.escape(str(value or ""))

def valid_url(value):
    try:
        p = urlparse(value.strip())
        return p.scheme in ("http", "https") and bool(p.netloc) and "." in p.netloc
    except Exception:
        return False

def write_cookie_file(tmp_dir):
    """
    Load an optional Netscape-format cookies.txt from Railway's secret variable.
    Never commit browser cookies to GitHub. Set YOUTUBE_COOKIES_B64 to the
    base64-encoded contents of your own exported cookies.txt file.
    """
    encoded = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
        # Netscape cookie files are text; reject obviously invalid payloads.
        content = raw.decode("utf-8-sig")
        if "Netscape" not in content[:500] and not content.lstrip().startswith("#"):
            raise ValueError("Cookie file is not in Netscape cookies.txt format.")
        cookie_path = Path(tmp_dir) / "youtube-cookies.txt"
        cookie_path.write_text(content, encoding="utf-8")
        try:
            cookie_path.chmod(0o600)
        except OSError:
            pass
        return str(cookie_path)
    except Exception as exc:
        raise RuntimeError(
            "YOUTUBE_COOKIES_B64 is invalid. Export cookies in Netscape cookies.txt "
            "format and store its base64 text in Railway Variables."
        ) from exc

def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download", callback_data="menu_download"),
         InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
        [InlineKeyboardButton("🎁 Stickers", callback_data="menu_sticker"),
         InlineKeyboardButton("⭐ Premium", callback_data="menu_premium")],
        [InlineKeyboardButton("❔ Help", callback_data="menu_help"),
         InlineKeyboardButton("ℹ️ About", callback_data="menu_about")],
    ])

def download_keyboard(url):
    # URL stays in user_data; callback payload contains only compact action IDs.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Video", callback_data="dl_video"),
         InlineKeyboardButton("🎵 MP3 Audio", callback_data="dl_audio")],
        [InlineKeyboardButton("🎞 1080p", callback_data="quality_1080"),
         InlineKeyboardButton("📺 720p", callback_data="quality_720")],
        [InlineKeyboardButton("🎧 Best Audio", callback_data="dl_audio"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])

async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        await query.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("audio_only", False)
    text = (
        f"✨ <b>Welcome to {esc(BOT_NAME)}!</b>\n\n"
        "Send me a supported public video link and choose what you want:\n"
        "🎬 Video download  •  🎵 MP3 audio\n"
        "📺 720p / 🎞 1080p options  •  📊 Progress updates\n\n"
        "<i>Use the buttons below to explore.</i>"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=menu_keyboard(),
        disable_web_page_preview=True,
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "<b>How to use</b>\n"
        "1. Send a public video URL.\n"
        "2. Tap Video, MP3, 720p or 1080p.\n"
        "3. Wait for the download and upload.\n\n"
        "<b>Commands</b>\n"
        "/start — Main menu\n"
        "/help — Help\n"
        "/settings — Preferences\n"
        "/cancel — Cancel your active download\n"
        "/audio URL — Download audio\n"
        "/video URL — Download video\n"
        "/info URL — Show media information\n\n"
        f"<i>Upload limit: {MAX_UPLOAD_MB} MB. Playlists are disabled.</i>",
        parse_mode=ParseMode.HTML,
    )

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    audio = context.user_data.get("audio_only", False)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'✅' if audio else '☑️'} Prefer MP3" if audio else "☑️ Prefer Video",
            callback_data="toggle_preference"
        )],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_home")],
    ])
    await update.effective_message.reply_text(
        "<b>⚙️ Settings</b>\nChoose your preferred default format.",
        parse_mode=ParseMode.HTML, reply_markup=keyboard,
    )

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "<b>About this bot</b>\nPowered by Python, python-telegram-bot and yt-dlp.\n"
        "Downloads are processed temporarily and removed after upload.",
        parse_mode=ParseMode.HTML,
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    task = active_jobs.get(uid)
    if task and not task.done():
        task.cancel()
        await update.effective_message.reply_text("🛑 Cancellation requested.")
    else:
        await update.effective_message.reply_text("There is no active download to cancel.")

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.effective_message.text or "").strip()
    if raw.startswith("/"):
        return
    # Allow a URL surrounded by normal text, but only use the first URL-looking token.
    match = re.search(r"https?://\S+", raw)
    url = match.group(0).rstrip(").,!?]}>") if match else raw
    if not valid_url(url):
        await update.effective_message.reply_text(
            "Please send a valid http/https link to a supported video."
        )
        return
    context.user_data["pending_url"] = url
    await update.effective_message.reply_text(
        "🔗 <b>Link received!</b>\nChoose your download format:",
        parse_mode=ParseMode.HTML,
        reply_markup=download_keyboard(url),
        disable_web_page_preview=True,
    )

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args).strip()
    if not valid_url(url):
        await update.effective_message.reply_text("Usage: /info https://...")
        return
    msg = await update.effective_message.reply_text("🔎 Fetching media information…")
    def extract():
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                "remote_components": ["ejs:npm"]}
        with tempfile.TemporaryDirectory(prefix="ytdlp_info_") as tmp:
            cookie_file = write_cookie_file(tmp)
            if cookie_file:
                opts["cookiefile"] = cookie_file
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
    try:
        data = await asyncio.to_thread(extract)
        title = esc(data.get("title", "Unknown title"))
        duration = int(data.get("duration") or 0)
        mins, secs = divmod(duration, 60)
        uploader = esc(data.get("uploader") or data.get("channel") or "Unknown")
        await msg.edit_text(
            f"🎬 <b>{title}</b>\n👤 {uploader}\n⏱ {mins}:{secs:02d}\n"
            f"🌐 {esc(urlparse(url).netloc)}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        await msg.edit_text(f"❌ Could not read media info: {esc(str(e)[:350])}", parse_mode=ParseMode.HTML)

async def audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args).strip()
    if not valid_url(url):
        await update.effective_message.reply_text("Usage: /audio https://...")
        return
    await begin_download(update, context, url, "audio", None)

async def video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args).strip()
    if not valid_url(url):
        await update.effective_message.reply_text("Usage: /video https://...")
        return
    await begin_download(update, context, url, "video", 720)

async def begin_download(update, context, url, kind, height):
    uid = update.effective_user.id
    old = active_jobs.get(uid)
    if old and not old.done():
        await update.effective_message.reply_text("⚠️ You already have a download running. Use /cancel first.")
        return
    status = await update.effective_message.reply_text("⏳ <b>Preparing your download…</b>", parse_mode=ParseMode.HTML)
    task = asyncio.create_task(run_download(
        context.application, uid, update.effective_chat.id, status.message_id,
        url, kind, height,
    ))
    active_jobs[uid] = task
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if active_jobs.get(uid) is task:
            active_jobs.pop(uid, None)

async def run_download(app, uid, chat_id, status_id, url, kind, height):
    status_message = None
    try:
        status_message = await app.bot.get_chat(chat_id)
        async with semaphore:
            await app.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            with tempfile.TemporaryDirectory(prefix="ytdlp_") as tmp:
                outtmpl = str(Path(tmp) / "%(title).90s [%(id)s].%(ext)s")
                loop = asyncio.get_running_loop()
                last_update = {"t": 0.0, "pct": ""}
                status_ref = {"message": None}

                async def update_status(text):
                    try:
                        if status_ref["message"] is None:
                            status_ref["message"] = await app.bot.send_message(
                                chat_id, text, parse_mode=ParseMode.HTML
                            )
                        else:
                            await status_ref["message"].edit_text(text, parse_mode=ParseMode.HTML)
                    except Exception:
                        pass

                def hook(d):
                    if d.get("status") == "downloading":
                        now = time.monotonic()
                        total = d.get("total_bytes") or d.get("total_bytes_estimate")
                        got = d.get("downloaded_bytes", 0)
                        pct = f"{got * 100 / total:.1f}%" if total else "…"
                        if now - last_update["t"] >= 3 or pct != last_update["pct"]:
                            last_update.update(t=now, pct=pct)
                            text = f"⬇️ <b>Downloading:</b> {pct}"
                            loop.call_soon_threadsafe(
                                lambda: asyncio.create_task(update_status(text))
                            )
                    elif d.get("status") == "finished":
                        loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(update_status("⚙️ <b>Processing file…</b>"))
                        )

                opts = {
                    "outtmpl": outtmpl,
                    "noplaylist": True,
                    "max_filesize": MAX_UPLOAD_BYTES,
                    "quiet": True,
                    "no_warnings": True,
                    "restrictfilenames": True,
                    "progress_hooks": [hook],
                    "socket_timeout": 25,
                    "retries": 2,
                    "fragment_retries": 2,
                    # yt-dlp's EJS challenge solver is installed via yt-dlp[default]
                    # and runs with Deno supplied by the Railway Docker image.
                    "remote_components": ["ejs:npm"],
                }
                cookie_file = write_cookie_file(tmp)
                if cookie_file:
                    opts["cookiefile"] = cookie_file

                if kind == "audio":
                    opts.update({
                        "format": "bestaudio/best",
                        "postprocessors": [{
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }, {"key": "FFmpegMetadata"}],
                    })
                else:
                    # Prefer separate video + audio streams, then fall back to
                    # a combined progressive file if the site does not expose
                    # separate streams. Do not filter by filesize in the format
                    # selector: YouTube often exposes streams without a known
                    # filesize, which can make every format appear unavailable.
                    quality = height or 720
                    # Prefer separate video+audio streams, while allowing a
                    # combined stream as a fallback. The * wildcard also
                    # accepts formats that already contain audio.
                    opts["format"] = (
                        f"bestvideo*[height<={quality}]+bestaudio/"
                        f"best[height<={quality}]/best"
                    )
                    opts["merge_output_format"] = "mp4"
                    opts["format_sort"] = [
                        f"res:{quality}",
                        "ext:mp4:m4a",
                        "codec:h264",
                    ]

                await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts).download([url]))
                files = [p for p in Path(tmp).iterdir() if p.is_file()]
                if not files:
                    raise RuntimeError("No downloadable file was produced.")
                file_path = max(files, key=lambda p: p.stat().st_size)
                if file_path.stat().st_size > MAX_UPLOAD_BYTES:
                    raise RuntimeError(f"File is over {MAX_UPLOAD_MB} MB. Try a lower quality.")
                caption = f"✨ <b>{esc(file_path.stem[:700])}</b>\n\n<i>Downloaded with {esc(BOT_NAME)}</i>"
                await app.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                with file_path.open("rb") as f:
                    if kind == "audio":
                        await app.bot.send_audio(chat_id, audio=f, filename=file_path.name,
                                                 caption=caption, parse_mode=ParseMode.HTML,
                                                 read_timeout=180, write_timeout=180)
                    else:
                        # send as video when Telegram accepts it; document fallback on failure
                        try:
                            with file_path.open("rb") as vf:
                                await app.bot.send_video(chat_id, video=vf, filename=file_path.name,
                                                         caption=caption, parse_mode=ParseMode.HTML,
                                                         supports_streaming=True,
                                                         read_timeout=180, write_timeout=180)
                        except Exception:
                            with file_path.open("rb") as df:
                                await app.bot.send_document(chat_id, document=df, filename=file_path.name,
                                                            caption=caption, parse_mode=ParseMode.HTML,
                                                            read_timeout=180, write_timeout=180)
                if status_ref["message"]:
                    try:
                        await status_ref["message"].delete()
                    except Exception:
                        pass
                try:
                    await app.bot.delete_message(chat_id, status_id)
                except Exception:
                    pass
    except asyncio.CancelledError:
        await app.bot.send_message(chat_id, "🛑 Download cancelled.")
        raise
    except Exception as e:
        log.exception("Download error for user %s", uid)
        try:
            await app.bot.send_message(
                chat_id, f"❌ <b>Download failed</b>\n<code>{esc(str(e)[:700])}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "menu_home":
        await safe_edit(q, f"✨ <b>{esc(BOT_NAME)}</b>\n\nChoose an option:", menu_keyboard())
    elif data == "menu_download":
        await safe_edit(q, "📥 Send a supported public video URL in this chat.")
    elif data == "menu_help":
        await safe_edit(q, "<b>Help</b>\nSend a public link, then choose Video, MP3, 720p or 1080p.\nUse /cancel to stop a download.")
    elif data == "menu_about":
        await safe_edit(q, "<b>About</b>\nPowered by yt-dlp and python-telegram-bot.\nTemporary files are deleted after processing.")
    elif data == "menu_settings":
        pref = context.user_data.get("audio_only", False)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'✅' if pref else '☑️'} Prefer MP3", callback_data="toggle_preference")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_home")],
        ])
        await safe_edit(q, "<b>⚙️ Settings</b>\nToggle your preferred format.", kb)
    elif data == "toggle_preference":
        context.user_data["audio_only"] = not context.user_data.get("audio_only", False)
        pref = context.user_data["audio_only"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{'✅' if pref else '☑️'} Prefer MP3", callback_data="toggle_preference")],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu_home")],
        ])
        await safe_edit(q, f"⚙️ Preference saved: <b>{'MP3 Audio' if pref else 'Video'}</b>", kb)
    elif data == "menu_sticker":
        if STICKER_FILE_ID:
            try:
                await q.message.reply_sticker(STICKER_FILE_ID)
                await safe_edit(q, "🎁 Sticker sent! You can add your own sticker file ID with the <code>STICKER_FILE_ID</code> Railway variable.")
            except Exception:
                await safe_edit(q, "🎁 Sticker is not configured correctly. Set <code>STICKER_FILE_ID</code> in Railway Variables.")
        else:
            await safe_edit(q, "🎁 <b>Sticker button</b>\n\nTo enable your custom sticker, add its Telegram file ID as the Railway variable <code>STICKER_FILE_ID</code>.")
    elif data == "menu_premium":
        await safe_edit(q, PREMIUM_TEXT)
    elif data == "cancel":
        task = active_jobs.get(q.from_user.id)
        if task and not task.done():
            task.cancel()
            await safe_edit(q, "🛑 Cancellation requested.")
        else:
            await safe_edit(q, "No active download to cancel.")
    elif data.startswith("dl_") or data.startswith("quality_"):
        url = context.user_data.get("pending_url")
        if not url or not valid_url(url):
            await safe_edit(q, "⚠️ Link expired. Send the URL again.")
            return
        if data == "dl_audio":
            kind, height = "audio", None
        elif data == "dl_video":
            kind, height = "video", 720
        elif data == "quality_1080":
            kind, height = "video", 1080
        elif data == "quality_720":
            kind, height = "video", 720
        else:
            return
        await safe_edit(q, f"⏳ <b>Starting {'MP3 audio' if kind == 'audio' else str(height) + 'p video'} download…</b>")
        await begin_download_from_callback(update, context, url, kind, height)

async def begin_download_from_callback(update, context, url, kind, height):
    q = update.callback_query
    uid = q.from_user.id
    old = active_jobs.get(uid)
    if old and not old.done():
        await q.message.reply_text("⚠️ You already have a download running. Use /cancel first.")
        return
    task = asyncio.create_task(run_download(
        context.application, uid, q.message.chat_id, q.message.message_id,
        url, kind, height,
    ))
    active_jobs[uid] = task
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if active_jobs.get(uid) is task:
            active_jobs.pop(uid, None)

async def error_handler(update, context):
    log.exception("Unhandled Telegram error", exc_info=context.error)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("audio", audio_cmd))
    app.add_handler(CommandHandler("video", video_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
    app.add_error_handler(error_handler)
    log.info("Starting %s with Telegram long polling", BOT_NAME)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
