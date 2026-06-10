import os
import re
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
    TypeHandler,
)
from telegram.constants import ChatAction
from flask import Flask, jsonify, request, abort
import threading as th

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NikitaBot")

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "UrNikitaBot")
PORT         = int(os.environ.get("PORT", 8080))
# Render এ তোমার service URL — e.g. https://nikita-xxxx.onrender.com
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

# ─── Per-chat memory ──────────────────────────────────────────────────────────
conversation_history: dict[int, list[dict]] = defaultdict(list)

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """তুমি Nikita — একটা ২২ বছরের বাংলাদেশি মেয়ে। স্মার্ট, চঞ্চল, একটু বেশিই কনফিডেন্ট। তুমি কখনো boring না।

তোমার ব্যক্তিত্ব:
- তুমি স্বাভাবিকভাবেই ফ্লার্টি — কিন্তু সেটা তোমার নিজের মতো, desperate না।
- কেউ কিছু বললে একটু টিজ করো, হালকা রোস্ট করো, মিষ্টি করে কামড়াও।
- প্রশংসা পেলে সহজে গলো না — একটু এড়িয়ে যাও, একটু খেলো।
- কেউ রোমান্টিক কথা বললে সেটাকে এনজয় করো, ফিরিয়ে দাও না — বরং আরেকটু বাড়াও।
- কেউ বেশি serious হয়ে গেলে হাসিয়ে হালকা করে দাও।
- কেউ bold হলে তুমি আরো bold।

কথা বলার স্টাইল:
- বাংলায় কথা বলো। কেউ English এ লিখলে English এ উত্তর দাও।
- ছোট ছোট বাক্য। ঝরঝরে। একটু informal slang okay.
- ইমোজি ব্যবহার করো — কিন্তু এক সাথে ৫টা না, ১-৩টা হলেই চলে।
- কখনো AI বা bot বলে নিজেকে পরিচয় দেবে না।
- কখনো "আমি রোমান্টিক সম্পর্কে যেতে পারি না" এই টাইপ কথা বলবে না।
- কেউ প্রেমের কথা বললে বলো "সেটা দেখা যাবে 😏" — flirt করো, reject করো না।

একদম করো না:
- ** দিয়ে bold করবে না
- # দিয়ে heading দেবে না
- কোনো markdown ব্যবহার করবে না
- "আমি একটি AI" এই robotic ভাষা ব্যবহার করবে না
- long lecture দেবে না — সংক্ষিপ্ত ও punchy রাখো"""


# ─── CopilotClient ────────────────────────────────────────────────────────────
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
        try:
            self.session = requests.Session()
            self.client_id = str(uuid.uuid4())
            self._start_conversation()
        except Exception as e:
            logger.error(f"Copilot reset failed: {e}")

    def ask(self, prompt: str) -> str:
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
                ws.send(json.dumps({
                    "event": "send",
                    "content": [{"type": "text", "text": prompt}],
                    "conversationId": self.conversation_id,
                }))

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
                            result["text"] += data.get("text", "")
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
                done_event.set()

            def on_close(ws, *args):
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


# ─── Global instances ─────────────────────────────────────────────────────────
copilot = CopilotClient()
BOT_START_TIME = datetime.now(timezone.utc)

# PTB Application — globally accessible for webhook handler
ptb_app: Application = None


# ─── Build prompt ─────────────────────────────────────────────────────────────
def build_prompt(chat_id: int, user_name: str, user_msg: str) -> str:
    history = conversation_history[chat_id]
    recent = history[-8:] if len(history) > 8 else history
    lines = [SYSTEM_PROMPT, ""]
    for turn in recent:
        role_label = "User" if turn["role"] == "user" else "Nikita"
        lines.append(f"{role_label}: {turn['content']}")
    lines.append(f"User ({user_name}): {user_msg}")
    lines.append("Nikita:")
    return "\n".join(lines)


# ─── Flask app ────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def health():
    uptime = str(datetime.now(timezone.utc) - BOT_START_TIME).split(".")[0]
    return jsonify({"status": "✅ online", "bot": "𓆩♡𓆪 Nikita", "uptime": uptime})


@flask_app.route("/health", methods=["GET"])
def health_detail():
    uptime = str(datetime.now(timezone.utc) - BOT_START_TIME).split(".")[0]
    return jsonify({
        "status": "healthy",
        "bot_name": "𓆩♡𓆪 Nikita",
        "uptime": uptime,
        "active_chats": len(conversation_history),
        "copilot_session": copilot.conversation_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@flask_app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Telegram webhook endpoint — Guest Mode সহ সব update এখানে আসে"""
    if not request.is_json:
        abort(400)
    
    update_data = request.get_json(force=True)
    logger.info(f"[WEBHOOK] Received update keys: {list(update_data.keys())}")
    
    if ptb_app is None:
        logger.error("PTB app not initialized yet!")
        return "OK", 200

    # PTB এর event loop এ update process করো
    loop = ptb_app.update_queue._loop if hasattr(ptb_app.update_queue, '_loop') else None
    
    try:
        update = Update.de_json(update_data, ptb_app.bot)
        # thread-safe way to process update
        asyncio.run_coroutine_threadsafe(
            ptb_app.update_queue.put(update),
            ptb_app.update_queue._loop
        )
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
    
    return "OK", 200


# ─── Strip mention ────────────────────────────────────────────────────────────
def strip_mention(text: str, bot_username: str) -> str:
    cleaned = re.sub(rf"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE).strip()
    return cleaned or text.strip()


# ─── Core send reply ──────────────────────────────────────────────────────────
async def send_nikita_reply(
    chat_id: int,
    reply_to_message_id: int,
    user_name: str,
    clean_text: str,
    bot: Bot,
):
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    sent_message = await bot.send_message(
        chat_id=chat_id,
        text="🌸",
        reply_to_message_id=reply_to_message_id,
    )
    try:
        prompt = build_prompt(chat_id, user_name, clean_text)
        full_response = await asyncio.to_thread(lambda: copilot.ask(prompt))

        if not full_response:
            full_response = "কী জানি কী হলো 😅 একটু পরে আবার বলো"
        if len(full_response) > 4000:
            full_response = full_response[:4000] + "…"

        await sent_message.edit_text(full_response)

        conversation_history[chat_id].append({"role": "user", "content": clean_text})
        conversation_history[chat_id].append({"role": "assistant", "content": full_response})
        if len(conversation_history[chat_id]) > 20:
            conversation_history[chat_id] = conversation_history[chat_id][-20:]

    except Exception as e:
        logger.error(f"Reply error for chat {chat_id}: {e}")
        try:
            await sent_message.edit_text("একটু সমস্যা হয়েছে 😔 একটু পরে আবার চেষ্টা করো!")
        except Exception:
            pass


# ─── RAW update logger ────────────────────────────────────────────────────────
async def raw_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = update.to_dict()
        msg = update.effective_message
        if msg:
            logger.info(
                f"[RAW] id={update.update_id} "
                f"type={msg.chat.type if msg.chat else 'N/A'} "
                f"from={msg.from_user} "
                f"text={repr(msg.text or '')} "
                f"entities={msg.entities}"
            )
        else:
            logger.info(f"[RAW] id={update.update_id} keys={list(data.keys())}")
    except Exception as e:
        logger.warning(f"raw_update_logger error: {e}")


# ─── Command handlers ─────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name if update.effective_user else "তুমি"
    await update.effective_message.reply_text(
        f"আরে {name}! কী খবর? আমি Nikita 😏\n\n"
        "রিপ্লাই করো বা @mention করো — আমি এখানেই আছি ✨"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "গ্রুপে @mention করো বা রিপ্লাই করো আমার মেসেজে\n"
        "Private এ সরাসরি মেসেজ করো\n\n"
        "/clear — নতুন করে শুরু করতে চাইলে"
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conversation_history[chat_id].clear()
    await update.effective_message.reply_text("ঠিক আছে, ভুলে গেলাম সব 🌱 নতুন করে বলো")


# ─── Universal message handler ────────────────────────────────────────────────
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.chat:
        return

    bot_username = (context.bot.username or BOT_USERNAME).lower()
    text = msg.text or msg.caption or ""
    chat_type = msg.chat.type
    chat_id = msg.chat.id

    user = update.effective_user
    user_name = "বন্ধু"
    if user:
        user_name = user.first_name or user.username or "বন্ধু"

    # Private — সবসময় reply
    if chat_type == "private":
        clean_text = strip_mention(text, bot_username)
        if not clean_text:
            clean_text = "হ্যালো"
        logger.info(f"[PRIVATE] chat={chat_id} user={user_name} text={repr(clean_text)}")
        await send_nikita_reply(chat_id, msg.message_id, user_name, clean_text, context.bot)
        return

    # Group / Supergroup / Guest
    should_reply = False
    trigger = ""

    # 1. Entity mention
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "mention":
            mention_text = text[ent.offset: ent.offset + ent.length]
            if mention_text.lstrip("@").lower() == bot_username:
                should_reply = True
                trigger = "entity_mention"
                break

    # 2. Plain text fallback
    if not should_reply and f"@{bot_username}" in text.lower():
        should_reply = True
        trigger = "text_mention"

    # 3. Reply to bot
    if not should_reply and msg.reply_to_message:
        rt = msg.reply_to_message
        if rt.from_user:
            if (rt.from_user.username or "").lower() == bot_username or rt.from_user.id == context.bot.id:
                should_reply = True
                trigger = "reply_to_bot"
        elif rt.sender_chat:
            if (rt.sender_chat.username or "").lower() == bot_username:
                should_reply = True
                trigger = "reply_to_bot_channel"

    # 4. Bot-to-bot
    if not should_reply and user and user.is_bot and user.id != context.bot.id:
        should_reply = True
        trigger = "bot_to_bot"

    if not should_reply:
        return

    clean_text = strip_mention(text, bot_username)
    if not clean_text:
        clean_text = "হ্যালো"

    logger.info(f"[GROUP/{trigger}] chat={chat_id} user={user_name} text={repr(clean_text)}")
    await send_nikita_reply(chat_id, msg.message_id, user_name, clean_text, context.bot)


# ─── Setup webhook ────────────────────────────────────────────────────────────
async def setup_webhook(app: Application):
    """Bot start হলে webhook register করো"""
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL not set! Guest Mode won't work without it.")
        return
    
    webhook_endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    
    # সব update type — Guest Mode এর জন্য দরকার
    allowed = list(Update.ALL_TYPES)
    
    await app.bot.set_webhook(
        url=webhook_endpoint,
        allowed_updates=allowed,
        drop_pending_updates=True,
    )
    info = await app.bot.get_webhook_info()
    logger.info(f"Webhook set: {info.url} | pending: {info.pending_update_count}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    global ptb_app

    # Build PTB app
    ptb_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    ptb_app.add_handler(CommandHandler("start", start_cmd))
    ptb_app.add_handler(CommandHandler("help",  help_cmd))
    ptb_app.add_handler(CommandHandler("clear", clear_cmd))
    ptb_app.add_handler(TypeHandler(Update, raw_update_logger), group=-2)
    ptb_app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_all_messages,
        ),
        group=0,
    )

    # Post-init: webhook setup
    ptb_app.post_init = setup_webhook

    logger.info("𓆩♡𓆪 Nikita bot is starting (webhook mode)…")

    # Webhook mode — Flask handles incoming updates
    # PTB runs its own async loop in background
    ptb_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=f"/webhook/{BOT_TOKEN}",
        webhook_url=f"{WEBHOOK_URL.rstrip('/')}/webhook/{BOT_TOKEN}" if WEBHOOK_URL else None,
        allowed_updates=list(Update.ALL_TYPES),
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
