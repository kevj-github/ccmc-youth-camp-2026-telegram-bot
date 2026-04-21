# Youth Camp Telegram Bot

A Telegram bot that lets you read and edit your camp registration Google Sheet,
and analyze images uploaded via Google Form — all powered by Claude AI.

## How it works

```
You (Telegram) → Bot → Claude Sonnet → Google Sheets / Drive
```

Claude understands natural language and decides which tools to call.
No need to memorize commands — just ask in plain English.

---

## Setup

### 1. Create your Telegram Bot

1. Open Telegram and message `@BotFather`
2. Send `/newbot` and follow the prompts
3. Copy the **bot token**
4. Get your own chat ID by messaging `@userinfobot`

---

### 2. Set up Google Cloud

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (e.g. "Youth Camp Bot")
3. Enable these APIs:
   - **Google Sheets API**
   - **Google Drive API**
4. Go to **IAM & Admin → Service Accounts**
5. Create a service account (name it anything, e.g. "camp-bot")
6. Click the service account → **Keys** tab → **Add Key → JSON**
7. Save the downloaded file as `service_account.json` in this folder

**Share access with the service account:**
- Open your Google Sheet → Share → paste the service account email → Editor
- Open your Google Drive folder → Share → paste the service account email → Viewer

---

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in all the values:
- `TELEGRAM_TOKEN` — from BotFather
- `ALLOWED_CHAT_IDS` — your Telegram user ID(s), comma-separated
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com
- `SPREADSHEET_ID` — from your Google Sheet URL
- `DRIVE_FOLDER_ID` — from your Google Drive folder URL
- `SHEET_NAME` — the tab name (default: Sheet1)

---

### 4. Install and run

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python bot.py
```

---

## Example Telegram messages

| What you type | What happens |
|---|---|
| How many campers are registered? | Reads sheet, counts rows |
| Show me all unpaid registrations | Reads sheet, filters by payment status |
| Mark row 5 as paid | Updates the cell in the sheet |
| List the images in Drive | Shows all uploaded files |
| Analyze photo named john_doe | Downloads from Drive, Claude describes it |
| What documents did Sarah upload? | Finds image by name and analyzes it |

---

## Deploying 24/7 (free options)

### Railway (recommended)
1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add all your `.env` variables in the Railway dashboard
4. Upload `service_account.json` as a file or paste its contents as an env var

### Render
1. Push to GitHub
2. Go to [render.com](https://render.com) → New Web Service
3. Set start command: `python bot.py`
4. Add environment variables

### Self-hosted (Raspberry Pi / VPS)
```bash
# Run in background with screen
screen -S campbot
python bot.py
# Ctrl+A then D to detach
```

---

## File structure

```
youth-camp-bot/
├── bot.py              # Telegram bot entry point
├── claude_handler.py   # Claude AI logic + tool definitions
├── google_services.py  # Google Sheets + Drive API helpers
├── requirements.txt
├── .env.example
├── .env                # Your secrets (never commit this!)
├── service_account.json  # Google credentials (never commit this!)
└── logs/
    └── bot.log
```

---

## Security notes

- Only users in `ALLOWED_CHAT_IDS` can use the bot
- Never commit `.env` or `service_account.json` to GitHub
- Add both to `.gitignore`
