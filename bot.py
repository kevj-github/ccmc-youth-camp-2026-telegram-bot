import os
import logging
import asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from gemini_handler import ask_gemini

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
ALLOWED_CHAT_IDS = list(map(int, os.environ.get("ALLOWED_CHAT_IDS", "").split(",")))
PORT = int(os.environ.get("PORT", 8000))

bot_app = Application.builder().token(TELEGRAM_TOKEN).build()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Unauthorized access from chat_id={chat_id}")
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    text = update.message.text or ""
    logger.info(f"Message from {user.first_name} ({chat_id}): {text}")

    thinking_msg = await update.message.reply_text("⏳ Thinking...")

    try:
        reply = ask_gemini(text)
    except Exception as e:
        logger.error(f"Error from Gemini: {e}", exc_info=True)
        reply = f"❌ Something went wrong: {e}"

    await thinking_msg.delete()
    await update.message.reply_text(reply)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "I only understand text messages. Try:\n"
        "• How many campers are registered?\n"
        "• Show unpaid registrations\n"
        "• Analyze photo john_doe.jpg"
    )


bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
bot_app.add_handler(MessageHandler(~filters.TEXT, handle_unknown))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await bot_app.initialize()
    await bot_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    yield
    # Shutdown — do NOT delete the webhook here, Render restarts shouldn't break it
    await bot_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}


@app.get("/")
async def health():
    return {"status": "running"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)