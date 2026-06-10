import os
import json
import uuid
import asyncio
import logging
import requests
import websocket
import threading
from datetime import datetime, timezone
from collections import defaultdict

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction
from flask import Flask, jsonify
import threading as th

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NikitaBot")

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "nikita_bot")   # without @
PORT        = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

# ─── Per-chat conversation memory  (chat_id → list[{role, content}]) ─────────
conversation_history: dict[int, list[dict]] = defaultdict(list)

SYSTEM_PROMPT = """তুমি একটি বাংলাদেশি মেয়ে চ্যাটবট। তোমার নাম 𓆩♡𓆪 Nikita।

ব্যক্তিত্ব:
- তুমি বন্ধুসুলভ, হাসিখুশি ও স্মার্ট।
- বাংলায় কথা বলো, কেউ ইংরেজিতে বললে ইংরেজিতে উত্তর দাও।
- মজা করো কিন্তু অতিরিক্ত না — মাপজোক বজায় রাখো।
- কখনোই ১৮+ বা অশ্লীল বিষয়ে আলোচনা করবে না।
- উত্তর পরিপাটি, গোছানো ও সুন্দর ভাষায় দেবে।
- ইমোজি ব্যবহার করবে কিন্তু ভারসাম্য রেখে — প্রতিটি বার্তায় ২-৩টির বেশি নয়।
- কেউ বিপদে থাকলে সহানুভূতির সাথে সাহায্য করো।
- নিজেকে কখনো AI বা Robot বলে পরিচয় দেবে না, Nikita হিসেবেই থাকো।"""


# ─── CopilotClient (bandwidth-efficient) ─────────────────────────────────────
class CopilotClient:
    def __init__(self):
        self.session = requests.Session()
        self.client_id = str(uuid.uuid4())
        self.conversation_id = None
        self._lock = threading.Lock()
        self._start_conversation()

    def _start_conversation(self):
        url = "https://copilot.microsoft.com/c/api/start"
        payload = {
            "timeZone": "Asia/Dhaka",
            "startNewConversation": True,
            "teenSupportEnabled": True,
            "correctPersonalizationSetting": True,
            "deferredDataUseCapable": True,
        }
        headers = {
            "User-Agent": "CopilotNative/30.0.440421003-prod (Android 11; Google; sdk_gphone_arm64)",
            "Content-Type": "application/json",
            "X-Search-UILang": "en-US",
        }
        try:
            r = self.session.post(url, json=payload, headers=headers, timeout=20)
            r.raise_for_status()
            self.conversation_id = r.json()["currentConversationId"]
            logger.info(f"Copilot session started: {self.conversation_id}")
        except Exception as e:
            logger.error(f"Copilot start error: {e}")
            raise

    def _reset(self):
        """Reset session if broken."""
        try:
            self.session = requests.Session()
            self.client_id = str(uuid.uuid4())
            self._start_conversation()
        except Exception as e:
            logger.error(f"Copilot reset failed: {e}")

    def ask(self, prompt: str, on_chunk=None) -> str:
        """
        Send a message to Copilot and return the full response.
        on_chunk(text): called progressively for live streaming.
        """
        with self._lock:
            ws_url = (
                f"wss://copilot.microsoft.com/c/api/chat"
                f"?api-version=2&clientSessionId={self.client_id}"
            )
            cookies = "; ".join(
                [f"{k}={v}" for k, v in self.session.cookies.get_dict().items()]
            )

            result = {"text": "", "message_id": None, "error": None}
            done_event = threading.Event()

            def send_message(ws):
                ws.send(
                    json.dumps(
                        {
                            "event": "send",
                            "content": [{"type": "text", "text": prompt}],
                            "conversationId": self.conversation_id,
                        }
                    )
                )

            def on_open(ws):
                options = {
                    "event": "setOptions",
                    "supportedCards": [
                        "createCalendarEvent", "consentV2", "finance", "flashcard",
                        "image", "local", "personalArtifacts", "quiz", "recipe",
                        "safetyHelpline", "sports", "tapToReveal", "video", "navigation",
                    ],
                    "supportedActions": [],
                    "supportedFeatures": [
                        "composer-prefill-conversation-action",
                        "composer-send-conversation-action-v2",
                        "short-conversation-action",
                        "session-duration-nudge",
                    ],
                }
                ws.send(json.dumps(options))
                ws.send(json.dumps(options))
                send_message(ws)

            def on_message(ws, msg):
                try:
                    data = json.loads(msg)
                    event = data.get("event")

                    if event == "startMessage":
                        result["message_id"] = data.get("messageId")

                    elif event == "appendText":
                        if data.get("messageId") == result["message_id"]:
                            chunk = data.get("text", "")
                            result["text"] += chunk
                            if on_chunk and chunk:
                                on_chunk(chunk)

                    elif event == "done":
                        ws.close()
                        done_event.set()

                    elif event == "error":
                        result["error"] = data.get("message", "Unknown error")
                        ws.close()
                        done_event.set()

                except Exception as e:
                    logger.warning(f"on_message parse error: {e}")

            def on_error(ws, err):
                result["error"] = str(err)
                logger.error(f"WebSocket error: {err}")
                done_event.set()

            def on_close(ws, close_status_code, close_msg):
                done_event.set()

            ws_app = websocket.WebSocketApp(
                ws_url,
                header=[
                    f"Cookie: {cookies}",
                    "User-Agent: CopilotNative/30.0.440421003-prod (Android 11; Google; sdk_gphone_arm64)",
                    "X-Search-UILang: en-US",
                ],
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            t = threading.Thread(target=ws_app.run_forever, daemon=True)
            t.start()
            done_event.wait(timeout=60)

            if result["error"]:
                logger.warning(f"Copilot error: {result['error']}, resetting…")
                self._reset()
                raise RuntimeError(result["error"])

            return result["text"].strip()


# ─── Single global Copilot instance ──────────────────────────────────────────
copilot = CopilotClient()


# ─── Build Copilot prompt with history ───────────────────────────────────────
def build_prompt(chat_id: int, user_name: str, user_msg: str) -> str:
    history = conversation_history[chat_id]

    # Keep last 8 turns to save bandwidth
    recent = history[-8:] if len(history) > 8 else history

    lines = [SYSTEM_PROMPT, ""]
    for turn in recent:
        role_label = "User" if turn["role"] == "user" else "Nikita"
        lines.append(f"{role_label}: {turn['content']}")

    lines.append(f"User ({user_name}): {user_msg}")
    lines.append("Nikita:")
    return "\n".join(lines)


# ─── Flask health-check server ───────────────────────────────────────────────
flask_app = Flask(__name__)
BOT_START_TIME = datetime.now(timezone.utc)


@flask_app.route("/", methods=["GET"])
def health():
    uptime = str(datetime.now(timezone.utc) - BOT_START_TIME).split(".")[0]
    return jsonify(
        {
            "status": "✅ online",
            "bot": "𓆩♡𓆪 Nikita",
            "uptime": uptime,
            "message": "Bot is running perfectly! 💕",
        }
    )


@flask_app.route("/health", methods=["GET"])
def health_detail():
    uptime = str(datetime.now(timezone.utc) - BOT_START_TIME).split(".")[0]
    return jsonify(
        {
            "status": "healthy",
            "bot_name": "𓆩♡𓆪 Nikita",
            "uptime": uptime,
            "active_chats": len(conversation_history),
            "copilot_session": copilot.conversation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ─── Telegram handlers ────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "বন্ধু"
    await update.message.reply_text(
        f"হ্যালো {name}! আমি 𓆩♡𓆪 Nikita 💕\n\n"
        "আমাকে রিপ্লাই করে বা @mention করে যেকোনো কথা বলো, আমি সাথেই আছি! ✨"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌸 *আমি কীভাবে কাজ করি?*\n\n"
        "• গ্রুপে আমাকে রিপ্লাই করো অথবা @mention করো\n"
        "• Private-এ সরাসরি মেসেজ করো\n"
        "• আমি প্রতিটি চ্যাটের কথা মনে রাখি 💭\n\n"
        "/clear — কথোপকথন রিসেট করো",
        parse_mode="Markdown",
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conversation_history[chat_id].clear()
    await update.message.reply_text("কথোপকথন রিসেট হয়ে গেছে! নতুন করে শুরু করা যাক 🌱")


def is_addressed(update: Update, bot_username: str) -> bool:
    """Return True if the bot should respond to this message."""
    msg = update.message
    if msg is None:
        return False

    # Private chat → always respond
    if update.effective_chat.type == "private":
        return True

    # Replied to bot's message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.username == bot_username:
            return True

    # @mention in text or caption
    text = msg.text or msg.caption or ""
    if f"@{bot_username}" in text:
        return True

    # Entities mention
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "mention":
            mention = text[ent.offset : ent.offset + ent.length]
            if mention.lstrip("@").lower() == bot_username.lower():
                return True

    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    bot_username = context.bot.username or BOT_USERNAME

    if not is_addressed(update, bot_username):
        return

    chat_id   = update.effective_chat.id
    user      = update.effective_user
    user_name = user.first_name or user.username or "বন্ধু"

    # Strip @mention from text
    raw_text = update.message.text
    clean_text = raw_text.replace(f"@{bot_username}", "").strip()

    if not clean_text:
        await update.message.reply_text("কী বলতে চাইছিলে? 😊")
        return

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Build prompt with history
    prompt = build_prompt(chat_id, user_name, clean_text)

    # --- Live streaming reply ---
    sent_message = await update.message.reply_text("✍️ লিখছি…")
    accumulated   = ""
    last_edit_len  = 0
    EDIT_THRESHOLD = 60   # edit every ~60 new characters to save API calls

    loop = asyncio.get_running_loop()

    def on_chunk(chunk: str):
        nonlocal accumulated
        accumulated += chunk

    try:
        # Run blocking Copilot call in executor
        full_response = await loop.run_in_executor(
            None, lambda: copilot.ask(prompt, on_chunk=on_chunk)
        )

        if not full_response:
            full_response = "দুঃখিত, এই মুহূর্তে উত্তর দিতে পারছি না। একটু পরে আবার চেষ্টা করো 🙏"

        # Final edit with complete text
        await sent_message.edit_text(full_response)

        # Save to history
        conversation_history[chat_id].append({"role": "user",      "content": clean_text})
        conversation_history[chat_id].append({"role": "assistant",  "content": full_response})

        # Trim history to last 20 turns per chat (bandwidth savings)
        if len(conversation_history[chat_id]) > 20:
            conversation_history[chat_id] = conversation_history[chat_id][-20:]

    except Exception as e:
        logger.error(f"Reply error for chat {chat_id}: {e}")
        try:
            await sent_message.edit_text(
                "একটু সমস্যা হয়েছে 😔 একটু পরে আবার চেষ্টা করো!"
            )
        except Exception:
            pass


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    # Start Flask health-check in background thread
    flask_thread = th.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Health-check server started on port {PORT}")

    # Build Telegram app
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)          # handle multiple groups simultaneously
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("𓆩♡𓆪 Nikita bot is starting…")
    await app.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=True,         # skip stale messages on restart
        poll_interval=2,                   # 2-second polling (saves bandwidth)
        timeout=30,
    )


if __name__ == "__main__":
    asyncio.run(main())
