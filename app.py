from flask import Flask, render_template, request, redirect, session
import datetime
from collections import defaultdict

app = Flask(__name__)
app.secret_key = "hackathon_secret_key"

# Valid registration numbers: Y24CM133 to Y24CM198
VALID_REG_NUMBERS = {f"Y24CM{i}" for i in range(133, 199)}

# Data storage (for demo)
ratings = []
quantity_feedback = {"Less": 0, "Enough": 0, "Excess": 0}

# Store student food bookings
student_bookings = defaultdict(lambda: defaultdict(dict))


def student_required():
    """Return a redirect if the student is NOT registered, else None."""
    if not session.get('reg_number'):
        return redirect('/student_login')
    return None


def admin_required():
    """Return a redirect if admin is NOT logged in, else None."""
    if not session.get('admin_logged_in'):
        return redirect('/admin_login')
    return None


def get_today_date():
    """Get today's date as a string."""
    return datetime.datetime.now().strftime("%Y-%m-%d")


def get_meal_counts():
    """Calculate actual meal counts from student bookings for today."""
    today = get_today_date()
    
    counts = {
        "breakfast": 0,
        "lunch": 0,
        "dinner": 0
    }
    
    for reg_num, dates in student_bookings.items():
        if today in dates:
            booking = dates[today]
            if booking.get('breakfast') == 'Yes':
                counts['breakfast'] += 1
            if booking.get('lunch') == 'Yes':
                counts['lunch'] += 1
            if booking.get('dinner') == 'Yes':
                counts['dinner'] += 1
    
    return counts


# ────────────────────────────────────────────────────────────────
# NEW HOME PAGE - Role Selection
# ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    """Landing page asking if user is student or administrator"""
    return render_template('home.html')


# ────────────────────────────────────────────────────────────────
# STUDENT FLOW
# ────────────────────────────────────────────────────────────────
@app.route('/student_login', methods=['GET', 'POST'])
def student_login():
    """Student registration/login page"""
    if request.method == 'POST':
        reg = request.form.get('reg_number', '').strip().upper()
        
        if reg not in VALID_REG_NUMBERS:
            return render_template('register.html',
                                 error="Invalid registration number. Please try again.")
        
        session['reg_number'] = reg
        return redirect('/student_portal')
    
    return render_template('register.html', error=None)


@app.route('/student_portal')
def student_portal():
    """Student portal with food booking and survey options"""
    guard = student_required()
    if guard:
        return guard
    
    reg_number = session.get('reg_number')
    return render_template('student_portal.html', reg_number=reg_number)


@app.route('/food', methods=['GET', 'POST'])
def food():
    """Food pre-booking for students"""
    guard = student_required()
    if guard:
        return guard

    result = ""
    reg_number = session.get('reg_number')
    today = get_today_date()
    day = datetime.datetime.now().strftime("%A")

    # Weekly menu
    menus = {
        "Monday": {"breakfast": "Idli & Sambar", "lunch": "Rice, Dal, Veg Curry", "dinner": "Chapati & Paneer"},
        "Tuesday": {"breakfast": "Dosa & Chutney", "lunch": "Veg Biryani", "dinner": "Fried Rice"},
        "Wednesday": {"breakfast": "Pongal", "lunch": "Sambar Rice", "dinner": "Paratha"},
        "Thursday": {"breakfast": "Upma", "lunch": "Curd Rice", "dinner": "Veg Pulao"},
        "Friday": {"breakfast": "Poori & Curry", "lunch": "Tomato Rice", "dinner": "Noodles"},
        "Saturday": {"breakfast": "Masala Dosa", "lunch": "Veg Meals", "dinner": "Chapati & Kurma"},
        "Sunday": {"breakfast": "Aloo Paratha", "lunch": "Special Biryani", "dinner": "Light Dinner"}
    }

    menu = menus.get(day, menus["Monday"])
    existing_booking = student_bookings[reg_number].get(today, {})

    if request.method == 'POST':
        b = request.form['breakfast']
        l = request.form['lunch']
        d = request.form['dinner']
        
        student_bookings[reg_number][today] = {
            'breakfast': b,
            'lunch': l,
            'dinner': d
        }
        
        meals_selected = []
        if b == 'Yes':
            meals_selected.append('Breakfast')
        if l == 'Yes':
            meals_selected.append('Lunch')
        if d == 'Yes':
            meals_selected.append('Dinner')
        
        if meals_selected:
            result = f"✅ Booking confirmed for: {', '.join(meals_selected)}"
        else:
            result = "⚠️ You have opted out of all meals for today"

    return render_template('food.html', 
                         menu=menu, 
                         day=day, 
                         result=result,
                         existing_booking=existing_booking)


@app.route('/food_survey', methods=['GET', 'POST'])
def food_survey():
    """Food quality and quantity survey"""
    guard = student_required()
    if guard:
        return guard

    message = ""
    if request.method == 'POST':
        r = int(request.form['rating'])
        q = request.form['quantity']

        ratings.append(r)
        quantity_feedback[q] += 1

        message = "Thank you! Your feedback has been recorded."

    return render_template('survey.html', message=message)


# ────────────────────────────────────────────────────────────────
# ADMIN FLOW
# ────────────────────────────────────────────────────────────────
@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    """Admin login page"""
    error = ""
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        if username == "admin" and password == "1234":
            session['admin_logged_in'] = True
            return redirect('/dashboard')
        else:
            error = "Invalid Username or Password"

    return render_template('admin_login.html', error=error)


@app.route('/dashboard')
def dashboard():
    """Kitchen staff dashboard"""
    guard = admin_required()
    if guard:
        return guard

    meal_counts = get_meal_counts()
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else "No ratings yet"

    suggestion = "Maintain same quantity."
    if quantity_feedback["Excess"] > quantity_feedback["Less"]:
        suggestion = "Reduce quantity to avoid wastage."
    elif quantity_feedback["Less"] > quantity_feedback["Excess"]:
        suggestion = "Increase quantity. Students feel it is less."

    stats = {
        "breakfast": meal_counts['breakfast'],
        "lunch": meal_counts['lunch'],
        "dinner": meal_counts['dinner'],
        "total_students": len(VALID_REG_NUMBERS),
        "students_booked": len([reg for reg in student_bookings if get_today_date() in student_bookings[reg]]),
        "avg_rating": avg_rating,
        "qty_less": quantity_feedback["Less"],
        "qty_enough": quantity_feedback["Enough"],
        "qty_excess": quantity_feedback["Excess"],
        "suggestion": suggestion
    }

    return render_template('dashboard.html', stats=stats)


# ────────────────────────────────────────────────────────────────
# LOGOUT
# ────────────────────────────────────────────────────────────────
@app.route('/logout')
def logout():
    """Logout for both students and admins"""
    session.clear()
    return redirect('/')


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
