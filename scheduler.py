import logging
from datetime import date, time, datetime
from telegram.ext import Application
from telegram.constants import ParseMode

from database import (
    get_all_active_users, fetch_orders_today, fetch_orders, get_user_settings,
    get_inactive_pharmacies, get_hours_since_last_order,
)
from reports import build_text_summary, generate_excel_report

logger = logging.getLogger(__name__)


async def send_daily_summary(context):
    today = date.today()
    users = get_all_active_users()
    for user in users:
        try:
            settings = get_user_settings(user["id"])
            if not settings.get("daily_summary"):
                continue
            orders = fetch_orders_today(user["id"])
            if not orders:
                await context.bot.send_message(
                    chat_id=user["telegram_id"],
                    text=f"📋 *ملخص يومك — {today}*\n\nلا توجد طلبات مسجلة اليوم.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                continue
            summary = build_text_summary(orders, "general")
            await context.bot.send_message(
                chat_id=user["telegram_id"],
                text=f"📋 *ملخص يومك — {today}*\n\n{summary}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Daily summary error for user {user['id']}: {e}")


async def send_weekly_report(context):
    from datetime import timedelta
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    users = get_all_active_users()
    for user in users:
        try:
            settings = get_user_settings(user["id"])
            if not settings.get("weekly_report"):
                continue
            orders = fetch_orders(user["id"], {"date_from": week_ago, "date_to": today.isoformat()})
            if not orders:
                continue
            summary = build_text_summary(orders, "general")
            excel = generate_excel_report(orders)
            await context.bot.send_message(
                chat_id=user["telegram_id"],
                text=f"📊 *تقريرك الأسبوعي*\n\n{summary}",
                parse_mode=ParseMode.MARKDOWN,
            )
            if excel:
                await context.bot.send_document(
                    chat_id=user["telegram_id"],
                    document=excel,
                    filename=f"تقرير_اسبوعي_{today}.xlsx",
                    caption="📊 التقرير الأسبوعي كاملاً",
                )
        except Exception as e:
            logger.error(f"Weekly report error for user {user['id']}: {e}")


async def check_inactivity_alert(context):
    """تنبيه إذا ما سجّل المندوب طلبا من 3 ساعات خلال وقت العمل."""
    tz = context.application.defaults.tzinfo if context.application.defaults else None
    now = datetime.now(tz)
    hour = now.hour
    if hour < 8 or hour > 20:
        return
    users = get_all_active_users()
    for user in users:
        try:
            settings = get_user_settings(user["id"])
            if not settings.get("alert_inactivity", True):
                continue
            hours = get_hours_since_last_order(user["id"])
            if hours is None or hours < 3:
                continue
            hrs = int(hours)
            await context.bot.send_message(
                chat_id=user["telegram_id"],
                text=(
                    f"⏰ *تنبيه نشاط*\n\n"
                    f"ما سجلت أي طلب من *{hrs} ساعة*.\n"
                    f"لا تنسى تسجيل زياراتك! 💪"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Inactivity alert error for user {user['id']}: {e}")


async def check_unvisited_pharmacies(context):
    """تنبيه بالصيدليات التي لم تُزار من أسبوع."""
    users = get_all_active_users()
    for user in users:
        try:
            settings = get_user_settings(user["id"])
            if not settings.get("alert_unvisited", True):
                continue
            inactive = get_inactive_pharmacies(user["id"], days=7)
            if not inactive:
                continue
            lines = "\n".join(
                f"• {p['pharmacy']} — آخر زيارة قبل {p['days_ago']} يوم"
                for p in inactive[:5]
            )
            await context.bot.send_message(
                chat_id=user["telegram_id"],
                text=(
                    f"🔔 *صيدليات لم تزرها من فترة:*\n\n{lines}\n\n"
                    f"_ذكّرها اليوم! 🏥_"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Unvisited alert error for user {user['id']}: {e}")


async def check_bot_health(context):
    """فحص صحة دوري خفيف — يراقب DB، إعداد الذكاء الاصطناعي، الذاكرة، والـ tasks
    العالقة. ينبّه الأدمن فقط لو صار شي غلط فعلاً (مو رسالة "كل شي تمام" كل مرة،
    حتى ما نزعج الأدمن برسائل بلا داعي)."""
    import asyncio
    import resource
    from admin import ADMIN_IDS
    from database import get_connection
    from parser import ai_is_enabled

    problems = []

    # 1) قاعدة البيانات — فحص اتصال سريع بـ thread منفصل (استعلام متزامن)
    def _db_ping():
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT 1")
            c.fetchone()
            return True
        finally:
            conn.close()

    try:
        loop = asyncio.get_running_loop()
        ok = await asyncio.wait_for(loop.run_in_executor(None, _db_ping), timeout=10)
        if not ok:
            problems.append("⚠️ قاعدة البيانات: الاستعلام التجريبي ما رجّع نتيجة متوقعة")
    except asyncio.TimeoutError:
        problems.append("🔴 قاعدة البيانات: لا تستجيب (timeout بعد 10 ثواني)")
    except Exception as e:
        problems.append(f"🔴 قاعدة البيانات: فشل الاتصال — {e}")

    # 2) الذكاء الاصطناعي — فقط تحقق من وجود مفتاح مُعدّ (بدون استدعاء API فعلي
    #    حتى ما نستهلك حصة الاستخدام كل نصف ساعة بلا داعي)
    if not ai_is_enabled():
        problems.append("⚠️ الذكاء الاصطناعي: لا يوجد مفتاح API مُفعّل حالياً")

    # 3) الذاكرة — ru_maxrss بالـ Linux/Android يرجع KB
    try:
        mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mem_mb = mem_kb / 1024
        threshold_mb = int(__import__("os").environ.get("HEALTH_MEM_THRESHOLD_MB", "500"))
        if mem_mb > threshold_mb:
            problems.append(f"⚠️ الذاكرة: استهلاك {mem_mb:.0f}MB (أعلى من الحد {threshold_mb}MB)")
    except Exception:
        pass  # resource module قد لا يكون متوفر ببعض بيئات Windows — تجاهل بأمان

    # 4) عدد الـ asyncio tasks — ارتفاع غير طبيعي يدل على tasks عالقة/متسربة
    try:
        n_tasks = len(asyncio.all_tasks())
        threshold_tasks = int(__import__("os").environ.get("HEALTH_TASKS_THRESHOLD", "200"))
        if n_tasks > threshold_tasks:
            problems.append(f"⚠️ عدد المهام النشطة {n_tasks} أعلى من الطبيعي (قد يدل على تسريب)")
    except Exception:
        pass

    if problems:
        msg = "🩺 *تنبيه فحص صحة البوت:*\n\n" + "\n".join(problems)
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Health alert delivery failed for admin {admin_id}: {e}")
        logger.warning("Health check found issues: %s", problems)


def setup_scheduler(app: Application):
    jq = app.job_queue
    jq.run_daily(send_daily_summary, time=time(hour=20, minute=0))
    jq.run_daily(send_weekly_report, time=time(hour=20, minute=30), days=(6,))
    jq.run_repeating(check_inactivity_alert, interval=10800, first=60)   # كل 3 ساعات
    jq.run_daily(check_unvisited_pharmacies, time=time(hour=19, minute=0))  # كل يوم 7م
    jq.run_repeating(check_bot_health, interval=1800, first=120)  # فحص صحة كل 30 دقيقة
    logger.info("Scheduler jobs registered.")
