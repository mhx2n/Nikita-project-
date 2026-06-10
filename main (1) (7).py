import os
import re
import json
import uuid
import time
import psutil
import asyncio
import logging
import requests
import threading
from datetime import datetime, timezone
from collections import defaultdict

from telegram import Update, Bot, InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    TypeHandler,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
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
OWNER_ID     = int(os.environ.get("OWNER_ID", "0"))   # তোমার Telegram user ID
PORT         = int(os.environ.get("PORT", 8080))
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set!")

# ─── Per-chat memory ──────────────────────────────────────────────────────────
conversation_history: dict[int, list[dict]] = defaultdict(list)

# ─── Stats tracking ───────────────────────────────────────────────────────────
BOT_START_TIME   = datetime.now(timezone.utc)
active_users:  set[int] = set()   # private chat user IDs
active_groups: set[int] = set()   # group chat IDs

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


# ══════════════════════════════════════════════════════════════════════════════
#  PERPLEXITY CLIENT
# ══════════════════════════════════════════════════════════════════════════════

class PerplexityClient:
    """Perplexity AI ব্যবহার করে প্রশ্নের উত্তর দেয়।"""

    BASE_URL = "https://www.perplexity.ai"
    SSE_URL  = "https://www.perplexity.ai/rest/sse/perplexity_ask"

    _MOBILE_UA = (
        "Mozilla/5.0 (Linux; Android 10; Redmi 8A Dual Build/QKQ1.191014.001) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.7499.34 Mobile Safari/537.36"
    )

    def __init__(self):
        self._lock = threading.Lock()

    # ── session scrape ─────────────────────────────────────────────────────────
    def _scrape_session(self):
        session = requests.Session()
        headers = {
            "User-Agent":        self._MOBILE_UA,
            "Accept":            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding":   "gzip, deflate, br",
            "Accept-Language":   "en-GB,en-US;q=0.9,en;q=0.8",
            "sec-ch-ua-platform": '"Android"',
            "sec-ch-ua":         '"Android WebView";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile":  "?1",
            "sec-fetch-site":    "none",
            "sec-fetch-mode":    "navigate",
            "sec-fetch-dest":    "document",
            "Cache-Control":     "no-cache",
            "Pragma":            "no-cache",
        }
        resp = session.get(self.BASE_URL, headers=headers, timeout=30)
        html = resp.text

        cookies = {c.name: c.value for c in session.cookies}
        visitor_id = cookies.get("pplx.visitor-id", str(uuid.uuid4()))
        session_id = cookies.get("pplx.session-id", str(uuid.uuid4()))

        m = re.search(r'"version":"([\d.]+)"', html)
        version = m.group(1) if m else "2.18"

        m = re.search(r'csrf-token["\']?\s*[:=]\s*["\']([^"\']+)', html)
        csrf_token = m.group(1) if m else f"{uuid.uuid4().hex}%7C{uuid.uuid4().hex}"

        m = re.search(r'"apiUrl":"([^"]+)"', html)
        api_url = m.group(1) if m else self.SSE_URL

        return {
            "session":    session,
            "cookies":    cookies,
            "visitor_id": visitor_id,
            "session_id": session_id,
            "version":    version,
            "csrf_token": csrf_token,
            "api_url":    api_url,
            "ts":         int(time.time()),
        }

    # ── SSE response parser ────────────────────────────────────────────────────
    @staticmethod
    def _parse(raw: str) -> str:
        answer = ""
        for line in raw.strip().splitlines():
            if not line.startswith("data: "):
                continue
            chunk = line[6:].strip()
            if not chunk or chunk == "{}":
                continue
            try:
                data = json.loads(chunk)

                # schematized path
                if data.get("step_type") == "FINAL" and "text" in data:
                    try:
                        steps = json.loads(data["text"])
                        if isinstance(steps, list):
                            for step in steps:
                                if step.get("step_type") == "FINAL":
                                    ans_str = step.get("content", {}).get("answer", "")
                                    if ans_str:
                                        ans_data = json.loads(ans_str)
                                        txt = ans_data.get("answer", "")
                                        if txt:
                                            return txt.strip()
                    except Exception:
                        pass

                # blocks path
                for block in data.get("blocks", []):
                    if block.get("intended_usage") in ("ask_text_0_markdown", "ask_text"):
                        txt = block.get("markdown_block", {}).get("answer", "")
                        if txt and not answer:
                            answer = txt

            except Exception:
                continue

        return answer.strip()

    # ── public ask ────────────────────────────────────────────────────────────
    def ask(self, full_prompt: str) -> str:
        with self._lock:
            sc = self._scrape_session()
            session    = sc["session"]
            visitor_id = sc["visitor_id"]
            session_id = sc["session_id"]
            version    = sc["version"]
            csrf_token = sc["csrf_token"]
            api_url    = sc["api_url"]
            ts         = sc["ts"]
            base_ck    = sc["cookies"]

            frontend_uuid    = str(uuid.uuid4())
            backend_uuid     = str(uuid.uuid4())
            read_write_token = str(uuid.uuid4())
            request_id       = str(uuid.uuid4())

            payload = {
                "params": {
                    "last_backend_uuid":   backend_uuid,
                    "read_write_token":    read_write_token,
                    "attachments":         [],
                    "language":            "en-US",
                    "timezone":            "Asia/Dhaka",
                    "search_focus":        "internet",
                    "sources":             ["web"],
                    "frontend_uuid":       frontend_uuid,
                    "mode":                "concise",
                    "model_preference":    "turbo",
                    "is_related_query":    False,
                    "is_sponsored":        False,
                    "prompt_source":       "user",
                    "query_source":        "followup",
                    "is_incognito":        False,
                    "time_from_first_type": 1485.7,
                    "local_search_enabled": False,
                    "use_schematized_api": True,
                    "send_back_text_in_streaming_api": False,
                    "supported_block_use_cases": [
                        "answer_modes", "media_items", "knowledge_cards",
                        "inline_entity_cards", "place_widgets", "finance_widgets",
                        "prediction_market_widgets", "sports_widgets",
                        "flight_status_widgets", "news_widgets", "shopping_widgets",
                        "jobs_widgets", "search_result_widgets", "inline_images",
                        "inline_assets", "placeholder_cards", "diff_blocks",
                        "inline_knowledge_cards", "entity_group_v2",
                        "refinement_filters", "canvas_mode", "maps_preview",
                        "answer_tabs", "price_comparison_widgets",
                        "preserve_latex", "in_context_suggestions",
                    ],
                    "client_coordinates":             None,
                    "mentions":                       [],
                    "skip_search_enabled":            True,
                    "is_nav_suggestions_disabled":    False,
                    "followup_source":                "link",
                    "source":                         "mweb",
                    "always_search_override":         False,
                    "override_no_search":             False,
                    "should_ask_for_mcp_tool_confirmation": True,
                    "supported_features":             ["browser_agent_permission_banner_v1.1"],
                    "version":                        version,
                },
                "query_str": full_prompt,
            }

            extra_cookies = {
                "pplx.visitor-id":            visitor_id,
                "pplx.session-id":            session_id,
                "next-auth.csrf-token":       csrf_token,
                "next-auth.callback-url":     (
                    "https%3A%2F%2Fwww.perplexity.ai%2Fapi%2Fauth%2Fsignin-callback"
                    "%3Fredirect%3Dhttps%253A%252F%252Fwww.perplexity.ai"
                ),
                "pplx.mweb-splash-page-dismissed": "true",
                "pplx.la-status":             "allowed",
                "__ps_r":                     "_",
                "__ps_sr":                    "_",
                "__ps_fva":                   str(ts * 1000),
                "_fbp":                       f"fb.1.{ts}.{uuid.uuid4().hex}",
                "pplx.metadata": json.dumps({
                    "qc": 2, "qcu": 0, "qcm": 0, "qcc": 0,
                    "qcco": 0, "qccol": 0, "qcdr": 0, "qcs": 0, "qcd": 0,
                    "hli": False, "hcga": False, "hcds": False,
                    "hso": False, "hfo": False, "hsco": False,
                    "hfco": False, "hsma": False, "hdc": False,
                    "fqa": ts * 1000, "lqa": ts * 1000,
                }),
            }

            all_cookies = {**base_ck, **extra_cookies}

            headers = {
                "User-Agent":      self._MOBILE_UA,
                "Accept":          "text/event-stream",
                "Accept-Encoding": "gzip, deflate, br",
                "Content-Type":    "application/json",
                "x-request-id":    request_id,
                "sec-ch-ua-platform": '"Android"',
                "sec-ch-ua":       '"Android WebView";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "x-perplexity-request-reason": "perplexity-query-state-provider",
                "origin":          "https://www.perplexity.ai",
                "x-requested-with": "mark.via.gp",
                "sec-fetch-site":  "same-origin",
                "sec-fetch-mode":  "cors",
                "sec-fetch-dest":  "empty",
                "referer":         "https://www.perplexity.ai/search/new",
                "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
                "Cache-Control":   "no-cache",
                "Pragma":          "no-cache",
            }
            if csrf_token:
                headers["x-csrf-token"] = csrf_token

            time.sleep(0.5)
            resp = session.post(
                api_url, json=payload, headers=headers,
                cookies=all_cookies, timeout=120,
            )

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Perplexity HTTP {resp.status_code}: {resp.text[:300]}"
                )

            answer = self._parse(resp.text)
            if not answer:
                raise RuntimeError("Perplexity returned empty answer")
            return answer


# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL INSTANCES
# ══════════════════════════════════════════════════════════════════════════════

perplexity = PerplexityClient()
ptb_app: Application = None


# ══════════════════════════════════════════════════════════════════════════════
#  PROMPT BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_prompt(chat_id: int, user_name: str, user_msg: str) -> str:
    history = conversation_history[chat_id]
    recent  = history[-8:] if len(history) > 8 else history
    lines   = [SYSTEM_PROMPT, ""]
    for turn in recent:
        label = "User" if turn["role"] == "user" else "Nikita"
        lines.append(f"{label}: {turn['content']}")
    lines.append(f"User ({user_name}): {user_msg}")
    lines.append("Nikita:")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  HELPER: strip mention
# ══════════════════════════════════════════════════════════════════════════════

def strip_mention(text: str, bot_username: str) -> str:
    cleaned = re.sub(
        rf"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE
    ).strip()
    return cleaned or text.strip()


# ══════════════════════════════════════════════════════════════════════════════
#  HELPER: owner check
# ══════════════════════════════════════════════════════════════════════════════

def is_owner(update: Update) -> bool:
    if not OWNER_ID:
        return False
    user = update.effective_user
    return user is not None and user.id == OWNER_ID


# ══════════════════════════════════════════════════════════════════════════════
#  CORE REPLY
# ══════════════════════════════════════════════════════════════════════════════

async def send_nikita_reply(
    chat_id: int,
    reply_to_message_id: int,
    user_name: str,
    clean_text: str,
    bot: Bot,
):
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    sent = await bot.send_message(
        chat_id=chat_id,
        text="🌸",
        reply_to_message_id=reply_to_message_id,
    )
    try:
        prompt        = build_prompt(chat_id, user_name, clean_text)
        full_response = await asyncio.to_thread(lambda: perplexity.ask(prompt))

        if not full_response:
            full_response = "কী জানি কী হলো 😅 একটু পরে আবার বলো"
        if len(full_response) > 4000:
            full_response = full_response[:4000] + "…"

        await sent.edit_text(full_response)

        conversation_history[chat_id].append({"role": "user",      "content": clean_text})
        conversation_history[chat_id].append({"role": "assistant", "content": full_response})
        if len(conversation_history[chat_id]) > 20:
            conversation_history[chat_id] = conversation_history[chat_id][-20:]

    except Exception as e:
        logger.error(f"Reply error for chat {chat_id}: {e}")
        try:
            await sent.edit_text("একটু সমস্যা হয়েছে 😔 একটু পরে আবার চেষ্টা করো!")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  RAW UPDATE LOGGER
# ══════════════════════════════════════════════════════════════════════════════

async def raw_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        if msg:
            logger.info(
                f"[RAW] id={update.update_id} "
                f"type={msg.chat.type if msg.chat else 'N/A'} "
                f"from={msg.from_user} "
                f"text={repr(msg.text or '')} "
            )
        else:
            logger.info(f"[RAW] id={update.update_id} keys={list(update.to_dict().keys())}")
    except Exception as e:
        logger.warning(f"raw_update_logger error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  USER / GROUP TRACKING  (stats এর জন্য)
# ══════════════════════════════════════════════════════════════════════════════

def track_chat(update: Update):
    msg = update.effective_message
    if not msg or not msg.chat:
        return
    if msg.chat.type == "private":
        if msg.from_user:
            active_users.add(msg.from_user.id)
    else:
        active_groups.add(msg.chat.id)


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_chat(update)
    name = update.effective_user.first_name if update.effective_user else "তুমি"
    await update.effective_message.reply_text(
        f"আরে {name}! কী খবর? আমি Nikita 😏\n\n"
        "রিপ্লাই করো বা @mention করো — আমি এখানেই আছি ✨"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_chat(update)
    await update.effective_message.reply_text(
        "গ্রুপে @mention করো বা রিপ্লাই করো আমার মেসেজে\n"
        "Private এ সরাসরি মেসেজ করো\n\n"
        "/clear — নতুন করে শুরু করতে চাইলে"
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_chat(update)
    chat_id = update.effective_chat.id
    conversation_history[chat_id].clear()
    await update.effective_message.reply_text("ঠিক আছে, ভুলে গেলাম সব 🌱 নতুন করে বলো")


# ══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS — OWNER ONLY
# ══════════════════════════════════════════════════════════════════════════════

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: /logs — RAM, disk, uptime সহ বটের সিস্টেম অবস্থা"""
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।")
        return

    proc  = psutil.Process(os.getpid())
    ram_mb = proc.memory_info().rss / 1024 / 1024
    ram_percent = psutil.virtual_memory().percent

    disk  = psutil.disk_usage("/")
    disk_used_gb  = disk.used  / 1024**3
    disk_total_gb = disk.total / 1024**3

    uptime_sec = (datetime.now(timezone.utc) - BOT_START_TIME).total_seconds()
    h = int(uptime_sec // 3600)
    m = int((uptime_sec % 3600) // 60)
    s = int(uptime_sec % 60)

    cpu = psutil.cpu_percent(interval=1)

    text = (
        "📊 বটের লগ রিপোর্ট\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕐 আপটাইম: {h}h {m}m {s}s\n"
        f"🧠 RAM (বট): {ram_mb:.1f} MB\n"
        f"💾 RAM (সিস্টেম): {ram_percent}% ব্যবহৃত\n"
        f"💿 Disk: {disk_used_gb:.2f} GB / {disk_total_gb:.2f} GB\n"
        f"⚡ CPU: {cpu}%\n"
        f"💬 Active chats: {len(conversation_history)}\n"
        f"👥 Total users (session): {len(active_users)}\n"
        f"🏘 Total groups (session): {len(active_groups)}\n"
    )
    await update.effective_message.reply_text(text)


async def state_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: /state — কতজন user, কতটি group, ইনবক্স ইত্যাদি"""
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।")
        return

    total_chats = len(conversation_history)
    group_count = len(active_groups)
    user_count  = len(active_users)

    # conversation_history তে কোনগুলো private (positive ID = user, negative = group)
    private_active = sum(1 for cid in conversation_history if cid > 0)
    group_active   = sum(1 for cid in conversation_history if cid < 0)

    text = (
        "📡 বটের স্টেট\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 ইনবক্স (active): {private_active}\n"
        f"🏘 গ্রুপ (active chat): {group_active}\n"
        f"📊 মোট active chat: {total_chats}\n"
        f"🆕 এই session এ users: {user_count}\n"
        f"🆕 এই session এ groups: {group_count}\n"
    )
    await update.effective_message.reply_text(text)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: /broadcast — সব user ও group এ মেসেজ/মিডিয়া পাঠাও।
    ব্যবহার:
      - সরাসরি: /broadcast <text>
      - রিপ্লাই করে: forwarded/media মেসেজে রিপ্লাই দিয়ে /broadcast
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।")
        return

    msg    = update.effective_message
    bot    = context.bot
    all_chats = list(conversation_history.keys())

    if not all_chats:
        await msg.reply_text("এখনো কোনো chat নেই।")
        return

    replied = msg.reply_to_message
    success = 0
    failed  = 0

    status_msg = await msg.reply_text(
        f"📢 Broadcasting শুরু হচ্ছে... ({len(all_chats)} chat)"
    )

    for chat_id in all_chats:
        try:
            if replied:
                # ── replied মেসেজ থেকে broadcast ──────────────────────────
                if replied.photo:
                    photo  = replied.photo[-1].file_id
                    cap    = replied.caption or ""
                    await bot.send_photo(chat_id=chat_id, photo=photo, caption=cap)

                elif replied.video:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=replied.video.file_id,
                        caption=replied.caption or "",
                    )

                elif replied.audio:
                    await bot.send_audio(
                        chat_id=chat_id,
                        audio=replied.audio.file_id,
                        caption=replied.caption or "",
                    )

                elif replied.voice:
                    await bot.send_voice(
                        chat_id=chat_id,
                        voice=replied.voice.file_id,
                        caption=replied.caption or "",
                    )

                elif replied.sticker:
                    await bot.send_sticker(
                        chat_id=chat_id,
                        sticker=replied.sticker.file_id,
                    )

                elif replied.document:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=replied.document.file_id,
                        caption=replied.caption or "",
                    )

                elif replied.animation:
                    await bot.send_animation(
                        chat_id=chat_id,
                        animation=replied.animation.file_id,
                        caption=replied.caption or "",
                    )

                else:
                    # plain text reply
                    text_to_send = replied.text or replied.caption or ""
                    if text_to_send:
                        await bot.send_message(chat_id=chat_id, text=text_to_send)

            else:
                # ── /broadcast <text> দিয়ে সরাসরি ────────────────────────
                broadcast_text = " ".join(context.args) if context.args else ""
                if not broadcast_text:
                    await status_msg.edit_text(
                        "❌ কিছু লিখো বা কোনো মেসেজে রিপ্লাই করে /broadcast দাও।"
                    )
                    return
                await bot.send_message(chat_id=chat_id, text=broadcast_text)

            success += 1

        except TelegramError as e:
            logger.warning(f"Broadcast failed for {chat_id}: {e}")
            failed += 1
        except Exception as e:
            logger.error(f"Broadcast unexpected error for {chat_id}: {e}")
            failed += 1

        await asyncio.sleep(0.05)   # flood control

    await status_msg.edit_text(
        f"✅ Broadcast সম্পন্ন!\n"
        f"পাঠানো হয়েছে: {success}\n"
        f"ব্যর্থ: {failed}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  UNIVERSAL MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════════════════

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.chat:
        return

    track_chat(update)

    bot_username = (context.bot.username or BOT_USERNAME).lower()
    text         = msg.text or msg.caption or ""
    chat_type    = msg.chat.type
    chat_id      = msg.chat.id

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

    # Group / Supergroup
    should_reply = False
    trigger      = ""

    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == "mention":
            mention_text = text[ent.offset: ent.offset + ent.length]
            if mention_text.lstrip("@").lower() == bot_username:
                should_reply = True
                trigger = "entity_mention"
                break

    if not should_reply and f"@{bot_username}" in text.lower():
        should_reply = True
        trigger = "text_mention"

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


# ══════════════════════════════════════════════════════════════════════════════
#  FLASK APP  (health + webhook)
# ══════════════════════════════════════════════════════════════════════════════

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def home():
    uptime_sec = (datetime.now(timezone.utc) - BOT_START_TIME).total_seconds()
    h = int(uptime_sec // 3600)
    m = int((uptime_sec % 3600) // 60)
    s = int(uptime_sec % 60)

    proc   = psutil.Process(os.getpid())
    ram_mb = proc.memory_info().rss / 1024 / 1024

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Nikita Bot — Status</title>
  <style>
    body {{
      margin: 0; padding: 0;
      background: #0f0f1a;
      color: #e8e8f0;
      font-family: 'Segoe UI', sans-serif;
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh;
    }}
    .card {{
      background: #1a1a2e;
      border: 1px solid #e040fb44;
      border-radius: 16px;
      padding: 40px 50px;
      max-width: 480px;
      width: 90%;
      text-align: center;
      box-shadow: 0 0 40px #e040fb22;
    }}
    h1 {{ font-size: 2rem; margin-bottom: 4px; color: #e040fb; }}
    .subtitle {{ color: #aaa; font-size: 0.9rem; margin-bottom: 28px; }}
    .badge {{
      display: inline-block;
      background: #00c85322;
      color: #00e676;
      border: 1px solid #00c853;
      border-radius: 20px;
      padding: 4px 16px;
      font-size: 0.85rem;
      margin-bottom: 28px;
    }}
    .stats {{
      display: grid; grid-template-columns: 1fr 1fr;
      gap: 14px; text-align: left;
    }}
    .stat {{
      background: #0f0f1a;
      border: 1px solid #333;
      border-radius: 10px;
      padding: 12px 16px;
    }}
    .stat .label {{ font-size: 0.75rem; color: #888; }}
    .stat .value {{ font-size: 1.1rem; font-weight: 600; margin-top: 2px; }}
    .footer {{ margin-top: 28px; font-size: 0.75rem; color: #555; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>𓆩♡𓆪 Nikita</h1>
    <p class="subtitle">Telegram AI Bot</p>
    <div class="badge">✅ Online</div>
    <div class="stats">
      <div class="stat">
        <div class="label">Uptime</div>
        <div class="value">{h}h {m}m {s}s</div>
      </div>
      <div class="stat">
        <div class="label">RAM Usage</div>
        <div class="value">{ram_mb:.1f} MB</div>
      </div>
      <div class="stat">
        <div class="label">Active Chats</div>
        <div class="value">{len(conversation_history)}</div>
      </div>
      <div class="stat">
        <div class="label">AI Engine</div>
        <div class="value">Perplexity</div>
      </div>
    </div>
    <p class="footer">Powered by Perplexity AI &bull; Hosted on Render</p>
  </div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@flask_app.route("/health", methods=["GET"])
def health_api():
    uptime = str(datetime.now(timezone.utc) - BOT_START_TIME).split(".")[0]
    proc   = psutil.Process(os.getpid())
    return jsonify({
        "status":        "healthy",
        "bot_name":      "𓆩♡𓆪 Nikita",
        "uptime":        uptime,
        "active_chats":  len(conversation_history),
        "active_users":  len(active_users),
        "active_groups": len(active_groups),
        "ram_mb":        round(proc.memory_info().rss / 1024 / 1024, 2),
        "ai_engine":     "Perplexity",
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    })


@flask_app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if not request.is_json:
        abort(400)

    update_data = request.get_json(force=True)
    logger.info(f"[WEBHOOK] keys: {list(update_data.keys())}")

    if ptb_app is None:
        logger.error("PTB app not initialized yet!")
        return "OK", 200

    try:
        upd = Update.de_json(update_data, ptb_app.bot)
        asyncio.run_coroutine_threadsafe(
            ptb_app.update_queue.put(upd),
            ptb_app.update_queue._loop,
        )
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")

    return "OK", 200


# ══════════════════════════════════════════════════════════════════════════════
#  WEBHOOK SETUP
# ══════════════════════════════════════════════════════════════════════════════

async def setup_webhook(app: Application):
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL not set!")
        return
    endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    await app.bot.set_webhook(
        url=endpoint,
        allowed_updates=list(Update.ALL_TYPES),
        drop_pending_updates=True,
    )
    info = await app.bot.get_webhook_info()
    logger.info(f"Webhook set: {info.url} | pending: {info.pending_update_count}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global ptb_app

    ptb_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Public commands
    ptb_app.add_handler(CommandHandler("start", start_cmd))
    ptb_app.add_handler(CommandHandler("help",  help_cmd))
    ptb_app.add_handler(CommandHandler("clear", clear_cmd))

    # Owner-only commands
    ptb_app.add_handler(CommandHandler("logs",      logs_cmd))
    ptb_app.add_handler(CommandHandler("state",     state_cmd))
    ptb_app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Raw logger (highest priority = lowest group number)
    ptb_app.add_handler(TypeHandler(Update, raw_update_logger), group=-2)

    # Message handler
    ptb_app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_all_messages,
        ),
        group=0,
    )

    ptb_app.post_init = setup_webhook

    logger.info("𓆩♡𓆪 Nikita bot starting (Perplexity AI, webhook mode)…")

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
