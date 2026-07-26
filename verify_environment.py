#!/usr/bin/env python3
"""
verify_environment.py — فحص بيئة التشغيل قبل تشغيل البوت.

أداة مستقلة كلياً عن كود البوت — ما تستورد bot.py ولا أي ملف من المشروع،
حتى تشتغل حتى لو البوت نفسه معطّل. ما تحتاج إنترنت إطلاقاً.

الاستخدام:
    python verify_environment.py

يفحص: نسخة Python، نظام التشغيل (وTermux/أندرويد تحديداً)، اكتمال
requirements.txt، إمكانية استيراد كل مكتبة فعلياً، وجود .env والمتغيرات
المطلوبة فيه، الاتصال بقاعدة البيانات (لو الإعدادات موجودة)، صلاحية الكتابة
بمجلد المشروع وبالمجلد المؤقت، وإنشاء/قراءة ملف Excel تجريبي بـopenpyxl.
"""

import sys
import os
import io
import platform
import tempfile
import importlib
import importlib.metadata as importlib_metadata

# ── إعداد عام ─────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_PATH = os.path.join(PROJECT_DIR, "requirements.txt")
ENV_PATH = os.path.join(PROJECT_DIR, ".env")
MIN_PYTHON = (3, 9)  # zoneinfo (مستخدمة بـbot.py) تحتاج 3.9+

# اسم الحزمة بـrequirements.txt → (اسم الاستيراد الفعلي أو None لو حزمة بيانات
# فقط بدون import مباشر، وصف مختصر لأي شي يستخدمها)
PACKAGE_IMPORT_MAP = {
    "python-telegram-bot": ("telegram", "التواصل مع Telegram API"),
    "tzdata": (None, "قاعدة بيانات المناطق الزمنية (تُستخدم ضمنياً عبر zoneinfo)"),
    "python-dotenv": ("dotenv", "تحميل ملف .env"),
    "openpyxl": ("openpyxl", "إنشاء وقراءة ملفات Excel"),
    "psycopg[binary]": ("psycopg", "الاتصال بقاعدة بيانات PostgreSQL"),
    "psycopg-pool": ("psycopg_pool", "تجميع اتصالات قاعدة البيانات (Connection Pool)"),
    "requests": ("requests", "استدعاء واجهة الذكاء الاصطناعي (OpenRouter)"),
}

# متغيرات .env: (اسم المتغير, ضروري لتشغيل أساسي؟, وصف قصير)
ENV_VARS = [
    ("TELEGRAM_BOT_TOKEN", True, "توكن البوت من BotFather — بدونه البوت ما يشتغل إطلاقاً"),
    ("DATABASE_URL", True, "رابط اتصال PostgreSQL — بدونه ما توجد قاعدة بيانات"),
    ("ADMIN_TELEGRAM_IDS", False, "أرقام تيليگرام للأدمن — بدونها ما فيه وصول للوحة الأدمن"),
    ("OPENROUTER_API_KEY", False, "مفتاح الذكاء الاصطناعي — بدونه تسجيل الطلبات لا يعمل (ميزة أساسية!)"),
]

RESULTS = []  # (اسم الفحص, الحالة "PASS"/"FAIL"/"WARN"/"SKIP", قائمة تفاصيل)


def record(name, status, details=None):
    RESULTS.append((name, status, details or []))


def print_line(label, status):
    dots = "." * max(1, 22 - len(label))
    print(f"{label} {dots} {status}")


# ── 1) Python ───────────────────────────────────────────────────────────────
def check_python():
    details = []
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"
    details.append(f"النسخة المكتشفة: Python {version_str}")
    if version >= MIN_PYTHON:
        record("Python", "PASS", details)
    else:
        details.append(
            f"الحد الأدنى المطلوب Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
            f"(البوت يستخدم zoneinfo المتوفرة من 3.9 فقط)"
        )
        details.append("الحل: ثبّت نسخة بايثون أحدث (على Termux: pkg install python)")
        record("Python", "FAIL", details)


# ── 2) Environment (OS / Termux detection) ──────────────────────────────────
def check_environment():
    details = []
    system = platform.system()
    machine = platform.machine()
    details.append(f"نظام التشغيل: {system} ({machine})")

    is_termux = bool(
        os.environ.get("TERMUX_VERSION")
        or "com.termux" in os.environ.get("PREFIX", "")
        or "com.termux" in sys.prefix
    )
    if is_termux:
        details.append("✅ تم اكتشاف بيئة Termux على أندرويد")
    elif system == "Linux" and os.path.exists("/system/build.prop"):
        details.append("⚠️ يبدو نظام أندرويد لكن بدون متغيرات Termux المعتادة")
    else:
        details.append("بيئة عادية (سيرفر/حاسوب) — مو Termux")

    details.append(f"مجلد المشروع: {PROJECT_DIR}")
    record("Environment", "PASS", details)


# ── 3) Requirements.txt completeness ────────────────────────────────────────
def check_requirements():
    details = []
    if not os.path.exists(REQUIREMENTS_PATH):
        details.append(f"ملف requirements.txt غير موجود بـ {REQUIREMENTS_PATH}")
        record("Requirements", "FAIL", details)
        return

    declared = []
    with open(REQUIREMENTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # "python-telegram-bot[job-queue]==21.6" -> "python-telegram-bot"
            pkg = line.split("[")[0].split("==")[0].split(">=")[0].split("<")[0].strip()
            declared.append(pkg)

    missing = []
    for pkg in declared:
        try:
            importlib_metadata.version(pkg)
        except importlib_metadata.PackageNotFoundError:
            missing.append(pkg)

    if missing:
        details.append(f"مكتبات بـrequirements.txt لكن غير مثبتة: {', '.join(missing)}")
        details.append(f"الحل: pip install -r {os.path.basename(REQUIREMENTS_PATH)}")
        record("Requirements", "FAIL", details)
    else:
        details.append(f"كل الحزم الـ{len(declared)} المذكورة بـrequirements.txt مثبتة")
        record("Requirements", "PASS", details)


# ── 4) Actual importability ─────────────────────────────────────────────────
def check_imports():
    details = []
    failed = []
    for pkg, (import_name, purpose) in PACKAGE_IMPORT_MAP.items():
        if import_name is None:
            continue  # حزمة بيانات فقط، ما فيها import مباشر (مثل tzdata)
        try:
            importlib.import_module(import_name)
        except ImportError as e:
            failed.append((pkg, import_name, purpose, str(e)))

    if failed:
        for pkg, import_name, purpose, err in failed:
            details.append(f"فشل استيراد '{import_name}' (من حزمة {pkg}, تُستخدم لـ: {purpose})")
            details.append(f"  الخطأ: {err}")
        details.append(f"الحل: pip install -r requirements.txt")
        record("Imports", "FAIL", details)
    else:
        ok_names = [v[0] for v in PACKAGE_IMPORT_MAP.values() if v[0]]
        details.append(f"كل المكتبات ({', '.join(ok_names)}) تُستورد بنجاح فعلياً")
        record("Imports", "PASS", details)


# ── 5) Database connectivity (only if configured) ───────────────────────────
def check_database():
    details = []
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url and os.path.exists(ENV_PATH):
        db_url = _read_env_file().get("DATABASE_URL", "")

    if not db_url:
        details.append("DATABASE_URL غير مُعدّ بعد — تخطّي فحص الاتصال")
        details.append("لازم تضيفه بـ.env قبل ما تشغّل البوت فعلياً")
        record("Database", "SKIP", details)
        return

    try:
        import psycopg
    except ImportError:
        details.append("psycopg غير مثبتة — ما نقدر نفحص الاتصال (راجع فحص Imports)")
        record("Database", "FAIL", details)
        return

    try:
        conn = psycopg.connect(db_url, connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        details.append("الاتصال بقاعدة البيانات نجح (SELECT 1 اشتغل)")
        record("Database", "PASS", details)
    except Exception as e:
        details.append(f"فشل الاتصال: {e}")
        details.append("تأكد DATABASE_URL صحيح، وإن السيرفر شغّال، وإن الجهاز عنده إنترنت")
        record("Database", "FAIL", details)


# ── 6) .env file + required variables ───────────────────────────────────────
def _read_env_file():
    """قراءة يدوية بسيطة لـ.env — ما تعتمد على مكتبة python-dotenv، حتى الفحص
    يضل مستقل حتى لو تلك المكتبة نفسها فيها مشكلة."""
    values = {}
    if not os.path.exists(ENV_PATH):
        return values
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def check_dotenv():
    details = []
    if not os.path.exists(ENV_PATH):
        details.append(f".env غير موجود بـ {ENV_PATH}")
        details.append("الحل: cp .env.example .env — وعدّل القيم بداخله")
        record(".env", "FAIL", details)
        return

    env_values = _read_env_file()
    # القيم ممكن تكون بالبيئة (export) بدل .env أيضاً — نتحقق من الاثنين
    missing_required = []
    missing_optional = []
    for name, required, purpose in ENV_VARS:
        present = bool(os.environ.get(name) or env_values.get(name))
        if not present:
            if required:
                missing_required.append((name, purpose))
            else:
                missing_optional.append((name, purpose))

    if missing_required:
        for name, purpose in missing_required:
            details.append(f"ناقص (ضروري): {name} — {purpose}")
        for name, purpose in missing_optional:
            details.append(f"ناقص (اختياري): {name} — {purpose}")
        record(".env", "FAIL", details)
    elif missing_optional:
        for name, purpose in missing_optional:
            details.append(f"ناقص (اختياري): {name} — {purpose}")
        details.append(".env موجود وكل المتغيرات الضرورية موجودة، لكن فيه اختيارية ناقصة")
        record(".env", "WARN", details)
    else:
        details.append("كل المتغيرات المطلوبة (ضرورية واختيارية) موجودة")
        record(".env", "PASS", details)


# ── 7) Filesystem write access (project dir + temp) ─────────────────────────
def check_filesystem():
    details = []
    ok = True

    # كتابة/حذف بمجلد المشروع
    test_path = os.path.join(PROJECT_DIR, ".verify_write_test.tmp")
    try:
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("test")
        with open(test_path, encoding="utf-8") as f:
            content = f.read()
        os.remove(test_path)
        if content == "test":
            details.append("الكتابة/القراءة/الحذف بمجلد المشروع: نجحت")
        else:
            ok = False
            details.append("محتوى الملف بمجلد المشروع غير متطابق بعد القراءة")
    except Exception as e:
        ok = False
        details.append(f"فشلت الكتابة بمجلد المشروع: {e}")
        details.append("الحل: تأكد صلاحيات الكتابة على المجلد (chmod / Termux storage permission)")

    # كتابة/قراءة بالمجلد المؤقت للنظام (tempfile)
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False, encoding="utf-8") as tf:
            tf.write("temp-test")
            tmp_path = tf.name
        with open(tmp_path, encoding="utf-8") as f:
            content = f.read()
        os.remove(tmp_path)
        if content == "temp-test":
            details.append("الكتابة/القراءة بالمجلد المؤقت (tempfile): نجحت")
        else:
            ok = False
            details.append("محتوى الملف المؤقت غير متطابق بعد القراءة")
    except Exception as e:
        ok = False
        details.append(f"فشلت الكتابة بالمجلد المؤقت: {e}")

    record("Filesystem", "PASS" if ok else "FAIL", details)


# ── 8) Excel creation via openpyxl ───────────────────────────────────────────
def check_excel():
    details = []
    try:
        from openpyxl import Workbook, load_workbook
    except ImportError as e:
        details.append(f"openpyxl غير مثبتة: {e}")
        record("Excel", "FAIL", details)
        return

    try:
        wb = Workbook()
        ws = wb.active
        ws.title = "اختبار"
        ws.append(["عمود1", "عمود2"])
        ws.append([1, "قيمة تجريبية"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        wb2 = load_workbook(buf)
        ws2 = wb2["اختبار"]
        rows = list(ws2.iter_rows(values_only=True))
        if rows == [("عمود1", "عمود2"), (1, "قيمة تجريبية")]:
            details.append("إنشاء وحفظ وقراءة ملف Excel تجريبي: نجح بالكامل")
            record("Excel", "PASS", details)
        else:
            details.append(f"البيانات المقروءة لا تطابق المتوقع: {rows}")
            record("Excel", "FAIL", details)
    except Exception as e:
        details.append(f"فشل إنشاء/قراءة ملف Excel: {e}")
        record("Excel", "FAIL", details)


# ── التشغيل والتقرير ──────────────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("🔍 فحص بيئة التشغيل — Salesbot")
    print("=" * 50)
    print()

    check_python()
    check_environment()
    check_requirements()
    check_imports()
    check_database()
    check_dotenv()
    check_filesystem()
    check_excel()

    print()
    has_fail = False
    has_warn = False
    for name, status, details in RESULTS:
        print_line(name, status)
        if status == "FAIL":
            has_fail = True
        elif status == "WARN":
            has_warn = True

    print()
    if has_fail:
        overall = "NOT READY ❌"
    elif has_warn:
        overall = "READY (مع تحذيرات) ⚠️"
    else:
        overall = "READY ✅"
    print(f"Overall: {overall}")
    print()

    # تفاصيل أي فحص ما نجح بالكامل
    problems = [(n, s, d) for n, s, d in RESULTS if s in ("FAIL", "WARN", "SKIP")]
    if problems:
        print("-" * 50)
        print("تفاصيل النقاط اللي تحتاج انتباه:")
        print("-" * 50)
        for name, status, details in problems:
            print(f"\n[{status}] {name}:")
            for d in details:
                print(f"    {d}")
        print()

    sys.exit(1 if has_fail else 0)


if __name__ == "__main__":
    main()
