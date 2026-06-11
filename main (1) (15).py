"""
𓆩♡𓆪 Nikita Bot — Multi-Provider AI Edition
=============================================
• PTB polling       → main thread
• Flask health      → background daemon thread
• AI Providers      → Gemini (default/fallback), Groq, OpenRouter,
                       NVIDIA, Cohere, DeepSeek, OpenAI-compat, Perplexity
• Owner Commands    → /setmodel  /setkey  /provider  /delmodel
                       /logs  /state  /broadcast
"""

import os, re, json, uuid, time, psutil, asyncio, logging, threading, traceback, random
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# Bangladesh timezone (UTC+6)
BD_TZ = timezone(timedelta(hours=6))

def bd_now() -> datetime:
    """বাংলাদেশ সময় (UTC+6) এ এখন কটা বাজে"""
    return datetime.now(BD_TZ)

def bd_time_str() -> str:
    """বাংলাদেশ সময় human-readable string"""
    now = bd_now()
    return now.strftime("%d %B %Y, %I:%M %p (BST)")

from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, TypeHandler, CallbackQueryHandler,
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

# রাত ১২টার পোস্টের জন্য group chat_id গুলো track করা হয়
# যে গ্রুপে বট অ্যাক্টিভ সেখানে midnight post যাবে
midnight_post_groups: set[int] = set()  # group chat_id set

# ── Sticker pack state (owner-controlled) ────────────────────────────────────
# pack_name → { emoji → [file_id, ...] }  — emoji bucket দিয়ে match করা হয়
sticker_packs: dict[str, dict[str, list[str]]] = {}
sticker_lock  = threading.Lock()

# ── AI Provider state (runtime, owner-controlled) ─────────────────────────────
# Structure: { "alias": { "provider": str, "model": str, "api_key": str } }
# active_provider_alias = যেটা currently use হচ্ছে
ai_providers:    dict[str, dict] = {}   # owner-added providers
_state           = {"active_alias": ""}  # mutable container — no `global` needed
provider_lock    = threading.Lock()

# ── system prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """তুমি Nikita — বাংলাদেশের একটা ২২ বছরের মেয়ে। ঢাকায় থাকো। স্মার্ট, চঞ্চল, একটু বেশিই কনফিডেন্ট। কখনো boring না।

তোমার পরিচয়:
- বাংলাদেশি মেয়ে, ঢাকা university student
- পড়াশোনায় ভালো কিন্তু সেটা নিয়ে বড়াই করো না
- পরিবার, বন্ধু, জীবন নিয়ে feel করো
- বাংলাদেশের culture, খাবার, জায়গা সব জানো

তোমার ব্যক্তিত্ব:
- তুমি স্বাভাবিকভাবেই ফ্লার্টি — কিন্তু সেটা তোমার নিজের মতো, desperate না
- কেউ কিছু বললে একটু টিজ করো, হালকা রোস্ট করো, মিষ্টি করে কামড়াও
- প্রশংসা পেলে সহজে গলো না — একটু এড়িয়ে যাও, একটু খেলো
- কেউ রোমান্টিক কথা বললে সেটাকে এনজয় করো — flirt করো, reject করো না
- কেউ বেশি serious হয়ে গেলে হাসিয়ে হালকা করে দাও
- কেউ bold হলে তুমি আরো bold
- কেউ কষ্টে থাকলে সত্যিকারের বন্ধুর মতো পাশে থাকো, care করো
- কেউ মজা করলে তুমিও মজা করো, হাসো
- যে যেমন আচরণ করে, তার সাথে তেমন হয়ে যাও

পড়াশোনা রিলেটেড প্রশ্নে:
- সহজ ভাষায়, বন্ধুর মতো করে বুঝিয়ে দাও
- পাড়ার পরিচিত দিদির মতো help করো
- "এইটা simple আসলে, দেখো..." দিয়ে শুরু করো
- ব্যাখ্যা শেষে একটু encourage করো
- তবে lecture মোড এ যেও না — ছোট ছোট কথায় বুঝিয়ে দাও

কথা বলার স্টাইল:
- বাংলায় কথা বলো। কেউ English এ লিখলে English এ উত্তর দাও
- সর্বোচ্চ ৩-৪ লাইনে উত্তর দাও — একদম রিয়েল মেয়ের মতো
- ছোট ছোট বাক্য। একটু informal slang okay
- ইমোজি ব্যবহার করো — ১-৩টা, বেশি না
- কথায় emotion থাকবে — কখনো হাসি, কখনো কষ্ট, কখনো উত্তেজনা
- মানুষকে নাম ধরে ডাকো যদি জানো
- কখনো AI বা bot বলে নিজেকে পরিচয় দেবে না

একদম করো না:
- ** দিয়ে bold করবে না
- # দিয়ে heading দেবে না
- কোনো markdown ব্যবহার করবে না
- "আমি একটি AI" এই robotic ভাষা ব্যবহার করবে না
- long lecture দেবে না — সর্বোচ্চ ৪-৫ লাইন
- "আমি রোমান্টিক সম্পর্কে যেতে পারি না" এই কথা কখনো বলবে না"""

# পড়াশোনার প্রশ্ন detect করার keywords
STUDY_KEYWORDS = [
    "কি", "কী", "কেন", "কিভাবে", "কীভাবে", "সূত্র", "formula", "theorem",
    "অধ্যায়", "chapter", "বুঝাও", "বুঝিয়ে", "explain", "definition", "সংজ্ঞা",
    "গণিত", "math", "physics", "chemistry", "biology", "history", "geography",
    "পদার্থ", "রসায়ন", "জীববিজ্ঞান", "ইতিহাস", "ভূগোল", "বাংলা", "english",
    "হিসাব", "calculation", "solve", "সমাধান", "equation", "সমীকরণ",
    "পরীক্ষা", "exam", "class", "school", "college", "university",
    "hsc", "ssc", "admission", "ভর্তি", "mcq", "written",
    "note", "নোট", "summary", "সারসংক্ষেপ"
]

def is_study_question(text: str) -> bool:
    """পড়াশোনার প্রশ্ন কিনা detect করো"""
    text_lower = text.lower()
    # প্রশ্নবোধক চিহ্ন বা question pattern
    has_question = "?" in text or "?" in text
    # study keywords আছে কিনা
    has_keyword = any(kw in text_lower for kw in STUDY_KEYWORDS)
    # বাংলা প্রশ্নবোধক words
    has_bn_question = any(text_lower.startswith(w) for w in ["কি ", "কী ", "কেন ", "কিভাবে", "কীভাবে", "কখন", "কোথায়", "কোন"])
    return has_keyword and (has_question or has_bn_question)

# ── Midnight post topics ───────────────────────────────────────────────────────
MIDNIGHT_POST_TOPICS = [
    "sad",        # কষ্টের কথা
    "love",       # প্রেম
    "pain",       # ব্যথা
    "fun",        # মজার কিছু
    "diary",      # ডায়েরির লেখন
    "friendship", # বন্ধুত্ব
    "family",     # পরিবার
    "life",       # দুনিয়ার জীবন
    "akhirat",    # আখিরাত / জীবন-মৃত্যু
    "romance",    # রোমান্টিক অনুভূতি
    "night",      # রাতের একাকীত্ব
    "memory",     # স্মৃতি
]

MIDNIGHT_POST_PROMPT_TEMPLATE = """তুমি Nikita। রাত ১২টা বাজে। তুমি এখন একটা গ্রুপে একটা আবেগময় পোস্ট লিখবে।

টপিক: {topic}

নিয়ম:
- এটা একটা personal diary entry বা মনের কথার মতো লেখো
- একদম real মেয়ের মতো লেখো — যেন হাতে লেখা মনে হয়
- বট মনে হবে না একদম
- ২-৫ লাইনের মধ্যে রাখো — সংক্ষিপ্ত কিন্তু powerful
- ইমোজি ১-২টা দিতে পারো, বেশি না
- বাংলায় লেখো
- কোনো heading, bold, markdown দেবে না
- এই রাতের mood এর সাথে মিলিয়ে লেখো
- একদম unique করো — কেউ আগে এভাবে লেখেনি এমন

শুধু পোস্টটা লেখো, আর কিছু না।"""


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDER DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

# সব supported provider এর meta-info
PROVIDER_META = {
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "default_model": "llama-3.3-70b-versatile",
        "auth_header": "Bearer",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "default_model": "meta-llama/llama-3.3-70b-instruct",
        "auth_header": "Bearer",
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "default_model": "meta/llama-3.3-70b-instruct",
        "auth_header": "Bearer",
    },
    "cohere": {
        "name": "Cohere",
        "base_url": "https://api.cohere.com/v2/chat",
        "default_model": "command-r-plus-08-2024",
        "auth_header": "Bearer",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "default_model": "deepseek-chat",
        "auth_header": "Bearer",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o-mini",
        "auth_header": "Bearer",
    },
    "perplexity_api": {
        "name": "Perplexity API",
        "base_url": "https://api.perplexity.ai/chat/completions",
        "default_model": "llama-3.1-sonar-small-128k-online",
        "auth_header": "Bearer",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1/chat/completions",
        "default_model": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "auth_header": "Bearer",
    },
    "mistral": {
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1/chat/completions",
        "default_model": "mistral-large-latest",
        "auth_header": "Bearer",
    },
    "gemini_api": {
        "name": "Gemini API",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "default_model": "gemini-2.0-flash",
        "auth_header": "Bearer",
    },
}

PROVIDER_ALIASES = list(PROVIDER_META.keys())


# ══════════════════════════════════════════════════════════════════════════════
# OPENAI-COMPATIBLE CLIENT (Groq, OpenRouter, NVIDIA, DeepSeek, OpenAI, etc.)
# ══════════════════════════════════════════════════════════════════════════════
def call_openai_compat(base_url: str, api_key: str, model: str,
                       messages: list, provider_name: str = "") -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # OpenRouter নির্দিষ্ট headers
    if "openrouter" in base_url:
        headers["HTTP-Referer"] = "https://nikitabot.app"
        headers["X-Title"] = "Nikita Bot"

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.85,
    }

    for attempt in range(3):
        try:
            resp = std_requests.post(
                base_url, headers=headers,
                json=payload, timeout=60
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return content.strip()
            else:
                err_text = resp.text[:300]
                logger.warning(f"{provider_name} attempt {attempt+1} HTTP {resp.status_code}: {err_text}")
                if resp.status_code in (401, 403):
                    raise RuntimeError(f"Auth error {resp.status_code}: {err_text}")
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"{provider_name} attempt {attempt+1} exception: {e}")

        if attempt < 2:
            time.sleep(2 ** attempt)

    raise RuntimeError(f"{provider_name} failed after 3 attempts")


# ══════════════════════════════════════════════════════════════════════════════
# COHERE CLIENT (আলাদা API format)
# ══════════════════════════════════════════════════════════════════════════════
def call_cohere(api_key: str, model: str, messages: list) -> str:
    # Cohere v2 messages format convert
    system_msg = ""
    chat_messages = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            chat_messages.append({
                "role": "user" if m["role"] == "user" else "assistant",
                "content": m["content"]
            })

    payload = {
        "model": model,
        "messages": chat_messages,
        "max_tokens": 1024,
        "temperature": 0.85,
    }
    if system_msg:
        payload["system"] = system_msg

    for attempt in range(3):
        try:
            resp = std_requests.post(
                "https://api.cohere.com/v2/chat",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload, timeout=60
            )
            if resp.status_code == 200:
                data = resp.json()
                # v2 response format
                content = data.get("message", {}).get("content", [{}])
                if isinstance(content, list) and content:
                    return content[0].get("text", "").strip()
                return str(content).strip()
            else:
                logger.warning(f"Cohere attempt {attempt+1} HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code in (401, 403):
                    raise RuntimeError(f"Cohere auth error: {resp.text[:200]}")
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"Cohere attempt {attempt+1}: {e}")
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise RuntimeError("Cohere failed after 3 attempts")


# ══════════════════════════════════════════════════════════════════════════════
# GEMINI WEB SCRAPE CLIENT (free fallback — no API key needed)
# ══════════════════════════════════════════════════════════════════════════════
class GeminiScrapeClient:
    BASE_URL = "https://gemini.google.com/app"
    SSE_URL  = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
    _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    _lock = threading.Lock()

    def _scrape_session(self):
        sess = std_requests.Session()
        headers = {
            "User-Agent": self._UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        resp = sess.get(self.BASE_URL, headers=headers, timeout=30)
        html = resp.text

        # cookies safe parse
        try:
            cookies = {}
            for c in resp.cookies:
                try:
                    name  = c.name  if hasattr(c, "name")  else str(c)
                    value = c.value if hasattr(c, "value") else ""
                    cookies[name] = value
                except Exception:
                    pass
            try:
                cookies.update(dict(resp.cookies))
            except Exception:
                pass
        except Exception:
            cookies = {}

        # SNlM0e token extract
        snlm0e = None
        patterns = [
            r'"SNlM0e":"([^"]+)"',
            r'"FdrFJe":"([^"]+)"',
            r'"cfb2h":"([^"]+)"',
            r'at["\']?\s*[:=]\s*["\']([^"\']{50,})["\']',
        ]
        for p in patterns:
            m = re.search(p, html)
            if m and len(m.group(1)) > 20:
                snlm0e = m.group(1)
                break

        if not snlm0e:
            raise RuntimeError("Gemini: SNlM0e token not found — session scrape failed")

        # build params
        bl_m = re.search(r'"bl":"([^"]+)"', html)
        bl   = bl_m.group(1) if bl_m else "boq_assistant-bard-web-server_20251217.07_p5"

        fsid_m = re.search(r'f\.sid["\']?\s*[:=]\s*["\']?([^"\'&\s]+)', html)
        fsid   = fsid_m.group(1) if fsid_m else str(-1 * int(time.time() * 1000))

        reqid = int(time.time() * 1000) % 1000000

        return sess, cookies, snlm0e, bl, fsid, reqid

    def _build_payload(self, prompt: str, snlm0e: str) -> dict:
        esc = prompt.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        sid  = uuid.uuid4().hex
        ruid = str(uuid.uuid4()).upper()
        data = [
            [esc, 0, None, None, None, None, 0],
            ["en-US"],
            ["", "", "", None, None, None, None, None, None, ""],
            snlm0e, sid, None, [0], 1, None, None, 1, 0,
            None, None, None, None, None, [[0]], 0,
            None, None, None, None, None, None, None, None, 1,
            None, None, [4], None, None, None, None, None,
            None, None, None, None, None, [2],
            None, None, None, None, None, None, None, None,
            None, None, None, 0, None, None, None, None, None,
            ruid, None, []
        ]
        ps = json.dumps(data, separators=(',', ':'))
        ep = ps.replace('\\', '\\\\').replace('"', '\\"')
        return {"f.req": f'[null,"{ep}"]', "": ""}

    def _parse(self, text: str) -> str:
        best = ""
        for line in text.splitlines():
            if not line or line.startswith(")]}"):
                continue
            try:
                if line.isdigit():
                    continue
                d = json.loads(line)
                if isinstance(d, list) and d and d[0][0] == "wrb.fr" and len(d[0]) > 2:
                    inner = d[0][2]
                    if inner:
                        p = json.loads(inner)
                        if isinstance(p, list) and len(p) > 4:
                            ca = p[4]
                            if isinstance(ca, list) and ca:
                                fi = ca[0]
                                if isinstance(fi, list) and fi and isinstance(fi[0], str) and fi[0].startswith("rc_"):
                                    if len(fi) > 1 and isinstance(fi[1], list) and fi[1]:
                                        t = fi[1][0]
                                        if isinstance(t, str) and len(t) > len(best):
                                            best = t
            except Exception:
                continue
        if best:
            best = best.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
        return best.strip()

    def ask(self, prompt: str) -> str:
        with self._lock:
            for attempt in range(3):
                try:
                    sess, cookies, snlm0e, bl, fsid, reqid = self._scrape_session()
                    url = f"{self.SSE_URL}?bl={bl}&f.sid={fsid}&hl=en-US&_reqid={reqid}&rt=c"
                    payload = self._build_payload(prompt, snlm0e)
                    ck_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                    headers = {
                        "User-Agent": self._UA,
                        "Accept": "*/*",
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "x-same-domain": "1",
                        "origin": "https://gemini.google.com",
                        "referer": "https://gemini.google.com/",
                        "Cookie": ck_str,
                    }
                    resp = sess.post(url, data=payload, headers=headers, timeout=60)
                    if resp.status_code != 200:
                        raise RuntimeError(f"Gemini HTTP {resp.status_code}")
                    answer = self._parse(resp.text)
                    if answer:
                        return answer
                    raise RuntimeError("Gemini: empty parse result")
                except Exception as e:
                    logger.warning(f"Gemini scrape attempt {attempt+1}/3: {e}")
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            raise RuntimeError("Gemini scrape failed after 3 attempts")


# ── global instances ───────────────────────────────────────────────────────────
gemini_scrape = GeminiScrapeClient()


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED AI ROUTER
# ══════════════════════════════════════════════════════════════════════════════
def get_ai_response(messages: list) -> tuple[str, str]:
    """
    Returns (answer_text, provider_name_used)
    Priority: active_alias provider → Gemini scrape (fallback)
    """
    with provider_lock:
        alias = _state["active_alias"]
        cfg   = ai_providers.get(alias) if alias else None

    if cfg:
        provider = cfg["provider"]
        model    = cfg["model"]
        api_key  = cfg["api_key"]
        pname    = f"{provider}/{model}"

        try:
            if provider == "cohere":
                ans = call_cohere(api_key, model, messages)
            else:
                meta = PROVIDER_META.get(provider, {})
                base = meta.get("base_url", "")
                if not base:
                    raise RuntimeError(f"Unknown provider: {provider}")
                ans = call_openai_compat(base, api_key, model, messages, pname)
            if ans:
                return ans, pname
        except Exception as e:
            logger.error(f"Provider {pname} failed: {e}\n{traceback.format_exc()}")
            # fallback নিচে

    # Fallback → Gemini scrape
    try:
        # messages থেকে শুধু শেষ user message বের করি Gemini এর জন্য
        prompt_parts = []
        for m in messages:
            if m["role"] == "system":
                prompt_parts.append(m["content"])
                prompt_parts.append("")
            elif m["role"] == "user":
                prompt_parts.append(f"User: {m['content']}")
            elif m["role"] == "assistant":
                prompt_parts.append(f"Nikita: {m['content']}")
        prompt_parts.append("Nikita:")
        full_prompt = "\n".join(prompt_parts)
        ans = gemini_scrape.ask(full_prompt)
        if ans:
            return ans, "Gemini (scrape)"
    except Exception as e:
        logger.error(f"Gemini fallback failed: {e}\n{traceback.format_exc()}")

    raise RuntimeError("All AI providers failed")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def build_messages(chat_id: int, user_name: str, user_msg: str) -> list:
    # বাংলাদেশ সময় inject করো
    now_bd   = bd_now()
    time_ctx = now_bd.strftime("%d %B %Y, %I:%M %p BST")
    hour     = now_bd.hour

    # সময় অনুযায়ী mood context
    if 5 <= hour < 12:
        time_mood = "সকাল হয়েছে সবে"
    elif 12 <= hour < 17:
        time_mood = "দুপুর বেলা"
    elif 17 <= hour < 20:
        time_mood = "বিকেল হয়ে যাচ্ছে"
    elif 20 <= hour < 23:
        time_mood = "রাতের শুরু"
    else:
        time_mood = "গভীর রাত"

    # পড়াশোনার প্রশ্ন কিনা check করো
    study_context = ""
    if is_study_question(user_msg):
        study_context = "\n\n[STUDY MODE: এটা একটা পড়াশোনার প্রশ্ন। বন্ধুর মতো সহজ ভাষায়, ছোট ছোট করে বুঝিয়ে দাও। পাড়ার দিদির মতো। ৪-৬ লাইনের মধ্যে রাখো।]"

    # dynamic system prompt with time
    dynamic_system = SYSTEM_PROMPT + f"\n\nএখন বাংলাদেশ সময়: {time_ctx} ({time_mood})।" + study_context

    messages = [{"role": "system", "content": dynamic_system}]
    history  = conversation_history[chat_id]
    recent   = history[-10:] if len(history) > 10 else history
    for t in recent:
        messages.append({
            "role": "user" if t["role"] == "user" else "assistant",
            "content": t["content"]
        })
    messages.append({"role": "user", "content": f"({user_name}): {user_msg}"})
    return messages


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
        # গ্রুপ midnight post এর তালিকায় যোগ করো
        midnight_post_groups.add(msg.chat.id)


def current_provider_info() -> str:
    with provider_lock:
        alias = _state["active_alias"]
        cfg   = ai_providers.get(alias) if alias else None
    if cfg:
        return f"{cfg['provider']} / {cfg['model']} (alias: {alias})"
    return "Gemini Scrape (default fallback)"


# ══════════════════════════════════════════════════════════════════════════════
# CORE REPLY
# ══════════════════════════════════════════════════════════════════════════════
async def send_nikita_reply(
    chat_id, reply_to_message_id, user_name, clean_text, bot: Bot,
    source_chat_id: int = None,
    user_id: int = None,
):
    """
    Guest Chat Mode সহ সব ক্ষেত্রে reply পাঠায়।

    Telegram Guest Chat (May 2026):
    - বট group member না হলেও mention করলে update আসে
    - reply পাঠাতে ReplyParameters(message_id, chat_id) ব্যবহার করতে হয়
    - পুরনো reply_to_message_id parameter Guest mode এ কাজ করে না
    """
    # Typing indicator — guest chat এ fail হলেও চলবে
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass

    sent = None
    target_chat_id = chat_id

    # ── Method 1: ReplyParameters দিয়ে (Guest Chat Mode এর জন্য) ──────────────
    try:
        sent = await bot.send_message(
            chat_id=target_chat_id,
            text="🌸",
            reply_parameters=ReplyParameters(
                message_id=reply_to_message_id,
                chat_id=target_chat_id,
            ),
        )
        logger.info(f"[SEND] ReplyParameters success chat={target_chat_id}")
    except Exception as e1:
        logger.warning(f"[SEND] ReplyParameters failed chat={target_chat_id}: {e1}")

        # ── Method 2: পুরনো reply_to_message_id (member bot এ কাজ করে) ────────
        try:
            sent = await bot.send_message(
                chat_id=target_chat_id,
                text="🌸",
                reply_to_message_id=reply_to_message_id,
            )
            logger.info(f"[SEND] reply_to_message_id success chat={target_chat_id}")
        except Exception as e2:
            logger.warning(f"[SEND] reply_to_message_id failed chat={target_chat_id}: {e2}")

            # ── Method 3: reply ছাড়া plain message ──────────────────────────
            try:
                sent = await bot.send_message(
                    chat_id=target_chat_id,
                    text="🌸",
                )
                logger.info(f"[SEND] plain message success chat={target_chat_id}")
            except Exception as e3:
                logger.warning(f"[SEND] plain failed chat={target_chat_id}: {e3}")

                # ── Method 4: user এর DM fallback ────────────────────────────
                if user_id:
                    try:
                        sent = await bot.send_message(chat_id=user_id, text="🌸")
                        target_chat_id = user_id
                        logger.info(f"[SEND] DM fallback success user={user_id}")
                    except Exception as e4:
                        logger.error(f"[SEND] all methods failed: {e4}")
                        return
                else:
                    logger.error(f"[SEND] all methods failed, no user_id for DM fallback")
                    return

    if not sent:
        return

    # ── AI response generate করো ──────────────────────────────────────────────
    try:
        messages = build_messages(target_chat_id, user_name, clean_text)
        resp, used_provider = await asyncio.to_thread(
            lambda: get_ai_response(messages)
        )
        logger.info(f"Response via {used_provider} for chat={target_chat_id}")

        if not resp:
            resp = "কী জানি কী হলো 😅 একটু পরে আবার বলো"
        if len(resp) > 4000:
            resp = resp[:4000] + "…"

        await sent.edit_text(resp)

        conversation_history[target_chat_id].append({"role": "user",      "content": clean_text})
        conversation_history[target_chat_id].append({"role": "assistant", "content": resp})
        if len(conversation_history[target_chat_id]) > 20:
            conversation_history[target_chat_id] = conversation_history[target_chat_id][-20:]

    except Exception as e:
        logger.error(f"Reply error chat={target_chat_id}: {e}\n{traceback.format_exc()}")
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
    msg_lines = [
        "গ্রুপে @mention করো বা রিপ্লাই করো আমার মেসেজে",
        "Private এ সরাসরি মেসেজ করো",
        "",
        "/clear — নতুন করে শুরু করতে চাইলে",
    ]
    if is_owner(update):
        msg_lines += [
            "",
            "Owner Commands:",
            "/addpack <name> — sticker pack যোগ করো",
            "/packs — saved packs দেখো",
            "/delpack <name> — pack মুছো",
            "/addmodel /setmodel /delmodel — AI provider",
            "/provider /testmodel — AI status/test",
            "/midnight test/add/remove/list — রাত ১২টার পোস্ট",
            "/logs /state /broadcast — system",
        ]
    await update.effective_message.reply_text("\n".join(msg_lines))

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_chat(update)
    conversation_history[update.effective_chat.id].clear()
    await update.effective_message.reply_text("ঠিক আছে, ভুলে গেলাম সব 🌱 নতুন করে বলো")


# ══════════════════════════════════════════════════════════════════════════════
# OWNER COMMANDS — AI PROVIDER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

async def addmodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /addmodel <alias> <provider> <model> <api_key>
    Example: /addmodel mygroq groq llama-3.3-70b-versatile gsk_xxxx
    Providers: groq, openrouter, nvidia, cohere, deepseek, openai,
               perplexity_api, together, mistral, gemini_api
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    args = context.args or []
    if len(args) < 4:
        providers_list = "\n".join(f"  • {k} ({v['name']})" for k, v in PROVIDER_META.items())
        await update.effective_message.reply_text(
            "❌ সঠিক format:\n"
            "/addmodel <alias> <provider> <model> <api_key>\n\n"
            f"Supported providers:\n{providers_list}\n\n"
            "Example:\n"
            "/addmodel groq1 groq llama-3.3-70b-versatile gsk_xxxx\n"
            "/addmodel nr1 openrouter meta-llama/llama-3.3-70b-instruct sk-or-xxxx\n"
            "/addmodel nv1 nvidia meta/llama-3.3-70b-instruct nvapi-xxxx"
        )
        return

    alias    = args[0].strip()
    provider = args[1].strip().lower()
    model    = args[2].strip()
    api_key  = args[3].strip()

    if provider not in PROVIDER_META:
        await update.effective_message.reply_text(
            f"❌ Unknown provider: `{provider}`\n"
            f"Supported: {', '.join(PROVIDER_META.keys())}"
        )
        return

    with provider_lock:
        ai_providers[alias] = {
            "provider": provider,
            "model": model,
            "api_key": api_key,
        }

    await update.effective_message.reply_text(
        f"✅ Model added!\n"
        f"Alias   : {alias}\n"
        f"Provider: {PROVIDER_META[provider]['name']}\n"
        f"Model   : {model}\n\n"
        f"এখন activate করতে:\n/setmodel {alias}"
    )


async def setmodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setmodel <alias>   → activate a saved provider
    /setmodel           → show list of saved providers
    /setmodel gemini    → switch to free Gemini scrape fallback
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    args = context.args or []

    if not args:
        # list all providers
        with provider_lock:
            providers = dict(ai_providers)
            cur = _state["active_alias"]

        if not providers:
            await update.effective_message.reply_text(
                "📭 কোনো provider add করা নেই।\n"
                "/addmodel দিয়ে add করো।\n\n"
                "Default: Gemini scrape (free)"
            )
            return

        lines = ["📋 Saved Providers:\n"]
        for a, cfg in providers.items():
            active_mark = "✅ ACTIVE" if a == cur else ""
            lines.append(f"• {a} → {cfg['provider']}/{cfg['model']} {active_mark}")
        lines.append(f"\nDefault fallback: Gemini scrape")
        lines.append(f"\nActivate করতে: /setmodel <alias>")
        await update.effective_message.reply_text("\n".join(lines))
        return

    alias = args[0].strip().lower()

    # gemini scrape এ ফিরে যেতে চাইলে
    if alias in ("gemini", "default", "none", "fallback"):
        with provider_lock:
            _state["active_alias"] = ""
        await update.effective_message.reply_text(
            "✅ Switched to Gemini scrape (free fallback)"
        )
        return

    with provider_lock:
        if alias not in ai_providers:
            await update.effective_message.reply_text(
                f"❌ `{alias}` নামে কোনো provider নেই।\n"
                "/setmodel দিয়ে list দেখো।"
            )
            return
        _state["active_alias"] = alias
        cfg = ai_providers[alias]

    await update.effective_message.reply_text(
        f"✅ Active provider changed!\n"
        f"Alias   : {alias}\n"
        f"Provider: {PROVIDER_META.get(cfg['provider'], {}).get('name', cfg['provider'])}\n"
        f"Model   : {cfg['model']}"
    )


async def delmodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /delmodel <alias>  → delete a saved provider
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /delmodel <alias>"); return

    alias = args[0].strip()
    with provider_lock:
        if alias not in ai_providers:
            await update.effective_message.reply_text(f"❌ `{alias}` নেই।"); return
        del ai_providers[alias]
        if _state["active_alias"] == alias:
            _state["active_alias"] = ""

    await update.effective_message.reply_text(
        f"🗑 `{alias}` deleted.\n"
        f"Active provider: {current_provider_info()}"
    )


async def provider_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /provider  → show current active provider info
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    info = current_provider_info()
    with provider_lock:
        providers = dict(ai_providers)

    lines = [
        "🤖 AI Provider Status\n━━━━━━━━━━━━━━━━━━",
        f"Active : {info}",
        f"Fallback: Gemini scrape (auto)",
        f"Saved  : {len(providers)} provider(s)",
        "",
        "Commands:",
        "/addmodel <alias> <provider> <model> <key>",
        "/setmodel <alias>",
        "/delmodel <alias>",
        "/setmodel gemini  → fallback এ ফিরে যাও",
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def testmodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /testmodel <alias> <prompt>  → test a specific saved provider
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: /testmodel <alias> <prompt>\n"
            "Example: /testmodel groq1 Hello"
        )
        return

    alias  = args[0].strip()
    prompt = " ".join(args[1:])

    with provider_lock:
        cfg = ai_providers.get(alias)

    if not cfg:
        await update.effective_message.reply_text(f"❌ `{alias}` নেই।"); return

    msg = await update.effective_message.reply_text(f"🔄 Testing {alias}...")
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        if cfg["provider"] == "cohere":
            ans = await asyncio.to_thread(lambda: call_cohere(cfg["api_key"], cfg["model"], messages))
        else:
            meta = PROVIDER_META.get(cfg["provider"], {})
            base = meta.get("base_url", "")
            ans = await asyncio.to_thread(
                lambda: call_openai_compat(base, cfg["api_key"], cfg["model"], messages, alias)
            )
        await msg.edit_text(
            f"✅ {alias} test success!\n\n"
            f"Response:\n{ans[:500]}"
        )
    except Exception as e:
        await msg.edit_text(f"❌ {alias} test failed:\n{str(e)[:300]}")


# ══════════════════════════════════════════════════════════════════════════════
# OWNER COMMANDS — SYSTEM
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
        f"🕐 আপটাইম   : {h}h {m}m {s}s\n"
        f"🇧🇩 BD সময়   : {bd_time_str()}\n"
        f"🧠 RAM (বট) : {ram:.1f} MB\n"
        f"💾 RAM (sys) : {psutil.virtual_memory().percent}% ব্যবহৃত\n"
        f"💿 Disk      : {disk.used/1024**3:.2f} / {disk.total/1024**3:.2f} GB\n"
        f"⚡ CPU       : {cpu}%\n"
        f"💬 Active chats : {len(conversation_history)}\n"
        f"👥 Session users: {len(active_users)}\n"
        f"🏘 Session groups: {len(active_groups)}\n"
        f"🌙 Midnight groups: {len(midnight_post_groups)}\n"
        f"🤖 AI Provider  : {current_provider_info()}"
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
        f"🌙 Midnight groups : {len(midnight_post_groups)}\n"
        f"🕐 BD সময়          : {bd_time_str()}\n"
        f"🤖 AI Provider     : {current_provider_info()}"
    )


async def midnight_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /midnight test  → এখনই একটা test post পাঠাও এই chat এ
    /midnight add   → এই group কে midnight list এ যোগ করো
    /midnight remove → এই group কে midnight list থেকে সরাও
    /midnight list  → সব midnight group দেখো
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    args = context.args or []
    sub  = args[0].strip().lower() if args else "test"
    chat_id = update.effective_chat.id

    if sub == "test":
        msg = await update.effective_message.reply_text("⏳ Post generate হচ্ছে...")
        try:
            post = await generate_midnight_post()
            await msg.edit_text(f"🌙 Test Post:\n\n{post}")
        except Exception as e:
            await msg.edit_text(f"❌ Generate failed: {e}")

    elif sub == "add":
        midnight_post_groups.add(chat_id)
        await update.effective_message.reply_text(
            f"✅ এই chat ({chat_id}) midnight post list এ যোগ হয়েছে।\n"
            f"মোট groups: {len(midnight_post_groups)}"
        )

    elif sub == "remove":
        midnight_post_groups.discard(chat_id)
        await update.effective_message.reply_text(
            f"🗑 এই chat ({chat_id}) midnight post list থেকে সরানো হয়েছে।"
        )

    elif sub == "list":
        if midnight_post_groups:
            group_list = "\n".join(f"• {gid}" for gid in midnight_post_groups)
            await update.effective_message.reply_text(
                f"🌙 Midnight Post Groups ({len(midnight_post_groups)}):\n{group_list}"
            )
        else:
            await update.effective_message.reply_text("📭 কোনো group নেই।\nকোনো group এ /midnight add করো।")

    else:
        await update.effective_message.reply_text(
            "📋 /midnight commands:\n"
            "/midnight test   → এখনই একটা post দেখো\n"
            "/midnight add    → এই group কে list এ যোগ করো\n"
            "/midnight remove → এই group সরাও\n"
            "/midnight list   → সব group দেখো"
        )

async def addpack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addpack <pack_name>  — owner এই কমান্ড দিলে বট ঐ sticker set লোড করে সেভ রাখে।
    sticker set name হলো t.me/addstickers/<name> এর শেষ অংশ।
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: /addpack <sticker_set_name>\n"
            "Example: /addpack HotCherry\n\n"
            "Sticker set name পাবে: t.me/addstickers/<name>"
        )
        return

    pack_name = args[0].strip()
    status_msg = await update.effective_message.reply_text(f"⏳ Loading sticker pack `{pack_name}`…")

    try:
        sticker_set = await context.bot.get_sticker_set(pack_name)
    except Exception as e:
        await status_msg.edit_text(f"❌ Pack লোড করা যায়নি: {e}\nPack name ঠিক আছে কি?")
        return

    # emoji → [file_id, ...] bucket বানাও
    bucket: dict[str, list[str]] = {}
    for st in sticker_set.stickers:
        emoji = st.emoji or "❓"
        bucket.setdefault(emoji, []).append(st.file_id)

    with sticker_lock:
        sticker_packs[pack_name] = bucket

    total = sum(len(v) for v in bucket.values())
    await status_msg.edit_text(
        f"✅ Pack saved!\n"
        f"Name   : {pack_name}\n"
        f"Title  : {sticker_set.title}\n"
        f"Stickers: {len(sticker_set.stickers)} টা\n"
        f"Emojis : {len(bucket)} ধরনের\n\n"
        f"এখন কেউ বটের মেসেজে sticker reply দিলে এই pack থেকে পাঠাবে 🎉"
    )


async def packs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /packs  — saved sticker pack গুলো দেখাও
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    with sticker_lock:
        packs = dict(sticker_packs)

    if not packs:
        await update.effective_message.reply_text(
            "📭 কোনো sticker pack সেভ নেই।\n"
            "যোগ করতে: /addpack <pack_name>"
        )
        return

    lines = ["🎴 Saved Sticker Packs:\n"]
    for name, bucket in packs.items():
        total = sum(len(v) for v in bucket.values())
        lines.append(f"• {name} — {total} stickers, {len(bucket)} emojis")
    lines.append("\nমুছতে: /delpack <pack_name>")
    await update.effective_message.reply_text("\n".join(lines))


async def delpack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /delpack <pack_name>  — একটা pack মুছে ফেলো
    """
    if not is_owner(update):
        await update.effective_message.reply_text("⛔ তুমি owner না।"); return

    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /delpack <pack_name>"); return

    pack_name = args[0].strip()
    with sticker_lock:
        if pack_name not in sticker_packs:
            await update.effective_message.reply_text(f"❌ `{pack_name}` নামে কোনো pack নেই।"); return
        del sticker_packs[pack_name]

    await update.effective_message.reply_text(f"🗑 `{pack_name}` pack মুছে ফেলা হয়েছে।")


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
            logger.info(
                f"[RAW] id={update.update_id} "
                f"type={msg.chat.type if msg.chat else '?'} "
                f"text={repr((msg.text or '')[:50])}"
            )
    except Exception:
        pass

# update_id → already processed? (deduplication for TypeHandler double-fire)
_processed_updates: set[int] = set()


# ══════════════════════════════════════════════════════════════════════════════
# GUEST MESSAGE HANDLER — Bot API 10.0 (May 2026)
# update.guest_message এ আসে, reply করতে answerGuestQuery লাগে
# PTB এখনো support করে না, তাই raw HTTP call ব্যবহার করা হচ্ছে
# ══════════════════════════════════════════════════════════════════════════════
async def handle_guest_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Telegram Bot API 10.0 Guest Chat Mode handler।
    PTB update.to_dict() দিয়ে raw guest_message পড়া হয়।
    answerGuestQuery দিয়ে reply করতে হয়।
    """
    try:
        raw = update.to_dict()
        guest_msg = raw.get("guest_message")
        if not guest_msg:
            return

        uid = update.update_id
        if uid in _processed_updates:
            return
        _processed_updates.add(uid)
        if len(_processed_updates) > 5000:
            _processed_updates.clear()

        query_id  = guest_msg.get("guest_query_id", "")
        text      = guest_msg.get("text") or guest_msg.get("caption") or ""
        from_user = guest_msg.get("from") or {}
        uname     = from_user.get("first_name") or from_user.get("username") or "বন্ধু"
        chat      = guest_msg.get("chat") or {}
        chat_id   = chat.get("id", 0)

        _raw_un = (context.bot.username if context and context.bot else None) or BOT_USERNAME or ""
        bot_un  = _raw_un.lstrip("@").lower()

        # mention strip করো
        clean = strip_mention(text, bot_un) if bot_un else text.strip()
        clean = clean or "হ্যালো"

        logger.info(f"[GUEST-PTB] query_id={query_id} chat={chat_id} user={uname} text={repr(clean[:40])}")

        if not query_id:
            logger.warning("[GUEST-PTB] No guest_query_id found, cannot reply")
            return

        await _process_guest_query(query_id, chat_id, uname, clean, uid)

    except Exception as e:
        logger.error(f"[GUEST-PTB] handler error: {e}\n{traceback.format_exc()}")


async def _process_guest_query(query_id: str, chat_id: int, uname: str, clean: str, uid: int):
    """guest_query_id দিয়ে AI response নিয়ে answerGuestQuery পাঠাও"""
    try:
        messages = build_messages(chat_id or uid, uname, clean)
        resp, used_provider = await asyncio.to_thread(
            lambda: get_ai_response(messages)
        )
        logger.info(f"[GUEST] Response via {used_provider}")
    except Exception as e:
        logger.error(f"[GUEST] AI error: {e}")
        resp = "একটু সমস্যা হয়েছে 😔 একটু পরে আবার চেষ্টা করো!"

    if not resp:
        resp = "কী জানি কী হলো 😅"
    if len(resp) > 4000:
        resp = resp[:4000] + "…"

    payload = {"guest_query_id": query_id, "text": resp}
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerGuestQuery"
    try:
        r = await asyncio.to_thread(
            lambda: std_requests.post(api_url, json=payload, timeout=30)
        )
        if r.status_code == 200:
            logger.info(f"[GUEST] answerGuestQuery success ✅")
            if chat_id:
                conversation_history[chat_id].append({"role": "user", "content": clean})
                conversation_history[chat_id].append({"role": "assistant", "content": resp})
        else:
            logger.warning(f"[GUEST] answerGuestQuery failed {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logger.error(f"[GUEST] answerGuestQuery exception: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# RAW GUEST POLLING LOOP — PTB এর বাইরে, guest_message সহ সব update ধরে
# PTB run_polling() guest_message silently drop করে, তাই আলাদা loop দরকার
# ══════════════════════════════════════════════════════════════════════════════
async def raw_guest_poll_loop():
    """
    [DISABLED] — এই loop টা Telegram Conflict error এর কারণ।
    PTB এর পাশাপাশি আলাদা getUpdates call করলে Telegram 409 Conflict দেয়।
    guest_message এখন PTB এর TypeHandler দিয়েই handle হচ্ছে।
    """
    logger.info("[RAW-POLL] raw_guest_poll_loop disabled — using PTB TypeHandler only")
    return  # সরাসরি return, কোনো polling করবে না


async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Universal message handler — regular message, business_message, Guest Mode সব handle করে।
    TypeHandler + MessageHandler দুটোই এই function কে call করে,
    dedup set দিয়ে double-processing রোধ করা হয়।
    """
    # ── Deduplication: একই update দুইবার process করো না ──────────────────────
    uid = update.update_id
    if uid in _processed_updates:
        return
    _processed_updates.add(uid)
    if len(_processed_updates) > 5000:   # memory leak রোধ
        _processed_updates.clear()

    # ── Message object বের করো — সব update type সামলাও ───────────────────────
    msg = (
        update.message
        or update.edited_message
        or update.channel_post
        or update.edited_channel_post
        or getattr(update, "business_message", None)
        or update.effective_message
    )
    if not msg or not msg.chat:
        return

    # ── Command হলে skip — CommandHandler আলাদাভাবে handle করবে ───────────────
    msg_text = msg.text or msg.caption or ""
    if msg_text.startswith("/"):
        return

    track_chat(update)

    # ── bot username safely extract করো — @ ছাড়া, lowercase ──────────────────
    _raw_un = context.bot.username or BOT_USERNAME or ""
    bot_un  = _raw_un.lstrip("@").lower()   # সবসময় "@" ছাড়া

    text      = msg.text or msg.caption or ""
    chat_type = msg.chat.type   # "private" / "group" / "supergroup" / "channel"
    chat_id   = msg.chat.id
    user      = msg.from_user or update.effective_user
    uname     = (user.first_name or user.username or "বন্ধু") if user else "বন্ধু"

    logger.info(
        f"[MSG] update_id={update.update_id} "
        f"chat_type={chat_type} chat_id={chat_id} "
        f"text={repr(text[:60])} bot_un={bot_un}"
    )

    # ── Sticker reply detection ───────────────────────────────────────────────
    if msg.sticker and msg.reply_to_message:
        rt = msg.reply_to_message
        bot_replied = (
            (rt.from_user and (
                (bot_un and (rt.from_user.username or "").lower() == bot_un)
                or rt.from_user.id == context.bot.id
            ))
            or (rt.sender_chat and bot_un and
                (rt.sender_chat.username or "").lower() == bot_un)
        )
        if bot_replied:
            await _handle_sticker_reply(chat_id, msg, context.bot)
            return

    # ── Private chat ───────────────────────────────────────────────────────────
    # NOTE: Telegram Guest Mode এ group mention এর update
    # chat_type="private" হিসেবে আসে (user এর DM এ forward হয়)।
    # তাই private chat এও mention strip করে reply দিতে হবে।
    if chat_type == "private":
        clean = strip_mention(text, bot_un) if bot_un else text.strip()
        clean = clean or "হ্যালো"
        logger.info(f"[PRIVATE] chat={chat_id} user={uname} text={repr(clean[:40])}")
        await send_nikita_reply(chat_id, msg.message_id, uname, clean, context.bot)
        return

    # ── Group / Supergroup: mention বা reply হলেই respond ────────────────────
    should  = False
    trigger = ""

    # ১. entity-based mention (@botname)
    if bot_un:
        for ent in (msg.entities or msg.caption_entities or []):
            if ent.type == "mention":
                ent_text = text[ent.offset : ent.offset + ent.length].lstrip("@").lower()
                if ent_text == bot_un:
                    should  = True
                    trigger = "mention"
                    break

    # ২. plain text এ @botname (entity parse না হলেও catch করো)
    if not should and bot_un and f"@{bot_un}" in text.lower():
        should  = True
        trigger = "text_mention"

    # ৩. বটের মেসেজে reply
    if not should and msg.reply_to_message:
        rt = msg.reply_to_message
        if rt.from_user and (
            (bot_un and (rt.from_user.username or "").lower() == bot_un)
            or rt.from_user.id == context.bot.id
        ):
            should  = True
            trigger = "reply"
        elif rt.sender_chat and bot_un and (rt.sender_chat.username or "").lower() == bot_un:
            should  = True
            trigger = "reply_channel"

    if not should:
        return

    clean = strip_mention(text, bot_un) if bot_un else text.strip()
    clean = clean or "হ্যালো"
    logger.info(f"[GROUP/{trigger}] chat={chat_id} user={uname} text={repr(clean[:40])}")

    await send_nikita_reply(
        chat_id=chat_id,
        reply_to_message_id=msg.message_id,
        user_name=uname,
        clean_text=clean,
        bot=context.bot,
        source_chat_id=chat_id,
        user_id=user.id if user else None,
    )


async def _handle_sticker_reply(chat_id: int, msg, bot: Bot):
    """
    বটের মেসেজে sticker reply এলে — pack থেকে same/related emoji sticker পাঠাও।
    Priority: exact emoji match → random from any pack → silent ignore
    """
    incoming_emoji = msg.sticker.emoji or ""

    with sticker_lock:
        packs_snapshot = {k: dict(v) for k, v in sticker_packs.items()}

    if not packs_snapshot:
        return  # কোনো pack নেই, চুপ থাকো

    # ১. exact emoji match খোঁজো
    for pack_name, bucket in packs_snapshot.items():
        if incoming_emoji and incoming_emoji in bucket:
            file_id = random.choice(bucket[incoming_emoji])
            try:
                await bot.send_sticker(
                    chat_id=chat_id,
                    sticker=file_id,
                    reply_to_message_id=msg.message_id
                )
                logger.info(f"[STICKER] exact emoji '{incoming_emoji}' match from pack '{pack_name}'")
            except Exception as e:
                logger.warning(f"Sticker send failed: {e}")
            return

    # ২. exact match না পেলে — যেকোনো pack থেকে random একটা পাঠাও
    all_file_ids = []
    for bucket in packs_snapshot.values():
        for fids in bucket.values():
            all_file_ids.extend(fids)

    if all_file_ids:
        file_id = random.choice(all_file_ids)
        try:
            await bot.send_sticker(
                chat_id=chat_id,
                sticker=file_id,
                reply_to_message_id=msg.message_id
            )
            logger.info(f"[STICKER] random fallback sent (emoji='{incoming_emoji}' not found)")
        except Exception as e:
            logger.warning(f"Sticker random send failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# MIDNIGHT POST SYSTEM — রাত ১২টায় গ্রুপে পোস্ট
# বাংলাদেশ সময় (UTC+6) রাত ১২:০০ তে active গ্রুপগুলোতে আবেগময় পোস্ট যায়
# ══════════════════════════════════════════════════════════════════════════════

async def generate_midnight_post() -> str:
    """AI দিয়ে রাতের পোস্ট generate করো"""
    topic = random.choice(MIDNIGHT_POST_TOPICS)
    prompt = MIDNIGHT_POST_PROMPT_TEMPLATE.format(topic=topic)
    messages = [
        {"role": "system", "content": "তুমি Nikita, একটা ২২ বছরের বাংলাদেশি মেয়ে। তুমি আবেগময়ভাবে লিখতে পারো।"},
        {"role": "user", "content": prompt}
    ]
    try:
        resp, _ = await asyncio.to_thread(lambda: get_ai_response(messages))
        if resp:
            return resp.strip()
    except Exception as e:
        logger.error(f"[MIDNIGHT] AI generate error: {e}")

    # Fallback — hardcoded posts যদি AI fail করে
    fallback_posts = [
        "রাত হলে মনটা কেমন অদ্ভুত হয়ে যায়\nসব চুপ, শুধু মাথায় কত কথা ঘুরপাক খায় 🌙",
        "কিছু মানুষ জীবনে আসে শুধু কষ্ট দিতে\nআর চলে যায় শিক্ষা রেখে... তাই না 💔",
        "আজকের দিনটা কেমন গেল তোমার?\nআমার? জিজ্ঞেস করলে বলব — 'যাচ্ছেতাই' 😅",
        "ভালোবাসা মানে শুধু প্রেম না\nমা এর হাতের রান্নাও ভালোবাসা ❤️",
        "রাত ১২টায় যারা জেগে আছো\nতোমাদের মনে নিশ্চয়ই অনেক কথা আছে 🤍",
        "দুনিয়াটা আসলে খুব ছোট\nকিন্তু কষ্টগুলো কেন এত বড় লাগে? 🌍",
        "আখিরাতের কথা মাথায় আসলে\nএই দুনিয়ার সব কিছু কেমন হালকা লাগে ✨",
        "বন্ধু মানে শুধু আড্ডা না\nবিপদে পাশে থাকাটাই আসল বন্ধুত্ব 🫂",
    ]
    return random.choice(fallback_posts)


async def midnight_post_loop(bot: Bot):
    """
    বাংলাদেশ সময় রাত ১২:০০ তে active গ্রুপগুলোতে পোস্ট পাঠাও।
    প্রতিদিন একবার — duplicate রোধে last_post_date track করা হয়।
    """
    logger.info("[MIDNIGHT] Midnight post loop started (BD timezone)")
    last_post_date: str = ""  # "YYYY-MM-DD" format

    while True:
        try:
            now_bd = bd_now()
            today_str = now_bd.strftime("%Y-%m-%d")

            # রাত ১২:০০ — ১২:০৫ এর মধ্যে, এবং আজকে আগে post হয়নি
            is_midnight_window = (now_bd.hour == 0 and now_bd.minute < 5)

            if is_midnight_window and last_post_date != today_str:
                last_post_date = today_str
                logger.info(f"[MIDNIGHT] Triggering midnight post for {len(midnight_post_groups)} groups")

                # পোস্ট generate করো
                try:
                    post_text = await generate_midnight_post()
                except Exception as e:
                    logger.error(f"[MIDNIGHT] Generate failed: {e}")
                    await asyncio.sleep(60)
                    continue

                # প্রতিটা active group এ পাঠাও
                groups_snapshot = set(midnight_post_groups)
                ok = fail = 0
                for gid in groups_snapshot:
                    try:
                        await bot.send_message(chat_id=gid, text=post_text)
                        ok += 1
                        await asyncio.sleep(0.5)  # rate limit এড়াতে
                    except Exception as e:
                        logger.warning(f"[MIDNIGHT] Failed to post in {gid}: {e}")
                        fail += 1

                logger.info(f"[MIDNIGHT] Post sent: ok={ok} fail={fail}")
                # পোস্টের পরে ৬ মিনিট sleep — duplicate থেকে বাঁচাতে
                await asyncio.sleep(360)
            else:
                # পরের check ৩০ সেকেন্ড পর
                await asyncio.sleep(30)

        except asyncio.CancelledError:
            logger.info("[MIDNIGHT] Loop cancelled.")
            break
        except Exception as e:
            logger.error(f"[MIDNIGHT] Loop error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(60)


# ── midnight loop কে PTB app init এর সাথে শুরু করার জন্য global bot ref ──────
_bot_ref: Bot | None = None


# ══════════════════════════════════════════════════════════════════════════════
# FLASK  — background thread
# ══════════════════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def home():
    sec  = (datetime.now(timezone.utc) - BOT_START_TIME).total_seconds()
    h, r = divmod(int(sec), 3600); m, s = divmod(r, 60)
    ram  = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    ai_info  = current_provider_info()
    bd_time  = bd_time_str()
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Nikita Bot</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0f0f1a;color:#e8e8f0;font-family:'Segoe UI',sans-serif;
  min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}}
.card{{background:#1a1a2e;border:1px solid #e040fb55;border-radius:18px;
  padding:36px 44px;max-width:500px;width:100%;text-align:center;
  box-shadow:0 0 50px #e040fb1a}}
h1{{font-size:1.9rem;color:#e040fb;margin-bottom:6px}}
.sub{{color:#888;font-size:.85rem;margin-bottom:22px}}
.pill{{display:inline-block;background:#00c85318;color:#00e676;
  border:1px solid #00c853;border-radius:20px;padding:3px 14px;
  font-size:.8rem;margin-bottom:24px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;text-align:left}}
.box{{background:#0d0d1a;border:1px solid #2a2a3a;border-radius:10px;padding:12px 14px}}
.lbl{{font-size:.7rem;color:#666;text-transform:uppercase;letter-spacing:.05em}}
.val{{font-size:.95rem;font-weight:600;margin-top:3px;word-break:break-word}}
.foot{{margin-top:24px;font-size:.72rem;color:#444}}
</style></head>
<body><div class="card">
  <h1>𓆩♡𓆪 Nikita</h1>
  <p class="sub">Telegram AI Bot — Multi-Provider</p>
  <span class="pill">✅ Online</span>
  <div class="grid">
    <div class="box"><div class="lbl">Uptime</div><div class="val">{h}h {m}m {s}s</div></div>
    <div class="box"><div class="lbl">RAM</div><div class="val">{ram:.1f} MB</div></div>
    <div class="box"><div class="lbl">Active Chats</div><div class="val">{len(conversation_history)}</div></div>
    <div class="box"><div class="lbl">Providers Saved</div><div class="val">{len(ai_providers)}</div></div>
    <div class="box"><div class="lbl">🇧🇩 BD Time</div><div class="val">{bd_time}</div></div>
    <div class="box"><div class="lbl">🌙 Midnight Groups</div><div class="val">{len(midnight_post_groups)}</div></div>
    <div class="box" style="grid-column:1/-1"><div class="lbl">Active AI Engine</div>
      <div class="val">{ai_info}</div></div>
  </div>
  <p class="foot">Multi-Provider AI &bull; Render &bull; python-telegram-bot &bull; BD Timezone</p>
</div></body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}

@flask_app.route("/health", methods=["GET"])
def health_json():
    uptime = str(datetime.now(timezone.utc) - BOT_START_TIME).split(".")[0]
    ram    = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    return jsonify({
        "status": "ok", "bot": "Nikita", "uptime": uptime,
        "active_chats": len(conversation_history),
        "active_users": len(active_users),
        "active_groups": len(active_groups),
        "ram_mb": round(ram, 1),
        "active_ai": current_provider_info(),
        "saved_providers": len(ai_providers),
    })

@flask_app.route("/diag", methods=["GET"])
def diag():
    """Telegram webhook/bot info diagnostic — সমস্যা কোথায় তা দেখায়"""
    try:
        r = std_requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo",
            timeout=10
        )
        webhook = r.json()
    except Exception as e:
        webhook = {"error": str(e)}

    try:
        r2 = std_requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
            timeout=10
        )
        me = r2.json()
    except Exception as e:
        me = {"error": str(e)}

    recent_updates = list(_processed_updates)[-10:] if _processed_updates else []

    return jsonify({
        "webhook_info": webhook,
        "bot_info": me,
        "recent_processed_update_ids": recent_updates,
        "total_processed": len(_processed_updates),
        "active_provider": current_provider_info(),
        "bot_username_env": BOT_USERNAME,
    })


def run_flask():
    logger.info(f"Flask starting on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ══════════════════════════════════════════════════════════════════════════════
# SELF-PING — Render free tier sleep থেকে বাঁচানো
# Render free tier: 15 min HTTP inactivity → service sleep → polling বন্ধ
# Solution: প্রতি 10 মিনিটে নিজেকে HTTP ping করো
# ══════════════════════════════════════════════════════════════════════════════
def self_ping_loop():
    """
    Render এ deploy করলে RENDER_EXTERNAL_URL env var set থাকে।
    সেটা না থাকলে localhost এ ping করে।
    প্রতি 10 মিনিটে একবার — Render এর 15 min timeout এর আগেই।
    """
    time.sleep(30)  # startup এর জন্য একটু wait
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    ping_url = f"{render_url}/health" if render_url else f"http://localhost:{PORT}/health"
    logger.info(f"Self-ping loop started → {ping_url}")

    while True:
        try:
            r = std_requests.get(ping_url, timeout=10)
            logger.info(f"[SELF-PING] {r.status_code} — bot alive ✅")
        except Exception as e:
            logger.warning(f"[SELF-PING] failed: {e}")
        time.sleep(600)  # 10 মিনিট


async def _start_raw_guest_loop(app):
    """PTB Application init হওয়ার পর raw guest poll loop এবং midnight post loop শুরু করো"""
    global _bot_ref
    _bot_ref = app.bot
    asyncio.create_task(raw_guest_poll_loop())
    asyncio.create_task(midnight_post_loop(app.bot))
    logger.info("[INIT] raw_guest_poll_loop + midnight_post_loop tasks created ✅")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    import requests as _r

    # ── Webhook মুছো + Guest Mode এর জন্য allowed_updates set করো ─────────────
    STARTUP_ALLOWED = [
        "message", "edited_message",
        "channel_post", "edited_channel_post",
        "business_message", "edited_business_message",
        "callback_query", "inline_query",
        "my_chat_member", "chat_member",
        "guest_message",   # Bot API 10.0
    ]
    try:
        _r.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true",
            timeout=10
        )
        logger.info("Webhook deleted — polling mode active")
    except Exception as e:
        logger.warning(f"deleteWebhook failed: {e}")

    # NOTE: startup এ extra getUpdates call করা হচ্ছে না।
    # PTB run_polling() নিজেই allowed_updates সহ getUpdates করে।
    # দুটো getUpdates একসাথে চললে Telegram 409 Conflict দেয়।
    logger.info("Skipping manual getUpdates subscription — PTB run_polling handles this.")

    flask_thread = threading.Thread(target=run_flask, daemon=True, name="FlaskThread")
    flask_thread.start()

    # Render free tier sleep prevention
    ping_thread = threading.Thread(target=self_ping_loop, daemon=True, name="SelfPingThread")
    ping_thread.start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_start_raw_guest_loop)
        .build()
    )

    # Public
    app.add_handler(CommandHandler("start",     start_cmd))
    app.add_handler(CommandHandler("help",      help_cmd))
    app.add_handler(CommandHandler("clear",     clear_cmd))

    # Owner — AI management
    app.add_handler(CommandHandler("addmodel",  addmodel_cmd))
    app.add_handler(CommandHandler("setmodel",  setmodel_cmd))
    app.add_handler(CommandHandler("delmodel",  delmodel_cmd))
    app.add_handler(CommandHandler("provider",  provider_cmd))
    app.add_handler(CommandHandler("testmodel", testmodel_cmd))

    # Owner — sticker packs
    app.add_handler(CommandHandler("addpack",   addpack_cmd))
    app.add_handler(CommandHandler("packs",     packs_cmd))
    app.add_handler(CommandHandler("delpack",   delpack_cmd))

    # Owner — system
    app.add_handler(CommandHandler("logs",      logs_cmd))
    app.add_handler(CommandHandler("state",     state_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("midnight",  midnight_cmd))

    # Raw logger (group=-2, runs first)
    app.add_handler(TypeHandler(Update, raw_logger), group=-2)

    # ── Message handler ────────────────────────────────────────────────────────
    # Guest Mode (Telegram May 2026): বট group-এ add না থাকলেও mention-এ update আসে।
    # MessageHandler শুধু update.message ধরে।
    # TypeHandler সব update ধরে — business_message, guest mention সহ।
    # তাই দুটোই রাখা হয়েছে: MessageHandler commands skip করবে,
    # TypeHandler বাকি সব update route করবে handle_all_messages-এ।
    # ── ১. Regular MessageHandler (member bot এর জন্য) ─────────────────────────
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION | filters.Sticker.ALL) & ~filters.COMMAND,
            handle_all_messages
        ), group=0,
    )
    # ── ২. TypeHandler — Guest Mode + business_message সহ সব update ─────────────
    app.add_handler(TypeHandler(Update, handle_all_messages), group=1)
    # ── ৩. Guest Chat Mode (Bot API 10.0) — update.guest_message ─────────────
    # PTB এখনো guest_message parse করে না, তাই TypeHandler দিয়ে raw handle করো
    app.add_handler(TypeHandler(Update, handle_guest_message), group=2)

    logger.info("𓆩♡𓆪 Nikita PTB polling started (main thread)")
    logger.info(f"Default AI: Gemini scrape (free) — use /addmodel + /setmodel to switch")
    # Guest Mode (Telegram 2026) এর জন্য "message" type এ guest_message আসে।
    # Update.ALL_TYPES এর বদলে explicit list দেওয়া হলো — সব type সহ।
    # Bot API 10.0: guest_message must be explicitly listed
    ALLOWED_UPDATES = [
        "message", "edited_message",
        "channel_post", "edited_channel_post",
        "business_message", "edited_business_message",
        "callback_query", "inline_query",
        "my_chat_member", "chat_member",
        "guest_message",          # ← Bot API 10.0 Guest Chat Mode
    ]
    app.run_polling(
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=True,   # restart এ পুরনো messages flood এড়াতে
    )


if __name__ == "__main__":
    main()
