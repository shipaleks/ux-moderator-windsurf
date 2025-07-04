"""Telegram bot that creates cloned ElevenLabs interview agents and Google Drive folders.
Minimal MVP.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Any
from urllib.parse import quote
import hmac
import hashlib
import math
import json
import tempfile
import time
from aiohttp import web
from googleapiclient.http import MediaInMemoryUpload

import aiohttp
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
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
    """Return cached Google Drive service instance."""
    creds = Credentials.from_service_account_file(GOOGLE_SERVICE_ACCOUNT_JSON, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds)

# helper: get or create subfolder inside a parent folder
def get_or_create_subfolder(parent_id: str, name: str) -> str:
    svc = _drive_service()
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed=false"
    res = svc.files().list(q=query, fields="files(id)").execute()
    if res.get("files"):
        return res["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return svc.files().create(body=meta, fields="id").execute()["id"]

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

async def fetch_and_upload_audio(conv_id: str, folder_id: str):
    """Poll ElevenLabs Conversation API until mp3 is ready, then upload to Drive."""
    api_key = ELEVENLABS_API_KEY
    if not api_key:
        logger.warning("ELEVENLABS_API_KEY not set; cannot fetch audio")
        return
    headers = {"xi-api-key": api_key}
    details_url = f"https://api.elevenlabs.io/v1/convai/conversations/{conv_id}"
    audio_url = f"https://api.elevenlabs.io/v1/convai/conversations/{conv_id}/audio"
    async with aiohttp.ClientSession(headers=headers) as session:
        start = time.time()
        while time.time() - start < 90:
            try:
                async with session.get(details_url, timeout=20) as resp:
                    if resp.status != 200:
                        logger.warning("conv details %s -> %s", conv_id, resp.status)
                        await asyncio.sleep(5)
                        continue
                    meta = await resp.json()
                    if meta.get("status") == "done" and meta.get("has_audio"):
                        break
            except Exception as e:
                logger.warning("conv details error: %s", e)
            await asyncio.sleep(5)
        else:
            logger.warning("Audio not ready for %s within timeout", conv_id)
            return
        # download
        try:
            async with session.get(audio_url, timeout=60) as aresp:
                if aresp.status != 200:
                    logger.warning("audio download %s -> %s", conv_id, aresp.status)
                    return
                audio_bytes = await aresp.read()
        except Exception as e:
            logger.exception("audio download failed: %s", e)
            return
    try:
        service = _drive_service()
        audio_folder = get_or_create_subfolder(folder_id, "audio")
        media_mp3 = MediaInMemoryUpload(audio_bytes, mimetype="audio/mpeg", resumable=False)
        service.files().create(body={"name": f"{conv_id}.mp3", "parents": [audio_folder]}, media_body=media_mp3).execute()
        logger.info("Uploaded audio mp3 for %s", conv_id)
    except Exception as e:
        logger.exception("Drive upload mp3 failed: %s", e)

# ---------------------------------------------------------------------------
# ElevenLabs helpers
# ---------------------------------------------------------------------------
# Base URL for the custom web page with ElevenLabs widget
# Base URL for public web widget (Cloudflare Pages). Set CF_BASE_URL env in Railway.
BASE_PAGE_URL = os.getenv("CF_BASE_URL", "https://shipaleks.github.io/ux-moderator-windsurf/web/index.html")

# URL для пользователей из России (использует Cloudflare прокси)
RU_PAGE_URL = os.getenv("CF_RU_URL", "https://ux-ai.pages.dev/web/ru.html")

def build_interview_link(dynamic_vars, is_russian=False):
    """
    Build a link to the custom web page with dynamic variables as query parameters.
    If is_russian=True, uses the special RU page with Cloudflare proxy.
    """
    # Prepare parameters
    params = {
        "agent_id": ELEVENLABS_BASE_AGENT_ID,
        "interview_topic": dynamic_vars.get("interview_topic", ""),
        "interview_goals": dynamic_vars.get("interview_goals", ""), 
        "interview_duration": str(dynamic_vars.get("interview_duration", "20")),
        "additional_instructions": dynamic_vars.get("additional_instructions", ""),
        "fid": dynamic_vars.get("fid", "")
    }
    
    # URL encode parameters
    query_string = "&".join([f"{k}={quote(str(v))}" for k, v in params.items() if v])
    
    # Выбираем базовый URL в зависимости от параметра is_russian
    base_url = RU_PAGE_URL if is_russian else BASE_PAGE_URL
    return f"{base_url}?{query_string}"

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
        # Save folder id for later link building
        user_data["fid"] = folder_info["id"]
    except Exception as e:
        logger.exception("Drive error")
        await update.message.reply_text(f"Ошибка создания папки на Google Drive: {e}")
        return ConversationHandler.END

    # 2. Build interview links (обычная и для России)
    interview_link = build_interview_link(user_data)
    ru_interview_link = build_interview_link(user_data, is_russian=True)

    # 3. Reply with links
    reply = (
        "Готово! \U0001F389\n\n"
        f"• Ссылка на интервью: {interview_link}\n"
        f"• Ссылка для пользователей из России: {ru_interview_link}\n"
        f"• Папка Google Drive: {folder_info['link']}\n\n"
        "Передайте обычную ссылку респондентам или специальную ссылку для России. \n"
        "Аудиозаписи будут сохраняться в указанную папку. Удачи!"
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

        # Always run in polling mode (simpler, allows custom aiohttp routes)
    logger.info("Starting bot in POLLING mode…")
    port = int(os.getenv("PORT", "8080"))
    asyncio.get_event_loop().create_task(start_webhook_server(port))
    application.run_polling()

# ---------------------------------------------------------------------------
# ElevenLabs post-call webhook handling
# ---------------------------------------------------------------------------

ELEVEN_WEBHOOK_SECRET = os.getenv("ELEVEN_WEBHOOK_SECRET")

async def elevenlabs_webhook(request: web.Request):
    try:
        # Temporarily disable signature validation for debugging
        # webhook_secret = os.getenv("ELEVEN_WEBHOOK_SECRET")
        # if webhook_secret:
        #     signature = request.headers.get("X-Elevenlabs-Signature")
        #     if not signature:
        #         logger.warning("Missing webhook signature")
        #         return web.Response(text="Missing signature", status=401)
        #     
        #     body = await request.read()
        #     expected_signature = hmac.new(
        #         webhook_secret.encode(), body, hashlib.sha256
        #     ).hexdigest()
        #     
        #     if not hmac.compare_digest(signature, expected_signature):
        #         logger.warning("Invalid webhook signature")
        #         return web.Response(text="Invalid signature", status=401)

        # HMAC signature validation (enabled if EL_WEBHOOK_SECRET env var is set)
        body = await request.read()
        webhook_secret = os.getenv("EL_WEBHOOK_SECRET") or os.getenv("ELEVEN_WEBHOOK_SECRET")
        if webhook_secret:
            signature = request.headers.get("X-Elevenlabs-Signature")
            if not signature:
                logger.warning("Missing webhook signature")
                return web.Response(text="Missing signature", status=401)
            expected_signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected_signature):
                logger.warning("Invalid webhook signature")
                return web.Response(text="Invalid signature", status=401)
        # Parse JSON payload
        data = json.loads(body.decode())
        logger.info("Webhook received payload: %s", json.dumps(data, indent=2))
        
        # Extract audio URL and folder ID (payload structure: payload['data'])
        inner = data.get("data", {})
        audio_url = inner.get("recording_url") or inner.get("audio_url")

        # fid can be in conversation_initiation_client_data or in dynamic_variables
        cicd = inner.get("conversation_initiation_client_data", {})
        fid = (
            cicd.get("fid") or
            cicd.get("folder_id") or
            cicd.get("dynamic_variables", {}).get("fid") or
            inner.get("dynamic_variables", {}).get("fid")
        )
        
        transcript = inner.get("transcript")

        if not audio_url and not transcript:
            logger.warning("Neither audio nor transcript in webhook payload")
            return web.Response(text="No audio or transcript", status=400)

        if not fid:
            logger.warning("No fid in webhook payload")
            return web.Response(text="No folder ID", status=400)

        service = _drive_service()

        # Handle audio file if present
        if audio_url:
            logger.info("Uploading audio %s to folder %s", audio_url, fid)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(audio_url) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"audio download failed {resp.status}")
                        audio_bytes = await resp.read()
                        filename = audio_url.split("/")[-1].split("?", 1)[0] or "recording.mp3"
                media = MediaInMemoryUpload(audio_bytes, mimetype="audio/mpeg", resumable=False)
                service.files().create(body={"name": filename, "parents": [fid]}, media_body=media).execute()
                logger.info("Uploaded %s to Drive folder %s", filename, fid)
            except Exception as e:
                logger.exception("Failed to handle audio: %s", e)

        # Handle transcript if present
        if transcript:
            try:
                convo_id = inner.get("conversation_id", "conversation")
                transcripts_folder = get_or_create_subfolder(fid, "transcripts")
                # Build VTT content (but save as .txt)
                def secs_to_ts(s: float):
                    h = int(s//3600); m = int((s%3600)//60); sec = s%60
                    return f"{h:02}:{m:02}:{sec:06.3f}".replace('.',',')
                cues = []
                for idx, item in enumerate(sorted(transcript, key=lambda x: x.get('time_in_call_secs', 0))):
                    start = item.get('time_in_call_secs', 0)
                    end = transcript[idx+1].get('time_in_call_secs', start+2) if idx+1 < len(transcript) else start+2
                    cues.append(f"{idx+1}\n{secs_to_ts(start)} --> {secs_to_ts(end)}\n{item.get('role','')}: {item.get('message','')}\n")
                vtt_content = "WEBVTT\n\n" + "\n".join(cues)
                media_vtt = MediaInMemoryUpload(vtt_content.encode('utf-8'), mimetype="text/plain", resumable=False)
                service.files().create(body={"name": f"{convo_id}.txt", "parents": [transcripts_folder]}, media_body=media_vtt).execute()
                logger.info("Uploaded transcript TXT and VTT for %s", convo_id)
                # trigger audio fetch in background
                asyncio.create_task(fetch_and_upload_audio(convo_id, fid))
            except Exception as e:
                logger.exception("Failed to upload transcript/VTT: %s", e)

        return web.Response(status=200)

    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return web.Response(status=500)

async def start_webhook_server(port: int):
    app = web.Application()
    app.router.add_post("/elevenlabs/webhook", elevenlabs_webhook)
    # Add health check endpoint
    async def health_check(request):
        return web.Response(text="OK", status=200)
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("ElevenLabs webhook server started on port %s", port)

if __name__ == "__main__":
    main()
