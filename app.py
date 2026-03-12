from flask import Flask, render_template, request, redirect, session
import datetime
from collections import defaultdict

app = Flask(__name__)
app.secret_key = "hackathon_secret_key"

# Valid registration numbers: Y24CM133 to Y24CM198
VALID_REG_NUMBERS = {f"Y24CM{i}" for i in range(133, 199)}

# ────────────────────────────────────────────────────────────────
# BOOKING WINDOW & RESET CONFIGURATION
# ────────────────────────────────────────────────────────────────
#   Meal        Book window       Reset hour (after meal is served)
#   ---------   ---------------   ---------------------------------
#   Breakfast   21:00 – 22:00     10:00  (after breakfast is over)
#   Lunch       09:00 – 10:00     14:00  (after lunch is over)
#   Dinner      14:00 – 15:00     21:00  (after dinner is over)

MAX_SUBMISSIONS_PER_MEAL = 2

# feedback_start/end = hours when students can rate that meal (after it is served,
# before the auto-reset clears the data at reset_hour).
#   Breakfast served ~08:00 → feedback 08:00–10:00 (resets 10:00)
#   Lunch     served ~12:00 → feedback 12:00–14:00 (resets 14:00)
#   Dinner    served ~19:00 → feedback 19:00–21:00 (resets 21:00)

MEAL_WINDOWS = {
    "breakfast": {"start": 21, "end": 22, "date_offset": 1, "reset_hour": 10,
                  "feedback_start":  8, "feedback_end": 10},
    "lunch":     {"start":  9, "end": 10, "date_offset": 0, "reset_hour": 14,
                  "feedback_start": 12, "feedback_end": 14},
    "dinner":    {"start": 14, "end": 15, "date_offset": 0, "reset_hour": 21,
                  "feedback_start": 19, "feedback_end": 21},
}

# ────────────────────────────────────────────────────────────────
# DATA STORE
# ────────────────────────────────────────────────────────────────
# student_bookings[reg][date] = {
#     'breakfast': 'Yes'/'No', 'breakfast_count': int,
#     'lunch':     'Yes'/'No', 'lunch_count':     int,
#     'dinner':    'Yes'/'No', 'dinner_count':    int,
# }
student_bookings = defaultdict(lambda: defaultdict(dict))

# Per-meal feedback stored separately so each can be reset independently
meal_ratings      = {"breakfast": [], "lunch": [], "dinner": []}
meal_qty_feedback = {
    "breakfast": {"Less": 0, "Enough": 0, "Excess": 0},
    "lunch":     {"Less": 0, "Enough": 0, "Excess": 0},
    "dinner":    {"Less": 0, "Enough": 0, "Excess": 0},
}

# Tracks which meals have already been auto-reset today
# reset_tracker[date_string][meal] = True
reset_tracker = defaultdict(dict)

# Tracks whether a student has already submitted feedback for a meal today
# student_feedback_given[reg][date][meal] = True
student_feedback_given = defaultdict(lambda: defaultdict(dict))


# ────────────────────────────────────────────────────────────────
# RESET LOGIC
# ────────────────────────────────────────────────────────────────
def do_reset_meal(meal: str, reason: str = "auto"):
    """
    Wipe all booking data and feedback for a single meal.
    Marks it as reset so auto_reset_if_due() won't fire again today.
    """
    today = get_today_date()

    # Remove this meal's keys from every student's booking record
    for reg in list(student_bookings.keys()):
        for date_key in list(student_bookings[reg].keys()):
            bk = student_bookings[reg][date_key]
            bk.pop(meal, None)
            bk.pop(f"{meal}_count", None)
            if not bk:                          # date entry now empty → remove
                del student_bookings[reg][date_key]
        if not student_bookings[reg]:           # student has no dates left → remove
            del student_bookings[reg]

    # Wipe that meal's feedback
    meal_ratings[meal].clear()
    meal_qty_feedback[meal] = {"Less": 0, "Enough": 0, "Excess": 0}

    # Wipe per-student feedback-given flags for this meal
    for reg in list(student_feedback_given.keys()):
        for date_key in list(student_feedback_given[reg].keys()):
            student_feedback_given[reg][date_key].pop(meal, None)

    # Mark so auto-reset doesn't repeat
    reset_tracker[today][meal] = True
    print(f"[RESET:{reason.upper()}] '{meal}' cleared at "
          f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def auto_reset_if_due():
    """
    Called at the top of every request.
    Checks each meal's reset_hour; if the current time has passed it
    and it hasn't been reset yet today, resets it automatically.
    """
    now   = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    for meal, cfg in MEAL_WINDOWS.items():
        if not reset_tracker[today].get(meal, False):
            if now.hour >= cfg["reset_hour"]:
                do_reset_meal(meal, reason="auto")


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
        "open":         cfg["start"] <= now.hour < cfg["end"],
        "target_date":  target_date,
        "start_hour":   cfg["start"],
        "end_hour":     cfg["end"],
        "current_hour": now.hour,
    }


def get_booking_status(reg_number):
    status = {}
    for meal in ("breakfast", "lunch", "dinner"):
        ws  = meal_window_status(meal)
        bk  = student_bookings[reg_number].get(ws["target_date"], {})
        used = bk.get(f"{meal}_count", 0)
        status[meal] = {
            "window_open":      ws["open"],
            "submissions_left": max(0, MAX_SUBMISSIONS_PER_MEAL - used),
            "current_choice":   bk.get(meal),
            "target_date":      ws["target_date"],
            "start_hour":       ws["start_hour"],
            "end_hour":         ws["end_hour"],
        }
    return status


def get_meal_counts():
    """Count 'Yes' bookings per meal using each meal's own target date."""
    now = get_now()
    meal_date = {
        meal: (now + datetime.timedelta(days=cfg["date_offset"])).strftime("%Y-%m-%d")
        for meal, cfg in MEAL_WINDOWS.items()
    }
    counts = {"breakfast": 0, "lunch": 0, "dinner": 0}
    for reg_num, dates in student_bookings.items():
        for meal in counts:
            if dates.get(meal_date[meal], {}).get(meal) == "Yes":
                counts[meal] += 1
    return counts


def get_reset_status():
    """Per-meal reset info for the dashboard display."""
    today = get_today_date()
    now   = get_now()
    result = {}
    for meal, cfg in MEAL_WINDOWS.items():
        done = reset_tracker[today].get(meal, False)
        result[meal] = {
            "reset_done":    done,
            "will_reset_at": f"{cfg['reset_hour']:02d}:00",
        }
    return result


def get_feedback_status(reg_number):
    """
    Returns per-meal feedback availability for a student:
    {
      meal: {
        'window_open': bool,        # feedback hour window is active right now
        'already_given': bool,      # student already submitted for this meal today
        'can_submit': bool,         # window_open AND NOT already_given
        'feedback_start': int,
        'feedback_end':   int,
      }
    }
    """
    now   = get_now()
    today = get_today_date()
    status = {}
    for meal, cfg in MEAL_WINDOWS.items():
        window_open   = cfg["feedback_start"] <= now.hour < cfg["feedback_end"]
        already_given = student_feedback_given[reg_number][today].get(meal, False)
        status[meal]  = {
            "window_open":      window_open,
            "already_given":    already_given,
            "can_submit":       window_open and not already_given,
            "feedback_start":   cfg["feedback_start"],
            "feedback_end":     cfg["feedback_end"],
        }
    return status


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
# HOME
# ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    auto_reset_if_due()
    return render_template("home.html")


# ────────────────────────────────────────────────────────────────
# STUDENT FLOW
# ────────────────────────────────────────────────────────────────
@app.route("/student_login", methods=["GET", "POST"])
def student_login():
    auto_reset_if_due()
    if request.method == "POST":
        reg = request.form.get("reg_number", "").strip().upper()
        if reg not in VALID_REG_NUMBERS:
            return render_template("register.html",
                                   error="Invalid registration number. Please try again.")
        session["reg_number"] = reg
        return redirect("/student_portal")
    return render_template("register.html", error=None)


@app.route("/student_portal")
def student_portal():
    auto_reset_if_due()
    guard = student_required()
    if guard:
        return guard
    return render_template("student_portal.html", reg_number=session.get("reg_number"))


@app.route("/food", methods=["GET", "POST"])
def food():
    auto_reset_if_due()
    guard = student_required()
    if guard:
        return guard

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
    menu    = menus.get(day, menus["Monday"])
    errors  = {}
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
                target_date = ws["target_date"]
                bk          = student_bookings[reg_number].setdefault(target_date, {})
                count_key   = f"{submitted_meal}_count"
                used        = bk.get(count_key, 0)

                if used >= MAX_SUBMISSIONS_PER_MEAL:
                    errors[submitted_meal] = (
                        f"You have already submitted your {submitted_meal.capitalize()} "
                        f"preference {MAX_SUBMISSIONS_PER_MEAL} times. No more changes allowed."
                    )
                else:
                    bk[submitted_meal] = choice
                    bk[count_key]      = used + 1
                    remaining          = MAX_SUBMISSIONS_PER_MEAL - bk[count_key]
                    results[submitted_meal] = (
                        f"✅ {submitted_meal.capitalize()} preference saved as '{choice}'. "
                        f"({remaining} change(s) remaining)"
                    )

    return render_template(
        "food.html",
        menu=menu, day=day,
        errors=errors, results=results,
        booking_status=get_booking_status(reg_number),
        max_submissions=MAX_SUBMISSIONS_PER_MEAL,
    )


@app.route("/food_survey", methods=["GET", "POST"])
def food_survey():
    auto_reset_if_due()
    guard = student_required()
    if guard:
        return guard

    reg_number = session.get("reg_number")
    today      = get_today_date()
    errors     = {}
    results    = {}

    if request.method == "POST":
        meal = request.form.get("meal_type", "").strip()
        r    = request.form.get("rating")
        q    = request.form.get("quantity")

        if meal not in MEAL_WINDOWS:
            errors["general"] = "Please select a valid meal."
        elif r is None or q is None:
            errors[meal] = "Please fill in all fields."
        else:
            cfg         = MEAL_WINDOWS[meal]
            now         = get_now()
            window_open = cfg["feedback_start"] <= now.hour < cfg["feedback_end"]
            already     = student_feedback_given[reg_number][today].get(meal, False)

            if not window_open:
                errors[meal] = (
                    f"Feedback for {meal.capitalize()} is only accepted between "
                    f"{cfg['feedback_start']:02d}:00 and {cfg['feedback_end']:02d}:00."
                )
            elif already:
                errors[meal] = f"You have already submitted feedback for {meal.capitalize()} today."
            else:
                meal_ratings[meal].append(int(r))
                meal_qty_feedback[meal][q] += 1
                student_feedback_given[reg_number][today][meal] = True
                results[meal] = f"✅ Thank you! Your {meal.capitalize()} feedback has been recorded."

    feedback_status = get_feedback_status(reg_number)
    return render_template("survey.html",
                           errors=errors,
                           results=results,
                           feedback_status=feedback_status)


# ────────────────────────────────────────────────────────────────
# ADMIN FLOW
# ────────────────────────────────────────────────────────────────
@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():
    auto_reset_if_due()
    error = ""
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if username == "admin" and password == "1234":
            session["admin_logged_in"] = True
            return redirect("/dashboard")
        else:
            error = "Invalid Username or Password"
    return render_template("admin_login.html", error=error)


@app.route("/dashboard")
def dashboard():
    auto_reset_if_due()
    guard = admin_required()
    if guard:
        return guard

    now         = get_now()
    meal_counts = get_meal_counts()
    reset_status = get_reset_status()

    def avg(meal):
        r = meal_ratings[meal]
        return round(sum(r) / len(r), 2) if r else "No ratings yet"

    def suggestion(meal):
        fb = meal_qty_feedback[meal]
        if fb["Excess"] > fb["Less"]:
            return "Reduce quantity to avoid wastage."
        elif fb["Less"] > fb["Excess"]:
            return "Increase quantity. Students feel it is less."
        return "Maintain same quantity."

    stats = {
        "breakfast":      meal_counts["breakfast"],
        "lunch":          meal_counts["lunch"],
        "dinner":         meal_counts["dinner"],
        "total_students": len(VALID_REG_NUMBERS),
        "students_booked": len([
            reg for reg in student_bookings
            if any(
                student_bookings[reg].get(
                    (now + datetime.timedelta(days=cfg["date_offset"])).strftime("%Y-%m-%d")
                )
                for cfg in MEAL_WINDOWS.values()
            )
        ]),
        # Per-meal ratings & feedback
        "avg_rating_breakfast":  avg("breakfast"),
        "avg_rating_lunch":      avg("lunch"),
        "avg_rating_dinner":     avg("dinner"),
        "qty_breakfast":         meal_qty_feedback["breakfast"],
        "qty_lunch":             meal_qty_feedback["lunch"],
        "qty_dinner":            meal_qty_feedback["dinner"],
        "suggestion_breakfast":  suggestion("breakfast"),
        "suggestion_lunch":      suggestion("lunch"),
        "suggestion_dinner":     suggestion("dinner"),
        # Reset status info
        "reset_status":   reset_status,
        "current_time":   now.strftime("%H:%M:%S"),
    }

    return render_template("dashboard.html", stats=stats)


# ────────────────────────────────────────────────────────────────
# ADMIN MANUAL RESET ROUTES
# ────────────────────────────────────────────────────────────────
@app.route("/admin/reset/<meal>", methods=["POST"])
def admin_reset_meal(meal):
    """Manual reset for a single meal or all meals at once."""
    guard = admin_required()
    if guard:
        return guard

    if meal == "all":
        for m in MEAL_WINDOWS:
            do_reset_meal(m, reason="manual")
    elif meal in MEAL_WINDOWS:
        do_reset_meal(meal, reason="manual")

    return redirect("/dashboard")


# ────────────────────────────────────────────────────────────────
# LOGOUT
# ────────────────────────────────────────────────────────────────
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=True)
