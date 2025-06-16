"""Telegram bot that creates cloned ElevenLabs interview agents and Google Drive folders.
Minimal MVP.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Any
from urllib.parse import quote
import hmac, hashlib, json, io

import aiohttp
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Environment & Logging
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Optionally reconstruct Google service account JSON from env variables
# ---------------------------------------------------------------------------
# If SERVICE_ACCOUNT_JSON (raw JSON string) is present, write it to file;
# otherwise, if SERVICE_ACCOUNT_B64 contains base64-encoded JSON, decode it.
if not os.path.exists("service_account.json"):
    if os.getenv("SERVICE_ACCOUNT_JSON"):
        with open("service_account.json", "w") as f:
            f.write(os.environ["SERVICE_ACCOUNT_JSON"])
    elif os.getenv("SERVICE_ACCOUNT_B64"):
        import base64
        with open("service_account.json", "wb") as f:
            f.write(base64.b64decode(os.environ["SERVICE_ACCOUNT_B64"]))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_BASE_AGENT_ID = os.getenv("ELEVENLABS_BASE_AGENT_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
GOOGLE_DRIVE_PARENT_FOLDER_ID = os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID")

REQUIRED_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_BASE_AGENT_ID",
    "GOOGLE_DRIVE_PARENT_FOLDER_ID",
]
missing_env = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_env:
    raise EnvironmentError(f"Missing environment variables: {', '.join(missing_env)}")

# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------
_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]

def _drive_service():
    creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)

def create_drive_folder(topic: str) -> Dict[str, str]:
    """Create a folder for the user, return {id, link}. Synchronous helper for to_thread."""
    service = _drive_service()
    metadata = {
        "name": f"UX-Interview-{topic}-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [GOOGLE_DRIVE_PARENT_FOLDER_ID],
    }
    folder = (
        service.files()
        .create(body=metadata, fields="id, webViewLink, webContentLink")
        .execute()
    )

    # Make anyone-w-link viewer (simple)
    permission_body = {"type": "anyone", "role": "reader"}
    service.permissions().create(fileId=folder["id"], body=permission_body).execute()

    return {"id": folder["id"], "link": folder.get("webViewLink")}

# ---------------------------------------------------------------------------
# ElevenLabs helpers
# ---------------------------------------------------------------------------
# Base URL for the custom web page with ElevenLabs widget
BASE_PAGE_URL = "https://shipaleks.github.io/ux-moderator-windsurf/web/index.html"

def build_interview_link(dynamic_vars):
    """
    Build a link to the custom web page with dynamic variables as query parameters.
    The web page will inject these variables into the ElevenLabs widget.
    """
    params = {
        "agent_id": ELEVENLABS_BASE_AGENT_ID,
        "interview_topic": dynamic_vars.get("interview_topic", ""),
        "interview_goals": dynamic_vars.get("interview_goals", ""), 
        "interview_duration": str(dynamic_vars.get("interview_duration", "20")),
        "additional_instructions": dynamic_vars.get("additional_instructions", ""),
        "drive_folder_id": dynamic_vars.get("drive_folder_id", "")
    }
    
    # URL encode parameters
    query_string = "&".join([f"{k}={quote(str(v))}" for k, v in params.items() if v])
    return f"{BASE_PAGE_URL}?{query_string}"

# ---------------------------------------------------------------------------
# Telegram conversation states
# ---------------------------------------------------------------------------
TOPIC, GOAL, EXTRA, DURATION = range(4)

# Temporary storage for user answers in-memory (user_id -> dict)
user_answers: Dict[int, Dict[str, str]] = {}

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Привет! Я помогу создать UX-интервьюера. Давай начнём.\n\n"
        "1/4 📚  Введите тему исследования (например: мобильное банковское приложение)"
    )
    user_answers[update.effective_user.id] = {}
    return TOPIC

async def topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_answers[update.effective_user.id]["interview_topic"] = update.message.text.strip()
    await update.message.reply_text("2/4 🎯  Какова цель исследования? (например: повысить конверсию онбординга)")
    return GOAL

async def goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_answers[update.effective_user.id]["interview_goal"] = update.message.text.strip()
    await update.message.reply_text("3/4 📝  Дополнительные инструкции для агента (если нет, введите -).\nНапример: обращаться на «ты», избегать технического жаргона")
    return EXTRA

async def extra_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == "-":
        text = ""
    user_answers[update.effective_user.id]["additional_instructions"] = text
    await update.message.reply_text("4/4 ⏱️  Планируемая длительность интервью в минутах? (например: 20)")
    return DURATION

async def duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_data = user_answers.get(update.effective_user.id, {})
    
    user_data["interview_duration"] = update.message.text.strip()

    await update.message.reply_text("⏳ Создаю агента и папку, подождите пару секунд…", reply_markup=ReplyKeyboardRemove())

    # 1. Create Drive folder
    try:
        folder_info = await asyncio.to_thread(create_drive_folder, user_data["interview_topic"])
        user_data["drive_folder_id"] = folder_info["id"]
    except Exception as e:
        logger.exception("Drive error")
        await update.message.reply_text(f"Ошибка создания папки на Google Drive: {e}")
        return ConversationHandler.END

    # 2. Build interview link
    interview_link = build_interview_link(user_data)

    # 3. Reply with links
    reply = (
        "Готово! \U0001F389\n\n"
        f"• Ссылка на интервью: {interview_link}\n"
        f"• Папка Google Drive: {folder_info['link']}\n\n"
        "Передайте ссылку респондентам сразу после генерации. \nАудиозаписи будут сохраняться в указанную папку. Удачи!"
    )
    await update.message.reply_text(reply)

    # Clean up user data
    user_answers.pop(update.effective_user.id, None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_answers.pop(update.effective_user.id, None)
    await update.message.reply_text("Диалог отменён.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ElevenLabs webhook handling
# ---------------------------------------------------------------------------
ELEVEN_WEBHOOK_SECRET = os.getenv("ELEVEN_WEBHOOK_SECRET", "")

async def download_and_upload(conv_id: str, folder_id: str):
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conv_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.error("Failed to fetch conversation %s: %s", conv_id, resp.status)
                return
            data = await resp.json()
    audio_urls = [m.get("audio_url") for m in data.get("messages", []) if m.get("audio_url")]
    if not audio_urls:
        logger.warning("No audio urls in conversation %s", conv_id)
        return
    service = _drive_service()
    for au in audio_urls:
        async with aiohttp.ClientSession() as s:
            async with s.get(au) as r:
                if r.status != 200:
                    logger.warning("Failed download %s", au)
                    continue
                content = await r.read()
        filename = au.split("/")[-1]
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype="audio/mpeg")
        service.files().create(media_body=media, body={"name": filename, "parents": [folder_id]}).execute()
    logger.info("Uploaded %s files from conv %s to folder %s", len(audio_urls), conv_id, folder_id)

async def eleven_webhook(request):
    raw = await request.read()
    if ELEVEN_WEBHOOK_SECRET:
        sig = request.headers.get("x-elevenlabs-signature", "")
        expected = hmac.new(ELEVEN_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return aiohttp.web.Response(status=401)
    data = json.loads(raw.decode())
    if data.get("event") != "conversation_finished":
        return aiohttp.web.Response(status=200)
    conv_id = data.get("conversation_id")
    dyn_vars = data.get("metadata", {}).get("dynamic_variables", {})
    folder_id = dyn_vars.get("drive_folder_id")
    if not folder_id:
        logger.error("drive_folder_id missing in webhook for conv %s", conv_id)
        return aiohttp.web.Response(status=200)
    asyncio.create_task(download_and_upload(conv_id, folder_id))
    return aiohttp.web.Response(status=200)

# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main() -> None:
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, topic)],
            GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal)],
            EXTRA: [MessageHandler(filters.TEXT & ~filters.COMMAND, extra_instructions)],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, duration)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    # Determine run mode
    use_webhook = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    if use_webhook:
        base_url = os.environ["APP_BASE_URL"].rstrip("/")  # e.g. https://my-app.up.railway.app
        port = int(os.getenv("PORT", "8080"))
        path = f"/{TELEGRAM_TOKEN}"
        logger.info("Starting bot in WEBHOOK mode on port %s", port)
                # Build aiohttp app with custom ElevenLabs endpoint
        def add_routes(app):
            app.router.add_post('/eleven-webhook', eleven_webhook)


        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{base_url}{path}",
            bootstrap_app=add_routes,
        )
    else:
        logger.info("Starting bot in POLLING mode…")
        application.run_polling()

if __name__ == "__main__":
    main()
