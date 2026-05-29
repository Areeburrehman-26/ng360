# NG360 Bot (Mac Mini Production)

This project runs the full NG360 quote pipeline:

- Receives webhook calls from GHL on port `8004`
- Queues jobs in `data/ng360_queue.json`
- Worker pulls queued jobs and runs `run_bot()` in `core/bridge_bot.py`
- Writes success/failure back to GHL
- Optionally uploads PDF to Google Drive
- Optionally sends Slack notifications

## 1) One-time setup on Mac mini

```bash
cd /path/to/nsg360_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Fill `.env` with real values before starting.

## 2) Required environment values

Required:

- `GHL_API_KEY`
- `GHL_LOCATION_ID`
- `NATGEN_USERNAME`
- `NATGEN_PASSWORD`
- `NATGEN_AGENT_ID`
- `ENABLE_NGROK=1`
- One of:
  - `NGROK_URL` (already provisioned static URL), or
  - `NGROK_DOMAIN` (static domain), or
  - `NGROK_AUTH_TOKEN` (auto tunnel start)

Optional integrations:

- Google Drive: `GOOGLE_DRIVE_CREDENTIALS_PATH`, `GDRIVE_FOLDER_ID`
- Slack: `SLACK_WEBHOOK_QUOTES`, `SLACK_WEBHOOK_ALERTS`

## 3) Start production flow

```bash
./start_bot.sh
```

Startup behavior:

- Runs syntax/import checks and pytest (unless skipped)
- Starts `core.webhook_server` and `core.worker`
- Starts ngrok if `ENABLE_NGROK=1`
- Prints public webhook URL for GHL, for example:
  - `https://your-domain.ngrok.app/webhook`

## 4) Configure GHL webhook action

In GHL workflow:

- Method: `POST`
- URL: `{YOUR_PUBLIC_NGROK_URL}/webhook`
- Body (JSON):

```json
{
  "contact_id": "{{contact.id}}",
  "state": "GA"
}
```

`state` is optional if state is present on the contact itself.

## 5) Health and queue checks

```bash
curl http://localhost:8004/health
curl http://localhost:8004/queue
```

`/health` returns `public_webhook_url` when ngrok is enabled.

## 6) Operational notes

- Do not edit `core/bridge_bot.py` in production flow updates.
- If `instantautofill` tag exists on a contact, webhook enqueues it at `EXTREME` priority.
- Worker timeout/crash/fetch failures now write `failed` state back to GHL and trigger Slack failure notifications when configured.
- To stop all services, use `Ctrl+C` in the `start_bot.sh` terminal.
