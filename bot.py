"""Telegram bot that creates cloned ElevenLabs interview agents and Google Drive folders.
Minimal MVP.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Any

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
ELEVEN_API_BASE = "https://api.elevenlabs.io/v1/convai"

async def clone_agent(variables: Dict[str, Any]) -> Dict[str, str]:
    """Clone base agent and return signed URL with injected dynamic variables."""
    """Clone base agent via `from_agent_id` and return {agent_id, share_url}."""
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    name = (
        f"UX Interviewer - {variables['interview_topic']} - "
        f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    )

    # 1) get base agent conversation config to copy
    get_url = f"{ELEVEN_API_BASE}/agents/{ELEVENLABS_BASE_AGENT_ID}"
    async with aiohttp.ClientSession() as session:
        async with session.get(get_url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to get base agent: {resp.status} {await resp.text()}")
            base_agent_data = await resp.json()
            base_conv_config = base_agent_data.get("conversation_config", {})
            logger.debug("Base agent conversation_config: %s", base_conv_config)
            logger.debug("Full base agent data structure: %s", base_agent_data)

    # map keys to agent variable names
    dynamic_vars = {
        "interview_topic": variables.get("interview_topic"),
        "interview_goals": variables.get("interview_goal"),
        "interview_duration": variables.get("interview_duration"),
        "additional_instructions": variables.get("additional_instructions"),
    }

    # 1.5) inject variables directly into system prompt instead of using dynamic variables
    modified_config = base_conv_config.copy()
    if "system_prompt" in modified_config:
        original_prompt = modified_config["system_prompt"]
        logger.debug("Original system_prompt: %s", original_prompt)
        # Replace placeholders with actual values
        modified_config["system_prompt"] = original_prompt.replace(
            "{{interview_topic}}", dynamic_vars.get("interview_topic", "[тема не указана]")
        ).replace(
            "{{interview_goals}}", dynamic_vars.get("interview_goals", "[цели не указаны]")
        ).replace(
            "{{interview_duration}}", str(dynamic_vars.get("interview_duration", "20"))
        ).replace(
            "{{additional_instructions}}", dynamic_vars.get("additional_instructions", "")
        )
        logger.debug("Modified system_prompt: %s", modified_config["system_prompt"])
    else:
        logger.warning("No system_prompt found in base agent config")
        
    # Also replace in first_message if it exists
    if "first_message" in modified_config:
        original_first = modified_config["first_message"]
        logger.debug("Original first_message: %s", original_first)
        modified_config["first_message"] = original_first.replace(
            "{{interview_topic}}", dynamic_vars.get("interview_topic", "[тема не указана]")
        ).replace(
            "{{interview_goals}}", dynamic_vars.get("interview_goals", "[цели не указаны]")
        ).replace(
            "{{interview_duration}}", str(dynamic_vars.get("interview_duration", "20"))
        ).replace(
            "{{additional_instructions}}", dynamic_vars.get("additional_instructions", "")
        )
        logger.debug("Modified first_message: %s", modified_config["first_message"])
    else:
        logger.warning("No first_message found in base agent config")

    # 1.6) create new agent by cloning with modified config
    name = f"UX-Интервьюер-{dynamic_vars.get('interview_topic', 'Topic')[:20]}"
    create_url = f"{ELEVEN_API_BASE}/agents/create"
    payload = {
        "from_agent_id": ELEVENLABS_BASE_AGENT_ID,
        "name": name,
        "description": dynamic_vars.get("interview_goals", ""),
        "conversation_config": modified_config,
    }
    logger.debug("Agent creation payload: %s", payload)
    async with aiohttp.ClientSession() as session:
        async with session.post(create_url, headers=headers, json=payload) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(
                    f"Failed to create agent: {resp.status} {await resp.text()}"
                )
            data = await resp.json()
        logger.debug("ElevenLabs create response: %s", data)
        agent_id = data.get("agent_id") or data.get("id")
        if not agent_id:
            raise RuntimeError("create response missing agent_id")

        # 2) get or create share link
        link_url = f"{ELEVEN_API_BASE}/agents/{agent_id}/link"
        async with session.get(link_url, headers=headers) as resp_link:
            if resp_link.status == 404:
                async with session.post(link_url, headers=headers) as resp_create:
                    if resp_create.status not in (200, 201):
                        raise RuntimeError(
                            f"Failed to create share link: {resp_create.status} "
                            f"{await resp_create.text()}"
                        )
                    link_data = await resp_create.json()
                    logger.debug("ElevenLabs link create response: %s", link_data)
            elif resp_link.status in (200, 201):
                link_data = await resp_link.json()
                logger.debug("ElevenLabs link get response: %s", link_data)
            else:
                raise RuntimeError(
                    f"Failed to fetch share link: {resp_link.status} {await resp_link.text()}"
                )

    share_url = (
        link_data.get("url")
        or link_data.get("share_link", {}).get("url")
        or link_data.get("web_url")
    )

    # If API returned token instead of URL, build signed or public link
    if not share_url:
        token_val = link_data.get("token")
        if token_val:
            # signed link with token
            share_url = (
                f"https://elevenlabs.io/app/talk-to?agent_id={agent_id}&token={token_val}"
                if isinstance(token_val, str)
                else None
            )
        # final fallback – public page pattern
        if not share_url:
            share_url = f"https://elevenlabs.io/app/talk-to?agent_id={agent_id}"
    if not share_url:
        logger.error("Could not determine share URL from link_data: %s", link_data)

    # Try signed URL API again now that overrides are enabled
    signed_url = f"{ELEVEN_API_BASE}/agents/{agent_id}/signed-url"
    async with aiohttp.ClientSession() as signed_session:
        async with signed_session.post(signed_url, headers=headers, json={"overrides": {"variables": dynamic_vars}}) as resp_signed:
            if resp_signed.status in (200, 201):
                signed_data = await resp_signed.json()
                logger.debug("ElevenLabs signed URL response: %s", signed_data)
                signed_url = signed_data.get("url")
                if signed_url:
                    logger.info("Using signed URL with overrides: %s", signed_url)
                    return {"agent_id": agent_id, "share_url": signed_url}

    # 2) since variables are now embedded in agent, use the simple share_url
    logger.info("Variables embedded in agent config, using public share URL: %s", share_url)

    return {"agent_id": agent_id, "share_url": share_url}

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
    except Exception as e:
        logger.exception("Drive error")
        await update.message.reply_text(f"Ошибка создания папки на Google Drive: {e}")
        return ConversationHandler.END

    # 2. Clone ElevenLabs agent
    try:
        agent_info = await clone_agent(user_data)
    except Exception as e:
        logger.exception("ElevenLabs error")
        await update.message.reply_text(f"Ошибка ElevenLabs: {e}")
        return ConversationHandler.END

    # 3. Reply with links
    reply = (
        "Готово! \U0001F389\n\n"
        f"• Ссылка на агента (действительна 15 мин): {agent_info['share_url']}\n"
        f"• Папка Google Drive: {folder_info['link']}\n\n"
        "Передайте ссылку респондентам сразу после генерации. \nПосле истечения 15 минут создайте новую командой /start.\nАудиозаписи будут сохраняться в указанную папку. Удачи!"
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

    # Determine run mode
    use_webhook = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    if use_webhook:
        base_url = os.environ["APP_BASE_URL"].rstrip("/")  # e.g. https://my-app.up.railway.app
        port = int(os.getenv("PORT", "8080"))
        path = f"/{TELEGRAM_TOKEN}"
        logger.info("Starting bot in WEBHOOK mode on port %s", port)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{base_url}{path}",
        )
    else:
        logger.info("Starting bot in POLLING mode…")
        application.run_polling()

if __name__ == "__main__":
    main()
