import os
import secrets
import string
from datetime import date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from database import (
    get_all_users_admin, admin_set_subscription, admin_ban_user, admin_unban_user,
    get_stats_admin, get_user_by_username,
    get_orders_for_admin_excel, close_subscription_request,
    get_pending_requests, create_activation_code,
    get_all_users_admin_stats, fetch_orders, fetch_orders_today,
    get_product_catalog, get_drug_targets, get_performance_stats,
    get_all_doctor_profiles, get_competitor_entries,
    set_bot_config, get_full_backup_data, get_today_admin_stats,
    get_all_payments, get_rep_pharmacy_balances, get_payments_by_rep, get_pharmacy_balance,
)
from reports import (generate_excel_report, generate_template_excel, generate_admin_summary_excel,
                      build_text_summary, generate_survey_excel, generate_full_backup_excel,
                      generate_payment_excel)
from parser import MONTH_NUM_TO_AR, AVAILABLE_AI_MODELS, _get_ai_settings, _ai_call
from botutils import run_blocking

ADMIN_IDS_RAW = os.environ.get("ADMIN_TELEGRAM_IDS", "")
ADMIN_IDS = set(
    int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()
)

PLAN_DAYS = {"monthly": 30, "yearly": 365, "lifetime": 36500}
PLAN_LABEL = {"monthly": "شهري Pro", "yearly": "سنوي Pro", "lifetime": "مدى الحياة"}


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


# ── لوحة التحكم الرئيسية ──────────────────────────────────────────────────────

async def handle_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not is_admin(tg_id):
        return

    text = update.message.text.strip()
    parts = text.split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/admin":
        await _admin_menu(update)
    elif cmd == "/stats":
        await _admin_stats(update)
    elif cmd == "/users":
        await _admin_users(update, page=0)
    elif cmd == "/upgrade":
        await _admin_upgrade_cmd(update, parts)
    elif cmd == "/ban":
        await _admin_ban_cmd(update, parts)
    elif cmd == "/extend":
        await _admin_extend_cmd(update, parts)
    elif cmd == "/requests":
        await _admin_pending_requests(update)
    elif cmd == "/activate":
        await _admin_generate_code_cmd(update, parts)
    elif cmd == "/find":
        await _admin_find_cmd(update, parts)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج الضغطات من الأزرار في لوحة الأدمن."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("غير مصرح.")
        return
    await query.answer()
    data = query.data

    # ── الشاشة الرئيسية الموحّدة ─────────────────────────────────────────────
    if data == "adm_home":
        await _admin_home_callback(query)

    elif data == "adm_stats":
        await _admin_stats_callback(query)

    elif data == "adm_search_prompt":
        context.user_data["waiting_admin_search"] = True
        await query.edit_message_text("🔍 اكتب اسم المندوب أو جزء منه:")

    # ── قائمة المستخدمين — صفحة ─────────────────────────────────────────────
    elif data.startswith("adm_page_"):
        page = int(data.split("_")[-1])
        await _admin_users_callback(query, page)

    # ── عرض مستخدم معين ─────────────────────────────────────────────────────
    elif data.startswith("adm_user_"):
        uid = int(data.split("_")[-1])
        await _admin_show_user(query, uid)

    # ── ترقية مستخدم ────────────────────────────────────────────────────────
    elif data.startswith("adm_upgr_"):
        parts = data.split("_")
        uid = int(parts[2])
        plan = parts[3]
        await _admin_do_upgrade(query, uid, plan, context)

    # ── حظر مستخدم (بتأكيد) ─────────────────────────────────────────────────
    elif data.startswith("adm_ban_confirm_"):
        uid = int(data.split("_")[-1])
        await _admin_confirm_ban(query, uid)
    elif data.startswith("adm_ban_yes_"):
        uid = int(data.split("_")[-1])
        await _admin_do_ban(query, uid, context)
    elif data.startswith("adm_ban_no_"):
        uid = int(data.split("_")[-1])
        await _admin_show_user(query, uid)
    elif data.startswith("adm_unban_"):
        uid = int(data.split("_")[-1])
        await _admin_do_unban(query, uid)

    # ── إعدادات الذكاء الاصطناعي ─────────────────────────────────────────────
    elif data == "adm_ai_menu":
        await _admin_ai_menu(query)
    elif data == "adm_ai_model_pick":
        await _admin_ai_model_pick(query)
    elif data.startswith("adm_ai_model_set_"):
        idx = int(data.split("_")[-1])
        await _admin_ai_model_set(query, idx)
    elif data == "adm_ai_key_prompt":
        context.user_data["waiting_admin_ai_key"] = True
        await query.edit_message_text(
            "🔑 *أرسل مفتاح OpenRouter API الجديد كرسالة نصية.*\n"
            "(يبدأ عادة بـ `sk-or-`)\n\nأو اكتب *إلغاء* للتراجع.",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── نسخة احتياطية شاملة ──────────────────────────────────────────────────
    elif data == "adm_backup":
        await _admin_send_backup(query, context)

    # ── توليد كود تفعيل (تفاعلي) ─────────────────────────────────────────────
    elif data == "adm_gen_code":
        await _admin_gen_code_menu(query)
    elif data.startswith("adm_gencode_"):
        plan = data.split("_", 2)[2]
        await _admin_gen_code_do(query, plan)

    # ── Excel مستخدم — اختيار شهر ───────────────────────────────────────────
    elif data.startswith("adm_excel_pick_"):
        uid = int(data.split("_")[-1])
        await _admin_excel_month_picker(query, uid)

    # ── Excel مستخدم — تحميل شهر محدد ──────────────────────────────────────
    elif data.startswith("adm_excel_dl_"):
        parts = data.split("_")
        uid = int(parts[3])
        year = int(parts[4])
        month = int(parts[5])
        await _admin_excel_download(query, uid, year, month, context)

    # ── طلبات الاشتراك المعلقة ───────────────────────────────────────────────
    elif data == "adm_requests":
        await _admin_pending_requests_callback(query)

    # ── التسديدات (تحصيل من الصيدليات) ───────────────────────────────────────
    elif data == "adm_payments":
        await _admin_payments_menu(query)

    elif data == "adm_pay_debts":
        await _admin_payment_debts(query)

    elif data == "adm_pay_excel":
        await _admin_payments_excel(query)

    # ── لوحة تحكم المبيعات الرئيسية ─────────────────────────────────────────
    elif data == "adm_main_dashboard":
        await _admin_main_sales_dashboard(query)

    # ── Excel ملخص كل المناديب ───────────────────────────────────────────────
    elif data.startswith("adm_all_excel_"):
        parts = data.split("_")
        year = int(parts[3])
        month = int(parts[4])
        await _admin_all_reps_excel(query, year, month)

    # ── Excel مندوب بعينه (بالتنسيق الكامل مع الكتالوج والتاركت) ───────────
    elif data.startswith("adm_rep_xl_"):
        parts = data.split("_")
        uid   = int(parts[3])
        year  = int(parts[4])
        month = int(parts[5])
        await _admin_single_rep_excel(query, uid, year, month)

    # ── قائمة تقارير مندوب واحد ──────────────────────────────────────────────
    elif data.startswith("adm_rep_menu_"):
        uid = int(data.split("_")[-1])
        await _admin_rep_report_menu(query, uid)

    # ── تقرير اليوم لمندوب ───────────────────────────────────────────────────
    elif data.startswith("adm_rep_today_"):
        uid = int(data.split("_")[-1])
        await _admin_rep_report_today(query, uid)

    # ── تقرير الشهر لمندوب ───────────────────────────────────────────────────
    elif data.startswith("adm_rep_month_"):
        parts = data.split("_")
        uid   = int(parts[3])
        year  = int(parts[4])
        month = int(parts[5])
        await _admin_single_rep_excel(query, uid, year, month)

    # ── أداء مندوب ───────────────────────────────────────────────────────────
    elif data.startswith("adm_rep_perf_"):
        uid = int(data.split("_")[-1])
        await _admin_rep_performance(query, uid)

    # ── أطباء مندوب: عرض قائمة ──────────────────────────────────────────────
    elif data.startswith("adm_doc_list_"):
        parts = data.split("_")
        uid  = int(parts[3])
        page = int(parts[4]) if len(parts) > 4 else 0
        await _admin_rep_doctors(query, uid, page)

    # ── أطباء مندوب: تفاصيل طبيب واحد ──────────────────────────────────────
    elif data.startswith("adm_doc_view_"):
        parts = data.split("_")
        uid    = int(parts[3])
        doc_id = int(parts[4])
        await _admin_rep_doctor_detail(query, uid, doc_id)

    # ── أطباء مندوب: Excel استبيان ──────────────────────────────────────────
    elif data.startswith("adm_doc_excel_"):
        uid = int(data.split("_")[-1])
        await _admin_rep_doctors_excel(query, uid, context)

    # ── تقارير التسديد لمندوب واحد ───────────────────────────────────────────
    elif data.startswith("adm_pay_rep_menu_"):
        uid = int(data.split("_")[-1])
        await _admin_pay_rep_menu(query, uid)

    elif data.startswith("adm_pay_rep_today_"):
        uid = int(data.split("_")[-1])
        await _admin_pay_rep_today(query, uid)

    elif data.startswith("adm_pay_rep_month_"):
        parts = data.split("_")
        uid   = int(parts[4])
        year  = int(parts[5])
        month = int(parts[6])
        await _admin_pay_rep_month(query, uid, year, month)

    elif data.startswith("adm_pay_rep_pick_"):
        uid = int(data.split("_")[-1])
        context.user_data["waiting_admin_pay_rep_month"] = uid
        months_ar = {
            1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
            7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"
        }
        today = date.today()
        hint = "\n".join(f"• `{m}` — {n}" for m, n in months_ar.items() if m <= today.month)
        users = get_all_users_admin()
        u = next((x for x in users if x["id"] == uid), None)
        rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"
        await query.edit_message_text(
            f"🗓 *تقرير تسديدات شهور أخرى — {rep_name}*\n\n"
            "أرسل *رقم الشهر* (أو رقم الشهر والسنة):\n\n"
            f"{hint}\n\n"
            "_مثال:_ `3` أو `3 2024`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data=f"adm_pay_rep_menu_{uid}")
            ]]),
        )

    # ── شهور أخرى: اطلب من الأدمن كتابة الشهر ───────────────────────────────
    elif data.startswith("adm_rep_pick_"):
        uid = int(data.split("_")[-1])
        context.user_data["waiting_admin_rep_month"] = uid
        months_ar = {
            1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
            7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"
        }
        today = date.today()
        hint = "\n".join(f"• `{m}` — {n}" for m, n in months_ar.items() if m <= today.month)
        users = get_all_users_admin()
        u = next((x for x in users if x["id"] == uid), None)
        rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"
        await query.edit_message_text(
            f"🗓 *تقرير شهور أخرى — {rep_name}*\n\n"
            "أرسل *رقم الشهر* (أو رقم الشهر والسنة):\n\n"
            f"{hint}\n\n"
            "_مثال:_ `3` أو `3 2024`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data=f"adm_rep_menu_{uid}")
            ]]),
        )


# ── وظائف المساعدة ────────────────────────────────────────────────────────────

def _build_admin_home() -> tuple:
    """الشاشة الرئيسية الموحّدة للوحة الأدمن — نفسها تماماً سواء دخلت من زر
    '🏢 لوحة الأدمن' أو من كتابة /admin، بدل ما كانوا صفحتين مختلفتين."""
    today_stats = get_today_admin_stats()
    stats = get_stats_admin()
    today = date.today()
    reps = get_all_users_admin_stats(today.month, today.year)
    m_name = MONTH_NUM_TO_AR.get(today.month, str(today.month))
    month_orders = sum(r.get("order_count", 0) for r in reps)
    month_sales = sum(r.get("total_sales", 0) for r in reps)
    pending = stats.get("pending_requests", 0)

    msg = (
        "🔧 *لوحة الأدمن*\n\n"
        f"📅 اليوم: *{today_stats['orders_today']}* طلب من *{today_stats['active_reps_today']}* مندوب نشط\n"
        f"📦 هذا الشهر ({m_name}): *{month_orders}* طلب | *{month_sales:,.0f} IQD*\n"
    )
    if pending:
        msg += f"🔔 عندك *{pending}* طلب اشتراك بانتظار الموافقة\n"
    else:
        msg += "✅ ما فيه طلبات اشتراك معلّقة\n"

    pending_label = f"📬 الطلبات المعلقة 🔴{pending}" if pending else "📬 الطلبات المعلقة"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 بحث عن مندوب", callback_data="adm_search_prompt")],
        [
            InlineKeyboardButton("📊 لوحة المبيعات", callback_data="adm_main_dashboard"),
            InlineKeyboardButton("👥 كل المناديب", callback_data="adm_page_0"),
        ],
        [
            InlineKeyboardButton(pending_label, callback_data="adm_requests"),
            InlineKeyboardButton("🔑 توليد كود تفعيل", callback_data="adm_gen_code"),
        ],
        [
            InlineKeyboardButton("📈 إحصائيات تفصيلية", callback_data="adm_stats"),
            InlineKeyboardButton("🤖 الذكاء الاصطناعي", callback_data="adm_ai_menu"),
        ],
        [InlineKeyboardButton("💰 التسديدات", callback_data="adm_payments")],
        [InlineKeyboardButton("💾 نسخة احتياطية", callback_data="adm_backup")],
    ])
    return msg, kb


async def _admin_menu(update: Update):
    msg, kb = await run_blocking(_build_admin_home)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_home_callback(query):
    msg, kb = await run_blocking(_build_admin_home)
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


def _build_admin_stats() -> tuple:
    s = get_stats_admin()
    msg = (
        "📊 *إحصائيات البوت:*\n\n"
        f"👥 إجمالي المندوبين: *{s['total_users']}*\n"
        f"💎 مشتركون Pro: *{s['paid']}*\n"
        f"🆓 شهر مجاني: *{s['basic']}*\n"
        f"📦 إجمالي الأوردرات: *{s['total_orders']}*\n"
        f"🔔 طلبات اشتراك معلقة: *{s['pending_requests']}*\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 المندوبين", callback_data="adm_page_0"),
         InlineKeyboardButton("📬 الطلبات المعلقة", callback_data="adm_requests")],
        [InlineKeyboardButton("📊 لوحة مبيعات المناديب", callback_data="adm_main_dashboard")],
        [InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")],
    ])
    return msg, kb


async def _admin_stats(update: Update):
    msg, kb = await run_blocking(_build_admin_stats)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_stats_callback(query):
    msg, kb = await run_blocking(_build_admin_stats)
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_search_and_reply(reply_fn, query_text: str):
    """منطق البحث المشترك — يستخدمه أمر /find وزر '🔍 بحث عن مندوب' بنفس الشكل."""
    query_text = query_text.strip().lower()
    users = get_all_users_admin()
    matches = [
        u for u in users
        if query_text in (u.get("name") or "").lower()
        or query_text in (u.get("username") or "").lower()
    ]
    if not matches:
        await reply_fn(
            f"❌ ما لقيت أي مندوب يطابق \"{query_text}\".",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")]]),
        )
        return matches
    if len(matches) == 1:
        return matches
    rows = []
    for u in matches[:15]:
        name = (u.get("name") or "مجهول")[:20]
        uname = f"@{u['username']}" if u.get("username") else f"id:{u['id']}"
        plan = u.get("plan") or "—"
        plan_icon = "💎" if plan in ("pro", "monthly", "yearly", "lifetime") else "🆓"
        rows.append([InlineKeyboardButton(f"{plan_icon} {name} ({uname})", callback_data=f"adm_user_{u['id']}")])
    rows.append([InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")])
    await reply_fn(
        f"🔍 لقيت {len(matches)} نتيجة لـ \"{query_text}\":",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return matches


async def handle_admin_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يعالج نص البحث بعد ضغط زر '🔍 بحث عن مندوب'."""
    if not context.user_data.get("waiting_admin_search"):
        return False
    context.user_data.pop("waiting_admin_search")
    query_text = update.message.text.strip()
    matches = await _admin_search_and_reply(update.message.reply_text, query_text)
    if len(matches) == 1:
        await _admin_show_user_msg(update, matches[0]["id"])
    return True


async def _admin_find_cmd(update: Update, parts: list):
    """بحث سريع عن مندوب بالاسم أو اليوزر: /find طه"""
    if len(parts) < 2:
        await update.message.reply_text(
            "الاستخدام: `/find اسم_المندوب_أو_جزء_منه`\nمثال: `/find طه`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    query_text = " ".join(parts[1:])
    matches = await _admin_search_and_reply(update.message.reply_text, query_text)
    if len(matches) == 1:
        await _admin_show_user_msg(update, matches[0]["id"])


async def _admin_users(update: Update, page: int = 0):
    users = get_all_users_admin()
    kb = _build_users_keyboard(users, page)
    count = len(users)
    await update.message.reply_text(
        f"👥 *المندوبون ({count} مستخدم):*\nاضغط على اسم المندوب للتفاصيل والترقية:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def _admin_users_callback(query, page: int):
    users = get_all_users_admin()
    kb = _build_users_keyboard(users, page)
    await query.edit_message_text(
        f"👥 *المندوبون ({len(users)} مستخدم):*\nاضغط على اسم المندوب:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


def _build_users_keyboard(users: list, page: int) -> InlineKeyboardMarkup:
    PAGE_SIZE = 8
    start = page * PAGE_SIZE
    chunk = users[start: start + PAGE_SIZE]
    rows = []
    for u in chunk:
        name = (u.get("name") or "مجهول")[:20]
        uname = f"@{u['username']}" if u.get("username") else f"id:{u['id']}"
        plan = u.get("plan") or "—"
        plan_icon = "💎" if plan in ("pro", "monthly", "yearly", "lifetime") else "🆓"
        rows.append([InlineKeyboardButton(
            f"{plan_icon} {name} ({uname})",
            callback_data=f"adm_user_{u['id']}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"adm_page_{page-1}"))
    if start + PAGE_SIZE < len(users):
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"adm_page_{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


def _build_user_detail(u: dict, uid: int) -> tuple:
    name = u.get("name") or "مجهول"
    uname = f"@{u['username']}" if u.get("username") else "بدون يوزر"
    plan = u.get("plan") or "—"
    status = u.get("status") or "—"
    end = u.get("end_date") or "—"

    msg = (
        f"👤 *{name}*\n"
        f"📱 {uname}\n"
        f"🆔 DB ID: `{uid}` | TG: `{u.get('telegram_id','—')}`\n"
        f"💎 الخطة: *{plan}* | الحالة: {status}\n"
        f"📅 الانتهاء: {end}\n"
    )

    ban_row = (
        [InlineKeyboardButton("✅ فك الحظر", callback_data=f"adm_unban_{uid}")]
        if status == "banned" else
        [InlineKeyboardButton("🚫 حظر", callback_data=f"adm_ban_confirm_{uid}")]
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 ترقية شهري Pro", callback_data=f"adm_upgr_{uid}_monthly"),
            InlineKeyboardButton("🌟 ترقية سنوي Pro", callback_data=f"adm_upgr_{uid}_yearly"),
        ],
        [InlineKeyboardButton("📊 تحميل تقرير شهري", callback_data=f"adm_excel_pick_{uid}")] + ban_row,
        [
            InlineKeyboardButton("🩺 أطباء المندوب",   callback_data=f"adm_doc_list_{uid}_0"),
            InlineKeyboardButton("📋 تقارير المبيعات", callback_data=f"adm_rep_menu_{uid}"),
        ],
        [InlineKeyboardButton("💰 تقارير التسديدات", callback_data=f"adm_pay_rep_menu_{uid}")],
        [InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="adm_page_0")],
    ])
    return msg, kb


async def _admin_show_user(query, uid: int):
    """يعرض تفاصيل مندوب — يُستدعى من زر (callback query)."""
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    if not u:
        await query.edit_message_text("المستخدم غير موجود.")
        return
    msg, kb = _build_user_detail(u, uid)
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_show_user_msg(update: Update, uid: int):
    """نفس شاشة تفاصيل المندوب لكن كرد على رسالة نصية (أمر /find) بدل زر."""
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    if not u:
        await update.message.reply_text("المستخدم غير موجود.")
        return
    msg, kb = _build_user_detail(u, uid)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_do_upgrade(query, uid: int, plan: str, context):
    days = PLAN_DAYS.get(plan, 30)
    label = PLAN_LABEL.get(plan, plan)
    admin_set_subscription(uid, plan, days)
    close_subscription_request(uid)
    end = (date.today() + timedelta(days=days)).isoformat()

    # إشعار المندوب
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    if u and u.get("telegram_id"):
        try:
            await context.bot.send_message(
                chat_id=u["telegram_id"],
                text=(
                    f"🎉 *تم تفعيل اشتراك Pro!*\n\n"
                    f"✅ الخطة: *{label}*\n"
                    f"📅 تنتهي: {end}\n\n"
                    "الآن كل ميزات البوت مفتوحة لك!"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    await query.edit_message_text(
        f"✅ *تم ترقية المستخدم `{uid}` إلى {label}*\n📅 ينتهي: {end}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _admin_confirm_ban(query, uid: int):
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    name = u.get("name", "مجهول") if u else "مجهول"
    await query.edit_message_text(
        f"⚠️ *متأكد تريد تحظر* {name}*؟*\nما راح يقدر يستخدم البوت لين تفك الحظر.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ نعم، احظر", callback_data=f"adm_ban_yes_{uid}"),
            InlineKeyboardButton("❌ إلغاء",      callback_data=f"adm_ban_no_{uid}"),
        ]])
    )


async def _admin_do_ban(query, uid: int, context):
    admin_ban_user(uid)
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    name = u.get("name", "مجهول") if u else "مجهول"
    await query.edit_message_text(
        f"🚫 تم حظر المستخدم *{name}* (`{uid}`)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👤 رجوع للمستخدم", callback_data=f"adm_user_{uid}")
        ]])
    )


async def _admin_do_unban(query, uid: int):
    new_status = admin_unban_user(uid)
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    name = u.get("name", "مجهول") if u else "مجهول"
    status_ar = "نشط 💎" if new_status == "active" else "منتهي 🆓"
    await query.edit_message_text(
        f"✅ تم فك الحظر عن *{name}* — الحالة الحالية: {status_ar}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👤 رجوع للمستخدم", callback_data=f"adm_user_{uid}")
        ]])
    )


async def _admin_excel_month_picker(query, uid: int):
    """يعرض 12 زر شهر للسنة الحالية."""
    today = date.today()
    rows = []
    # 12 شهر بـ 3 أعمدة
    month_row = []
    for m in range(1, 13):
        year = today.year if m <= today.month else today.year - 1
        label = MONTH_NUM_TO_AR.get(m, str(m))
        month_row.append(InlineKeyboardButton(
            label,
            callback_data=f"adm_excel_dl_{uid}_{year}_{m}"
        ))
        if len(month_row) == 3:
            rows.append(month_row)
            month_row = []
    if month_row:
        rows.append(month_row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"adm_user_{uid}")])
    await query.edit_message_text(
        f"📊 اختر الشهر لتحميل Excel المندوب `{uid}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _admin_excel_download(query, uid: int, year: int, month: int, context):
    orders = get_orders_for_admin_excel(uid, month, year)
    if not orders:
        await query.answer(f"لا يوجد أوردرات في هذا الشهر.", show_alert=True)
        return

    excel = await run_blocking(generate_excel_report, orders, filters={"month": month, "year": year})
    if not excel:
        await query.answer("فشل إنشاء الملف.", show_alert=True)
        return

    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    rep_name = (u.get("name") or f"rep{uid}").replace(" ", "_") if u else f"rep{uid}"
    month_name = MONTH_NUM_TO_AR.get(month, str(month))
    filename = f"{rep_name}_{month_name}_{year}.xlsx"

    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=excel,
        filename=filename,
        caption=f"📊 أوردرات *{u.get('name','مجهول') if u else uid}* — {month_name} {year}\nعدد الأوردرات: {len(orders)}",
        parse_mode=ParseMode.MARKDOWN,
    )
    await query.answer("تم إرسال الملف ✅")


async def _admin_pending_requests(update: Update):
    reqs = get_pending_requests()
    if not reqs:
        await update.message.reply_text("✅ لا توجد طلبات اشتراك معلقة.")
        return
    await _send_requests_list(update.message.reply_text, reqs)


async def _admin_pending_requests_callback(query):
    reqs = get_pending_requests()
    home_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")]])
    if not reqs:
        await query.edit_message_text("✅ لا توجد طلبات اشتراك معلقة.", reply_markup=home_kb)
        return
    await _send_requests_list(query.edit_message_text, reqs)


async def _send_requests_list(send_fn, reqs: list):
    lines = ["📬 *طلبات الاشتراك المعلقة:*\n"]
    buttons = []
    for r in reqs[:10]:
        name = r.get("name") or "مجهول"
        uname = f"@{r['username']}" if r.get("username") else ""
        plan = "شهري" if r["plan_type"] == "monthly" else "سنوي"
        lines.append(f"• *{name}* {uname} — {plan}")
        buttons.append([
            InlineKeyboardButton(
                f"✅ ترقية {name} ({plan})",
                callback_data=f"adm_upgr_{r['user_id']}_{r['plan_type']}"
            )
        ])
    buttons.append([InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")])
    kb = InlineKeyboardMarkup(buttons)
    await send_fn("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ── أوامر النص ────────────────────────────────────────────────────────────────

# ── إعدادات الذكاء الاصطناعي ─────────────────────────────────────────────────

async def _admin_ai_menu(query):
    key, model = _get_ai_settings()
    key_status = f"مفعّل (`...{key[-4:]}`)" if key else "❌ غير مفعّل"
    model_label = AVAILABLE_AI_MODELS.get(model, model)
    msg = (
        f"🤖 *إعدادات الذكاء الاصطناعي*\n\n"
        f"🔑 المفتاح: {key_status}\n"
        f"📦 الموديل الحالي: *{model_label}*\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 تغيير الموديل", callback_data="adm_ai_model_pick")],
        [InlineKeyboardButton("🔑 تحديث مفتاح API", callback_data="adm_ai_key_prompt")],
        [InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")],
    ])
    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_ai_model_pick(query):
    _, current_model = _get_ai_settings()
    rows = []
    for idx, (model_key, label) in enumerate(AVAILABLE_AI_MODELS.items()):
        mark = "✅ " if model_key == current_model else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"adm_ai_model_set_{idx}")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm_ai_menu")])
    await query.edit_message_text(
        "🤖 *اختر موديل الذكاء الاصطناعي:*\nكل الموديلات تشتغل عبر نفس مفتاح OpenRouter.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _admin_ai_model_set(query, idx: int):
    keys = list(AVAILABLE_AI_MODELS.keys())
    if idx < 0 or idx >= len(keys):
        await query.answer("خيار غير صالح.", show_alert=True)
        return
    model_key = keys[idx]
    set_bot_config("ai_model", model_key)
    await query.answer(f"✅ تم التغيير إلى {AVAILABLE_AI_MODELS[model_key]}")
    await _admin_ai_menu(query)


async def handle_admin_ai_key_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يعالج رسالة المفتاح الجديد بعد ضغط 'تحديث مفتاح API'.
    يرجع True لو عالج الرسالة (حتى bot.py يعرف يوقف معالجة إضافية)."""
    if not context.user_data.get("waiting_admin_ai_key"):
        return False
    context.user_data.pop("waiting_admin_ai_key")
    text = update.message.text.strip()

    if text.lower() in ("إلغاء", "cancel", "لا"):
        await update.message.reply_text("❌ تم الإلغاء.")
        return True

    if len(text) < 15 or " " in text:
        context.user_data["waiting_admin_ai_key"] = True
        await update.message.reply_text(
            "⚠️ هذا ما يبدو مفتاح API صحيح. أرسل المفتاح كامل بدون مسافات، أو *إلغاء*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    await update.message.reply_text("⏳ جاري التحقق من المفتاح...")
    set_bot_config("openrouter_api_key", text)
    # فراغ الكاش فوراً حتى المفتاح الجديد يشتغل بنفس اللحظة بدون انتظار الـ TTL
    from parser import _config_cache
    _config_cache["ts"] = 0

    test_result = await run_blocking(_ai_call, "Reply with exactly: OK", "test")
    if test_result:
        await update.message.reply_text(
            f"✅ *تم تحديث المفتاح وهو يشتغل تمام.*\n🔑 `...{text[-4:]}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "⚠️ *تم حفظ المفتاح، لكن التحقق منه فشل* (ممكن يكون غلط أو المزود واقف مؤقتاً).\n"
            "جرب ترسل طلب أوردر وشوف يشتغل صح أو لا.",
            parse_mode=ParseMode.MARKDOWN,
        )
    return True


# ── نسخة احتياطية شاملة ──────────────────────────────────────────────────────

async def _admin_send_backup(query, context: ContextTypes.DEFAULT_TYPE):
    await query.edit_message_text("⏳ جاري تجهيز النسخة الاحتياطية... قد تاخذ شوي لو البيانات كبيرة.")
    try:
        data = await run_blocking(get_full_backup_data)
        excel = await run_blocking(generate_full_backup_excel, data)
    except Exception as e:
        await query.message.reply_text(f"❌ فشل إنشاء النسخة الاحتياطية: {e}")
        return

    counts = " | ".join(f"{name}: {len(rows)}" for name, rows in data.items())
    today_str = date.today().isoformat()
    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=excel,
        filename=f"salesbot_backup_{today_str}.xlsx",
        caption=f"💾 *نسخة احتياطية شاملة* — {today_str}\n{counts}",
        parse_mode=ParseMode.MARKDOWN,
    )


def _generate_unique_code(plan: str, days: int) -> str | None:
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    for _ in range(5):
        if create_activation_code(code, plan, days):
            return code
        code = "".join(secrets.choice(alphabet) for _ in range(8))
    return None


async def _admin_generate_code_cmd(update: Update, parts: list):
    """يولّد كود تفعيل جديد يقدر أي مندوب يستعمله لتفعيل اشتراكه بنفسه.
    الاستخدام: /activate [monthly/yearly/lifetime] [أيام مخصصة اختياري]"""
    if len(parts) < 2 or parts[1] not in PLAN_DAYS:
        await update.message.reply_text(
            "الاستخدام: `/activate monthly` أو `/activate yearly` أو `/activate lifetime`\n"
            "تكدر تحدد عدد أيام مخصص: `/activate monthly 45`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    plan = parts[1]
    days = PLAN_DAYS.get(plan, 30)
    if len(parts) >= 3 and parts[2].isdigit():
        days = int(parts[2])
    label = PLAN_LABEL.get(plan, plan)

    code = _generate_unique_code(plan, days)
    if not code:
        await update.message.reply_text("❌ تعذّر توليد كود فريد، حاول مرة أخرى.")
        return

    await update.message.reply_text(
        f"✅ *تم توليد كود تفعيل جديد*\n\n"
        f"🔑 الكود: `{code}`\n"
        f"📦 الباقة: {label}\n"
        f"📅 المدة: {days} يوم\n\n"
        f"أرسل الكود للمندوب ليكتبه بالبوت لتفعيل اشتراكه.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _admin_gen_code_menu(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("شهري (30 يوم)", callback_data="adm_gencode_monthly")],
        [InlineKeyboardButton("سنوي (365 يوم)", callback_data="adm_gencode_yearly")],
        [InlineKeyboardButton("مدى الحياة", callback_data="adm_gencode_lifetime")],
        [InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")],
    ])
    await query.edit_message_text("🔑 *اختر نوع الباقة للكود:*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_gen_code_do(query, plan: str):
    days = PLAN_DAYS.get(plan, 30)
    label = PLAN_LABEL.get(plan, plan)
    code = _generate_unique_code(plan, days)
    if not code:
        await query.edit_message_text("❌ تعذّر توليد كود فريد، حاول مرة أخرى.")
        return
    await query.edit_message_text(
        f"✅ *تم توليد كود تفعيل جديد*\n\n"
        f"🔑 الكود: `{code}`\n"
        f"📦 الباقة: {label}\n"
        f"📅 المدة: {days} يوم\n\n"
        f"أرسل الكود للمندوب ليكتبه بالبوت لتفعيل اشتراكه.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 كود ثاني", callback_data="adm_gen_code")],
            [InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")],
        ]),
    )


async def _admin_upgrade_cmd(update: Update, parts: list):
    if len(parts) < 3:
        await update.message.reply_text("الاستخدام: /upgrade [user_id أو @username] [monthly/yearly/lifetime]")
        return
    target, plan = parts[1], parts[2]
    days = PLAN_DAYS.get(plan, 30)
    label = PLAN_LABEL.get(plan, plan)

    if target.startswith("@"):
        u = get_user_by_username(target)
        if not u:
            await update.message.reply_text(f"ما وجدت مستخدم بيوزر {target}")
            return
        uid = u["id"]
    elif target.isdigit():
        uid = int(target)
    else:
        await update.message.reply_text("أرسل user_id رقمياً أو @username")
        return

    admin_set_subscription(uid, plan, days)
    close_subscription_request(uid)
    end = (date.today() + timedelta(days=days)).isoformat()
    await update.message.reply_text(
        f"✅ تم ترقية المستخدم `{uid}` إلى *{label}*\n📅 ينتهي: {end}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _admin_ban_cmd(update: Update, parts: list):
    if len(parts) < 2:
        await update.message.reply_text("الاستخدام: /ban [user_id]")
        return
    uid = int(parts[1])
    admin_ban_user(uid)
    await update.message.reply_text(f"🚫 تم حظر المستخدم `{uid}`", parse_mode=ParseMode.MARKDOWN)


async def _admin_extend_cmd(update: Update, parts: list):
    if len(parts) < 3:
        await update.message.reply_text("الاستخدام: /extend [user_id] [days]")
        return
    uid, days = int(parts[1]), int(parts[2])
    admin_set_subscription(uid, "monthly", days)
    end = (date.today() + timedelta(days=days)).isoformat()
    await update.message.reply_text(
        f"✅ تم تمديد اشتراك `{uid}` لـ {days} يوم\n📅 ينتهي: {end}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── لوحة المبيعات الرئيسية للأدمن ────────────────────────────────────────────

def _gather_dashboard_data(month: int, year: int) -> dict:
    """يجمع كل بيانات لوحة المبيعات بمكالمة واحدة — يحسب تحقيق كل مندوب مرة وحدة
    فقط (كان سابقاً يُحسب مرتين لكل مندوب بنفس الشاشة، كل مرة تفتح 3 اتصالات DB)."""
    from database import get_rep_achievement
    reps = get_all_users_admin_stats(month, year)
    achievement_map = {r["id"]: get_rep_achievement(r["id"], month, year) for r in reps}
    return {"reps": reps, "achievement_map": achievement_map}


async def _admin_main_sales_dashboard(query):
    today = date.today()
    gathered = await run_blocking(_gather_dashboard_data, today.month, today.year)
    reps = gathered["reps"]
    achievement_map = gathered["achievement_map"]
    months_ar = ["", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
                 "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
    m_name = months_ar[today.month]

    total_orders = sum(r.get("order_count", 0) for r in reps)
    total_qty    = sum(r.get("total_qty", 0) for r in reps)
    total_sales  = sum(r.get("total_sales", 0) for r in reps)

    lines = [
        f"🏢 *لوحة المبيعات — {m_name} {today.year}*\n",
        f"👥 المناديب: *{len(reps)}*",
        f"📦 إجمالي الطلبات: *{total_orders}*",
        f"🔢 إجمالي الكميات: *{total_qty}*",
        f"💰 إجمالي المبيعات: *{total_sales:,.0f} IQD*\n",
        "*تفاصيل المناديب (الأكثر مبيعاث):*",
    ]

    # Sort reps by total_sales descending for better view
    reps_sorted = sorted(reps, key=lambda x: x.get("total_sales", 0), reverse=True)
    for r in reps_sorted[:20]:
        name = r.get("name") or "مجهول"
        oc   = r.get("order_count", 0)
        qty  = r.get("total_qty", 0)
        sales = r.get("total_sales", 0)
        plan = r.get("plan") or "—"
        ach = achievement_map.get(r["id"], 0)
        ach_str = f" | 🎯 {ach}%" if ach > 0 else ""
        lines.append(f"• {name}: {oc} طلب | {qty} وحدة | {sales:,.0f} دينار{ach_str} | {plan}")

    btns = [
        [InlineKeyboardButton(
            f"📊 Excel ملخص الكل — {m_name}",
            callback_data=f"adm_all_excel_{today.year}_{today.month}"
        )],
    ]
    for r in reps_sorted[:15]:
        uid  = r["id"]
        name = (r.get("name") or "مجهول")[:22]
        oc   = r.get("order_count", 0)
        ach  = achievement_map.get(r["id"], 0)
        label = f"👤 {name} ({oc} طلب 🎯{ach}%)"
        btns.append([InlineKeyboardButton(
            label,
            callback_data=f"adm_user_{uid}"
        )])
    btns.append([InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btns),
    )


async def _admin_rep_report_menu(query, uid: int):
    """يعرض قائمة تقارير مندوب واحد للأدمن."""
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"
    today = date.today()
    months_ar = ["","يناير","فبراير","مارس","أبريل","مايو","يونيو",
                 "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
    await query.edit_message_text(
        f"📊 *تقارير المندوب — {rep_name}*\n\nاختر نوع التقرير:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 تقرير اليوم",  callback_data=f"adm_rep_today_{uid}"),
                InlineKeyboardButton("📆 هذا الشهر",    callback_data=f"adm_rep_month_{uid}_{today.year}_{today.month}"),
            ],
            [
                InlineKeyboardButton("📈 أداء المندوب", callback_data=f"adm_rep_perf_{uid}"),
                InlineKeyboardButton("🗓 شهور أخرى",    callback_data=f"adm_rep_pick_{uid}"),
            ],
            [InlineKeyboardButton("🔙 رجوع للوحة", callback_data="adm_main_dashboard")],
        ]),
    )


async def _admin_rep_report_today(query, uid: int):
    """تقرير اليوم لمندوب واحد — بنفس تنسيق Excel المندوب."""
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"
    today = date.today()

    orders = fetch_orders_today(uid)
    if not orders:
        await query.message.reply_text(
            f"📭 لا توجد طلبات اليوم للمندوب *{rep_name}*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data=f"adm_rep_menu_{uid}")
            ]]),
        )
        return

    try:
        summary = build_text_summary(orders, "daily")
        await query.message.reply_text(
            f"📅 *تقرير اليوم — {rep_name}*\n\n{summary}",
            parse_mode=ParseMode.MARKDOWN,
        )
        catalog = get_product_catalog(uid)
        period  = f"{today.year}-{today.month:02d}"
        targets = get_drug_targets(uid, period)
        buf = await run_blocking(generate_template_excel, orders, catalog, rep_name, today.month, today.year, drug_targets=targets)
        fname = f"{rep_name}_تقرير_اليوم_{today.isoformat()}.xlsx"
        await query.message.reply_document(
            document=buf,
            filename=fname,
            caption=f"📊 Excel اليوم — {rep_name}",
        )
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ: {e}")


async def _admin_rep_performance(query, uid: int):
    """ملخص أداء مندوب واحد للأدمن."""
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"
    months_ar = ["","يناير","فبراير","مارس","أبريل","مايو","يونيو",
                 "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
    try:
        stats = get_performance_stats(uid)
        curr_name = months_ar[stats["curr_month"]]
        prev_name = months_ar[stats["prev_month"]]
        arrow = "📈" if stats["change_pct"] >= 0 else "📉"
        change_str = f"+{stats['change_pct']}%" if stats["change_pct"] >= 0 else f"{stats['change_pct']}%"

        if stats["curr_orders"] == 0:
            msg = f"📊 *أداء {rep_name} — {curr_name}*\n\n📭 لا توجد طلبات هذا الشهر بعد."
        else:
            msg = (
                f"📊 *أداء المندوب — {rep_name}*\n"
                f"📅 *{curr_name}*\n\n"
                f"📦 الطلبات: *{stats['curr_orders']}* طلب\n"
                f"🔢 الكميات: *{stats['curr_qty']}* وحدة\n"
                f"🏥 الصيدليات المزارة: *{stats['pharmacies_count']}*\n"
                f"💊 أكثر منتج: *{stats['top_product']}*\n\n"
                f"📅 مقارنة بـ {prev_name}: {arrow} *{change_str}*\n"
                f"_(كان {stats['prev_orders']} طلب، {stats['prev_qty']} وحدة)_"
            )
        await query.message.reply_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data=f"adm_rep_menu_{uid}")
            ]]),
        )
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ في جلب الأداء: {e}")


async def _admin_single_rep_excel_msg(message, uid: int, year: int, month: int):
    """نفس _admin_single_rep_excel لكن يُرسل على message بدل query."""
    months_ar = {1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
                 7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"}
    month_name = months_ar.get(month, str(month))
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"
    period   = f"{year}-{month:02d}"
    orders   = fetch_orders(uid, filters={"month": month, "year": year})
    catalog  = get_product_catalog(uid)
    targets  = get_drug_targets(uid, period)
    if not orders and not catalog:
        await message.reply_text(
            f"📭 لا توجد بيانات للمندوب *{rep_name}* في *{month_name} {year}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    summary = build_text_summary(orders or [], "general")
    await message.reply_text(
        f"📊 *{month_name} {year} — {rep_name}*\n\n{summary}",
        parse_mode=ParseMode.MARKDOWN,
    )
    buf = await run_blocking(generate_template_excel, orders or [], catalog, rep_name, month, year, drug_targets=targets)
    fname = f"{rep_name}_{year}_{month:02d}_{month_name}.xlsx"
    await message.reply_document(
        document=buf,
        filename=fname,
        caption=f"📊 تقرير {rep_name} — {month_name} {year}",
    )


async def _admin_all_reps_excel(query, year: int, month: int):
    reps = get_all_users_admin_stats(month, year)
    if not reps:
        await query.answer("لا توجد بيانات.", show_alert=True)
        return
    await query.answer("جارٍ إنشاء الملف…")
    try:
        buf = await run_blocking(generate_admin_summary_excel, reps, month, year)
        fname = f"ملخص_مناديب_{year}_{month:02d}.xlsx"
        await query.message.reply_document(
            document=buf,
            filename=fname,
            caption=f"📊 ملخص مبيعات جميع المناديب — {month}/{year}",
        )
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ في إنشاء الملف: {e}")


async def _admin_single_rep_excel(query, uid: int, year: int, month: int):
    all_users = get_all_users_admin()
    user = next((u for u in all_users if u["id"] == uid), None)
    rep_name = (user.get("name") if user else None) or f"مندوب_{uid}"

    period  = f"{year}-{month:02d}"
    orders  = fetch_orders(uid, filters={"month": month, "year": year})
    catalog = get_product_catalog(uid)
    targets = get_drug_targets(uid, period)

    if not orders and not catalog:
        await query.answer("لا توجد بيانات لهذا المندوب.", show_alert=True)
        return
    await query.answer("جارٍ إنشاء الملف…")
    try:
        buf = await run_blocking(
            generate_template_excel, orders or [], catalog, rep_name, month, year, drug_targets=targets
        )
        fname = f"{rep_name}_{year}_{month:02d}.xlsx"
        await query.message.reply_document(
            document=buf,
            filename=fname,
            caption=f"📊 تقرير {rep_name} — {month}/{year}",
        )
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ في إنشاء الملف: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ── Admin: Doctor Profiles for any rep ───────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

async def _admin_rep_doctors(query, uid: int, page: int = 0):
    """Show paginated doctor profiles of a rep."""
    try:
        import re
        users    = get_all_users_admin()
        u        = next((x for x in users if x["id"] == uid), None)
        rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"

        doctors = get_all_doctor_profiles(uid)
        PAGE  = 8
        start = page * PAGE
        chunk = doctors[start:start + PAGE]

        cls_emoji = {"A": "🟢", "B": "🟡", "C": "🔵"}
        cls_count = {
            "A": sum(1 for d in doctors if d.get("class") == "A"),
            "B": sum(1 for d in doctors if d.get("class") == "B"),
            "C": sum(1 for d in doctors if d.get("class") == "C"),
        }

        def _esc(s):
            return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', s or "")

        rep_name_esc = _esc(rep_name)
        lines = [
            f"🩺 *أطباء المندوب: {rep_name_esc}*\n",
            f"👨‍⚕️ إجمالي: *{len(doctors)}*  \\|  "
            f"🟢 A:{cls_count['A']}  🟡 B:{cls_count['B']}  🔵 C:{cls_count['C']}\n",
        ]
        for d in chunk:
            ce   = cls_emoji.get(d.get("class", "C"), "🔵")
            name = _esc(d.get("name",""))
            spec = _esc(d.get("specialty",""))
            area = _esc(d.get("area",""))
            line = f"{ce} *{name}*"
            if spec: line += f" — {spec}"
            if area: line += f" — {area}"
            lines.append(line)

        btns = []
        for d in chunk[:5]:
            btns.append([InlineKeyboardButton(
                f"📄 {d['name'][:22]}",
                callback_data=f"adm_doc_view_{uid}_{d['id']}",
            )])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"adm_doc_list_{uid}_{page-1}"))
        if start + PAGE < len(doctors):
            nav.append(InlineKeyboardButton("▶️", callback_data=f"adm_doc_list_{uid}_{page+1}"))
        if nav:
            btns.append(nav)

        footer = []
        if doctors:
            footer.append(InlineKeyboardButton("📊 Excel استبيان", callback_data=f"adm_doc_excel_{uid}"))
        footer.append(InlineKeyboardButton("🔙 ملف المندوب", callback_data=f"adm_user_{uid}"))
        btns.append(footer)

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup(btns),
        )
    except Exception as e:
        await query.edit_message_text(
            f"⚠️ حدث خطأ أثناء تحميل قائمة أطباء المندوب. حاول مرة أخرى.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 إعادة المحاولة", callback_data=f"adm_doc_list_{uid}_0"),
                InlineKeyboardButton("🔙 ملف المندوب",    callback_data=f"adm_user_{uid}"),
            ]]),
        )


async def _admin_rep_doctor_detail(query, uid: int, doc_id: int):
    """Show full details of one doctor profile (admin view, read-only)."""
    from database import get_doctor_profile_by_id
    users = get_all_users_admin()
    u     = next((x for x in users if x["id"] == uid), None)
    rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"

    doc = get_doctor_profile_by_id(doc_id, uid)
    if not doc:
        await query.answer("⚠️ لم يُعثر على الطبيب.", show_alert=True)
        return

    competitors = get_competitor_entries(uid, doc_id)
    comp_lines  = ""
    if competitors:
        comp_lines = "\n\n🦠 *منافسون:*\n" + "\n".join(
            f"  • {c.get('competitor_name','—')} — {c.get('competitor_product','—')}"
            + (f"\n    _{c.get('notes')}_" if c.get("notes") else "")
            for c in competitors
        )

    ce  = {"A": "🟢", "B": "🟡", "C": "🔵"}.get(doc.get("class","C"), "🔵")
    msg = (
        f"🩺 *د. {doc['name']}*  |  مندوب: {rep_name}\n\n"
        f"🔬 التخصص: {doc.get('specialty') or '—'}\n"
        f"🏥 العيادة: {doc.get('clinic_name') or '—'}\n"
        f"📍 المنطقة: {doc.get('area') or '—'}\n"
        f"📞 الهاتف:  {doc.get('phone') or '—'}\n"
        f"{ce} الفئة: *{doc.get('class','C')}*\n"
        f"📝 ملاحظات: {doc.get('notes') or '—'}"
        + comp_lines
    )

    # Count orders linked to this doctor
    from database import get_orders_for_doctor
    orders = get_orders_for_doctor(uid, doc["name"])
    msg += f"\n\n📦 الطلبات المرتبطة: *{len(orders)}*"

    await query.edit_message_text(
        msg, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 قائمة الأطباء", callback_data=f"adm_doc_list_{uid}_0"),
            InlineKeyboardButton("👤 ملف المندوب",    callback_data=f"adm_user_{uid}"),
        ]]),
    )


async def _admin_rep_doctors_excel(query, uid: int, context):
    """Generate and send a survey Excel for a rep's doctors (admin action)."""
    users    = get_all_users_admin()
    u        = next((x for x in users if x["id"] == uid), None)
    rep_name = u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"
    doctors  = get_all_doctor_profiles(uid)

    if not doctors:
        await query.answer("⚠️ لا يوجد أطباء مسجلون لهذا المندوب.", show_alert=True)
        return

    await query.answer("⏳ جارٍ إنشاء ملف الاستبيان…")
    today = date.today()
    try:
        buf   = await run_blocking(generate_survey_excel, doctors, rep_name, today.month, today.year)
        fname = f"استبيان_{rep_name}_{today.month}_{today.year}.xlsx"
        await query.message.reply_document(
            document=buf,
            filename=fname,
            caption=(
                f"📊 *استبيان أطباء — {rep_name}*\n"
                f"👨‍⚕️ {len(doctors)} طبيب  |  "
                f"🟢 A:{sum(1 for d in doctors if d.get('class')=='A')}  "
                f"🟡 B:{sum(1 for d in doctors if d.get('class')=='B')}  "
                f"🔵 C:{sum(1 for d in doctors if d.get('class')=='C')}"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ: `{e}`", parse_mode=ParseMode.MARKDOWN)


# ── التسديدات (تحصيل من الصيدليات) — لوحة الأدمن ─────────────────────────────

async def _admin_payments_menu(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 الصيدليات المدينة", callback_data="adm_pay_debts")],
        [InlineKeyboardButton("📥 تصدير Excel شامل", callback_data="adm_pay_excel")],
        [InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")],
    ])
    await query.edit_message_text(
        "💰 *التسديدات*\n\nعرض كل صيدليات كل المناديب المدينة، أو تصدير سجل التسديدات كامل.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def _admin_payment_debts(query):
    await query.answer("جارٍ الحساب…")
    balances = await run_blocking(get_rep_pharmacy_balances, None)

    if not balances:
        await query.edit_message_text(
            "✅ ما فيه صيدليات مدينة حالياً (أو ما فيه أسعار بالكتالوج لحساب الديون).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")]]
            ),
        )
        return

    all_users = await run_blocking(get_all_users_admin)
    name_by_uid = {u["id"]: u.get("name") for u in all_users}

    lines = ["💰 *الصيدليات المدينة* (الأعلى أولاً):\n"]
    for b in balances[:30]:
        rep_name = name_by_uid.get(b.get("user_id"), "؟")
        remaining = b.get("remaining")
        if remaining:
            lines.append(f"🏪 {b['pharmacy_name']} — *{remaining:,.0f}* د.ع  ({rep_name})")

    if len(balances) > 30:
        lines.append(f"\n… و{len(balances) - 30} صيدلية إضافية (صدّر Excel للقائمة الكاملة)")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 تصدير Excel شامل", callback_data="adm_pay_excel")],
        [InlineKeyboardButton("🏠 الرئيسية", callback_data="adm_home")],
    ])
    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def _admin_payments_excel(query):
    await query.answer("جارٍ إنشاء الملف…")
    try:
        payments = await run_blocking(get_all_payments)
        if not payments:
            await query.message.reply_text("لا توجد تسديدات مسجّلة بعد.")
            return

        balances = await run_blocking(get_rep_pharmacy_balances, None)

        totals_by_rep = {}
        for p in payments:
            key = p.get("rep_name") or "؟"
            totals_by_rep[key] = totals_by_rep.get(key, 0) + (p.get("amount") or 0)
        rep_totals = [{"rep_name": k, "total": v} for k, v in totals_by_rep.items()]

        buf = await run_blocking(generate_payment_excel, payments, balances, rep_totals, True)
        await query.message.reply_document(
            document=buf,
            filename="تسديدات_كل_المناديب.xlsx",
            caption="💰 كل التسديدات — كل المناديب",
        )
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ في إنشاء الملف: {e}")


# ── تقارير التسديد لمندوب واحد (من ملف المندوب بلوحة الأدمن) ─────────────────

def _pay_rep_name(uid: int) -> str:
    users = get_all_users_admin()
    u = next((x for x in users if x["id"] == uid), None)
    return u.get("name", f"مندوب {uid}") if u else f"مندوب {uid}"


async def _admin_pay_rep_menu(query, uid: int):
    rep_name = await run_blocking(_pay_rep_name, uid)
    today = date.today()
    await query.edit_message_text(
        f"💰 *تقارير التسديد — {rep_name}*\n\nاختر الفترة:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 تقرير اليوم", callback_data=f"adm_pay_rep_today_{uid}"),
                InlineKeyboardButton("📆 هذا الشهر",   callback_data=f"adm_pay_rep_month_{uid}_{today.year}_{today.month}"),
            ],
            [InlineKeyboardButton("🗓 شهور أخرى", callback_data=f"adm_pay_rep_pick_{uid}")],
            [InlineKeyboardButton("🔙 رجوع للوحة", callback_data="adm_main_dashboard")],
        ]),
    )


async def _admin_pay_rep_today(query, uid: int):
    rep_name = await run_blocking(_pay_rep_name, uid)
    today = date.today()
    await _admin_pay_rep_send(
        query.message, uid, rep_name,
        today.isoformat(), today.isoformat(),
        f"تسديدات_{rep_name}_{today.isoformat()}.xlsx",
        "اليوم",
        back_cb=f"adm_pay_rep_menu_{uid}",
    )


async def _admin_pay_rep_month(query, uid: int, year: int, month: int):
    rep_name = await run_blocking(_pay_rep_name, uid)
    await _admin_pay_rep_month_msg(query.message, uid, year, month, rep_name, back_cb=f"adm_pay_rep_menu_{uid}")


async def _admin_pay_rep_month_msg(message, uid: int, year: int, month: int,
                                    rep_name: str = None, back_cb: str = None):
    """نفس منطق تقرير الشهر — قابلة للاستدعاء من bot.py (اختيار شهر عبر النص) أو من admin.py."""
    rep_name = rep_name or await run_blocking(_pay_rep_name, uid)
    months_ar = {1:"يناير",2:"فبراير",3:"مارس",4:"أبريل",5:"مايو",6:"يونيو",
                 7:"يوليو",8:"أغسطس",9:"سبتمبر",10:"أكتوبر",11:"نوفمبر",12:"ديسمبر"}
    date_from = date(year, month, 1).isoformat()
    date_to = (date(year, month + 1, 1).isoformat() if month < 12 else date(year + 1, 1, 1).isoformat())
    fname = f"تسديدات_{rep_name}_{year}_{month:02d}.xlsx"
    await _admin_pay_rep_send(message, uid, rep_name, date_from, date_to, fname,
                               f"{months_ar.get(month, month)} {year}", back_cb=back_cb)


async def _admin_pay_rep_send(message, uid: int, rep_name: str, date_from: str, date_to: str,
                               fname: str, period_label: str, back_cb: str = None):
    payments = await run_blocking(get_payments_by_rep, uid, {"date_from": date_from, "date_to": date_to})
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=back_cb)]]) if back_cb else None
    if not payments:
        await message.reply_text(
            f"📭 لا توجد تسديدات للمندوب *{rep_name}* — {period_label}.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
        return
    pharmacies = sorted({p["pharmacy_name"] for p in payments})
    balances = [await run_blocking(get_pharmacy_balance, uid, ph) for ph in pharmacies]
    buf = await run_blocking(generate_payment_excel, payments, balances, None, False)
    await message.reply_document(
        document=buf,
        filename=fname,
        caption=f"💰 تسديدات {rep_name} — {period_label}",
        reply_markup=kb,
    )
