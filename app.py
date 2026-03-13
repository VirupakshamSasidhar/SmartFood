from flask import Flask, render_template, request, redirect, session
import datetime
import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()   # reads .env file when running locally

app = Flask(__name__)
app.secret_key = "hackathon_secret_key"

# ────────────────────────────────────────────────────────────────
# VALID REGISTRATION NUMBERS
# ────────────────────────────────────────────────────────────────
VALID_REG_NUMBERS = {f"Y24CM{i}" for i in range(133, 199)}

# ────────────────────────────────────────────────────────────────
# CONFIGURATION
# ────────────────────────────────────────────────────────────────
MAX_SUBMISSIONS_PER_MEAL = 2

MEAL_WINDOWS = {
    "breakfast": {"start": 21, "end": 22, "date_offset": 1, "reset_hour": 10,
                  "feedback_start": 8,  "feedback_end": 10},
    "lunch":     {"start": 9,  "end": 10, "date_offset": 0, "reset_hour": 14,
                  "feedback_start": 12, "feedback_end": 14},
    "dinner":    {"start": 14, "end": 15, "date_offset": 0, "reset_hour": 21,
                  "feedback_start": 19, "feedback_end": 21},
}

def is_in_window(start_hour: int, end_hour: int, current_hour: int) -> bool:
    if start_hour < end_hour:
        return start_hour <= current_hour < end_hour
    if start_hour > end_hour:
        return current_hour >= start_hour or current_hour < end_hour
    return False


# ────────────────────────────────────────────────────────────────
# DATABASE — PostgreSQL via DATABASE_URL
# ────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set.\n"
        "Create a .env file with: DATABASE_URL=postgresql://user:pass@host/dbname"
    )

# Render.com gives postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def dict_row(cursor, row):
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def fetchall_dict(cur):
    rows = cur.fetchall()
    return [dict_row(cur, r) for r in rows]


def fetchone_dict(cur):
    row = cur.fetchone()
    return dict_row(cur, row) if row else None


def init_db():
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            reg_number  TEXT PRIMARY KEY,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    for reg in sorted(VALID_REG_NUMBERS):
        cur.execute(
            "INSERT INTO students (reg_number) VALUES (%s) ON CONFLICT DO NOTHING",
            (reg,)
        )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meal_bookings (
            id               SERIAL PRIMARY KEY,
            reg_number       TEXT    NOT NULL REFERENCES students(reg_number),
            target_date      DATE    NOT NULL,
            meal             TEXT    NOT NULL CHECK (meal IN ('breakfast','lunch','dinner')),
            choice           TEXT    NOT NULL CHECK (choice IN ('Yes','No')),
            submission_count INTEGER NOT NULL DEFAULT 1,
            updated_at       TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE (reg_number, target_date, meal)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meal_feedback (
            id            SERIAL PRIMARY KEY,
            reg_number    TEXT    NOT NULL REFERENCES students(reg_number),
            feedback_date DATE    NOT NULL,
            meal          TEXT    NOT NULL CHECK (meal IN ('breakfast','lunch','dinner')),
            rating        INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            quantity      TEXT    NOT NULL CHECK (quantity IN ('Less','Enough','Excess')),
            submitted_at  TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE (reg_number, feedback_date, meal)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meal_feedback_agg (
            agg_date     DATE NOT NULL,
            meal         TEXT NOT NULL CHECK (meal IN ('breakfast','lunch','dinner')),
            total_rating INTEGER NOT NULL DEFAULT 0,
            rating_count INTEGER NOT NULL DEFAULT 0,
            qty_less     INTEGER NOT NULL DEFAULT 0,
            qty_enough   INTEGER NOT NULL DEFAULT 0,
            qty_excess   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (agg_date, meal)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reset_log (
            id         SERIAL PRIMARY KEY,
            reset_date DATE      NOT NULL,
            meal       TEXT      NOT NULL,
            reason     TEXT      NOT NULL CHECK (reason IN ('auto','manual')),
            reset_at   TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Tables ready.")


# ────────────────────────────────────────────────────────────────
# GENERAL HELPERS
# ────────────────────────────────────────────────────────────────
def get_now():
    return datetime.datetime.now()

def get_today_date():
    return get_now().strftime("%Y-%m-%d")

def meal_window_status(meal: str):
    cfg = MEAL_WINDOWS[meal]
    now = get_now()
    target_date = (now + datetime.timedelta(days=cfg["date_offset"])).strftime("%Y-%m-%d")
    return {
        "open":         is_in_window(cfg["start"], cfg["end"], now.hour),
        "target_date":  target_date,
        "start_hour":   cfg["start"],
        "end_hour":     cfg["end"],
        "current_hour": now.hour,
    }


# ────────────────────────────────────────────────────────────────
# RESET LOGIC
# ────────────────────────────────────────────────────────────────
def do_reset_meal(meal: str, reason: str = "auto"):
    today = get_today_date()
    now   = get_now()
    cfg   = MEAL_WINDOWS[meal]
    target_date = (now + datetime.timedelta(days=cfg["date_offset"])).strftime("%Y-%m-%d")

    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("DELETE FROM meal_bookings WHERE target_date=%s AND meal=%s", (target_date, meal))
        cur.execute("DELETE FROM meal_feedback WHERE feedback_date=%s AND meal=%s", (today, meal))
        cur.execute("DELETE FROM meal_feedback_agg WHERE agg_date=%s AND meal=%s", (today, meal))
        cur.execute("INSERT INTO reset_log (reset_date, meal, reason) VALUES (%s, %s, %s)", (today, meal, reason))
        conn.commit()
        print(f"[RESET:{reason.upper()}] '{meal}' cleared at {now.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        conn.rollback()
        print(f"[RESET ERROR] {e}")
    finally:
        conn.close()


def auto_reset_if_due():
    now   = get_now()
    today = now.strftime("%Y-%m-%d")
    conn  = get_db()
    cur   = conn.cursor()
    try:
        for meal, cfg in MEAL_WINDOWS.items():
            if now.hour >= cfg["reset_hour"]:
                cur.execute(
                    "SELECT id FROM reset_log WHERE reset_date=%s AND meal=%s LIMIT 1",
                    (today, meal)
                )
                if not cur.fetchone():
                    conn.close()
                    do_reset_meal(meal, reason="auto")
                    conn = get_db()
                    cur  = conn.cursor()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────
# BOOKING HELPERS
# ────────────────────────────────────────────────────────────────
def get_booking_status(reg_number: str) -> dict:
    conn = get_db()
    cur  = conn.cursor()
    status = {}
    try:
        for meal in ("breakfast", "lunch", "dinner"):
            ws = meal_window_status(meal)
            cur.execute(
                "SELECT choice, submission_count FROM meal_bookings "
                "WHERE reg_number=%s AND target_date=%s AND meal=%s",
                (reg_number, ws["target_date"], meal)
            )
            row = fetchone_dict(cur)
            used           = row["submission_count"] if row else 0
            current_choice = row["choice"]           if row else None
            status[meal] = {
                "window_open":      ws["open"],
                "submissions_left": max(0, MAX_SUBMISSIONS_PER_MEAL - used),
                "current_choice":   current_choice,
                "target_date":      ws["target_date"],
                "start_hour":       ws["start_hour"],
                "end_hour":         ws["end_hour"],
            }
    finally:
        conn.close()
    return status


def get_meal_counts() -> dict:
    now    = get_now()
    counts = {}
    conn   = get_db()
    cur    = conn.cursor()
    try:
        for meal, cfg in MEAL_WINDOWS.items():
            target_date = (now + datetime.timedelta(days=cfg["date_offset"])).strftime("%Y-%m-%d")
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM meal_bookings "
                "WHERE target_date=%s AND meal=%s AND choice='Yes'",
                (target_date, meal)
            )
            row = fetchone_dict(cur)
            counts[meal] = row["cnt"] if row else 0
    finally:
        conn.close()
    return counts


def get_students_booked_today() -> int:
    now  = get_now()
    conn = get_db()
    cur  = conn.cursor()
    try:
        dates = [
            (now + datetime.timedelta(days=cfg["date_offset"])).strftime("%Y-%m-%d")
            for cfg in MEAL_WINDOWS.values()
        ]
        cur.execute(
            "SELECT COUNT(DISTINCT reg_number) AS cnt FROM meal_bookings WHERE target_date = ANY(%s::date[])",
            (dates,)
        )
        row = fetchone_dict(cur)
        return row["cnt"] if row else 0
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────
# FEEDBACK HELPERS
# ────────────────────────────────────────────────────────────────
def get_feedback_status(reg_number: str) -> dict:
    now   = get_now()
    today = get_today_date()
    conn  = get_db()
    cur   = conn.cursor()
    status = {}
    try:
        for meal, cfg in MEAL_WINDOWS.items():
            window_open = is_in_window(cfg["feedback_start"], cfg["feedback_end"], now.hour)
            cur.execute(
                "SELECT id FROM meal_feedback WHERE reg_number=%s AND feedback_date=%s AND meal=%s",
                (reg_number, today, meal)
            )
            already_given = cur.fetchone() is not None
            status[meal] = {
                "window_open":    window_open,
                "already_given":  already_given,
                "can_submit":     window_open and not already_given,
                "feedback_start": cfg["feedback_start"],
                "feedback_end":   cfg["feedback_end"],
            }
    finally:
        conn.close()
    return status


def get_feedback_agg(meal: str, date: str) -> dict:
    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM meal_feedback_agg WHERE agg_date=%s AND meal=%s", (date, meal))
        row = fetchone_dict(cur)
        if row:
            return {k: row[k] for k in ("total_rating","rating_count","qty_less","qty_enough","qty_excess")}
        return {"total_rating": 0, "rating_count": 0, "qty_less": 0, "qty_enough": 0, "qty_excess": 0}
    finally:
        conn.close()


def upsert_feedback_agg(cur, date: str, meal: str, rating: int, quantity: str):
    qty_col = {"Less": "qty_less", "Enough": "qty_enough", "Excess": "qty_excess"}[quantity]
    cur.execute(f"""
        INSERT INTO meal_feedback_agg (agg_date, meal, total_rating, rating_count, {qty_col})
        VALUES (%s, %s, %s, 1, 1)
        ON CONFLICT (agg_date, meal) DO UPDATE SET
            total_rating = meal_feedback_agg.total_rating + EXCLUDED.total_rating,
            rating_count = meal_feedback_agg.rating_count + 1,
            {qty_col}    = meal_feedback_agg.{qty_col} + 1
    """, (date, meal, rating))


def get_reset_status() -> dict:
    today  = get_today_date()
    conn   = get_db()
    cur    = conn.cursor()
    result = {}
    try:
        for meal, cfg in MEAL_WINDOWS.items():
            cur.execute("SELECT id FROM reset_log WHERE reset_date=%s AND meal=%s LIMIT 1", (today, meal))
            result[meal] = {
                "reset_done":    cur.fetchone() is not None,
                "will_reset_at": f"{cfg['reset_hour']:02d}:00",
            }
    finally:
        conn.close()
    return result


# ────────────────────────────────────────────────────────────────
# AUTH GUARDS
# ────────────────────────────────────────────────────────────────
def student_required():
    if not session.get("reg_number"):
        return redirect("/student_login")
    return None

def admin_required():
    if not session.get("admin_logged_in"):
        return redirect("/admin_login")
    return None


# ────────────────────────────────────────────────────────────────
# ROUTES
# ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    auto_reset_if_due()
    return render_template("home.html")


@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    auto_reset_if_due()
    if request.method == "POST":
        reg = request.form.get("reg_number", "").strip().upper()
        if reg not in VALID_REG_NUMBERS:
            return render_template("register.html", error="Invalid registration number. Please try again.")
        session["reg_number"] = reg
        return redirect("/student_portal")
    return render_template("register.html", error=None)


@app.route("/student_portal")
def student_portal():
    auto_reset_if_due()
    guard = student_required()
    if guard: return guard
    return render_template("student_portal.html", reg_number=session.get("reg_number"))


@app.route("/food", methods=["GET", "POST"])
def food():
    auto_reset_if_due()
    guard = student_required()
    if guard: return guard

    reg_number = session.get("reg_number")
    now = get_now()
    day = now.strftime("%A")

    menus = {
        "Monday":    {"breakfast": "Idli & Sambar",    "lunch": "Rice, Dal, Veg Curry", "dinner": "Chapati & Paneer"},
        "Tuesday":   {"breakfast": "Dosa & Chutney",   "lunch": "Veg Biryani",          "dinner": "Fried Rice"},
        "Wednesday": {"breakfast": "Pongal",            "lunch": "Sambar Rice",           "dinner": "Paratha"},
        "Thursday":  {"breakfast": "Upma",              "lunch": "Curd Rice",             "dinner": "Veg Pulao"},
        "Friday":    {"breakfast": "Poori & Curry",     "lunch": "Tomato Rice",           "dinner": "Noodles"},
        "Saturday":  {"breakfast": "Masala Dosa",       "lunch": "Veg Meals",             "dinner": "Chapati & Kurma"},
        "Sunday":    {"breakfast": "Aloo Paratha",      "lunch": "Special Biryani",       "dinner": "Light Dinner"},
    }
    menu   = menus.get(day, menus["Monday"])
    errors = {}
    results = {}

    if request.method == "POST":
        submitted_meal = request.form.get("meal")
        choice         = request.form.get("choice")

        if submitted_meal not in MEAL_WINDOWS:
            errors[submitted_meal] = "Invalid meal selection."
        else:
            ws = meal_window_status(submitted_meal)
            if not ws["open"]:
                errors[submitted_meal] = (
                    f"Booking window for {submitted_meal.capitalize()} is closed. "
                    f"Window: {ws['start_hour']:02d}:00 – {ws['end_hour']:02d}:00"
                )
            else:
                conn = get_db()
                cur  = conn.cursor()
                try:
                    cur.execute(
                        "SELECT submission_count FROM meal_bookings "
                        "WHERE reg_number=%s AND target_date=%s AND meal=%s",
                        (reg_number, ws["target_date"], submitted_meal)
                    )
                    row  = fetchone_dict(cur)
                    used = row["submission_count"] if row else 0

                    if used >= MAX_SUBMISSIONS_PER_MEAL:
                        errors[submitted_meal] = (
                            f"You have already submitted your {submitted_meal.capitalize()} "
                            f"preference {MAX_SUBMISSIONS_PER_MEAL} times."
                        )
                    else:
                        cur.execute("""
                            INSERT INTO meal_bookings (reg_number, target_date, meal, choice, submission_count)
                            VALUES (%s, %s, %s, %s, 1)
                            ON CONFLICT (reg_number, target_date, meal) DO UPDATE SET
                                choice           = EXCLUDED.choice,
                                submission_count = meal_bookings.submission_count + 1,
                                updated_at       = NOW()
                        """, (reg_number, ws["target_date"], submitted_meal, choice))
                        conn.commit()
                        remaining = MAX_SUBMISSIONS_PER_MEAL - (used + 1)
                        results[submitted_meal] = (
                            f"✅ {submitted_meal.capitalize()} preference saved as '{choice}'. "
                            f"({remaining} change(s) remaining)"
                        )
                except Exception as e:
                    conn.rollback()
                    errors[submitted_meal] = f"Database error: {e}"
                finally:
                    conn.close()

    return render_template(
        "food.html", menu=menu, day=day, errors=errors, results=results,
        booking_status=get_booking_status(reg_number),
        max_submissions=MAX_SUBMISSIONS_PER_MEAL,
    )


@app.route("/food_survey", methods=["GET", "POST"])
def food_survey():
    auto_reset_if_due()
    guard = student_required()
    if guard: return guard

    reg_number = session.get("reg_number")
    today  = get_today_date()
    errors = {}
    results = {}

    if request.method == "POST":
        meal = request.form.get("meal_type", "").strip()
        r    = request.form.get("rating")
        q    = request.form.get("quantity")

        if meal not in MEAL_WINDOWS:
            errors["general"] = "Please select a valid meal."
        elif r is None or q is None:
            errors[meal] = "Please fill in all fields."
        else:
            cfg = MEAL_WINDOWS[meal]
            now = get_now()
            window_open = is_in_window(cfg["feedback_start"], cfg["feedback_end"], now.hour)

            if not window_open:
                errors[meal] = (
                    f"Feedback for {meal.capitalize()} is only accepted between "
                    f"{cfg['feedback_start']:02d}:00 and {cfg['feedback_end']:02d}:00."
                )
            else:
                conn = get_db()
                cur  = conn.cursor()
                try:
                    cur.execute(
                        "SELECT id FROM meal_feedback WHERE reg_number=%s AND feedback_date=%s AND meal=%s",
                        (reg_number, today, meal)
                    )
                    if cur.fetchone():
                        errors[meal] = f"You have already submitted feedback for {meal.capitalize()} today."
                    else:
                        cur.execute(
                            "INSERT INTO meal_feedback (reg_number, feedback_date, meal, rating, quantity) "
                            "VALUES (%s, %s, %s, %s, %s)",
                            (reg_number, today, meal, int(r), q)
                        )
                        upsert_feedback_agg(cur, today, meal, int(r), q)
                        conn.commit()
                        results[meal] = f"✅ Thank you! Your {meal.capitalize()} feedback has been recorded."
                except Exception as e:
                    conn.rollback()
                    errors[meal] = f"Database error: {e}"
                finally:
                    conn.close()

    return render_template("survey.html", errors=errors, results=results,
                           feedback_status=get_feedback_status(reg_number))


@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():
    auto_reset_if_due()
    error = ""
    if request.method == "POST":
        if request.form["username"] == "admin" and request.form["password"] == "1234":
            session["admin_logged_in"] = True
            return redirect("/dashboard")
        error = "Invalid Username or Password"
    return render_template("admin_login.html", error=error)


@app.route("/dashboard")
def dashboard():
    auto_reset_if_due()
    guard = admin_required()
    if guard: return guard

    now   = get_now()
    today = get_today_date()
    meal_counts  = get_meal_counts()
    reset_status = get_reset_status()

    def avg(meal):
        agg = get_feedback_agg(meal, today)
        return round(agg["total_rating"] / agg["rating_count"], 2) if agg["rating_count"] else "No ratings yet"

    def qty(meal):
        agg = get_feedback_agg(meal, today)
        return {"Less": agg["qty_less"], "Enough": agg["qty_enough"], "Excess": agg["qty_excess"]}

    def suggestion(meal):
        fb = qty(meal)
        if fb["Excess"] > fb["Less"]:   return "Reduce quantity to avoid wastage."
        if fb["Less"]   > fb["Excess"]: return "Increase quantity. Students feel it is less."
        return "Maintain same quantity."

    stats = {
        "breakfast": meal_counts["breakfast"], "lunch": meal_counts["lunch"], "dinner": meal_counts["dinner"],
        "total_students": len(VALID_REG_NUMBERS), "students_booked": get_students_booked_today(),
        "avg_rating_breakfast": avg("breakfast"), "avg_rating_lunch": avg("lunch"), "avg_rating_dinner": avg("dinner"),
        "qty_breakfast": qty("breakfast"), "qty_lunch": qty("lunch"), "qty_dinner": qty("dinner"),
        "suggestion_breakfast": suggestion("breakfast"), "suggestion_lunch": suggestion("lunch"), "suggestion_dinner": suggestion("dinner"),
        "reset_status": reset_status, "current_time": now.strftime("%H:%M:%S"),
    }
    return render_template("dashboard.html", stats=stats)


@app.route("/admin/reset/<meal>", methods=["POST"])
def admin_reset_meal(meal):
    guard = admin_required()
    if guard: return guard
    if meal == "all":
        for m in MEAL_WINDOWS: do_reset_meal(m, reason="manual")
    elif meal in MEAL_WINDOWS:
        do_reset_meal(meal, reason="manual")
    return redirect("/dashboard")


@app.route("/admin/db")
def admin_db_viewer():
    guard = admin_required()
    if guard: return guard

    active_table = request.args.get("table", "students")
    conn = get_db()
    cur  = conn.cursor()
    try:
        counts = {}
        for tbl in ("students", "meal_bookings", "meal_feedback", "meal_feedback_agg", "reset_log"):
            cur.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}")
            row = fetchone_dict(cur)
            counts[tbl] = row["cnt"] if row else 0

        data  = []
        extra = {}

        if active_table == "students":
            cur.execute("SELECT * FROM students ORDER BY reg_number")
            data = fetchall_dict(cur)

        elif active_table == "meal_bookings":
            cur.execute("SELECT * FROM meal_bookings ORDER BY target_date DESC, updated_at DESC")
            data = fetchall_dict(cur)
            cur.execute("SELECT COUNT(*) AS c FROM meal_bookings WHERE choice='Yes'")
            y = fetchone_dict(cur)
            cur.execute("SELECT COUNT(*) AS c FROM meal_bookings WHERE choice='No'")
            n = fetchone_dict(cur)
            extra = {"yes_count": y["c"] if y else 0, "no_count": n["c"] if n else 0}

        elif active_table == "meal_feedback":
            cur.execute("SELECT * FROM meal_feedback ORDER BY feedback_date DESC, submitted_at DESC")
            data = fetchall_dict(cur)
            cur.execute("SELECT ROUND(AVG(rating)::numeric, 2) AS avg FROM meal_feedback")
            a = fetchone_dict(cur)
            extra = {"avg_rating": a["avg"] if a and a["avg"] else "—"}

        elif active_table == "meal_feedback_agg":
            cur.execute("SELECT * FROM meal_feedback_agg ORDER BY agg_date DESC, meal")
            data = fetchall_dict(cur)

        elif active_table == "reset_log":
            cur.execute("SELECT * FROM reset_log ORDER BY reset_at DESC")
            data = fetchall_dict(cur)
            cur.execute("SELECT COUNT(*) AS c FROM reset_log WHERE reason='auto'")
            ar = fetchone_dict(cur)
            cur.execute("SELECT COUNT(*) AS c FROM reset_log WHERE reason='manual'")
            mr = fetchone_dict(cur)
            extra = {"auto_resets": ar["c"] if ar else 0, "manual_resets": mr["c"] if mr else 0}

    finally:
        conn.close()

    return render_template("db_viewer.html", active_table=active_table, counts=counts, data=data, extra=extra)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.errorhandler(404)
def page_not_found(e):
    return redirect("/")

@app.errorhandler(500)
def server_error(e):
    return redirect("/")

if __name__ == "__main__":
    init_db()
    app.run(debug=False)
