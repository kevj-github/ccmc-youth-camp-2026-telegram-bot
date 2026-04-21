import os
import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.error import BadRequest, TelegramError
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
# Silence noisy library loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
ALLOWED_CHAT_IDS = [
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip().isdigit()
]
PORT = int(os.environ.get("PORT", 8000))
MAX_TELEGRAM_MSG_LEN = 4000  # Telegram limit is 4096, leave some room

bot_app = Application.builder().token(TELEGRAM_TOKEN).build()


async def _safe_reply(message, text: str):
    """Send a reply, handling long messages and Telegram errors gracefully."""
    # Telegram caps messages at 4096 chars — split if needed
    chunks = [text[i:i + MAX_TELEGRAM_MSG_LEN] for i in range(0, len(text), MAX_TELEGRAM_MSG_LEN)] or [""]
    for chunk in chunks:
        try:
            await message.reply_text(chunk)
        except BadRequest as e:
            logger.warning(f"Telegram BadRequest: {e}. Retrying as plain text.")
            try:
                # Strip anything that could confuse Telegram
                clean = chunk.encode("utf-8", errors="ignore").decode("utf-8")
                await message.reply_text(clean[:MAX_TELEGRAM_MSG_LEN])
            except TelegramError as e2:
                logger.error(f"Failed to send reply even after cleaning: {e2}")
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    user_name = user.first_name if user else "Unknown"

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Unauthorized access from chat_id={chat_id} ({user_name})")
        try:
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
        except TelegramError:
            pass
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    logger.info(f"Message from {user_name} ({chat_id}): {text[:200]}")

    thinking_msg = None
    try:
        thinking_msg = await update.message.reply_text("⏳ Thinking...")
    except TelegramError as e:
        logger.warning(f"Could not send 'thinking' message: {e}")

    try:
        reply = ask_gemini(text)
    except Exception as e:
        logger.error(f"Error from Gemini: {e}", exc_info=True)
        reply = f"❌ Something went wrong: {e}"

    # Delete the "thinking" message (best-effort)
    if thinking_msg:
        try:
            await thinking_msg.delete()
        except TelegramError:
            pass

    if not reply or not reply.strip():
        reply = "I didn't get a response. Please try again."

    await _safe_reply(update.message, reply)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    try:
        await update.message.reply_text(
            "I only understand text messages. Try:\n"
            "• How many campers are registered?\n"
            "• Show unpaid registrations\n"
            "• Add a column called Fees paid\n"
            "• Analyze photo john_doe.jpg"
        )
    except TelegramError:
        pass


bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
bot_app.add_handler(MessageHandler(~filters.TEXT, handle_unknown))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await bot_app.initialize()
    try:
        await bot_app.bot.set_webhook(f"{WEBHOOK_URL}/webhook", drop_pending_updates=False)
        logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    except TelegramError as e:
        logger.error(f"Failed to set webhook: {e}")
    yield
    # Shutdown — do NOT delete the webhook, Render restarts should keep it alive
    try:
        await bot_app.shutdown()
    except Exception as e:
        logger.error(f"Shutdown error: {e}")


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        if update:
            await bot_app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
    return {"ok": True}


@app.get("/")
async def health_get():
    return {"status": "running"}


@app.head("/")
async def health_head():
    return Response(status_code=200)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)