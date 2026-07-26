import psycopg
import os
import json
from collections import Counter
from datetime import datetime, date, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL", "")


class _DictCursorRow(dict):
    """صف نتائج يحافظ على نفس سلوك psycopg2.extras.DictCursor بالضبط:
    يدعم الوصول بالاسم row["col"] وبالفهرس الرقمي row[0] معاً، بالإضافة
    إلى dict(row) و row.get(...). موجود فقط عشان التحويل إلى psycopg3
    ما يغيّر أي سطر آخر بالملف يعتمد على هذا السلوك."""

    __slots__ = ("_values",)

    def __init__(self, keys, values):
        super().__init__(zip(keys, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _dict_cursor_row_factory(cursor):
    keys = [col.name for col in (cursor.description or ())]

    def make_row(values):
        return _DictCursorRow(keys, values)

    return make_row


import threading
from psycopg_pool import ConnectionPool

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=5,
                    kwargs={
                        "row_factory": _dict_cursor_row_factory,
                        "connect_timeout": 10,
                    },
                    open=True,
                )
    return _pool


class _PooledConnection:
    """يغلّف اتصال مأخوذ من الـpool ويجعل conn.close() يرجّعه للـpool
    (putconn) بدل ما يقفله فعليًا. هذا يحل مشكلة التأخير مع Neon: بدل ما
    نفتح اتصال جديد (TCP + TLS + مصادقة كاملة عبر الشبكة) مع كل استعلام،
    الـpool يبقي اتصالات دافئة مفتوحة ويعيد استخدامها. كل بقية الملف
    يستمر يستدعي conn.cursor()/conn.commit()/conn.close() بنفس الشكل
    بدون أي تغيير آخر."""

    def __init__(self, real_conn, pool):
        object.__setattr__(self, "_real", real_conn)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_returned", False)

    def close(self):
        if not self._returned:
            object.__setattr__(self, "_returned", True)
            self._pool.putconn(self._real)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def get_connection():
    pool = _get_pool()
    real_conn = pool.getconn()
    return _PooledConnection(real_conn, pool)


def init_db():
    conn = get_connection()
    try:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                name TEXT,
                username TEXT,
                company_id INTEGER,
                role TEXT DEFAULT 'rep',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS subscription_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                manager_user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL,
                plan TEXT DEFAULT 'trial',
                status TEXT DEFAULT 'active',
                start_date TEXT,
                end_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS activation_codes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                plan TEXT DEFAULT 'monthly',
                duration_days INTEGER DEFAULT 30,
                used INTEGER DEFAULT 0,
                used_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL,
                rep_name_memory TEXT,
                manager_telegram_id BIGINT,
                notify_manager INTEGER DEFAULT 0,
                daily_summary INTEGER DEFAULT 0,
                weekly_report INTEGER DEFAULT 0,
                alert_inactivity INTEGER DEFAULT 1,
                alert_unvisited INTEGER DEFAULT 1,
                has_seen_help INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        for col_name, col_def in [
            ("alert_inactivity", "INTEGER DEFAULT 1"),
            ("alert_unvisited", "INTEGER DEFAULT 1"),
            ("has_seen_help", "INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
            except Exception:
                conn.rollback()

        c.execute("""
            CREATE TABLE IF NOT EXISTS product_catalog (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                drug_name TEXT NOT NULL,
                price REAL DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS user_templates (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL,
                field_order TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                rep_name TEXT,
                order_date TEXT,
                month INTEGER,
                year INTEGER,
                area TEXT,
                pharmacy TEXT,
                item_name TEXT,
                quantity INTEGER,
                bonus TEXT,
                price REAL,
                doctor_name TEXT,
                payment_type TEXT,
                notes TEXT,
                location_link TEXT,
                raw_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS drug_targets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                drug_name TEXT NOT NULL,
                target_units INTEGER DEFAULT 0,
                period TEXT NOT NULL,
                UNIQUE(user_id, drug_name, period),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)


        # Migration: change bonus column from INTEGER to TEXT if needed
        try:
            c.execute("ALTER TABLE orders ALTER COLUMN bonus TYPE TEXT USING bonus::TEXT")
            conn.commit()
        except Exception:
            conn.rollback()

        # Migration: add registration columns to users table
        for col_name, col_def in [
            ("full_name",     "TEXT"),
            ("password_hash", "TEXT"),
            ("is_registered", "INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
            except Exception:
                conn.rollback()

        # ── Doctor profile tables ─────────────────────────────────────────────
        init_doctor_tables(conn, c)

        # ── Competitor drug library ───────────────────────────────────────────
        init_competitor_drug_tables(conn, c)

        # ── Payments (تسديد) ──────────────────────────────────────────────────
        init_payment_tables(conn, c)

        conn.commit()
        conn.close()
    finally:
        conn.close()


# ─── Users ────────────────────────────────────────────────────────────────────

# ── إعدادات البوت الديناميكية (bot_config) ────────────────────────────────────
# تُستخدم لتخزين إعدادات تتغيّر من لوحة الأدمن مباشرة (مفتاح AI، الموديل...)
# بدون حاجة نعيد تشغيل البوت أو نلمس Config Vars بالسيرفر.

def get_bot_config(key: str, default: str = None) -> str:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT value FROM bot_config WHERE key=%s", (key,))
        row = c.fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_bot_config(key: str, value: str):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO bot_config (key, value, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP""",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_or_create_user(telegram_id: int, name: str = None, username: str = None) -> int:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE telegram_id = %s", (telegram_id,))
        row = c.fetchone()
        if row:
            user_id = row["id"]
            c.execute("UPDATE users SET name=%s, username=%s WHERE id=%s", (name, username, user_id))
        else:
            c.execute(
                "INSERT INTO users (telegram_id, name, username) VALUES (%s, %s, %s) RETURNING id",
                (telegram_id, name, username),
            )
            user_id = c.fetchone()[0]
            start = date.today().isoformat()
            end = (date.today() + timedelta(days=30)).isoformat()
            c.execute(
                "INSERT INTO subscriptions (user_id, plan, status, start_date, end_date) VALUES (%s, 'basic', 'active', %s, %s)",
                (user_id, start, end),
            )
            c.execute("INSERT INTO user_settings (user_id) VALUES (%s)", (user_id,))
        conn.commit()
        conn.close()
        return user_id
    finally:
        conn.close()


def register_user(telegram_id: int, full_name: str, password_hash: str) -> bool:
    """Save full_name and password for the user, mark as registered."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET full_name=%s, password_hash=%s, is_registered=1 WHERE telegram_id=%s",
            (full_name.strip(), password_hash, telegram_id),
        )
        updated = c.rowcount > 0
        conn.commit()
        conn.close()
        return updated
    finally:
        conn.close()


def is_user_registered(telegram_id: int) -> bool:
    """Return True if user completed registration (has full_name + password)."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT is_registered FROM users WHERE telegram_id=%s",
            (telegram_id,),
        )
        row = c.fetchone()
        conn.close()
        return bool(row and row["is_registered"])
    finally:
        conn.close()


def get_user_full_name(user_id: int):
    """Return the registered full_name (الاسم الثلاثي) for the user, or None."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT full_name FROM users WHERE id=%s", (user_id,))
        row = c.fetchone()
        conn.close()
        return row["full_name"] if row and row.get("full_name") else None
    finally:
        conn.close()


def get_all_active_users() -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT u.id, u.telegram_id, u.name
            FROM users u
            JOIN subscriptions s ON s.user_id = u.id
            WHERE s.status = 'active'
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


# ─── Settings ─────────────────────────────────────────────────────────────────

def get_user_settings(user_id: int) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM user_settings WHERE user_id = %s", (user_id,))
        row = c.fetchone()
        if not row:
            c.execute("INSERT INTO user_settings (user_id) VALUES (%s)", (user_id,))
            conn.commit()
            c.execute("SELECT * FROM user_settings WHERE user_id = %s", (user_id,))
            row = c.fetchone()
        conn.close()
        return dict(row) if row else {}
    finally:
        conn.close()


def set_rep_name_memory(user_id: int, rep_name: str):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO user_settings (user_id, rep_name_memory)
            VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET rep_name_memory = EXCLUDED.rep_name_memory
        """, (user_id, rep_name))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def toggle_setting(user_id: int, field: str) -> bool:
    allowed = {"alert_inactivity", "alert_unvisited", "has_seen_help",
               "daily_summary", "weekly_report", "notify_manager"}
    if field not in allowed:
        return False
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(f"SELECT {field} FROM user_settings WHERE user_id = %s", (user_id,))
        row = c.fetchone()
        current = row[field] if row else 0
        new_val = 0 if current else 1
        c.execute(f"""
            INSERT INTO user_settings (user_id, {field}) VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET {field} = EXCLUDED.{field}
        """, (user_id, new_val))
        conn.commit()
        conn.close()
        return bool(new_val)
    finally:
        conn.close()


def set_has_seen_help(user_id: int, seen: bool = True):
    """Mark whether the user has already seen the help message on first /start."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO user_settings (user_id, has_seen_help) VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET has_seen_help = EXCLUDED.has_seen_help
        """, (user_id, 1 if seen else 0))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def set_manager(user_id: int, manager_telegram_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO user_settings (user_id, manager_telegram_id, notify_manager)
            VALUES (%s, %s, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                manager_telegram_id = EXCLUDED.manager_telegram_id,
                notify_manager = 1
        """, (user_id, manager_telegram_id))
        conn.commit()
        conn.close()
    finally:
        conn.close()


# ─── Subscriptions ────────────────────────────────────────────────────────────

def get_subscription(user_id: int) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM subscriptions WHERE user_id = %s", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        conn.close()


def activate_code(user_id: int, code: str) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM activation_codes WHERE code = %s", (code.upper(),))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "reason": "invalid"}
        if row["used"]:
            conn.close()
            return {"success": False, "reason": "used"}
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=row["duration_days"])).isoformat()
        c.execute(
            "UPDATE activation_codes SET used=1, used_by=%s, used_at=%s WHERE id=%s",
            (user_id, datetime.now().isoformat(), row["id"]),
        )
        c.execute("""
            INSERT INTO subscriptions (user_id, plan, status, start_date, end_date)
            VALUES (%s, %s, 'active', %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET plan=EXCLUDED.plan, status='active',
            start_date=EXCLUDED.start_date, end_date=EXCLUDED.end_date
        """, (user_id, row["plan"], start, end))
        conn.commit()
        conn.close()
        return {"success": True, "plan": row["plan"], "end_date": end}
    finally:
        conn.close()


def create_activation_code(code: str, plan: str = "monthly", days: int = 30):
    conn = get_connection()
    try:
        c = conn.cursor()
        try:
            c.execute(
                "INSERT INTO activation_codes (code, plan, duration_days) VALUES (%s, %s, %s)",
                (code.upper(), plan, days),
            )
            conn.commit()
            success = True
        except Exception:
            conn.rollback()
            success = False
        conn.close()
        return success
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        uname = username.lstrip("@")
        c.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(%s)", (uname,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_users_admin() -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT u.id, u.telegram_id, u.name, u.username, u.created_at,
                   s.plan, s.status, s.end_date
            FROM users u
            LEFT JOIN subscriptions s ON s.user_id = u.id
            ORDER BY u.created_at DESC
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def admin_set_subscription(user_id: int, plan: str, days: int):
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days)).isoformat()
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO subscriptions (user_id, plan, status, start_date, end_date)
            VALUES (%s, %s, 'active', %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET plan=EXCLUDED.plan, status='active',
            start_date=EXCLUDED.start_date, end_date=EXCLUDED.end_date
        """, (user_id, plan, start, end))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def admin_ban_user(user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("UPDATE subscriptions SET status='banned' WHERE user_id=%s", (user_id,))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def admin_unban_user(user_id: int):
    """يرجّع حالة المندوب بعد فك الحظر — active لو اشتراكه لسه ساري، وإلا expired
    (ما نرجّعه active دائماً حتى ما نعطيه صلاحيات اشتراك منتهي أصلاً)."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT end_date FROM subscriptions WHERE user_id=%s", (user_id,))
        row = c.fetchone()
        end_date = row["end_date"] if row else None
        new_status = "expired"
        if end_date:
            try:
                if date.fromisoformat(str(end_date)) >= date.today():
                    new_status = "active"
            except ValueError:
                pass
        c.execute("UPDATE subscriptions SET status=%s WHERE user_id=%s", (new_status, user_id))
        conn.commit()
        return new_status
    finally:
        conn.close()


def get_stats_admin() -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as total FROM users")
        total_users = c.fetchone()["total"]
        c.execute("SELECT COUNT(*) as total FROM subscriptions WHERE status='active' AND plan IN ('pro','monthly','yearly','lifetime')")
        paid = c.fetchone()["total"]
        c.execute("SELECT COUNT(*) as total FROM subscriptions WHERE status='active' AND plan IN ('basic','trial')")
        basic = c.fetchone()["total"]
        c.execute("SELECT COUNT(*) as total FROM orders")
        total_orders = c.fetchone()["total"]
        c.execute("SELECT COUNT(*) as total FROM subscription_requests WHERE status='pending'")
        pending_requests = c.fetchone()["total"]
        conn.close()
        return {
            "total_users": total_users, "paid": paid, "basic": basic,
            "total_orders": total_orders, "pending_requests": pending_requests,
        }
    finally:
        conn.close()


def get_today_admin_stats() -> dict:
    """إحصائيات اليوم عبر كل المناديب — تُستخدم بالشاشة الرئيسية الموحّدة للوحة الأدمن."""
    conn = get_connection()
    try:
        c = conn.cursor()
        today = date.today().isoformat()
        c.execute("SELECT COUNT(*) AS c FROM orders WHERE order_date=%s", (today,))
        orders_today = c.fetchone()["c"]
        c.execute("SELECT COUNT(DISTINCT user_id) AS c FROM orders WHERE order_date=%s", (today,))
        active_reps_today = c.fetchone()["c"]
        return {"orders_today": orders_today, "active_reps_today": active_reps_today}
    finally:
        conn.close()


def create_subscription_request(user_id: int, plan_type: str) -> int:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO subscription_requests (user_id, plan_type) VALUES (%s, %s) RETURNING id",
            (user_id, plan_type),
        )
        rid = c.fetchone()[0]
        conn.commit()
        conn.close()
        return rid
    finally:
        conn.close()


def get_pending_requests() -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT sr.id, sr.user_id, sr.plan_type, sr.created_at,
                   u.name, u.username, u.telegram_id
            FROM subscription_requests sr
            JOIN users u ON u.id = sr.user_id
            WHERE sr.status = 'pending'
            ORDER BY sr.created_at DESC
        """)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def close_subscription_request(user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE subscription_requests SET status='done' WHERE user_id=%s AND status='pending'",
            (user_id,),
        )
        conn.commit()
        conn.close()
    finally:
        conn.close()


def get_orders_for_admin_excel(user_id: int, month: int, year: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id=%s AND month=%s AND year=%s ORDER BY order_date ASC",
            (user_id, month, year),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


# ─── Templates ────────────────────────────────────────────────────────────────

def save_user_template(user_id: int, field_order: list):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO user_templates (user_id, field_order, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET field_order=EXCLUDED.field_order, updated_at=EXCLUDED.updated_at
        """, (user_id, json.dumps(field_order, ensure_ascii=False), datetime.now().isoformat()))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def get_user_template(user_id: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT field_order FROM user_templates WHERE user_id = %s", (user_id,))
        row = c.fetchone()
        conn.close()
        return json.loads(row["field_order"]) if row else None
    finally:
        conn.close()


def delete_user_template(user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM user_templates WHERE user_id = %s", (user_id,))
        conn.commit()
        conn.close()
    finally:
        conn.close()


# ─── Orders ───────────────────────────────────────────────────────────────────

def save_order(user_id: int, data: dict) -> int:
    from datetime import date as _date

    data = dict(data)

    # اشتق month و year من order_date دائماً — يمنع اختفاء الطلبات من التقارير
    od = data.get("order_date")
    if od:
        try:
            d = _date.fromisoformat(str(od)[:10])
            data["month"] = d.month
            data["year"]  = d.year
        except Exception:
            pass
    if not data.get("month") or not data.get("year"):
        today = _date.today()
        data.setdefault("month", today.month)
        data.setdefault("year",  today.year)
    if not data.get("order_date"):
        data["order_date"] = _date.today().isoformat()

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO orders
                (user_id, rep_name, order_date, month, year, area, pharmacy,
                 item_name, quantity, bonus, price, doctor_name, payment_type, notes, location_link, raw_text)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                user_id, data.get("rep_name"), data.get("order_date"),
                data.get("month"), data.get("year"), data.get("area"),
                data.get("pharmacy"), data.get("item_name"), data.get("quantity"),
                data.get("bonus"), data.get("price"), data.get("doctor_name"),
                data.get("payment_type"), data.get("notes"), data.get("location_link"),
                data.get("raw_text"),
            ),
        )
        order_id = c.fetchone()[0]
        conn.commit()
        conn.close()
        return order_id
    finally:
        conn.close()


def bulk_save_orders(user_id: int, orders: list) -> int:
    """يحفظ قائمة طلبات دفعة واحدة. يعيد عدد الطلبات المحفوظة."""
    if not orders:
        return 0
    conn = get_connection()
    try:
        c = conn.cursor()
        saved = 0
        for data in orders:
            try:
                od = data.get("order_date")
                month = data.get("month")
                year  = data.get("year")
                if od:
                    try:
                        d = datetime.strptime(str(od)[:10], "%Y-%m-%d").date()
                        month = d.month
                        year  = d.year
                    except Exception:
                        pass
                if not month or not year:
                    today = date.today()
                    month = month or today.month
                    year  = year  or today.year
                c.execute(
                    """INSERT INTO orders
                        (user_id, rep_name, order_date, month, year, area, pharmacy,
                         item_name, quantity, bonus, price, notes, raw_text)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        user_id, data.get("rep_name"), od,
                        month, year, data.get("area"),
                        data.get("pharmacy"), data.get("item_name"),
                        data.get("quantity"), data.get("bonus"),
                        data.get("price"), data.get("notes"),
                        data.get("raw_text"),
                    ),
                )
                saved += 1
            except Exception:
                pass
        conn.commit()
        conn.close()
        return saved
    finally:
        conn.close()


def get_last_order(user_id: int) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        conn.close()


def get_order_by_id(order_id: int) -> dict:
    """يجيب طلب واحد بواسطة id مباشرةً."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        conn.close()


def update_order_field(order_id: int, field: str, value):
    allowed = {"rep_name", "order_date", "area", "pharmacy", "item_name",
               "quantity", "bonus", "price", "payment_type", "notes"}
    if field not in allowed:
        return False
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE orders SET {field} = %s WHERE id = %s", (value, order_id))
        conn.commit()
        conn.close()
        return True
    finally:
        conn.close()


def delete_order(order_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM orders WHERE id = %s", (order_id,))
        conn.commit()
        conn.close()
    finally:
        conn.close()


# ── ملف الصيدلية ─────────────────────────────────────────────────────────────

def get_pharmacy_profile(user_id: int, pharmacy_name: str) -> dict | None:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id = %s AND pharmacy ILIKE %s ORDER BY order_date DESC",
            (user_id, f"%{pharmacy_name}%"),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        if not rows:
            return None

        total = len(rows)
        products = Counter(r["item_name"] for r in rows if r["item_name"])
        top_product = products.most_common(1)[0][0] if products else "—"
        quantities = [r["quantity"] for r in rows if r["quantity"]]
        avg_qty = round(sum(quantities) / len(quantities)) if quantities else 0
        last_3 = rows[:3]
        return {
            "pharmacy": rows[0]["pharmacy"],
            "total_orders": total,
            "last_visit": rows[0]["order_date"],
            "top_product": top_product,
            "avg_quantity": avg_qty,
            "last_3_visits": last_3,
            "all_products": products.most_common(5),
        }
    finally:
        conn.close()


# ── بحث ذكي ──────────────────────────────────────────────────────────────────

def _normalize_arabic(text: str) -> str:
    """تطبيع الهمزات واليا للبحث — يعوّض عن اختلافات الكتابة."""
    import re as _re
    text = _re.sub(r'[أإآا]', 'ا', text)
    text = _re.sub(r'[ىي]', 'ي', text)
    text = text.replace('ة', 'ه')
    return text


def search_orders(user_id: int, product: str = None, pharmacy: str = None,
                  area: str = None, keyword: str = None,
                  month: int = None, year: int = None) -> list:
    """بحث ذكي يبحث بأي كلمة في كل حقول الطلب مع تطبيع الهمزات."""
    conn = get_connection()
    try:
        c = conn.cursor()

        if keyword:
            # ابحث بالكلمة الأصلية + الكلمة المطبّعة لتغطية اختلافات الهمزة والألف
            kw_norm = _normalize_arabic(keyword)
            patterns = list({f"%{keyword}%", f"%{kw_norm}%"})  # مجموعة لتجنب التكرار

            # بناء شروط الـ OR لكل حقل × كل نمط
            field_cols = ["rep_name", "pharmacy", "area", "item_name",
                          "doctor_name", "notes", "raw_text"]
            conditions = []
            params = [user_id]
            for col in field_cols:
                for pat in patterns:
                    conditions.append(f"{col} ILIKE %s")
                    params.append(pat)
            # quantity كنص
            for pat in patterns:
                conditions.append(f"CAST(quantity AS TEXT) ILIKE %s")
                params.append(pat)

            query = (
                f"SELECT * FROM orders WHERE user_id = %s AND ({' OR '.join(conditions)}) "
                f"ORDER BY order_date DESC"
            )
        else:
            query = "SELECT * FROM orders WHERE user_id = %s"
            params = [user_id]
            if product:
                query += " AND item_name ILIKE %s"
                params.append(f"%{product}%")
            if pharmacy:
                kw_norm = _normalize_arabic(pharmacy)
                query += " AND (pharmacy ILIKE %s OR pharmacy ILIKE %s)"
                params.extend([f"%{pharmacy}%", f"%{kw_norm}%"])
            if area:
                kw_norm = _normalize_arabic(area)
                query += " AND (area ILIKE %s OR area ILIKE %s)"
                params.extend([f"%{area}%", f"%{kw_norm}%"])
            if month:
                query += " AND month = %s"
                params.append(month)
            if year:
                query += " AND year = %s"
                params.append(year)
            query += " ORDER BY order_date DESC"

        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


# ── أداء ومقارنة ──────────────────────────────────────────────────────────────

def get_performance_stats(user_id: int) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        today = date.today()
        curr_m, curr_y = today.month, today.year
        prev_m = curr_m - 1 if curr_m > 1 else 12
        prev_y = curr_y if curr_m > 1 else curr_y - 1

        c.execute(
            "SELECT COUNT(*), COALESCE(SUM(quantity),0) FROM orders WHERE user_id=%s AND month=%s AND year=%s",
            (user_id, curr_m, curr_y),
        )
        curr = c.fetchone()
        c.execute(
            "SELECT COUNT(*), COALESCE(SUM(quantity),0) FROM orders WHERE user_id=%s AND month=%s AND year=%s",
            (user_id, prev_m, prev_y),
        )
        prev = c.fetchone()

        c.execute(
            "SELECT DISTINCT pharmacy FROM orders WHERE user_id=%s AND month=%s AND year=%s",
            (user_id, curr_m, curr_y),
        )
        pharmacies = c.fetchall()
        c.execute(
            "SELECT item_name, COUNT(*) as cnt FROM orders WHERE user_id=%s AND month=%s AND year=%s "
            "AND item_name IS NOT NULL GROUP BY item_name ORDER BY cnt DESC LIMIT 1",
            (user_id, curr_m, curr_y),
        )
        top = c.fetchone()
        conn.close()

        curr_orders = curr[0] or 0
        curr_qty = curr[1] or 0
        prev_orders = prev[0] or 0
        prev_qty = prev[1] or 0
        change = round(((curr_orders - prev_orders) / prev_orders * 100)) if prev_orders else 0
        return {
            "curr_orders": curr_orders,
            "curr_qty": curr_qty,
            "prev_orders": prev_orders,
            "prev_qty": prev_qty,
            "change_pct": change,
            "pharmacies_count": len(pharmacies),
            "top_product": top["item_name"] if top else "—",
            "curr_month": curr_m,
            "prev_month": prev_m,
        }
    finally:
        conn.close()


def get_rep_ranking(manager_telegram_id: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        today = date.today()
        c.execute(
            "SELECT user_id FROM user_settings WHERE manager_telegram_id=%s",
            (manager_telegram_id,),
        )
        user_ids = [r["user_id"] for r in c.fetchall()]
        if not user_ids:
            conn.close()
            return []
        ranking = []
        for uid in user_ids:
            c.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(o.quantity),0) as qty, u.name FROM orders o "
                "JOIN users u ON o.user_id=u.id "
                "WHERE o.user_id=%s AND o.month=%s AND o.year=%s GROUP BY u.name",
                (uid, today.month, today.year),
            )
            row = c.fetchone()
            if row:
                ranking.append({
                    "user_id": uid, "name": row["name"] or "مجهول",
                    "orders": row["cnt"] or 0, "qty": row["qty"] or 0,
                })
            else:
                ranking.append({"user_id": uid, "name": "مجهول", "orders": 0, "qty": 0})
        ranking.sort(key=lambda x: x["orders"], reverse=True)
        conn.close()
        return ranking
    finally:
        conn.close()


# ── تنبيهات ذكية ──────────────────────────────────────────────────────────────

def get_inactive_pharmacies(user_id: int, days: int = 7) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        c.execute(
            "SELECT DISTINCT pharmacy FROM orders WHERE user_id=%s AND pharmacy IS NOT NULL",
            (user_id,),
        )
        all_ph = [r["pharmacy"] for r in c.fetchall()]
        inactive = []
        for ph in all_ph:
            c.execute(
                "SELECT MAX(order_date) FROM orders WHERE user_id=%s AND pharmacy=%s",
                (user_id, ph),
            )
            row = c.fetchone()
            last = row[0] if row else None
            if last and str(last) < cutoff:
                days_ago = (date.today() - date.fromisoformat(str(last))).days
                inactive.append({"pharmacy": ph, "last_visit": str(last), "days_ago": days_ago})
        inactive.sort(key=lambda x: x["days_ago"], reverse=True)
        conn.close()
        return inactive[:5]
    finally:
        conn.close()


def get_hours_since_last_order(user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT created_at FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        try:
            last_val = row[0]
            if isinstance(last_val, datetime):
                last = last_val
            else:
                last = datetime.fromisoformat(str(last_val))
            return (datetime.now() - last).total_seconds() / 3600
        except Exception:
            return None
    finally:
        conn.close()


# ── Fuzzy helpers ─────────────────────────────────────────────────────────────

def get_known_products(user_id: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT DISTINCT item_name FROM orders WHERE user_id=%s AND item_name IS NOT NULL",
            (user_id,),
        )
        rows = [r["item_name"] for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def get_known_pharmacies(user_id: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT DISTINCT pharmacy FROM orders WHERE user_id=%s AND pharmacy IS NOT NULL",
            (user_id,),
        )
        rows = [r["pharmacy"] for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


# ── تكرار الأوردر ─────────────────────────────────────────────────────────────


def get_last_n_orders(user_id: int, n: int = 5) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC LIMIT %s",
            (user_id, n),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


# ── لوحة المدير ───────────────────────────────────────────────────────────────

def get_team_stats(manager_telegram_id: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        today = date.today()
        c.execute(
            "SELECT user_id FROM user_settings WHERE manager_telegram_id=%s",
            (manager_telegram_id,),
        )
        user_ids = [r["user_id"] for r in c.fetchall()]
        if not user_ids:
            conn.close()
            return []
        result = []
        for uid in user_ids:
            c.execute(
                "SELECT COUNT(*) as cnt, COALESCE(SUM(o.quantity),0) as qty, u.name, u.telegram_id "
                "FROM orders o JOIN users u ON o.user_id=u.id "
                "WHERE o.user_id=%s AND o.month=%s AND o.year=%s GROUP BY u.name, u.telegram_id",
                (uid, today.month, today.year),
            )
            row = c.fetchone()
            c.execute(
                "SELECT MAX(order_date) FROM orders WHERE user_id=%s", (uid,)
            )
            last_row = c.fetchone()
            last = last_row[0] if last_row and last_row[0] else "—"
            if row:
                result.append({
                    "user_id": uid,
                    "name": row["name"] or "مجهول",
                    "telegram_id": row["telegram_id"],
                    "orders": row["cnt"] or 0,
                    "qty": row["qty"] or 0,
                    "last_order": str(last),
                })
            else:
                result.append({
                    "user_id": uid, "name": "مجهول", "telegram_id": None,
                    "orders": 0, "qty": 0, "last_order": str(last),
                })
        result.sort(key=lambda x: x["orders"], reverse=True)
        conn.close()
        return result
    finally:
        conn.close()


def is_duplicate_order(user_id: int, pharmacy: str, item_name: str, order_date: str) -> bool:
    if not pharmacy or not item_name or not order_date:
        return False
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id FROM orders
            WHERE user_id = %s AND pharmacy = %s AND item_name = %s AND order_date = %s
            ORDER BY id DESC LIMIT 1
        """, (user_id, pharmacy, item_name, order_date))
        row = c.fetchone()
        conn.close()
        return row is not None
    finally:
        conn.close()


def fetch_orders(user_id: int, filters: dict = None) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        query = "SELECT * FROM orders WHERE user_id = %s"
        params = [user_id]
        if filters:
            if filters.get("month"):
                query += " AND month = %s"; params.append(filters["month"])
            if filters.get("year"):
                query += " AND year = %s"; params.append(filters["year"])
            if filters.get("date_from"):
                query += " AND order_date >= %s"; params.append(filters["date_from"])
            if filters.get("date_to"):
                query += " AND order_date <= %s"; params.append(filters["date_to"])
        query += " ORDER BY order_date DESC"
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def fetch_orders_today(user_id: int) -> list:
    today = date.today().isoformat()
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id = %s AND created_at::date = %s",
            (user_id, today),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def delete_all_orders(user_id: int) -> int:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM orders WHERE user_id = %s", (user_id,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return deleted
    finally:
        conn.close()


def delete_orders_by_month(user_id: int, month: int, year: int) -> int:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "DELETE FROM orders WHERE user_id = %s AND month = %s AND year = %s",
            (user_id, month, year),
        )
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return deleted
    finally:
        conn.close()


# ── كتالوج الأدوية ────────────────────────────────────────────────────────────

def get_product_catalog(user_id: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT id, drug_name, price, sort_order FROM product_catalog "
            "WHERE user_id=%s ORDER BY sort_order ASC, id ASC",
            (user_id,)
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def add_product_to_catalog(user_id: int, drug_name: str, price: float) -> int:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM product_catalog WHERE user_id=%s",
            (user_id,)
        )
        next_order = c.fetchone()[0]
        c.execute(
            "INSERT INTO product_catalog (user_id, drug_name, price, sort_order) VALUES (%s,%s,%s,%s) RETURNING id",
            (user_id, drug_name.strip(), price, next_order)
        )
        rid = c.fetchone()[0]
        conn.commit()
        conn.close()
        return rid
    finally:
        conn.close()


def update_product_price(product_id: int, user_id: int, price: float):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE product_catalog SET price=%s WHERE id=%s AND user_id=%s",
            (price, product_id, user_id)
        )
        conn.commit()
        conn.close()
    finally:
        conn.close()


def delete_product_from_catalog(product_id: int, user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM product_catalog WHERE id=%s AND user_id=%s", (product_id, user_id))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def clear_product_catalog(user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM product_catalog WHERE user_id=%s", (user_id,))
        conn.commit()
        conn.close()
    finally:
        conn.close()


# ── التاركت الشهري ─────────────────────────────────────────────────────────────

def set_drug_target(user_id: int, drug_name: str, target_units: int, period: str):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO drug_targets (user_id, drug_name, target_units, period)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id, drug_name, period) DO UPDATE SET target_units=EXCLUDED.target_units
        """, (user_id, drug_name.strip(), int(target_units), period))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def get_drug_targets(user_id: int, period: str) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT drug_name, target_units FROM drug_targets WHERE user_id=%s AND period=%s",
            (user_id, period)
        )
        result = {r["drug_name"]: r["target_units"] for r in c.fetchall()}
        conn.close()
        return result
    finally:
        conn.close()


def get_all_users_admin_stats(month: int, year: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT u.id, u.name, u.telegram_id,
                   (SELECT COUNT(*) FROM orders o
                    WHERE o.user_id=u.id AND o.month=%s AND o.year=%s) AS order_count,
                   (SELECT COALESCE(SUM(o.quantity), 0) FROM orders o
                    WHERE o.user_id=u.id AND o.month=%s AND o.year=%s) AS total_qty,
                   (SELECT COALESCE(SUM(o.quantity * COALESCE(o.price, pc.price, 0)), 0)
                    FROM orders o
                    LEFT JOIN product_catalog pc
                           ON pc.user_id = o.user_id AND LOWER(pc.drug_name) = LOWER(o.item_name)
                    WHERE o.user_id=u.id AND o.month=%s AND o.year=%s) AS total_sales,
                   s.plan, s.status
            FROM users u
            LEFT JOIN subscriptions s ON s.id=(
                SELECT MAX(id) FROM subscriptions WHERE user_id=u.id
            )
            ORDER BY order_count DESC
        """, (month, year, month, year, month, year))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def get_rep_achievement(user_id: int, month: int, year: int) -> float:
    """Return overall achievement % for a rep in a given month (0-100)."""
    period = f"{year}-{month:02d}"
    catalog = get_product_catalog(user_id)
    if not catalog:
        return 0.0
    drug_list = [d["drug_name"] for d in catalog]
    targets = get_drug_targets(user_id, period)
    if not targets:
        return 0.0
    orders = fetch_orders(user_id, filters={"month": month, "year": year})
    total_tgt = sum(targets.get(d, 0) for d in drug_list)
    if total_tgt <= 0:
        return 0.0
    total_units = 0
    for o in orders:
        item = (o.get("item_name") or "").strip()
        if item in drug_list:
            total_units += o.get("quantity") or 0
    return round(total_units / total_tgt * 100, 1)


# ── Doctor Profiles & Competitor Entries ──────────────────────────────────────

def init_payment_tables(conn=None, c=None):
    """Create the payments table (تسديد) — called from init_db(). لا يمس جدول orders."""
    _close = False
    if conn is None:
        conn = get_connection()
        c = conn.cursor()
        _close = True

    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id             SERIAL PRIMARY KEY,
            user_id        INTEGER NOT NULL,
            rep_name       TEXT,
            pharmacy_name  TEXT NOT NULL,
            amount         BIGINT NOT NULL,
            payment_date   DATE NOT NULL DEFAULT CURRENT_DATE,
            payment_method TEXT DEFAULT 'cash',
            notes          TEXT,
            created_at     TIMESTAMPTZ DEFAULT NOW(),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_user_pharmacy
        ON payments(user_id, LOWER(pharmacy_name))
    """)

    if _close:
        conn.commit()
        conn.close()


# ─── Payments (تسديد) ───────────────────────────────────────────────────────

def save_payment(user_id: int, rep_name: str, pharmacy_name: str, amount: int,
                  payment_date: str = None, payment_method: str = "cash",
                  notes: str = None) -> int:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO payments
               (user_id, rep_name, pharmacy_name, amount, payment_date, payment_method, notes)
               VALUES (%s, %s, %s, %s, COALESCE(%s, CURRENT_DATE), %s, %s)
               RETURNING id""",
            (user_id, rep_name, pharmacy_name.strip(), amount,
             payment_date, payment_method, notes)
        )
        rid = c.fetchone()[0]
        conn.commit()
        conn.close()
        return rid
    finally:
        conn.close()


def get_payment(payment_id: int) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM payments WHERE id=%s", (payment_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        conn.close()


def update_payment_field(payment_id: int, field: str, value):
    """يحدّث حقل واحد بالتسديد. field يجب أن يكون من القائمة المسموحة (whitelist)."""
    allowed = {"pharmacy_name", "amount", "payment_date", "payment_method", "notes"}
    if field not in allowed:
        raise ValueError(f"حقل غير مسموح: {field}")
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE payments SET {field}=%s WHERE id=%s", (value, payment_id))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def delete_payment(payment_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM payments WHERE id=%s", (payment_id,))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def get_payments_by_rep(user_id: int, filters: dict = None) -> list:
    """filters: {"date_from":..., "date_to":..., "pharmacy":...} — نفس نمط fetch_orders."""
    filters = filters or {}
    conn = get_connection()
    try:
        c = conn.cursor()
        query = "SELECT * FROM payments WHERE user_id=%s"
        params = [user_id]
        if filters.get("date_from"):
            query += " AND payment_date >= %s"
            params.append(filters["date_from"])
        if filters.get("date_to"):
            query += " AND payment_date <= %s"
            params.append(filters["date_to"])
        if filters.get("pharmacy"):
            query += " AND LOWER(pharmacy_name) = LOWER(%s)"
            params.append(filters["pharmacy"])
        query += " ORDER BY payment_date DESC, id DESC"
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def get_all_payments(filters: dict = None) -> list:
    """بدون فلتر user_id — للأدمن فقط."""
    filters = filters or {}
    conn = get_connection()
    try:
        c = conn.cursor()
        query = "SELECT * FROM payments WHERE 1=1"
        params = []
        if filters.get("date_from"):
            query += " AND payment_date >= %s"
            params.append(filters["date_from"])
        if filters.get("date_to"):
            query += " AND payment_date <= %s"
            params.append(filters["date_to"])
        query += " ORDER BY payment_date DESC, id DESC"
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def get_pharmacy_balance(user_id: int, pharmacy_name: str) -> dict:
    """
    يحسب رصيد صيدلية واحدة لمندوب واحد لحظياً (بدون تخزين).
    مصدر السعر: product_catalog (نفس مصدر reports.py) — مو orders.price لأنه غالباً فارغ.
    الدين يُحسب فقط من طلبات (آجل/كريدت). طلبات "كاش" لا تُحتسب دين لأنها مسددة وقت الطلب.
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT item_name, quantity FROM orders
               WHERE user_id=%s AND LOWER(pharmacy)=LOWER(%s)
                 AND payment_type IN ('آجل', 'كريدت')""",
            (user_id, pharmacy_name)
        )
        order_rows = c.fetchall()
        conn.close()
    finally:
        conn.close()

    catalog = get_product_catalog(user_id)
    price_map = {p["drug_name"]: float(p.get("price") or 0) for p in catalog}
    has_prices = any(price_map.values())

    total_sales = None
    if has_prices:
        total_sales = sum(
            (row["quantity"] or 0) * price_map.get(row["item_name"], 0)
            for row in order_rows
        )

    payments = get_payments_by_rep(user_id, {"pharmacy": pharmacy_name})
    total_payments = sum(p["amount"] for p in payments)

    return {
        "pharmacy_name": pharmacy_name,
        "total_sales": total_sales,          # None لو ما في أسعار بالكتالوج
        "total_payments": total_payments,
        "remaining": (total_sales - total_payments) if total_sales is not None else None,
    }


def get_rep_pharmacy_balances(user_id: int = None) -> list:
    """
    كل الصيدليات المدينة (بترتيب تنازلي حسب المتبقي).
    user_id=None → كل المندوبين (للأدمن). user_id محدد → صيدليات هذا المندوب فقط.
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        if user_id is not None:
            c.execute(
                """SELECT DISTINCT user_id, pharmacy AS pharmacy_name FROM orders
                   WHERE user_id=%s AND payment_type IN ('آجل', 'كريدت')""",
                (user_id,)
            )
        else:
            c.execute(
                """SELECT DISTINCT user_id, pharmacy AS pharmacy_name FROM orders
                   WHERE payment_type IN ('آجل', 'كريدت')"""
            )
        pairs = [(r["user_id"], r["pharmacy_name"]) for r in c.fetchall()]
        conn.close()
    finally:
        conn.close()

    results = []
    for uid, pharmacy in pairs:
        bal = get_pharmacy_balance(uid, pharmacy)
        bal["user_id"] = uid
        if bal["remaining"]:
            results.append(bal)
    results.sort(key=lambda b: b["remaining"] or 0, reverse=True)
    return results


def init_doctor_tables(conn=None, c=None):
    """Create doctor_profiles and competitor_entries tables (called from init_db)."""
    _close = False
    if conn is None:
        conn = get_connection()
        c = conn.cursor()
        _close = True

    c.execute("""
        CREATE TABLE IF NOT EXISTS doctor_profiles (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            specialty TEXT,
            clinic_name TEXT,
            area TEXT,
            phone TEXT,
            class TEXT DEFAULT 'C',
            working_days TEXT,
            linked_pharmacy TEXT,
            current_product TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Migration: add new columns to existing DBs (safe if already exist)
    for col_def in [
        "ALTER TABLE doctor_profiles ADD COLUMN IF NOT EXISTS working_days TEXT",
        "ALTER TABLE doctor_profiles ADD COLUMN IF NOT EXISTS linked_pharmacy TEXT",
        "ALTER TABLE doctor_profiles ADD COLUMN IF NOT EXISTS current_product TEXT",
    ]:
        try:
            c.execute(col_def)
        except Exception:
            pass
    # Migration: add unique index if not exists
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_doctor_user_name_area
        ON doctor_profiles(user_id, LOWER(name), LOWER(COALESCE(area,'')))
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS competitor_entries (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            doctor_profile_id INTEGER,
            competitor_name TEXT,
            competitor_product TEXT,
            notes TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (doctor_profile_id) REFERENCES doctor_profiles(id)
        )
    """)

    if _close:
        conn.commit()
        conn.close()


def upsert_doctor_profile(user_id: int, data: dict) -> int:
    """Insert or update a doctor profile. Returns the profile id."""
    conn = get_connection()
    try:
        c = conn.cursor()
        name = (data.get("name") or "").strip()
        area = (data.get("area") or "").strip() or None
        try:
            c.execute("""
                INSERT INTO doctor_profiles
                    (user_id, name, specialty, clinic_name, area, phone, class,
                     working_days, linked_pharmacy, current_product, notes, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                ON CONFLICT ON CONSTRAINT idx_doctor_user_name_area
                DO UPDATE SET
                    specialty       = COALESCE(EXCLUDED.specialty,       doctor_profiles.specialty),
                    clinic_name     = COALESCE(EXCLUDED.clinic_name,     doctor_profiles.clinic_name),
                    phone           = COALESCE(EXCLUDED.phone,           doctor_profiles.phone),
                    working_days    = COALESCE(EXCLUDED.working_days,    doctor_profiles.working_days),
                    linked_pharmacy = COALESCE(EXCLUDED.linked_pharmacy, doctor_profiles.linked_pharmacy),
                    current_product = COALESCE(EXCLUDED.current_product, doctor_profiles.current_product),
                    notes           = COALESCE(EXCLUDED.notes,           doctor_profiles.notes),
                    class           = EXCLUDED.class,
                    updated_at      = CURRENT_TIMESTAMP
                RETURNING id
            """, (
                user_id, name,
                data.get("specialty"), data.get("clinic_name"),
                area, data.get("phone"), data.get("class", "C"),
                data.get("working_days"), data.get("linked_pharmacy"),
                data.get("current_product"), data.get("notes"),
            ))
        except Exception:
            conn.rollback()
            # fallback: plain insert ignoring duplicate
            c.execute("""
                INSERT INTO doctor_profiles (user_id, name, specialty, clinic_name, area, phone, class,
                                             working_days, linked_pharmacy, current_product, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (user_id, name, data.get("specialty"), data.get("clinic_name"),
                  area, data.get("phone"), data.get("class", "C"),
                  data.get("working_days"), data.get("linked_pharmacy"),
                  data.get("current_product"), data.get("notes")))
        row = c.fetchone()
        profile_id = row[0] if row else None
        if not profile_id:
            c.execute(
                "SELECT id FROM doctor_profiles WHERE user_id=%s AND LOWER(name)=LOWER(%s) LIMIT 1",
                (user_id, name),
            )
            row = c.fetchone()
            profile_id = row[0] if row else None
        conn.commit()
        conn.close()
        return profile_id
    finally:
        conn.close()


def get_doctor_profile_by_id(profile_id: int, user_id: int) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM doctor_profiles WHERE id=%s AND user_id=%s", (profile_id, user_id))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_doctor_profiles(user_id: int) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM doctor_profiles WHERE user_id=%s ORDER BY class ASC, name ASC",
            (user_id,),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def calculate_doctor_class(user_id: int, doctor_name: str) -> str:
    """Auto-calculate A/B/C based on linked orders in the last 30 days."""
    since = (date.today() - timedelta(days=30)).isoformat()
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE user_id=%s AND LOWER(doctor_name) LIKE LOWER(%s) AND order_date >= %s",
            (user_id, f"%{doctor_name.strip()}%", since),
        )
        cnt = c.fetchone()["cnt"]
        conn.close()
        if cnt >= 10:
            return "A"
        elif cnt >= 5:
            return "B"
        return "C"
    finally:
        conn.close()


def update_doctor_class_auto(user_id: int, doctor_name: str) -> str:
    """Recalculate and save class for a doctor profile. Returns new class."""
    cls = calculate_doctor_class(user_id, doctor_name)
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE doctor_profiles SET class=%s WHERE user_id=%s AND LOWER(name) LIKE LOWER(%s)",
            (cls, user_id, f"%{doctor_name.strip()}%"),
        )
        conn.commit()
        conn.close()
        return cls
    finally:
        conn.close()


def find_duplicate_doctors(user_id: int, name: str, area: str = None) -> list:
    """Find doctors with same name + area registered by OTHER users."""
    conn = get_connection()
    try:
        c = conn.cursor()
        if area and area.strip():
            c.execute("""
                SELECT dp.*, u.name as rep_name FROM doctor_profiles dp
                JOIN users u ON u.id = dp.user_id
                WHERE dp.user_id != %s
                  AND LOWER(dp.name) LIKE LOWER(%s)
                  AND LOWER(COALESCE(dp.area,'')) LIKE LOWER(%s)
            """, (user_id, f"%{name.strip()}%", f"%{area.strip()}%"))
        else:
            c.execute("""
                SELECT dp.*, u.name as rep_name FROM doctor_profiles dp
                JOIN users u ON u.id = dp.user_id
                WHERE dp.user_id != %s AND LOWER(dp.name) LIKE LOWER(%s)
            """, (user_id, f"%{name.strip()}%"))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def add_competitor_entry(user_id: int, doctor_profile_id: int,
                         competitor_name: str, competitor_product: str,
                         notes: str = None) -> int:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO competitor_entries
                (user_id, doctor_profile_id, competitor_name, competitor_product, notes)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (user_id, doctor_profile_id, competitor_name, competitor_product, notes))
        rid = c.fetchone()[0]
        conn.commit()
        conn.close()
        return rid
    finally:
        conn.close()


def get_competitor_entries(user_id: int, doctor_profile_id: int = None) -> list:
    conn = get_connection()
    try:
        c = conn.cursor()
        if doctor_profile_id:
            c.execute(
                "SELECT * FROM competitor_entries WHERE user_id=%s AND doctor_profile_id=%s ORDER BY recorded_at DESC",
                (user_id, doctor_profile_id),
            )
        else:
            c.execute(
                "SELECT * FROM competitor_entries WHERE user_id=%s ORDER BY recorded_at DESC",
                (user_id,),
            )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def get_orders_for_doctor(user_id: int, doctor_name: str) -> list:
    """Get all orders linked to a doctor name."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id=%s AND LOWER(doctor_name) LIKE LOWER(%s) ORDER BY order_date DESC",
            (user_id, f"%{doctor_name.strip()}%"),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def delete_doctor_profile(profile_id: int, user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM competitor_entries WHERE doctor_profile_id=%s AND user_id=%s",
                  (profile_id, user_id))
        c.execute("DELETE FROM doctor_profiles WHERE id=%s AND user_id=%s", (profile_id, user_id))
        conn.commit()
        conn.close()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# ── Competitor Drug Library ───────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def init_competitor_drug_tables(conn=None, c=None):
    """Create competitor_drugs and doctor_competitor_drugs tables."""
    _close = False
    if conn is None:
        conn = get_connection()
        c = conn.cursor()
        _close = True

    c.execute("""
        CREATE TABLE IF NOT EXISTS competitor_drugs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            drug_name TEXT NOT NULL,
            company TEXT,
            form TEXT,
            routes TEXT,
            price REAL,
            features TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS doctor_competitor_drugs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            doctor_profile_id INTEGER NOT NULL,
            competitor_drug_id INTEGER NOT NULL,
            linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, doctor_profile_id, competitor_drug_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (doctor_profile_id) REFERENCES doctor_profiles(id),
            FOREIGN KEY (competitor_drug_id) REFERENCES competitor_drugs(id)
        )
    """)

    if _close:
        conn.commit()
        conn.close()


def add_competitor_drug(user_id: int, drug_name: str, company: str,
                        form: str, routes: str, price, features: str) -> int:
    """Insert a new competitor drug and return its id."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """INSERT INTO competitor_drugs (user_id, drug_name, company, form, routes, price, features)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (user_id, drug_name, company, form, routes,
             float(price) if price else None, features),
        )
        new_id = c.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    finally:
        conn.close()


def get_competitor_drugs(user_id: int) -> list:
    """Return all competitor drugs for a user."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM competitor_drugs WHERE user_id=%s ORDER BY drug_name",
            (user_id,),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def get_competitor_drug_by_id(drug_id: int, user_id: int) -> dict:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM competitor_drugs WHERE id=%s AND user_id=%s",
            (drug_id, user_id),
        )
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_competitor_drug(drug_id: int, user_id: int):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM doctor_competitor_drugs WHERE competitor_drug_id=%s AND user_id=%s",
                  (drug_id, user_id))
        c.execute("DELETE FROM competitor_drugs WHERE id=%s AND user_id=%s", (drug_id, user_id))
        conn.commit()
        conn.close()
    finally:
        conn.close()


def toggle_doctor_competitor_drug(user_id: int, drug_id: int, doctor_id: int) -> bool:
    """Link/unlink a doctor to a competitor drug. Returns True if linked, False if unlinked."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT id FROM doctor_competitor_drugs WHERE user_id=%s AND competitor_drug_id=%s AND doctor_profile_id=%s",
            (user_id, drug_id, doctor_id),
        )
        existing = c.fetchone()
        if existing:
            c.execute("DELETE FROM doctor_competitor_drugs WHERE id=%s", (existing["id"],))
            linked = False
        else:
            c.execute(
                "INSERT INTO doctor_competitor_drugs (user_id, doctor_profile_id, competitor_drug_id) VALUES (%s,%s,%s)",
                (user_id, doctor_id, drug_id),
            )
            linked = True
        conn.commit()
        conn.close()
        return linked
    finally:
        conn.close()


def get_linked_doctors_for_drug(user_id: int, drug_id: int) -> list:
    """Return doctor_profile_ids linked to this competitor drug."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT dp.id, dp.name, dp.specialty, dp.area, dp.class
                 FROM doctor_competitor_drugs dcd
                 JOIN doctor_profiles dp ON dp.id = dcd.doctor_profile_id
                WHERE dcd.user_id=%s AND dcd.competitor_drug_id=%s
                ORDER BY dp.name""",
            (user_id, drug_id),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def get_linked_drug_ids_for_drug(user_id: int, drug_id: int) -> set:
    """Return set of doctor_profile_ids already linked to this drug."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT doctor_profile_id FROM doctor_competitor_drugs WHERE user_id=%s AND competitor_drug_id=%s",
            (user_id, drug_id),
        )
        ids = {row[0] for row in c.fetchall()}
        conn.close()
        return ids
    finally:
        conn.close()


def get_competitor_drugs_for_doctor(user_id: int, doctor_id: int) -> list:
    """Return all competitor drugs linked to a doctor."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT cd.*
                 FROM doctor_competitor_drugs dcd
                 JOIN competitor_drugs cd ON cd.id = dcd.competitor_drug_id
                WHERE dcd.user_id=%s AND dcd.doctor_profile_id=%s
                ORDER BY cd.drug_name""",
            (user_id, doctor_id),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def search_doctor_profiles(user_id: int, keyword: str) -> list:
    """Search doctor profiles by name, specialty, or area (case-insensitive)."""
    if not keyword or not keyword.strip():
        return []
    conn = get_connection()
    try:
        c = conn.cursor()
        kw = f"%{keyword.strip()}%"
        c.execute(
            """SELECT id, name, specialty, area, clinic_name, phone, class, notes
                 FROM doctor_profiles
                WHERE user_id = %s
                  AND (name ILIKE %s OR specialty ILIKE %s OR area ILIKE %s OR clinic_name ILIKE %s)
                ORDER BY name
                LIMIT 20""",
            (user_id, kw, kw, kw, kw),
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    finally:
        conn.close()


def search_doctors_by_competitor_drug(user_id: int, drug_keyword: str) -> list:
    """Find doctors linked to a competitor drug whose name matches the keyword."""
    if not drug_keyword or not drug_keyword.strip():
        return []
    conn = get_connection()
    try:
        c = conn.cursor()
        kw = f"%{drug_keyword.strip()}%"

        c.execute(
            """SELECT DISTINCT dp.id, dp.name, dp.specialty, dp.area, dp.class,
                      dp.clinic_name, cd.drug_name AS matched_drug
                 FROM doctor_profiles dp
                 JOIN doctor_competitor_drugs dcd
                   ON dcd.doctor_profile_id = dp.id AND dcd.user_id = dp.user_id
                 JOIN competitor_drugs cd ON cd.id = dcd.competitor_drug_id
                WHERE dp.user_id = %s
                  AND (cd.drug_name ILIKE %s OR cd.company ILIKE %s)
                ORDER BY dp.name
                LIMIT 30""",
            (user_id, kw, kw),
        )
        rows1 = [dict(r) for r in c.fetchall()]

        c.execute(
            """SELECT DISTINCT dp.id, dp.name, dp.specialty, dp.area, dp.class,
                      dp.clinic_name, ce.competitor_product AS matched_drug
                 FROM doctor_profiles dp
                 JOIN competitor_entries ce
                   ON ce.doctor_profile_id = dp.id AND ce.user_id = dp.user_id
                WHERE dp.user_id = %s AND ce.competitor_product ILIKE %s
                ORDER BY dp.name
                LIMIT 30""",
            (user_id, kw),
        )
        rows2 = [dict(r) for r in c.fetchall()]

        seen_ids = set()
        result = []
        for r in rows1 + rows2:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                result.append(r)

        conn.close()
        return result
    finally:
        conn.close()


# ── نسخة احتياطية شاملة ────────────────────────────────────────────────────────

def fetch_orders_yesterday(user_id: int) -> list:
    """Fetch all orders for today and yesterday for a user."""
    from datetime import date, timedelta
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM orders WHERE user_id=%s AND order_date IN (%s, %s) ORDER BY order_date DESC, id DESC",
            (user_id, today, yesterday),
        )
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        conn.close()

def get_full_backup_data() -> dict:
    """يرجع كل بيانات البوت كقاموس {اسم القسم: [صفوف]} — لتوليد نسخة احتياطية شاملة."""
    conn = get_connection()
    try:
        c = conn.cursor()
        data = {}

        c.execute("""
            SELECT u.id, u.telegram_id, u.name, u.username, u.role, u.created_at,
                   s.plan, s.status, s.start_date, s.end_date
            FROM users u
            LEFT JOIN subscriptions s ON s.user_id = u.id
            ORDER BY u.id
        """)
        data["المندوبين"] = [dict(r) for r in c.fetchall()]

        c.execute("SELECT * FROM orders ORDER BY id")
        data["المبيعات"] = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT dp.*, u.name AS rep_name
            FROM doctor_profiles dp LEFT JOIN users u ON u.id = dp.user_id
            ORDER BY dp.id
        """)
        data["الأطباء"] = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT pc.*, u.name AS rep_name
            FROM product_catalog pc LEFT JOIN users u ON u.id = pc.user_id
            ORDER BY pc.id
        """)
        data["كتالوج المنتجات"] = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT dt.*, u.name AS rep_name
            FROM drug_targets dt LEFT JOIN users u ON u.id = dt.user_id
            ORDER BY dt.id
        """)
        data["أهداف المبيعات"] = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT ce.*, u.name AS rep_name
            FROM competitor_entries ce LEFT JOIN users u ON u.id = ce.user_id
            ORDER BY ce.id
        """)
        data["منافسين عند الأطباء"] = [dict(r) for r in c.fetchall()]

        try:
            c.execute("""
                SELECT cd.*, u.name AS rep_name
                FROM competitor_drugs cd LEFT JOIN users u ON u.id = cd.user_id
                ORDER BY cd.id
            """)
            data["أدوية منافسة"] = [dict(r) for r in c.fetchall()]
        except Exception:
            conn.rollback()

        c.execute("SELECT * FROM activation_codes ORDER BY id")
        data["أكواد التفعيل"] = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT sr.*, u.name AS rep_name
            FROM subscription_requests sr LEFT JOIN users u ON u.id = sr.user_id
            ORDER BY sr.id
        """)
        data["طلبات الاشتراك"] = [dict(r) for r in c.fetchall()]

        return data
    finally:
        conn.close()
