"""
𓆩♡𓆪 Nikita Bot
================
• PTB polling  → main thread (asyncio signal handler এখানেই কাজ করে)
• Flask health  → background daemon thread
• Perplexity AI → curl_cffi (Cloudflare bypass)
• Owner: /logs  /state  /broadcast
"""

import os, re, json, uuid, time, psutil, asyncio, logging, threading
from datetime import datetime, timezone
from collections import defaultdict

from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, TypeHandler,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from flask import Flask, jsonify

try:
    from curl_cffi import requests as cffi_requests
    USE_CFFI = True
except ImportError:
    import requests as cffi_requests
    USE_CFFI = False

import requests as std_requests

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NikitaBot")
logger.info(f"curl_cffi={USE_CFFI}")

# ── config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "UrNikitaBot")
OWNER_ID     = int(os.environ.get("OWNER_ID", "0"))
PORT         = int(os.environ.get("PORT", 8080))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set!")

# ── state ─────────────────────────────────────────────────────────────────────
conversation_history: dict[int, list[dict]] = defaultdict(list)
BOT_START_TIME = datetime.now(timezone.utc)
active_users:  set[int] = set()
active_groups: set[int] = set()

# ── system prompt ─────────────────────────────────────────────────────────────
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
# PERPLEXITY CLIENT
# ══════════════════════════════════════════════════════════════════════════════
class PerplexityClient:
    SSE_URL  = "https://www.perplexity.ai/rest/sse/perplexity_ask"
    BASE_URL = "https://www.perplexity.ai"
    _UA = (
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    )

    def __init__(self):
        self._lock = threading.Lock()

    def _scrape(self):
        hdrs = {
            "User-Agent":      self._UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Cache-Control":   "no-cache",
        }
        if USE_CFFI:
            sess = cffi_requests.Session(impersonate="chrome124")
            resp = sess.get(self.BASE_URL, headers=hdrs, timeout=30)
        else:
            sess = std_requests.Session()
            resp = sess.get(self.BASE_URL, headers=hdrs, timeout=30)

        html    = resp.text
        # Safe cookie parsing — handle both cookie objects and plain dicts/strings
        try:
            cookies = {}
            for c in resp.cookies:
                try:
                    # requests.cookies.RequestsCookieJar yields Morsel or cookie objects
                    name  = c.name  if hasattr(c, "name")  else str(c)
                    value = c.value if hasattr(c, "value") else ""
                    cookies[name] = value
                except Exception:
                    pass
            # Also merge from dict-style access as fallback
            try:
                cookies.update(dict(resp.cookies))
            except Exception:
                pass
        except Exception:
            cookies = {}

        visitor_id = cookies.get("pplx.visitor-id") or str(uuid.uuid4())
        session_id = cookies.get("pplx.session-id") or str(uuid.uuid4())

        m       = re.search(r'"version"\s*:\s*"([\d.]+)"', html)
        version = m.group(1) if m else "2.18"

        m    = re.search(r'csrf-token["\']?\s*[:=]\s*["\']([^"\']{10,})', html)
        csrf = m.group(1) if m else f"{uuid.uuid4().hex}%7C{uuid.uuid4().hex}"

        m       = re.search(r'"apiUrl"\s*:\s*"([^"]+)"', html)
        api_url = m.group(1) if m else self.SSE_URL

        return dict(
            session=sess, cookies=cookies,
            visitor_id=visitor_id, session_id=session_id,
            version=version, csrf=csrf, api_url=api_url,
            ts=int(time.time()),
        )

    @staticmethod
    def _parse(raw: str) -> str:
        answer = ""
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            chunk = line[6:].strip()
            if not chunk or chunk == "{}":
                continue
            try:
                d = json.loads(chunk)

                # Pattern 1: step_type=FINAL with nested text
                if d.get("step_type") == "FINAL" and "text" in d:
                    try:
                        steps = json.loads(d["text"])
                        if isinstance(steps, list):
                            for step in steps:
                                if step.get("step_type") == "FINAL":
                                    a = step.get("content", {}).get("answer", "")
                                    if a:
                                        ans = json.loads(a).get("answer", "")
                                        if ans:
                                            return ans.strip()
                    except Exception:
                        pass

                # Pattern 2: direct answer field
                if d.get("step_type") == "FINAL" and d.get("answer"):
                    return str(d["answer"]).strip()

                # Pattern 3: blocks (markdown)
                for blk in d.get("blocks", []):
                    if blk.get("intended_usage") in ("ask_text_0_markdown", "ask_text"):
                        t = blk.get("markdown_block", {}).get("answer", "")
                        if t and not answer:
                            answer = t

                # Pattern 4: direct text field as last resort
                if not answer and d.get("text") and isinstance(d["text"], str):
                    txt = d["text"].strip()
                    # Only use if it looks like actual answer text (not JSON)
                    if txt and not txt.startswith("[") and not txt.startswith("{") and len(txt) > 10:
                        answer = txt

            except Exception:
                continue
        return answer.strip()

    def ask(self, prompt: str) -> str:
        last_err = None
        for attempt in range(3):  # 3 বার retry
            try:
                return self._ask_once(prompt)
            except Exception as e:
                last_err = e
                logger.warning(f"Perplexity attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s backoff
        raise last_err

    def _ask_once(self, prompt: str) -> str:
        with self._lock:
            sc  = self._scrape()
            ts  = sc["ts"]
            fid = str(uuid.uuid4())
            bid = str(uuid.uuid4())
            rwt = str(uuid.uuid4())
            rid = str(uuid.uuid4())

            payload = {
                "params": {
                    "last_backend_uuid": bid,
                    "read_write_token":  rwt,
                    "attachments": [], "language": "en-US",
                    "timezone": "Asia/Dhaka", "search_focus": "internet",
                    "sources": ["web"], "frontend_uuid": fid,
                    "mode": "concise", "model_preference": "turbo",
                    "is_related_query": False, "is_sponsored": False,
                    "prompt_source": "user", "query_source": "followup",
                    "is_incognito": False, "time_from_first_type": 1200.0,
                    "local_search_enabled": False,
                    "use_schematized_api": True,
                    "send_back_text_in_streaming_api": False,
                    "supported_block_use_cases": [
                        "answer_modes","media_items","knowledge_cards",
                        "inline_entity_cards","news_widgets",
                        "search_result_widgets","inline_images",
                        "placeholder_cards","diff_blocks",
                        "inline_knowledge_cards","refinement_filters",
                        "canvas_mode","answer_tabs","preserve_latex",
                        "in_context_suggestions",
                    ],
                    "client_coordinates": None, "mentions": [],
                    "skip_search_enabled": True,
                    "is_nav_suggestions_disabled": False,
                    "followup_source": "link", "source": "mweb",
                    "always_search_override": False,
                    "override_no_search": False,
                    "should_ask_for_mcp_tool_confirmation": True,
                    "supported_features": ["browser_agent_permission_banner_v1.1"],
                    "version": sc["version"],
                },
                "query_str": prompt,
            }

            ck = {
                **sc["cookies"],
                "pplx.visitor-id": sc["visitor_id"],
                "pplx.session-id": sc["session_id"],
                "next-auth.csrf-token": sc["csrf"],
                "pplx.mweb-splash-page-dismissed": "true",
                "pplx.la-status": "allowed",
                "__ps_r": "_", "__ps_sr": "_",
                "__ps_fva": str(ts * 1000),
                "_fbp": f"fb.1.{ts}.{uuid.uuid4().hex}",
                "pplx.metadata": json.dumps({
                    "qc":2,"qcu":0,"qcm":0,"qcc":0,"qcco":0,"qccol":0,
                    "qcdr":0,"hli":False,"hcga":False,"hcds":False,
                    "hso":False,"hfo":False,"fqa":ts*1000,"lqa":ts*1000,
                }),
            }

            hdrs = {
                "User-Agent":      self._UA,
                "Accept":          "text/event-stream",
                "Content-Type":    "application/json",
                "x-request-id":    rid,
                "x-csrf-token":    sc["csrf"],
                "x-perplexity-request-reason": "perplexity-query-state-provider",
                "origin":          "https://www.perplexity.ai",
                "referer":         "https://www.perplexity.ai/",
                "x-requested-with":"mark.via.gp",
                "sec-fetch-site":  "same-origin",
                "sec-fetch-mode":  "cors",
                "sec-fetch-dest":  "empty",
                "accept-language": "en-US,en;q=0.9",
                "Cache-Control":   "no-cache",
            }

            time.sleep(0.8)
            resp = sc["session"].post(
                sc["api_url"], json=payload,
                headers=hdrs, cookies=ck, timeout=120,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Perplexity {resp.status_code}: {resp.text[:200]}")
            answer = self._parse(resp.text)
            if not answer:
                raise RuntimeError("Empty answer from Perplexity")
            return answer


# ══════════════════════════════════════════════════════════════════════════════
# GLOBALS
# ══════════════════════════════════════════════════════════════════════════════
perplexity = PerplexityClient()


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def build_prompt(chat_id: int, user_name: str, user_msg: str) -> str:
    history = conversation_history[chat_id]
    recent  = history[-8:] if len(history) > 8 else history
    lines   = [SYSTEM_PROMPT, ""]
    for t in recent:
        lines.append(f"{'User' if t['role']=='user' else 'Nikita'}: {t['content']}")
    lines += [f"User ({user_name}): {user_msg}", "Nikita:"]
    return "\n".join(lines)


def strip_mention(text: str, bot_username: str) -> str:
    return re.sub(rf"@{re.escape(bot_username)}", "", text, flags=re.IGNORECASE).strip() or text.strip()


def is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(OWNER_ID) and u is not None and u.id == OWNER_ID


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
# CORE REPLY
# ══════════════════════════════════════════════════════════════════════════════
async def send_nikita_reply(chat_id, reply_to_message_id, user_name, clean_text, bot: Bot):
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    sent = await bot.send_message(chat_id=chat_id, text="🌸",
                                   reply_to_message_id=reply_to_message_id)
    try:
        prompt = build_prompt(chat_id, user_name, clean_text)
        resp   = await asyncio.to_thread(lambda: perplexity.ask(prompt))
        if not resp:
            resp = "কী জানি কী হলো 😅 একটু পরে আবার বলো"
        if len(resp) > 4000:
            resp = resp[:4000] + "…"
        await sent.edit_text(resp)
        conversation_history[chat_id].append({"role": "user",      "content": clean_text})
        conversation_history[chat_id].append({"role": "assistant", "content": resp})
        if len(conversation_history[chat_id]) > 20:
            conversation_history[chat_id] = conversation_history[chat_id][-20:]
    except Exception as e:
        import traceback
        logger.error(f"Reply error chat={chat_id}: {e}\n{traceback.format_exc()}")
        try:
            await sent.edit_text("একটু সমস্যা হয়েছে 😔 একটু পরে আবার চেষ্টা করো!")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_chat(update)
    name = (update.effective_user.first_name or "তুমি") if update.effective_user else "তুমি"
    await update.effective_message.reply_text(
        f"আরে {name}! কী খবর? আমি Nikita 😏\n\nরিপ্লাই করো বা @mention করো — আমি এখানেই আছি ✨"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_chat(update)
    await update.effective_message.reply_text(
        "গ্রুপে @mention করো বা রিপ্লাই করো আমার মেসেজে\nPrivate এ সরাসরি মেসেজ করো\n\n/clear — নতুন করে শুরু করতে চাইলে"
    )

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_chat(update)
    conversation_history[update.effective_chat.id].clear()
    await update.effective_message.reply_text("ঠিক আছে, ভুলে গেলাম সব 🌱 নতুন করে বলো")


# ══════════════════════════════════════════════════════════════════════════════
# OWNER COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return
    proc = psutil.Process(os.getpid())
    ram  = proc.memory_info().rss / 1024 / 1024
    disk = psutil.disk_usage("/")
    cpu  = psutil.cpu_percent(interval=1)
    sec  = (datetime.now(timezone.utc) - BOT_START_TIME).total_seconds()
    h, r = divmod(int(sec), 3600); m, s = divmod(r, 60)
    await update.effective_message.reply_text(
        "📊 বটের লগ রিপোর্ট\n━━━━━━━━━━━━━━━━━━\n"
        f"🕐 আপটাইম  : {h}h {m}m {s}s\n"
        f"🧠 RAM (বট): {ram:.1f} MB\n"
        f"💾 RAM (sys): {psutil.virtual_memory().percent}% ব্যবহৃত\n"
        f"💿 Disk     : {disk.used/1024**3:.2f} / {disk.total/1024**3:.2f} GB\n"
        f"⚡ CPU      : {cpu}%\n"
        f"💬 Active chats : {len(conversation_history)}\n"
        f"👥 Session users : {len(active_users)}\n"
        f"🏘 Session groups: {len(active_groups)}\n"
    )

async def state_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return
    priv = sum(1 for c in conversation_history if c > 0)
    grp  = sum(1 for c in conversation_history if c < 0)
    await update.effective_message.reply_text(
        "📡 বটের স্টেট\n━━━━━━━━━━━━━━━━━━\n"
        f"👤 ইনবক্স (active) : {priv}\n"
        f"🏘 গ্রুপ  (active) : {grp}\n"
        f"📊 মোট active chat : {len(conversation_history)}\n"
        f"🆕 Session users   : {len(active_users)}\n"
        f"🆕 Session groups  : {len(active_groups)}\n"
    )

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return
    msg       = update.effective_message
    bot       = context.bot
    all_chats = list(conversation_history.keys())
    if not all_chats:
        await msg.reply_text("এখনো কোনো chat নেই।"); return
    replied = msg.reply_to_message
    ok = fail = 0
    status = await msg.reply_text(f"📢 Broadcasting... ({len(all_chats)} chat)")
    for cid in all_chats:
        try:
            if replied:
                if replied.photo:
                    await bot.send_photo(cid, replied.photo[-1].file_id, caption=replied.caption or "")
                elif replied.video:
                    await bot.send_video(cid, replied.video.file_id, caption=replied.caption or "")
                elif replied.audio:
                    await bot.send_audio(cid, replied.audio.file_id, caption=replied.caption or "")
                elif replied.voice:
                    await bot.send_voice(cid, replied.voice.file_id, caption=replied.caption or "")
                elif replied.sticker:
                    await bot.send_sticker(cid, replied.sticker.file_id)
                elif replied.document:
                    await bot.send_document(cid, replied.document.file_id, caption=replied.caption or "")
                elif replied.animation:
                    await bot.send_animation(cid, replied.animation.file_id, caption=replied.caption or "")
                else:
                    t = replied.text or replied.caption or ""
                    if t: await bot.send_message(cid, t)
            else:
                t = " ".join(context.args) if context.args else ""
                if not t:
                    await status.edit_text("❌ কিছু লিখো বা কোনো মেসেজে রিপ্লাই করে /broadcast দাও।"); return
                await bot.send_message(cid, t)
            ok += 1
        except TelegramError as e:
            logger.warning(f"Broadcast fail {cid}: {e}"); fail += 1
        except Exception as e:
            logger.error(f"Broadcast err {cid}: {e}"); fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Broadcast সম্পন্ন!\nপাঠানো: {ok}  ব্যর্থ: {fail}")


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════════════════════
async def raw_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        if msg:
            logger.info(f"[RAW] id={update.update_id} type={msg.chat.type if msg.chat else '?'} text={repr((msg.text or '')[:50])}")
    except Exception:
        pass

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.chat:
        return
    track_chat(update)
    bot_un    = (context.bot.username or BOT_USERNAME).lower()
    text      = msg.text or msg.caption or ""
    chat_type = msg.chat.type
    chat_id   = msg.chat.id
    user      = update.effective_user
    uname     = (user.first_name or user.username or "বন্ধু") if user else "বন্ধু"

    if chat_type == "private":
        clean = strip_mention(text, bot_un) or "হ্যালো"
        logger.info(f"[PRIVATE] chat={chat_id} user={uname}")
        await send_nikita_reply(chat_id, msg.message_id, uname, clean, context.bot)
        return

    should = False; trigger = ""
    for ent in (msg.entities or msg.caption_entities or []):
        if ent.type == "mention" and text[ent.offset:ent.offset+ent.length].lstrip("@").lower() == bot_un:
            should = True; trigger = "mention"; break
    if not should and f"@{bot_un}" in text.lower():
        should = True; trigger = "text_mention"
    if not should and msg.reply_to_message:
        rt = msg.reply_to_message
        if rt.from_user and ((rt.from_user.username or "").lower() == bot_un or rt.from_user.id == context.bot.id):
            should = True; trigger = "reply"
        elif rt.sender_chat and (rt.sender_chat.username or "").lower() == bot_un:
            should = True; trigger = "reply_channel"
    if not should and user and user.is_bot and user.id != context.bot.id:
        should = True; trigger = "bot2bot"
    if not should:
        return
    clean = strip_mention(text, bot_un) or "হ্যালো"
    logger.info(f"[GROUP/{trigger}] chat={chat_id} user={uname}")
    await send_nikita_reply(chat_id, msg.message_id, uname, clean, context.bot)


# ══════════════════════════════════════════════════════════════════════════════
# FLASK  — background thread
# ══════════════════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def home():
    sec  = (datetime.now(timezone.utc) - BOT_START_TIME).total_seconds()
    h, r = divmod(int(sec), 3600); m, s = divmod(r, 60)
    ram  = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Nikita Bot</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f0f1a;color:#e8e8f0;font-family:'Segoe UI',sans-serif;
  min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}}
.card{{background:#1a1a2e;border:1px solid #e040fb55;border-radius:18px;
  padding:36px 44px;max-width:460px;width:100%;text-align:center;
  box-shadow:0 0 50px #e040fb1a}}
h1{{font-size:1.9rem;color:#e040fb;margin-bottom:6px}}
.sub{{color:#888;font-size:.85rem;margin-bottom:22px}}
.pill{{display:inline-block;background:#00c85318;color:#00e676;
  border:1px solid #00c853;border-radius:20px;padding:3px 14px;
  font-size:.8rem;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;text-align:left}}
.box{{background:#0d0d1a;border:1px solid #2a2a3a;border-radius:10px;padding:12px 14px}}
.lbl{{font-size:.7rem;color:#666;text-transform:uppercase;letter-spacing:.05em}}
.val{{font-size:1.05rem;font-weight:600;margin-top:3px}}
.foot{{margin-top:24px;font-size:.72rem;color:#444}}
</style></head>
<body><div class="card">
  <h1>𓆩♡𓆪 Nikita</h1>
  <p class="sub">Telegram AI Bot</p>
  <span class="pill">✅ Online</span>
  <div class="grid">
    <div class="box"><div class="lbl">Uptime</div><div class="val">{h}h {m}m {s}s</div></div>
    <div class="box"><div class="lbl">RAM</div><div class="val">{ram:.1f} MB</div></div>
    <div class="box"><div class="lbl">Active Chats</div><div class="val">{len(conversation_history)}</div></div>
    <div class="box"><div class="lbl">AI Engine</div><div class="val">Perplexity</div></div>
  </div>
  <p class="foot">Perplexity AI &bull; Render &bull; python-telegram-bot</p>
</div></body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}

@flask_app.route("/health", methods=["GET"])
def health_json():
    uptime = str(datetime.now(timezone.utc) - BOT_START_TIME).split(".")[0]
    ram    = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    return jsonify({"status":"ok","bot":"Nikita","uptime":uptime,
        "active_chats":len(conversation_history),"active_users":len(active_users),
        "active_groups":len(active_groups),"ram_mb":round(ram,1),
        "ai_engine":"Perplexity","curl_cffi":USE_CFFI})

def run_flask():
    logger.info(f"Flask starting on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN  — Flask in bg thread, PTB polling in MAIN thread (signal handler OK)
# ══════════════════════════════════════════════════════════════════════════════
def main():
    # 1. Delete any existing webhook first — so Telegram uses polling
    import requests as _r
    try:
        _r.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
        logger.info("Webhook deleted — polling mode active")
    except Exception as e:
        logger.warning(f"deleteWebhook failed: {e}")

    # 2. Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True, name="FlaskThread")
    flask_thread.start()

    # 3. PTB in MAIN thread
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start",     start_cmd))
    app.add_handler(CommandHandler("help",      help_cmd))
    app.add_handler(CommandHandler("clear",     clear_cmd))
    app.add_handler(CommandHandler("logs",      logs_cmd))
    app.add_handler(CommandHandler("state",     state_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(TypeHandler(Update, raw_logger), group=-2)
    app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                       handle_all_messages), group=0,
    )

    logger.info("𓆩♡𓆪 Nikita PTB polling started (main thread)")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
