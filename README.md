# 💊 SalesBot — Pharma Sales Assistant

> **The smart Telegram assistant for pharmaceutical sales reps in Iraq** — log orders, track visits and payments from your phone, and get performance reports automatically.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot_API-2CA5E0.svg)](https://core.telegram.org/bots/api)
[![Database](https://img.shields.io/badge/Database-PostgreSQL-336791.svg)](https://www.postgresql.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 📖 About

**SalesBot** is a Telegram bot designed for pharmaceutical sales representatives across Iraq. Instead of paperwork and spreadsheets, reps log orders, doctor visits, and pharmacy payments directly from their phones — in free-form Arabic — while managers get automatic reports, Excel exports, and performance rankings.

---

## ✨ Key Features

### 🧠 Smart Order Entry
- **Write in free Arabic** — the bot understands natural order messages via AI (OpenRouter, default `GPT-4o-mini`)
- **Offline-safe fallback** — regex + fuzzy matching keeps working even if the AI is unreachable
- **Auto-save** — orders are saved automatically after 30 seconds, no extra taps needed
- **Arabic number words** — understands amounts written as words (e.g. "خمسة آلاف")

### 👨‍⚕️ Doctor & Pharmacy CRM
- **Doctor profiles** with automatic classification
- **Competitor drug tracking** per doctor
- **Pharmacy balances** tracked per sales rep
- **Visit history** for every account

### 📊 Reports & Analytics
- **Text reports** and **Excel exports** on demand
- **Bulk import** from Excel using a ready-made template
- **Monthly comparisons** and performance statistics
- **Rep leaderboard** — see who is performing best

### ⏰ Scheduled Alerts (Asia/Baghdad)
- 📅 **Daily summary** of the day's activity
- 📈 **Weekly Excel report** delivered automatically
- 😴 **Inactivity alert** — notified if a rep is idle for 3 hours
- 🏪 **Pharmacy reminder** — flags pharmacies not visited in 7 days

### 💳 Subscriptions
- **First month free**, then **5,000 IQD/month** or **50,000 IQD/year** (via Qi Card)
- **Soft-lock mode** for expired subscriptions
- **In-bot admin panel** to manage users and payments

---

## 🛠️ Tech Stack

| Component      | Technology                                      |
|----------------|-------------------------------------------------|
| Language       | Python 3.11+                                    |
| Bot Framework  | `python-telegram-bot` 21.6 (with `job-queue`)   |
| AI             | OpenRouter API (default: GPT-4o-mini)           |
| Excel          | `openpyxl`                                      |
| Database       | PostgreSQL (`psycopg[binary]` + `psycopg-pool`)  |
| Timezone       | `tzdata` (Asia/Baghdad)                         |

---

## 🚀 Installation

### Prerequisites
- Python 3.11+
- PostgreSQL database
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### Setup

```bash
# 1. Clone the repository
git clone https://github.com/tahagamer712-beep/Salesb0t.git
cd Salesb0t

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and fill in:
#   TELEGRAM_BOT_TOKEN=your_bot_token
#   ADMIN_TELEGRAM_IDS=your_user_id

# 4. Run the bot
python bot.py
```

> 📱 **Termux users:** install PostgreSQL client libraries first:
> ```bash
> pkg install postgresql libpq
> ```

---

## ⚙️ Configuration

| Variable               | Required | Description                                  |
|------------------------|----------|----------------------------------------------|
| `TELEGRAM_BOT_TOKEN`   | ✅       | Telegram bot token from BotFather            |
| `ADMIN_TELEGRAM_IDS`   | ✅       | Comma-separated Telegram IDs of admins       |
| `DATABASE_URL`         | ✅       | PostgreSQL connection string                 |
| `OPENROUTER_API_KEY`   | ➖       | For AI-powered order parsing                 |

See `.env.example` for the full list.

---

## 📁 Project Structure

```
Salesb0t/
├── bot.py               # Main entry point
├── handlers/            # Command & message handlers
├── crm.py               # Doctor/pharmacy CRM logic
├── reports.py           # Report & Excel generation
├── scheduler.py         # Scheduled alerts & jobs
├── subscriptions.py     # Payment & subscription logic
├── db/                  # PostgreSQL models & queries
├── templates/           # Excel import templates
├── .env.example         # Environment template
└── requirements.txt     # Dependencies
```

---

## 💡 Usage Examples

**Log an order** — just send a message like:
> طلب من صيدلية الشفاء: 10 علب دواء X بـ 5000 دينار

The bot parses it, confirms, and auto-saves.

**Get a report** — use `/report` for today's summary or `/excel` for a spreadsheet export.

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

## 📬 Contact

- **Developer:** [@tahagamer712-beep](https://github.com/tahagamer712-beep)
- **Issues:** [GitHub Issues](https://github.com/tahagamer712-beep/Salesb0t/issues)

---

<p align="center">Built to make pharma sales easier 💊</p>
