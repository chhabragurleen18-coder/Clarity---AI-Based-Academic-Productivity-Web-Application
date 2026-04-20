import re

with open('c:/Users/hp/OneDrive/Desktop/antigravity/app.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace imports
code = code.replace(
    'from flask import Flask, request, jsonify, render_template',
    'from flask import Flask, request, jsonify, render_template, session, redirect, url_for\nfrom functools import wraps\nfrom werkzeug.security import generate_password_hash, check_password_hash'
)

# old db stuff to replace
old_db_init = """# --- JSON STORAGE ---
DATA_FILE = os.path.join(basedir, 'data.json')

db_user = {
    "wake": "07:00",
    "sleep": "23:00",
    "commitments": [],
    "target_study_hours": 4,
    "streak": 0,
    "score": 0,
    "onboarded": False
}
db_tasks = []          
db_calendar_events = []
db_weekly_timetable = [] 
db_timetable = []      
db_reflections = []    
db_focus_sessions = [] 
db_notified_events = []

def load_data():
    global db_user, db_tasks, db_calendar_events, db_weekly_timetable, db_timetable, db_reflections, db_focus_sessions, db_notified_events
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
                db_user = d.get('user', db_user)
                db_tasks = d.get('tasks', db_tasks)
                db_calendar_events = d.get('calendar_events', db_calendar_events)
                db_weekly_timetable = d.get('weekly_timetable', db_weekly_timetable)
                db_timetable = d.get('timetable', db_timetable)
                db_reflections = d.get('reflections', db_reflections)
                db_focus_sessions = d.get('focus_sessions', db_focus_sessions)
                db_notified_events = d.get('notified_events', db_notified_events)
        except Exception as e:
            print("Error loading data.json:", e)

def save_data():
    d = {
        "user": db_user,
        "tasks": db_tasks,
        "calendar_events": db_calendar_events,
        "weekly_timetable": db_weekly_timetable,
        "timetable": db_timetable,
        "reflections": db_reflections,
        "focus_sessions": db_focus_sessions,
        "notified_events": db_notified_events
    }
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(d, f, indent=4)

load_data()"""

new_db_init = """app.secret_key = 'super_secret_clarity_key' # Added for sessions

DATA_DIR = os.path.join(basedir, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, 'users.json')

if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump({}, f)

def get_user_data_path(user_id):
    user_dir = os.path.join(DATA_DIR, f"user_{user_id}")
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, 'data.json')

def load_data():
    if 'user_id' not in session:
        return {
            "user": {"wake": "07:00", "sleep": "23:00", "commitments": [], "target_study_hours": 4, "streak": 0, "score": 0, "onboarded": False},
            "tasks": [], "calendar_events": [], "weekly_timetable": [], "timetable": [], "reflections": [], "focus_sessions": [], "notified_events": []
        }
    path = get_user_data_path(session['user_id'])
    default_data = {
        "user": {"wake": "07:00", "sleep": "23:00", "commitments": [], "target_study_hours": 4, "streak": 0, "score": 0, "onboarded": False},
        "tasks": [], "calendar_events": [], "weekly_timetable": [], "timetable": [], "reflections": [], "focus_sessions": [], "notified_events": []
    }
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
                return {**default_data, **d}
        except Exception as e:
            print(f"Error loading {path}:", e)
    return default_data

def save_data(data_store):
    if 'user_id' not in session: return
    path = get_user_data_path(session['user_id'])
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data_store, f, indent=4)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Auth routes
@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    with open(USERS_FILE, 'r') as f:
        users = json.load(f)
        
    if username in users and check_password_hash(users[username]['password'], password):
        session['user_id'] = users[username]['id']
        session['username'] = username
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid username or password"}), 401

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if len(username) < 3 or len(password) < 6:
        return jsonify({"success": False, "error": "Username must be 3+ chars, password 6+ chars"}), 400
        
    with open(USERS_FILE, 'r+') as f:
        users = json.load(f)
        if username in users:
            return jsonify({"success": False, "error": "Username already exists"}), 400
            
        user_id = str(len(users) + 1)
        users[username] = {
            "id": user_id,
            "password": generate_password_hash(password)
        }
        f.seek(0)
        json.dump(users, f, indent=4)
        f.truncate()
        
    session['user_id'] = user_id
    session['username'] = username
    return jsonify({"success": True, "is_new": True})

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({"success": True})
"""

code = code.replace(old_db_init, new_db_init)

# Now, add login_required and inject variables to each route
def modify_route(match):
    full_def = match.group(0)
    
    if "('/login'" in full_def or "('/')" in full_def or "('/api/auth" in full_def:
        return full_def
        
    header = match.group(1) # @app.route... def api_func():
    
    # Check if login_required is already there
    if "@login_required" in header:
        return full_def

    new_header = header.replace("@app.route", "@app.route") # no-op just to be safe
    new_header = "@login_required\n" + new_header
    
    injection = """
    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
"""
    # Find the end of def func():
    end_of_def = new_header.find(":\n") + 2
    
    new_header = new_header[:end_of_def] + injection + new_header[end_of_def:]
    
    return new_header

# regex to capture entire route decorators + def
# we need to be careful with routes that have multiple lines
code = re.sub(r'(@app\.route\([^\)]+\)\n(?:@[^\n]+\n)*def \w+\([^)]*\):\n)', modify_route, code)

# fix globals statements
code = re.sub(r'^\s*global\s+db_[a-z_]+(?:,\s*db_[a-z_]+)*\n', '', code, flags=re.MULTILINE)

# Replace save_data() with save_data(data_store)
code = re.sub(r'\bsave_data\(\)', 'save_data(data_store)', code)

with open('c:/Users/hp/OneDrive/Desktop/antigravity/app.py', 'w', encoding='utf-8') as f:
    f.write(code)
print("done")
