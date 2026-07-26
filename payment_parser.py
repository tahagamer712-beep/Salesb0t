"""تحليل رسائل التسديد (تحصيل المندوب من الصيدلية) — نمط AI أولاً ثم regex fallback.
يعيد استخدام _ai_call() و parse_date() من parser.py بدل تكرارهم."""
import re
import json
import logging

from parser import _ai_call, parse_date

_pay_log = logging.getLogger("salesbot.payment")

# ─── AI ──────────────────────────────────────────────────────────────────────

_PAYMENT_SYSTEM = """أنت مساعد يستخرج بيانات عملية تسديد (تحصيل مبلغ من صيدلية) من رسالة مندوب مبيعات عراقي.
استخرج البيانات كـ JSON صارم (بدون تعليقات أو نص خارجه) بهذه الحقول:
{"pharmacy":null,"amount":null,"payment_date":null,"notes":null}

قواعد:
- pharmacy: اسم الصيدلية فقط (بدون كلمة "سدد" أو "تسديد" أو "دفع")
- amount: رقم صحيح بالدينار العراقي الكامل (مثال: "50" أو "خمسين" تعني 50000، "خمسين الف" تعني 50000، "مليون ونص" تعني 1500000)
- payment_date: صيغة "YYYY-MM-DD" لو مذكور تاريخ صراحة، وإلا null (يعني اليوم)
- notes: أي ملاحظة إضافية (مثل "شيك" أو "دفعة جزئية") أو null
- لا تضيف أي نص خارج JSON

أمثلة:
رسالة: "سدد صيدلية الحور خمسين"
النتيجة: {"pharmacy":"صيدلية الحور","amount":50000,"payment_date":null,"notes":null}

رسالة: "استلمت مليون ونص من صيدلية النور اليوم"
النتيجة: {"pharmacy":"صيدلية النور","amount":1500000,"payment_date":null,"notes":null}

رسالة: "تسديد ربع مليون صيدلية الرافدين بشيك"
النتيجة: {"pharmacy":"صيدلية الرافدين","amount":250000,"payment_date":null,"notes":"شيك"}"""


def _ai_parse_payment(text: str) -> dict | None:
    """يطلب من الـ AI استخراج بيانات التسديد. يعيد dict أو None."""
    raw = _ai_call(_PAYMENT_SYSTEM, text)
    if not raw:
        return None
    raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("amount"):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# ─── Regex fallback — تحليل المبلغ بالأرقام أو بالكلمات العربية ────────────────

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

_AR_ONES = {
    "واحد": 1, "احد": 1, "اثنين": 2, "اثنان": 2, "ثنين": 2,
    "ثلاثة": 3, "ثلاث": 3, "اربعة": 4, "أربعة": 4, "اربع": 4,
    "خمسة": 5, "خمس": 5, "ستة": 6, "ست": 6, "سبعة": 7, "سبع": 7,
    "ثمانية": 8, "ثماني": 8, "تمانية": 8, "تسعة": 9, "تسع": 9,
}
_AR_TEENS = {
    "عشرة": 10, "عشره": 10, "احدعشر": 11, "حداشر": 11, "اثناعشر": 12, "اثنعشر": 12, "طناشر": 12,
    "ثلاثطعش": 13, "ثلاثةعشر": 13, "اربعطعش": 14, "اربعةعشر": 14, "خمسطعش": 15, "خمسةعشر": 15,
    "سطعش": 16, "ستةعشر": 16, "سبعطعش": 17, "سبعةعشر": 17, "ثمنطعش": 18, "ثمانيةعشر": 18,
    "تسعطعش": 19, "تسعةعشر": 19,
}
_AR_TENS = {
    "عشرين": 20, "ثلاثين": 30, "ثلاثون": 30, "اربعين": 40, "أربعين": 40,
    "خمسين": 50, "ستين": 60, "سبعين": 70, "ثمانين": 80, "تسعين": 90,
}
_AR_HUNDREDS = {"مية": 100, "مائة": 100, "مئة": 100}
_AR_FRACTIONS = {"ربع": 0.25, "نص": 0.5, "نصف": 0.5, "ثلث": 1 / 3, "تلت": 1 / 3}
_AR_THOUSAND = ("الف", "ألف")
_AR_MILLION = ("مليون",)

_CURRENCY_NOISE = re.compile(r'دينار|د\.?ع|iqd|عراقي', re.IGNORECASE)


def _normalize_amount_text(text: str) -> str:
    text = text.translate(_ARABIC_DIGITS)
    text = _CURRENCY_NOISE.sub('', text)
    text = text.replace(",", "").replace("،", "")
    # "ونص"/"وخمسين" (و ملتصقة بالكلمة بدون فراغ) → "و نص"/"و خمسين"
    text = re.sub(r'\bو(?=[\u0600-\u06FF])', 'و ', text)
    return text.strip()


def _word_value(word: str) -> float | None:
    """يحوّل كلمة عربية واحدة لرقمها (وحدات/عشرات/معشرات/مئات) أو None لو مو رقم معروف."""
    w = word.strip()
    if w in _AR_HUNDREDS:
        return _AR_HUNDREDS[w]
    if w in _AR_TEENS:
        return _AR_TEENS[w]
    if w in _AR_TENS:
        return _AR_TENS[w]
    if w in _AR_ONES:
        return _AR_ONES[w]
    return None


def _eval_term(term: str, has_million_elsewhere: bool) -> float | None:
    """
    يقيّم جزء واحد من عبارة المبلغ (بعد التقسيم على 'و').
    يرجع القيمة النهائية بالدينار الكامل، أو None لو الجزء يحتوي كلمة غير معروفة
    (عشان ما نبلع اسم الصيدلية أو كلمات ثانية بالغلط كأنها رقم).
    """
    term = term.strip()
    if not term:
        return 0.0

    words = term.split()

    def _known(w: str) -> bool:
        return (w in _AR_MILLION or w in _AR_THOUSAND or w in _AR_FRACTIONS
                or _word_value(w) is not None or re.fullmatch(r'\d+', w) is not None)

    if not all(_known(w) for w in words):
        return None

    # ── حالة: يحتوي "مليون" ────────────────────────────────────────────────
    if any(w in _AR_MILLION for w in words):
        for w in words:
            if w in _AR_FRACTIONS:
                return _AR_FRACTIONS[w] * 1_000_000
        num = None
        for w in words:
            if w in _AR_MILLION:
                continue
            v = _word_value(w)
            if v is not None:
                num = (num or 0) + v
        return (num or 1) * 1_000_000

    # ── حالة: كسر منفرد بدون مليون بجانبه، لكن "مليون" مذكور بجزء ثاني ─────
    if words and all(w in _AR_FRACTIONS for w in words) and has_million_elsewhere:
        return sum(_AR_FRACTIONS[w] for w in words) * 1_000_000

    # ── حالة: يحتوي "الف/ألف" ──────────────────────────────────────────────
    if any(w in _AR_THOUSAND for w in words):
        num = None
        for w in words:
            if w in _AR_THOUSAND:
                continue
            if re.fullmatch(r'\d+', w):
                num = (num or 0) + int(w)
                continue
            v = _word_value(w)
            if v is not None:
                num = (num or 0) + v
        return (num or 1) * 1000

    # ── حالة: رقم بالأرقام مباشرة ────────────────────────────────────────
    digits = re.fullmatch(r'\d+', term.replace(" ", ""))
    if digits:
        val = int(digits.group())
        return val if val >= 1000 else val * 1000

    # ── حالة: كلمة/كلمات رقمية بدون مضاعِف (يُفترض آلاف حسب القاعدة العامة) ─
    total = None
    for w in words:
        v = _word_value(w)
        if v is not None:
            total = (total or 0) + v
        else:
            return None  # كلمة غير معروفة ضمن جزء يُفترض إنه كله رقمي
    if total is not None:
        return total * 1000

    return None


def parse_amount_words(text: str) -> int | None:
    """
    يحلل عبارة مبلغ (أرقام أو كلمات عربية) ويرجعها كعدد صحيح بالدينار الكامل.
    القاعدة: رقم < 1000 بدون "الف/مليون" → يُضرب × 1000.
    أمثلة: "50"→50000, "خمسين الف"→50000, "مليون وخمسين"→1050000, "ربع مليون"→250000.
    يرجع None لو ما قدر يفهم شي.
    """
    if not text:
        return None
    norm = _normalize_amount_text(text)
    if not norm:
        return None

    has_million = bool(re.search(r'مليون', norm))
    terms = [t for t in re.split(r'\bو\b', norm) if t.strip()]
    if not terms:
        return None

    values = [_eval_term(t, has_million) for t in terms]
    if any(v is None for v in values):
        return None
    total = sum(values)
    return int(total) if total > 0 else None


# ─── الدالة الرئيسية ─────────────────────────────────────────────────────────

def parse_payment_smart(text: str) -> dict | None:
    """
    يحلل نص تسديد حر كامل (صيدلية + مبلغ [+ تاريخ/ملاحظة]).
    المرحلة 1: AI. المرحلة 2: regex fallback (استخراج تقريبي: يشيل كلمات الفعل
    ["سدد","تسديد","دفع","استلمت","استلم"] ثم يعتبر آخر جزء رقمي/كلامي = المبلغ،
    والباقي = اسم الصيدلية المرشّح — يُترك للمستدعي (bot.py) يطابقه بـ fuzzy_match_pharmacy).
    """
    ai_result = _ai_parse_payment(text)
    if ai_result:
        return ai_result

    cleaned = re.sub(r'\b(سدد|تسديد|دفع|استلمت|استلم|من|بشيك|كاش)\b', ' ', text).strip()

    # ابحث عن أطول عبارة مبلغ صالحة بنهاية النص
    amount = None
    amount_span = None
    tokens = cleaned.split()
    for start in range(len(tokens)):
        candidate = " ".join(tokens[start:])
        val = parse_amount_words(candidate)
        if val:
            amount = val
            amount_span = start
            break

    if amount is None:
        return None

    pharmacy_guess = " ".join(tokens[:amount_span]).strip() or None

    return {
        "pharmacy": pharmacy_guess,
        "amount": amount,
        "payment_date": None,
        "notes": None,
    }
