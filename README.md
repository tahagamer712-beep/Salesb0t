# بوت مبيعات طبي

## الإعداد

### 1. انسخ ملف الإعدادات
```bash
cp .env.example .env
```

### 2. عدّل ملف `.env` وضع قيمك الحقيقية
```
TELEGRAM_BOT_TOKEN=توكن البوت من BotFather
ADMIN_TELEGRAM_IDS=رقمك على تيليغرام (يمكن أكثر من رقم مفصولة بفاصلة)
```

### 3. ثبّت المتطلبات
```bash
pip install -r requirements.txt
```

### 4. شغّل البوت
```bash
python bot.py
```

## التثبيت على Termux (أندرويد)

كل مكتبات المشروع (`requirements.txt`) خفيفة ومكتوبة بايثون خالص، **ما عدا `psycopg2-binary`** — هذي وحدة قد تحتاج تصريف من المصدر على Termux لأن PyPI ما يوفّر wheel جاهز لكل نسخ بايثون/معمارية Termux دائماً. لو واجهت خطأ أثناء `pip install -r requirements.txt` يذكر `pg_config` أو `error: command 'gcc' failed`، نفّذ هذا **قبل** إعادة المحاولة:

```bash
pkg update
pkg install postgresql libpq
pip install -r requirements.txt
```

لو استمر الفشل بعد هذا، جرب تثبيت `psycopg2-binary` لحالها أول للتأكد:
```bash
pip install psycopg2-binary
```

باقي المكتبات (`python-telegram-bot`, `openpyxl`, `python-dotenv`, `requests`, `tzdata`) بايثون خالص 100% ولا تحتاج أي تصريف — يفترض تتثبت مباشرة بدون مشاكل.

## كيف تحصل على TELEGRAM_BOT_TOKEN
1. افتح تيليغرام وابحث عن `@BotFather`
2. أرسل `/newbot`
3. اختر اسماً ويوزر للبوت
4. انسخ التوكن الذي يعطيك إياه


## كيف تحصل على ADMIN_TELEGRAM_IDS (رقمك)
1. ابحث عن `@userinfobot` في تيليغرام
2. أرسل له `/start`
3. سيعطيك الـ ID الخاص بك

## ملاحظة مهمة
- ملف `.env` **لا يُرفع على GitHub** (محمي بـ .gitignore)
- ملف `.env.example` هو القالب الآمن للرفع
