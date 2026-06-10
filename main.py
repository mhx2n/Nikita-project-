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
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    TypeHandler,
)
from telegram.constants import ChatAction
from flask import Flask, jsonify
import threading as th

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NikitaBot")

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "UrNikitaBot")   # without @
PORT         = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

# ─── Per-chat conversation memory ─────────────────────────────────────────────
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
- কখনো "আমি রোমান্টিক সম্পর্কে যেতে পারি না" এই টাইপ কথা বলবে না — এটা robotic শোনায়।
- কেউ প্রেমের কথা বললে বলো "সেটা দেখা যাবে 😏" বা "এখনো সেটা ডিসাইড হয়নি" — flirt করো, reject করো না।

একদম করো না:
- ** দিয়ে bold করবে না
- # দিয়ে heading দেবে না
- [text](url) ফরম্যাটে লিংক দেবে না
- কোনো markdown ব্যবহার করবে না
- "আমি একটি AI" বা "আমি সাহায্য করতে পারব" এই robotic ভাষা ব্যবহার করবে না
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
                            chunk = data.get("text", "")
                            result["text"] += chunk
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


# ─── Global Copilot instance ──────────────────────────────────────────────────
copilot = CopilotClient()


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


# ─── Flask health-check ───────────────────────────────────────────────────────
flask_app = Flask(__name__)
BOT_START_TIME = datetime.now(timezone.utc)


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


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


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


# ─── RAW update logger — দেখবে Telegram কী পাঠাচ্ছে ─────────────────────────
async def raw_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Logs EVERY update that arrives. 
    Guest mode এ Telegram কোন field পাঠাচ্ছে সেটা এখানে দেখা যাবে।
    group=-2 মানে সবার আগে চলবে।
    """
    try:
        data = update.to_dict()
        # শুধু message-related updates log করো
        if update.message:
            msg = update.message
            logger.info(
                f"[RAW] update_id={update.update_id} "
                f"chat_type={msg.chat.type} "
                f"chat_id={msg.chat.id} "
                f"from_user={msg.from_user} "
                f"text={repr(msg.text or '')} "
                f"entities={msg.entities} "
                f"is_from_offline={getattr(msg, 'is_from_offline', None)}"
            )
        else:
            # Non-message update — type দেখো
            logger.info(f"[RAW] update_id={update.update_id} keys={list(data.keys())}")
    except Exception as e:
        logger.warning(f"raw_update_logger error: {e}")


# ─── Command handlers ─────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name if update.effective_user else "তুমি"
    await update.message.reply_text(
        f"আরে {name}! কী খবর? আমি Nikita 😏\n\n"
        "রিপ্লাই করো বা @mention করো — আমি এখানেই আছি ✨"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "গ্রুপে @mention করো বা রিপ্লাই করো আমার মেসেজে\n"
        "Private এ সরাসরি মেসেজ করো\n\n"
        "/clear — নতুন করে শুরু করতে চাইলে"
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conversation_history[chat_id].clear()
    await update.message.reply_text("ঠিক আছে, ভুলে গেলাম সব 🌱 নতুন করে বলো")


# ─── Universal message handler ────────────────────────────────────────────────
async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    একটাই handler — সব message এখানে আসে।
    Private, group member, guest @mention, bot-to-bot সব।

    ✅ Guest Mode Fix:
    - update.message এবং update.effective_message দুটোই চেক করা হয়
    - Guest mention-এ from_user None হলেও crash হবে না
    - @mention plain text fallback আরো robust
    """
    # ── effective_message ব্যবহার করো — Guest mode সহ সব ধরনের message ধরে ──
    msg = update.effective_message
    if not msg:
        return

    # Guest mode-এ chat নাও থাকতে পারে
    if not msg.chat:
        return

    bot_username = (context.bot.username or BOT_USERNAME).lower()
    text = msg.text or msg.caption or ""
    chat_type = msg.chat.type
    chat_id = msg.chat.id

    # ── User info — Guest mode-এ from_user অনেক সময় None ────────────────────
    user = update.effective_user
    user_name = "বন্ধু"
    if user:
        user_name = user.first_name or user.username or "বন্ধু"

    # ── Private chat: সবসময় reply ────────────────────────────────────────────
    if chat_type == "private":
        clean_text = strip_mention(text, bot_username)
        if not clean_text:
            clean_text = "হ্যালো"
        logger.info(f"[PRIVATE] chat={chat_id} user={user_name} text={repr(clean_text)}")
        await send_nikita_reply(chat_id, msg.message_id, user_name, clean_text, context.bot)
        return

    # ── Group / Supergroup / Channel / Guest ──────────────────────────────────
    should_reply = False
    trigger = ""

    # 1. Entity mention — সবচেয়ে reliable (Guest mode-এও কাজ করে)
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "mention":
            mention_text = text[ent.offset: ent.offset + ent.length]
            if mention_text.lstrip("@").lower() == bot_username:
                should_reply = True
                trigger = "entity_mention"
                break

    # 2. Plain text @mention fallback — Guest mode-এ entity নাও আসতে পারে
    if not should_reply:
        if f"@{bot_username}" in text.lower():
            should_reply = True
            trigger = "text_mention_plain"

    # 3. Reply to bot's own message
    if not should_reply and msg.reply_to_message:
        rt = msg.reply_to_message
        rt_user = rt.from_user
        if rt_user:
            rt_username = (rt_user.username or "").lower()
            if rt_username == bot_username or rt_user.id == context.bot.id:
                should_reply = True
                trigger = "reply_to_bot"
        # reply_to_message.sender_chat also check (channel/guest messages)
        elif rt.sender_chat:
            if (rt.sender_chat.username or "").lower() == bot_username:
                should_reply = True
                trigger = "reply_to_bot_channel"

    # 4. Bot-to-bot (Bot Management Mode)
    if not should_reply and user and user.is_bot:
        if user.id != context.bot.id:
            should_reply = True
            trigger = "bot_to_bot"

    if not should_reply:
        return

    clean_text = strip_mention(text, bot_username)
    if not clean_text:
        clean_text = "হ্যালো"

    logger.info(f"[GROUP/{trigger}] chat={chat_id} user={user_name} text={repr(clean_text)}")
    await send_nikita_reply(chat_id, msg.message_id, user_name, clean_text, context.bot)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def run_bot():
    """Async entry point — Python 3.10+ এ asyncio.run() দিয়ে চালাতে হবে"""
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))

    # Raw logger — group=-2, সবার আগে চলে, কোনো update miss হবে না
    app.add_handler(TypeHandler(Update, raw_update_logger), group=-2)

    # Universal handler — TEXT + CAPTION, non-command, সব chat type
    # ✅ Guest Mode: filters.CAPTION যোগ করা হয়েছে
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_all_messages,
        ),
        group=0,
    )

    logger.info("𓆩♡𓆪 Nikita bot is starting…")
    await app.run_polling(
        allowed_updates=Update.ALL_TYPES,   # সব update type নাও
        drop_pending_updates=True,
        poll_interval=1,                    # 1 second — faster response
        timeout=30,
    )


def main():
    # Flask — আলাদা thread-এ চলে, কোনো asyncio conflict নেই
    flask_thread = th.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Health-check server started on port {PORT}")

    # ✅ Python 3.10+ fix: asyncio.run() দিয়ে চালাও
    # সরাসরি app.run_polling() call করলে
    # "There is no current event loop in thread 'MainThread'" error আসে
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
