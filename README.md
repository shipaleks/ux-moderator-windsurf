# UX Moderator Bot (Windsurf)

Minimal evening-MVP: Telegram bot + ElevenLabs Conversational Agent + Google Drive folders.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill secrets
python bot.py
```

## Env vars (put in Railway variables or local `.env`)

```
TELEGRAM_BOT_TOKEN=…
ELEVENLABS_API_KEY=…
ELEVENLABS_BASE_AGENT_ID=…
GOOGLE_DRIVE_PARENT_FOLDER_ID=…
```

Keep `service_account.json` (Google Drive service account) in project root or set path via `GOOGLE_SERVICE_ACCOUNT_JSON`.

## Deploy to Railway

1. Connect this repo.
2. Set environment variables above.
3. Railway auto-detects `Procfile` and runs `python bot.py`.

## Roadmap

> Minor touch to trigger Railway redeploy

- [x] Collect interview parameters via Telegram conversation.
- [x] Create Drive folder & share link.
- [x] Clone ElevenLabs base agent and return share link.
- [ ] Cron job: poll completed conversations, download audio, upload to Drive.
- [ ] Error monitoring & logging.
