import os
import re
import hashlib
import logging
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes, Defaults,
)
from telegram.constants import ParseMode

from database import (
    init_db, get_or_create_user, save_order, fetch_orders, activate_code,
    save_user_template, get_user_template, delete_user_template,
    get_last_order, get_order_by_id, update_order_field, delete_order, is_duplicate_order,
    get_user_settings, set_rep_name_memory, toggle_setting, set_manager,
    fetch_orders_today, get_pharmacy_profile, search_orders,
    get_performance_stats, get_rep_ranking, get_known_products,
    get_known_pharmacies, get_last_n_orders, get_team_stats,
    delete_all_orders, delete_orders_by_month,
    get_product_catalog, add_product_to_catalog, delete_product_from_catalog,
    clear_product_catalog, update_product_price,
    set_drug_target, get_drug_targets,
    register_user, is_user_registered, get_user_full_name,
    set_has_seen_help,
    upsert_doctor_profile, get_doctor_profile_by_id,
    get_all_doctor_profiles, calculate_doctor_class, update_doctor_class_auto,
    find_duplicate_doctors, add_competitor_entry, get_competitor_entries,
    get_orders_for_doctor, delete_doctor_profile, fetch_orders_yesterday,
    search_doctor_profiles,
    add_competitor_drug, get_competitor_drugs, get_competitor_drug_by_id,
    delete_competitor_drug, toggle_doctor_competitor_drug,
    get_linked_doctors_for_drug, get_linked_drug_ids_for_drug,
    get_competitor_drugs_for_doctor,
    search_doctors_by_competitor_drug,
    save_payment, update_payment_field, delete_payment, get_payment,
    get_payments_by_rep, get_pharmacy_balance, get_rep_pharmacy_balances,
)
from parser import (
    detect_intent, parse_order_with_template,
    parse_report_filters, parse_template_definition, format_template_display,
    parse_edit_instruction, parse_compare_months, MONTH_NUM_TO_AR,
    parse_quantity_bonus, parse_pharmacy_name, parse_smart_search,
    parse_repeat_scope, parse_order_smart,
    ai_suggest_doctor, parse_competitor_entry,
    DOCTOR_PROFILE_TRIGGERS, AIParseError,
)
from payment_parser import parse_payment_smart, parse_amount_words
from fuzzy import fuzzy_match_product, fuzzy_match_pharmacy, fuzzy_match_candidates, detect_contradictions
from reports import (generate_excel_report, build_text_summary, build_compare_summary,
                     generate_template_excel, generate_import_template, parse_import_excel,
                     generate_catalog_import_template, parse_catalog_import_excel,
                     generate_survey_excel, parse_survey_import_excel,
                     generate_payment_excel)
from database import bulk_save_orders
from subscription import (
    check_subscription, can_use,
    format_expired_basic_message, format_expired_pro_message,
    format_banned_message, format_welcome_trial,
    PLAN_NAMES, QI_CARD_NUMBER, PRICES,
)
from admin import handle_admin, handle_admin_callback, is_admin, ADMIN_IDS, handle_admin_ai_key_text, handle_admin_search_text
from database import (
    create_subscription_request,
)
from scheduler import setup_scheduler

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ملف لوگ دوّار (5MB × 3 نسخ) — يمنع امتلاء تخزين الجهاز مع تشغيل 24/7 بدون
# ما يحتاج إعادة تشغيل الخدمة أو تدوير يدوي؛ لو فشل إنشاء الملف (نظام قراءة فقط
# مثلاً) نكمل بالـ console logging العادي بدون ما نوقف تشغيل البوت.
try:
    from logging.handlers import RotatingFileHandler
    _file_handler = RotatingFileHandler(
        os.environ.get("LOG_FILE", "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(_file_handler)
except Exception as _log_err:
    logger.warning(f"Could not set up file logging, continuing with console only: {_log_err}")

# ── الحفظ التلقائي بعد 30 ثانية ─────────────────────────────────────────────
# chat_id → {"user_id", "order"(or None), "multi"(or None), "settings", "tg_name"}
_pending_autosave: dict[int, dict] = {}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# توقيت البوت — كل الجدولة (الملخص اليومي، تنبيه النشاط...) تُحسب على هذا التوقيت
# بدل توقيت السيرفر الافتراضي (UTC على Heroku)، وإلا الإشعارات تجي بوقت غلط بـ3 ساعات.
BOT_TIMEZONE = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Asia/Baghdad"))
PORT = int(os.environ.get("PORT", 5000))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

from botutils import run_blocking


ALL_FIELDS = [
    ("rep_name",     "👤 اسم المندوب"),
    ("order_date",   "📅 التاريخ"),
    ("area",         "📍 المنطقة"),
    ("pharmacy",     "🏥 الصيدلية"),
    ("doctor_name",  "🩺 اسم الطبيب"),
    ("item_name",    "💊 المنتج"),
    ("quantity",     "🔢 الكمية"),
    ("bonus",        "🎁 البونص"),
    ("price",        "💵 السعر"),
    ("payment_type", "💰 نوع الدفع"),
    ("notes",        "📝 ملاحظات"),
]
FIELD_LABELS = {k: v for k, v in ALL_FIELDS}
ORDER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


# ── مساعد: اسم المندوب المرتبط بالحساب ──────────────────────────────────────
async def _get_order_rep_name(user_id: int, tg_user) -> str:
    """يرجع الاسم الثلاثي المسجّل للمندوب (أولوية)، ثم rep_name_memory، ثم اسم تيلگرام."""
    full_name = await run_blocking(get_user_full_name, user_id)
    if full_name:
        return full_name
    settings = await run_blocking(get_user_settings, user_id)
    return settings.get("rep_name_memory") or tg_user.full_name


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.strip().encode()).hexdigest()


# ── الحفظ التلقائي بعد 30 ثانية ─────────────────────────────────────────────
async def _auto_save_job(context: ContextTypes.DEFAULT_TYPE):
    """Job يحفظ الطلب تلقائياً بعد 30 ثانية إذا ما ضغط المندوب حفظ أو إلغاء."""
    chat_id = context.job.chat_id
    entry = _pending_autosave.pop(chat_id, None)
    if not entry:
        return  # تم الحفظ أو الإلغاء يدوياً

    user_id = entry["user_id"]
    tg_name = entry.get("tg_name", "")
    order = entry.get("order")
    multi = entry.get("multi")

    try:
        if multi:
            base_data = multi["base_data"]
            products = multi["products"]
            saved_ids = []
            general_note = base_data.get("notes")
            for prod in products:
                od = {**base_data, **prod}
                item_note = prod.get("notes")
                if general_note and item_note:
                    od["notes"] = f"{general_note} | {item_note}"
                else:
                    od["notes"] = item_note or general_note
                oid = await run_blocking(save_order, user_id, od)
                saved_ids.append(oid)
            count = len(saved_ids)
            msg = f"⏱️ *تم الحفظ التلقائي — {count} منتج*\n🏪 {base_data.get('pharmacy', '—')}"
            admin_txt = f"📋 *طلب متعدد تلقائي من {tg_name}*\n{msg}"
            post_kbd = build_post_save_multi_keyboard(saved_ids)
        else:
            saved_id = await run_blocking(save_order, user_id, order)
            msg = (
                f"⏱️ *تم الحفظ التلقائي*\n"
                f"🏪 {order.get('pharmacy', '—')} — {order.get('item_name', '—')} × {order.get('quantity', '—')}"
            )
            admin_txt = f"📋 *طلب تلقائي من {tg_name}*\n{msg}"
            post_kbd = build_post_save_keyboard(saved_id) if saved_id else None

        await context.bot.send_message(
            chat_id=chat_id, text=msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=post_kbd,
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_txt, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Auto-save job error: {e}")


def _schedule_autosave(context: ContextTypes.DEFAULT_TYPE, chat_id: int, entry: dict):
    """يجدول حفظاً تلقائياً بعد 30 ثانية ويلغي أي جدولة سابقة لنفس المحادثة."""
    _cancel_autosave(context, chat_id)
    _pending_autosave[chat_id] = entry
    context.job_queue.run_once(
        _auto_save_job,
        when=30,
        chat_id=chat_id,
        name=f"autosave_{chat_id}",
    )


def _cancel_autosave(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """يلغي الحفظ التلقائي المجدوَل ويمسح البيانات المنتظِرة."""
    _pending_autosave.pop(chat_id, None)
    jobs = context.job_queue.get_jobs_by_name(f"autosave_{chat_id}")
    for job in jobs:
        job.schedule_removal()


def _build_help_message() -> str:
    """يبني رسالة دليل الاستخدام — تُستخدم في /start و /help."""
    return (
        "📖 *دليل الاستخدام — طريقة تسجيل الطلبات*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✏️ *١. الطريقة الحرة — الأسهل والأسرع*\n"
        "فقط اكتب طلبك بشكل طبيعي:\n"
        "`صيدلية الأمل امبيسلين 10+2 دين`\n\n"
        "البوت يفهم تلقائياً:\n"
        "• الصيدلية ← الأمل\n"
        "• المنتج ← امبيسلين\n"
        "• الكمية ← 10 + بونص 2\n"
        "• الدفع ← دين\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✏️ *٢. طلبات متعددة لنفس الصيدلية*\n"
        "كل منتج بسطر منفصل:\n"
        "```\nصيدلية النور\nامبيسلين 10\nاموكسيل 5+1\nسيفالكس 8\n```\n"
        "يحفظ ٣ طلبات دفعة واحدة ✅\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✏️ *٣. إدخال حقل بحقل*\n"
        "اكتب: `طلب جديد`\n"
        "البوت يسألك خطوة خطوة حتى تكتمل كل التفاصيل\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *التقارير:*\n"
        "• `تقرير اليوم` — ملخص فوري\n"
        "• `تقرير هذا الشهر` — تقرير شهري\n"
        "• `تقرير excel` — ملف Excel للتنزيل\n"
        "• `قارن مارس وأبريل` — مقارنة شهرين\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 *بحث وتعديل:*\n"
        "• `بحث صيدلية الأمل` — كل طلبات الصيدلية\n"
        "• `عدّل آخر طلب` — تعديل الطلب الأخير\n"
        "• `كرر آخر طلب` — إعادة آخر طلب بتاريخ اليوم\n"
        "• `احذف آخر طلب` — حذف الطلب الأخير\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📥 *إضافة بيانات شهور سابقة:*\n"
        "من القائمة الرئيسية اضغط *📥 إضافة بيانات سابقة*\n"
        "ثم اختر الشهر، وعندك خيارين:\n\n"
        "① *استيراد Excel* — حمّل القالب الجاهز، عبّيه، وارسله\n"
        "   كل سطر = صيدلية واحدة + منتج واحد\n\n"
        "② *إدخال يدوي* — اكتب طلباتك بشكل عادي\n"
        "   لازم تذكر التاريخ: `5/4 صيدلية الأمل Maxiflex 10`\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *نصائح سريعة:*\n"
        "• بعد عرض الطلب اضغط ✅ *حفظ* أو ❌ *إلغاء*\n"
        "• إذا ما ضغطت شيء — يُحفظ تلقائياً بعد 30 ثانية\n"
        "• اسمك الثلاثي يُسجَّل تلقائياً بكل طلب\n"
        "• البوت يصحّح أسماء الصيدليات والمنتجات تلقائياً"
    )


def build_template_builder(selected: list) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(ALL_FIELDS), 2):
        row = []
        for field_key, field_label in ALL_FIELDS[i:i + 2]:
            if field_key in selected:
                idx = selected.index(field_key)
                emoji = ORDER_EMOJIS[idx] if idx < len(ORDER_EMOJIS) else f"{idx + 1}"
                btn_label = f"{emoji} {field_label}"
            else:
                btn_label = f"⬜ {field_label}"
            row.append(InlineKeyboardButton(btn_label, callback_data=f"tmpl_{field_key}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✅ حفظ القالب", callback_data="tmpl_save"),
        InlineKeyboardButton("🗑️ مسح الكل", callback_data="tmpl_clear"),
    ])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")])
    return InlineKeyboardMarkup(rows)


async def _notify_admin(context, text: str, excel=None, filename: str = ""):
    for admin_id in ADMIN_IDS:
        try:
            if excel:
                import io
                excel.seek(0)
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=excel,
                    filename=filename,
                    caption=text,
                )
                excel.seek(0)
            else:
                await context.bot.send_message(
                    chat_id=admin_id, text=text, parse_mode=ParseMode.MARKDOWN
                )
        except Exception:
            pass


# ── ثوابت الأدوية المنافسة ────────────────────────────────────────────────────
DRUG_FORMS = [
    ("cap",    "💊 كبسول"),
    ("tab",    "🔵 حبة / أقراص"),
    ("supp",   "🕯️ تحميلة"),
    ("amp",    "💉 أمبولة"),
    ("syrup",  "🧴 شراب"),
    ("cream",  "🫧 كريم / مرهم"),
    ("drops",  "💧 قطرة"),
    ("spray",  "💨 بخاخ"),
    ("sachet", "🧂 ساشيه / مسحوق"),
    ("patch",  "🩹 لصقة"),
    ("vial",   "🫙 فيال"),
    ("other",  "📦 أخرى"),
]
DRUG_FORM_LABELS = {k: v for k, v in DRUG_FORMS}

DRUG_ROUTES = [
    ("oral",    "🔵 بلع (Orally)"),
    ("iv",      "💉 وريدي (IV)"),
    ("im",      "💉 عضلي (IM)"),
    ("sc",      "📍 تحت الجلد (SC)"),
    ("vag",     "🔴 مهبلي (Vaginally)"),
    ("rect",    "🔴 مستقيمي (Rectally)"),
    ("top",     "🫧 موضعي (Topically)"),
    ("inh",     "💨 استنشاق (Inhalation)"),
    ("sublin",  "👅 تحت اللسان (Sublingual)"),
    ("nasal",   "👃 أنفي (Nasal)"),
    ("ocular",  "👁️ عيني (Ocular)"),
    ("other_r", "📦 أخرى"),
]
DRUG_ROUTE_LABELS = {k: v for k, v in DRUG_ROUTES}


def _format_comp_drug(d: dict) -> str:
    """تنسيق عرض الدواء المنافس بشكل جميل."""
    form_lbl  = DRUG_FORM_LABELS.get(d.get("form", ""), d.get("form") or "—")
    routes_raw = d.get("routes") or ""
    routes_lbl = " / ".join(
        DRUG_ROUTE_LABELS.get(r.strip(), r.strip())
        for r in routes_raw.split(",") if r.strip()
    ) or "—"
    price_str = f"{float(d['price']):,.0f} د.ع" if d.get("price") else "—"
    lines = [
        f"💊 *{d['drug_name']}*",
        f"🏭 الشركة: {d.get('company') or '—'}",
        f"📦 الشكل: {form_lbl}",
        f"🔀 طريق الإدخال: {routes_lbl}",
        f"💵 السعر: {price_str}",
        f"⭐ المميزات: {d.get('features') or '—'}",
    ]
    return "\n".join(lines)


_PAYMENT_TRIGGER = re.compile(r'\b(سدد|سددت|تسديد|استلمت|استلم|دفعلي|دفعتلي|حصلت)\b')


def build_main_menu(sub: dict = None, is_admin_user: bool = False) -> InlineKeyboardMarkup:
    is_pro = sub and can_use(sub, "reports") and can_use(sub, "search")
    rows = [
        [
            InlineKeyboardButton("📝 تسجيل طلب", callback_data="help_order"),
            InlineKeyboardButton("📊 التقارير", callback_data="menu_reports"),
        ],
        [
            InlineKeyboardButton("🏥 ملف صيدلية", callback_data="help_pharmacy"),
            InlineKeyboardButton("🔍 بحث ذكي", callback_data="help_search"),
        ],
        [
            InlineKeyboardButton("📈 أدائي", callback_data="do_performance"),
            InlineKeyboardButton("🔔 التنبيهات", callback_data="menu_alerts"),
        ],
        [
            InlineKeyboardButton("📥 إضافة بيانات سابقة", callback_data="menu_past_data"),
            InlineKeyboardButton("💰 تسجيل تسديد", callback_data="do_payment_start"),
        ],
        [InlineKeyboardButton("📦 طلبات اليوم والأمس", callback_data="daily_review")],
    [InlineKeyboardButton("🩺 ملف الطبيب", callback_data="menu_doctors")],
    [InlineKeyboardButton("💊 الأدوية المنافسة", callback_data="menu_comp_drugs")],
    [InlineKeyboardButton("⚙️ الإعدادات", callback_data="menu_settings")],
    ]
    if not is_pro:
        rows[-1].append(InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"))
    if is_admin_user:
        rows.append([InlineKeyboardButton("🏢 لوحة الأدمن", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


def build_past_month_menu() -> InlineKeyboardMarkup:
    """قائمة اختيار الشهر لإضافة بيانات سابقة."""
    months_ar = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
                 "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
    today = date.today()
    rows = []
    row = []
    for m in range(1, 13):
        y = today.year if m <= today.month else today.year - 1
        row.append(InlineKeyboardButton(
            f"{months_ar[m-1]} {y}",
            callback_data=f"past_month_{m}_{y}"
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def build_reports_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 تقرير اليوم",    callback_data="do_report_today"),
            InlineKeyboardButton("📆 هذا الشهر",      callback_data="do_report_month"),
        ],
        [
            InlineKeyboardButton("🗓 شهور أخرى",      callback_data="do_report_pick_month"),
        ],
        [
            InlineKeyboardButton("📊 مقارنة شهرين",   callback_data="help_compare"),
            InlineKeyboardButton("📈 أدائي",           callback_data="do_performance"),
        ],
        [
            InlineKeyboardButton("📥 استيراد Excel",  callback_data="do_import_template"),
        ],
        [
            InlineKeyboardButton("💰 تقارير التسديد",  callback_data="pmt_report_menu"),
        ],
        [InlineKeyboardButton("🔙 رجوع للقائمة",      callback_data="menu_main")],
    ])


def build_alerts_menu(settings: dict) -> InlineKeyboardMarkup:
    inact = "✅ تنبيه الخمول" if settings.get("alert_inactivity") else "⬜ تنبيه الخمول"
    unvis = "✅ صيدليات غير مزارة" if settings.get("alert_unvisited") else "⬜ صيدليات غير مزارة"
    daily = "✅ ملخص يومي" if settings.get("daily_summary") else "⬜ ملخص يومي"
    weekly = "✅ تقرير أسبوعي" if settings.get("weekly_report") else "⬜ تقرير أسبوعي"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(inact, callback_data="toggle_inactivity"),
            InlineKeyboardButton(unvis, callback_data="toggle_unvisited"),
        ],
        [
            InlineKeyboardButton(daily, callback_data="toggle_daily"),
            InlineKeyboardButton(weekly, callback_data="toggle_weekly"),
        ],
        [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="menu_main")],
    ])


def build_settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 تغيير اسمك", callback_data="set_name_prompt"),
            InlineKeyboardButton("📋 تصميم قالب", callback_data="help_template"),
        ],
        [
            InlineKeyboardButton("💊 كتالوج أدويتي", callback_data="catalog_menu"),
            InlineKeyboardButton("❌ إلغاء القالب", callback_data="tmpl_delete"),
        ],
        [InlineKeyboardButton("🔬 عرض الأدوية المنافسة", callback_data="menu_comp_drugs")],
        [
            InlineKeyboardButton("🗑️ احذف آخر طلب", callback_data="do_delete_last"),
            InlineKeyboardButton("🗑️ حذف جميع طلباتي", callback_data="do_delete_all_confirm"),
        ],
        [InlineKeyboardButton("📅 حذف مبيعات شهر معين", callback_data="do_delete_month_pick")],
        [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="menu_main")],
    ])


def format_order_confirmation(data: dict) -> str:
    lines = ["✅ *تم حفظ الطلب بنجاح!*\n"]
    if data.get("rep_name"):
        lines.append(f"👤 *المندوب:* {data['rep_name']}")
    if data.get("order_date"):
        lines.append(f"📅 *التاريخ:* {data['order_date']}")
    if data.get("area"):
        lines.append(f"📍 *المنطقة:* {data['area']}")
    if data.get("pharmacy"):
        lines.append(f"🏪 *الصيدلية:* {data['pharmacy']}")
    if data.get("item_name"):
        item_str = data["item_name"]
        if data.get("quantity"):
            item_str += f" × {data['quantity']}"
        if data.get("bonus"):
            item_str += f" + {data['bonus']} (بونص)"
        lines.append(f"💊 *المنتج:* {item_str}")
    if data.get("doctor_name"):
        lines.append(f"🩺 *الطبيب:* {data['doctor_name']}")
    if data.get("price"):
        lines.append(f"💵 *السعر:* {data['price']:,}")
    if data.get("payment_type"):
        lines.append(f"💰 *الدفع:* {data['payment_type']}")
    if data.get("location_link"):
        lines.append(f"📍 *[الموقع على الخريطة]({data['location_link']})*")
    if data.get("notes"):
        lines.append(f"📝 *ملاحظات:* {data['notes']}")
    return "\n".join(lines)


def format_order_preview(data: dict, is_multi: bool = False, products: list = None) -> str:
    lines = ["📋 *مراجعة الطلب*\n"]
    if data.get("rep_name"):
        lines.append(f"👤 *المندوب:* {data['rep_name']}")
    if data.get("order_date"):
        lines.append(f"📅 *التاريخ:* {data['order_date']}")
    if data.get("order_type"):
        lines.append(f"🔄 *النوع:* {data['order_type']}")
    if data.get("area"):
        lines.append(f"📍 *المنطقة:* {data['area']}")
    if data.get("pharmacy"):
        lines.append(f"🏪 *الصيدلية:* {data['pharmacy']}")
    if is_multi and products:
        lines.append("")
        for p in products:
            qty_str = str(p.get("quantity") or "—")
            if p.get("bonus"):
                qty_str += f" +{p['bonus']} بونص"
            pline = f"  💊 *{p.get('item_name','—')}* × {qty_str}"
            if p.get("notes"):
                pline += f"\n     📝 _{p['notes']}_"
            lines.append(pline)
    else:
        if data.get("item_name"):
            lines.append(f"💊 *المنتج:* {data['item_name']}")
        if data.get("quantity"):
            lines.append(f"📦 *الكمية:* {data['quantity']}")
        if data.get("bonus"):
            lines.append(f"🎁 *البونص:* {data['bonus']}")
        if data.get("doctor_name"):
            lines.append(f"👨‍⚕️ *الطبيب:* {data['doctor_name']}")
        if data.get("price"):
            lines.append(f"💵 *السعر:* {data['price']:,}")
        if data.get("payment_type"):
            lines.append(f"💰 *الدفع:* {data['payment_type']}")
        if data.get("notes"):
            lines.append(f"📝 *الملاحظات:* {data['notes']}")
    lines.append("\n_اختر إجراء:_")
    return "\n".join(lines)


def build_order_review_keyboard(is_multi: bool = False) -> InlineKeyboardMarkup:
    if is_multi:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأكيد وحفظ", callback_data="order_confirm")],
            [
                InlineKeyboardButton("🏪 تعديل الصيدلية", callback_data="order_edit_pharmacy"),
                InlineKeyboardButton("📍 تعديل المنطقة", callback_data="order_edit_area"),
            ],
            [
                InlineKeyboardButton("📝 تعديل الملاحظات", callback_data="order_edit_notes"),
                InlineKeyboardButton("📅 تعديل التاريخ", callback_data="order_edit_date"),
            ],
            [InlineKeyboardButton("❌ إلغاء", callback_data="order_cancel")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد وحفظ", callback_data="order_confirm")],
        [
            InlineKeyboardButton("🏪 تعديل الصيدلية", callback_data="order_edit_pharmacy"),
            InlineKeyboardButton("💊 تعديل المنتج", callback_data="order_edit_product"),
        ],
        [
            InlineKeyboardButton("📍 تعديل المنطقة", callback_data="order_edit_area"),
            InlineKeyboardButton("👨‍⚕️ تعديل الطبيب", callback_data="order_edit_doctor"),
        ],
        [
            InlineKeyboardButton("📦 تعديل الكمية", callback_data="order_edit_quantity"),
            InlineKeyboardButton("🎁 تعديل البونص", callback_data="order_edit_bonus"),
        ],
        [
            InlineKeyboardButton("💵 تعديل السعر", callback_data="order_edit_price"),
            InlineKeyboardButton("💰 تعديل الدفع", callback_data="order_edit_payment"),
        ],
        [
            InlineKeyboardButton("📝 تعديل الملاحظات", callback_data="order_edit_notes"),
            InlineKeyboardButton("📅 تعديل التاريخ", callback_data="order_edit_date"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="order_cancel")],
    ])


def build_payment_pharmacy_keyboard(pharmacies: list) -> InlineKeyboardMarkup:
    """أزرار اختيار الصيدلية عند تسجيل تسديد — الأسماء تُخزّن بـ user_data['pmt_ph_choices']
    والزر يحمل الفهرس فقط (تجنّباً لمشاكل طول callback_data مع الأسماء العربية الطويلة)."""
    rows = []
    for i, name in enumerate(pharmacies[:20]):
        rows.append([InlineKeyboardButton(f"🏪 {name}", callback_data=f"pmt_ph_pick_{i}")])
    rows.append([InlineKeyboardButton("✍️ اكتب اسم صيدلية يدوياً", callback_data="pmt_ph_manual")])
    rows.append([InlineKeyboardButton("🔙 إلغاء", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def build_payment_post_save_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    """أزرار تظهر بعد حفظ التسديد — تعديل أو حذف أو كشف حساب."""
    pid = str(payment_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏪 تعديل الصيدلية", callback_data=f"pmt_pharmacy_{pid}"),
            InlineKeyboardButton("💰 تعديل المبلغ",   callback_data=f"pmt_amount_{pid}"),
        ],
        [
            InlineKeyboardButton("📅 تعديل التاريخ", callback_data=f"pmt_date_{pid}"),
            InlineKeyboardButton("📝 تعديل الملاحظة", callback_data=f"pmt_notes_{pid}"),
        ],
        [
            InlineKeyboardButton("📒 كشف حساب الصيدلية", callback_data=f"pmt_ledger_{pid}"),
        ],
        [
            InlineKeyboardButton("🗑 حذف", callback_data=f"pmt_delete_{pid}"),
            InlineKeyboardButton("🏠 القائمة", callback_data="menu_main"),
        ],
    ])


def build_post_save_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """أزرار تظهر بعد حفظ الطلب مباشرةً — تعديل أو حذف الطلب المحفوظ."""
    oid = str(order_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏪 تعديل الصيدلية",  callback_data=f"ps_pharmacy_{oid}"),
            InlineKeyboardButton("💊 تعديل المنتج",    callback_data=f"ps_product_{oid}"),
        ],
        [
            InlineKeyboardButton("📍 تعديل المنطقة",   callback_data=f"ps_area_{oid}"),
            InlineKeyboardButton("📦 تعديل الكمية",    callback_data=f"ps_quantity_{oid}"),
        ],
        [
            InlineKeyboardButton("💵 تعديل السعر",     callback_data=f"ps_price_{oid}"),
            InlineKeyboardButton("💰 تعديل الدفع",     callback_data=f"ps_payment_{oid}"),
        ],
        [
            InlineKeyboardButton("📝 تعديل الملاحظات", callback_data=f"ps_notes_{oid}"),
            InlineKeyboardButton("📅 تعديل التاريخ",   callback_data=f"ps_date_{oid}"),
        ],
        [
            InlineKeyboardButton("🗑️ حذف الطلب",      callback_data=f"ps_delete_{oid}"),
            InlineKeyboardButton("🏠 القائمة",         callback_data="menu_main"),
        ],
    ])


def build_post_save_multi_keyboard(order_ids: list) -> InlineKeyboardMarkup:
    """أزرار ما بعد حفظ طلب متعدد المنتجات."""
    last_id = str(order_ids[-1]) if order_ids else ""
    rows = []
    if last_id:
        rows.append([InlineKeyboardButton("🗑️ حذف آخر طلب محفوظ", callback_data=f"ps_delete_{last_id}")])
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يرسل دليل الاستخدام عند الضغط على /help."""
    await update.message.reply_text(_build_help_message(), parse_mode=ParseMode.MARKDOWN)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user_id = await run_blocking(get_or_create_user, tg_user.id, tg_user.full_name, tg_user.username)

    # ── فحص التسجيل — إذا المندوب ما أكمل التسجيل نبدأ تدفق إنشاء الحساب ──
    if not await run_blocking(is_user_registered, tg_user.id) and not is_admin(tg_user.id):
        context.user_data.pop("reg_fullname", None)
        context.user_data["waiting_reg_fullname"] = True
        await update.message.reply_text(
            "👋 *أهلاً! يبدو أنك مستخدم جديد.*\n\n"
            "لإنشاء حسابك الخاص، أرسل *اسمك الثلاثي* الآن:\n\n"
            "_(مثال: طه محمد علي)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    sub = check_subscription(user_id)
    name = tg_user.first_name or "مستخدم"

    if sub.get("reason") == "banned":
        await update.message.reply_text(format_banned_message(), parse_mode=ParseMode.MARKDOWN)
        return

    if sub["allowed"] and sub.get("plan") in ("basic", "trial") and not sub.get("soft_locked"):
        msg = format_welcome_trial(name, sub.get("days_left", 30), sub.get("end_date", "—"))
    elif sub["allowed"] and sub.get("soft_locked"):
        reason = sub.get("reason", "")
        if reason == "basic_expired":
            msg = (
                f"👋 أهلاً *{name}*!\n\n"
                + format_expired_basic_message()
            )
        else:
            msg = (
                f"👋 أهلاً *{name}*!\n\n"
                + format_expired_pro_message()
            )
    elif sub["allowed"]:
        plan_name = PLAN_NAMES.get(sub.get("plan"), sub.get("plan", "Pro"))
        days_left = sub.get("days_left")
        days_str = f" — *{days_left} يوم* متبقية" if days_left is not None else ""
        msg = (
            f"👋 أهلاً *{name}*!\n\n"
            f"✅ اشتراكك نشط — *{plan_name}*{days_str}\n"
            f"📅 ينتهي: {sub.get('end_date', '—')}\n\n"
            "اختر من القائمة أو أرسل طلبك مباشرة:"
        )
    else:
        msg = (
            f"👋 أهلاً *{name}*!\n\n"
            "⛔ اشتراكك غير مفعّل.\n"
            "اضغط *ترقية إلى Pro* للاشتراك:"
        )
    # ── رسالة ١: الترحيب والمزايا ───────────────────────────────────────────
    reg_name = await run_blocking(get_user_full_name, user_id) or name
    welcome_msg = (
        f"👋 *أهلاً {reg_name}!*\n\n"
        "🤖 *ما هو هذا البوت؟*\n"
        "بوت ذكي مخصص لمندوبي مبيعات الأدوية — يُسجّل طلباتك، ويُنتج تقارير Excel احترافية، "
        "كل ذلك بالعربية العراقية بشكل طبيعي.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ *المزايا الرئيسية:*\n\n"
        "📦 تسجيل الطلبات بأكثر من طريقة\n"
        "📊 تقارير يومية وشهرية ومقارنة بين الأشهر\n"
        "📁 تصدير Excel جاهز للطباعة\n"
        "🔍 بحث سريع بالصيدلية أو المنتج\n"
        "✏️ تعديل وحذف الطلبات بعد الحفظ\n"
        "♻️ تكرار طلبات سابقة بضغطة واحدة\n"
        "🎯 متابعة أهداف المبيعات لكل منتج\n"
        "🧠 تصحيح أسماء الصيدليات والمنتجات تلقائياً\n"
        "⏱️ حفظ تلقائي للطلب بعد 30 ثانية إذا نسيت\n"
        "👤 اسمك الثلاثي يُسجَّل تلقائياً بكل طلب\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 اضغط /help لمعرفة طريقة الاستخدام بالتفصيل"
    )
    await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN)

    # ── رسالة ٢: دليل الاستخدام (فقط أول مرة) ──────────────────────────────────
    settings = await run_blocking(get_user_settings, user_id)
    if not settings.get("has_seen_help"):
        await update.message.reply_text(
            _build_help_message(),
            parse_mode=ParseMode.MARKDOWN,
        )
        await run_blocking(set_has_seen_help, user_id, True)

    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_main_menu(sub, is_admin_user=is_admin(tg_user.id))
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    tg_user = update.effective_user
    user_id = await run_blocking(get_or_create_user, tg_user.id, tg_user.full_name, tg_user.username)

    async def edit(text: str, markup=None):
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup or build_main_menu(is_admin_user=is_admin(tg_user.id))
        )

    if data == "menu_main":
        await query.edit_message_text(
            "اختر من القائمة أو أرسل طلبك مباشرة:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
        )

    elif data == "menu_reports":
        sub_chk = check_subscription(user_id)
        if not can_use(sub_chk, "reports"):
            await edit(
                "📊 *التقارير*\n\n"
                + (format_expired_basic_message() if sub_chk.get("reason") == "basic_expired"
                   else format_expired_pro_message()),
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
                ]]),
            )
            return
        await edit("📊 *التقارير*\n\nاختر نوع التقرير:", markup=build_reports_menu())

    # ── إضافة بيانات سابقة ───────────────────────────────────────────────────
    elif data == "menu_past_data":
        await edit(
            "📥 *إضافة بيانات سابقة*\n\n"
            "اختر الشهر الذي تريد إضافة بياناته:",
            markup=build_past_month_menu(),
        )

    elif data.startswith("past_month_"):
        parts = data.split("_")
        p_m, p_y = int(parts[2]), int(parts[3])
        months_ar = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
                     "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
        month_name = months_ar[p_m - 1]
        await edit(
            f"📅 *{month_name} {p_y}*\n\n"
            "كيف تريد إضافة البيانات؟",
            markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 استيراد من Excel", callback_data=f"past_excel_{p_m}_{p_y}")],
                [InlineKeyboardButton("✏️ إدخال يدوي",       callback_data=f"past_manual_{p_m}_{p_y}")],
                [InlineKeyboardButton("🔙 رجوع",              callback_data="menu_past_data")],
            ]),
        )

    elif data.startswith("past_excel_"):
        parts = data.split("_")
        p_m, p_y = int(parts[2]), int(parts[3])
        months_ar = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
                     "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
        month_name = months_ar[p_m - 1]
        try:
            catalog = await run_blocking(get_product_catalog, user_id)
            settings = await run_blocking(get_user_settings, user_id)
            rep_name = settings.get("rep_name_memory") or await run_blocking(get_user_full_name, tg_user.id) or tg_user.full_name or "مندوب"
            if catalog:
                buf = generate_catalog_import_template(catalog, rep_name, p_m, p_y)
                caption = (
                    f"📥 *قالب الاستيراد — {month_name} {p_y}*\n\n"
                    "✅ القالب مخصص لك — يحوي أدويتك بأسعارها\n\n"
                    "1️⃣ افتح شيت *البيانات*\n"
                    "2️⃣ كل سطر = صيدلية، واكتب الكميات في أعمدة الأدوية\n"
                    "3️⃣ اترك الخلية فارغة إذا لم تبع الدواء في هذه الزيارة\n"
                    "4️⃣ ارسل الملف هنا بعد التعبئة\n\n"
                    "📋 شيت *تعليمات* فيه شرح كامل"
                )
            else:
                buf = generate_import_template()
                caption = (
                    f"📥 *قالب — {month_name} {p_y}*\n\n"
                    "1️⃣ افتح الملف واملأ بياناتك في شيت *البيانات*\n"
                    "2️⃣ كل سطر = صيدلية + منتج واحد\n"
                    "3️⃣ الأرقام إنجليزي (10) أو عربي (١٠) — كلاهما مقبول\n"
                    "4️⃣ ارسل الملف هنا وسيُحفظ تلقائياً لهذا الشهر\n\n"
                    "📋 شيت *تعليمات* فيه شرح كامل"
                )
            await query.message.reply_document(
                document=buf,
                filename=f"قالب_{month_name}_{p_y}.xlsx",
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            # ضبط الشهر مسبقاً — لن نسأل مجدداً
            context.user_data["import_month_preset"] = (p_m, p_y)
            await query.answer("تم إرسال القالب ✅")
        except Exception as e:
            logger.error(f"past_excel error: {e}")
            await query.answer("❌ حدث خطأ")

    elif data.startswith("past_manual_"):
        parts = data.split("_")
        p_m, p_y = int(parts[2]), int(parts[3])
        months_ar = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
                     "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
        month_name = months_ar[p_m - 1]
        context.user_data["past_month"] = p_m
        context.user_data["past_year"]  = p_y
        await edit(
            f"✏️ *وضع الإدخال اليدوي — {month_name} {p_y}*\n\n"
            "الآن أرسل طلباتك بشكل عادي.\n"
            f"كل طلب سيُحفظ تلقائياً لشهر *{month_name} {p_y}*.\n\n"
            "📌 *مثال:*\n"
            f"`5/{p_m} صيدلية الشفاء Maxiflex 10`\n\n"
            "_لإنهاء هذا الوضع اضغط /menu_",
            markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 إلغاء ورجوع", callback_data="menu_main")
            ]]),
        )

    elif data == "do_report_today":
        await query.edit_message_text("⏳ جاري تجهيز تقرير اليوم...")
        try:
            orders = await run_blocking(fetch_orders_today, user_id)
            if not orders:
                await query.message.reply_text("📭 لا توجد طلبات اليوم.", reply_markup=build_reports_menu())
            else:
                summary = build_text_summary(orders, "daily")
                await query.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=build_reports_menu())
                today = date.today()
                buf = await run_blocking(_generate_rep_template_excel, user_id, orders, today.month, today.year)
                fname = f"تقرير_اليوم_{today.isoformat()}.xlsx"
                await query.message.reply_document(
                    document=buf,
                    filename=fname,
                    caption="📊 Excel تقرير اليوم (بصيغة التاركت)",
                )
        except Exception as e:
            logger.error(f"Report today error: {e}")
            await query.message.reply_text("❌ حدث خطأ.", reply_markup=build_reports_menu())

    elif data == "do_report_month":
        await query.edit_message_text("⏳ جاري تجهيز تقرير الشهر...")
        try:
            today = date.today()
            report_filters = {"month": today.month, "year": today.year}
            orders = await run_blocking(fetch_orders, user_id, report_filters)
            if not orders:
                await query.message.reply_text("📭 لا توجد بيانات هذا الشهر.", reply_markup=build_reports_menu())
            else:
                summary = build_text_summary(orders, "general")
                await query.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=build_reports_menu())
                buf = await run_blocking(_generate_rep_template_excel, user_id, orders, today.month, today.year)
                fname = f"تقرير_{today.year}_{today.month:02d}.xlsx"
                await query.message.reply_document(
                    document=buf,
                    filename=fname,
                    caption="📊 تقرير Excel الكامل (بصيغة التاركت)",
                )
                await _notify_admin(
                    context,
                    f"📊 تقرير {tg_user.full_name} — {today.year}/{today.month:02d}",
                    excel=buf,
                    filename=fname,
                )
        except Exception as e:
            logger.error(f"Report month error: {e}")
            await query.message.reply_text("❌ حدث خطأ.", reply_markup=build_reports_menu())

    elif data == "do_report_pick_month":
        context.user_data["waiting_report_month"] = True
        months_ar = {
            1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
            7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"
        }
        today = date.today()
        hint_lines = "\n".join(
            f"• `{m}` — {name}" for m, name in months_ar.items() if m <= today.month
        )
        await query.edit_message_text(
            "🗓 *اختر الشهر*\n\n"
            "أرسل *رقم الشهر* الذي تريد تقريره:\n\n"
            f"{hint_lines}\n\n"
            "_يمكنك أيضاً كتابة السنة: مثال_ `3 2025`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="menu_reports")
            ]]),
        )

    elif data == "do_import_template":
        # ── إرسال قالب Excel للمندوب ──────────────────────────────────────
        try:
            today = date.today()
            catalog = await run_blocking(get_product_catalog, user_id)
            settings = await run_blocking(get_user_settings, user_id)
            rep_name = settings.get("rep_name_memory") or await run_blocking(get_user_full_name, tg_user.id) or tg_user.full_name or "مندوب"
            if catalog:
                buf = generate_catalog_import_template(catalog, rep_name, today.month, today.year)
                caption = (
                    "📥 *قالب الاستيراد*\n\n"
                    "✅ القالب مخصص لك — يحوي أدويتك بأسعارها\n\n"
                    "1️⃣ افتح شيت *البيانات*\n"
                    "2️⃣ كل سطر = صيدلية، واكتب الكميات في أعمدة الأدوية\n"
                    "3️⃣ اترك الخلية فارغة إذا لم تبع الدواء في هذه الزيارة\n"
                    "4️⃣ ارسل الملف هنا بعد التعبئة\n\n"
                    "📋 شيت *تعليمات* فيه شرح كامل"
                )
            else:
                buf = generate_import_template()
                caption = (
                    "📥 *قالب استيراد البيانات*\n\n"
                    "1️⃣ افتح الملف واملأ بياناتك في شيت *البيانات*\n"
                    "2️⃣ كل سطر = صيدلية واحدة + منتج واحد\n"
                    "3️⃣ الأرقام: إنجليزي (10) أو عربي (١٠) — كلاهما مقبول\n"
                    "4️⃣ ارسل الملف هنا بعد التعبئة\n\n"
                    "📋 شيت *تعليمات* فيه شرح كامل"
                )
            await query.message.reply_document(
                document=buf,
                filename="قالب_الاستيراد.xlsx",
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            await query.answer("تم إرسال القالب ✅")
        except Exception as e:
            logger.error(f"Import template error: {e}")
            await query.answer("❌ حدث خطأ في إنشاء القالب")

    elif data == "confirm_import":
        # ── تأكيد حفظ الطلبات المستوردة ──────────────────────────────────
        orders = context.user_data.pop("import_orders", [])
        if not orders:
            await query.edit_message_text("⚠️ لا توجد بيانات للحفظ.")
            return
        try:
            saved = await run_blocking(bulk_save_orders, user_id, orders)
            months_set = sorted(set(f"{o['year']}/{o['month']:02d}" for o in orders))
            months_str = "، ".join(months_set)
            await query.edit_message_text(
                f"✅ *تم حفظ {saved} طلب بنجاح!*\n\n"
                f"📅 الفترة: *{months_str}*\n"
                f"🏥 الصيدليات: *{len(set(o['pharmacy'] for o in orders))}*\n\n"
                "يمكنك الآن رؤية الطلبات في تقاريرك.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_reports_menu(),
            )
        except Exception as e:
            logger.error(f"Import confirm error: {e}")
            await query.edit_message_text(f"❌ خطأ في الحفظ: {e}", reply_markup=build_reports_menu())

    elif data == "cancel_import":
        context.user_data.pop("import_orders", None)
        context.user_data.pop("import_file_id", None)
        await query.edit_message_text(
            "❌ تم إلغاء الاستيراد.",
            reply_markup=build_reports_menu(),
        )

    elif data == "do_report_ranking":
        try:
            ranking = await run_blocking(get_rep_ranking, user_id)
            if not ranking:
                await query.message.reply_text("📭 لا توجد بيانات كافية.", reply_markup=build_reports_menu())
            else:
                lines = ["🏆 *ترتيب المناديب:*\n"]
                for i, r in enumerate(ranking, 1):
                    lines.append(f"{i}. {r.get('rep_name', '—')} — {r.get('total_orders', 0)} طلب")
                await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=build_reports_menu())
        except Exception as e:
            logger.error(f"Ranking error: {e}")
            await query.message.reply_text("❌ حدث خطأ.", reply_markup=build_reports_menu())

    elif data == "menu_alerts":
        sub_chk = check_subscription(user_id)
        if not can_use(sub_chk, "alerts"):
            await edit(
                "🔔 *التنبيهات*\n\n🔒 هذه الميزة متاحة فقط لمشتركي Pro.",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
                ]]),
            )
            return
        settings = await run_blocking(get_user_settings, user_id)
        await edit("🔔 *التنبيهات الذكية*\n\nاضغط لتفعيل/إيقاف:", markup=build_alerts_menu(settings))

    elif data in ("toggle_inactivity", "toggle_unvisited", "toggle_daily", "toggle_weekly"):
        key_map = {
            "toggle_inactivity": "alert_inactivity",
            "toggle_unvisited": "alert_unvisited",
            "toggle_daily": "daily_summary",
            "toggle_weekly": "weekly_report",
        }
        await run_blocking(toggle_setting, user_id, key_map[data])
        settings = await run_blocking(get_user_settings, user_id)
        await query.edit_message_text(
            "🔔 *التنبيهات الذكية*\n\nاضغط لتفعيل/إيقاف:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_alerts_menu(settings),
        )

    elif data == "menu_settings":
        await edit("⚙️ *الإعدادات*\n\nاختر:", markup=build_settings_menu())

    elif data == "set_name_prompt":
        context.user_data["waiting_for_name"] = True
        await edit("👤 أرسل اسمك الآن وسيتم حفظه تلقائياً في كل طلب.\n\nمثال: `طه محمد`")

    elif data == "do_delete_last":
        last = await run_blocking(get_last_order, user_id)
        if not last:
            await query.message.reply_text("⚠️ لا يوجد طلب سابق للحذف.", reply_markup=build_settings_menu())
        else:
            await run_blocking(delete_order, last["id"])
            await query.message.reply_text("🗑️ تم حذف آخر طلب بنجاح.", reply_markup=build_settings_menu())

    elif data == "menu_activate":
        context.user_data["waiting_for_code"] = True
        await edit(
            "🔑 *تفعيل الاشتراك*\n\n"
            "أرسل كود التفعيل الآن.\n"
            "مثال: `ABC123`\n\n"
            "_ستجد الكود من المسؤول أو في رسالة الدفع._"
        )

    # ── طلب الاشتراك Pro ────────────────────────────────────────────────────
    elif data == "menu_subscribe":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"💳 شهري — {PRICES['monthly']}", callback_data="subscribe_monthly"),
                InlineKeyboardButton(f"🌟 سنوي — {PRICES['yearly']}", callback_data="subscribe_yearly"),
            ],
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_main")],
        ])
        await edit(
            "🚀 *الترقية إلى Pro*\n\n"
            f"💎 *الخطة الشهرية:* {PRICES['monthly']}\n"
            f"🌟 *الخطة السنوية:* {PRICES['yearly']}\n\n"
            "📦 *ما تحصل عليه:*\n"
            "✅ تقارير ذكية ومقارنة شهرية\n"
            "✅ بحث داخل بياناتك\n"
            "✅ ملف الصيدليات\n"
            "✅ تنبيهات الأداء\n"
            "✅ Excel تفصيلي\n\n"
            "اختر الخطة:",
            markup=kb,
        )

    elif data in ("subscribe_monthly", "subscribe_yearly"):
        plan_type = data.split("_")[1]
        label = "شهري" if plan_type == "monthly" else "سنوي"
        amount = PRICES[plan_type]

        # احفظ الخطة المطلوبة في الجلسة
        context.user_data["pending_sub_plan"] = plan_type

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data="menu_subscribe")],
        ])
        await edit(
            f"💳 *طريقة الدفع — اشتراك {label}*\n\n"
            f"المبلغ: *{amount}*\n\n"
            f"*حوّل المبلغ على:*\n"
            f"🏦 QI CARD — رقم الحساب:\n"
            f"`{QI_CARD_NUMBER}`\n\n"
            f"📸 *بعد التحويل:*\n"
            f"أرسل صورة الفاتورة (لقطة شاشة) مباشرةً هنا\n"
            f"وسيقوم الأدمن بتفعيل اشتراكك خلال وقت قصير.",
            markup=kb,
        )

    elif data == "help_order":
        tmpl = await run_blocking(get_user_template, user_id)
        if tmpl:
            field_labels_ordered = "\n".join(
                f"  {i+1}. {FIELD_LABELS.get(f, f)}" for i, f in enumerate(tmpl)
            )
            context.user_data["guided_order"] = {"fields": tmpl, "current_idx": 0, "data": {}}
            await query.message.reply_text(
                f"📝 *إدخال طلب جديد*\n\n"
                f"*الطريقة السريعة:* أرسل كل الحقول دفعةً واحدة — كل حقل في سطر بهذا الترتيب:\n"
                f"{field_labels_ordered}\n\n"
                f"*أو* أرسل الحقل الأول فقط وسأسألك واحداً واحداً:\n"
                f"أرسل *{FIELD_LABELS.get(tmpl[0], tmpl[0])}:*\n"
                f"_(«تخطي» لترك الحقل فارغاً — «إلغاء» للخروج)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await edit(
                "📝 *تسجيل طلب*\n\n"
                "أرسل بيانات الطلب بأي شكل عربي، مثال:\n\n"
                "`صيدلية الرازي — pirocomplex 10 حبة — 50,000 آجل`\n\n"
                "💡 *نصيحة:* صمّم قالباً من ⚙️ الإعدادات لإدخال دقيق حقل بحقل."
            )

    elif data == "help_template":
        selected = context.user_data.get("template_building", [])
        if not selected:
            existing = await run_blocking(get_user_template, user_id)
            if existing:
                selected = list(existing)
                context.user_data["template_building"] = selected
        await query.edit_message_text(
            "📋 *تصميم القالب الشخصي*\n\n"
            "اضغط على الحقل لإضافته أو إزالته — يظهر برقم ترتيبه عند الاختيار.\n"
            "بعد الحفظ، البوت سيسألك عن كل حقل بالترتيب عند إدخال طلب:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_template_builder(selected),
        )

    elif data.startswith("tmpl_") and data not in ("tmpl_save", "tmpl_clear", "tmpl_delete"):
        field_key = data[5:]
        if field_key in FIELD_LABELS:
            selected = context.user_data.get("template_building", [])
            if field_key in selected:
                selected.remove(field_key)
            else:
                selected.append(field_key)
            context.user_data["template_building"] = selected
            await query.edit_message_text(
                "📋 *تصميم القالب الشخصي*\n\n"
                "اضغط على الحقل لإضافته أو إزالته — يظهر برقم ترتيبه عند الاختيار.\n"
                "بعد الحفظ، البوت سيسألك عن كل حقل بالترتيب عند إدخال طلب:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_template_builder(selected),
            )

    elif data == "tmpl_clear":
        context.user_data["template_building"] = []
        await query.edit_message_text(
            "📋 *تصميم القالب الشخصي*\n\n"
            "تم مسح الاختيارات. ابدأ من جديد:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_template_builder([]),
        )

    elif data == "tmpl_save":
        selected = context.user_data.get("template_building", [])
        if len(selected) < 2:
            await query.answer("⚠️ اختر حقلين على الأقل", show_alert=True)
            return
        await run_blocking(save_user_template, user_id, selected)
        context.user_data.pop("template_building", None)
        labels = " ← ".join(FIELD_LABELS.get(f, f) for f in selected)
        await edit(
            f"✅ *تم حفظ القالب:*\n\n{labels}\n\n"
            "الآن عند الضغط على 📝 تسجيل طلب، سيسألك البوت عن كل حقل بالترتيب.",
            markup=build_settings_menu(),
        )

    elif data == "tmpl_delete":
        await run_blocking(delete_user_template, user_id)
        context.user_data.pop("template_building", None)
        await edit("🗑️ تم حذف القالب. البوت سيعود للكشف التلقائي.", markup=build_settings_menu())

    elif data == "do_performance":
        sub_chk = check_subscription(user_id)
        if not can_use(sub_chk, "reports"):
            await edit(
                "📈 *أدائي*\n\n🔒 هذه الميزة متاحة فقط لمشتركي Pro.",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
                ]]),
            )
            return
        try:
            stats = await run_blocking(get_performance_stats, user_id)
            months_ar = ["", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
                         "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
            curr_name = months_ar[stats["curr_month"]]
            prev_name = months_ar[stats["prev_month"]]
            arrow = "📈" if stats["change_pct"] >= 0 else "📉"
            change_str = f"+{stats['change_pct']}%" if stats["change_pct"] >= 0 else f"{stats['change_pct']}%"
            msg = (
                f"📊 *أدائي — {curr_name}*\n\n"
                f"📦 الطلبات: *{stats['curr_orders']}* طلب\n"
                f"🔢 الكميات: *{stats['curr_qty']}* وحدة\n"
                f"🏥 الصيدليات المزارة: *{stats['pharmacies_count']}*\n"
                f"💊 أكثر منتج: *{stats['top_product']}*\n\n"
                f"📅 مقارنة بـ {prev_name}: {arrow} *{change_str}*\n"
                f"_(كان {stats['prev_orders']} طلب، {stats['prev_qty']} وحدة)_"
            )
            if stats["curr_orders"] == 0:
                msg = f"📊 *أدائي — {curr_name}*\n\n📭 لا توجد طلبات هذا الشهر بعد."
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=build_reports_menu())
        except Exception as e:
            logger.error(f"Performance error: {e}")
            await query.message.reply_text("❌ حدث خطأ.", reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)))

    elif data == "do_delete_all_confirm":
        await query.edit_message_text(
            "⚠️ *تحذير!*\n\n"
            "سيتم حذف *جميع طلباتك* نهائياً ولا يمكن التراجع.\n\n"
            "هل أنت متأكد؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ نعم، احذف كل شيء", callback_data="do_delete_all_execute"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="menu_settings"),
                ]
            ]),
        )

    elif data == "do_delete_all_execute":
        deleted = await run_blocking(delete_all_orders, user_id)
        await query.edit_message_text(
            f"🗑️ تم حذف *{deleted}* طلب بنجاح.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_settings_menu(),
        )

    elif data == "do_delete_month_pick":
        months_ar = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
                     "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
        today = datetime.now()
        rows = []
        row = []
        for i in range(12):
            m = today.month - i
            y = today.year
            if m <= 0:
                m += 12
                y -= 1
            btn = InlineKeyboardButton(
                f"{months_ar[m-1]} {y}",
                callback_data=f"del_month_{m}_{y}",
            )
            row.append(btn)
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings")])
        await query.edit_message_text(
            "📅 *حذف مبيعات شهر معين*\n\nاختر الشهر الذي تريد حذف بياناته:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("del_month_exec_"):
        parts = data.split("_")
        m, y = int(parts[3]), int(parts[4])
        months_ar = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
                     "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
        month_name = months_ar[m-1]
        deleted = await run_blocking(delete_orders_by_month, user_id, m, y)
        await query.edit_message_text(
            f"🗑️ تم حذف *{deleted}* طلب من شهر *{month_name} {y}* بنجاح.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_settings_menu(),
        )

    elif data.startswith("del_month_"):
        parts = data.split("_")
        m, y = int(parts[2]), int(parts[3])
        months_ar = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
                     "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
        month_name = months_ar[m-1]
        await query.edit_message_text(
            f"⚠️ *تحذير!*\n\n"
            f"سيتم حذف *جميع مبيعات {month_name} {y}* نهائياً ولا يمكن التراجع.\n\n"
            f"هل أنت متأكد؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ نعم، احذف {month_name}", callback_data=f"del_month_exec_{m}_{y}"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="do_delete_month_pick"),
                ]
            ]),
        )

    elif data == "help_search":
        sub_chk = check_subscription(user_id)
        if not can_use(sub_chk, "search"):
            await edit(
                "🔍 *البحث الذكي*\n\n🔒 هذه الميزة متاحة فقط لمشتركي Pro.",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
                ]]),
            )
            return
        context.user_data["waiting_for_search"] = True
        await query.edit_message_text(
            "🔍 *البحث الذكي*\n\n"
            "اكتب *أي اسم* وأنا أجيب لك كل الأوردرات المرتبطة:\n\n"
            "أرسل أي كلمة تريد البحث عنها:\n\n"
            "• اسم *منطقة* مثل: `الاورزدي` ← يعرض صيدلياتها\n"
            "• اسم *منتج* مثل: `trazola` ← يعرض طلباته\n"
            "• اسم *صيدلية* مثل: `صيدلية الاماني` ← يعرض زياراتها\n"
            "• مع شهر مثل: `الاورزدي مايو`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_main")
            ]]),
        )

    elif data in ("help_pharmacy", "help_compare"):
        sub_chk = check_subscription(user_id)
        if data == "help_pharmacy" and not can_use(sub_chk, "pharmacy"):
            await edit(
                "🏥 *ملف الصيدلية*\n\n🔒 هذه الميزة متاحة فقط لمشتركي Pro.",
                markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
                ]]),
            )
            return
        tips = {
            "help_pharmacy": (
                "🏥 *ملف الصيدلية*\n\n"
                "اكتب: `ملف صيدلية الزهراء`\n"
                "لتحصل على كامل تاريخ الطلبات والإحصائيات."
            ),
            "help_compare": (
                "📊 *مقارنة شهرين*\n\nمثال:\n"
                "`قارن مايو بأبريل`"
            ),
        }
        await edit(tips[data])

    # ── كتالوج الأدوية ───────────────────────────────────────────────────────
    elif data == "catalog_menu":
        await _show_catalog_menu(query, user_id)

    elif data == "catalog_add":
        context.user_data["waiting_catalog_add"] = True
        await query.edit_message_text(
            "💊 *إضافة دواء إلى الكتالوج*\n\n"
            "أرسل اسم الدواء وسعره بهذا الشكل:\n\n"
            "`Trazola - 12000`\n"
            "`Pirocomplex 5000`\n"
            "`Maxiflex / 8500`\n\n"
            "_(السعر بالدينار العراقي — أرسل «إلغاء» للخروج)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="catalog_menu")
            ]]),
        )

    elif data == "catalog_clear_confirm":
        await query.edit_message_text(
            "⚠️ *تحذير!*\n\nسيتم حذف جميع الأدوية من الكتالوج.\nهل أنت متأكد؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ نعم، احذف الكل", callback_data="catalog_clear_do"),
                    InlineKeyboardButton("❌ إلغاء", callback_data="catalog_menu"),
                ]
            ]),
        )

    elif data == "catalog_clear_do":
        await run_blocking(clear_product_catalog, user_id)
        await _show_catalog_menu(query, user_id, msg="🗑️ تم حذف كل الأدوية.")

    elif data == "catalog_del_pick":
        catalog = await run_blocking(get_product_catalog, user_id)
        if not catalog:
            await query.answer("الكتالوج فارغ.", show_alert=True)
            return
        btns = [
            [InlineKeyboardButton(
                f"🗑 {d['drug_name']}",
                callback_data=f"catdel_{d['id']}"
            )]
            for d in catalog
        ]
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="catalog_menu")])
        await query.edit_message_text(
            "اختر الدواء الذي تريد حذفه:",
            reply_markup=InlineKeyboardMarkup(btns)
        )

    elif data == "catalog_price_pick":
        catalog = await run_blocking(get_product_catalog, user_id)
        if not catalog:
            await query.answer("الكتالوج فارغ.", show_alert=True)
            return
        btns = []
        for d in catalog:
            price_label = f"{int(d['price']):,}" if d['price'] else "—"
            btns.append([InlineKeyboardButton(
                f"✏️ {d['drug_name']} — {price_label} IQD",
                callback_data=f"catprice_{d['id']}"
            )])
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="catalog_menu")])
        await query.edit_message_text(
            "اختر الدواء الذي تريد تعديل سعره:",
            reply_markup=InlineKeyboardMarkup(btns)
        )

    elif data.startswith("catprice_"):
        product_id = int(data.split("_", 1)[1])
        catalog = await run_blocking(get_product_catalog, user_id)
        drug = next((d for d in catalog if d["id"] == product_id), None)
        if not drug:
            await query.answer("الدواء غير موجود.", show_alert=True)
            return
        context.user_data["waiting_catalog_price_edit"] = product_id
        cur_price = f"{int(drug['price']):,}" if drug["price"] else "—"
        await query.edit_message_text(
            f"✏️ *تعديل سعر:* {drug['drug_name']}\n"
            f"السعر الحالي: {cur_price} IQD\n\n"
            f"أرسل السعر الجديد (رقم فقط)، أو *إلغاء* للتراجع.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data.startswith("catdel_"):
        product_id = int(data.split("_", 1)[1])
        await run_blocking(delete_product_from_catalog, product_id, user_id)
        await _show_catalog_menu(query, user_id, msg="✅ تم حذف الدواء.")

    elif data == "catalog_excel":
        today = date.today()
        await _send_catalog_excel(query, context, user_id, today.month, today.year)

    elif data.startswith("catalog_excel_"):
        parts = data.split("_")
        month_val = int(parts[2])
        year_val  = int(parts[3])
        await _send_catalog_excel(query, context, user_id, month_val, year_val)

    elif data == "catalog_pick_month":
        today = date.today()
        rows = []
        row = []
        from parser import MONTH_NUM_TO_AR
        for m in range(1, 13):
            yr = today.year if m <= today.month else today.year - 1
            row.append(InlineKeyboardButton(
                MONTH_NUM_TO_AR.get(m, str(m)),
                callback_data=f"catalog_excel_{m}_{yr}"
            ))
            if len(row) == 3:
                rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="catalog_menu")])
        await query.edit_message_text(
            "📊 اختر الشهر لإنشاء Excel:",
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ── التاركت الشهري ──────────────────────────────────────────────────────
    elif data == "tgt_menu":
        await _show_target_menu(query, user_id)

    elif data.startswith("tgt_drug_"):
        try:
            drug_idx = int(data.split("_")[-1])
        except ValueError:
            await query.answer("خطأ", show_alert=True)
            return
        catalog = await run_blocking(get_product_catalog, user_id)
        if drug_idx >= len(catalog):
            await query.answer("الدواء غير موجود", show_alert=True)
            return
        drug = catalog[drug_idx]
        today = date.today()
        period = f"{today.year}-{today.month:02d}"
        current_targets = await run_blocking(get_drug_targets, user_id, period)
        current_tgt = current_targets.get(drug["drug_name"], 0)
        context.user_data["waiting_target_drug"] = drug["drug_name"]
        context.user_data["waiting_target_period"] = period
        await query.edit_message_text(
            f"🎯 *تحديد التاركت*\n\n"
            f"💊 الدواء: *{drug['drug_name']}*\n"
            f"📅 الفترة: *{today.month}/{today.year}*\n"
            f"🎯 التاركت الحالي: *{current_tgt:,} وحدة*\n\n"
            "أرسل العدد المستهدف من الوحدات:\n"
            "_(مثال: 500)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="tgt_menu")
            ]]),
        )

    # ── مراجعة الطلب قبل الحفظ ──────────────────────────────────────────────
    elif data == "order_confirm":
        chat_id = query.message.chat_id
        # إلغاء الحفظ التلقائي لأن المندوب ضغط حفظ يدوياً
        _cancel_autosave(context, chat_id)

        review = context.user_data.get("review_order")
        review_multi = context.user_data.get("review_multi")
        settings_r = context.user_data.get("review_settings", {})
        rep_name_account = await _get_order_rep_name(user_id, tg_user)

        if review_multi:
            base_data = review_multi["base_data"]
            base_data["rep_name"] = rep_name_account
            products = review_multi["products"]
            order_ids = []
            general_note = base_data.get("notes")
            for prod in products:
                order_data = {**base_data, **prod}
                item_note = prod.get("notes")
                if general_note and item_note:
                    order_data["notes"] = f"{general_note} | {item_note}"
                else:
                    order_data["notes"] = item_note or general_note
                oid = await run_blocking(save_order, user_id, order_data)
                order_ids.append(oid)
            context.user_data.pop("review_multi", None)
            context.user_data.pop("review_settings", None)
            ids_str = ", ".join(f"#{i}" for i in order_ids if i)
            reply_txt = (
                f"✅ *تم حفظ {len(products)} منتج بنجاح!*\n"
                f"🏪 {base_data.get('pharmacy','—')}\n"
                + (f"🔢 أرقام الطلبات: {ids_str}" if ids_str else "")
            )
            await query.edit_message_text(reply_txt, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_post_save_multi_keyboard(order_ids))
            admin_msg = f"📋 *طلب متعدد من {rep_name_account}*\n\n{reply_txt}"
            await _notify_admin(context, admin_msg)
            if settings_r.get("notify_manager") and settings_r.get("manager_telegram_id"):
                try:
                    await context.bot.send_message(chat_id=settings_r["manager_telegram_id"],
                                                   text=f"🔔 {admin_msg}", parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass
        elif review:
            review["rep_name"] = rep_name_account
            order_id = await run_blocking(save_order, user_id, review)
            context.user_data.pop("review_order", None)
            context.user_data.pop("review_settings", None)
            id_line = f"\n🔢 *رقم الطلب:* #{order_id}" if order_id else ""
            reply_txt = format_order_confirmation(review) + id_line
            post_kbd = build_post_save_keyboard(order_id) if order_id else build_main_menu(is_admin_user=is_admin(tg_user.id))
            await query.edit_message_text(reply_txt, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=post_kbd)
            admin_msg = f"📋 *طلب جديد من {rep_name_account}*\n\n{reply_txt}"
            await _notify_admin(context, admin_msg)
            if settings_r.get("notify_manager") and settings_r.get("manager_telegram_id"):
                try:
                    await context.bot.send_message(chat_id=settings_r["manager_telegram_id"],
                                                   text=f"🔔 {admin_msg}", parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass
        else:
            await query.edit_message_text("⚠️ انتهت صلاحية الطلب. أعد إرساله.",
                                          reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)))

    elif data == "order_cancel":
        chat_id = query.message.chat_id
        _cancel_autosave(context, chat_id)
        context.user_data.pop("review_order", None)
        context.user_data.pop("review_multi", None)
        context.user_data.pop("review_settings", None)
        context.user_data.pop("awaiting_field", None)
        await query.edit_message_text("❌ تم إلغاء الطلب.",
                                      reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)))

    # ── تعديل / حذف طلب محفوظ (أزرار ما بعد الحفظ) ────────────────────────
    elif data.startswith("ps_delete_"):
        try:
            oid = int(data.split("_", 2)[2])
        except (IndexError, ValueError):
            await query.answer("⚠️ رقم طلب غير صالح", show_alert=True)
            return
        try:
            order_row = await run_blocking(get_order_by_id, oid)
        except Exception:
            order_row = None
        ph   = (order_row or {}).get("pharmacy", "—")
        item = (order_row or {}).get("item_name", "—")
        try:
            await run_blocking(delete_order, oid)
        except Exception as _del_err:
            logger.error(f"delete_order failed oid={oid}: {_del_err}")
            await query.answer(f"❌ فشل الحذف: {_del_err}", show_alert=True)
            return
        success_text = (
            f"🗑️ *تم حذف الطلب #{oid} بنجاح.*\n\n"
            f"🏪 {ph} — 💊 {item}"
        )
        try:
            await query.edit_message_text(
                success_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
            )
        except Exception:
            # إذا فشل تعديل الرسالة (مثلاً قديمة جداً) → أرسل رسالة جديدة
            await query.answer("🗑️ تم الحذف بنجاح", show_alert=True)
            await query.message.reply_text(
                success_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
            )

    elif data.startswith("ps_"):
        # ps_<field>_<order_id>  مثال: ps_pharmacy_42
        parts = data.split("_", 2)
        if len(parts) < 3:
            await query.answer("⚠️ بيانات غير صالحة", show_alert=True)
            return
        field_slug = parts[1]
        try:
            oid = int(parts[2])
        except ValueError:
            await query.answer("⚠️ رقم طلب غير صالح", show_alert=True)
            return

        ps_field_map = {
            "pharmacy": ("pharmacy",      "🏪 أرسل اسم الصيدلية الجديد:"),
            "product":  ("item_name",     "💊 أرسل اسم المنتج والكمية (مثال: neogram 10+3):"),
            "area":     ("area",          "📍 أرسل اسم المنطقة الجديدة:"),
            "quantity": ("quantity",      "📦 أرسل الكمية الجديدة (مثال: 10):"),
            "price":    ("price",         "💵 أرسل السعر الجديد:"),
            "payment":  ("payment_type",  "💰 أرسل نوع الدفع (كاش / آجل / مضمون):"),
            "notes":    ("notes",         "📝 أرسل الملاحظات الجديدة:"),
            "date":     ("order_date",    "📅 أرسل التاريخ الجديد (YYYY-MM-DD):"),
        }
        if field_slug not in ps_field_map:
            await query.answer("⚠️ حقل غير معروف", show_alert=True)
            return
        db_field, prompt = ps_field_map[field_slug]
        context.user_data["ps_awaiting"] = {"order_id": oid, "field": db_field}
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 إلغاء", callback_data="menu_main")
            ]]),
        )

    # ── التسديدات (تحصيل من الصيدليات) ───────────────────────────────────────
    elif data == "do_payment_start":
        pharmacies = await run_blocking(get_known_pharmacies, user_id)
        context.user_data["pmt_ph_choices"] = pharmacies
        if pharmacies:
            await query.edit_message_text(
                "💰 *تسجيل تسديد*\n\nاختر الصيدلية:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_payment_pharmacy_keyboard(pharmacies),
            )
        else:
            context.user_data["pmt_awaiting_pharmacy_manual"] = True
            await query.edit_message_text(
                "💰 *تسجيل تسديد*\n\nما عندك صيدليات مسجّلة بعد. أرسل *اسم الصيدلية*:",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif data == "pmt_ph_manual":
        context.user_data["pmt_awaiting_pharmacy_manual"] = True
        await query.edit_message_text(
            "✍️ أرسل *اسم الصيدلية*:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 إلغاء", callback_data="menu_main")
            ]]),
        )

    elif data.startswith("pmt_ph_pick_"):
        idx = int(data.split("_")[-1])
        choices = context.user_data.get("pmt_ph_choices") or []
        if idx >= len(choices):
            await query.answer("⚠️ منتهي الصلاحية، ابدأ من جديد", show_alert=True)
            return
        pharmacy_name = choices[idx]
        pending_amount = context.user_data.pop("pmt_new_amount", None)
        context.user_data.pop("pmt_awaiting_pharmacy_confirm_raw", None)
        if pending_amount:
            notes = context.user_data.pop("pmt_new_notes", None)
            pid = await run_blocking(save_payment, user_id, tg_user.full_name, pharmacy_name, pending_amount,
                                      None, "cash", notes)
            await query.edit_message_text(
                f"✅ *تم تسجيل التسديد!*\n\n🏪 الصيدلية: {pharmacy_name}\n💰 المبلغ: {pending_amount:,.0f} د.ع",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_payment_post_save_keyboard(pid),
            )
            return
        context.user_data["pmt_new_pharmacy"] = pharmacy_name
        context.user_data["pmt_awaiting_amount"] = True
        await query.edit_message_text(
            f"🏪 *{pharmacy_name}*\n\n💰 أرسل *المبلغ* (مثال: 50 أو خمسين الف أو مليون ونص):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 إلغاء", callback_data="menu_main")
            ]]),
        )

    elif data == "pmt_ph_use_raw":
        pharmacy_name = context.user_data.pop("pmt_awaiting_pharmacy_confirm_raw", None)
        pending_amount = context.user_data.pop("pmt_new_amount", None)
        if not pharmacy_name or not pending_amount:
            await query.answer("⚠️ منتهي الصلاحية، ابدأ من جديد", show_alert=True)
            return
        notes = context.user_data.pop("pmt_new_notes", None)
        pid = await run_blocking(save_payment, user_id, tg_user.full_name, pharmacy_name, pending_amount,
                                  None, "cash", notes)
        await query.edit_message_text(
            f"✅ *تم تسجيل التسديد!*\n\n🏪 الصيدلية: {pharmacy_name}\n💰 المبلغ: {pending_amount:,.0f} د.ع",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_payment_post_save_keyboard(pid),
        )

    elif data.startswith("pmt_pharmacy_") or data.startswith("pmt_amount_") \
            or data.startswith("pmt_date_") or data.startswith("pmt_notes_"):
        field_slug, pid_str = data.rsplit("_", 1)
        try:
            pid = int(pid_str)
        except ValueError:
            await query.answer("⚠️ رقم غير صالح", show_alert=True)
            return
        pmt_field_map = {
            "pmt_pharmacy": ("pharmacy_name", "🏪 أرسل اسم الصيدلية الجديد:"),
            "pmt_amount":   ("amount",        "💰 أرسل المبلغ الجديد (مثال: 50 أو خمسين الف):"),
            "pmt_date":     ("payment_date",  "📅 أرسل التاريخ الجديد (YYYY-MM-DD):"),
            "pmt_notes":    ("notes",         "📝 أرسل الملاحظة الجديدة:"),
        }
        db_field, prompt = pmt_field_map[field_slug]
        context.user_data["pmt_awaiting_edit"] = {"payment_id": pid, "field": db_field}
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 إلغاء", callback_data="menu_main")
            ]]),
        )

    elif data.startswith("pmt_delete_"):
        pid = int(data.split("_")[-1])
        await run_blocking(delete_payment, pid)
        await query.edit_message_text(
            "🗑 *تم حذف التسديد.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
        )

    elif data.startswith("pmt_ledger_"):
        pid = int(data.split("_")[-1])
        payment = await run_blocking(get_payment, pid)
        if not payment:
            await query.answer("⚠️ ما لقيت التسديد", show_alert=True)
            return
        pharmacy_name = payment["pharmacy_name"]
        bal = await run_blocking(get_pharmacy_balance, user_id, pharmacy_name)
        pays = await run_blocking(get_payments_by_rep, user_id, {"pharmacy": pharmacy_name})
        lines = [f"📒 *كشف حساب — {pharmacy_name}*\n"]
        for p in pays[:15]:
            lines.append(f"{p['payment_date']}  💰 {p['amount']:,.0f} د.ع" + (f" — {p['notes']}" if p.get('notes') else ""))
        lines.append("─" * 20)
        if bal["total_sales"] is not None:
            lines.append(f"إجمالي المبيعات (آجل/كريدت): *{bal['total_sales']:,.0f}* د.ع")
            lines.append(f"إجمالي التسديدات: *{bal['total_payments']:,.0f}* د.ع")
            lines.append(f"المتبقي: *{bal['remaining']:,.0f}* د.ع")
        else:
            lines.append(f"إجمالي التسديدات: *{bal['total_payments']:,.0f}* د.ع")
            lines.append("_(لا تتوفر أسعار بالكتالوج لحساب المتبقي)_")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 القائمة", callback_data="menu_main")
            ]]),
        )

    elif data == "pmt_report_menu":
        await query.edit_message_text(
            "💰 *تقارير التسديد*\n\nاختر الفترة:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📅 اليوم", callback_data="pmt_report_today"),
                    InlineKeyboardButton("📆 هذا الشهر", callback_data="pmt_report_month"),
                ],
                [InlineKeyboardButton("🗓 شهور أخرى", callback_data="pmt_report_pick_month")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="menu_reports")],
            ]),
        )

    elif data in ("pmt_report_today", "pmt_report_month"):
        today = date.today()
        if data == "pmt_report_today":
            date_from = date_to = today.isoformat()
            fname = f"تسديدات_اليوم_{today.isoformat()}.xlsx"
        else:
            date_from = today.replace(day=1).isoformat()
            date_to = today.isoformat()
            fname = f"تسديدات_{today.year}_{today.month:02d}.xlsx"
        payments = await run_blocking(get_payments_by_rep, user_id, {"date_from": date_from, "date_to": date_to})
        if not payments:
            await query.message.reply_text("📭 لا توجد تسديدات بهذه الفترة.", reply_markup=build_reports_menu())
        else:
            pharmacies = sorted({p["pharmacy_name"] for p in payments})
            balances = [await run_blocking(get_pharmacy_balance, user_id, ph) for ph in pharmacies]
            buf = await run_blocking(generate_payment_excel, payments, balances, None, False)
            await query.message.reply_document(document=buf, filename=fname, caption="💰 تقرير التسديدات")

    elif data == "pmt_report_pick_month":
        context.user_data["waiting_pmt_report_month"] = True
        await query.edit_message_text(
            "🗓 *اختر الشهر*\n\nأرسل *رقم الشهر* (مثال: `3` أو `3 2025`):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="pmt_report_menu")
            ]]),
        )

    elif data in ("order_edit_pharmacy", "order_edit_product", "order_edit_area",
                  "order_edit_doctor", "order_edit_quantity", "order_edit_bonus",

                  "order_edit_price", "order_edit_payment", "order_edit_notes",
                  "order_edit_date"):
        field_map = {
            "order_edit_pharmacy": ("pharmacy", "🏪 أرسل اسم الصيدلية الصحيح:"),
            "order_edit_product":  ("item_name", "💊 أرسل اسم المنتج والكمية (مثال: neogram 10+3):"),
            "order_edit_area":     ("area",      "📍 أرسل اسم المنطقة:"),
            "order_edit_doctor":   ("doctor_name","👨‍⚕️ أرسل اسم الطبيب:"),
            "order_edit_quantity": ("quantity",  "📦 أرسل الكمية الجديدة (مثال: 10):"),
            "order_edit_bonus":    ("bonus",     "🎁 أرسل البونص الجديد (مثال: 2):"),
            "order_edit_price":    ("price",     "💵 أرسل السعر الجديد:"),
            "order_edit_payment":  ("payment_type", "💰 أرسل نوع الدفع (كاش / آجل):"),
            "order_edit_notes":    ("notes",     "📝 أرسل الملاحظات:"),
            "order_edit_date":     ("order_date", "📅 أرسل التاريخ (YYYY-MM-DD):"),
        }
        field_key, prompt = field_map[data]
        # لو تعديل المنتج → عرض اقتراحات من الكتالوج
        if data == "order_edit_product":
            catalog = await run_blocking(get_product_catalog, user_id)
            known_pr = await run_blocking(get_known_products, user_id)
            all_names = list({c["drug_name"] for c in catalog} | set(known_pr))[:10]
            if all_names:
                btns = []
                row = []
                for name in all_names:
                    row.append(InlineKeyboardButton(name, callback_data=f"order_prod_:{name}"))
                    if len(row) == 2:
                        btns.append(row); row = []
                if row: btns.append(row)
                btns.append([InlineKeyboardButton("✏️ أكتب يدوياً", callback_data="order_edit_product_manual")])
                btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="order_back_preview")])
                context.user_data["awaiting_field"] = field_key
                await query.edit_message_text("💊 اختر المنتج أو اكتب يدوياً:",
                                              reply_markup=InlineKeyboardMarkup(btns))
                return
        context.user_data["awaiting_field"] = field_key
        # أعد جدولة الحفظ التلقائي بـ 30 ثانية جديدة من لحظة الضغط على التعديل
        chat_id = query.message.chat_id
        review_now  = context.user_data.get("review_order")
        review_multi_now = context.user_data.get("review_multi")
        if review_now is not None or review_multi_now is not None:
            _schedule_autosave(context, chat_id, {
                "user_id": user_id,
                "order": review_now,
                "multi": review_multi_now,
                "tg_name": tg_user.full_name,
            })
        await query.edit_message_text(prompt,
                                      reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton("🔙 رجوع", callback_data="order_back_preview")
                                      ]]))

    elif data == "order_edit_product_manual":
        context.user_data["awaiting_field"] = "item_name"
        await query.edit_message_text("💊 أرسل اسم المنتج والكمية (مثال: neogram 10+3):",
                                      reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton("🔙 رجوع", callback_data="order_back_preview")
                                      ]]))

    elif data.startswith("order_prod_:"):
        product_name = data[len("order_prod_:"):]
        review = context.user_data.get("review_order")
        review_multi = context.user_data.get("review_multi")
        context.user_data.pop("awaiting_field", None)
        if review:
            review["item_name"] = product_name
            preview = format_order_preview(review)
            await query.edit_message_text(preview, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_order_review_keyboard())
        elif review_multi:
            # تعديل أول منتج في القائمة كـ fallback بسيط
            if review_multi["products"]:
                review_multi["products"][0]["item_name"] = product_name
            preview = format_order_preview(review_multi["base_data"], is_multi=True,
                                           products=review_multi["products"])
            await query.edit_message_text(preview, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_order_review_keyboard(is_multi=True))

    elif data == "order_back_preview":
        context.user_data.pop("awaiting_field", None)
        review = context.user_data.get("review_order")
        review_multi = context.user_data.get("review_multi")
        if review:
            preview = format_order_preview(review)
            await query.edit_message_text(preview, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_order_review_keyboard())
        elif review_multi:
            preview = format_order_preview(review_multi["base_data"], is_multi=True,
                                           products=review_multi["products"])
            await query.edit_message_text(preview, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_order_review_keyboard(is_multi=True))
        else:
            await query.edit_message_text("⚠️ انتهت صلاحية الطلب.",
                                          reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)))


    # ══════════════════════════════════════════════════════════════════════════
    # ── Doctor Profile Callbacks (Phase 1, 2, 3) ─────────────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif data == "daily_review":
        await _handle_daily_review(query, user_id, context)

    elif data == "menu_doctors":
        doctors = await run_blocking(get_all_doctor_profiles, user_id)
        count   = len(doctors)
        classes = {"A": sum(1 for d in doctors if d.get("class") == "A"),
                   "B": sum(1 for d in doctors if d.get("class") == "B"),
                   "C": sum(1 for d in doctors if d.get("class") == "C")}
        lines = [
            "🩺 *ملف الأطباء*\n",
            f"👨‍⚕️ إجمالي الأطباء المسجلين: *{count}*",
        ]
        if count:
            lines.append(f"🟢 فئة A: {classes['A']}  |  🟡 B: {classes['B']}  |  🔵 C: {classes['C']}")
        lines.append("\nماذا تريد؟")
        btns = [
            [InlineKeyboardButton("➕ تسجيل طبيب جديد", callback_data="doctor_add")],
        ]
        if count:
            btns.append([InlineKeyboardButton("📋 عرض قائمة الأطباء", callback_data="doctor_list_0")])
            btns.append([
                InlineKeyboardButton("📊 تصدير استبيان",  callback_data="doctor_survey_excel"),
                InlineKeyboardButton("📥 استيراد استبيان", callback_data="doctor_import_survey"),
            ])
            btns.append([InlineKeyboardButton("🗑️ حذف طبيب باسمه", callback_data="doc_del_by_name")])
            btns.append([InlineKeyboardButton("💊 إضافة دواء منافس لجميع الأطباء", callback_data="competitor_bulk_add")])
        btns.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu_main")])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(btns),
        )

    elif data == "doctor_add":
        context.user_data.pop("doctor_ai_suggestion", None)
        context.user_data.pop("waiting_doctor_entry", None)
        await query.edit_message_text(
            "🩺 *تسجيل طبيب جديد*\n\nكيف تريد إضافة الطبيب؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ إدخال يدوي (كتابة)", callback_data="doctor_add_manual")],
                [InlineKeyboardButton("📥 استيراد من Excel", callback_data="doctor_add_excel")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")],
            ]),
        )

    elif data == "doctor_add_manual":
        context.user_data["waiting_doctor_entry"] = True
        context.user_data.pop("doctor_ai_suggestion", None)
        await query.edit_message_text(
            "🩺 *تسجيل طبيب — إدخال يدوي*\n\n"
            "أرسل معلومات الطبيب في رسالة واحدة. مثال:\n\n"
            "`د. أحمد الجبوري — قلبية — عيادة الرشاد — الكرادة — A`\n"
            "`دكتور سامي عيون — مستشفى النهرين — الكرخ — 07901234567 — B`\n\n"
            "الحقول: الاسم، التخصص، العيادة، المنطقة، الهاتف، الفئة (A/B/C)\n"
            "الفئة A/B/C/Z/A+ تُحفظ كفئة، وD/S/scientific/dealer تُحفظ بالملاحظات",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")
            ]]),
        )

    elif data == "doctor_add_excel":
        context.user_data["waiting_doctor_excel_import"] = True
        await query.edit_message_text(
            "📥 *استيراد أطباء من Excel*\n\n"
            "أرسل ملف Excel يحتوي بيانات الأطباء.\n\n"
            "الأعمدة المطلوبة (يكفي اسم الطبيب):\n"
            "• *الاسم* — اسم الطبيب (مطلوب)\n"
            "• *التخصص* — مثل: قلبية، أطفال، نساء\n"
            "• *المنطقة* — المنطقة أو الحي\n"
            "• *العيادة* — اسم العيادة أو المستشفى\n"
            "• *الهاتف* — رقم التواصل\n"
            "• *الفئة* — A أو B أو C\n"
            "• *ملاحظات* — أي ملاحظات إضافية\n\n"
            "_يمكنك أيضاً إرسال ملف الاستبيان الصادر من البوت مباشرة._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")
            ]]),
        )

    elif data == "doctor_confirm_save":
        # Phase 3: user confirmed the AI suggestion
        suggestion = context.user_data.pop("doctor_ai_suggestion", None)
        if not suggestion:
            await query.answer("⚠️ انتهت جلسة الاقتراح. أعد الإدخال.", show_alert=True)
            return
        # Use user-provided class if valid; otherwise auto-calculate from orders
        name = suggestion.get("name", "")
        user_cls = (suggestion.get("class") or "").strip().upper()
        valid_classes = {"A", "A+", "B", "C", "Z"}
        if user_cls in valid_classes:
            cls = user_cls
        else:
            cls = (await run_blocking(calculate_doctor_class, user_id, name)) if name else "C"
        suggestion["class"] = cls
        # Duplicate check
        dupes = await run_blocking(find_duplicate_doctors, user_id, name, suggestion.get("area"))
        profile_id = await run_blocking(upsert_doctor_profile, user_id, suggestion)
        cls_emoji = {"A": "🟢", "B": "🟡", "C": "🔵"}.get(cls, "🔵")
        msg = (
            f"✅ *تم حفظ ملف الطبيب!*\n\n"
            f"👨‍⚕️ الاسم: *{suggestion.get('name','')}*\n"
            f"🔬 التخصص: {suggestion.get('specialty') or '—'}\n"
            f"🏥 العيادة: {suggestion.get('clinic_name') or '—'}\n"
            f"📍 المنطقة: {suggestion.get('area') or '—'}\n"
            f"📞 الهاتف: {suggestion.get('phone') or '—'}\n"
            f"{cls_emoji} الفئة: *{cls}*\n"
            f"📝 ملاحظات: {suggestion.get('notes') or '—'}"
        )
        if dupes:
            dup_names = ", ".join(d.get("rep_name","مندوب") for d in dupes[:3])
            msg += f"\n\n⚠️ _تنبيه: طبيب بنفس الاسم مسجّل عند: {dup_names}_"
        btns = [
            [InlineKeyboardButton(f"💊 إضافة دواء منافس لهذا الطبيب", callback_data=f"competitor_add_{profile_id}")],
            [InlineKeyboardButton("➕ تسجيل طبيب آخر", callback_data="doctor_add"),
             InlineKeyboardButton("📋 قائمة الأطباء",  callback_data="doctor_list_0")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu_main")],
        ]
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(btns))

    elif data == "doctor_edit_entry":
        # User rejected AI suggestion — go back to manual entry
        context.user_data.pop("doctor_ai_suggestion", None)
        context.user_data["waiting_doctor_entry"] = True
        await query.edit_message_text(
            "✏️ *تعديل يدوي*\n\n"
            "أرسل بيانات الطبيب مرة أخرى بهذا الترتيب:\n"
            "`الاسم — التخصص — العيادة — المنطقة — الهاتف`\n\n"
            "مثال: `محمد علي الموسوي — أطفال — مستشفى الأطفال — الكرادة`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")
            ]]),
        )

    elif data.startswith("doctor_list_"):
        try:
            page = int(data.split("_")[-1])
            doctors = await run_blocking(get_all_doctor_profiles, user_id)
            PAGE  = 8
            start = page * PAGE
            chunk = doctors[start:start + PAGE]
            cls_emoji = {"A": "🟢", "B": "🟡", "C": "🔵"}
            lines = [f"📋 *قائمة الأطباء* \\({len(doctors)} طبيب\\)\n"]
            for d in chunk:
                ce = cls_emoji.get(d.get("class", "C"), "🔵")
                name = re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', d.get("name","") or "")
                spec = re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', d.get("specialty","") or "")
                area = re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', d.get("area","") or "")
                line = f"{ce} *{name}*"
                if spec: line += f" — {spec}"
                if area: line += f" — {area}"
                lines.append(line)
            btns = []
            for d in chunk[:5]:
                btns.append([
                    InlineKeyboardButton(f"📄 {d['name'][:20]}", callback_data=f"doctor_view_{d['id']}"),
                ])
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"doctor_list_{page-1}"))
            if start + PAGE < len(doctors):
                nav.append(InlineKeyboardButton("▶️ التالي", callback_data=f"doctor_list_{page+1}"))
            if nav:
                btns.append(nav)
            btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="menu_doctors")])
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="MarkdownV2",
                reply_markup=InlineKeyboardMarkup(btns),
            )
        except Exception as e:
            await query.edit_message_text(
                "⚠️ حدث خطأ أثناء تحميل قائمة الأطباء. حاول مرة أخرى.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 إعادة المحاولة", callback_data="doctor_list_0"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_doctors"),
                ]]),
            )

    elif data.startswith("doctor_view_"):
        doc_id = int(data.split("_")[-1])
        doc = await run_blocking(get_doctor_profile_by_id, doc_id, user_id)
        if not doc:
            await query.answer("⚠️ لم يُعثر على الطبيب.", show_alert=True)
            return
        # Refresh class from orders
        cls = await run_blocking(update_doctor_class_auto, user_id, doc["name"])
        doc["class"] = cls
        ce = {"A": "🟢", "B": "🟡", "C": "🔵"}.get(cls, "🔵")
        orders = await run_blocking(get_orders_for_doctor, user_id, doc["name"])
        competitors = await run_blocking(get_competitor_entries, user_id, doc_id)
        comp_lines = ""
        if competitors:
            comp_lines = "\n\n🦠 *منافسون مسجلون:*\n" + "\n".join(
                f"  • {c.get('competitor_name','—')} — {c.get('competitor_product','—')}"
                for c in competitors[:5]
            )
        msg = (
            f"🩺 *ملف د. {doc['name']}*\n\n"
            f"🔬 التخصص: {doc.get('specialty') or '—'}\n"
            f"🏥 العيادة: {doc.get('clinic_name') or '—'}\n"
            f"📍 المنطقة: {doc.get('area') or '—'}\n"
            f"📞 الهاتف: {doc.get('phone') or '—'}\n"
            f"{ce} الفئة: *{cls}* _(محدثة تلقائياً)_\n"
            f"📦 الطلبات المرتبطة: *{len(orders)}*\n"
            f"📝 ملاحظات: {doc.get('notes') or '—'}"
            + comp_lines
        )
        btns = [
            [InlineKeyboardButton("💊 إضافة دواء منافس", callback_data=f"competitor_add_{doc_id}")],
            [InlineKeyboardButton("🗑️ حذف", callback_data=f"doc_del_ask_{doc_id}"),
             InlineKeyboardButton("🔙 القائمة", callback_data="doctor_list_0")],
        ]
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("doc_del_ask_"):
        doc_id = int(data.split("_")[-1])
        doc = await run_blocking(get_doctor_profile_by_id, doc_id, user_id)
        if not doc:
            await query.answer("⚠️ لم يُعثر على الطبيب.", show_alert=True)
            return
        await query.edit_message_text(
            f"⚠️ *تأكيد الحذف*\n\nهل تريد حذف ملف د. *{doc['name']}* نهائياً؟\n"
            "_سيتم حذف منافسيه المسجلين أيضاً._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ نعم، احذف", callback_data=f"doc_del_yes_{doc_id}"),
                 InlineKeyboardButton("❌ إلغاء",       callback_data=f"doctor_view_{doc_id}")],
            ]),
        )

    elif data.startswith("doc_del_yes_"):
        doc_id = int(data.split("_")[-1])
        doc = await run_blocking(get_doctor_profile_by_id, doc_id, user_id)
        name   = doc["name"] if doc else "—"
        await run_blocking(delete_doctor_profile, doc_id, user_id)
        await query.edit_message_text(
            f"🗑️ *تم حذف ملف د. {name}.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 قائمة الأطباء", callback_data="doctor_list_0"),
                InlineKeyboardButton("🔙 الرئيسية",      callback_data="menu_main"),
            ]]),
        )

    elif data == "doc_del_by_name":
        context.user_data["waiting_doc_delete_name"] = True
        await query.edit_message_text(
            "🗑️ *حذف طبيب*\n\n"
            "اكتب *اسم الطبيب* الذي تريد حذفه:\n"
            "_سيُحذف الطبيب وجميع معلوماته من قاعدة البيانات نهائياً._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")
            ]]),
        )

    elif data == "doctor_import_survey":
        context.user_data["waiting_survey_import"] = True
        await query.edit_message_text(
            "📥 *استيراد الاستبيان*\n\n"
            "أرسل ملف Excel الاستبيان الذي عبّأته ميدانياً.\n"
            "_تأكد أنه الملف الصادر من البوت بدون تغيير أسماء الأعمدة._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")
            ]]),
        )

    elif data == "doctor_survey_excel":
        doctors = await run_blocking(get_all_doctor_profiles, user_id)
        if not doctors:
            await query.answer("⚠️ لا يوجد أطباء مسجلون بعد.", show_alert=True)
            return
        await query.answer("⏳ جارٍ إنشاء استبيان Excel…")
        settings = await run_blocking(get_user_settings, user_id)
        rep_name  = settings.get("rep_name_memory") or "مندوب"
        today     = date.today()
        try:
            buf   = await run_blocking(generate_survey_excel, doctors, rep_name, today.month, today.year)
            fname = f"استبيان_أطباء_{rep_name}_{today.month}_{today.year}.xlsx"
            await query.message.reply_document(
                document=buf,
                filename=fname,
                caption=(
                    f"📊 *استبيان الأطباء — {rep_name}*\n"
                    f"👨‍⚕️ {len(doctors)} طبيب  |  "
                    f"🟢 A: {sum(1 for d in doctors if d.get('class')=='A')}  "
                    f"🟡 B: {sum(1 for d in doctors if d.get('class')=='B')}  "
                    f"🔵 C: {sum(1 for d in doctors if d.get('class')=='C')}"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await query.message.reply_text(f"❌ خطأ في إنشاء الملف:\n`{e}`",
                                           parse_mode=ParseMode.MARKDOWN)

    # ══════════════════════════════════════════════════════════════════════════
    # ── مكتبة الأدوية المنافسة (Competitor Drug Library) ──────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    elif data == "menu_comp_drugs":
        drugs = await run_blocking(get_competitor_drugs, user_id)
        count = len(drugs)
        btns = [
            [InlineKeyboardButton("➕ إضافة دواء منافس جديد", callback_data="comp_drug_add")],
        ]
        if count:
            btns.append([InlineKeyboardButton(f"📋 قائمة الأدوية المنافسة ({count})", callback_data="comp_drug_list_0")])
        btns.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu_main")])
        summary = f"لديك *{count}* دواء منافس مسجّل." if count else "لم تسجّل أي دواء منافس بعد."
        await query.edit_message_text(
            f"💊 *الأدوية المنافسة*\n\n{summary}\n\n"
            "سجّل الأدوية التي يستخدمها المنافسون ثم اربطها بالأطباء الذين يصفونها.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(btns),
        )

    elif data == "comp_drug_add":
        context.user_data["comp_drug_draft"] = {}
        context.user_data["waiting_comp_drug_name"] = True
        await query.edit_message_text(
            "💊 *إضافة دواء منافس — الخطوة 1/5*\n\n"
            "أرسل *اسم الدواء المنافس:*\n\n"
            "_مثال: Crestor, Lipitor, Amoxil_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_comp_drugs")
            ]]),
        )

    elif data == "comp_drug_pick_form":
        # عرض اختيار شكل الدواء
        draft = context.user_data.get("comp_drug_draft", {})
        rows = []
        for i in range(0, len(DRUG_FORMS), 2):
            row = []
            for key, label in DRUG_FORMS[i:i+2]:
                row.append(InlineKeyboardButton(label, callback_data=f"comp_drug_form_{key}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu_comp_drugs")])
        await query.edit_message_text(
            f"💊 *إضافة دواء منافس — الخطوة 3/5*\n\n"
            f"الدواء: *{draft.get('drug_name','')}*\n"
            f"الشركة: {draft.get('company','—')}\n\n"
            "اختر *شكل الدواء:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data.startswith("comp_drug_form_"):
        form_key = data[len("comp_drug_form_"):]
        draft = context.user_data.setdefault("comp_drug_draft", {})
        draft["form"] = form_key
        # انتقل لاختيار طرق الإدخال (multi-select)
        selected_routes = draft.get("routes_selected", [])
        await _show_route_picker(query, draft, selected_routes)

    elif data.startswith("comp_drug_route_"):
        route_key = data[len("comp_drug_route_"):]
        draft = context.user_data.setdefault("comp_drug_draft", {})
        sel = draft.setdefault("routes_selected", [])
        if route_key in sel:
            sel.remove(route_key)
        else:
            sel.append(route_key)
        await _show_route_picker(query, draft, sel)

    elif data == "comp_drug_routes_done":
        draft = context.user_data.get("comp_drug_draft", {})
        sel = draft.get("routes_selected", [])
        if not sel:
            await query.answer("⚠️ اختر طريق إدخال واحد على الأقل.", show_alert=True)
            return
        draft["routes"] = ",".join(sel)
        context.user_data["waiting_comp_drug_price"] = True
        await query.edit_message_text(
            f"💊 *إضافة دواء منافس — الخطوة 4/5*\n\n"
            f"الدواء: *{draft.get('drug_name','')}*  |  الشركة: {draft.get('company','—')}\n"
            f"الشكل: {DRUG_FORM_LABELS.get(draft.get('form',''),'—')}\n\n"
            "أرسل *سعر الدواء* بالدينار العراقي:\n"
            "_مثال: 5000 أو 12500_\n\n"
            "_أو أرسل — لتخطي السعر_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_comp_drugs")
            ]]),
        )

    elif data == "comp_drug_confirm":
        draft = context.user_data.pop("comp_drug_draft", {})
        for k in ("waiting_comp_drug_name", "waiting_comp_drug_company",
                  "waiting_comp_drug_price", "waiting_comp_drug_features"):
            context.user_data.pop(k, None)
        if not draft.get("drug_name"):
            await query.answer("⚠️ البيانات غير مكتملة.", show_alert=True)
            return
        new_id = await run_blocking(
            add_competitor_drug,
            user_id,
            draft["drug_name"],
            draft.get("company"),
            draft.get("form"),
            draft.get("routes"),
            draft.get("price"),
            draft.get("features"),
        )
        form_lbl = DRUG_FORM_LABELS.get(draft.get("form", ""), "—")
        routes_lbl = " / ".join(
            DRUG_ROUTE_LABELS.get(r.strip(), r.strip())
            for r in (draft.get("routes") or "").split(",") if r.strip()
        ) or "—"
        price_str = f"{float(draft['price']):,.0f} د.ع" if draft.get("price") else "—"
        await query.edit_message_text(
            f"✅ *تم حفظ الدواء المنافس بنجاح!*\n\n"
            f"💊 الاسم: *{draft['drug_name']}*\n"
            f"🏭 الشركة: {draft.get('company') or '—'}\n"
            f"📦 الشكل: {form_lbl}\n"
            f"🔀 الإدخال: {routes_lbl}\n"
            f"💵 السعر: {price_str}\n"
            f"⭐ المميزات: {draft.get('features') or '—'}\n\n"
            "_الآن يمكنك ربطه بالأطباء الذين يستخدمونه:_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🩺 ربط بالأطباء", callback_data=f"comp_drug_assign_{new_id}")],
                [InlineKeyboardButton("➕ إضافة دواء آخر", callback_data="comp_drug_add"),
                 InlineKeyboardButton("📋 قائمة الأدوية",  callback_data="comp_drug_list_0")],
                [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu_main")],
            ]),
        )

    elif data == "comp_drug_cancel_add":
        context.user_data.pop("comp_drug_draft", None)
        for k in ("waiting_comp_drug_name", "waiting_comp_drug_company",
                  "waiting_comp_drug_price", "waiting_comp_drug_features"):
            context.user_data.pop(k, None)
        await query.edit_message_text(
            "❌ تم إلغاء إضافة الدواء.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 الأدوية المنافسة", callback_data="menu_comp_drugs")
            ]]),
        )

    elif data.startswith("comp_drug_list_"):
        page = int(data.split("_")[-1])
        drugs = await run_blocking(get_competitor_drugs, user_id)
        per_page = 8
        start = page * per_page
        chunk = drugs[start:start + per_page]
        if not drugs:
            await query.edit_message_text(
                "💊 *الأدوية المنافسة*\n\nلا يوجد أدوية مسجّلة بعد.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ إضافة دواء منافس", callback_data="comp_drug_add"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_comp_drugs"),
                ]]),
            )
            return
        btns = []
        for d in chunk:
            form_lbl = DRUG_FORM_LABELS.get(d.get("form", ""), "")
            btn_txt = f"💊 {d['drug_name']}"
            if d.get("company"): btn_txt += f" — {d['company']}"
            if form_lbl: btn_txt += f" ({form_lbl})"
            btns.append([InlineKeyboardButton(btn_txt, callback_data=f"comp_drug_view_{d['id']}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"comp_drug_list_{page-1}"))
        if start + per_page < len(drugs):
            nav.append(InlineKeyboardButton("التالي ➡️", callback_data=f"comp_drug_list_{page+1}"))
        if nav: btns.append(nav)
        btns.append([InlineKeyboardButton("➕ إضافة دواء جديد", callback_data="comp_drug_add"),
                     InlineKeyboardButton("🔙 رجوع", callback_data="menu_comp_drugs")])
        await query.edit_message_text(
            f"💊 *قائمة الأدوية المنافسة* ({len(drugs)} دواء)\n\nاختر دواءً للتفاصيل:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(btns),
        )

    elif data.startswith("comp_drug_view_"):
        drug_id = int(data.split("_")[-1])
        d = await run_blocking(get_competitor_drug_by_id, drug_id, user_id)
        if not d:
            await query.answer("⚠️ الدواء غير موجود.", show_alert=True)
            return
        linked = await run_blocking(get_linked_doctors_for_drug, user_id, drug_id)
        doc_lines = ""
        if linked:
            cls_emoji = {"A": "🟢", "B": "🟡", "C": "🔵"}
            doc_lines = "\n\n🩺 *أطباء يستخدمونه:*\n" + "\n".join(
                f"  {cls_emoji.get(d2.get('class','C'),'🔵')} {d2['name']}"
                + (f" — {d2.get('specialty','')}" if d2.get('specialty') else "")
                for d2 in linked[:10]
            )
            if len(linked) > 10:
                doc_lines += f"\n  _...و {len(linked)-10} آخرين_"
        await query.edit_message_text(
            _format_comp_drug(d) + doc_lines,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🩺 ربط / إلغاء ربط أطباء", callback_data=f"comp_drug_assign_{drug_id}")],
                [InlineKeyboardButton("🗑️ حذف الدواء", callback_data=f"comp_drug_del_ask_{drug_id}"),
                 InlineKeyboardButton("🔙 القائمة",    callback_data="comp_drug_list_0")],
            ]),
        )

    elif data.startswith("comp_drug_assign_"):
        drug_id = int(data.split("_")[-1])
        d = await run_blocking(get_competitor_drug_by_id, drug_id, user_id)
        if not d:
            await query.answer("⚠️ الدواء غير موجود.", show_alert=True)
            return
        doctors = await run_blocking(get_all_doctor_profiles, user_id)
        if not doctors:
            await query.answer("⚠️ لا يوجد أطباء مسجّلون.", show_alert=True)
            return
        linked_ids = await run_blocking(get_linked_drug_ids_for_drug, user_id, drug_id)
        cls_emoji = {"A": "🟢", "A+": "🌟", "B": "🟡", "C": "🔵", "Z": "⚫"}
        btns = []
        for i in range(0, len(doctors), 2):
            row = []
            for doc in doctors[i:i+2]:
                is_linked = doc["id"] in linked_ids
                ce = cls_emoji.get(doc.get("class", "C"), "🔵")
                tick = "✅" if is_linked else "◻️"
                lbl = f"{tick} {ce} {doc['name']}"
                row.append(InlineKeyboardButton(lbl, callback_data=f"comp_drug_tog_{drug_id}_{doc['id']}"))
            btns.append(row)
        btns.append([
            InlineKeyboardButton("✅ جميع الأطباء",  callback_data=f"comp_drug_all_link_{drug_id}"),
            InlineKeyboardButton("🗑️ إلغاء الكل",    callback_data=f"comp_drug_all_unlink_{drug_id}"),
        ])
        btns.append([InlineKeyboardButton("🔙 رجوع للدواء", callback_data=f"comp_drug_view_{drug_id}")])
        await query.edit_message_text(
            f"🩺 *ربط الأطباء بـ «{d['drug_name']}»*\n\n"
            "اضغط على اسم الطبيب لإضافته أو إزالته.\n"
            "✅ = يستخدم هذا الدواء  |  ◻️ = لا يستخدمه",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(btns),
        )

    elif data.startswith("comp_drug_tog_"):
        parts = data.split("_")
        drug_id = int(parts[3])
        doc_id  = int(parts[4])
        d = await run_blocking(get_competitor_drug_by_id, drug_id, user_id)
        if not d:
            await query.answer("⚠️ الدواء غير موجود.", show_alert=True)
            return
        linked = await run_blocking(toggle_doctor_competitor_drug, user_id, drug_id, doc_id)
        await query.answer("✅ تم الربط" if linked else "❌ تم إلغاء الربط")
        # أعد رسم القائمة
        doctors = await run_blocking(get_all_doctor_profiles, user_id)
        linked_ids = await run_blocking(get_linked_drug_ids_for_drug, user_id, drug_id)
        cls_emoji = {"A": "🟢", "A+": "🌟", "B": "🟡", "C": "🔵", "Z": "⚫"}
        btns = []
        for i in range(0, len(doctors), 2):
            row = []
            for doc in doctors[i:i+2]:
                is_linked = doc["id"] in linked_ids
                ce = cls_emoji.get(doc.get("class", "C"), "🔵")
                tick = "✅" if is_linked else "◻️"
                row.append(InlineKeyboardButton(
                    f"{tick} {ce} {doc['name']}",
                    callback_data=f"comp_drug_tog_{drug_id}_{doc['id']}"
                ))
            btns.append(row)
        btns.append([
            InlineKeyboardButton("✅ جميع الأطباء", callback_data=f"comp_drug_all_link_{drug_id}"),
            InlineKeyboardButton("🗑️ إلغاء الكل",  callback_data=f"comp_drug_all_unlink_{drug_id}"),
        ])
        btns.append([InlineKeyboardButton("🔙 رجوع للدواء", callback_data=f"comp_drug_view_{drug_id}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("comp_drug_all_link_"):
        drug_id = int(data.split("_")[-1])
        doctors = await run_blocking(get_all_doctor_profiles, user_id)
        for doc in doctors:
            try:
                linked_ids = await run_blocking(get_linked_drug_ids_for_drug, user_id, drug_id)
                if doc["id"] not in linked_ids:
                    await run_blocking(toggle_doctor_competitor_drug, user_id, drug_id, doc["id"])
            except Exception:
                pass
        await query.answer(f"✅ تم ربط {len(doctors)} طبيب")
        # أعد رسم
        d = await run_blocking(get_competitor_drug_by_id, drug_id, user_id)
        linked_ids = await run_blocking(get_linked_drug_ids_for_drug, user_id, drug_id)
        cls_emoji = {"A": "🟢", "A+": "🌟", "B": "🟡", "C": "🔵", "Z": "⚫"}
        btns = []
        for i in range(0, len(doctors), 2):
            row = []
            for doc in doctors[i:i+2]:
                ce = cls_emoji.get(doc.get("class", "C"), "🔵")
                row.append(InlineKeyboardButton(
                    f"✅ {ce} {doc['name']}",
                    callback_data=f"comp_drug_tog_{drug_id}_{doc['id']}"
                ))
            btns.append(row)
        btns.append([
            InlineKeyboardButton("✅ جميع الأطباء", callback_data=f"comp_drug_all_link_{drug_id}"),
            InlineKeyboardButton("🗑️ إلغاء الكل",  callback_data=f"comp_drug_all_unlink_{drug_id}"),
        ])
        btns.append([InlineKeyboardButton("🔙 رجوع للدواء", callback_data=f"comp_drug_view_{drug_id}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("comp_drug_all_unlink_"):
        drug_id = int(data.split("_")[-1])
        doctors = await run_blocking(get_all_doctor_profiles, user_id)
        linked_ids = await run_blocking(get_linked_drug_ids_for_drug, user_id, drug_id)
        for doc in doctors:
            if doc["id"] in linked_ids:
                await run_blocking(toggle_doctor_competitor_drug, user_id, drug_id, doc["id"])
        await query.answer("✅ تم إلغاء ربط جميع الأطباء")
        btns = []
        cls_emoji = {"A": "🟢", "A+": "🌟", "B": "🟡", "C": "🔵", "Z": "⚫"}
        for i in range(0, len(doctors), 2):
            row = []
            for doc in doctors[i:i+2]:
                ce = cls_emoji.get(doc.get("class", "C"), "🔵")
                row.append(InlineKeyboardButton(
                    f"◻️ {ce} {doc['name']}",
                    callback_data=f"comp_drug_tog_{drug_id}_{doc['id']}"
                ))
            btns.append(row)
        btns.append([
            InlineKeyboardButton("✅ جميع الأطباء", callback_data=f"comp_drug_all_link_{drug_id}"),
            InlineKeyboardButton("🗑️ إلغاء الكل",  callback_data=f"comp_drug_all_unlink_{drug_id}"),
        ])
        btns.append([InlineKeyboardButton("🔙 رجوع للدواء", callback_data=f"comp_drug_view_{drug_id}")])
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("comp_drug_del_ask_"):
        drug_id = int(data.split("_")[-1])
        d = await run_blocking(get_competitor_drug_by_id, drug_id, user_id)
        if not d:
            await query.answer("⚠️ الدواء غير موجود.", show_alert=True)
            return
        await query.edit_message_text(
            f"🗑️ *هل تريد حذف «{d['drug_name']}»؟*\n\n"
            "سيُحذف الدواء وجميع ربطاته بالأطباء.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، احذف",   callback_data=f"comp_drug_del_yes_{drug_id}"),
                 InlineKeyboardButton("❌ إلغاء",         callback_data=f"comp_drug_view_{drug_id}")],
            ]),
        )

    elif data.startswith("comp_drug_del_yes_"):
        drug_id = int(data.split("_")[-1])
        d = await run_blocking(get_competitor_drug_by_id, drug_id, user_id)
        name = d["drug_name"] if d else "الدواء"
        await run_blocking(delete_competitor_drug, drug_id, user_id)
        await query.edit_message_text(
            f"🗑️ تم حذف «{name}» بنجاح.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 قائمة الأدوية", callback_data="comp_drug_list_0"),
                InlineKeyboardButton("🔙 رجوع",          callback_data="menu_comp_drugs"),
            ]]),
        )

    # ── Phase 2: Competitor entry ──────────────────────────────────────────────
    elif data.startswith("competitor_add_"):
        doc_id = int(data.split("_")[-1])
        doc = await run_blocking(get_doctor_profile_by_id, doc_id, user_id)
        if not doc:
            await query.answer("⚠️ لم يُعثر على الطبيب.", show_alert=True)
            return
        context.user_data["waiting_competitor_entry"] = {"doc_id": doc_id, "doc_name": doc["name"]}
        await query.edit_message_text(
            f"💊 *إضافة دواء منافس لـ د. {doc['name']}*\n\n"
            "أرسل معلومات الدواء المنافس بهذا الشكل:\n"
            "`اسم الشركة — اسم الدواء — ملاحظات (اختياري)`\n\n"
            "مثال: `شركة فارما — Crestor 10mg — يروجونه بسعر أقل`\n\n"
            "_الحقول: اسم الشركة / اسم الدواء المنافس / ملاحظات_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data=f"doctor_view_{doc_id}")
            ]]),
        )

    elif data == "competitor_bulk_add":
        context.user_data["waiting_competitor_bulk_name"] = True
        await query.edit_message_text(
            "💊 *إضافة دواء منافس بالتخصص*\n\n"
            "أرسل *اسم الدواء المنافس* فقط:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")
            ]]),
        )

    elif data.startswith("comp_bulk_spec_"):
        # ── اختيار تخصص → عرض أطباء التخصص ─────────────────────────────────
        try:
            doctors_all = await run_blocking(get_all_doctor_profiles, user_id)
            drug_name   = context.user_data.get("comp_bulk_drug_name", "—")

            if data == "comp_bulk_spec_ALL":
                filtered    = doctors_all
                spec_label  = "جميع التخصصات"
            else:
                idx          = int(data.split("_")[-1])
                specialties  = context.user_data.get("comp_bulk_specialties", [])
                spec         = specialties[idx] if idx < len(specialties) else ""
                filtered     = [d for d in doctors_all if (d.get("specialty") or "").strip() == spec]
                spec_label   = spec or "بدون تخصص"

            context.user_data["comp_bulk_specialty_label"] = spec_label
            context.user_data["comp_bulk_filtered_docs"]   = [{"id": d["id"], "name": d["name"], "class": d.get("class","C")} for d in filtered]
            context.user_data["comp_bulk_selected_docs"]   = {d["id"] for d in filtered}

            await _bulk_redraw_doctors(query, context, drug_name)
        except Exception:
            await query.answer("⚠️ حدث خطأ. حاول مجدداً.", show_alert=True)

    elif data.startswith("comp_bulk_doc_tog_"):
        doc_id   = int(data.split("_")[-1])
        selected = context.user_data.setdefault("comp_bulk_selected_docs", set())
        if doc_id in selected:
            selected.discard(doc_id)
        else:
            selected.add(doc_id)
        drug_name = context.user_data.get("comp_bulk_drug_name", "—")
        await _bulk_redraw_doctors(query, context, drug_name)

    elif data == "comp_bulk_all_on":
        filtered  = context.user_data.get("comp_bulk_filtered_docs", [])
        context.user_data["comp_bulk_selected_docs"] = {d["id"] for d in filtered}
        drug_name = context.user_data.get("comp_bulk_drug_name", "—")
        await _bulk_redraw_doctors(query, context, drug_name)

    elif data == "comp_bulk_all_off":
        context.user_data["comp_bulk_selected_docs"] = set()
        drug_name = context.user_data.get("comp_bulk_drug_name", "—")
        await _bulk_redraw_doctors(query, context, drug_name)

    elif data == "comp_bulk_confirm":
        drug_name  = context.user_data.pop("comp_bulk_drug_name", "—")
        selected   = context.user_data.pop("comp_bulk_selected_docs", set())
        spec_label = context.user_data.pop("comp_bulk_specialty_label", "—")
        context.user_data.pop("comp_bulk_filtered_docs", None)
        context.user_data.pop("comp_bulk_specialties", None)

        count = 0
        for doc_id in selected:
            try:
                await run_blocking(add_competitor_entry, user_id, doc_id, "—", drug_name, None)
                count += 1
            except Exception:
                pass

        await query.edit_message_text(
            f"✅ *تم تسجيل الدواء المنافس بنجاح*\n\n"
            f"💊 الدواء: *{drug_name}*\n"
            f"🔬 التخصص: {spec_label}\n"
            f"👨‍⚕️ عدد الأطباء: *{count}*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 قائمة الأطباء", callback_data="doctor_list_0"),
                InlineKeyboardButton("🔙 رجوع",           callback_data="menu_doctors"),
            ]]),
        )

    elif data == "comp_bulk_back":
        drug_name   = context.user_data.get("comp_bulk_drug_name", "—")
        specialties = context.user_data.get("comp_bulk_specialties", [])
        doctors_all = await run_blocking(get_all_doctor_profiles, user_id)
        if not specialties:
            specialties = sorted({(d.get("specialty") or "").strip() for d in doctors_all if (d.get("specialty") or "").strip()})
            context.user_data["comp_bulk_specialties"] = specialties
        await _bulk_show_specialties(query, drug_name, specialties, len(doctors_all))

    elif data == "smart_search_again":
        context.user_data["waiting_for_search"] = True
        await query.edit_message_text(
            "🔍 *البحث الذكي*\n\nأرسل كلمة البحث (اسم طبيب، صيدلية، دواء منافس…):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_main")
            ]]),
        )

    # ── أزرار لوحة الأدمن ────────────────────────────────────────────────────
    elif data.startswith("adm_"):
        await handle_admin_callback(update, context)
        return


async def _show_catalog_menu(query, user_id: int, msg: str = None):
    catalog = await run_blocking(get_product_catalog, user_id)
    lines = []
    if msg:
        lines.append(msg + "\n")
    if catalog:
        lines.append("💊 *كتالوج أدويتك:*\n")
        for d in catalog:
            price_str = f"{int(d['price']):,}" if d['price'] else "—"
            lines.append(f"• {d['drug_name']} — {price_str} IQD")
    else:
        lines.append("📭 لا توجد أدوية في الكتالوج بعد.\nأضف أدويتك للحصول على تقارير Excel منسقة.")

    btns = [
        [InlineKeyboardButton("➕ إضافة دواء", callback_data="catalog_add")],
    ]
    if catalog:
        btns.append([InlineKeyboardButton("🎯 التاركت الشهري", callback_data="tgt_menu")])
        btns.append([
            InlineKeyboardButton("📊 Excel هذا الشهر", callback_data="catalog_excel"),
            InlineKeyboardButton("📅 اختر شهراً", callback_data="catalog_pick_month"),
        ])
        btns.append([InlineKeyboardButton("🗑️ حذف دواء", callback_data="catalog_del_pick")])
        btns.append([InlineKeyboardButton("✏️ تعديل سعر", callback_data="catalog_price_pick")])
        btns.append([InlineKeyboardButton("⚠️ مسح الكتالوج", callback_data="catalog_clear_confirm")])
    btns.append([InlineKeyboardButton("🔙 الإعدادات", callback_data="menu_settings")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btns),
    )


async def _show_target_menu(query, user_id: int):
    catalog = await run_blocking(get_product_catalog, user_id)
    today = date.today()
    period = f"{today.year}-{today.month:02d}"
    targets = await run_blocking(get_drug_targets, user_id, period)

    if not catalog:
        await query.edit_message_text(
            "📭 الكتالوج فارغ. أضف أدوية أولاً.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💊 الكتالوج", callback_data="catalog_menu")
            ]])
        )
        return

    lines = [f"🎯 *التاركت الشهري — {today.month}/{today.year}*\n",
             "اضغط على الدواء لتعيين أو تعديل التاركت:\n"]
    for d in catalog:
        tgt_val = targets.get(d["drug_name"], 0)
        tgt_str = f"{tgt_val:,}" if tgt_val else "لم يُحدد بعد"
        lines.append(f"• {d['drug_name']}: *{tgt_str}*")

    btns = []
    for i, d in enumerate(catalog):
        tgt_val = targets.get(d["drug_name"], 0)
        lbl = f"🎯 {d['drug_name']}" + (f" ({tgt_val:,})" if tgt_val else "")
        btns.append([InlineKeyboardButton(lbl, callback_data=f"tgt_drug_{i}")])
    btns.append([InlineKeyboardButton("🔙 رجوع للكتالوج", callback_data="catalog_menu")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btns),
    )


async def _send_catalog_excel(query, context, user_id: int, month: int, year: int):
    from reports import generate_template_excel
    settings = await run_blocking(get_user_settings, user_id)
    rep_name = settings.get("rep_name_memory") or "مندوب"
    catalog = await run_blocking(get_product_catalog, user_id)
    period = f"{year}-{month:02d}"
    targets = await run_blocking(get_drug_targets, user_id, period)

    orders = await run_blocking(fetch_orders, user_id, filters={"month": month, "year": year})
    if not orders and not catalog:
        await query.answer("لا توجد بيانات لهذا الشهر.", show_alert=True)
        return

    await query.answer("جارٍ إنشاء الملف…")
    try:
        buf = await run_blocking(generate_template_excel, orders, catalog, rep_name, month, year, drug_targets=targets)
        file_name = f"{rep_name}_{month}_{year}.xlsx"
        await query.message.reply_document(
            document=buf,
            filename=file_name,
            caption=f"📊 تقرير {rep_name} — {month}/{year}",
        )
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ في إنشاء الملف:\n`{e}`", parse_mode=ParseMode.MARKDOWN)


async def _show_route_picker(query, draft: dict, selected: list):
    """عرض لوحة اختيار طرق الإدخال (multi-select)."""
    rows = []
    for i in range(0, len(DRUG_ROUTES), 2):
        row = []
        for key, label in DRUG_ROUTES[i:i+2]:
            tick = "✅" if key in selected else "◻️"
            row.append(InlineKeyboardButton(
                f"{tick} {label}",
                callback_data=f"comp_drug_route_{key}"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton(
        f"✅ تأكيد الاختيار ({len(selected)} طريق)" if selected else "◻️ اختر طريقاً واحداً على الأقل",
        callback_data="comp_drug_routes_done"
    )])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu_comp_drugs")])
    drug_name = draft.get("drug_name", "")
    company   = draft.get("company", "—")
    form_lbl  = DRUG_FORM_LABELS.get(draft.get("form", ""), "—")
    await query.edit_message_text(
        f"💊 *إضافة دواء منافس — الخطوة 4/5*\n\n"
        f"الدواء: *{drug_name}*  |  الشركة: {company}\n"
        f"الشكل: {form_lbl}\n\n"
        "اختر *طرق الإدخال* (يمكن اختيار أكثر من طريق):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _bulk_show_specialties(query, drug_name: str, specialties: list, total_docs: int):
    """Show specialty selection buttons for the bulk competitor drug flow."""
    btns = []
    for i, spec in enumerate(specialties[:20]):
        btns.append([InlineKeyboardButton(f"🔬 {spec}", callback_data=f"comp_bulk_spec_{i}")])
    if total_docs:
        btns.append([InlineKeyboardButton(f"👥 جميع الأطباء ({total_docs})", callback_data="comp_bulk_spec_ALL")])
    btns.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")])
    await query.edit_message_text(
        f"💊 *الدواء المنافس:* {drug_name}\n\n"
        "اختر *التخصص* الذي تريد إضافة الدواء له:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btns),
    )


async def _bulk_redraw_doctors(query, context, drug_name: str):
    """Redraw the doctor selection keyboard for bulk competitor drug flow."""
    filtered  = context.user_data.get("comp_bulk_filtered_docs", [])
    selected  = context.user_data.get("comp_bulk_selected_docs", set())
    spec_label = context.user_data.get("comp_bulk_specialty_label", "—")

    cls_emoji = {"A": "🟢", "B": "🟡", "C": "🔵"}
    btns = []
    for doc in filtered[:20]:
        is_sel = doc["id"] in selected
        tick   = "✅" if is_sel else "◻️"
        ce     = cls_emoji.get(doc.get("class","C"), "🔵")
        label  = f"{tick} {ce} {doc['name'][:22]}"
        btns.append([InlineKeyboardButton(label, callback_data=f"comp_bulk_doc_tog_{doc['id']}")])

    btns.append([
        InlineKeyboardButton("✅ تحديد الكل",   callback_data="comp_bulk_all_on"),
        InlineKeyboardButton("❌ إلغاء الكل",    callback_data="comp_bulk_all_off"),
    ])
    btns.append([InlineKeyboardButton(f"💾 تأكيد الإضافة لـ {len(selected)} طبيب", callback_data="comp_bulk_confirm")])
    btns.append([InlineKeyboardButton("🔙 تغيير التخصص", callback_data="comp_bulk_back")])

    await query.edit_message_text(
        f"💊 *الدواء:* {drug_name}\n"
        f"🔬 *التخصص:* {spec_label}\n"
        f"👨‍⚕️ الأطباء: {len(filtered)}  |  ✅ محدد: {len(selected)}\n\n"
        "اضغط على الطبيب لتحديده أو إلغاء تحديده:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btns),
    )


async def _run_smart_search(update, context, user_id: int, text: str):
    tg_user = update.effective_user
    params  = parse_smart_search(text)
    keyword = params.get("keyword", text.strip())

    order_results = await run_blocking(search_orders, user_id, keyword=keyword)
    doctor_results = await run_blocking(search_doctor_profiles, user_id, keyword=keyword)
    comp_drug_results = await run_blocking(search_doctors_by_competitor_drug, user_id, keyword)

    # إزالة الأطباء المكررين (موجودون في doctor_results وcomp_drug_results)
    doctor_ids_in_main = {d["id"] for d in doctor_results}
    comp_drug_results  = [d for d in comp_drug_results if d["id"] not in doctor_ids_in_main]

    if not order_results and not doctor_results and not comp_drug_results:
        await update.message.reply_text(
            f"🔍 لا توجد نتائج لـ «{keyword}»\nجرّب اسماً مختلفاً.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔍 بحث مرة أخرى", callback_data="smart_search_again"),
            ]]),
        )
        return

    cls_emoji = {"A": "🟢", "B": "🟡", "C": "🔵"}
    msg_parts = [f"🔍 *نتائج البحث عن «{keyword}»*\n"]

    # ── نتائج الأطباء (مطابقة الاسم / التخصص / المنطقة) ───────────────────
    if doctor_results:
        msg_parts.append(f"🩺 *أطباء مطابقون ({len(doctor_results)}):*")
        for d in doctor_results[:5]:
            ce   = cls_emoji.get(d.get("class","C"), "🔵")
            line = f"{ce} *{d['name']}*"
            if d.get("specialty"):   line += f" — {d['specialty']}"
            if d.get("area"):        line += f" — {d['area']}"
            if d.get("clinic_name"): line += f" | {d['clinic_name']}"
            # عرض أدوية المنافسين المرتبطة بهذا الطبيب
            comp_drugs = await run_blocking(get_competitor_drugs_for_doctor, user_id, d["id"])
            if comp_drugs:
                names = ", ".join(cd["drug_name"] for cd in comp_drugs[:4])
                line += f"\n   💊 منافسون: {names}"
            msg_parts.append(line)
        if len(doctor_results) > 5:
            msg_parts.append(f"_...و {len(doctor_results)-5} آخرين_")
        msg_parts.append("")

    # ── أطباء يستخدمون الدواء المنافس ──────────────────────────────────────
    if comp_drug_results:
        msg_parts.append(f"💊 *أطباء يستخدمون «{keyword}» كدواء منافس ({len(comp_drug_results)}):*")
        for d in comp_drug_results[:8]:
            ce   = cls_emoji.get(d.get("class","C"), "🔵")
            line = f"{ce} *{d['name']}*"
            if d.get("specialty"): line += f" — {d['specialty']}"
            if d.get("area"):      line += f" — {d['area']}"
            line += f"\n   💊 {d.get('matched_drug', keyword)}"
            msg_parts.append(line)
        if len(comp_drug_results) > 8:
            msg_parts.append(f"_...و {len(comp_drug_results)-8} آخرين_")
        msg_parts.append("")

    # ── نتائج الطلبات ────────────────────────────────────────────────────────
    if order_results:
        msg_parts.append(f"📦 *طلبات مطابقة ({len(order_results)}){' (أول 15)' if len(order_results) > 15 else ''}:*")
        for r in order_results[:15]:
            date_str  = r.get("order_date") or "—"
            ph        = r.get("pharmacy") or "—"
            item      = r.get("item_name") or "—"
            qty       = r.get("quantity") or "—"
            bonus_str = f" +{r['bonus']}" if r.get("bonus") else ""
            msg_parts.append(f"📦 {date_str} | {ph}\n   💊 {item} × {qty}{bonus_str}")

    search_again_btn = [[InlineKeyboardButton("🔍 بحث مرة أخرى", callback_data="smart_search_again")]]

    await update.message.reply_text(
        "\n".join(msg_parts),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(search_again_btn),
    )

    if len(order_results) > 3:
        try:
            excel = await run_blocking(generate_excel_report, order_results)
            if excel:
                safe_label = re.sub(r'[^\w\u0600-\u06FF]', '_', keyword)[:30]
                await update.message.reply_document(
                    document=excel,
                    filename=f"بحث_{safe_label}.xlsx",
                    caption=f"📊 Excel نتائج البحث عن «{keyword}» ({len(order_results)} طلب)",
                )
        except Exception:
            pass

  
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    tg_user = update.effective_user

    if is_admin(tg_user.id) and text.startswith("/"):
        await handle_admin(update, context)
        return

    user_id = await run_blocking(get_or_create_user, tg_user.id, tg_user.full_name, tg_user.username)

    # ── تدفق التسجيل (أولوية قصوى — قبل كل شيء) ─────────────────────────────
    if context.user_data.get("waiting_reg_fullname"):
        words = text.strip().split()
        if len(words) < 2:
            await update.message.reply_text(
                "⚠️ الرجاء إدخال *الاسم الثلاثي* (كلمتان على الأقل).\n\n"
                "_(مثال: طه محمد علي)_",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        context.user_data.pop("waiting_reg_fullname", None)
        context.user_data["reg_fullname"] = text.strip()
        context.user_data["waiting_reg_password"] = True
        await update.message.reply_text(
            f"✅ *اسمك:* {text.strip()}\n\n"
            "الآن اختر *كلمة مرور* لحسابك:\n"
            "_(أرسلها وستُحفظ مشفّرة — لا تشاركها مع أحد)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if context.user_data.get("waiting_reg_password"):
        password = text.strip()
        if len(password) < 4:
            await update.message.reply_text(
                "⚠️ كلمة المرور قصيرة جداً (٤ أحرف على الأقل). حاول مجدداً:",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        full_name = context.user_data.pop("reg_fullname", tg_user.full_name)
        context.user_data.pop("waiting_reg_password", None)
        pw_hash = _hash_password(password)
        await run_blocking(register_user, tg_user.id, full_name, pw_hash)
        # احفظ الاسم الثلاثي في rep_name_memory أيضاً للتوافق مع الكود القديم
        await run_blocking(set_rep_name_memory, user_id, full_name)

        sub = check_subscription(user_id)
        await update.message.reply_text(
            f"🎉 *تم إنشاء حسابك بنجاح!*\n\n"
            f"👤 *الاسم الثلاثي:* {full_name}\n"
            f"🔑 *كلمة المرور:* محفوظة ومشفّرة\n\n"
            "الآن يمكنك البدء باستخدام البوت. أرسل طلبك أو اختر من القائمة:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu(sub, is_admin_user=is_admin(tg_user.id)),
        )
        return

    # ── حجب المستخدمين غير المسجّلين ─────────────────────────────────────────
    if not await run_blocking(is_user_registered, tg_user.id) and not is_admin(tg_user.id):
        context.user_data["waiting_reg_fullname"] = True
        await update.message.reply_text(
            "⚠️ يجب إكمال التسجيل أولاً.\n\nأرسل *اسمك الثلاثي:*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── انتظار رقم الشهر من الأدمن لتقرير مندوب بعينه ───────────────────────
    if context.user_data.get("waiting_admin_rep_month") and is_admin(tg_user.id):
        from admin import _admin_single_rep_excel_msg
        parts = text.strip().split()
        month_num = None
        year_num  = date.today().year
        try:
            if len(parts) >= 2:
                month_num = int(parts[0])
                year_num  = int(parts[1])
            else:
                month_num = int(parts[0])
        except ValueError:
            pass

        if not month_num or not (1 <= month_num <= 12):
            await update.message.reply_text(
                "⚠️ أرسل رقم الشهر بشكل صحيح (1-12)\nمثال: `3` أو `3 2024`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        target_uid = context.user_data.pop("waiting_admin_rep_month")
        months_ar  = {1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
                      7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"}
        month_name = months_ar.get(month_num, str(month_num))
        await update.message.reply_text(f"⏳ جاري تجهيز تقرير *{month_name} {year_num}*...", parse_mode=ParseMode.MARKDOWN)
        try:
            await _admin_single_rep_excel_msg(update.message, target_uid, year_num, month_num)
        except Exception as e:
            logger.error(f"Admin rep other month error: {e}")
            await update.message.reply_text(f"❌ حدث خطأ: {e}")
        return

    # ── انتظار رقم الشهر لتقرير تسديدات مندوب واحد (من لوحة الأدمن) ─────────
    if context.user_data.get("waiting_admin_pay_rep_month") and is_admin(tg_user.id):
        from admin import _admin_pay_rep_month_msg
        parts = text.strip().split()
        month_num, year_num = None, date.today().year
        try:
            if len(parts) >= 2:
                month_num, year_num = int(parts[0]), int(parts[1])
            else:
                month_num = int(parts[0])
        except ValueError:
            pass
        if not month_num or not (1 <= month_num <= 12):
            await update.message.reply_text(
                "⚠️ أرسل رقم الشهر بشكل صحيح (1-12)\nمثال: `3` أو `3 2024`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        target_uid = context.user_data.pop("waiting_admin_pay_rep_month")
        await update.message.reply_text("⏳ جاري تجهيز تقرير التسديدات...", parse_mode=ParseMode.MARKDOWN)
        try:
            await _admin_pay_rep_month_msg(update.message, target_uid, year_num, month_num,
                                            back_cb=f"adm_pay_rep_menu_{target_uid}")
        except Exception as e:
            logger.error(f"Admin pay rep other month error: {e}")
            await update.message.reply_text(f"❌ حدث خطأ: {e}")
        return

    # ── انتظار رقم الشهر لاستيراد Excel ──────────────────────────────────────
    if context.user_data.get("waiting_import_month"):
        parts = text.strip().split()
        month_num = None
        year_num  = date.today().year
        try:
            if len(parts) >= 2:
                month_num = int(parts[0])
                year_num  = int(parts[1])
            else:
                month_num = int(parts[0])
        except ValueError:
            pass

        if not month_num or not (1 <= month_num <= 12):
            await update.message.reply_text(
                "⚠️ أرسل رقم الشهر بشكل صحيح (1-12)\nمثال: `4` أو `4 2025`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        context.user_data.pop("waiting_import_month", None)
        await _do_import_parse(update, context, month_num, year_num)
        return

    # ── التسديدات: صيدلية يدوية ─────────────────────────────────────────────
    if context.user_data.get("pmt_awaiting_pharmacy_manual"):
        context.user_data.pop("pmt_awaiting_pharmacy_manual", None)
        pharmacy_name = text.strip()
        pending_amount = context.user_data.pop("pmt_new_amount", None)
        if pending_amount:
            # المبلغ معروف مسبقاً (جاي من مسار النص الحر) — احفظ مباشرة
            notes = context.user_data.pop("pmt_new_notes", None)
            pid = await run_blocking(save_payment, user_id, tg_user.full_name, pharmacy_name, pending_amount,
                                      None, "cash", notes)
            await update.message.reply_text(
                f"✅ *تم تسجيل التسديد!*\n\n🏪 الصيدلية: {pharmacy_name}\n💰 المبلغ: {pending_amount:,.0f} د.ع",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_payment_post_save_keyboard(pid),
            )
            return
        context.user_data["pmt_new_pharmacy"] = pharmacy_name
        context.user_data["pmt_awaiting_amount"] = True
        await update.message.reply_text(
            f"🏪 *{pharmacy_name}*\n\n💰 أرسل *المبلغ* (مثال: 50 أو خمسين الف أو مليون ونص):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── التسديدات: إدخال المبلغ (بعد اختيار الصيدلية) ───────────────────────
    if context.user_data.get("pmt_awaiting_amount"):
        context.user_data.pop("pmt_awaiting_amount", None)
        pharmacy_name = context.user_data.pop("pmt_new_pharmacy", None)
        amount = parse_amount_words(text)
        if not amount:
            context.user_data["pmt_awaiting_amount"] = True
            context.user_data["pmt_new_pharmacy"] = pharmacy_name
            await update.message.reply_text(
                "⚠️ ما فهمت المبلغ. أرسله مرة ثانية (مثال: 50 أو خمسين الف):",
            )
            return
        pid = await run_blocking(save_payment, user_id, tg_user.full_name, pharmacy_name, amount)
        await update.message.reply_text(
            f"✅ *تم تسجيل التسديد!*\n\n🏪 الصيدلية: {pharmacy_name}\n💰 المبلغ: {amount:,.0f} د.ع",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_payment_post_save_keyboard(pid),
        )
        return

    # ── التسديدات: تعديل حقل بتسديد محفوظ ───────────────────────────────────
    if context.user_data.get("pmt_awaiting_edit"):
        pe = context.user_data.pop("pmt_awaiting_edit")
        pe_pid, pe_field = pe["payment_id"], pe["field"]
        val = text.strip()
        try:
            if pe_field == "amount":
                parsed = parse_amount_words(val)
                if not parsed:
                    await update.message.reply_text("⚠️ ما فهمت المبلغ، حاول مرة ثانية.")
                    context.user_data["pmt_awaiting_edit"] = pe
                    return
                await run_blocking(update_payment_field, pe_pid, "amount", parsed)
            else:
                await run_blocking(update_payment_field, pe_pid, pe_field, val)
        except Exception as _pmt_err:
            logger.error(f"pmt_awaiting_edit error payment={pe_pid} field={pe_field}: {_pmt_err}")
            await update.message.reply_text(f"❌ حدث خطأ أثناء التعديل: {_pmt_err}")
            return
        await update.message.reply_text(
            "✅ *تم تعديل التسديد بنجاح.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_payment_post_save_keyboard(pe_pid),
        )
        return

    # ── التسديدات: انتظار رقم الشهر لتقرير "شهور أخرى" ──────────────────────
    if context.user_data.get("waiting_pmt_report_month"):
        parts = text.strip().split()
        month_num, year_num = None, date.today().year
        try:
            if len(parts) >= 2:
                month_num, year_num = int(parts[0]), int(parts[1])
            else:
                month_num = int(parts[0])
        except ValueError:
            pass
        if not month_num or not (1 <= month_num <= 12):
            await update.message.reply_text("⚠️ أرسل رقم الشهر بشكل صحيح (1-12)\nمثال: `3` أو `3 2025`", parse_mode=ParseMode.MARKDOWN)
            return
        context.user_data.pop("waiting_pmt_report_month", None)
        date_from = date(year_num, month_num, 1).isoformat()
        date_to = date(year_num, month_num + 1, 1).isoformat() if month_num < 12 else date(year_num + 1, 1, 1).isoformat()
        payments = await run_blocking(get_payments_by_rep, user_id, {"date_from": date_from, "date_to": date_to})
        if not payments:
            await update.message.reply_text("📭 لا توجد تسديدات بهذا الشهر.", reply_markup=build_reports_menu())
        else:
            pharmacies = sorted({p["pharmacy_name"] for p in payments})
            balances = [await run_blocking(get_pharmacy_balance, user_id, ph) for ph in pharmacies]
            buf = await run_blocking(generate_payment_excel, payments, balances, None, False)
            fname = f"تسديدات_{year_num}_{month_num:02d}.xlsx"
            await update.message.reply_document(document=buf, filename=fname, caption="💰 تقرير التسديدات")
        return

    # ── انتظار رقم الشهر لتقرير "شهور أخرى" ─────────────────────────────────
    if context.user_data.get("waiting_report_month"):
        parts = text.strip().split()
        month_num = None
        year_num = date.today().year
        try:
            if len(parts) >= 2:
                month_num = int(parts[0])
                year_num  = int(parts[1])
            else:
                month_num = int(parts[0])
        except ValueError:
            pass

        if not month_num or not (1 <= month_num <= 12):
            await update.message.reply_text(
                "⚠️ أرسل رقم الشهر بشكل صحيح (1-12)\n"
                "مثال: `3` أو `3 2025`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        context.user_data.pop("waiting_report_month", None)
        months_ar = {
            1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
            7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"
        }
        month_name = months_ar.get(month_num, str(month_num))
        await update.message.reply_text(f"⏳ جاري تجهيز تقرير *{month_name} {year_num}*...", parse_mode=ParseMode.MARKDOWN)
        try:
            orders = await run_blocking(fetch_orders, user_id, {"month": month_num, "year": year_num})
            if not orders:
                await update.message.reply_text(
                    f"📭 لا توجد طلبات في *{month_name} {year_num}*.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_reports_menu(),
                )
            else:
                summary = build_text_summary(orders, "general")
                await update.message.reply_text(
                    summary, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_reports_menu(),
                )
                buf = await run_blocking(_generate_rep_template_excel, user_id, orders, month_num, year_num)
                fname = f"تقرير_{year_num}_{month_num:02d}_{month_name}.xlsx"
                await update.message.reply_document(
                    document=buf,
                    filename=fname,
                    caption=f"📊 تقرير Excel — {month_name} {year_num}",
                )
                await _notify_admin(
                    context,
                    f"📊 تقرير {tg_user.full_name} — {month_name} {year_num}",
                    excel=buf,
                    filename=fname,
                )
        except Exception as e:
            logger.error(f"Report other month error: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء تجهيز التقرير.", reply_markup=build_reports_menu())
        return

    # ── إلغاء عام ────────────────────────────────────────────────────────────
    if text.strip() in ("إلغاء", "الغاء", "cancel", "وقف", "بطّل", "بطل", "لا", "اوقف"):
        context.user_data.pop("guided_order", None)
        context.user_data.pop("waiting_for_search", None)
        context.user_data.pop("waiting_doctor_entry", None)
        context.user_data.pop("doctor_ai_suggestion", None)
        context.user_data.pop("waiting_competitor_entry", None)
        await update.message.reply_text("❌ تم الإلغاء.", reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)))
        return

    # ── تعديل سعر دواء بالكتالوج ─────────────────────────────────────────────
    if context.user_data.get("waiting_admin_search"):
        if await handle_admin_search_text(update, context):
            return

    if context.user_data.get("waiting_admin_ai_key"):
        if await handle_admin_ai_key_text(update, context):
            return

    if context.user_data.get("waiting_catalog_price_edit"):
        product_id = context.user_data.pop("waiting_catalog_price_edit")
        if text.strip().lower() in ("إلغاء", "cancel", "لا"):
            await update.message.reply_text(
                "❌ تم الإلغاء.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💊 الكتالوج", callback_data="catalog_menu")
                ]])
            )
            return
        cleaned = text.strip().replace(",", "")
        try:
            new_price = float(cleaned)
            if new_price < 0:
                raise ValueError
        except ValueError:
            context.user_data["waiting_catalog_price_edit"] = product_id
            await update.message.reply_text(
                "⚠️ أرسل رقم صحيح فقط (مثلاً `12000`)، أو *إلغاء* للتراجع.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await run_blocking(update_product_price, product_id, user_id, new_price)
        await update.message.reply_text(
            f"✅ تم تحديث السعر إلى {int(new_price):,} IQD.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💊 الكتالوج", callback_data="catalog_menu")
            ]])
        )
        return

    # ── إضافة دواء/أدوية للكتالوج ────────────────────────────────────────────
    if context.user_data.get("waiting_catalog_add"):
        if text.strip().lower() in ("إلغاء", "cancel", "لا"):
            context.user_data.pop("waiting_catalog_add", None)
            await update.message.reply_text(
                "❌ تم الإلغاء.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💊 الكتالوج", callback_data="catalog_menu")
                ]])
            )
            return

        import re as _re

        def _parse_drug_line(raw_line: str):
            """استخرج (drug_name, price) من سطر واحد."""
            raw_line = raw_line.strip()
            if not raw_line:
                return None, None
            m = _re.match(r'^(.+?)[\s\-\|/,]+(\d[\d,\.]*)$', raw_line)
            if m:
                drug_name = m.group(1).strip()
                try:
                    price = float(m.group(2).replace(",", ""))
                except ValueError:
                    price = 0.0
            else:
                drug_name = raw_line
                price = 0.0
            return drug_name, price

        raw_lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        added = []
        for line in raw_lines:
            name, price = _parse_drug_line(line)
            if name and len(name) >= 2:
                await run_blocking(add_product_to_catalog, user_id, name, price)
                price_str = f"{int(price):,}" if price else "—"
                added.append(f"💊 {name}  |  {price_str} IQD")

        context.user_data.pop("waiting_catalog_add", None)

        if not added:
            await update.message.reply_text(
                "⚠️ لم أتمكن من قراءة اسم الدواء. أعد المحاولة مثلاً:\n"
                "`Trazola - 12000`\n"
                "أو أكثر من سطر:\n"
                "`Trazola 12000\nMaxiflex 8500\nPirocomplex 5000`",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data["waiting_catalog_add"] = True
            return

        count = len(added)
        summary = "\n".join(added[:15])
        if count > 15:
            summary += f"\n…و{count-15} أدوية أخرى"
        await update.message.reply_text(
            f"✅ *تمت إضافة {count} {'دواء' if count == 1 else 'دواء'}!*\n\n{summary}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("➕ إضافة المزيد", callback_data="catalog_add"),
                    InlineKeyboardButton("💊 الكتالوج", callback_data="catalog_menu"),
                ]
            ])
        )
        return

    # ── إدخال التاركت ────────────────────────────────────────────────────────
    if context.user_data.get("waiting_target_drug"):
        drug_name = context.user_data.get("waiting_target_drug")
        period = context.user_data.get("waiting_target_period",
                                       f"{date.today().year}-{date.today().month:02d}")
        try:
            target_val = int(text.replace(",", "").replace(".", "").strip())
            if target_val <= 0:
                raise ValueError("must be positive")
            context.user_data.pop("waiting_target_drug", None)
            context.user_data.pop("waiting_target_period", None)
            await run_blocking(set_drug_target, user_id, drug_name, target_val, period)
            await update.message.reply_text(
                f"✅ *تم حفظ التاركت!*\n\n"
                f"💊 الدواء: *{drug_name}*\n"
                f"🎯 التاركت: *{target_val:,} وحدة*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎯 تعيين تاركت آخر", callback_data="tgt_menu"),
                    InlineKeyboardButton("💊 الكتالوج", callback_data="catalog_menu"),
                ]])
            )
        except (ValueError, TypeError):
            await update.message.reply_text(
                "⚠️ الرجاء إرسال *رقم صحيح موجب* فقط، مثال:\n`500`",
                parse_mode=ParseMode.MARKDOWN
            )
        return

    # ── وضع البحث الذكي ──────────────────────────────────────────────────────
    if context.user_data.get("waiting_for_search"):
        context.user_data.pop("waiting_for_search", None)
        await _run_smart_search(update, context, user_id, text)
        return

    # ── الإدخال الموجّه (حقل بحقل) ──────────────────────────────────────────
    if context.user_data.get("guided_order"):
        guided = context.user_data["guided_order"]
        fields = guided["fields"]
        idx = guided["current_idx"]
        order_data = guided["data"]

        # إذا أرسل المستخدم أسطراً متعددة تكفي لملء بقية الحقول → إدخال دفعي
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        remaining = fields[idx:]
        if len(lines) >= 2:
            context.user_data.pop("guided_order", None)
            for i, field_key in enumerate(remaining):
                val = lines[i] if i < len(lines) else None
                if field_key == "item_name" and val:
                    qty, bonus = parse_quantity_bonus(val)
                    if qty is not None:
                        order_data["quantity"] = qty
                        order_data["bonus"] = str(bonus) if bonus else None
                        clean = re.sub(r'\d+\s*[\+x×]\s*\d+|\d+', '', val).strip()
                        order_data[field_key] = clean or val
                    else:
                        order_data[field_key] = val
                elif field_key == "quantity" and val:
                    qty, bonus = parse_quantity_bonus(val)
                    order_data["quantity"] = qty
                    if bonus and not order_data.get("bonus"):
                        order_data["bonus"] = str(bonus)
                else:
                    order_data[field_key] = val
            if not order_data.get("order_date"):
                order_data["order_date"] = date.today().isoformat()
            settings = await run_blocking(get_user_settings, user_id)
            order_data["rep_name"] = await _get_order_rep_name(user_id, tg_user)
            saved_oid = await run_blocking(save_order, user_id, order_data)
            reply = format_order_confirmation(order_data)
            post_kbd = build_post_save_keyboard(saved_oid) if saved_oid else build_main_menu(is_admin_user=is_admin(tg_user.id))
            await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN, reply_markup=post_kbd)
            await _notify_admin(context, f"📋 *طلب جديد من {order_data.get('rep_name') or tg_user.full_name}*\n\n{reply}")
            return

        field_key = fields[idx]
        value = text.strip() if text.strip() not in ("تخطي", "skip", "-") else None
        if field_key == "item_name" and value:
            qty, bonus = parse_quantity_bonus(value)
            if qty is not None:
                order_data["quantity"] = qty
                order_data["bonus"] = str(bonus) if bonus else None
                clean = re.sub(r'\d+\s*[\+x×]\s*\d+|\d+', '', value).strip()
                order_data[field_key] = clean or value
            else:
                order_data[field_key] = value
        elif field_key == "quantity" and value:
            qty, bonus = parse_quantity_bonus(value)
            order_data["quantity"] = qty
            if bonus and not order_data.get("bonus"):
                order_data["bonus"] = str(bonus)
        else:
            order_data[field_key] = value
        idx += 1
        guided["current_idx"] = idx
        context.user_data["guided_order"] = guided

        if idx >= len(fields):
            context.user_data.pop("guided_order", None)
            if not order_data.get("order_date"):
                order_data["order_date"] = date.today().isoformat()
            order_data["rep_name"] = await _get_order_rep_name(user_id, tg_user)
            saved_oid2 = await run_blocking(save_order, user_id, order_data)
            reply = format_order_confirmation(order_data)
            post_kbd2 = build_post_save_keyboard(saved_oid2) if saved_oid2 else build_main_menu(is_admin_user=is_admin(tg_user.id))
            await update.message.reply_text(
                reply, parse_mode=ParseMode.MARKDOWN, reply_markup=post_kbd2
            )
            await _notify_admin(
                context,
                f"📋 *طلب جديد من {order_data.get('rep_name') or tg_user.full_name}*\n\n{reply}",
            )
        else:
            next_label = FIELD_LABELS.get(fields[idx], fields[idx])
            done_label = FIELD_LABELS.get(field_key, field_key)
            await update.message.reply_text(
                f"{'✅' if value else '⏭️'} {done_label}\n\n"
                f"أرسل *{next_label}:*\n"
                f"_({idx}/{len(fields)} — أرسل «تخطي» لتركه فارغاً)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── تعديل حقل في طلب محفوظ (أزرار ما بعد الحفظ) ────────────────────────
    if context.user_data.get("ps_awaiting"):
        ps = context.user_data.pop("ps_awaiting")
        ps_oid   = ps["order_id"]
        ps_field = ps["field"]
        val = text.strip()
        try:
            if ps_field == "item_name":
                qty, bonus = parse_quantity_bonus(val)
                # إصلاح: نبدأ الحذف من أول رقم فقط (لا من حرف x)
                # هذا يمنع حذف آخر حرف من أسماء مثل neatrix أو maxiflex
                name_part = re.sub(r'[\d٠-٩][\d٠-٩\+\-×x\s]*$', '', val).strip() or val
                await run_blocking(update_order_field, ps_oid, "item_name", name_part)
                if qty:
                    await run_blocking(update_order_field, ps_oid, "quantity", qty)
                if bonus:
                    await run_blocking(update_order_field, ps_oid, "bonus", str(bonus))
            elif ps_field == "quantity":
                qty, bonus = parse_quantity_bonus(val)
                await run_blocking(update_order_field, ps_oid, "quantity", qty)
                if bonus:
                    await run_blocking(update_order_field, ps_oid, "bonus", str(bonus))
            elif ps_field == "price":
                m = re.search(r'[\d\.]+', val.replace(',', ''))
                await run_blocking(update_order_field, ps_oid, "price", float(m.group()) if m else None)
            else:
                await run_blocking(update_order_field, ps_oid, ps_field, val)
        except Exception as _edit_err:
            logger.error(f"ps_awaiting edit error order={ps_oid} field={ps_field}: {_edit_err}")
            await update.message.reply_text(
                f"❌ حدث خطأ أثناء التعديل: {_edit_err}\nحاول مجدداً.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # أظهر الطلب المحدّث مع رسالة النجاح
        try:
            updated_row = await run_blocking(get_order_by_id, ps_oid)
        except Exception:
            updated_row = None
        if updated_row:
            ph_u   = updated_row.get("pharmacy") or "—"
            item_u = updated_row.get("item_name") or "—"
            qty_u  = updated_row.get("quantity") or "—"
            bon_u  = updated_row.get("bonus")
            area_u = updated_row.get("area") or "—"
            date_u = updated_row.get("order_date") or "—"
            bon_txt = f" +{bon_u}" if bon_u else ""
            edit_summary = (
                f"✅ *تم تعديل الطلب #{ps_oid} بنجاح!*\n\n"
                f"📅 التاريخ: {date_u}\n"
                f"📍 المنطقة: {area_u}\n"
                f"🏪 الصيدلية: {ph_u}\n"
                f"💊 المنتج: {item_u} × {qty_u}{bon_txt}"
            )
        else:
            edit_summary = f"✅ *تم تعديل الطلب #{ps_oid} بنجاح.*"
        await update.message.reply_text(
            edit_summary,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_post_save_keyboard(ps_oid),
        )
        return

    if context.user_data.get("awaiting_field"):
        field_key = context.user_data.pop("awaiting_field")
        review = context.user_data.get("review_order")
        review_multi = context.user_data.get("review_multi")
        if review is not None:
            if field_key == "item_name":
                qty, bonus = parse_quantity_bonus(text)
                # إصلاح: نبدأ الحذف من أول رقم لا من حرف x (يمنع تقليم neatrix → neatri)
                name_part = re.sub(r'[\d٠-٩][\d٠-٩\+\-×x\s]*$', '', text).strip() or text.strip()
                review["item_name"] = name_part
                if qty:
                    review["quantity"] = qty
                if bonus:
                    review["bonus"] = str(bonus)
            else:
                review[field_key] = text.strip()
            preview = format_order_preview(review)
            await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN,
                                            reply_markup=build_order_review_keyboard())
            # أعد جدولة الحفظ التلقائي بـ 30 ثانية من لحظة اكتمال التعديل
            _schedule_autosave(context, update.effective_chat.id, {
                "user_id": user_id,
                "order": review,
                "multi": None,
                "tg_name": tg_user.full_name,
            })
        elif review_multi is not None:
            review_multi["base_data"][field_key] = text.strip()
            preview = format_order_preview(review_multi["base_data"], is_multi=True,
                                           products=review_multi["products"])
            await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN,
                                            reply_markup=build_order_review_keyboard(is_multi=True))
            # أعد جدولة الحفظ التلقائي بـ 30 ثانية من لحظة اكتمال التعديل
            _schedule_autosave(context, update.effective_chat.id, {
                "user_id": user_id,
                "order": None,
                "multi": review_multi,
                "tg_name": tg_user.full_name,
            })
        else:
            await update.message.reply_text("⚠️ انتهت صلاحية الطلب. أعد إرساله.")
        return

    # ── انتظار اسم المندوب من زر الإعدادات ──────────────────────────────────
    if context.user_data.get("waiting_for_name"):
        context.user_data.pop("waiting_for_name", None)
        await run_blocking(set_rep_name_memory, user_id, text)
        await update.message.reply_text(
            f"✅ تم حفظ اسمك: *{text}*\nلن تحتاج لكتابته في كل طلب.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_settings_menu(),
        )
        return

    # ── انتظار كود التفعيل من زر تفعيل الاشتراك ─────────────────────────────
    if context.user_data.get("waiting_for_code"):
        context.user_data.pop("waiting_for_code", None)
        code = re.sub(r'\s+', '', text).upper()
        result = await run_blocking(activate_code, user_id, code)
        if result["success"]:
            plan_name = PLAN_NAMES.get(result["plan"], result["plan"])
            await update.message.reply_text(
                f"🎉 *تم التفعيل بنجاح!*\n\n✅ الخطة: *{plan_name}*\n📅 تنتهي: {result['end_date']}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
            )
        elif result["reason"] == "used":
            await update.message.reply_text("❌ هذا الكود تم استخدامه مسبقاً.", reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)))
        else:
            await update.message.reply_text("❌ الكود غير صحيح. حاول مجدداً.", reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)))
        return

    # ── تفعيل بكتابة "كود XXXXX" يدوياً (بديل نصي) ──────────────────────────
    code_match = re.match(r'^كود\s+([A-Z0-9]{4,})', text, re.IGNORECASE)
    if code_match:
        code = code_match.group(1).upper()
        result = await run_blocking(activate_code, user_id, code)
        if result["success"]:
            plan_name = PLAN_NAMES.get(result["plan"], result["plan"])
            await update.message.reply_text(
                f"🎉 *تم التفعيل بنجاح!*\n\n✅ الخطة: *{plan_name}*\n📅 تنتهي: {result['end_date']}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
            )
        elif result["reason"] == "used":
            await update.message.reply_text("❌ هذا الكود تم استخدامه مسبقاً.")
        else:
            await update.message.reply_text("❌ الكود غير صحيح.")
        return

    sub = check_subscription(user_id)

    # ── طلب اشتراك بالكلام ───────────────────────────────────────────────────
    # مهم: كلمة "pro" وحدها محذوفة من القائمة لأنها قد تكون اسم دواء
    # فقط نكتشف "pro" إذا كانت مع كلمات الاشتراك
    subscribe_triggers = re.compile(
        r'(اريد اشتراك|أريد اشتراك|ابي اشتراك|أبي اشتراك|'
        r'اشتراك pro|pro اشتراك|اشتراك برو|'
        r'اشترك|ترقية|اريد الترقية|أريد الترقية|'
        r'شلون اشترك|كيف اشترك|باچر ينتهي|ينتهي اشتراكي|'
        r'(?:اريد|أريد|ابي|أبي)\s+(?:pro|برو))',
        re.IGNORECASE
    )
    if subscribe_triggers.search(text) and not context.user_data.get("waiting_for_code"):
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"💳 شهري — {PRICES['monthly']}", callback_data="subscribe_monthly"),
                InlineKeyboardButton(f"🌟 سنوي — {PRICES['yearly']}", callback_data="subscribe_yearly"),
            ],
        ])
        await update.message.reply_text(
            f"🚀 *الترقية إلى Pro*\n\n"
            f"• شهري: *{PRICES['monthly']}*\n"
            f"• سنوي: *{PRICES['yearly']}*\n\n"
            "اختر الخطة وسأعطيك رقم الحساب للدفع:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return

    # ── حظر كامل ────────────────────────────────────────────────────────────
    if sub.get("reason") == "banned":
        await update.message.reply_text(format_banned_message(), parse_mode=ParseMode.MARKDOWN)
        return

    # ── تسجيل الأوردرات مسموح حتى بعد انتهاء الاشتراك (soft lock) ──────────
    # في حالة soft_locked: الأوردرات تعمل، الميزات الأخرى مقيّدة لكن يستمر
    # بدون إيقاف كامل

    if sub.get("days_left") is not None and sub["days_left"] <= 3 and not sub.get("soft_locked") and sub.get("plan") in ("basic", "trial"):
        await update.message.reply_text(
            f"⚠️ *تبقى {sub['days_left']} يوم* من شهرك المجاني!\n"
            "بعدها تقدر تكمل تسجيل الأوردرات مجاناً، أو اشترك بـ Pro للحصول على التقارير والميزات الكاملة.",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── تعريف read_only (يُحجب تسجيل الطلبات فقط إذا انتهى الاشتراك كلياً) ──
    read_only = not sub.get("allowed") or not can_use(sub, "orders")

    # ══════════════════════════════════════════════════════════════════════════
    # ── Doctor Profile — waiting states (Phase 1, 2, 3) ─────────────────────
    # ══════════════════════════════════════════════════════════════════════════

    # ── حذف طبيب بالاسم ────────────────────────────────────────────────────
    if context.user_data.get("waiting_doc_delete_name"):
        context.user_data.pop("waiting_doc_delete_name", None)
        search_name = text.strip()
        matches = await run_blocking(search_doctor_profiles, user_id, search_name)
        if not matches:
            await update.message.reply_text(
                f"❌ لم يُعثر على طبيب باسم *{search_name}*.\n"
                "تأكد من كتابة الاسم بشكل صحيح.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑️ حاول مجدداً", callback_data="doc_del_by_name")],
                    [InlineKeyboardButton("🔙 قائمة الأطباء", callback_data="menu_doctors")],
                ]),
            )
            return
        if len(matches) == 1:
            doc = matches[0]
            await run_blocking(delete_doctor_profile, doc["id"], user_id)
            await update.message.reply_text(
                f"✅ *تم حذف د. {doc['name']}* ومعلوماته كاملاً من قاعدة البيانات.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 قائمة الأطباء", callback_data="doctor_list_0"),
                    InlineKeyboardButton("🏠 القائمة",        callback_data="menu_main"),
                ]]),
            )
        else:
            # أكثر من نتيجة — اطلب التأكيد بالضغط على الاسم الدقيق
            btns = []
            for d in matches[:8]:
                spec = f" — {d['specialty']}" if d.get("specialty") else ""
                area = f" ({d.get('area','')})" if d.get("area") else ""
                btns.append([InlineKeyboardButton(
                    f"🗑️ {d['name']}{spec}{area}",
                    callback_data=f"doc_del_yes_{d['id']}"
                )])
            btns.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")])
            await update.message.reply_text(
                f"🔍 وُجد *{len(matches)}* طبيب بهذا الاسم — اختر الطبيب المراد حذفه:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(btns),
            )
        return

    # ── استيراد الاستبيان (waiting_survey_import goes through handle_document) ──
    # Handled by handle_document — nothing to do in handle_message for text msgs

    # ── Phase 3: تأكيد اقتراح الذكاء الاصطناعي للطبيب ─────────────────────
    if context.user_data.get("waiting_doctor_entry"):
        context.user_data.pop("waiting_doctor_entry", None)
        context.user_data.pop("doctor_ai_suggestion", None)
        # استخدم AI أولاً (Phase 3) وإلا regex
        suggestion = await run_blocking(ai_suggest_doctor, text)
        if not suggestion or not suggestion.get("name"):
            await update.message.reply_text(
                "⚠️ لم أتمكن من فهم البيانات.\n"
                "حاول مرة أخرى بهذا الشكل:\n"
                "`د. اسم الطبيب — التخصص — المنطقة`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ حاول مجدداً", callback_data="doctor_add"),
                    InlineKeyboardButton("🔙 رجوع",        callback_data="menu_doctors"),
                ]]),
            )
            return
        # احفظ الاقتراح مؤقتاً حتى يؤكد المندوب
        context.user_data["doctor_ai_suggestion"] = suggestion
        # عرض الفئة: استخدم ما أدخله المندوب إن صلح، وإلا احسبها تلقائياً
        user_cls = (suggestion.get("class") or "").strip().upper()
        valid_classes = {"A", "A+", "B", "C", "Z"}
        if user_cls in valid_classes:
            cls_preview = user_cls
            cls_label   = f"الفئة (من المدخل)"
        else:
            cls_preview = await run_blocking(calculate_doctor_class, user_id, suggestion["name"])
            cls_label   = "الفئة المتوقعة"
        ce = {"A": "🟢", "A+": "🌟", "B": "🟡", "C": "🔵", "Z": "⚫"}.get(cls_preview, "🔵")
        preview = (
            f"🩺 *تأكيد بيانات الطبيب*\n\n"
            f"👨‍⚕️ الاسم: *{suggestion.get('name','')}*\n"
            f"🔬 التخصص: {suggestion.get('specialty') or '—'}\n"
            f"🏥 العيادة: {suggestion.get('clinic_name') or '—'}\n"
            f"📍 المنطقة: {suggestion.get('area') or '—'}\n"
            f"📞 الهاتف: {suggestion.get('phone') or '—'}\n"
            f"{ce} {cls_label}: *{cls_preview}*\n"
            f"📝 ملاحظات: {suggestion.get('notes') or '—'}\n\n"
            "_هل البيانات صحيحة؟_"
        )
        await update.message.reply_text(
            preview,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، احفظ",     callback_data="doctor_confirm_save"),
                 InlineKeyboardButton("✏️ تعديل يدوي",   callback_data="doctor_edit_entry")],
                [InlineKeyboardButton("❌ إلغاء",          callback_data="menu_doctors")],
            ]),
        )
        return

    # ══════════════════════════════════════════════════════════════════════
    # ── تدفق إضافة دواء منافس جديد (Competitor Drug Library) ──────────────
    # ══════════════════════════════════════════════════════════════════════

    if context.user_data.get("waiting_comp_drug_name"):
        context.user_data.pop("waiting_comp_drug_name")
        draft = context.user_data.setdefault("comp_drug_draft", {})
        draft["drug_name"] = text.strip()
        context.user_data["waiting_comp_drug_company"] = True
        await update.message.reply_text(
            f"💊 *إضافة دواء منافس — الخطوة 2/5*\n\n"
            f"الدواء: *{draft['drug_name']}*\n\n"
            "أرسل *اسم الشركة المصنّعة:*\n\n"
            "_مثال: AstraZeneca, Pfizer, Sanofi_\n"
            "_أو أرسل — لتخطي هذه الخطوة_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_comp_drugs")
            ]]),
        )
        return

    if context.user_data.get("waiting_comp_drug_company"):
        context.user_data.pop("waiting_comp_drug_company")
        draft = context.user_data.setdefault("comp_drug_draft", {})
        draft["company"] = None if text.strip() in ("—", "-", "لا", "skip") else text.strip()
        # انتقل لاختيار الشكل (callback)
        rows = []
        for i in range(0, len(DRUG_FORMS), 2):
            row = []
            for key, label in DRUG_FORMS[i:i+2]:
                row.append(InlineKeyboardButton(label, callback_data=f"comp_drug_form_{key}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu_comp_drugs")])
        await update.message.reply_text(
            f"💊 *إضافة دواء منافس — الخطوة 3/5*\n\n"
            f"الدواء: *{draft['drug_name']}*\n"
            f"الشركة: {draft.get('company') or '—'}\n\n"
            "اختر *شكل الدواء:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if context.user_data.get("waiting_comp_drug_price"):
        context.user_data.pop("waiting_comp_drug_price")
        draft = context.user_data.setdefault("comp_drug_draft", {})
        raw_price = text.strip().replace(",", "").replace("،", "")
        try:
            draft["price"] = float(raw_price) if raw_price not in ("—", "-", "لا", "skip") else None
        except ValueError:
            draft["price"] = None
        context.user_data["waiting_comp_drug_features"] = True
        await update.message.reply_text(
            f"💊 *إضافة دواء منافس — الخطوة 5/5*\n\n"
            f"الدواء: *{draft.get('drug_name','')}*  |  الشركة: {draft.get('company') or '—'}\n\n"
            "أرسل *مميزات الدواء وملاحظاتك عنه:*\n\n"
            "_مثال: يُعطى مرة يومياً، مريح للمعدة، أرخص بـ30%، له نكهة جيدة_\n"
            "_أو أرسل — لتخطي هذه الخطوة_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ إلغاء", callback_data="menu_comp_drugs")
            ]]),
        )
        return

    if context.user_data.get("waiting_comp_drug_features"):
        context.user_data.pop("waiting_comp_drug_features")
        draft = context.user_data.get("comp_drug_draft", {})
        draft["features"] = None if text.strip() in ("—", "-", "لا", "skip") else text.strip()
        # عرض ملخص التأكيد
        form_lbl = DRUG_FORM_LABELS.get(draft.get("form", ""), "—")
        routes_lbl = " / ".join(
            DRUG_ROUTE_LABELS.get(r.strip(), r.strip())
            for r in (draft.get("routes") or "").split(",") if r.strip()
        ) or "—"
        price_str = f"{float(draft['price']):,.0f} د.ع" if draft.get("price") else "—"
        await update.message.reply_text(
            f"✅ *تأكيد بيانات الدواء المنافس*\n\n"
            f"💊 الاسم: *{draft.get('drug_name','')}*\n"
            f"🏭 الشركة: {draft.get('company') or '—'}\n"
            f"📦 الشكل: {form_lbl}\n"
            f"🔀 طريق الإدخال: {routes_lbl}\n"
            f"💵 السعر: {price_str}\n"
            f"⭐ المميزات: {draft.get('features') or '—'}\n\n"
            "_هل البيانات صحيحة؟_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ حفظ الدواء",  callback_data="comp_drug_confirm"),
                 InlineKeyboardButton("❌ إلغاء",        callback_data="comp_drug_cancel_add")],
            ]),
        )
        return

    # ── Phase 2b: إدخال اسم الدواء المنافس (خطوة 1 من التدفق الجديد) ────────
    if context.user_data.pop("waiting_competitor_bulk_name", False):
        drug_name = text.strip()
        if not drug_name:
            context.user_data["waiting_competitor_bulk_name"] = True
            await update.message.reply_text(
                "⚠️ الرجاء إرسال اسم الدواء المنافس.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")
                ]]),
            )
            return

        context.user_data["comp_bulk_drug_name"] = drug_name
        doctors_all = await run_blocking(get_all_doctor_profiles, user_id)
        if not doctors_all:
            await update.message.reply_text(
                "⚠️ لم تسجّل أي أطباء بعد. أضف أطباء أولاً.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_doctors")
                ]]),
            )
            return

        specialties = sorted({(d.get("specialty") or "").strip() for d in doctors_all if (d.get("specialty") or "").strip()})
        context.user_data["comp_bulk_specialties"] = specialties

        # بناء أزرار التخصصات
        btns = []
        for i, spec in enumerate(specialties[:20]):
            btns.append([InlineKeyboardButton(f"🔬 {spec}", callback_data=f"comp_bulk_spec_{i}")])
        btns.append([InlineKeyboardButton(f"👥 جميع الأطباء ({len(doctors_all)})", callback_data="comp_bulk_spec_ALL")])
        btns.append([InlineKeyboardButton("❌ إلغاء", callback_data="menu_doctors")])

        await update.message.reply_text(
            f"💊 *الدواء المنافس:* {drug_name}\n\n"
            "اختر *التخصص* الذي تريد إضافة الدواء له:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(btns),
        )
        return

    # ── Phase 2: إدخال منافس ───────────────────────────────────────────────
    if context.user_data.get("waiting_competitor_entry"):
        comp_ctx = context.user_data.pop("waiting_competitor_entry")
        doc_id   = comp_ctx["doc_id"]
        doc_name = comp_ctx["doc_name"]
        comp     = parse_competitor_entry(text)
        if not comp.get("competitor_name") and not comp.get("competitor_product"):
            await update.message.reply_text(
                "⚠️ لم أفهم البيانات. مثال:\n`AstraZeneca — Crestor — يروجونه بسعر أقل`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 حاول مجدداً",  callback_data=f"competitor_add_{doc_id}"),
                    InlineKeyboardButton("❌ إلغاء",          callback_data=f"doctor_view_{doc_id}"),
                ]]),
            )
            return
        await run_blocking(
            add_competitor_entry,
            user_id, doc_id,
            comp.get("competitor_name") or "—",
            comp.get("competitor_product") or "—",
            comp.get("notes"),
        )
        await update.message.reply_text(
            f"✅ *تم تسجيل منافس لـ د. {doc_name}*\n\n"
            f"🏭 الشركة: *{comp.get('competitor_name') or '—'}*\n"
            f"💊 المنتج: *{comp.get('competitor_product') or '—'}*\n"
            f"📝 ملاحظات: {comp.get('notes') or '—'}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ إضافة منافس آخر", callback_data=f"competitor_add_{doc_id}"),
                 InlineKeyboardButton("📄 ملف الطبيب",      callback_data=f"doctor_view_{doc_id}")],
                [InlineKeyboardButton("🔙 قائمة الأطباء",   callback_data="doctor_list_0")],
            ]),
        )
        return

    # ── التسديدات: مسار النص الحر ("سدد الحور خمسين") — قبل تصنيف النية العام ─
    if _PAYMENT_TRIGGER.search(text):
        pmt_result = await run_blocking(parse_payment_smart, text)
        if pmt_result and pmt_result.get("amount"):
            amount = pmt_result["amount"]
            pharmacy_raw = (pmt_result.get("pharmacy") or "").strip() or None
            known_ph = await run_blocking(get_known_pharmacies, user_id)
            candidates = fuzzy_match_candidates(pharmacy_raw, known_ph) if pharmacy_raw else []
            # تطابق واضح فقط لو مرشّح واحد، أو الأول أعلى من الثاني بفرق واضح (≥0.10) —
            # وإلا نعتبرها حالة غموض (زي "النور" مقابل "نور الهدى") ونسأل المندوب بدل التخمين.
            matched = None
            ambiguous_candidates = []
            if len(candidates) == 1:
                matched = candidates[0][0]
            elif len(candidates) >= 2:
                if candidates[0][1] - candidates[1][1] >= 0.10:
                    matched = candidates[0][0]
                else:
                    ambiguous_candidates = [c[0] for c in candidates]

            if matched or (pharmacy_raw and not known_ph):
                final_pharmacy = matched or pharmacy_raw
                pid = await run_blocking(
                    save_payment, user_id, tg_user.full_name, final_pharmacy, amount,
                    pmt_result.get("payment_date"), "cash", pmt_result.get("notes"),
                )
                await update.message.reply_text(
                    f"✅ *تم تسجيل التسديد!*\n\n🏪 الصيدلية: {final_pharmacy}\n💰 المبلغ: {amount:,.0f} د.ع",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_payment_post_save_keyboard(pid),
                )
                return

            # حالة غموض (كذا صيدلية متشابهة) — أو لا تطابق إطلاقاً — أو ما انفهم الاسم أصلاً
            context.user_data["pmt_new_amount"] = amount
            context.user_data["pmt_new_notes"] = pmt_result.get("notes")
            if pharmacy_raw and ambiguous_candidates:
                context.user_data["pmt_ph_choices"] = ambiguous_candidates
                context.user_data["pmt_awaiting_pharmacy_confirm_raw"] = pharmacy_raw
                await update.message.reply_text(
                    f"⚠️ لقيت أكثر من صيدلية تشبه *{pharmacy_raw}* — أي وحدة تقصد؟",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        build_payment_pharmacy_keyboard(ambiguous_candidates).inline_keyboard[:-1] + [
                            [InlineKeyboardButton(f"➕ استخدم \"{pharmacy_raw}\" كما هو", callback_data="pmt_ph_use_raw")],
                            [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_main")],
                        ]
                    ),
                )
            elif pharmacy_raw and known_ph:
                context.user_data["pmt_ph_choices"] = known_ph
                context.user_data["pmt_awaiting_pharmacy_confirm_raw"] = pharmacy_raw
                await update.message.reply_text(
                    f"⚠️ ما لقيت صيدلية باسم *{pharmacy_raw}* بسجلاتك.\n\n"
                    f"اختر الصيدلية الصحيحة، أو استخدم الاسم كما كتبته:",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(
                        build_payment_pharmacy_keyboard(known_ph).inline_keyboard[:-1] + [
                            [InlineKeyboardButton(f"➕ استخدم \"{pharmacy_raw}\"", callback_data="pmt_ph_use_raw")],
                            [InlineKeyboardButton("🔙 إلغاء", callback_data="menu_main")],
                        ]
                    ),
                )
            else:
                context.user_data["pmt_awaiting_pharmacy_manual"] = True
                await update.message.reply_text(
                    f"💰 فهمت المبلغ ({amount:,.0f} د.ع) — بس ما فهمت اسم الصيدلية.\n\nأرسل *اسم الصيدلية*:",
                    parse_mode=ParseMode.MARKDOWN,
                )
            return

    intent = await run_blocking(detect_intent, text)

    # ── ضبط اسم المندوب ────────────────────────────────────────────────────
    if intent == "set_rep_name":
        name = re.sub(r'^(اسمي|انا)\s+', '', text, flags=re.IGNORECASE).strip()
        await run_blocking(set_rep_name_memory, user_id, name)
        await update.message.reply_text(
            f"✅ تم حفظ اسمك: *{name}*\nلن تحتاج لكتابته في كل طلب.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    elif intent == "show_rep_name":
        settings = await run_blocking(get_user_settings, user_id)
        name = settings.get("rep_name_memory")
        if name:
            await update.message.reply_text(f"👤 اسمك المحفوظ: *{name}*", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("لا يوجد اسم محفوظ. أرسل: `اسمي طه محمد`", parse_mode=ParseMode.MARKDOWN)
        return

    # ── تعديل آخر طلب ──────────────────────────────────────────────────────
    elif intent == "edit_order":
        last = await run_blocking(get_last_order, user_id)
        if not last:
            await update.message.reply_text("⚠️ لا يوجد طلب سابق للتعديل.")
            return
        edit = parse_edit_instruction(text)
        if not edit["field"] or not edit["value"]:
            await update.message.reply_text(
                "⚠️ لم أفهم التعديل. مثال:\n`عدل آخر طلب الكمية إلى 5`\n`عدل الصيدلية إلى صيدلية النور`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        value = edit["value"]
        if edit["field"] == "quantity":
            qty, bonus = parse_quantity_bonus(value)
            await run_blocking(update_order_field, last["id"], "quantity", qty)
            if bonus:
                await run_blocking(update_order_field, last["id"], "bonus", bonus)
        elif edit["field"] == "price":
            m = re.search(r'[\d\.]+', value.replace(',', ''))
            await run_blocking(update_order_field, last["id"], "price", float(m.group()) if m else None)
        else:
            await run_blocking(update_order_field, last["id"], edit["field"], value)
        await update.message.reply_text(
            f"✅ تم التعديل بنجاح.\n*{edit['field']}* → `{edit['value']}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── حذف آخر طلب ────────────────────────────────────────────────────────
    elif intent == "delete_order":
        last = await run_blocking(get_last_order, user_id)
        if not last:
            await update.message.reply_text("⚠️ لا يوجد طلب لحذفه.")
            return
        await run_blocking(delete_order, last["id"])
        ph = last.get("pharmacy") or "—"
        item = last.get("item_name") or "—"
        await update.message.reply_text(
            f"🗑️ تم حذف الطلب الأخير:\n*{ph}* — {item}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── ملف الصيدلية ────────────────────────────────────────────────────────
    elif intent == "pharmacy_profile":
        ph_name = parse_pharmacy_name(text)
        profile = await run_blocking(get_pharmacy_profile, user_id, ph_name)
        if not profile:
            await update.message.reply_text(
                f"❌ لا توجد سجلات لصيدلية *{ph_name}*. تأكد من الاسم.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        last_3 = profile["last_3_visits"]
        visits_lines = []
        for v in last_3:
            item = v.get("item_name") or "—"
            qty = v.get("quantity") or "—"
            d = v.get("order_date") or "—"
            visits_lines.append(f"  📌 {d} — {item} × {qty}")
        top_products = "\n".join(
            f"  {i+1}. {p[0]} ({p[1]} طلب)"
            for i, p in enumerate(profile["all_products"])
        ) or "  —"
        msg = (
            f"🏥 *ملف صيدلية {profile['pharmacy']}*\n\n"
            f"📅 آخر زيارة: `{profile['last_visit']}`\n"
            f"📦 إجمالي الطلبات: *{profile['total_orders']}*\n"
            f"💊 أكثر منتج: *{profile['top_product']}*\n"
            f"📊 متوسط الكمية: *{profile['avg_quantity']} قطعة*\n\n"
            f"🏆 أكثر المنتجات طلباً:\n{top_products}\n\n"
            f"🕐 آخر {len(last_3)} زيارات:\n" + "\n".join(visits_lines)
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # ── بحث ذكي ─────────────────────────────────────────────────────────────
    elif intent == "doctor_profile":
        # Trigger doctor add flow if triggered by text
        context.user_data["waiting_doctor_entry"] = True
        # Strip common trigger words to get actual text if present
        clean = text
        for trigger in DOCTOR_PROFILE_TRIGGERS:
            clean = clean.replace(trigger, "").strip()
        if clean and len(clean) > 3:
            # User wrote trigger + doctor info together e.g. "سجل طبيب د. أحمد قلب الكرادة"
            suggestion = await run_blocking(ai_suggest_doctor, clean)
            if suggestion and suggestion.get("name"):
                context.user_data.pop("waiting_doctor_entry", None)
                context.user_data["doctor_ai_suggestion"] = suggestion
                cls_preview = await run_blocking(calculate_doctor_class, user_id, suggestion["name"])
                ce = {"A": "🟢", "B": "🟡", "C": "🔵"}.get(cls_preview, "🔵")
                preview = (
                    f"🩺 *تأكيد بيانات الطبيب*\n\n"
                    f"👨‍⚕️ الاسم: *{suggestion.get('name','')}*\n"
                    f"🔬 التخصص: {suggestion.get('specialty') or '—'}\n"
                    f"🏥 العيادة: {suggestion.get('clinic_name') or '—'}\n"
                    f"📍 المنطقة: {suggestion.get('area') or '—'}\n"
                    f"📞 الهاتف: {suggestion.get('phone') or '—'}\n"
                    f"{ce} الفئة المتوقعة: *{cls_preview}*\n"
                    f"📝 ملاحظات: {suggestion.get('notes') or '—'}\n\n"
                    "_هل البيانات صحيحة؟_"
                )
                await update.message.reply_text(
                    preview, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ نعم، احفظ",   callback_data="doctor_confirm_save"),
                         InlineKeyboardButton("✏️ تعديل يدوي", callback_data="doctor_edit_entry")],
                        [InlineKeyboardButton("❌ إلغاء",        callback_data="menu_doctors")],
                    ]),
                )
                return
        await update.message.reply_text(
            "🩺 *ملف الأطباء*\n\nأرسل معلومات الطبيب، مثال:\n"
            "`د. أحمد الجبوري — قلبية — الكرادة`\n\n"
            "_أو اضغط الزر للانتقال للقائمة:_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🩺 قائمة الأطباء", callback_data="menu_doctors"),
            ]]),
        )
        return

    elif intent == "smart_search":
        await _run_smart_search(update, context, user_id, text)
        return

    # ── أداء وترتيب ──────────────────────────────────────────────────────────
    elif intent == "performance":
        sub_chk = check_subscription(user_id)
        if not can_use(sub_chk, "reports"):
            await update.message.reply_text(
                "📈 *أدائي*\n\n🔒 هذه الميزة متاحة فقط لمشتركي Pro.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
                ]]),
            )
            return
        stats = await run_blocking(get_performance_stats, user_id)
        m_ar = MONTH_NUM_TO_AR.get(stats["curr_month"], str(stats["curr_month"]))
        m_prev_ar = MONTH_NUM_TO_AR.get(stats["prev_month"], str(stats["prev_month"]))
        chg = stats["change_pct"]
        chg_icon = "📈" if chg >= 0 else "📉"
        chg_str = f"+{chg}%" if chg >= 0 else f"{chg}%"
        settings = await run_blocking(get_user_settings, user_id)
        manager_id = settings.get("manager_telegram_id")
        ranking_msg = ""
        if manager_id:
            ranking = await run_blocking(get_rep_ranking, manager_id)
            pos = next((i + 1 for i, r in enumerate(ranking) if r["user_id"] == user_id), None)
            if pos:
                medals = {1: "🥇", 2: "🥈", 3: "🥉"}
                medal = medals.get(pos, f"#{pos}")
                ranking_msg = f"\n🏆 *ترتيبك بين المندوبين: {medal}*\n"
                for i, r in enumerate(ranking[:5], 1):
                    m2 = medals.get(i, f"#{i}")
                    marker = " ← أنت" if r["user_id"] == user_id else ""
                    ranking_msg += f"  {m2} {r['name']} — {r['orders']} طلب{marker}\n"
        msg = (
            f"📊 *أداؤك هذا الشهر — {m_ar}*\n\n"
            f"📦 عدد الطلبات: *{stats['curr_orders']}*\n"
            f"📦 الشهر الماضي ({m_prev_ar}): *{stats['prev_orders']}*\n"
            f"{chg_icon} التغيير: *{chg_str}*\n"
            f"💊 أكثر منتج: *{stats['top_product']}*\n"
            f"🏥 عدد الصيدليات: *{stats['pharmacies_count']}*\n"
            f"📦 إجمالي الكميات: *{stats['curr_qty']}*\n"
            f"{ranking_msg}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # ── تفعيل / إيقاف التنبيهات ─────────────────────────────────────────────
    elif intent == "alerts_on":
        # toggle_setting تقبل user_id و field فقط — نفعّل بالضبط
        settings_now = await run_blocking(get_user_settings, user_id)
        if not settings_now.get("alert_inactivity"):
            await run_blocking(toggle_setting, user_id, "alert_inactivity")
        if not settings_now.get("alert_unvisited"):
            await run_blocking(toggle_setting, user_id, "alert_unvisited")
        await update.message.reply_text(
            "🔔 *تم تفعيل التنبيهات الذكية!*\n\n"
            "سأذكّرك إذا:\n"
            "• ما سجلت طلب من 3 ساعات\n"
            "• صيدلية ما زرتها من أسبوع",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    elif intent == "alerts_off":
        settings_now = await run_blocking(get_user_settings, user_id)
        if settings_now.get("alert_inactivity"):
            await run_blocking(toggle_setting, user_id, "alert_inactivity")
        if settings_now.get("alert_unvisited"):
            await run_blocking(toggle_setting, user_id, "alert_unvisited")
        await update.message.reply_text("🔕 تم إيقاف التنبيهات الذكية.")
        return

    # ── مقارنة شهرين ───────────────────────────────────────────────────────
    elif intent == "compare_months":
        sub_chk = check_subscription(user_id)
        if not can_use(sub_chk, "reports"):
            await update.message.reply_text(
                "📊 *مقارنة شهرين*\n\n🔒 هذه الميزة متاحة فقط لمشتركي Pro.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 ترقية إلى Pro", callback_data="menu_subscribe"),
                    InlineKeyboardButton("🔙 رجوع", callback_data="menu_main"),
                ]]),
            )
            return
        m1, m2 = parse_compare_months(text)
        today = date.today()
        orders1 = await run_blocking(fetch_orders, user_id, {"month": m1, "year": today.year})
        orders2 = await run_blocking(fetch_orders, user_id, {"month": m2, "year": today.year})
        name1 = MONTH_NUM_TO_AR.get(m1, str(m1))
        name2 = MONTH_NUM_TO_AR.get(m2, str(m2))
        msg = build_compare_summary(orders1, orders2, name1, name2)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        # إرسال Excel بصيغة التاركت للشهر الحالي مع الأدوية والتاركت
        if (orders1 or orders2) and len(orders1) + len(orders2) > 3:
            try:
                buf = await run_blocking(_generate_rep_template_excel, user_id, (orders1 or []) + (orders2 or []), today.month, today.year)
                await update.message.reply_document(
                    document=buf,
                    filename=f"مقارنة_{name1}_{name2}.xlsx",
                    caption=f"📊 Excel مقارنة {name1} و {name2} (بصيغة التاركت)",
                )
            except Exception:
                pass
        return

    # ── ضبط القالب ─────────────────────────────────────────────────────────
    elif intent == "set_template":
        field_order = parse_template_definition(text)
        # نصفّي الحقول غير المعروفة (اللي تبدأ بـ ?:)
        known_fields = [f for f in field_order if not f.startswith("?:")]
        if len(known_fields) < 2:
            await update.message.reply_text(
                "⚠️ *ما فهمت الترتيب!*\n\n"
                "قولي الترتيب بأي طريقة، مثلاً:\n\n"
                "`ترتيبي: صيدلية / علاج / كمية / سعر / كاش`\n"
                "`طريقتي: صيدلية ثم دكتور ثم علاج ثم كمية`\n"
                "`خلي الترتيب: 1. صيدلية 2. علاج 3. دفع`\n\n"
                "الحقول المتاحة: صيدلية، علاج، كمية، سعر، دفع، طبيب، منطقة، اسم المندوب، بونص، تاريخ، ملاحظات",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        # احفظ الحقول المعروفة فقط
        await run_blocking(save_user_template, user_id, known_fields)
        display = format_template_display(known_fields)
        example_lines = "\n".join(f"  السطر {i+1}: ..." for i in range(min(3, len(known_fields))))
        await update.message.reply_text(
            f"✅ *تم حفظ قالبك:*\n\n{display}\n\n"
            f"📝 *كيف ترسل الطلب؟* كل حقل بسطر منفصل بهذا الترتيب:\n{example_lines}\n\n"
            f"💡 مثال: `صيدلية الاماني` ← سطر أول، ثم `pirocomplex 10` ← سطر ثاني...",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    elif intent == "delete_template":
        await run_blocking(delete_user_template, user_id)
        await update.message.reply_text(
            "🗑️ *تم حذف القالب.*\n"
            "البوت سيفهم طلباتك تلقائياً بدون ترتيب محدد.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    elif intent == "show_template":
        tmpl = await run_blocking(get_user_template, user_id)
        if tmpl:
            display = format_template_display(tmpl)
            await update.message.reply_text(
                f"📋 *قالبك الحالي:*\n\n{display}\n\n"
                f"لتغييره اكتب مثلاً:\n`ترتيبي: صيدلية / علاج / كمية / كاش`",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                "📋 *ما عندك قالب حالياً.*\n\n"
                "تكدر تحدد ترتيب الطلب بأي طريقة:\n"
                "`ترتيبي: صيدلية / علاج / كمية / سعر / كاش`\n"
                "`طريقتي: صيدلية ثم دكتور ثم علاج ثم كمية`",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── الملخص اليومي التلقائي ─────────────────────────────────────────────
    elif intent == "toggle_daily":
        enabled = await run_blocking(toggle_setting, user_id, "daily_summary")
        state = "مفعّل ✅" if enabled else "موقف ❌"
        await update.message.reply_text(f"📋 الملخص اليومي (8 مساءً): *{state}*", parse_mode=ParseMode.MARKDOWN)
        return

    elif intent == "toggle_weekly":
        enabled = await run_blocking(toggle_setting, user_id, "weekly_report")
        state = "مفعّل ✅" if enabled else "موقف ❌"
        await update.message.reply_text(f"📊 التقرير الأسبوعي (كل سبت): *{state}*", parse_mode=ParseMode.MARKDOWN)
        return

    # ── ربط المدير ─────────────────────────────────────────────────────────
    elif intent == "set_manager":
        m = re.search(r'\d{5,}', text)
        if m:
            await run_blocking(set_manager, user_id, int(m.group()))
            await update.message.reply_text(
                f"✅ تم ربط حسابك بالمدير `{m.group()}`.\nسيصله إشعار بكل طلب جديد.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                "أرسل رقم Telegram ID للمدير. مثال:\n`مديري 123456789`",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── تكرار آخر أوردر ────────────────────────────────────────────────────
    elif intent == "repeat_order":
        if read_only:
            await update.message.reply_text("⛔ لا يمكن إضافة طلبات. جدّد اشتراكك.")
            return
        scope = parse_repeat_scope(text)
        if scope == "last_week":
            source_orders = await run_blocking(get_last_n_orders, user_id, 5)
            from datetime import timedelta
            # خذ أوردرات من أسبوع مضى
            week_ago = (date.today() - timedelta(days=7)).isoformat()
            source_orders = [o for o in source_orders if o.get("order_date", "") <= week_ago]
        else:
            last = await run_blocking(get_last_order, user_id)
            source_orders = [last] if last else []

        if not source_orders:
            await update.message.reply_text("⚠️ ما في أوردر سابق لتكراره.")
            return

        today_str = date.today().isoformat()
        rep_name_account = await _get_order_rep_name(user_id, tg_user)
        saved = []
        for src in source_orders:
            new_data = dict(src)
            new_data.pop("id", None)
            new_data.pop("created_at", None)
            new_data["order_date"] = today_str
            new_data["month"] = date.today().month
            new_data["year"] = date.today().year
            new_data["raw_text"] = f"تكرار من {src.get('order_date')}"
            new_data["rep_name"] = rep_name_account
            await run_blocking(save_order, user_id, new_data)
            saved.append(new_data)

        lines = [f"  • {o.get('pharmacy','—')} — {o.get('item_name','—')} × {o.get('quantity','—')}"
                 for o in saved]
        await update.message.reply_text(
            f"♻️ *تم تكرار {len(saved)} طلب بتاريخ اليوم:*\n\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── لوحة المدير ────────────────────────────────────────────────────────
    elif intent == "team_dashboard":
        tg_id = tg_user.id
        team = await run_blocking(get_team_stats, tg_id)
        if not team:
            await update.message.reply_text(
                "⚠️ لا يوجد مندوبون مرتبطون بك.\n"
                "المندوبون يربطون حساباتهم بإرسال:\n`مديري " + str(tg_id) + "`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        today = date.today()
        from parser import MONTH_NUM_TO_AR
        m_ar = MONTH_NUM_TO_AR.get(today.month, str(today.month))
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, rep in enumerate(team, 1):
            medal = medals.get(i, f"#{i}")
            lines.append(
                f"{medal} *{rep['name']}*\n"
                f"  📦 {rep['orders']} طلب | 📊 {rep['qty']} وحدة\n"
                f"  📅 آخر طلب: {rep['last_order']}"
            )
        total_orders = sum(r["orders"] for r in team)
        total_qty = sum(r["qty"] for r in team)
        msg = (
            f"👥 *لوحة الفريق — {m_ar}*\n"
            f"👤 {len(team)} مندوب | 📦 {total_orders} طلب | 📊 {total_qty} وحدة\n\n"
            + "\n\n".join(lines)
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # ── تسجيل طلب ──────────────────────────────────────────────────────────
    elif intent == "order":
        if read_only:
            await update.message.reply_text(
                "⛔ اشتراكك انتهى — لا يمكن إضافة طلبات.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
            )
            return
        tmpl_check = await run_blocking(get_user_template, user_id)
        if tmpl_check:
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            if len(lines) >= 2:
                # إدخال دفعي — كل سطر يقابل حقلاً في القالب
                order_data = parse_order_with_template(text, tmpl_check)
                order_data["rep_name"] = await _get_order_rep_name(user_id, tg_user)
                saved_oid3 = await run_blocking(save_order, user_id, order_data)
                reply = format_order_confirmation(order_data)
                post_kbd3 = build_post_save_keyboard(saved_oid3) if saved_oid3 else build_main_menu(is_admin_user=is_admin(tg_user.id))
                await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN, reply_markup=post_kbd3)
                await _notify_admin(context, f"📋 *طلب جديد من {order_data.get('rep_name') or tg_user.full_name}*\n\n{reply}")
                return
            # سطر واحد → إدخال موجّه حقل بحقل
            field_labels_ordered = " ← ".join(FIELD_LABELS.get(f, f) for f in tmpl_check)
            context.user_data["guided_order"] = {"fields": tmpl_check, "current_idx": 0, "data": {}}
            await update.message.reply_text(
                f"📝 *إدخال طلب — حقل بحقل*\n\n"
                f"💡 *نصيحة:* يمكنك إرسال كل الحقول دفعةً واحدة كل حقل في سطر بهذا الترتيب:\n"
                f"`{field_labels_ordered}`\n\n"
                f"أو استمر حقلاً بحقل — أرسل *{FIELD_LABELS.get(tmpl_check[0], tmpl_check[0])}:*\n"
                f"_(«تخطي» لتركه فارغاً — «إلغاء» للخروج)_",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await update.message.reply_text("⏳ جاري معالجة الطلب...")
        try:
            settings = await run_blocking(get_user_settings, user_id)

            # ── تحليل موحّد بطلب AI واحد فقط ────────────────────────────────
            # (سابقاً: كان فيه فحص regex منفصل is_multi_product() يقرر إذا نستخدم
            #  مسار "متعدد منتجات" أو لا — ولو فشل بالتعرف على التنسيق (مثلاً
            #  الكمية بسطر منفصل، أو ما فيه رقم بنفس سطر الدواء)، كان الطلب يروح
            #  لمسار "منتج واحد" الذي أصلاً ما يقدر يرجع غير دواء واحد مهما كان
            #  عدد الأدوية بالرسالة → هذا سبب ضياع بقية المنتجات.
            #  الحل: AI واحد يرجّع قائمة منتجات دائماً، وعدد العناصر بالقائمة
            #  نفسه يقرر إذا نعرض شاشة "متعدد" أو "منتج واحد" — بدون تخمين.)
            try:
                base_data, products = await run_blocking(parse_order_smart, text)
            except AIParseError as _ai_err:
                await update.message.reply_text(str(_ai_err), parse_mode=ParseMode.MARKDOWN)
                return

            if not base_data.get("rep_name") and settings.get("rep_name_memory"):
                base_data["rep_name"] = settings["rep_name_memory"]

            # Fuzzy matching للصيدلية
            known_ph = await run_blocking(get_known_pharmacies, user_id)
            if base_data.get("pharmacy") and known_ph:
                matched = fuzzy_match_pharmacy(base_data["pharmacy"], known_ph)
                if matched and matched != base_data["pharmacy"]:
                    base_data["pharmacy"] = matched

            # Fuzzy matching لكل منتج (كتالوج أولاً ثم الطلبات السابقة)
            catalog = await run_blocking(get_product_catalog, user_id)
            catalog_names = [c["drug_name"] for c in catalog] if catalog else []
            known_pr = await run_blocking(get_known_products, user_id)
            all_known_pr = list({*catalog_names, *known_pr})
            for prod in products:
                if prod.get("item_name") and all_known_pr:
                    matched_pr = fuzzy_match_product(prod["item_name"], all_known_pr)
                    if matched_pr and matched_pr != prod["item_name"]:
                        prod["item_name"] = matched_pr

            # تنبيه بدون صيدلية
            if not base_data.get("pharmacy"):
                await update.message.reply_text(
                    "🏥 *ما ذكرت اسم الصيدلية!*\nأرسل اسمها وأكمل الطلب.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                context.user_data["pending_order"] = {**base_data, **(products[0] if products else {})}
                return

            base_data["rep_name"] = await _get_order_rep_name(user_id, tg_user)

            if len(products) >= 2:
                # ── أكثر من منتج بنفس الطلب — نحفظهم كلهم عند التأكيد ────────
                context.user_data["review_multi"] = {"base_data": base_data, "products": products}
                context.user_data["review_settings"] = settings
                preview = format_order_preview(base_data, is_multi=True, products=products)
                await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN,
                                                reply_markup=build_order_review_keyboard(is_multi=True))
                _schedule_autosave(context, update.effective_chat.id, {
                    "user_id": user_id,
                    "multi": {"base_data": base_data, "products": products},
                    "order": None,
                    "tg_name": tg_user.full_name,
                })
                return

            # ── منتج واحد ────────────────────────────────────────────────────
            data = {**base_data, **(products[0] if products else {})}

            # Anti-error: تحقق من التناقضات
            warnings = detect_contradictions(data)
            if warnings:
                warn_text = "\n".join(warnings)
                await update.message.reply_text(warn_text, parse_mode=ParseMode.MARKDOWN)

            # تحقق من التكرار — لكن نعرض preview مع تنبيه بدل الإيقاف
            dup_warning = ""
            if await run_blocking(is_duplicate_order, user_id, data.get("pharmacy"), data.get("item_name"), data.get("order_date")):
                dup_warning = "\n⚠️ _هذا الطلب يبدو مسجلاً مسبقاً — تأكد قبل الحفظ_"

            # عرض preview قبل الحفظ
            context.user_data["review_order"] = data
            context.user_data["review_settings"] = settings
            preview = format_order_preview(data) + dup_warning
            await update.message.reply_text(preview, parse_mode=ParseMode.MARKDOWN,
                                            reply_markup=build_order_review_keyboard())
            _schedule_autosave(context, update.effective_chat.id, {
                "user_id": user_id,
                "order": data,
                "multi": None,
                "tg_name": tg_user.full_name,
            })

        except Exception as e:
            logger.error(f"Order error: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء معالجة الطلب. أعد المحاولة.")

    # ── تأكيد الطلب المكرر ─────────────────────────────────────────────────
    elif text.strip() in ("تأكيد", "نعم", "اكيد", "أكيد"):
        pending = context.user_data.get("pending_order")
        if pending:
            pending["rep_name"] = await _get_order_rep_name(user_id, tg_user)
            await run_blocking(save_order, user_id, pending)
            context.user_data.pop("pending_order", None)
            await update.message.reply_text("✅ تم حفظ الطلب.", parse_mode=ParseMode.MARKDOWN)
        return

    # ── تقرير ───────────────────────────────────────────────────────────────
    elif intent == "report":
        await update.message.reply_text("⏳ جاري إعداد التقرير...")
        try:
            report_filters = parse_report_filters(text)
            orders = await run_blocking(fetch_orders, user_id, report_filters)
            if not orders:
                await update.message.reply_text("📭 لا توجد بيانات للفترة المحددة.")
                return
            summary = build_text_summary(orders, report_filters.get("report_type", "general"))
            await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
            m = report_filters.get("month") or date.today().month
            y = report_filters.get("year") or date.today().year
            buf = await run_blocking(_generate_rep_template_excel, user_id, orders, m, y)
            label = _build_report_label(report_filters)
            fname = f"تقرير_المبيعات_{label}.xlsx"
            await update.message.reply_document(
                document=buf,
                filename=fname,
                caption="📊 تقرير Excel الكامل (بصيغة التاركت)",
            )
            await _notify_admin(
                context,
                f"📊 تقرير {tg_user.full_name} — {label}",
                excel=buf,
                filename=fname,
            )
        except Exception as e:
            logger.error(f"Report error: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء إعداد التقرير.")

    else:
        await update.message.reply_text(
            "👋 *ما فهمت قصدك!*\n\n"
            "تكدر تقولي بشكل مباشر، مثلاً:\n"
            "• *تسجيل طلب:* `صيدلية الاماني — pirocomplex 10 حبة — كاش`\n"
            "• *تقرير:* `وريني تقرير اليوم` أو `شگد باعنا هالشهر`\n"
            "• *قالب:* `ترتيبي: صيدلية / علاج / كمية / سعر / كاش`\n"
            "• *أداء:* `شلون شغلي هالشهر`\n\n"
            "أو اختر من القائمة:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
        )


async def _do_import_parse(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    month_num: int,
    year_num: int,
):
    """يحلّل ملف Excel المرفوع ويعرض المعاينة — مشترك بين مسار الشهر المحدد مسبقاً والعادي."""
    tg_user = update.effective_user
    user_id = await run_blocking(get_or_create_user, tg_user.id, tg_user.full_name, tg_user.username)
    file_id = context.user_data.pop("import_file_id", None)
    if not file_id:
        await update.message.reply_text("⚠️ لم يتم العثور على الملف، أرسله مجدداً.")
        return
    months_ar = {1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
                 7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"}
    month_name = months_ar.get(month_num, str(month_num))
    await update.message.reply_text(
        f"⏳ جاري تحليل الملف لشهر *{month_name} {year_num}*...",
        parse_mode=ParseMode.MARKDOWN,
    )
    try:
        settings = await run_blocking(get_user_settings, user_id)
        rep_name  = settings.get("rep_name_memory") or await run_blocking(get_user_full_name, tg_user.id) or tg_user.full_name or "مندوب"
        tg_file    = await context.bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()
        raw_bytes  = bytes(file_bytes)
        # جرّب قالب الكتالوج أولاً (أعمدة أدوية)، وإلا انتقل للمحلّل القديم
        orders = parse_catalog_import_excel(raw_bytes, month_num, year_num, rep_name)
        if not orders:
            orders = parse_import_excel(raw_bytes, month_num, year_num, rep_name)
        if not orders:
            await update.message.reply_text(
                "⚠️ *لم يتم العثور على بيانات في الملف.*\n\n"
                "تأكد من:\n"
                "• تعبئة شيت *البيانات* وليس شيت آخر\n"
                "• وجود عناوين الأعمدة في السطر الأول\n"
                "• عدم حذف أو تعديل صف الترويسة (الصف الداكن)",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_reports_menu(),
            )
            return
        pharmacies  = len(set(o["pharmacy"] for o in orders))
        products    = len(set(o["item_name"] for o in orders))
        months_used = sorted(set(f"{months_ar.get(o['month'], o['month'])} {o['year']}" for o in orders))
        months_str  = "، ".join(months_used)
        context.user_data["import_orders"] = orders
        await update.message.reply_text(
            f"📋 *معاينة الاستيراد*\n\n"
            f"📦 الطلبات المكتشفة: *{len(orders)}*\n"
            f"🏥 الصيدليات: *{pharmacies}*\n"
            f"💊 المنتجات المختلفة: *{products}*\n"
            f"📅 الشهور: *{months_str}*\n\n"
            "هل تريد حفظ هذه البيانات؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ نعم، احفظ الكل", callback_data="confirm_import"),
                    InlineKeyboardButton("❌ إلغاء",           callback_data="cancel_import"),
                ]
            ]),
        )
    except Exception as e:
        logger.error(f"Import parse error: {e}")
        await update.message.reply_text(
            f"❌ خطأ في قراءة الملف: {e}\n\nتأكد أن الملف بصيغة .xlsx وليس .xls",
            parse_mode=ParseMode.MARKDOWN,
        )



# ══════════════════════════════════════════════════════════════════════════════
# ── طلبات اليوم والأمس ────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_daily_review(query, user_id: int, context):
    """
    يرسل كل طلب من اليوم والأمس برسالة منفصلة.
    على كل رسالة أزرار تعديل/حذف (build_post_save_keyboard).
    الطلبات محفوظة أصلاً — لا حاجة لـ «حفظ تلقائي» إضافي.
    """
    from datetime import timedelta
    today     = date.today()
    yesterday = today - timedelta(days=1)

    orders = await run_blocking(fetch_orders_yesterday, user_id)   # اليوم + الأمس

    if not orders:
        await query.edit_message_text(
            "📭 *لا توجد طلبات مسجّلة اليوم أو الأمس.*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 القائمة", callback_data="menu_main")
            ]]),
        )
        return

    today_orders = [o for o in orders if str(o.get("order_date", "")).startswith(str(today))]
    yest_orders  = [o for o in orders if str(o.get("order_date", "")).startswith(str(yesterday))]
    total = len(orders)

    # ── رسالة إجمالية ────────────────────────────────────────────────────────
    summary_lines = [f"📦 *مراجعة الطلبات — {total} طلب*\n"]
    if today_orders:
        summary_lines.append(f"📅 *اليوم ({today}):* {len(today_orders)} طلب")
    if yest_orders:
        summary_lines.append(f"📅 *الأمس ({yesterday}):* {len(yest_orders)} طلب")
    summary_lines.append("\n_كل طلب أدناه في رسالة منفصلة — يمكنك التعديل أو الحذف مباشرةً._")

    await query.message.reply_text(
        "\n".join(summary_lines),
        parse_mode=ParseMode.MARKDOWN,
    )

    # ── فاصل اليوم ───────────────────────────────────────────────────────────
    if today_orders:
        await query.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━\n📅 *طلبات اليوم — {today}*  ({len(today_orders)} طلب)\n━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN,
        )
        for o in today_orders:
            msg = _format_saved_order(o)
            kbd = build_post_save_keyboard(o["id"])
            await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kbd)

    # ── فاصل الأمس ───────────────────────────────────────────────────────────
    if yest_orders:
        await query.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━\n📅 *طلبات الأمس — {yesterday}*  ({len(yest_orders)} طلب)\n━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN,
        )
        for o in yest_orders:
            msg = _format_saved_order(o)
            kbd = build_post_save_keyboard(o["id"])
            await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kbd)

    # ── زر إغلاق مستقل في الأسفل دائماً ─────────────────────────────────────
    await query.message.reply_text(
        "✅ *انتهت قائمة الطلبات* — يمكنك تعديل أي طلب أعلاه.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="menu_main"),
        ]]),
    )


def _format_saved_order(o: dict) -> str:
    """تنسيق رسالة طلب محفوظ للعرض في مراجعة اليوم/الأمس."""
    date_str = str(o.get("order_date", ""))[:10]
    lines = [f"📋 *طلب #{o['id']}*  —  {date_str}"]
    if o.get("area"):        lines.append(f"📍 *المنطقة:* {o['area']}")
    if o.get("pharmacy"):    lines.append(f"🏪 *الصيدلية:* {o['pharmacy']}")
    if o.get("item_name"):   lines.append(f"💊 *المنتج:* {o['item_name']}")
    if o.get("quantity"):
        qty = str(o["quantity"])
        if o.get("bonus"):
            qty += f" +{o['bonus']} بونص"
        lines.append(f"📦 *الكمية:* {qty}")
    if o.get("doctor_name"): lines.append(f"👨‍⚕️ *الطبيب:* {o['doctor_name']}")
    if o.get("price"):
        try:
            lines.append(f"💵 *السعر:* {float(o['price']):,.0f}")
        except (ValueError, TypeError):
            lines.append(f"💵 *السعر:* {o['price']}")
    if o.get("payment_type"):lines.append(f"💰 *الدفع:* {o['payment_type']}")
    if o.get("notes"):       lines.append(f"📝 *ملاحظات:* {o['notes']}")
    lines.append("\n_اختر إجراء أو اتركه كما هو — محفوظ تلقائياً:_")
    return "\n".join(lines)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستقبل ملف Excel ويبدأ تدفق الاستيراد."""
    if not update.message or not update.message.document:
        return
    doc = update.message.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".xlsx") or fname.endswith(".xls")):
        return  # تجاهل الملفات غير Excel

    tg_user = update.effective_user
    user_id = await run_blocking(get_or_create_user, tg_user.id, tg_user.full_name, tg_user.username)

    if not await run_blocking(is_user_registered, tg_user.id) and not is_admin(tg_user.id):
        await update.message.reply_text("⚠️ يجب إكمال التسجيل أولاً.")
        return

    # ── استيراد أطباء من Excel (doctor_add_excel) ────────────────────────────
    if context.user_data.pop("waiting_doctor_excel_import", False):
        await _do_doctor_excel_import(update, context, doc.file_id, user_id)
        return

    # ── استيراد الاستبيان (يقبل أيضاً ملف الأعمدة الحرة كـ fallback) ──────
    if context.user_data.pop("waiting_survey_import", False):
        await _do_survey_import(update, context, doc.file_id, user_id)
        return

    context.user_data["import_file_id"] = doc.file_id

    # هل اختار المندوب الشهر مسبقاً من زر "بيانات سابقة"؟
    preset = context.user_data.pop("import_month_preset", None)
    if preset:
        p_m, p_y = preset
        months_ar_map = {1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
                         7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"}
        context.user_data["import_month"]         = p_m
        context.user_data["import_year"]          = p_y
        context.user_data["waiting_import_month"] = False
        await _do_import_parse(update, context, p_m, p_y)
        return

    context.user_data["waiting_import_month"] = True
    months_ar = {1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
                 7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"}
    today = date.today()
    hint  = "\n".join(f"• `{m}` — {n}" for m, n in months_ar.items() if m <= today.month)

    await update.message.reply_text(
        f"📂 *تم استلام الملف: {doc.file_name}*\n\n"
        "أرسل *رقم الشهر* الذي تنتمي إليه البيانات:\n\n"
        f"{hint}\n\n"
        "_إذا الملف يحوي أكثر من شهر، اكتب الشهر الرئيسي\n"
        "يمكنك أيضاً كتابة الشهر والسنة: مثال_ `4 2024`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel_import")
        ]]),
    )



async def _do_doctor_excel_import(update, context, file_id: str, user_id: int):
    """
    استيراد ملف Excel أطباء (تنسيق حر) وحفظهم في قاعدة البيانات.
    أعمدة مدعومة: الاسم، التخصص، المنطقة، العيادة، الهاتف، الفئة، ملاحظات
    يقبل أيضاً ملف الاستبيان الصادر من البوت (يُعالَج بـ parse_survey_import_excel).
    """
    import openpyxl, io as _io
    try:
        await update.message.reply_text("⏳ *جارٍ استيراد الأطباء من Excel…*", parse_mode=ParseMode.MARKDOWN)
        tg_file   = await context.bot.get_file(file_id)
        raw_bytes = bytes(await tg_file.download_as_bytearray())

        # أولاً: حاول استيراد بصيغة الاستبيان (إن كانت الأعمدة تطابق)
        doctors = []
        try:
            doctors = parse_survey_import_excel(raw_bytes)
        except Exception:
            pass

        # ثانياً: إن لم ينجح، استخدم المُحلِّل العام للأعمدة الحرة
        if not doctors:
            wb  = openpyxl.load_workbook(_io.BytesIO(raw_bytes), read_only=True, data_only=True)
            ws  = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
            wb.close()

            if not all_rows:
                await update.message.reply_text("⚠️ الملف فارغ أو لا يحتوي بيانات.")
                return

            # كشف صف العناوين (أول صف يحتوي "اسم" أو "name")
            header_row_idx = 0
            for i, row in enumerate(all_rows[:5]):
                cells = [str(c).strip().lower() for c in row if c]
                if any("اسم" in c or "name" in c for c in cells):
                    header_row_idx = i
                    break

            header = [str(c).strip() if c else "" for c in all_rows[header_row_idx]]
            # خريطة تطبيع الأعمدة
            _COL_MAP = {
                "الاسم": "name",   "اسم الطبيب": "name",     "name": "name",
                "التخصص": "specialty", "تخصص": "specialty",  "specialty": "specialty",
                "المنطقة": "area",  "منطقة": "area",          "area": "area",
                "العيادة": "clinic_name", "عيادة": "clinic_name", "المستشفى": "clinic_name",
                "الهاتف": "phone",  "هاتف": "phone",           "phone": "phone",
                "الفئة": "class",   "فئة": "class",            "class": "class",
                "ملاحظات": "notes", "ملاحظة": "notes",         "notes": "notes",
            }
            col_idx = {}
            for idx, h in enumerate(header):
                h_lower = h.lower().strip()
                for ar_key, field in _COL_MAP.items():
                    if ar_key in h_lower or h_lower in ar_key:
                        if field not in col_idx:
                            col_idx[field] = idx

            if "name" not in col_idx:
                await update.message.reply_text(
                    "❌ لم أجد عمود *الاسم* في الملف.\n"
                    "تأكد أن الصف الأول يحتوي عنوان «الاسم» أو «اسم الطبيب».",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 قائمة الأطباء", callback_data="menu_doctors")
                    ]]),
                )
                return

            for row in all_rows[header_row_idx + 1:]:
                if not row or not any(row):
                    continue
                def _get(field):
                    idx = col_idx.get(field)
                    if idx is None or idx >= len(row):
                        return None
                    val = row[idx]
                    return str(val).strip() if val and str(val).strip() not in ("None", "") else None

                name = _get("name")
                if not name:
                    continue
                doctors.append({
                    "name":        name,
                    "specialty":   _get("specialty"),
                    "area":        _get("area"),
                    "clinic_name": _get("clinic_name"),
                    "phone":       _get("phone"),
                    "class":       (_get("class") or "").upper() or None,
                    "notes":       _get("notes"),
                })

        if not doctors:
            await update.message.reply_text(
                "⚠️ لم يتم العثور على أطباء في الملف.\n"
                "تأكد أن الملف يحتوي عمود *الاسم*.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 قائمة الأطباء", callback_data="menu_doctors")
                ]]),
            )
            return

        saved, skipped = 0, 0
        for doc_data in doctors:
            try:
                await run_blocking(upsert_doctor_profile, user_id, doc_data)
                saved += 1
            except Exception:
                skipped += 1

        cls_count = {
            "A": sum(1 for d in doctors if (d.get("class") or "").upper() == "A"),
            "B": sum(1 for d in doctors if (d.get("class") or "").upper() == "B"),
            "C": sum(1 for d in doctors if (d.get("class") or "").upper() == "C"),
        }
        await update.message.reply_text(
            f"✅ *تم استيراد {saved} طبيب بنجاح!*"
            + (f"\n⚠️ تم تجاهل {skipped} صف" if skipped else "") + "\n\n"
            f"🟢 فئة A: {cls_count['A']}  🟡 B: {cls_count['B']}  🔵 C: {cls_count['C']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 عرض قائمة الأطباء", callback_data="doctor_list_0")],
                [InlineKeyboardButton("🏠 القائمة الرئيسية",  callback_data="menu_main")],
            ]),
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ *خطأ في استيراد الملف:*\n`{e}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="menu_doctors")
            ]]),
        )


async def _do_survey_import(update, context, file_id: str, user_id: int):
    """
    استيراد ملف Excel الاستبيان.
    يحاول أولاً parse_survey_import_excel (التنسيق الكامل).
    إن فشل (أعمدة مختلفة / ألوان / تنسيق حر) يتراجع إلى المُحلِّل العام.
    """
    import openpyxl as _opxl, io as _io
    try:
        await update.message.reply_text(
            "⏳ *جارٍ استيراد الاستبيان…*",
            parse_mode=ParseMode.MARKDOWN,
        )
        tg_file   = await context.bot.get_file(file_id)
        raw_bytes = bytes(await tg_file.download_as_bytearray())

        # ── المحاولة الأولى: تنسيق الاستبيان الكامل ─────────────────────────
        doctors = []
        try:
            doctors = parse_survey_import_excel(raw_bytes)
        except Exception:
            pass

        # ── المحاولة الثانية: مُحلِّل الأعمدة الحرة (fallback) ───────────────
        if not doctors:
            wb  = _opxl.load_workbook(_io.BytesIO(raw_bytes), read_only=True, data_only=True)
            ws  = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
            wb.close()

            header_row_idx = 0
            for i, row in enumerate(all_rows[:5]):
                cells = [str(c).strip().lower() for c in row if c]
                if any("اسم" in c or "name" in c for c in cells):
                    header_row_idx = i
                    break

            header = [str(c).strip() if c else "" for c in all_rows[header_row_idx]]
            _COL_MAP = {
                "الاسم": "name",        "اسم الطبيب": "name",     "name": "name",
                "التخصص": "specialty",  "تخصص": "specialty",      "specialty": "specialty",
                "المنطقة": "area",       "منطقة": "area",          "area": "area",
                "العيادة": "clinic_name","عيادة": "clinic_name",   "المستشفى": "clinic_name",
                "الهاتف": "phone",       "هاتف": "phone",          "phone": "phone",
                "الفئة": "class",        "فئة": "class",           "class": "class",  "الكلاس": "class",
                "ملاحظات": "notes",      "ملاحظة": "notes",        "notes": "notes",
                "أيام الدوام": "notes",  "الصيدلية المرتبطة": "linked_pharmacy",
                "الصيدليه المرتبطه": "linked_pharmacy",
            }
            col_idx = {}
            for idx2, h in enumerate(header):
                h_lower = h.lower().strip()
                for ar_key, field in _COL_MAP.items():
                    if ar_key in h_lower or h_lower in ar_key:
                        if field not in col_idx:
                            col_idx[field] = idx2

            if "name" not in col_idx:
                await update.message.reply_text(
                    "❌ لم أجد عمود *اسم الطبيب* في الملف.\n"
                    "تأكد أن الملف يحتوي عمود باسم *اسم الطبيب* أو *الاسم*.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 قائمة الأطباء", callback_data="menu_doctors")
                    ]]),
                )
                return

            for row in all_rows[header_row_idx + 1:]:
                def _get(field):
                    ci = col_idx.get(field)
                    if ci is None or ci >= len(row):
                        return ""
                    v = row[ci]
                    return str(v).strip() if v is not None else ""

                name = _get("name")
                if not name:
                    continue
                doc_data = {
                    "name":             name,
                    "specialty":        _get("specialty") or None,
                    "area":             _get("area") or None,
                    "clinic_name":      _get("clinic_name") or None,
                    "phone":            _get("phone") or None,
                    "class":            (_get("class") or "C").upper()[:2],
                    "notes":            _get("notes") or None,
                    "linked_pharmacy":  _get("linked_pharmacy") or None,
                }
                doctors.append(doc_data)

        if not doctors:
            await update.message.reply_text(
                "⚠️ لم يتم العثور على أطباء في الملف.\n"
                "تأكد من وجود بيانات وأن الملف يحتوي عمود اسم الطبيب.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 قائمة الأطباء", callback_data="menu_doctors")
                ]]),
            )
            return

        saved, skipped = 0, 0
        for doc_data in doctors:
            try:
                await run_blocking(upsert_doctor_profile, user_id, doc_data)
                saved += 1
            except Exception:
                skipped += 1

        cls_count = {
            "A": sum(1 for d in doctors if (d.get("class") or "").upper() == "A"),
            "B": sum(1 for d in doctors if (d.get("class") or "").upper() == "B"),
            "C": sum(1 for d in doctors if (d.get("class") or "").upper() == "C"),
        }
        await update.message.reply_text(
            f"✅ *تم استيراد الاستبيان بنجاح!*\n\n"
            f"👨‍⚕️ أطباء مُحدَّثة: *{saved}*"
            + (f"  |  ⚠️ تجاهل: {skipped}" if skipped else "") + "\n"
            f"🟢 A:{cls_count['A']}  🟡 B:{cls_count['B']}  🔵 C:{cls_count['C']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 عرض قائمة الأطباء", callback_data="doctor_list_0")],
                [InlineKeyboardButton("🏠 القائمة الرئيسية",  callback_data="menu_main")],
            ]),
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ حدث خطأ غير متوقع: `{e}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 قائمة الأطباء", callback_data="menu_doctors")
            ]]),
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستقبل صورة الفاتورة ويرسلها للأدمن كطلب اشتراك."""
    if not update.message or not update.message.photo:
        return
    tg_user = update.effective_user
    user_id = await run_blocking(get_or_create_user, tg_user.id, tg_user.full_name, tg_user.username)

    plan_type = context.user_data.get("pending_sub_plan", "monthly")
    label = "شهري" if plan_type == "monthly" else "سنوي"
    amount = PRICES.get(plan_type, "—")

    # احفظ الطلب في DB
    await run_blocking(create_subscription_request, user_id, plan_type)

    # أرسل للأدمن صورة الفاتورة مع تفاصيل المندوب
    caption = (
        f"📬 *طلب اشتراك جديد!*\n\n"
        f"👤 *الاسم:* {tg_user.full_name}\n"
        f"📱 *يوزر:* @{tg_user.username or 'بدون يوزر'}\n"
        f"🆔 *DB ID:* `{user_id}`\n"
        f"🆔 *TG ID:* `{tg_user.id}`\n"
        f"💎 *الخطة:* {label} — {amount}\n\n"
        f"🔧 *للترقية:*\n"
        f"`/upgrade {user_id} {plan_type}`"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.forward_message(
                chat_id=admin_id,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id,
            )
            await context.bot.send_message(
                chat_id=admin_id,
                text=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    context.user_data.pop("pending_sub_plan", None)
    await update.message.reply_text(
        "✅ *تم استلام فاتورتك!*\n\n"
        "سيتم مراجعتها وتفعيل اشتراكك خلال وقت قصير.\n"
        "شكراً لثقتك! 🙏",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_main_menu(is_admin_user=is_admin(tg_user.id)),
    )


def _generate_rep_template_excel(user_id: int, orders: list, month: int, year: int):
    """Generate the rep's template Excel using their catalog, prices, and targets.
    ملاحظة: هذي دالة متزامنة عادية — يستدعيها المتصلون بها عبر run_blocking()،
    فلا تحتاج (ولا يصح) تستخدم await بداخلها (ما فيها event loop شغال بالثريد)."""
    from database import get_user_settings, get_product_catalog, get_drug_targets
    settings = get_user_settings(user_id)
    rep_name = settings.get("rep_name_memory") or "مندوب"
    catalog = get_product_catalog(user_id)
    period = f"{year}-{month:02d}"
    targets = get_drug_targets(user_id, period)
    return generate_template_excel(
        orders or [], catalog or [], rep_name, month, year, drug_targets=targets
    )


def _build_report_label(f: dict) -> str:
    today = date.today()
    if f.get("date_from") and f.get("date_to"):
        return f"{f['date_from']}_to_{f['date_to']}"
    if f.get("month"):
        return f"{f.get('year', today.year)}-{f['month']:02d}"
    return str(today)


async def _global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """يمسك أي استثناء غير متوقع بأي handler — بدونه، الخطأ كان يروح بصمت
    بالـ log فقط والمستخدم ما يوصله أي رد إطلاقاً."""
    logger.error("Unhandled exception while processing update", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ صار خطأ غير متوقع. حاول مرة أخرى أو تواصل مع الدعم لو تكرر.",
            )
    except Exception:
        pass


def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        return
    init_db()
    logger.info("Database initialized.")
    app = Application.builder().token(TOKEN).defaults(Defaults(tzinfo=BOT_TIMEZONE)).build()
    app.add_error_handler(_global_error_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(
        ["admin", "stats", "users", "activate", "ban", "extend", "upgrade", "requests"],
        handle_admin,
    ))

    # ── قائمة الأوامر التي تظهر بجانب مكان الكتابة في تيلگرام ───────────────
    from telegram import BotCommand

    async def _set_commands(application):
        await application.bot.set_my_commands([
            BotCommand("start", "🏠 بدء استخدام البوت"),
            BotCommand("help",  "📖 دليل الاستخدام"),
        ])

    app.post_init = _set_commands
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    setup_scheduler(app)
    logger.info("Bot is running...")
    if WEBHOOK_URL:
        webhook_path = f"/webhook/{TOKEN}"
        logger.info(f"Starting webhook on port {PORT} → {WEBHOOK_URL}{webhook_path}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=f"{WEBHOOK_URL}{webhook_path}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting polling mode...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
