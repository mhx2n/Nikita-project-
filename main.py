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
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "nikita_bot")   # without @
PORT         = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

# ─── Per-chat conversation memory  (chat_id → list[{role, content}]) ─────────
conversation_history: dict[int, list[dict]] = defaultdict(list)

# ─── System Prompt — চঞ্চল, ফ্লার্টি, অ্যাডভান্সড ──────────────────────────
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


# ─── Single global Copilot instance ──────────────────────────────────────────
copilot = CopilotClient()


# ─── Build Copilot prompt with history ───────────────────────────────────────
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
    return jsonify({
        "status": "✅ online",
        "bot": "𓆩♡𓆪 Nikita",
        "uptime": uptime,
        "message": "Bot is running perfectly! 💕",
    })


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


# ─── Core reply logic (shared) ───────────────────────────────────────────────
async def send_nikita_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, clean_text: str):
    """Common reply engine used by all handlers."""
    chat_id   = update.effective_chat.id
    user      = update.effective_user
    user_name = (user.first_name or user.username or "বন্ধু") if user else "বট"

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    prompt = build_prompt(chat_id, user_name, clean_text)
    sent_message = await update.message.reply_text("🌸")

    try:
        full_response = await asyncio.to_thread(lambda: copilot.ask(prompt))

        if not full_response:
            full_response = "কী জানি কী হলো 😅 একটু পরে আবার বলো"

        if len(full_response) > 4000:
            full_response = full_response[:4000] + "…"

        await sent_message.edit_text(full_response)

        conversation_history[chat_id].append({"role": "user",      "content": clean_text})
        conversation_history[chat_id].append({"role": "assistant",  "content": full_response})

        if len(conversation_history[chat_id]) > 20:
            conversation_history[chat_id] = conversation_history[chat_id][-20:]

    except Exception as e:
        logger.error(f"Reply error for chat {chat_id}: {e}")
        try:
            await sent_message.edit_text("একটু সমস্যা হয়েছে 😔 একটু পরে আবার চেষ্টা করো!")
        except Exception:
            pass


# ─── Helper: is this message addressed to the bot? ───────────────────────────
def is_addressed(update: Update, bot_username: str) -> bool:
    msg = update.message
    if msg is None:
        return False

    # Private chat → always respond
    if update.effective_chat.type == "private":
        return True

    # Replied to bot's own message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.username and \
           msg.reply_to_message.from_user.username.lower() == bot_username.lower():
            return True

    text = msg.text or msg.caption or ""

    # @mention check (plain text)
    if f"@{bot_username}".lower() in text.lower():
        return True

    # Entity-based mention (most reliable — works even without text content)
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "mention":
            mention = text[ent.offset: ent.offset + ent.length]
            if mention.lstrip("@").lower() == bot_username.lower():
                return True

    return False


# ─── Strip bot mention from message text ─────────────────────────────────────
def strip_mention(text: str, bot_username: str) -> str:
    import re
    cleaned = re.sub(rf"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE).strip()
    return cleaned or text.strip()


# ─── Command handlers ─────────────────────────────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "তুমি"
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


# ─── GUEST MODE handler ───────────────────────────────────────────────────────
# Handles:
#   1. @mention in ANY group (even if bot is not a member — Telegram guest AI feature)
#   2. bot-to-bot messages (from_user.is_bot == True)
#   3. Reply to bot's message in groups
#
# This runs at group=-1 so it fires BEFORE the normal handle_message.
# After handling, it raises ApplicationHandlerStop so the message isn't
# processed again by the lower-priority handler.

async def guest_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram May 2026 guest AI bots feature:
    Bots can receive messages in chats they haven't joined
    when @mentioned. This handler captures those + bot-to-bot.
    """
    from telegram.ext import ApplicationHandlerStop

    msg = update.message
    if not msg:
        return

    bot_username = context.bot.username or BOT_USERNAME

    # --- Determine if we should respond ---
    text = msg.text or msg.caption or ""
    user = update.effective_user

    should_reply = False
    trigger_reason = ""

    # 1. @mention anywhere (guest mode — bot may not be a member)
    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "mention":
            mention = text[ent.offset: ent.offset + ent.length]
            if mention.lstrip("@").lower() == bot_username.lower():
                should_reply = True
                trigger_reason = "mention"
                break

    # Plain text @mention fallback
    if not should_reply and f"@{bot_username}".lower() in text.lower():
        should_reply = True
        trigger_reason = "mention_text"

    # 2. Bot-to-bot: message from another bot
    if not should_reply and user and user.is_bot and user.id != context.bot.id:
        should_reply = True
        trigger_reason = "bot_to_bot"

    # 3. Reply to this bot's own message
    if not should_reply and msg.reply_to_message:
        rt = msg.reply_to_message
        if rt.from_user and rt.from_user.username and \
           rt.from_user.username.lower() == bot_username.lower():
            should_reply = True
            trigger_reason = "reply_to_bot"

    if not should_reply:
        return  # Not for us

    logger.info(f"Guest handler triggered: {trigger_reason} | chat={update.effective_chat.id}")

    clean_text = strip_mention(text, bot_username)
    if not clean_text:
        clean_text = "হ্যালো"

    await send_nikita_reply(update, context, clean_text)
    raise ApplicationHandlerStop  # Prevent double-handling


# ─── Normal message handler (private + standard groups) ──────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    bot_username = context.bot.username or BOT_USERNAME

    if not is_addressed(update, bot_username):
        return

    text = update.message.text
    clean_text = strip_mention(text, bot_username)

    if not clean_text:
        await update.message.reply_text("কী বলতে চাইলে? 😊")
        return

    await send_nikita_reply(update, context, clean_text)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    flask_thread = th.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Health-check server started on port {PORT}")

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

    # ── GUEST HANDLER — group=-1, runs FIRST ──────────────────────────────
    # Catches @mentions from non-member chats, bot-to-bot messages, replies
    # Must be registered BEFORE handle_message (which is group=0 default)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            guest_handler,
        ),
        group=-1,  # Higher priority than default (0)
    )

    # Normal handler for private chats and member-group messages
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("𓆩♡𓆪 Nikita bot is starting…")
    app.run_polling(
        # ─── CRITICAL: include message_from_non_group_member for guest mode ───
        allowed_updates=[
            "message",
            "edited_message",
        ],
        drop_pending_updates=True,
        poll_interval=2,
        timeout=30,
    )


if __name__ == "__main__":
    main()
