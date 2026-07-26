from datetime import date
from database import get_subscription

# ── أسماء الخطط ────────────────────────────────────────────────────────────
PLAN_NAMES = {
    "basic":    "الخطة الأساسية",
    "trial":    "الشهر الأول المجاني",
    "pro":      "الخطة الاحترافية Pro",
    "monthly":  "اشتراك شهري Pro",
    "yearly":   "اشتراك سنوي Pro",
    "lifetime": "ترخيص دائم",
}

# ── الأسعار ─────────────────────────────────────────────────────────────────
PRICES = {
    "monthly": "5,000 دينار عراقي / شهر",
    "yearly":  "50,000 دينار عراقي / سنة",
}

QI_CARD_NUMBER = "8223886667"

# ── صلاحيات الخطط ──────────────────────────────────────────────────────────
#
# basic_active  → أول شهر: تسجيل أوردرات + تقارير شخصية + Excel
# basic_expired → بعد الشهر: تسجيل أوردرات فقط (بدون تقارير)
# pro_active    → كل الميزات مفتوحة
# banned        → محظور كلياً
#
PLAN_FEATURES = {
    "basic_active": {
        "orders":   True,
        "reports":  True,
        "excel":    True,
        "search":   True,
        "pharmacy": True,
        "alerts":   True,
        "team":     True,
    },
    "basic_expired": {
        "orders":   True,
        "reports":  False,
        "excel":    False,
        "search":   False,
        "pharmacy": False,
        "alerts":   False,
        "team":     False,
    },
    "pro": {
        "orders":   True,
        "reports":  True,
        "excel":    True,
        "search":   True,
        "pharmacy": True,
        "alerts":   True,
        "team":     True,
    },
}


def check_subscription(user_id: int) -> dict:
    """
    Returns a dict with:
      allowed       — يقدر يستخدم البوت بشكل عام
      features      — dict بالميزات المتاحة
      plan          — اسم الخطة
      days_left     — أيام متبقية (None إذا مدى الحياة)
      end_date      — تاريخ الانتهاء
      reason        — سبب الرفض إذا allowed=False
      soft_locked   — انتهى الشهر الأول (أوردرات فقط)
    """
    sub = get_subscription(user_id)
    if not sub:
        return {"allowed": False, "reason": "no_sub", "features": {}}

    if sub["status"] == "banned":
        return {"allowed": False, "reason": "banned", "features": {}}

    plan = sub.get("plan", "trial")

    # ── مدى الحياة / Pro ────────────────────────────────────────────────────
    if plan == "lifetime":
        return {
            "allowed": True, "plan": "lifetime",
            "features": PLAN_FEATURES["pro"],
            "days_left": None, "end_date": None,
            "soft_locked": False,
        }

    # ── Pro (شهري / سنوي) ───────────────────────────────────────────────────
    if plan in ("pro", "monthly", "yearly"):
        if sub.get("end_date"):
            end = date.fromisoformat(sub["end_date"])
            days_left = (end - date.today()).days
            if days_left < 0:
                # انتهى Pro → soft lock أوردرات فقط
                return {
                    "allowed": True,
                    "plan": plan,
                    "features": PLAN_FEATURES["basic_expired"],
                    "days_left": 0,
                    "end_date": sub["end_date"],
                    "soft_locked": True,
                    "reason": "pro_expired",
                }
            return {
                "allowed": True, "plan": plan,
                "features": PLAN_FEATURES["pro"],
                "days_left": days_left,
                "end_date": sub["end_date"],
                "soft_locked": False,
            }
        return {"allowed": False, "reason": "no_end_date", "features": {}}

    # ── الخطة الأساسية / trial (الشهر الأول) ────────────────────────────────
    if plan in ("basic", "trial"):
        if sub.get("end_date"):
            end = date.fromisoformat(sub["end_date"])
            days_left = (end - date.today()).days
            if days_left < 0:
                # انتهى الشهر الأول → أوردرات فقط
                return {
                    "allowed": True,
                    "plan": plan,
                    "features": PLAN_FEATURES["basic_expired"],
                    "days_left": 0,
                    "end_date": sub["end_date"],
                    "soft_locked": True,
                    "reason": "basic_expired",
                }
            return {
                "allowed": True, "plan": plan,
                "features": PLAN_FEATURES["basic_active"],
                "days_left": days_left,
                "end_date": sub["end_date"],
                "soft_locked": False,
            }
        return {"allowed": False, "reason": "no_end_date", "features": {}}

    return {"allowed": False, "reason": "unknown_plan", "features": {}}


def can_use(sub: dict, feature: str) -> bool:
    """تحقق إذا المستخدم يقدر يستخدم ميزة معينة."""
    return bool(sub.get("features", {}).get(feature, False))


def format_expired_basic_message() -> str:
    return (
        "🔒 *انتهى الشهر الأول المجاني*\n\n"
        "✅ تقدر تكمل *تسجيل الأوردرات* مجاناً\n"
        "❌ التقارير والبحث وباقي الميزات تحتاج اشتراك Pro\n\n"
        "🚀 *للترقية إلى Pro:*\n"
        "اضغط زر *ترقية إلى Pro* أو أرسل: `اريد اشتراك`\n\n"
        f"💰 الأسعار:\n"
        f"• شهري: {PRICES['monthly']}\n"
        f"• سنوي: {PRICES['yearly']}"
    )


def format_expired_pro_message() -> str:
    return (
        "🔒 *انتهى اشتراك Pro*\n\n"
        "✅ تقدر تكمل *تسجيل الأوردرات* فقط\n"
        "❌ التقارير وباقي الميزات موقوفة مؤقتاً\n\n"
        "🔄 *لتجديد الاشتراك:*\n"
        "اضغط زر *ترقية إلى Pro* أو أرسل: `اريد اشتراك`"
    )


def format_banned_message() -> str:
    return "🚫 تم إيقاف حسابك. تواصل مع الإدارة."


def format_welcome_trial(name: str, days_left: int, end_date: str) -> str:
    return (
        f"👋 أهلاً *{name}*!\n\n"
        f"🎁 *الشهر الأول المجاني* — متبقي *{days_left} يوم* (ينتهي {end_date})\n\n"
        "📦 *ما تقدر تسوّيه الآن:*\n"
        "• ✅ تسجيل الأوردرات\n"
        "• ✅ تقارير شخصية\n"
        "• ✅ Excel شهري\n\n"
        "🚀 *للحصول على كل الميزات (تقارير ذكية، بحث، صيدليات، تنبيهات):*\n"
        "اشترك بـ Pro بعد انتهاء الشهر المجاني\n\n"
        "أرسل طلبك مباشرة أو اختر من القائمة:"
    )


