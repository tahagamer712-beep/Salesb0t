import re
import os
import json
import logging
from datetime import datetime, date

# ── إعدادات الذكاء الاصطناعي ─────────────────────────────────────────────────
# القيم الافتراضية تجي من Config Vars بالسيرفر (env)، لكن الأدمن يقدر يغيّرها
# مباشرة من لوحة الأدمن بالبوت (تُخزّن بجدول bot_config) بدون إعادة تشغيل.
_ENV_AI_KEY   = os.getenv("OPENROUTER_API_KEY", "")
_ENV_AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-4o-mini")
_AI_URL       = "https://openrouter.ai/api/v1/chat/completions"
_ai_log       = logging.getLogger("salesbot.ai")

# قائمة الموديلات المتاحة للاختيار من لوحة الأدمن (اسم داخلي → اسم عرض)
AVAILABLE_AI_MODELS = {
    "openai/gpt-4o-mini":        "GPT-4o Mini (سريع ورخيص — الافتراضي)",
    "openai/gpt-4o":             "GPT-4o (أدق وأبطأ شوي)",
    "anthropic/claude-3.5-sonnet": "Claude 3.5 Sonnet (دقة عالية)",
    "google/gemini-flash-1.5":   "Gemini Flash 1.5 (سريع جداً)",
    "deepseek/deepseek-chat":    "DeepSeek Chat (رخيص جداً)",
}

_config_cache = {"key": None, "model": None, "ts": 0}
_CONFIG_TTL = 60  # ثانية — نكاش القيم بدل ما نضرب قاعدة البيانات بكل رسالة


def _get_ai_settings() -> tuple[str, str]:
    """يرجع (api_key, model) الحاليين — من قاعدة البيانات لو الأدمن غيّرهم،
    وإلا من Config Vars بالسيرفر. مكاشة لمدة دقيقة لتقليل الضغط على القاعدة."""
    import time
    now = time.time()
    if now - _config_cache["ts"] < _CONFIG_TTL and _config_cache["ts"] > 0:
        return _config_cache["key"], _config_cache["model"]
    try:
        from database import get_bot_config
        key = get_bot_config("openrouter_api_key") or _ENV_AI_KEY
        model = get_bot_config("ai_model") or _ENV_AI_MODEL
    except Exception:
        key, model = _ENV_AI_KEY, _ENV_AI_MODEL
    _config_cache.update(key=key, model=model, ts=now)
    return key, model


def ai_is_enabled() -> bool:
    key, _ = _get_ai_settings()
    return bool(key)


# للتوافق مع أي كود قديم يفحص _AI_ENABLED مباشرة كـ property وقت الاستيراد فقط
_AI_ENABLED = bool(_ENV_AI_KEY)

_ORDER_SYSTEM = """أنت مساعد متخصص في استخراج بيانات أوردرات المبيعات الطبية من رسائل مكتوبة باللهجة العراقية أو الفصحى.
استخرج البيانات كـ JSON صارم (بدون تعليقات أو نص خارجه) بهذه الحقول:
{"rep_name":null,"pharmacy":null,"doctor_name":null,"area":null,"item_name":null,"quantity":null,"bonus":null,"price":null,"payment_type":null,"order_date":null,"notes":null}

قواعد أساسية:
- quantity: رقم صحيح فقط (مثل 25 لا "25 حبة")
- bonus: الرقم بعد + (مثل 10+5 → quantity=10, bonus=5) وإلا null
- order_date: صيغة "YYYY-MM-DD" أو null — التاريخ يُقرأ يوم/شهر، مثلاً 23/5 يعني اليوم 23 شهر 5، و2/5 يعني اليوم 2 شهر 5 (الرقم الأول هو اليوم والثاني هو الشهر دائماً)
- payment_type: "كاش" أو "آجل" أو "كريدت" أو null
- item_name: اسم الدواء بالإنجليزي (أو العربي لو إنجليزي ما موجود)، صحّح الإملاء الواضح (مثلاً "ليفوقران"→"Levogran")
- أي قيمة غير واضحة اتركها null
- لا تضيف أي نص خارج JSON

قواعد مهمة جداً لتمييز الحقول:
- كلمات مثل "re-order", "reorder", "test-order", "ري اوردر", "ريوردر" هي ملاحظات (notes) وليست اسم صيدلية أو دواء
- أي سطر يحتوي كلمة "صيدلية" هو اسم الصيدلية (pharmacy) حتى لو جاء بعد re-order
- الدواء (item_name) هو الاسم الإنجليزي المصحوب برقم مثل "neogram 10+3" أو "levogran 25"
- روابط waze أو google maps أو أي URL هي location وليست pharmacy أو item
- اسم المندوب يكون في بداية الرسالة وغالباً يتبع كلمة "مندوب" أو يكون اسماً عربياً

مثال:
رسالة: "طه محمد\n16/5\nالاورزدي\nre-order\nصيدلية علي خالد\nneogram 10+3\nللدكتورة ايلاف"
النتيجة: {"rep_name":"طه محمد","pharmacy":"صيدلية علي خالد","doctor_name":"ايلاف","area":"الاورزدي","item_name":"neogram","quantity":10,"bonus":3,"payment_type":null,"order_date":"2026-05-16","notes":"re-order"}"""

_MULTI_SYSTEM = """استخرج جميع المنتجات من رسالة أوردر طبي عراقي. أعد JSON array فقط (بدون أي نص خارجه):
[{"item_name":"...","quantity":10,"bonus":5,"note":null},...]
قواعد:
- quantity: رقم صحيح فقط
- bonus: الرقم بعد + أو 0
- item_name: اسم الدواء (صحّح الإملاء الواضح)
- note: أي ملاحظة تخص هذا المنتج تحديداً (مثل "قطعتين بيد المندوب") أو null
- كل سطر دواء له عنصر منفصل في الـ array
- تجاهل: re-order, test-order, روابط waze, اسم المندوب, الصيدلية, المنطقة, التاريخ — هذه ليست منتجات
- "قطعتين/قطعة بيد المندوب" = ملاحظة للمنتج السابق مباشرة وليس منتجاً جديداً"""

_FULL_SYSTEM = """أنت مساعد متخصص في استخراج بيانات أوردرات المبيعات الطبية من رسائل مكتوبة باللهجة العراقية أو الفصحى.
الرسالة قد تحتوي على منتج واحد أو عدة منتجات. استخرج البيانات كـ JSON صارم (بدون تعليقات أو نص خارجه) بهذا الشكل بالضبط:
{"rep_name":null,"pharmacy":null,"doctor_name":null,"area":null,"payment_type":null,"order_date":null,"notes":null,
 "products":[{"item_name":null,"quantity":null,"bonus":null,"price":null,"note":null}]}

قواعد أساسية:
- products: قائمة تحوي كل الأدوية المذكورة، كل دواء له عنصر منفصل في القائمة — حتى لو كان دواءً واحداً فقط ضعه بعنصر واحد داخل القائمة
- quantity: رقم صحيح فقط (مثل 25 لا "25 حبة")
- bonus: الرقم بعد + (مثل 10+5 → quantity=10, bonus=5) وإلا null
- order_date: صيغة "YYYY-MM-DD" أو null — التاريخ يُقرأ يوم/شهر، مثلاً 23/5 يعني اليوم 23 شهر 5 (الرقم الأول هو اليوم والثاني هو الشهر دائماً)
- payment_type: "كاش" أو "آجل" أو "كريدت" أو null
- item_name: اسم الدواء بالإنجليزي (أو العربي لو إنجليزي ما موجود)، صحّح الإملاء الواضح (مثلاً "ليفوقران"→"Levogran")
- note: أي ملاحظة تخص هذا المنتج تحديداً (مثل "قطعتين بيد المندوب") أو null — "قطعتين/قطعة بيد المندوب" تُعتبر ملاحظة للمنتج السابق مباشرة وليست منتجاً جديداً
- أي قيمة غير واضحة اتركها null
- لا تضيف أي نص خارج JSON

قواعد مهمة جداً لتمييز الحقول:
- كلمات مثل "re-order", "reorder", "test-order", "ري اوردر", "ريوردر" هي ملاحظات عامة (notes) وليست اسم صيدلية أو دواء
- أي سطر يحتوي كلمة "صيدلية" هو اسم الصيدلية (pharmacy) حتى لو جاء بعد re-order
- روابط waze أو google maps أو أي URL هي location وليست pharmacy أو item
- اسم المندوب يكون في بداية الرسالة وغالباً يتبع كلمة "مندوب" أو يكون اسماً عربياً

مثال (منتج واحد):
رسالة: "طه محمد\\n16/5\\nالاورزدي\\nre-order\\nصيدلية علي خالد\\nneogram 10+3\\nللدكتورة ايلاف"
النتيجة: {"rep_name":"طه محمد","pharmacy":"صيدلية علي خالد","doctor_name":"ايلاف","area":"الاورزدي","payment_type":null,"order_date":"2026-05-16","notes":"re-order","products":[{"item_name":"neogram","quantity":10,"bonus":3,"price":null,"note":null}]}

مثال (أكثر من منتج):
رسالة: "صيدلية النور\\nneogram 20+5\\nlevogran 15\\nقطعتين بيد المندوب"
النتيجة: {"rep_name":null,"pharmacy":"صيدلية النور","doctor_name":null,"area":null,"payment_type":null,"order_date":null,"notes":null,"products":[{"item_name":"neogram","quantity":20,"bonus":5,"price":null,"note":null},{"item_name":"levogran","quantity":15,"bonus":null,"price":null,"note":"قطعتين بيد المندوب"}]}"""

_INTENT_SYSTEM = """صنّف الرسالة إلى نية واحدة بالضبط من هذه القائمة. أعد الاسم فقط بدون أي شرح:
order, report, edit_order, delete_order, performance, pharmacy_profile,
team_dashboard, compare_months, repeat_order, set_template, show_template,
delete_template, set_rep_name, show_rep_name, set_manager, alerts_on, alerts_off,
toggle_daily, toggle_weekly, smart_search, unknown"""


def _ai_call(system: str, user: str) -> str | None:
    """استدعاء OpenRouter API — يعيد النص أو None لو فشل."""
    key, model = _get_ai_settings()
    if not key:
        return None
    try:
        import requests as _req
        r = _req.post(
            _AI_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user",   "content": user}],
                  "temperature": 0.0,
                  "max_tokens": 500},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        _ai_log.warning("AI call failed: %s", exc)
        return None


_NOTES_PATTERNS = re.compile(
    r'^(re[\-\s/]?order|test[\-\s/]?order|reorder|ري[\s\-]?اوردر|ريوردر|اوردر|use\s+waze|https?://)',
    re.IGNORECASE
)

_URL_PATTERN = re.compile(r'https?://\S+|waze\.com\S*|maps\.google\S*|goo\.gl\S*', re.IGNORECASE)

# الحقول النصية التي يجب تنظيفها من URLs
_TEXT_FIELDS = ("pharmacy", "area", "item_name", "doctor_name", "rep_name", "notes")


def _strip_urls_from_text(val: str) -> str:
    """حذف أي URL من نص معين."""
    return _URL_PATTERN.sub('', val).strip()


def _preprocess_order_text(text: str) -> str:
    """
    تنظيف نص الطلب قبل إرساله للـ AI:
    - حذف الأسطر التي هي فقط URLs أو روابط Waze/Maps
    - حذف الأسطر الفارغة الزائدة
    """
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.strip()
        # تجاهل الأسطر التي هي فقط URL أو بداية بـ "Use Waze"
        if re.match(r'^https?://', stripped, re.IGNORECASE):
            continue
        if re.match(r'^use\s+waze', stripped, re.IGNORECASE):
            continue
        if re.match(r'^https?://', _URL_PATTERN.sub('', stripped).strip(), re.IGNORECASE):
            continue
        # إذا بعد حذف الـ URL ما بقي شيء → تجاهل السطر
        after_url = _URL_PATTERN.sub('', stripped).strip()
        if not after_url and _URL_PATTERN.search(stripped):
            continue
        cleaned.append(line)
    return '\n'.join(cleaned).strip()


def _ai_validate(data: dict) -> dict:
    """تحقق من نتيجة الـ AI وصحّح الأخطاء الواضحة."""

    # ── تنظيف URLs من جميع الحقول النصية ───────────────────────────────────
    for field in _TEXT_FIELDS:
        val = (data.get(field) or "").strip()
        if val and _URL_PATTERN.search(val):
            cleaned = _strip_urls_from_text(val)
            data[field] = cleaned if cleaned else None

    # ── لو الـ pharmacy هو re-order أو رابط → نقله إلى notes واحذفه ─────────
    ph = (data.get("pharmacy") or "").strip()
    if ph and _NOTES_PATTERNS.match(ph):
        existing_notes = data.get("notes") or ""
        data["notes"] = (existing_notes + " | " + ph).strip(" |") if existing_notes else ph
        data["pharmacy"] = None

    # ── لو item_name يحتوي "صيدلية" → هو في الحقيقة pharmacy ────────────────
    item = (data.get("item_name") or "").strip()
    if item and "صيدلية" in item and not data.get("pharmacy"):
        drug_match = re.search(r'[a-zA-Z][a-zA-Z0-9\-]+', item)
        data["pharmacy"] = item
        data["item_name"] = drug_match.group(0) if drug_match else None

    # ── لو item_name فارغ أو يحتوي re-order → null ───────────────────────────
    item2 = (data.get("item_name") or "").strip()
    if item2 and _NOTES_PATTERNS.match(item2):
        data["item_name"] = None

    # ── لو area يحتوي "صيدلية" ───────────────────────────────────────────────
    area = (data.get("area") or "").strip()
    if area and "صيدلية" in area:
        ph_cur = (data.get("pharmacy") or "").strip()
        # لو pharmacy فارغ → انقل area إلى pharmacy مباشرة
        if not ph_cur:
            data["pharmacy"] = area
            data["area"] = None
        # لو pharmacy يبدو اسم دواء (حروف إنجليزية + أرقام) → انقل area→pharmacy وpharmacy→item_name
        elif re.search(r'[a-zA-Z]', ph_cur) and not data.get("item_name"):
            data["item_name"] = ph_cur
            data["pharmacy"] = area
            data["area"] = None

    # ── لو pharmacy يحتوي كمية (مثل "Neatrix 50+25") → حركه لـ item_name ────
    ph2 = (data.get("pharmacy") or "").strip()
    if ph2 and re.search(r'[a-zA-Z].*\d+\s*\+\s*\d+', ph2) and not data.get("item_name"):
        data["item_name"] = ph2
        data["pharmacy"] = None

    return data


def _ai_parse_order(text: str) -> dict | None:
    """يطلب من الـ AI استخراج حقول الطلب. يعيد dict أو None."""
    # نظّف النص من URLs وأسطر Waze قبل الإرسال
    text = _preprocess_order_text(text)
    raw = _ai_call(_ORDER_SYSTEM, text)
    if not raw:
        return None
    # احذف كود blocks لو موجودة
    raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return _ai_validate(data)
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _ai_parse_multi(text: str) -> list | None:
    """يطلب من الـ AI قائمة المنتجات. يعيد list أو None."""
    raw = _ai_call(_MULTI_SYSTEM, text)
    if not raw:
        return None
    raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _ai_parse_full(text: str) -> dict | None:
    """يطلب من الـ AI استخراج بيانات الطلب كاملة (حقول أساسية + قائمة منتجات) بطلب واحد فقط.
    هذا يحل محل الاعتماد على is_multi_product() لتحديد عدد المنتجات — الـ AI نفسه
    يحدد كم منتج بالرسالة، فلا تضيع منتجات إضافية حتى لو صياغة الرسالة غير معتادة."""
    clean_text = _preprocess_order_text(text)
    raw = _ai_call(_FULL_SYSTEM, clean_text)
    if not raw:
        return None
    raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("products"), list):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def parse_order_smart(text: str) -> tuple[dict, list]:
    """
    نقطة الدخول الموحّدة لتحليل رسالة الطلب — تكتشف تلقائياً كم منتج بالرسالة
    (واحد أو أكثر) بطلب AI واحد فقط بدل طلبين منفصلين (أسرع وأدق وما يفوّت منتجات).

    ترجع (base_data, products):
      - base_data: باقي حقول الطلب (rep_name, order_date, month, year, area,
        pharmacy, doctor_name, payment_type, notes, location_link, raw_text)
      - products: قائمة عنصر واحد أو أكثر، كل عنصر فيه item_name/quantity/bonus/price/notes

    يرمي AIParseError فقط لو الـ AI غير مفعّل/فشل ولم نستطع استخراج أي منتج حتى بالـ regex.
    """
    today = date.today()
    ai_data = _ai_parse_full(text)

    if ai_data:
        order_date_str = ai_data.get("order_date")
        if order_date_str:
            try:
                d_obj = date.fromisoformat(order_date_str)
                month, year = d_obj.month, d_obj.year
            except ValueError:
                d_obj, month, year = None, today.month, today.year
        else:
            d_obj, month, year = parse_date(text)

        base_data = _ai_validate({
            "rep_name":    ai_data.get("rep_name"),
            "pharmacy":    ai_data.get("pharmacy"),
            "doctor_name": ai_data.get("doctor_name"),
            "area":        ai_data.get("area"),
            "payment_type": ai_data.get("payment_type"),
            "notes":       ai_data.get("notes"),
        })
        base_data["order_date"]    = (d_obj or today).isoformat()
        base_data["month"]         = month
        base_data["year"]          = year
        base_data["location_link"] = None
        base_data["raw_text"]      = text

        products = []
        for p in (ai_data.get("products") or []):
            item_name = (p.get("item_name") or "").strip()
            if not item_name:
                continue
            try:
                qty_val = int(p.get("quantity")) if p.get("quantity") is not None else None
            except (ValueError, TypeError):
                qty_val = None
            try:
                bonus_raw = p.get("bonus") or 0
                bonus_val = str(int(bonus_raw)) if bonus_raw else None
            except (ValueError, TypeError):
                bonus_val = None
            try:
                price_val = float(p["price"]) if p.get("price") is not None else None
            except (ValueError, TypeError):
                price_val = None
            products.append({
                "item_name": item_name,
                "quantity":  qty_val,
                "bonus":     bonus_val,
                "price":     price_val,
                "notes":     (p.get("note") or None),
            })

        if products:
            return base_data, products
        # الـ AI رجع بدون أي منتج صالح → انتقل لنظام الـ regex الاحتياطي بالأسفل

    # ── لا يوجد AI مفعّل أو فشل تماماً → نظام regex احتياطي (بدون فقدان منتجات) ──
    fb_base, fb_products = parse_multi_product_order(text)
    for p in fb_products:
        p["notes"] = p.pop("_note", None)
    if fb_products:
        return fb_base, fb_products

    if not ai_is_enabled():
        raise AIParseError(
            "⚠️ *الذكاء الاصطناعي غير مفعّل.*\n"
            "أضف مفتاح API من لوحة الأدمن (🔑 مفاتيح API) أو عبر `OPENROUTER_API_KEY` بالسيرفر."
        )
    raise AIParseError(
        "❌ *فشل الذكاء الاصطناعي في تحليل الطلب.*\n"
        "حاول مرة أخرى أو أعد صياغة الطلب."
    )


def _ai_detect_intent(text: str) -> str | None:
    """يطلب من الـ AI تصنيف نية الرسالة. يعيد intent string أو None."""
    raw = _ai_call(_INTENT_SYSTEM, text)
    if not raw:
        return None
    # خذ أول كلمة فقط
    token = raw.strip().split()[0].strip(".,").lower()
    valid = {
        "order", "report", "edit_order", "delete_order", "performance",
        "pharmacy_profile", "team_dashboard", "compare_months", "repeat_order",
        "set_template", "show_template", "delete_template", "set_rep_name",
        "show_rep_name", "set_manager", "alerts_on", "alerts_off",
        "toggle_daily", "toggle_weekly", "smart_search", "unknown",
    }
    return token if token in valid else None

MONTH_AR = {
    "يناير": 1, "جانوري": 1, "كانون الثاني": 1,
    "فبراير": 2, "فيفري": 2, "شباط": 2,
    "مارس": 3, "آذار": 3, "اذار": 3,
    "ابريل": 4, "أبريل": 4, "نيسان": 4,
    "مايو": 5, "ايار": 5, "أيار": 5,
    "يونيو": 6, "حزيران": 6,
    "يوليو": 7, "تموز": 7,
    "اغسطس": 8, "أغسطس": 8, "آب": 8,
    "سبتمبر": 9, "ايلول": 9, "أيلول": 9,
    "اكتوبر": 10, "أكتوبر": 10, "تشرين الأول": 10, "تشرين الاول": 10,
    "نوفمبر": 11, "تشرين الثاني": 11,
    "ديسمبر": 12, "كانون الأول": 12, "كانون الاول": 12,
}

MONTH_NUM_TO_AR = {v: k for k, v in MONTH_AR.items() if len(k) <= 6}

PAYMENT_KEYWORDS = {
    "كاش": "كاش", "نقد": "كاش", "نقدي": "كاش",
    "كاشه": "كاش", "نقده": "كاش",
    "كرديت": "كريدت", "كريدت": "كريدت", "credit": "كريدت",
    "دين": "آجل", "اجل": "آجل", "آجل": "آجل", "مؤجل": "آجل",
    "ناسي": "آجل", "نسيه": "آجل", "نسية": "آجل", "ذمة": "آجل",
}

# ── عبارات اللهجة العراقية للتقارير ─────────────────────────────────────────
REPORT_TRIGGERS = [
    # فصحى
    "تقرير", "ريبورت", "report", "احصاء", "إحصاء",
    "مبيعات", "عرض", "اعرض", "أعرض",
    "صيدليات", "مناطق", "منتجات",
    "أفضل", "افضل", "top", "summary",
    "اليوم", "امس", "أمس", "هذا الاسبوع",
    "من تاريخ", "الى تاريخ", "حتى تاريخ",
    # عراقي
    "وريني", "ورني", "أوريني", "اوريني",
    "شگد", "شقد", "كم بعنا", "كم طلبنا",
    "شنو باعنا", "شنو بعنا",
    "الشهر", "هذا الشهر", "الشهر الحالي", "الشهر الماضي",
    "باچر", "باكر", "اريد تقرير", "أريد تقرير",
    "اريد احصاء", "كشف", "جيب تقرير", "طلعلي",
    "طلع لي", "عطني", "اعطني", "خبرني",
    "شوف مبيعاتي", "شوف طلباتي", "شلون مبيعاتي",
    "گبل شهر", "قبل شهر", "هالشهر",
]

PHARMACY_PROFILE_TRIGGERS = [
    "ملف صيدلية", "سجل صيدلية", "معلومات صيدلية",
    "احصائيات صيدلية", "تفاصيل صيدلية",
    "شنو عندنا ب", "شكو من صيدلية", "وين وصلنا",
    "شنو باعنا ل", "شلون صيدلية", "تاريخ صيدلية",
]

SEARCH_TRIGGERS = [
    "أوردرات", "اوردرات", "بحث", "بحث عن", "اعرض كل",
    "كم طلبنا", "كمان طلبنا", "شوف", "كم مرة",
    "دور على", "فتش", "ابحث",
]

PERFORMANCE_TRIGGERS = [
    "ترتيبي", "أدائي", "ادائي", "احصائياتي",
    "كيف أدائي", "كيف ادائي", "مقارنة أدائي",
    "ترتيب المندوبين", "الترتيب", "احصاء ادائي",
    "ليدربورد", "leaderboard", "ranking",
    "ايش مسويت", "شنو حققت", "نجومي", "نقاطي",
    "شلون شغلي", "شلون ادائي", "وين وصلت",
]

ALERT_TRIGGERS = {
    "on":  ["فعل التنبيهات", "وقف التنبيه لا", "شغل التنبيهات", "فعل الانذار",
            "فعل التنبيه", "شغل التنبيه"],
    "off": ["وقف التنبيهات", "ايقاف التنبيهات", "بلا تنبيهات", "وقف الانذارات",
            "وقف التنبيه", "سكت التنبيه"],
}

REPEAT_TRIGGERS = [
    "كرر آخر", "كرر اخر", "نفس آخر", "نفس اخر",
    "نفس أوردر", "نفس اوردر", "كرر زيارة", "كرر زيارتي",
    "نفس الأسبوع الماضي", "نفس اسبوع", "كرر الأسبوع",
    "اعد آخر", "اعد اخر",
]

TEAM_TRIGGERS = [
    "فريقي", "مندوبيني", "فريق المندوبين", "لوحة المدير",
    "احصائيات الفريق", "تقرير الفريق",
    "وريني المندوبين", "شوف المندوبين",
]

TEMPLATE_TRIGGERS = [
    "ترتيبي", "قالبي", "فورمة", "فورماتي",
    "راح اكتب", "راح ارسل", "طريقتي", "شكل الاوردر",
    "شكل الطلب", "template", "format",
    # عراقي
    "طريقة اكتب", "طريقة طلبي", "شلون اكتب",
    "حط ترتيب", "عيّن ترتيب", "خلي الترتيب",
    "ابي الترتيب", "أبي الترتيب", "ابي القالب",
]

FIELD_KEYWORDS = {
    "rep_name":     ["مندوب", "اسم المندوب", "اسمي", "الاسم", "اسم", "rep"],
    "pharmacy":     ["صيدلية", "الصيدلية", "pharmacy", "صيدلياتي", "عند"],
    "doctor_name":  ["طبيب", "الطبيب", "دكتور", "doctor", "د.", "dr",
                     "دكتوره", "الدكتور", "حكيم"],
    "item_name":    ["علاج", "دواء", "منتج", "صنف", "العلاج", "الدواء",
                     "المنتج", "medicine", "item", "حبوب", "امبول",
                     "شريط", "علبة", "كبسول", "قرص", "صنف"],
    "quantity":     ["كمية", "عدد", "الكمية", "quantity", "كم حبة",
                     "عدد الحبوب", "چد"],
    "price":        ["سعر", "السعر", "price", "بكم", "بچد", "فلوس"],
    "bonus":        ["بونص", "bonus", "زيادة", "اضافي", "بونس", "هدية"],
    "payment_type": ["دفع", "نوع الدفع", "كاش", "آجل", "اجل", "كريدت",
                     "payment", "نسيه", "ذمة", "ناسي"],
    "area":         ["منطقة", "المنطقة", "محلة", "حي", "area",
                     "ناحية", "قضاء", "مدينة", "محافظة"],
    "order_date":   ["تاريخ", "date", "يوم", "التاريخ"],
    "notes":        ["ملاحظات", "نوت", "notes", "ملاحظة", "نوتة"],
}

EDIT_FIELD_MAP = {
    "الاسم": "rep_name", "المندوب": "rep_name", "اسم المندوب": "rep_name",
    "الصيدلية": "pharmacy", "صيدلية": "pharmacy",
    "الطبيب": "doctor_name", "طبيب": "doctor_name", "دكتور": "doctor_name", "اسم الطبيب": "doctor_name",
    "العلاج": "item_name", "الدواء": "item_name", "المنتج": "item_name", "الصنف": "item_name",
    "الكمية": "quantity", "العدد": "quantity", "كمية": "quantity",
    "السعر": "price", "سعر": "price",
    "البونص": "bonus", "بونص": "bonus",
    "الدفع": "payment_type", "نوع الدفع": "payment_type",
    "المنطقة": "area", "منطقة": "area",
    "التاريخ": "order_date", "تاريخ": "order_date",
    "الملاحظات": "notes", "ملاحظات": "notes",
}

# ── كلمات مفتاحية لاكتشاف التقرير من اللهجة العراقية ─────────────────────
IRAQI_REPORT_PHRASES = [
    r"وريني\s*(تقرير|مبيعات|طلبات|احصاء)?",
    r"ورني\s*(تقرير|مبيعات|طلبات|احصاء)?",
    r"شگد\s*(باعنا|بعنا|طلبنا|بعت|باعت)?",
    r"شقد\s*(باعنا|بعنا|طلبنا)?",
    r"اريد\s*(تقرير|احصاء|مبيعات|شوف)",
    r"أريد\s*(تقرير|احصاء|مبيعات|شوف)",
    r"جيب\s*(تقرير|مبيعات|احصاء)",
    r"طلع\s*لي\s*(تقرير|مبيعات)?",
    r"كم\s*(باعنا|بعنا|طلبنا|بيعة)",
    r"شلون\s*(مبيعاتي|شغلي|ادائي)",
    r"(هالشهر|هذا\s*الشهر)\s*(شگد|كم|شنو)?",
    r"(اليوم|امس|أمس)\s*(شگد|كم|شنو|وريني)?",
]


def _is_iraqi_report(text: str) -> bool:
    t = text.lower().strip()
    for pattern in IRAQI_REPORT_PHRASES:
        if re.search(pattern, t):
            return True
    return False


def detect_intent(text: str) -> str:
    t = text.lower().strip()

    # ── تعديل / حذف ─────────────────────────────────────────────────────────
    if re.match(r'^(عدل|تعديل|غير|صحح)\s', t) or "عدل آخر" in t or "غير آخر" in t:
        return "edit_order"

    if re.match(r'^(احذف|امسح|حذف|مسح)\s*(آخر|اخر)?\s*(طلب|اوردر|order)?$', t):
        return "delete_order"

    # ── مقارنة شهرين ────────────────────────────────────────────────────────
    if ("قارن" in t or "مقارنه" in t or "مقارنة" in t) and "أداء" not in t and "ادائي" not in t:
        return "compare_months"

    # ── اسم المندوب ─────────────────────────────────────────────────────────
    if any(t == x for x in ["اسمي", "شوف اسمي", "اسم المندوب"]):
        return "show_rep_name"

    if re.match(r'^(اسمي|انا)\s+\S', t):
        return "set_rep_name"

    # ── تنبيهات ─────────────────────────────────────────────────────────────
    for kw in ALERT_TRIGGERS["on"]:
        if kw in t:
            return "alerts_on"
    for kw in ALERT_TRIGGERS["off"]:
        if kw in t:
            return "alerts_off"

    # ── تقارير دورية ────────────────────────────────────────────────────────
    if any(x in t for x in ["ملخص يومي", "تقرير يومي", "فعل اليومي", "ايقاف اليومي"]):
        return "toggle_daily"

    if any(x in t for x in ["تقرير اسبوعي", "ملخص اسبوعي", "فعل الاسبوعي", "ايقاف الاسبوعي"]):
        return "toggle_weekly"

    if any(x in t for x in ["مديري", "مشرفي", "ارسل للمدير", "manager"]):
        return "set_manager"

    # ── قالب ────────────────────────────────────────────────────────────────
    if t in ("حذف القالب", "امسح القالب", "مسح القالب", "reset template", "بدون قالب"):
        return "delete_template"

    if t in ("قالبي", "شكل طلبي", "شكل ترتيبي", "اشوف قالبي", "ايش ترتيبي",
             "شنو قالبي", "وريني قالبي", "شلون ترتيبي"):
        return "show_template"

    for trigger in TEMPLATE_TRIGGERS:
        if trigger in t:
            return "set_template"

    # ── تكرار أوردر ─────────────────────────────────────────────────────────
    for trigger in REPEAT_TRIGGERS:
        if trigger in t:
            return "repeat_order"

    # ── لوحة المدير ─────────────────────────────────────────────────────────
    for trigger in TEAM_TRIGGERS:
        if trigger in t:
            return "team_dashboard"

    # ── أداء وترتيب ─────────────────────────────────────────────────────────
    for trigger in PERFORMANCE_TRIGGERS:
        if trigger in t:
            return "performance"

    # ── ملف صيدلية ──────────────────────────────────────────────────────────
    for trigger in PHARMACY_PROFILE_TRIGGERS:
        if trigger in t:
            return "pharmacy_profile"

    # ── بحث ذكي ─────────────────────────────────────────────────────────────
    for trigger in SEARCH_TRIGGERS:
        if trigger in t:
            return "smart_search"

    # ── كشف إشارات الطلب القوية أولاً (قبل أي فحص تقرير) ──────────────────
    forwarded_order_signals = [
        bool(re.search(r'مندوب\s+\S', t)),
        bool(re.search(r'للطبيب|للدكتور|الطبيب|دكتور\s+\S', t)),
        bool(re.search(r'https?://\S+', t)),
        bool(re.search(r'صيدلية\s*\S', t)),
        bool(re.search(r'\d{1,2}[/\-\.]\d{1,2}', t)),
        bool(re.search(r'[a-zA-Z]{3,}\s*\d+\s*[+]\s*\d+', t)),
    ]
    if sum(forwarded_order_signals) >= 2:
        return "order"

    # رسالة متعددة الأسطر فيها اسم دواء (إنجليزي) أو تاريخ أو دفع → طلب
    lines_nonempty = [l for l in text.split("\n") if l.strip()]
    if len(lines_nonempty) >= 3:
        has_date    = bool(re.search(r'\d{1,2}[/\-\.]\d{1,2}', text))
        has_pharmacy= "صيدلية" in t
        has_drug    = bool(re.search(r'[a-zA-Z]{3,}', text))
        has_pay     = any(kw in t for kw in ["كاش", "آجل", "اجل", "كريدت", "نسيه"])
        if has_date or has_pharmacy or has_drug or has_pay:
            return "order"

    if re.search(r'\d', text) and any(
        kw in t for kw in ["صيدلية", "pharmacy", "دواء", "علاج", "مندوب", "منطقة",
                           "كاش", "كريدت", "آجل", "نسيه", "ذمة"]
    ):
        return "order"

    # ── تقرير: فحص اللهجة العراقية أولاً ────────────────────────────────────
    if _is_iraqi_report(t):
        return "report"

    # ── تقرير: الكلمات المعيارية (فقط عبارات كاملة — لا كلمات منفردة عامة) ──
    SAFE_REPORT_TRIGGERS = [
        "تقرير", "ريبورت", "report", "احصاء", "إحصاء",
        "مبيعات", "عرض", "اعرض", "أعرض",
        "صيدليات", "مناطق", "منتجات",
        "أفضل", "افضل", "top", "summary",
        "من تاريخ", "الى تاريخ", "حتى تاريخ",
        "وريني", "ورني", "أوريني", "اوريني",
        "شگد", "شقد", "كم بعنا", "كم طلبنا",
        "شنو باعنا", "شنو بعنا",
        "الشهر", "هذا الشهر", "الشهر الحالي", "الشهر الماضي",
        "باچر", "باكر", "اريد تقرير", "أريد تقرير",
        "اريد احصاء", "كشف", "جيب تقرير", "طلعلي",
        "طلع لي", "عطني", "اعطني", "خبرني",
        "شوف مبيعاتي", "شوف طلباتي", "شلون مبيعاتي",
        "گبل شهر", "قبل شهر", "هالشهر",
        "هذا الاسبوع", "امس", "أمس",
    ]
    for trigger in SAFE_REPORT_TRIGGERS:
        if trigger in t:
            return "report"

    # "اليوم" كلمة منفردة فقط (مو كجزء من جملة طلب)
    if t.strip() in ("اليوم", "تقرير اليوم", "مبيعات اليوم", "طلبات اليوم"):
        return "report"

    # ── fallback: اسأل الـ AI لو الـ regex ما عرف يصنّف ────────────────────
    ai_intent = _ai_detect_intent(text)
    if ai_intent and ai_intent != "unknown":
        return ai_intent

    # ── Doctor profile intent ──────────────────────────────────────────────
    for trigger in DOCTOR_PROFILE_TRIGGERS:
        if trigger in t:
            return "doctor_profile"
    return "unknown"


def _is_medicine_line(line: str) -> bool:
    """سطر دواء: لازم يحتوي حروف إنجليزية + رقم، وما يكون رابط أو Waze."""
    s = line.strip()
    if re.search(r'https?://', s):
        return False
    if re.match(r'use\s+waze', s, re.IGNORECASE):
        return False
    if re.match(r'^(re[\-\s]?order|test[\-\s]?order)', s, re.IGNORECASE):
        return False
    return bool(re.search(r'[a-zA-Z]{2,}', s) and re.search(r'\d', s))


def is_multi_product(text: str) -> bool:
    """طلب متعدد: فيه سطرين أو أكثر من أسماء أدوية (حروف إنجليزية + رقم)."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    med_lines = [l for l in lines if _is_medicine_line(l)]
    return len(med_lines) >= 2


def _regex_parse_base_data(text: str) -> dict:
    """استخراج احتياطي بالـ regex فقط للحقول الأساسية (صيدلية/طبيب/تاريخ/ملاحظات) —
    يُستخدم فقط عندما الـ AI معطّل أو فشل تماماً، حتى ما يتوقف تسجيل الطلبات كلياً
    (بدل ما كان سابقاً يرمي خطأ فوراً بدون أي محاولة بديلة)."""
    today = date.today()
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    pharmacy, doctor_name, rep_name = None, None, None
    notes_parts = []

    for line in lines:
        if _NOTES_PATTERNS.match(line):
            notes_parts.append(line)
            continue
        if "صيدلية" in line and not pharmacy:
            pharmacy = re.sub(r'^(صيدلية|الصيدلية)\s*', '', line, flags=re.IGNORECASE).strip() or line
            continue
        if re.search(r'(دكتور|دكتورة|د\.|طبيب)', line) and not doctor_name:
            doctor_name = re.sub(r'.*?(دكتور|دكتورة|د\.|طبيب)\s*', '', line).strip() or None

    if lines:
        first = lines[0]
        if re.fullmatch(r'[\u0600-\u06FF\s]{4,30}', first) and not _is_medicine_line(first):
            rep_name = first

    d_obj, month, year = parse_date(text)

    return {
        "rep_name":      rep_name,
        "order_date":    (d_obj or today).isoformat(),
        "month":         month,
        "year":          year,
        "area":          None,
        "pharmacy":      pharmacy,
        "doctor_name":   doctor_name,
        "payment_type":  None,
        "notes":         " | ".join(notes_parts) if notes_parts else None,
        "location_link": None,
        "raw_text":      text,
    }


def parse_multi_product_order(text: str) -> tuple:
    try:
        base_data = parse_order(text)
    except AIParseError:
        # الـ AI غير متاح تماماً → لا نوقف تسجيل الطلب، نكمل بالـ regex الاحتياطي
        base_data = _regex_parse_base_data(text)
    products = []

    # ── حاول الـ AI أولاً لاستخراج قائمة المنتجات ───────────────────────────
    ai_products = _ai_parse_multi(text)
    if ai_products:
        for p in ai_products:
            item_name = p.get("item_name") or ""
            if not item_name:
                continue
            try:
                qty = int(p.get("quantity") or 0) or None
            except (ValueError, TypeError):
                qty = None
            try:
                bonus_raw = p.get("bonus") or 0
                bonus_val = str(int(bonus_raw)) if bonus_raw else None
            except (ValueError, TypeError):
                bonus_val = None
            note_val = p.get("note") or None
            products.append({
                "item_name": item_name,
                "quantity":  qty,
                "bonus":     bonus_val,
                "_note":     note_val,
            })

    # ── fallback: regex ───────────────────────────────────────────────────────
    if not products:
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        for line in lines:
            if not _is_medicine_line(line):
                continue
            qty, bonus = parse_quantity_bonus(line)
            if not qty:
                continue
            item_m = re.search(r'[a-zA-Z][a-zA-Z0-9\-]+', line)
            item_name = item_m.group(0) if item_m else re.sub(r'\d+.*', '', line).strip()
            if not item_name:
                continue
            arabic_note = re.sub(r'[a-zA-Z0-9\+\-×x\s,،\.]+', '', line).strip()
            products.append({
                "item_name": item_name,
                "quantity":  qty,
                "bonus":     str(bonus) if bonus else None,
                "_note":     arabic_note if len(arabic_note) > 1 else None,
            })

    if not products and base_data.get("item_name"):
        products.append({
            "item_name": base_data["item_name"],
            "quantity":  base_data.get("quantity"),
            "bonus":     base_data.get("bonus"),
        })

    return base_data, products


def parse_repeat_scope(text: str) -> str:
    t = text.lower()
    if "أسبوع" in t or "اسبوع" in t or "الأسبوع" in t:
        return "last_week"
    if "زيارة" in t or "زياره" in t:
        return "last_visit"
    return "last_order"


def parse_pharmacy_name(text: str) -> str:
    t = text.strip()
    for prefix in PHARMACY_PROFILE_TRIGGERS:
        if prefix in t:
            t = t.replace(prefix, "").strip()
    t = re.sub(r'^(صيدلية|الصيدلية)\s*', '', t, flags=re.IGNORECASE).strip()
    return t or text.strip()


def parse_smart_search(text: str) -> dict:
    t = text.strip()
    for trigger in SEARCH_TRIGGERS:
        t = t.replace(trigger, "").strip()
    return {"keyword": t if t else text.strip()}

def parse_edit_instruction(text: str) -> dict:
    result = {"field": None, "value": None}
    t = text.strip()
    t = re.sub(r'^(عدل|تعديل|غير|صحح)\s*(آخر\s*)?(طلب|اوردر|order)?\s*', '', t, flags=re.IGNORECASE).strip()

    for label, field in sorted(EDIT_FIELD_MAP.items(), key=lambda x: -len(x[0])):
        if t.startswith(label):
            result["field"] = field
            val = t[len(label):].strip().lstrip("=:إلى الى ").strip()
            result["value"] = val
            break

    if not result["field"]:
        for label, field in sorted(EDIT_FIELD_MAP.items(), key=lambda x: -len(x[0])):
            if label in t:
                result["field"] = field
                parts = re.split(rf'{re.escape(label)}', t, maxsplit=1)
                if len(parts) > 1:
                    result["value"] = parts[1].strip().lstrip("=:إلى الى ").strip()
                break

    return result


def parse_compare_months(text: str) -> tuple:
    months_found = []
    for ar_month, num in MONTH_AR.items():
        if ar_month in text:
            months_found.append(num)
    nums = re.findall(r'\b(\d{1,2})\b', text)
    for n in nums:
        n = int(n)
        if 1 <= n <= 12 and n not in months_found:
            months_found.append(n)
    if len(months_found) >= 2:
        return months_found[0], months_found[1]
    today = date.today()
    if len(months_found) == 1:
        m2 = months_found[0] - 1 or 12
        return months_found[0], m2
    return today.month, (today.month - 1 or 12)


def parse_date(text: str):
    patterns = [
        r'(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})',
        r'(\d{1,2})[/\-\.](\d{1,2})',
    ]
    today = date.today()
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            groups = m.groups()
            try:
                if len(groups) == 3:
                    d, mo, y = int(groups[0]), int(groups[1]), int(groups[2])
                    if y < 100:
                        y += 2000
                    return date(y, mo, d), mo, y
                else:
                    d, mo = int(groups[0]), int(groups[1])
                    return date(today.year, mo, d), mo, today.year
            except ValueError:
                pass
    for ar_month, num in MONTH_AR.items():
        if ar_month in text:
            yr_match = re.search(r'20\d{2}', text)
            yr = int(yr_match.group()) if yr_match else today.year
            return date(yr, num, 1), num, yr
    return None, today.month, today.year


def parse_quantity_bonus(text: str):
    m = re.search(r'(\d+)\s*\+\s*(\d+)', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'(\d+)\s*(حبة|قرص|علبة|كرتون|أمبول|امبول|شريط|قطعة|كبسول)?', text)
    if m:
        return int(m.group(1)), 0
    return None, 0


def parse_payment(text: str):
    text_lower = text.lower()
    for kw, val in PAYMENT_KEYWORDS.items():
        if kw in text_lower:
            return val
    return None


class AIParseError(Exception):
    """يُرمى لما الـ AI غير متاح أو فشل في تحليل الطلب."""
    pass


def parse_order(text: str) -> dict:
    """محلل الطلبات — يعتمد على الـ AI فقط."""
    today = date.today()

    # ── حاول الـ AI أولاً ────────────────────────────────────────────────────
    ai_data = _ai_parse_order(text)
    if ai_data:
        # أكمل الحقول المحسوبة التي الـ AI لا يعرفها
        order_date_str = ai_data.get("order_date")
        if order_date_str:
            try:
                d_obj = date.fromisoformat(order_date_str)
                month, year = d_obj.month, d_obj.year
            except ValueError:
                d_obj, month, year = None, today.month, today.year
        else:
            # ابحث عن التاريخ بالـ regex من النص الخام
            d_obj, month, year = parse_date(text)

        # bonus: لو الـ AI أعاد رقم حوّله لـ str
        bonus_raw = ai_data.get("bonus")
        bonus_val = str(int(bonus_raw)) if bonus_raw else None

        # price: تأكد أنه float
        price_raw = ai_data.get("price")
        try:
            price_val = float(price_raw) if price_raw is not None else None
        except (ValueError, TypeError):
            price_val = None

        # quantity: تأكد أنه int
        qty_raw = ai_data.get("quantity")
        try:
            qty_val = int(qty_raw) if qty_raw is not None else None
        except (ValueError, TypeError):
            qty_val = None

        return {
            "rep_name":      ai_data.get("rep_name"),
            "order_date":    (d_obj or today).isoformat(),
            "month":         month,
            "year":          year,
            "area":          ai_data.get("area"),
            "pharmacy":      ai_data.get("pharmacy"),
            "doctor_name":   ai_data.get("doctor_name"),
            "item_name":     ai_data.get("item_name"),
            "quantity":      qty_val,
            "bonus":         bonus_val,
            "price":         price_val,
            "payment_type":  ai_data.get("payment_type"),
            "notes":         ai_data.get("notes"),
            "location_link": None,
            "raw_text":      text,
        }

    # الـ AI غير متاح أو فشل → أبلغ البوت ليُظهر رسالة خطأ للمستخدم
    if not ai_is_enabled():
        raise AIParseError(
            "⚠️ *الذكاء الاصطناعي غير مفعّل.*\n"
            "أضف مفتاح API من لوحة الأدمن (🔑 مفاتيح API) أو عبر `OPENROUTER_API_KEY` بالسيرفر."
        )
    raise AIParseError(
        "❌ *فشل الذكاء الاصطناعي في تحليل الطلب.*\n"
        "حاول مرة أخرى أو أعد صياغة الطلب."
    )


def parse_order_with_template(text: str, field_order: list) -> dict:
    today = date.today()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    data = {
        "rep_name": None, "order_date": today.isoformat(), "month": today.month,
        "year": today.year, "area": None, "pharmacy": None, "doctor_name": None,
        "item_name": None, "quantity": None, "bonus": 0,
        "price": None, "payment_type": None, "notes": None,
        "location_link": None, "raw_text": text,
    }
    for i, field in enumerate(field_order):
        if i >= len(lines):
            break
        value = lines[i].strip()
        if not value or field.startswith("?:"):
            continue
        if field == "rep_name":
            data["rep_name"] = value
        elif field == "pharmacy":
            data["pharmacy"] = value
        elif field == "item_name":
            qty, bonus = parse_quantity_bonus(value)
            data["quantity"] = qty
            data["bonus"] = bonus
            clean = re.sub(r'\d+\s*[\+x×]\s*\d+|\d+', '', value).strip()
            data["item_name"] = clean or value
        elif field == "quantity":
            qty, bonus = parse_quantity_bonus(value)
            data["quantity"] = qty
            data["bonus"] = bonus
        elif field == "price":
            m = re.search(r'[\d\.]+', value.replace(',', ''))
            if m:
                try:
                    data["price"] = float(m.group())
                except ValueError:
                    pass
        elif field == "bonus":
            m = re.search(r'\d+', value)
            if m:
                data["bonus"] = int(m.group())
        elif field == "payment_type":
            data["payment_type"] = parse_payment(value) or value
        elif field == "doctor_name":
            data["doctor_name"] = value
        elif field == "area":
            data["area"] = value
        elif field == "order_date":
            d, mo, yr = parse_date(value)
            if d:
                data["order_date"] = d.isoformat()
                data["month"] = mo
                data["year"] = yr
        elif field == "notes":
            data["notes"] = value

    if not data.get("order_date"):
        d, mo, yr = parse_date(text)
        if d:
            data["order_date"] = d.isoformat()
            data["month"] = mo
            data["year"] = yr

    if not data["payment_type"]:
        data["payment_type"] = parse_payment(text)

    return data


def parse_template_definition(text: str) -> list:
    """
    يفهم القالب بأشكال متعددة:
    - "الترتيب: اسم المندوب / صيدلية / علاج / سعر / آجل"
    - "ترتيبي صيدلية ثم علاج ثم كاش"
    - "خلي الترتيب: 1. صيدلية 2. علاج 3. كمية"
    - "اريد اكتب صيدلية، دكتور، علاج، كمية، سعر"
    """
    cleaned = re.sub(
        r'(?i)(الترتيب|ترتيبي|قالبي|فورمة|فورماتي|راح اكتب|راح ارسل|طريقتي|'
        r'شكل الاوردر|شكل الطلب|template|format|طريقة اكتب|طريقة طلبي|'
        r'شلون اكتب|حط ترتيب|عيّن ترتيب|خلي الترتيب|ابي الترتيب|أبي الترتيب|'
        r'ابي القالب|حط لي قالب|عيّن قالب|اريد الترتيب|أريد الترتيب)[:\s]*',
        '', text
    )
    # حذف أرقام الترتيب مثل "1." أو "١."
    cleaned = re.sub(r'[\d١٢٣٤٥٦٧٨٩٠]+\s*[.\-\)]\s*', ' ', cleaned)
    # فصل بكلمة "ثم" أو "بعدين" أو "وبعد"
    cleaned = re.sub(r'\s+(ثم|بعدين|وبعد|وبعدها|ثم بعد)\s+', '/', cleaned)

    parts = [p.strip() for p in re.split(r'[،,/\n\-\|]+', cleaned) if p.strip()]
    field_order = []
    for part in parts:
        part_lower = part.lower()
        matched = False
        for field, keywords in FIELD_KEYWORDS.items():
            if any(kw in part_lower for kw in keywords):
                if field not in field_order:
                    field_order.append(field)
                matched = True
                break
        if not matched and part:
            field_order.append(f"?:{part}")
    return field_order


def format_template_display(field_order: list) -> str:
    labels = {
        "rep_name": "اسم المندوب", "pharmacy": "الصيدلية",
        "doctor_name": "اسم الطبيب", "item_name": "العلاج/المنتج",
        "quantity": "الكمية", "price": "السعر", "bonus": "البونص",
        "payment_type": "نوع الدفع", "area": "المنطقة",
        "order_date": "التاريخ", "notes": "ملاحظات",
    }
    return "\n".join(
        f"{i}. {f[2:]} (غير معروف)" if f.startswith("?:") else f"{i}. {labels.get(f, f)}"
        for i, f in enumerate(field_order, 1)
    )


def parse_report_filters(text: str) -> dict:
    """
    يفهم طلبات التقارير بالفصحى والعراقي:
    - "وريني تقرير اليوم"
    - "شگد باعنا هالشهر"
    - "اريد تقرير مايو"
    - "تقرير الشهر الماضي"
    - "مبيعات من 1/5 الى 15/5"
    """
    filters = {}
    today = date.today()
    t = text.lower().strip()

    # ── تحديد الفترة الزمنية ────────────────────────────────────────────────
    if any(x in t for x in ["هذا الشهر", "الشهر الحالي", "هالشهر", "الشهر"]) \
            and not any(x in t for x in ["الشهر الماضي", "گبل شهر", "قبل شهر"]):
        filters["month"] = today.month
        filters["year"] = today.year

    elif any(x in t for x in ["الشهر الماضي", "گبل شهر", "قبل شهر", "الشهر اللي فات"]):
        m = today.month - 1 or 12
        y = today.year if today.month > 1 else today.year - 1
        filters["month"] = m
        filters["year"] = y

    elif any(x in t for x in ["اليوم", "هذا اليوم", "يوم اليوم"]):
        filters["date_from"] = today.isoformat()
        filters["date_to"] = today.isoformat()

    elif any(x in t for x in ["امس", "أمس", "البارح", "البارحة"]):
        from datetime import timedelta
        yesterday = (today - timedelta(days=1)).isoformat()
        filters["date_from"] = yesterday
        filters["date_to"] = yesterday

    elif any(x in t for x in ["هذا الاسبوع", "الاسبوع", "هالاسبوع"]) \
            and "الماضي" not in t and "اللي فات" not in t:
        from datetime import timedelta
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        filters["date_from"] = week_start
        filters["date_to"] = today.isoformat()

    else:
        # ابحث عن اسم شهر عربي
        for ar_month, num in MONTH_AR.items():
            if ar_month in t:
                filters["month"] = num
                yr_m = re.search(r'20\d{2}', text)
                filters["year"] = int(yr_m.group()) if yr_m else today.year
                break

    # ── نطاق تاريخ محدد ─────────────────────────────────────────────────────
    date_range = re.findall(r'(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?', text)
    if len(date_range) >= 2:
        d1, m1, y1 = date_range[0]
        d2, m2, y2 = date_range[1]
        y1 = int(y1) + 2000 if y1 and int(y1) < 100 else (int(y1) if y1 else today.year)
        y2 = int(y2) + 2000 if y2 and int(y2) < 100 else (int(y2) if y2 else today.year)
        try:
            filters["date_from"] = date(y1, int(m1), int(d1)).isoformat()
            filters["date_to"] = date(y2, int(m2), int(d2)).isoformat()
        except ValueError:
            pass

    # ── إذا ما في فترة محددة → افتراضياً الشهر الحالي ───────────────────────
    if not filters:
        filters["month"] = today.month
        filters["year"] = today.year

    # ── نوع التقرير ─────────────────────────────────────────────────────────
    report_type = "general"
    if any(kw in t for kw in ["صيدلية", "صيدليات", "pharmacy"]):
        report_type = "pharmacies"
    elif any(kw in t for kw in ["منطقة", "مناطق", "area"]):
        report_type = "areas"
    elif any(kw in t for kw in ["منتج", "منتجات", "دواء", "صنف", "item"]):
        report_type = "items"
    elif any(kw in t for kw in ["اداء", "أداء", "performance", "كيف"]):
        report_type = "performance"

    filters["report_type"] = report_type
    return filters


# ══════════════════════════════════════════════════════════════════════════════
# ── Doctor Profile Parsing (Phase 1 + 3) ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_DOCTOR_SYSTEM = """أنت مساعد يستخرج بيانات طبيب من نص عربي مختصر مكتوب باللهجة العراقية أو الفصحى.
استخرج البيانات كـ JSON صارم بدون أي نص خارجه:
{"name":null,"specialty":null,"clinic_name":null,"area":null,"phone":null,"class":null,"notes":null}

قواعد:
- name: اسم الطبيب فقط (احذف لقب "د." أو "دكتور" أو "دكتورة")
- specialty: التخصص الطبي (قلبية، عظام، أطفال، نساء وولادة، عيون، جلدية، باطنية، أعصاب، مسالك، صدرية، أنف وأذن وحنجرة...)
  كلمات مثل Gyne=نساء وولادة، Internal=باطنية، Cardio=قلبية
- clinic_name: اسم العيادة أو المستشفى (لو ذُكر)
- area: المنطقة أو الحي (مثل: الكرادة، الكرخ، المنصور، زيونة)
- phone: رقم هاتف بصيغة 07XXXXXXXXX أو +964...
- class: فئة الطبيب إذا ذُكرت صراحةً كحرف منفرد: A أو A+ أو B أو C أو Z فقط.
  لو وُجد حرف منفرد A أو B أو C أو Z في النص (بدون كلمات أخرى بجانبه في السطر) → ضعه هنا.
  لو وُجد S أو D أو scientific أو dealer → ضع القيمة في notes وليس هنا. اتركها null لو غير واضح.
- notes: أي تفصيل إضافي. الكلمات S/D/scientific/dealer تُوضع هنا كملاحظة تسويقية.
- أي قيمة غير واضحة اتركها null
- لا تضف نص خارج JSON"""

_COMPETITOR_SYSTEM = """أنت مساعد يستخرج بيانات منافس دوائي من نص عربي مختصر.
أعد JSON صارم فقط:
{"competitor_name":null,"competitor_product":null,"notes":null}

قواعد:
- competitor_name: اسم شركة المنافس أو مندوبه
- competitor_product: اسم المنتج الدوائي للمنافس
- notes: أي ملاحظة إضافية
- لا تضف نص خارج JSON"""

# كلمات تشغّل نية "doctor_profile"
DOCTOR_PROFILE_TRIGGERS = [
    "ملف طبيب", "سجل طبيب", "معلومات طبيب", "بيانات طبيب",
    "أضف طبيب", "اضف طبيب", "إضافة طبيب", "اضافة طبيب",
    "قائمة أطباء", "قائمة اطباء", "أطباء", "اطباء",
    "عرض الأطباء", "عرض اطباء", "استعراض طبيب",
    "منافس طبيب", "منافس للطبيب",
]

# التخصصات الطبية المعروفة
_SPECIALTIES = {
    "قلب": "قلبية", "قلبية": "قلبية", "قلبي": "قلبية", "cardiolog": "قلبية",
    "عظام": "عظام", "عظمي": "عظام", "orthop": "عظام",
    "أطفال": "أطفال", "اطفال": "أطفال", "pediatr": "أطفال",
    "نساء": "نساء وولادة", "ولادة": "نساء وولادة", "نسائية": "نساء وولادة", "gynec": "نساء وولادة",
    "عيون": "عيون", "عيني": "عيون", "ophthalm": "عيون",
    "جلد": "جلدية", "جلدية": "جلدية", "dermat": "جلدية",
    "باطنية": "باطنية", "داخلية": "باطنية", "intern": "باطنية",
    "أعصاب": "أعصاب", "اعصاب": "أعصاب", "عصبية": "أعصاب", "neurol": "أعصاب",
    "مسالك": "مسالك بولية", "بولية": "مسالك بولية", "urol": "مسالك بولية",
    "تخدير": "تخدير",
    "جراحة": "جراحة عامة", "جراح": "جراحة عامة", "surg": "جراحة عامة",
    "أنف": "أنف وأذن وحنجرة", "اذن": "أنف وأذن وحنجرة", "حنجرة": "أنف وأذن وحنجرة", "ent": "أنف وأذن وحنجرة",
    "صدر": "صدرية", "صدرية": "صدرية", "pulmon": "صدرية",
    "دم": "أمراض دم", "أورام": "أورام", "oncol": "أورام",
    "نفسية": "نفسية", "نفس": "نفسية", "psychiatr": "نفسية",
    "سكري": "غدد وسكري", "غدد": "غدد وسكري", "endocr": "غدد وسكري",
    "روماتيزم": "روماتيزم", "rheumat": "روماتيزم",
    "طوارئ": "طوارئ",
    "عام": "طب عام", "طب عام": "طب عام",
    "رئة": "صدرية",
    "كلى": "كلى ومسالك",
    "مفاصل": "روماتيزم",
}


def ai_suggest_doctor(text: str) -> dict | None:
    """
    Use AI to parse a doctor profile from freeform text.
    Returns dict {name, specialty, clinic_name, area, phone, notes} or None.
    Falls back to regex parser if AI is unavailable.
    """
    raw = _ai_call(_DOCTOR_SYSTEM, text)
    if raw:
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw).rstrip('`').strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("name"):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    # Fallback to regex parser
    return parse_doctor_entry(text)


# حروف الفئة المعتمدة (تُكتب منفردة في سطر أو جزء)
_CLASS_MARKERS = {"A", "A+", "B", "C", "Z"}
# علامات تذهب للملاحظات (مناديب/علمي/تاجر)
_NOTES_MARKERS = {"S", "D", "SCIENTIFIC", "DEALER", "SEMI", "SEMI-SCIENTIFIC"}

# تخصصات إنجليزية إضافية
_SPECIALTIES_EN = {
    "gyne": "نساء وولادة", "gynec": "نساء وولادة", "gynaec": "نساء وولادة",
    "ob/gyn": "نساء وولادة", "obgyn": "نساء وولادة",
    "cardio": "قلبية", "cardiac": "قلبية",
    "neuro": "أعصاب", "neurology": "أعصاب",
    "ortho": "عظام", "orthop": "عظام",
    "pedia": "أطفال", "pediatr": "أطفال",
    "ophthal": "عيون", "optic": "عيون",
    "derma": "جلدية", "skin": "جلدية",
    "urol": "مسالك بولية",
    "pulmon": "صدرية", "chest": "صدرية",
    "internal": "باطنية", "internist": "باطنية",
    "ent": "أنف وأذن وحنجرة",
    "endocr": "غدد وسكري", "diabeto": "غدد وسكري",
    "rheumat": "روماتيزم",
    "oncol": "أورام", "hematol": "أمراض دم",
    "psychiatr": "نفسية", "psychol": "نفسية",
    "general": "جراحة عامة", "surg": "جراحة عامة",
    "nephrol": "كلى ومسالك",
    "gastro": "جهاز هضمي",
    "hepatol": "كبد",
    "emerg": "طوارئ",
}


def parse_doctor_entry(text: str) -> dict:
    """
    Parse doctor info from freeform Arabic text using regex.
    Examples:
      "د. أحمد الجبوري - قلبية - الكرادة - A"
      "دكتور سامي — Gyne — مستشفى النهرين — الكرخ — D — B"
    يستخرج الفئة (A/B/C/Z/A+) من النص مباشرة.
    D/S/scientific/dealer تذهب للملاحظات.
    """
    data = {"name": None, "specialty": None, "clinic_name": None,
            "area": None, "phone": None, "class": None, "notes": None}

    # استخرج رقم الهاتف أولاً ثم احذفه من النص
    phone_m = re.search(r'(07\d{8,9}|\+964\d{9,11})', text)
    if phone_m:
        data["phone"] = phone_m.group(0)
        text = text[:phone_m.start()].strip() + " " + text[phone_m.end():].strip()

    # فصّل الأجزاء بالفواصل والشرطات والسطور
    parts = re.split(r'[\-–—/|،,\n]+', text)
    parts = [p.strip() for p in parts if p.strip()]

    # الجزء الأول: اسم الطبيب (احذف ألقاب "دكتور / د.")
    if parts:
        name = re.sub(
            r'^(دكتورة|دكتور|د\.|dr\.?)\s*', '',
            parts[0], flags=re.IGNORECASE
        ).strip()
        data["name"] = name

    found_specialty = False
    found_clinic = False
    notes_parts = []

    def _detect_specialty(text_piece: str):
        t = text_piece.lower().strip()
        # فحص التخصصات الإنجليزية أولاً
        for kw, canonical in _SPECIALTIES_EN.items():
            if t == kw or t.startswith(kw):
                return canonical
        # ثم العربية
        for kw, canonical in _SPECIALTIES.items():
            if kw in t:
                return canonical
        return None

    remaining = parts[1:]
    for part in remaining:
        p_stripped = part.strip()
        p_upper = p_stripped.upper()

        # ── فحص علامات الفئة (A/B/C/Z/A+) أولاً ─────────────────────────
        if p_upper in _CLASS_MARKERS and len(p_stripped) <= 2:
            data["class"] = p_upper
            continue

        # ── فحص علامات الملاحظات (S/D/scientific/dealer) ────────────────
        if p_upper in _NOTES_MARKERS and len(p_stripped) <= 15:
            notes_parts.append(p_stripped)
            continue

        # ── عيادة / مستشفى / مركز ─────────────────────────────────────────
        if not found_clinic and re.search(r'عيادة|مستشفى|مركز طبي|مركز|hospital|clinic', p_stripped, re.IGNORECASE):
            data["clinic_name"] = p_stripped
            found_clinic = True
            continue

        # ── تخصص ──────────────────────────────────────────────────────────
        if not found_specialty:
            sp = _detect_specialty(p_stripped)
            if sp:
                data["specialty"] = sp
                found_specialty = True
                continue

        # ── منطقة ─────────────────────────────────────────────────────────
        if not data["area"]:
            data["area"] = p_stripped
            continue

        # ── باقي الكلام → ملاحظات ─────────────────────────────────────────
        notes_parts.append(p_stripped)

    # دمج الملاحظات
    if notes_parts:
        existing = data.get("notes") or ""
        combined = " | ".join(notes_parts)
        data["notes"] = (existing + " | " + combined).strip(" |") if existing else combined

    # إذا لم يُعثر على التخصص في الأجزاء، جرب كامل النص
    if not found_specialty:
        sp = _detect_specialty(text)
        if sp:
            data["specialty"] = sp

    return data


def parse_competitor_entry(text: str) -> dict:
    """
    Parse competitor info from freeform text.
    Example: "AstraZeneca - Crestor 10mg - يروجونه بسعر أقل"
    """
    raw = _ai_call(_COMPETITOR_SYSTEM, text)
    if raw:
        raw = re.sub(r'^```[a-zA-Z]*\n?', '', raw).rstrip('`').strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback regex
    data = {"competitor_name": None, "competitor_product": None, "notes": None}
    parts = re.split(r'[\-–—/|،,\n]+', text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 1:
        data["competitor_name"] = parts[0]
    if len(parts) >= 2:
        data["competitor_product"] = parts[1]
    if len(parts) >= 3:
        data["notes"] = " | ".join(parts[2:])
    return data
