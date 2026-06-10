# 𓆩♡𓆪 Nikita — Telegram AI Chatbot

একটি advanced Telegram girl chatbot যা Copilot AI ব্যবহার করে রিপ্লাই দেয়।

---

## ✨ ফিচারসমূহ

| ফিচার | বিবরণ |
|---|---|
| 💬 স্মার্ট রিপ্লাই | Copilot AI দিয়ে প্রতিটি মেসেজে সুন্দর উত্তর |
| 🔗 চেইন কথোপকথন | প্রতিটি চ্যাটের history আলাদাভাবে সংরক্ষিত |
| ✍️ লাইভ স্ট্রিমিং | টাইপিং indicator সহ live response |
| 👥 একাধিক গ্রুপ | সব গ্রুপে একসাথে স্বাধীনভাবে কাজ করে |
| 🔔 Guest Mode | Reply বা @mention করলেই রিপ্লাই |
| 🏥 Health Check | `/health` endpoint দিয়ে bot status দেখা যায় |
| 💾 Bandwidth সাশ্রয়ী | Render Free (5GB/মাস) এর মধ্যে চলার জন্য optimize করা |

---

## 🚀 Render-এ Deploy করার ধাপ

### ১. GitHub-এ Code আপলোড করো

```
nikita_bot/
├── bot.py
├── requirements.txt
└── .env.example
```

GitHub-এ একটি নতুন repository তৈরি করে এই ফাইলগুলো push করো।

---

### ২. Render-এ Web Service তৈরি করো

1. [render.com](https://render.com) এ লগইন করো
2. **New → Web Service** ক্লিক করো
3. তোমার GitHub repo সংযুক্ত করো
4. নিচের সেটিং দাও:

| সেটিং | মান |
|---|---|
| **Name** | `nikita-bot` (যেকোনো নাম) |
| **Region** | Singapore (কাছের) |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |
| **Instance Type** | `Free` |

---

### ৩. Environment Variables সেট করো

Render Dashboard → তোমার service → **Environment** ট্যাবে যাও:

```
BOT_TOKEN      =  তোমার_bot_token_এখানে
BOT_USERNAME   =  তোমার_bot_username_এখানে  (@ ছাড়া)
PORT           =  8080
```

> **BOT_TOKEN কোথায় পাবে?**
> Telegram-এ @BotFather তে গিয়ে `/newbot` দিয়ে বট তৈরি করো।
> BOT_USERNAME = BotFather যে username দেবে সেটি (@ ছাড়া)

---

### ৪. Deploy করো

**Manual Deploy** বাটনে ক্লিক করো — কিছুক্ষণের মধ্যে bot চালু হয়ে যাবে।

---

## 🏥 Health Check URL

Deploy হলে Render তোমাকে একটি URL দেবে, যেমন:
```
https://nikita-bot.onrender.com
```

**Bot চলছে কিনা দেখতে:**
```
https://nikita-bot.onrender.com/health
```

সেখানে গেলে এরকম দেখাবে:
```json
{
  "status": "healthy",
  "bot_name": "𓆩♡𓆪 Nikita",
  "uptime": "2:34:15",
  "active_chats": 5,
  "timestamp": "2025-01-01T12:00:00Z"
}
```

---

## 💬 Bot ব্যবহারের নিয়ম

### Private Chat
সরাসরি যেকোনো মেসেজ পাঠাও — Nikita উত্তর দেবে।

### Group Chat
দুটি উপায়ে Nikita-কে ডাকা যাবে:

```
@তোমার_bot_username হ্যালো, কেমন আছো?
```
অথবা Nikita-র কোনো মেসেজে **Reply** করো।

### Commands
| Command | কাজ |
|---|---|
| `/start` | বট চালু করো |
| `/help` | সাহায্য দেখো |
| `/clear` | কথোপকথন রিসেট করো |

---

## ⚠️ Bandwidth সাশ্রয়ের টিপস (5GB/মাস)

- প্রতি চ্যাটে সর্বোচ্চ শেষ **10টি** বার্তা মনে রাখে
- Polling interval **2 সেকেন্ড** (অপ্রয়োজনীয় request কমায়)
- শুধুমাত্র Reply বা @mention এ রিপ্লাই করে
- Pending/পুরনো মেসেজ restart-এ skip করে

---

## 🔄 কথোপকথন কীভাবে কাজ করে

```
Group A:                    Group B:
User1 → Nikita              User2 → Nikita
Nikita → User1              Nikita → User2
User1 → Nikita              (Group A-র কথা মনে নেই)
  (Group A-র history চলে)
```

প্রতিটি chat আলাদা — একটি গ্রুপের কথা অন্য গ্রুপে মিশবে না।
