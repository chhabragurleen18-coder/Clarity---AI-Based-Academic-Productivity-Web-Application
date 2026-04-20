from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_from_directory
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import os
import math
import datetime
import re as _re
import re
import pdfplumber
from werkzeug.utils import secure_filename
import json
import requests
import base64
from dotenv import load_dotenv

load_dotenv()  # Load API keys from .env file

api_key = os.environ.get("OPENAI_API_KEY", "")
google_vision_key = os.environ.get("GOOGLE_VISION_API_KEY", "")

def call_openai_json(prompt, text):
    if not api_key:
        print("[ERROR] OPENAI_API_KEY not set! Please add your key to the .env file.")
        return None
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful academic assistant. You MUST return valid JSON only."},
                {"role": "user", "content": f"{prompt}\n\nTEXT:\n{text[:30000]}"}
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 4000,
            "temperature": 0.3
        }
        print(f"[OpenAI] Sending {len(text)} chars for JSON extraction...")
        res = requests.post(url, headers=headers, json=payload, timeout=60)
        res.raise_for_status()
        data = res.json()
        raw_text = data['choices'][0]['message']['content']
        # Strip markdown tags
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("` \n").removeprefix("json").strip()
        result = json.loads(raw_text)
        print(f"[OpenAI] JSON extraction successful. Keys: {list(result.keys()) if isinstance(result, dict) else 'list'}")
        return result
    except requests.exceptions.HTTPError as e:
        print(f"[OpenAI HTTP Error] Status {e.response.status_code}: {e.response.text[:500]}")
        return None
    except json.JSONDecodeError as e:
        print(f"[OpenAI JSON Error] Could not parse response: {e}")
        return None
    except Exception as e:
        print(f"[OpenAI Error] {type(e).__name__}: {e}")
        return None

def call_openai_text(prompt, text):
    if not api_key: return ""
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": f"{prompt}\n\nTEXT:\n{text}"}
            ]
        }
        res = requests.post(url, headers=headers, json=payload, timeout=30)
        res.raise_for_status()
        return res.json()['choices'][0]['message']['content']
    except Exception as e:
        print("OpenAI Text Error:", e)
        return ""

def clean_pdf_text(raw_text):
    lines = raw_text.split('\n')
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line: continue
        if len(line) < 3 and not line.isalnum(): continue
        cleaned.append(line)
    return " ".join(cleaned)

def normalize_date_str(raw_date, fallback_year=None):
    """Normalize any date string to YYYY-MM-DD format.
    Handles: YYYY-MM-DD, YYYY-MM-DD HH:MM:SS, pandas Timestamps,
    Month DD YYYY, DD Month YYYY, etc.
    """
    if not raw_date:
        return datetime.datetime.now().strftime("%Y-%m-%d")
    s = str(raw_date).strip()
    # Already valid YYYY-MM-DD?
    if _re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s
    # Timestamp like "2026-03-01 00:00:00" or ISO "2026-03-01T00:00:00"
    m = _re.match(r'^(\d{4}-\d{2}-\d{2})[T\s]', s)
    if m:
        return m.group(1)
    # Try common formats
    if fallback_year is None:
        fallback_year = datetime.datetime.now().year
    month_names = {
        'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
        'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
        'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
        'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12
    }
    # "March 15, 2026" or "March 15 2026" or "Mar 15"
    m = _re.match(r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]*(\d{4})?', s)
    if m:
        mon = month_names.get(m.group(1).lower())
        if mon:
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else fallback_year
            return f"{year}-{str(mon).zfill(2)}-{str(day).zfill(2)}"
    # "15 March 2026" or "15-Mar-2026"
    m = _re.match(r'(\d{1,2})[\s\-]+([A-Za-z]+)[\s\-,]*(\d{4})?', s)
    if m:
        mon = month_names.get(m.group(2).lower())
        if mon:
            day = int(m.group(1))
            year = int(m.group(3)) if m.group(3) else fallback_year
            return f"{year}-{str(mon).zfill(2)}-{str(day).zfill(2)}"
    # "MM/DD/YYYY" or "DD/MM/YYYY" — assume MM/DD/YYYY
    m = _re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', s)
    if m:
        return f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}"
    # Last resort: return today
    return datetime.datetime.now().strftime("%Y-%m-%d")


# ===================================================================
# CLEAN PIPELINE: Upload → Detect → Extract → OpenAI → JSON → Store
# ===================================================================

basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, template_folder=os.path.join(basedir, 'templates'), static_folder=os.path.join(basedir, 'static'))


def step1_extract_text(filepath, filename):
    """
    STEP 1: Detect file type (PDF / Image) and extract ALL raw text.
    - PDF  → pdfplumber (text + tables)
    - Image → OCR (pytesseract or GPT-4o vision)
    Returns: (raw_text, quality_info)
    """
    raw_text = ""
    table_text = ""
    quality = {"score": "good", "confidence": 85, "warnings": [], "source": "pdf"}

    is_image = filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff'))

    if is_image:
        quality["source"] = "image_ocr"
        ocr_text = ""

        # Method A: Tesseract OCR
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = r'C:\Users\hp\AppData\Local\Programs\Tesseract-OCR\tesseract.exe'
            from PIL import Image
            img = Image.open(filepath)
            ocr_text = pytesseract.image_to_string(img)
            if ocr_text and len(ocr_text.strip()) > 20:
                raw_text = ocr_text
                quality["source"] = "tesseract_ocr"
                quality["confidence"] = 60
                print(f"[OCR] Tesseract extracted {len(ocr_text)} chars")
        except Exception as e:
            print(f"[OCR] Tesseract failed: {e}")

        # Method B: GPT-4o Vision (if OCR got little/no text)
        if len(raw_text.strip()) < 50 and api_key:
            try:
                with open(filepath, 'rb') as f:
                    img_b64 = base64.b64encode(f.read()).decode('utf-8')
                ext = filepath.rsplit('.', 1)[-1].lower()
                mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
                vision_payload = {
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": "You are an expert document reader. Extract EVERY piece of text from this image exactly as written. Preserve tables using | separators. Do not skip anything."},
                        {"role": "user", "content": [
                            {"type": "text", "text": "Read this academic document image completely. Extract ALL text — every date, event name, subject, time, and detail. Be extremely thorough."},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}", "detail": "high"}}
                        ]}
                    ],
                    "max_tokens": 4000
                }
                headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
                res = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=vision_payload, timeout=60)
                res.raise_for_status()
                vision_text = res.json()['choices'][0]['message']['content']
                if vision_text and len(vision_text.strip()) > 20:
                    raw_text = (raw_text + "\n\n--- AI VISION READING ---\n" + vision_text) if raw_text else vision_text
                    quality["source"] = "gpt4o_vision"
                    quality["confidence"] = 90
                    quality["warnings"] = []
                    print(f"[OCR] GPT-4o Vision extracted {len(vision_text)} chars")
            except Exception as e:
                print(f"[OCR] GPT-4o Vision failed: {e}")

        if not raw_text.strip():
            raise ValueError("Could not extract any text from this image. Please upload a clearer image or a PDF.")

    else:
        # PDF: Use pdfplumber
        quality["source"] = "pdfplumber"
        try:
            with pdfplumber.open(filepath) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        raw_text += page_text + "\n"
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table:
                            if row:
                                clean_row = [str(cell).strip() if cell else "" for cell in row]
                                table_text += " | ".join(clean_row) + "\n"
                print(f"[PDF] Extracted {len(raw_text)} chars from {len(pdf.pages)} pages")
        except Exception as e:
            raise ValueError(f"Failed to read PDF: {str(e)}")

        if table_text:
            raw_text += "\n\n--- TABLE DATA ---\n" + table_text

    text_len = len(raw_text.strip())
    if text_len < 10:
        quality["score"] = "poor"
        quality["confidence"] = 10
        quality["warnings"].append("Almost no text could be extracted.")
    elif text_len < 100:
        quality["score"] = "low"
        quality["confidence"] = 40
        quality["warnings"].append("Very little text extracted — results may be incomplete.")

    return raw_text, quality


def step2_openai_understand_and_extract(raw_text, doc_type="calendar", year=None):
    """
    STEP 2: Send extracted text to OpenAI to understand format and extract all events.
    Returns: (events_list, confidence, ai_notes, doc_info)
    """
    if year is None:
        year = datetime.datetime.now().year

    if not api_key:
        print("[AI] No OpenAI API key — using regex fallback")
        events, conf, notes = _regex_fallback_extraction(raw_text, year)
        return events, conf, notes, {"document_type": "unknown", "institution": "", "semester": ""}

    prompt = (
        "You are an expert academic document analyst. You will receive text extracted from a document (like an academic calendar, date sheet, exam schedule, syllabus, or event list).\n\n"
        "YOUR TASK:\n"
        "1. UNDERSTAND the document format completely.\n"
        "2. EXTRACT every single item as structured data (events, exams, holidays, deadlines)\n"
        "   - Use FULL descriptive names (e.g., 'Mathematics-III End Semester Exam' not just 'Exam')\n"
        "   - EXPAND date ranges into individual days if it's a long holiday (e.g., 'Winter Break Dec 20-31' = multiple entries or just provide start and end date)\n"
        "3. CLASSIFY each item: 'exam', 'deadline', 'holiday', or 'event'\n\n"
        "RETURN THIS EXACT JSON STRUCTURE:\n"
        "{\n"
        '  "document_type": "academic_calendar | date_sheet | exam_schedule | event_list | other",\n'
        '  "institution": "University/College name if found",\n'
        '  "semester": "Semester/Term info if found",\n'
        '  "events": [\n'
        '    { "title": "Full Descriptive Name", "date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "type": "exam | deadline | holiday | event", "description": "any details" }\n'
        '  ],\n'
        '  "confidence": 85,\n'
        '  "notes": "any issues or uncertainties",\n'
        '  "total_items_found": 42\n'
        "}\n\n"
        "CRITICAL RULES:\n"
        f"- If year is missing in the document, assume the current year is: {year}\n"
        "- ALL dates ('date' and 'end_date') MUST be in YYYY-MM-DD format.\n"
        "- If it's a single day event, 'date' and 'end_date' should be the same.\n"
        "- DO NOT SKIP ANY ITEMS — extract EVERYTHING. Double check for exams and deadlines.\n"
        "- Return ONLY valid JSON, nothing else."
    )

    ai_data = call_openai_json(prompt, raw_text[:30000])

    events = []
    confidence = 50
    ai_notes = "AI did not return data."
    doc_info = {"document_type": "unknown", "institution": "", "semester": ""}

    if ai_data and isinstance(ai_data, dict):
        confidence = ai_data.get("confidence", 70)
        ai_notes = ai_data.get("notes", "")
        doc_info = {
            "document_type": ai_data.get("document_type", "unknown"),
            "institution": ai_data.get("institution", ""),
            "semester": ai_data.get("semester", "")
        }
        
        # New preferred format: flat list
        events_list = ai_data.get("events", [])
        
        # Fallback if AI still returns date_mapping for some reason
        date_mapping = ai_data.get("date_mapping", None)
        
        if events_list and isinstance(events_list, list):
            for ev in events_list:
                dt = ev.get("date", datetime.datetime.now().strftime("%Y-%m-%d"))
                edt = ev.get("end_date", dt)
                
                # Normalize dates just in case the AI messed up
                normalized_dt = normalize_date_str(dt, year)
                normalized_edt = normalize_date_str(edt, year)
                
                events.append({
                    "title": ev.get("title", "Event"),
                    "date": normalized_dt,
                    "end_date": normalized_edt,
                    "type": ev.get("type", "event"),
                    "description": ev.get("description", "")
                })
        elif date_mapping and isinstance(date_mapping, dict):
            for date_str, events_on_day in date_mapping.items():
                if not isinstance(events_on_day, list):
                    continue
                normalized_date = normalize_date_str(date_str, year)
                for ev in events_on_day:
                    events.append({
                        "title": ev.get("title", "Event"),
                        "date": normalized_date,
                        "end_date": normalized_date,
                        "type": ev.get("type", "event"),
                        "description": ev.get("description", "")
                    })

    if not events:
        events, confidence, ai_notes = _regex_fallback_extraction(raw_text, year)

    # Deduplicate by (date, title)
    unique = {}
    for ev in events:
        key = (ev['date'], ev['title'].strip().lower())
        if key not in unique:
            unique[key] = ev
    events = sorted(list(unique.values()), key=lambda x: x['date'])

    return events, confidence, ai_notes, doc_info


def _regex_fallback_extraction(raw_text, year):
    """Fallback: extract events using regex when AI is unavailable."""
    events = []
    date_pattern = re.compile(
        r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|'
        r'Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'\s+(\d{1,2})(?:st|nd|rd|th)?'
        r'(?:[,\s]+(\d{4}))?\b',
        re.IGNORECASE
    )
    for line in raw_text.split('\n'):
        match = date_pattern.search(line)
        if match:
            raw_date_str = match.group(0)
            parsed_date = normalize_date_str(raw_date_str, year)
            line_lower = line.lower()
            etype = "event"
            if 'exam' in line_lower:
                etype = "exam"
            elif 'deadline' in line_lower or 'submission' in line_lower:
                etype = "deadline"
            elif 'holiday' in line_lower:
                etype = "holiday"
            events.append({
                "title": line.strip()[:80],
                "date": parsed_date,
                "end_date": parsed_date,
                "type": etype,
                "description": ""
            })
    return events, 30, "AI unavailable — used regex fallback. Results may be incomplete."


def ai_extract_timetable(enhanced_text):
    """AI-powered weekly timetable extraction.
    Returns (timetable_list, confidence, ai_notes) tuple."""
    prompt = (
        "CRITICAL: Extract the COMPLETE weekly class timetable from this document.\n"
        "You MUST extract EVERY SINGLE class/period for EVERY day of the week.\n"
        "DO NOT SKIP ANY CLASSES OR PERIODS.\n\n"
        "IMPORTANT RULES:\n"
        "1. Extract ALL classes for each day (Monday through Sunday)\n"
        "2. Use the EXACT subject names from the document\n"
        "3. Ensure time ranges are in 24-hour format (e.g., '10:00-11:00')\n"
        "4. If a class has a room number or section, include it in the subject name\n\n"
        "Return STRICT JSON:\n"
        "{\n"
        '  "timetable": {\n'
        '    "Monday": [ { "time": "09:00-10:00", "subject": "Mathematics-III" } ],\n'
        '    "Tuesday": [ { "time": "09:00-10:00", "subject": "English" } ]\n'
        "  },\n"
        '  "confidence": 85,\n'
        '  "notes": "any issues"\n'
        "}\n\n"
        "Include ALL classes for ALL days. Return ONLY the JSON."
    )

    ai_data = call_openai_json(prompt, enhanced_text[:30000])

    weekly = []
    confidence = 50
    ai_notes = "AI extraction did not return data."
    day_map = {"Sunday": 0, "Monday": 1, "Tuesday": 2, "Wednesday": 3, "Thursday": 4, "Friday": 5, "Saturday": 6}

    timetable_data = None
    if ai_data and isinstance(ai_data, dict):
        confidence = ai_data.get("confidence", 70)
        ai_notes = ai_data.get("notes", "")
        timetable_data = ai_data.get("timetable", None)
        if timetable_data is None:
            for key in ai_data:
                if key.strip().capitalize() in day_map:
                    timetable_data = {k: v for k, v in ai_data.items() if k not in ("confidence", "notes")}
                    break

    if timetable_data and isinstance(timetable_data, dict):
        for day_str, periods in timetable_data.items():
            d_idx = day_map.get(day_str.strip().capitalize(), None)
            if d_idx is None:
                for key, val in day_map.items():
                    if key.lower().startswith(day_str.strip().lower()[:3]):
                        d_idx = val
                        break
            if d_idx is None:
                d_idx = 1
            if not isinstance(periods, list):
                continue
            for p in periods:
                if not isinstance(p, dict):
                    continue
                times = p.get("time", "09:00-10:00").split("-")
                start_t = times[0].strip() if len(times) > 0 else "09:00"
                end_t = times[1].strip() if len(times) > 1 else "10:00"
                subject = p.get("subject", p.get("title", "Class"))
                if subject and subject.strip():
                    weekly.append({
                        "day": d_idx,
                        "title": subject.strip(),
                        "start": start_t,
                        "end": end_t
                    })
        weekly = sorted(weekly, key=lambda x: (x['day'], x['start']))

    if not weekly:
        confidence = 0
        ai_notes = "No classes could be extracted. The document may not contain a timetable, or the format was not recognized."

    return weekly, confidence, ai_notes


def save_to_excel(data, columns_map, filepath):
    """Export data list to an Excel file with renamed columns."""
    try:
        import pandas as pd
        df = pd.DataFrame(data)
        if not df.empty:
            df.rename(columns=columns_map, inplace=True)
            df.to_excel(filepath, index=False)
            return True
    except Exception as e:
        print(f"Excel Export Error: {e}")
    return False


# Folders configuration
UPLOAD_FOLDER = os.path.join(basedir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

app.secret_key = 'super_secret_clarity_key' # Added for sessions

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
            "tasks": [], "calendar_events": [], "weekly_timetable": [], "timetable": [], "reflections": [], "focus_sessions": [], "notified_events": [], "calendar_raw_text": ""
        }
    path = get_user_data_path(session['user_id'])
    default_data = {
        "user": {"wake": "07:00", "sleep": "23:00", "commitments": [], "target_study_hours": 4, "streak": 0, "score": 0, "onboarded": False},
        "tasks": [], "calendar_events": [], "weekly_timetable": [], "timetable": [], "reflections": [], "focus_sessions": [], "notified_events": [], "calendar_raw_text": ""
    }
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
                merged = {**default_data, **d}
                # Deep-merge user dict so new default fields aren't lost
                merged['user'] = {**default_data['user'], **d.get('user', {})}
                return merged
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

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_users(users):
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=4)

# Auth routes
@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    users = load_users()
        
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
        
    users = load_users()
    if username in users:
        return jsonify({"success": False, "error": "Username already exists"}), 400
        
    user_id = str(len(users) + 1)
    users[username] = {
        "id": user_id,
        "password": generate_password_hash(password)
    }
    save_users(users)
        
    session['user_id'] = user_id
    session['username'] = username
    return jsonify({"success": True, "is_new": True})

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({"success": True})


# --- HTML ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    return render_template('dashboard.html', user=db_user)

@app.route('/focusmode')
@login_required
def focusmode():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    return render_template('focusmode.html')

# --- API ROUTES: ONBOARDING ---
@app.route('/api/onboard', methods=['POST'])
@login_required
def api_onboard():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    data = request.json
    db_user['wake'] = data.get('wake', '07:00')
    db_user['sleep'] = data.get('sleep', '23:00')
    db_user['commitments'] = data.get('commitments', [])
    db_user['free_slots'] = data.get('free_slots', [])
    db_user['target_study_hours'] = int(data.get('study_hours', 4))
    db_user['onboarded'] = True
    db_user['last_plan_date'] = datetime.datetime.now().strftime("%Y-%m-%d")
    save_data(data_store)
    return jsonify({"success": True, "message": "Onboarding complete", "user": db_user})

# --- API ROUTES: GET DAILY SETUP DATA ---
@app.route('/api/daily_setup', methods=['GET'])
@login_required
def api_daily_setup():
    data_store = load_data()
    db_user = data_store['user']
    return jsonify({
        "success": True,
        "wake": db_user.get('wake', '07:00'),
        "sleep": db_user.get('sleep', '23:00'),
        "commitments": db_user.get('commitments', []),
        "free_slots": db_user.get('free_slots', []),
        "target_study_hours": db_user.get('target_study_hours', 4),
        "last_plan_date": db_user.get('last_plan_date', '')
    })

# --- API ROUTES: TASKS ---
@app.route('/api/tasks', methods=['GET', 'POST'])
@login_required
def api_tasks():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    if request.method == 'POST':
        data = request.json
        # Use max existing ID + 1 to avoid collisions if tasks were deleted
        task_id = (max((t['id'] for t in db_tasks), default=0) + 1)
        
        # Rule-based logic for AI estimation (if manual wasn't provided directly)
        estimated_time = data.get('estimated_time')
        if not estimated_time:
            study_type = data.get('study_type', 'reading')
            if study_type == 'reading':
                estimated_time = 30
            elif study_type == 'understanding':
                estimated_time = 60
            elif study_type == 'exam':
                estimated_time = 120
            else:
                estimated_time = 45
                
        new_task = {
            "id": task_id,
            "title": data.get('title', 'Untitled Task'),
            "desc": data.get('description', ''),
            "deadline": data.get('deadline', ''),
            "priority": data.get('priority', 'Medium'),
            "type": data.get('study_type', 'reading'),
            "estimated_time": int(estimated_time or 30),
            "status": "pending",
            "material": data.get('material'),
            "material_link": data.get('material_link'),
            "created_at": datetime.datetime.now().isoformat()
        }
        db_tasks.append(new_task)
        
        # Check deadline risk
        risk_warning = None
        if new_task['deadline']:
            dead_date = datetime.datetime.strptime(new_task['deadline'], "%Y-%m-%d")
            diff = (dead_date - datetime.datetime.now()).days
            if diff < 3 and new_task['estimated_time'] > 120:
                risk_warning = "At your current pace, you may miss your deadline."

        save_data(data_store)
        return jsonify({"success": True, "task": new_task, "risk_warning": risk_warning})
        
    return jsonify({"success": True, "tasks": db_tasks})

@app.route('/api/tasks/<int:task_id>', methods=['PUT'])
@login_required
def api_update_task(task_id):

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    data = request.json
    for task in db_tasks:
        if task['id'] == task_id:
            task['title'] = data.get('title', task['title'])
            task['deadline'] = data.get('deadline', task['deadline'])
            task['priority'] = data.get('priority', task['priority'])
            task['estimated_time'] = int(data.get('estimated_time', task['estimated_time']))
            if 'material' in data: task['material'] = data['material']
            if 'material_link' in data: task['material_link'] = data['material_link']
            save_data(data_store)
            return jsonify({"success": True, "task": task})
    return jsonify({"success": False, "error": "Task not found"}), 404

# --- API ROUTES: CALENDAR & PDF ---
@app.route('/api/upload_calendar', methods=['POST'])
@login_required
def api_upload_calendar():
    """
    CLEAN PIPELINE:
    Upload File → Detect Type → Extract Text → Send to OpenAI → Get JSON → Clean → Store → Display
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    now = datetime.datetime.now()

    try:
        # ========== STEP 1: Extract Text (pdfplumber / OCR) ==========
        print(f"\n[PIPELINE] Starting calendar upload: {filename}")
        raw_text, quality = step1_extract_text(filepath, filename)
        print(f"[PIPELINE] Step 1 done: {len(raw_text)} chars, source={quality['source']}, confidence={quality['confidence']}")

        if quality["score"] == "poor" and len(raw_text.strip()) < 10:
            return jsonify({
                "success": False,
                "error": "Could not extract any text from this document.",
                "quality": quality,
                "suggestion": "Please upload a clearer image or a text-based PDF."
            }), 400

        # ========== STEP 2: Send to OpenAI (understand format + extract data) ==========
        print(f"[PIPELINE] Step 2: Sending to OpenAI for understanding & extraction...")
        events, confidence, ai_notes, doc_info = step2_openai_understand_and_extract(raw_text, "calendar", now.year)
        print(f"[PIPELINE] Step 2 done: {len(events)} events, confidence={confidence}, doc_type={doc_info.get('document_type', 'unknown')}")

        # ========== STEP 3: Clean + Normalize Data ==========
        # (Already done inside step2 — dates normalized, events deduplicated)

        # ========== STEP 4: Build summaries for frontend ==========
        month_summary = {}
        type_summary = {}
        for ev in events:
            try:
                d = datetime.datetime.strptime(ev['date'], "%Y-%m-%d")
                month_key = d.strftime("%B %Y")
                month_summary[month_key] = month_summary.get(month_key, 0) + 1
            except Exception:
                pass
            type_summary[ev.get('type', 'event')] = type_summary.get(ev.get('type', 'event'), 0) + 1

        # ========== STEP 5: Store (JSON + Excel) ==========
        # Save to Excel
        excel_path = os.path.join(app.config['UPLOAD_FOLDER'], "academic_calendar.xlsx")
        save_to_excel(
            events,
            {"title": "Event Title", "date": "Date", "end_date": "End Date", "type": "Category", "description": "Description"},
            excel_path
        )

        # Store raw text for Clarity AI chat context
        data_store = load_data()
        existing_raw = data_store.get('calendar_raw_text', '')
        data_store['calendar_raw_text'] = existing_raw + "\n\n=== NEW UPLOAD ===\n" + raw_text[:30000]
        save_data(data_store)

        print(f"[PIPELINE] Complete! {len(events)} events ready for display.")

        # ========== STEP 6: Return for Calendar UI display ==========
        return jsonify({
            "success": True,
            "preview": True,
            "events": events,
            "total_events": len(events),
            "confidence": confidence,
            "ai_notes": ai_notes,
            "quality": quality,
            "doc_info": doc_info,
            "month_summary": month_summary,
            "type_summary": type_summary
        })

    except ValueError as e:
        return jsonify({"error": str(e), "quality": {"score": "error"}}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500


# --- CONFIRM CALENDAR EVENTS (save user-reviewed data) ---
@app.route('/api/confirm_calendar_events', methods=['POST'])
@login_required
def api_confirm_calendar_events():
    """Save user-reviewed calendar events to the data store."""
    data_store = load_data()
    db_calendar_events = data_store['calendar_events']

    confirmed_events = request.json.get('events', [])
    if not confirmed_events:
        return jsonify({"error": "No events to save"}), 400

    now = datetime.datetime.now()
    saved = []
    for i, ev in enumerate(confirmed_events):
        saved_event = {
            "id": f"cal-confirmed-{int(now.timestamp()*1000)}-{i}",
            "title": ev.get("title", "Event"),
            "date": normalize_date_str(ev.get("date", ""), now.year),
            "end_date": normalize_date_str(ev.get("end_date", ev.get("date", "")), now.year),
            "type": ev.get("type", "event"),
            "description": ev.get("description", "")
        }
        db_calendar_events.append(saved_event)
        saved.append(saved_event)

    # Export final confirmed events to Excel
    excel_path = os.path.join(app.config['UPLOAD_FOLDER'], "academic_calendar.xlsx")
    save_to_excel(
        saved,
        {"title": "Event Title", "date": "Date", "end_date": "End Date", "type": "Category", "description": "Description"},
        excel_path
    )

    save_data(data_store)
    return jsonify({"success": True, "saved_count": len(saved), "events": saved})


# --- CALENDAR SEARCH ---
@app.route('/api/calendar_search', methods=['GET'])
@login_required
def api_calendar_search():
    """Search calendar events by keyword across title, type, description, and date."""
    data_store = load_data()
    db_calendar_events = data_store['calendar_events']
    query = request.args.get('q', '').strip().lower()

    if not query:
        return jsonify({"success": True, "results": [], "query": ""})

    results = []
    for ev in db_calendar_events:
        title = (ev.get('title', '') or '').lower()
        etype = (ev.get('type', '') or '').lower()
        desc = (ev.get('description', '') or '').lower()
        date_str = (ev.get('date', '') or '').lower()

        # Score relevance
        score = 0
        if query in title:
            score += 10
        if query in etype:
            score += 5
        if query in desc:
            score += 3
        if query in date_str:
            score += 4

        # Fuzzy: check if any word in query partially matches
        query_words = query.split()
        searchable = f"{title} {etype} {desc} {date_str}"
        for word in query_words:
            if len(word) >= 2 and word in searchable:
                score += 2

        # Month name matching
        month_names = {
            'jan': '01', 'january': '01', 'feb': '02', 'february': '02',
            'mar': '03', 'march': '03', 'apr': '04', 'april': '04',
            'may': '05', 'jun': '06', 'june': '06', 'jul': '07', 'july': '07',
            'aug': '08', 'august': '08', 'sep': '09', 'sept': '09', 'september': '09',
            'oct': '10', 'october': '10', 'nov': '11', 'november': '11',
            'dec': '12', 'december': '12'
        }
        for mname, mnum in month_names.items():
            if mname in query and f"-{mnum}-" in date_str:
                score += 8

        if score > 0:
            results.append({**ev, "_score": score})

    # Sort by relevance score descending, then by date
    results.sort(key=lambda x: (-x['_score'], x.get('date', '')))

    # Remove internal score from response
    for r in results:
        r.pop('_score', None)

    return jsonify({"success": True, "results": results, "query": query, "count": len(results)})


# --- AI CALENDAR CHAT ---
@app.route('/api/calendar_chat', methods=['POST'])
@login_required
def api_calendar_chat():
    """AI chatbot that can answer questions about the uploaded calendar."""
    data_store = load_data()
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    calendar_raw_text = data_store.get('calendar_raw_text', '')

    user_message = request.json.get('message', '').strip()
    chat_history = request.json.get('history', [])

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    if not api_key:
        return jsonify({"error": "AI is not configured (missing API key)"}), 500

    # Build calendar context for AI
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    day_of_week = now.strftime("%A")

    # Format all calendar events as structured text
    events_text = ""
    if db_calendar_events:
        events_by_date = {}
        for ev in db_calendar_events:
            d = ev.get('date', 'Unknown')
            if d not in events_by_date:
                events_by_date[d] = []
            events_by_date[d].append(ev)

        for date_key in sorted(events_by_date.keys()):
            events_text += f"\n\ud83d\udcc5 {date_key}:\n"
            for ev in events_by_date[date_key]:
                events_text += f"  - {ev.get('title', 'Event')} [{ev.get('type', 'event')}]"
                if ev.get('description'):
                    events_text += f" \u2014 {ev['description']}"
                events_text += "\n"
    else:
        events_text = "No calendar events uploaded yet."

    # Weekly timetable context
    timetable_text = ""
    if db_weekly_timetable:
        day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        tt_by_day = {}
        for cls in db_weekly_timetable:
            day_idx = cls.get('day', 1)
            day_name = day_names[day_idx] if 0 <= day_idx < 7 else 'Unknown'
            if day_name not in tt_by_day:
                tt_by_day[day_name] = []
            tt_by_day[day_name].append(cls)

        for day_name in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
            if day_name in tt_by_day:
                timetable_text += f"\n{day_name}:\n"
                for cls in sorted(tt_by_day[day_name], key=lambda x: x.get('start', '')):
                    timetable_text += f"  {cls.get('start', '?')}-{cls.get('end', '?')} : {cls.get('title', 'Class')}\n"

    # Upcoming events (next 30 days)
    upcoming_text = ""
    for ev in db_calendar_events:
        try:
            ev_date = datetime.datetime.strptime(ev['date'], "%Y-%m-%d").date()
            diff = (ev_date - now.date()).days
            if 0 <= diff <= 30:
                upcoming_text += f"  - In {diff} day(s): {ev['title']} [{ev['type']}] on {ev['date']}\n"
        except Exception:
            pass

    # Include raw document text (truncated) for deeper AI understanding
    raw_doc_context = ""
    if calendar_raw_text:
        # Take the most recent 8000 chars of raw text
        raw_doc_context = f"\n=== RAW DOCUMENT TEXT (from uploaded PDF/image) ===\n{calendar_raw_text[-8000:]}\n"

    system_prompt = (
        "You are Clarity AI, an intelligent and friendly academic calendar assistant. "
        "You have COMPLETE access to the user's academic calendar, weekly class timetable, "
        "and the RAW text extracted from their uploaded documents (PDFs, date sheets, exam schedules).\n\n"
        "CAPABILITIES:\n"
        "- Tell the user what events/exams/holidays are on ANY specific date\n"
        "- List upcoming exams, deadlines, or holidays (today, this week, this month, etc.)\n"
        "- Count events by type (how many exams, holidays, etc.) or month\n"
        "- Show today's or any day's complete schedule including classes\n"
        "- Give study planning advice based on upcoming deadlines and exams\n"
        "- Answer questions about the original uploaded documents\n"
        "- Calculate days remaining until specific events\n"
        "- Identify free days (days with no events)\n"
        "- Suggest which subjects to prioritize based on exam proximity\n"
        "- Warn about overlapping events or tight schedules\n\n"
        "RULES:\n"
        "- Be concise but THOROUGH \u2014 don't leave out important details\n"
        "- Use emojis to make responses friendly and scannable\n"
        "- If asked about something not in the calendar, say so clearly\n"
        "- Format dates nicely (e.g., 'Monday, April 21, 2026')\n"
        "- When listing multiple events, organize them chronologically\n"
        "- If the user hasn't uploaded a calendar yet, tell them to upload one first\n"
        "- For exam-related questions, always mention the subject name and date\n"
        "- If the user asks 'what do I need to do', list all upcoming deadlines and exams\n"
        "- Be proactive: if you notice an exam is coming soon, mention it!\n\n"
        f"TODAY: {today_str} ({day_of_week})\n\n"
        f"=== ACADEMIC CALENDAR EVENTS ({len(db_calendar_events)} total) ==="
        f"{events_text}\n\n"
        f"=== UPCOMING EVENTS (next 30 days) ==="
        f"\n{upcoming_text if upcoming_text else '  No upcoming events in the next 30 days.'}\n\n"
        f"=== WEEKLY CLASS TIMETABLE ==="
        f"\n{timetable_text if timetable_text else '  No weekly timetable set up.'}\n"
        f"{raw_doc_context}"
    )

    # Build messages with chat history
    messages = [{"role": "system", "content": system_prompt}]

    # Add recent chat history (last 10 messages)
    for msg in chat_history[-10:]:
        messages.append({
            "role": msg.get("role", "user"),
            "content": msg.get("content", "")
        })

    messages.append({"role": "user", "content": user_message})

    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        payload = {
            "model": "gpt-4o",
            "messages": messages,
            "max_tokens": 1500,
            "temperature": 0.7
        }
        res = requests.post(url, headers=headers, json=payload, timeout=45)
        res.raise_for_status()
        ai_response = res.json()['choices'][0]['message']['content']

        return jsonify({
            "success": True,
            "response": ai_response,
            "events_count": len(db_calendar_events)
        })
    except Exception as e:
        print(f"Calendar Chat Error: {e}")
        return jsonify({"error": f"AI response failed: {str(e)}"}), 500



# --- CALENDAR CRUD ENDPOINTS ---
@app.route('/api/calendar', methods=['GET', 'POST'])
@login_required
def api_calendar():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    if request.method == 'GET':
        return jsonify({"success": True, "events": db_calendar_events})
    
    if request.method == 'POST':
        data = request.json
        new_event = {
            "id": f"cal-user-{int(datetime.datetime.now().timestamp()*1000)}",
            "title": data.get('title', 'Untitled'),
            "date": data.get('date', datetime.datetime.now().strftime("%Y-%m-%d")),
            "end_date": data.get('end_date', data.get('date', datetime.datetime.now().strftime("%Y-%m-%d"))),
            "type": data.get('type', 'event')
        }
        db_calendar_events.append(new_event)
        save_data(data_store)
        return jsonify({"success": True, "event": new_event})

@app.route('/api/calendar/<event_id>', methods=['PUT', 'DELETE'])
@login_required
def api_calendar_item(event_id):

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    if request.method == 'PUT':
        data = request.json
        for ev in db_calendar_events:
            if str(ev['id']) == str(event_id):
                ev['title'] = data.get('title', ev['title'])
                ev['date'] = data.get('date', ev['date'])
                ev['end_date'] = data.get('end_date', ev.get('end_date', ev['date']))
                ev['type'] = data.get('type', ev['type'])
                save_data(data_store)
                return jsonify({"success": True, "event": ev})
        return jsonify({"error": "Event not found"}), 404
        
    if request.method == 'DELETE':
        db_calendar_events[:] = [ev for ev in db_calendar_events if str(ev['id']) != str(event_id)]
        save_data(data_store)
        return jsonify({"success": True})

# --- WEEKLY TIMETABLE ENDPOINTS ---
@app.route('/api/weekly_timetable', methods=['GET', 'POST'])
@login_required
def api_weekly_timetable():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    if request.method == 'GET':
        return jsonify({"success": True, "timetable": db_weekly_timetable})
    if request.method == 'POST':
        data = request.json
        if isinstance(data, list):
            db_weekly_timetable.clear()
            db_weekly_timetable.extend(data)
            save_data(data_store)
            return jsonify({"success": True})
        return jsonify({"error": "Expected a list"}), 400

# --- NOTIFICATIONS ENDPOINT ---
@app.route('/api/notifications', methods=['GET'])
@login_required
def api_notifications():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    now = datetime.datetime.now().date()
    upcoming = []
    
    # Check Calendar Events (up to 3 days ahead)
    for ev in db_calendar_events:
        try:
            ev_date = datetime.datetime.strptime(ev['date'], "%Y-%m-%d").date()
            diff = (ev_date - now).days
            if 0 <= diff <= 3:
                n_id = f"ev_{ev['id']}"
                if n_id not in db_notified_events:
                    upcoming.append({"id": n_id, "title": ev['title'], "type": "event", "date": ev['date'], "days_left": diff})
        except ValueError:
            pass
            
    # Check Task Deadlines
    for task in db_tasks:
        if task['status'] == 'completed' or not task['deadline']: continue
        try:
            task_date = datetime.datetime.strptime(task['deadline'], "%Y-%m-%d").date()
            diff = (task_date - now).days
            if 0 <= diff <= 3:
                n_id = f"tsk_{task['id']}"
                if n_id not in db_notified_events:
                    upcoming.append({"id": n_id, "title": task['title'], "type": "task", "date": task['deadline'], "days_left": diff})
        except ValueError:
            pass
            
    upcoming.sort(key=lambda x: x['days_left'])
    return jsonify({"success": True, "notifications": upcoming})

@app.route('/api/notifications/dismiss', methods=['POST'])
@login_required
def dismiss_notification():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    notif_id = request.json.get('id')
    if notif_id and notif_id not in db_notified_events:
        db_notified_events.append(notif_id)
        save_data(data_store)
    return jsonify({"success": True})

@app.route('/api/upload_study_material', methods=['POST'])
@login_required
def api_upload_study_material():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files['file']
    study_type = request.form.get('study_type', 'reading')
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
    file.save(filepath)
    
    try:
        filename_sm = secure_filename(file.filename)
        full_text, _quality = step1_extract_text(filepath, filename_sm)
        full_text = clean_pdf_text(full_text)
        words = full_text.split()
        word_count = len(words)
        
        # Rule-based calculation (mins)
        rule_read = max(15, math.ceil(word_count / 200)) 
        rule_understand = max(30, math.ceil(word_count / 100))
        rule_exam = max(60, math.ceil(word_count / 50))
        
        # Chunking if > 2500 words
        processed_text = full_text
        if word_count > 2500:
            chunks = [' '.join(words[i:i+2000]) for i in range(0, len(words), 2000)]
            summaries = []
            for chunk in chunks[:5]: # Hard limit to prevent massive API bills
                sum_text = call_openai_text("Summarize the key educational concepts concisely.", chunk)
                if sum_text: summaries.append(sum_text)
            processed_text = " ".join(summaries) if summaries else full_text[:8000]

        prompt = """Analyze the provided study material document. Determine the overall length and complexity.
Provide realistic, exhaustive time estimates (in HOURS as raw numbers) for a student to study this.
You MUST provide THREE distinct estimates: basic reading, deep understanding, and exam preparation.
Provide a subjective difficulty rating ('easy', 'medium', 'hard'), confidence matching this exact casing, and a short reasoning.

Return STRICT JSON formatted exactly like this:
{
  "reading_time_hours": 0.5,
  "understanding_time_hours": 1.5,
  "exam_prep_time_hours": 3.0,
  "difficulty": "medium",
  "confidence": 85,
  "reasoning": "short explanation"
}"""

        ai_data = call_openai_json(prompt, processed_text[:30000])
        sections = []
        final_mins = 0
        
        if ai_data and isinstance(ai_data, dict) and "reading_time_hours" in ai_data:
            # Map the new structure to UI, convert to minutes
            ai_map = {
                "reading": int(float(ai_data.get("reading_time_hours", 0.5)) * 60),
                "understanding": int(float(ai_data.get("understanding_time_hours", 1.5)) * 60),
                "exam": int(float(ai_data.get("exam_prep_time_hours", 3.0)) * 60)
            }
            
            # Hybrid Calculation
            hybrid_map = {
                "reading": int((ai_map["reading"] * 0.7) + (rule_read * 0.3)),
                "understanding": int((ai_map["understanding"] * 0.7) + (rule_understand * 0.3)),
                "exam": int((ai_map["exam"] * 0.7) + (rule_exam * 0.3))
            }
            
            t_val = hybrid_map.get(study_type, hybrid_map["reading"])
            final_mins += t_val
            
            sections.append({
                "title": "Full Material Profile",
                "time": t_val,
                "all_times": hybrid_map,
                "difficulty": str(ai_data.get("difficulty", "medium")).title(),
                "reasoning": ai_data.get("reasoning", "Standard text"),
                "confidence": ai_data.get("confidence", 80),
                "is_ai": True
            })
        else:
            # Fallback logic if AI fails/invalid JSON
            base_map = {"reading": rule_read, "understanding": rule_understand, "exam": rule_exam}
            t_val = base_map.get(study_type, rule_read)
            final_mins += t_val
            sections = [{"title": "Full Document", "time": t_val, "all_times": base_map, "difficulty": "Medium", "confidence": 50, "reasoning": "Fallback to rule-based estimate (AI unavailable).", "is_ai": False}]
            
        return jsonify({"success": True, "estimated_time": final_mins, "word_count": word_count, "sections": sections})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/upload_weekly_pdf', methods=['POST'])
@login_required
def api_upload_weekly_pdf():
    """AI-powered weekly timetable extraction — returns preview data for user review."""
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        # Step 1: Extract raw text with quality assessment
        raw_text, quality = step1_extract_text(filepath, filename)

        if quality["score"] == "poor" and len(raw_text.strip()) < 10:
            return jsonify({
                "success": False,
                "error": "Could not extract text from this document.",
                "quality": quality,
                "suggestion": "Please upload a clearer image or text-based PDF."
            }), 400

        # Step 2: AI extraction with confidence
        timetable, confidence, ai_notes = ai_extract_timetable(raw_text)

        # Step 4: Export to Excel (preview)
        day_map_inv = {0:"Sun", 1:"Mon", 2:"Tue", 3:"Wed", 4:"Thu", 5:"Fri", 6:"Sat"}
        excel_data = [{"day": day_map_inv.get(w["day"], "?"), "title": w["title"], "start": w["start"], "end": w["end"]} for w in timetable]
        excel_path = os.path.join(app.config['UPLOAD_FOLDER'], "weekly_timetable.xlsx")
        save_to_excel(
            excel_data,
            {"day": "Day", "title": "Subject", "start": "Start Time", "end": "End Time"},
            excel_path
        )

        # Return preview — NOT saved yet
        return jsonify({
            "success": True,
            "preview": True,
            "timetable": timetable,
            "total_classes": len(timetable),
            "confidence": confidence,
            "ai_notes": ai_notes,
            "quality": quality
        })

    except ValueError as e:
        return jsonify({"error": str(e), "quality": {"score": "error"}}), 400
    except Exception as e:
        return jsonify({"error": f"Processing failed: {str(e)}"}), 500


# --- CONFIRM WEEKLY TIMETABLE (save user-reviewed data) ---
@app.route('/api/confirm_weekly_timetable', methods=['POST'])
@login_required
def api_confirm_weekly_timetable():
    """Save user-reviewed weekly timetable to data store."""
    data_store = load_data()
    db_weekly_timetable = data_store['weekly_timetable']

    confirmed = request.json.get('timetable', [])
    if not confirmed:
        return jsonify({"error": "No timetable data to save"}), 400

    db_weekly_timetable.clear()
    db_weekly_timetable.extend(confirmed)

    # Export to Excel
    day_map_inv = {0:"Sun", 1:"Mon", 2:"Tue", 3:"Wed", 4:"Thu", 5:"Fri", 6:"Sat"}
    excel_data = [{"day": day_map_inv.get(w.get("day", 1), "?"), "title": w.get("title", ""), "start": w.get("start", ""), "end": w.get("end", "")} for w in confirmed]
    excel_path = os.path.join(app.config['UPLOAD_FOLDER'], "weekly_timetable.xlsx")
    save_to_excel(
        excel_data,
        {"day": "Day", "title": "Subject", "start": "Start Time", "end": "End Time"},
        excel_path
    )

    save_data(data_store)
    return jsonify({"success": True, "saved_count": len(confirmed)})


# --- API ROUTES: TIMETABLE ---
@app.route('/api/generate_timetable', methods=['POST'])
@login_required
def api_generate_timetable():
    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_timetable = data_store['timetable']

    tasks_to_schedule = [t for t in db_tasks if t['status'] == 'pending']

    if not tasks_to_schedule:
        return jsonify({"success": True, "timetable": [], "message": "No pending tasks to schedule."})

    # Collect user availability
    free_slots = sorted(db_user.get('free_slots', []), key=lambda x: x.get('from', '00:00'))
    if not free_slots:
        free_slots = [{"from": "16:00", "to": "20:00"}]

    # Try AI-powered task ordering
    ai_ordered_ids = None
    try:
        task_info = []
        for t in tasks_to_schedule:
            task_info.append({
                "id": t['id'],
                "title": t['title'],
                "priority": t['priority'],
                "deadline": t.get('deadline', 'none'),
                "estimated_minutes": t.get('estimated_time', 30)
            })

        now = datetime.datetime.now()
        ai_prompt = (
            "You are a smart study planner AI. Given the following tasks and free time slots, "
            "determine the OPTIMAL ORDER to schedule these tasks for maximum productivity.\n\n"
            "RULES:\n"
            "1. Urgent deadlines first (closest deadline = highest urgency)\n"
            "2. High priority tasks should go in early slots (when energy is highest)\n"
            "3. If a task is > 60 min, it should be split into chunks\n"
            "4. Lighter/easier tasks can go in later slots\n"
            "5. Mix difficult and easy tasks to avoid burnout\n\n"
            f"Current date/time: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"Free slots: {json.dumps(free_slots)}\n"
            f"Tasks: {json.dumps(task_info)}\n\n"
            "Return a JSON object with key 'task_order' — an array of task IDs "
            "in the order they should be scheduled. Example:\n"
            '{"task_order": [3, 1, 5, 2, 4]}'
        )

        ai_result = call_openai_json(ai_prompt, "")
        if ai_result and 'task_order' in ai_result:
            ai_ordered_ids = ai_result['task_order']
    except Exception as e:
        print(f"AI ordering failed, using fallback: {e}")

    # Apply AI ordering or fallback to deadline+priority sort
    if ai_ordered_ids:
        id_to_task = {t['id']: t for t in tasks_to_schedule}
        ordered = []
        for tid in ai_ordered_ids:
            if tid in id_to_task:
                ordered.append(id_to_task[tid])
                del id_to_task[tid]
        # Add any tasks AI missed
        ordered.extend(id_to_task.values())
        tasks_to_schedule = ordered
    else:
        priority_map = {"High": 3, "Medium": 2, "Low": 1}
        tasks_to_schedule.sort(key=lambda x: (
            x.get('deadline', '9999-12-31'),
            -priority_map.get(x.get('priority', 'Low'), 1)
        ))

    timetable = []

    # Build available blocks across multiple days
    def get_available_blocks():
        now_dt = datetime.datetime.now()
        day_offset = 0
        while day_offset <= 30:
            d_str = (now_dt.date() + datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d ")
            date_str = (now_dt.date() + datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d")
            for slot in free_slots:
                try:
                    start_dt = datetime.datetime.strptime(d_str + slot['from'], "%Y-%m-%d %H:%M")
                    end_dt = datetime.datetime.strptime(d_str + slot['to'], "%Y-%m-%d %H:%M")
                    if end_dt > start_dt:
                        if end_dt > now_dt:
                            start_dt = max(start_dt, now_dt)
                            yield {"start": start_dt, "end": end_dt, "date": date_str, "carry_forward": day_offset > 0}
                except Exception:
                    pass
            day_offset += 1

    block_gen = get_available_blocks()

    try:
        current_block = next(block_gen)
        current_time = current_block['start']
        end_of_slot = current_block['end']
        is_carry = current_block['carry_forward']
        block_date = current_block['date']
    except StopIteration:
        return jsonify({"success": True, "timetable": [], "message": "No available time slots found."})

    consecutive_study_mins = 0
    priority_colors = {"High": "#EF4444", "Medium": "#F59E0B", "Low": "#6366F1"}

    for task in tasks_to_schedule:
        mins_remaining = task.get('estimated_time', 30)
        part_num = 1

        while mins_remaining > 0:
            slot_duration = (end_of_slot - current_time).total_seconds() / 60

            if slot_duration <= 0:
                try:
                    current_block = next(block_gen)
                    current_time = current_block['start']
                    end_of_slot = current_block['end']
                    is_carry = current_block['carry_forward']
                    block_date = current_block['date']
                    consecutive_study_mins = 0
                    continue
                except StopIteration:
                    break

            # Insert break if studied > 45 min consecutively
            if consecutive_study_mins >= 45:
                break_dur = 15 if consecutive_study_mins >= 90 else 10
                break_end = current_time + datetime.timedelta(minutes=break_dur)

                if break_end <= end_of_slot:
                    timetable.append({
                        "id": len(timetable) + 1,
                        "task_id": None,
                        "title": "☕ Break" if break_dur <= 10 else "🧘 Long Break",
                        "start": current_time.strftime("%H:%M"),
                        "end": break_end.strftime("%H:%M"),
                        "date": block_date,
                        "day_label": current_time.strftime("%a") if is_carry else "Today",
                        "duration": break_dur,
                        "priority": "break",
                        "color": "#D1FAE5",
                        "carry_forward": is_carry,
                        "is_break": True
                    })
                    current_time = break_end
                    consecutive_study_mins = 0
                    slot_duration = (end_of_slot - current_time).total_seconds() / 60
                    if slot_duration <= 0:
                        continue

            allocable_mins = min(mins_remaining, slot_duration, 60)  # Max 60 min chunks

            if allocable_mins < 10 and mins_remaining > allocable_mins:
                try:
                    current_block = next(block_gen)
                    current_time = current_block['start']
                    end_of_slot = current_block['end']
                    is_carry = current_block['carry_forward']
                    block_date = current_block['date']
                    consecutive_study_mins = 0
                    continue
                except StopIteration:
                    break

            projected_end = current_time + datetime.timedelta(minutes=allocable_mins)

            title = task['title']
            if mins_remaining > allocable_mins or part_num > 1:
                title += f" (Part {part_num})"

            timetable.append({
                "id": len(timetable) + 1,
                "task_id": task['id'],
                "title": title,
                "start": current_time.strftime("%H:%M"),
                "end": projected_end.strftime("%H:%M"),
                "date": block_date,
                "day_label": current_time.strftime("%a") if is_carry else "Today",
                "duration": int(allocable_mins),
                "priority": task['priority'],
                "color": priority_colors.get(task['priority'], "#6366F1"),
                "carry_forward": is_carry,
                "is_break": False,
                "deadline": task.get('deadline', '')
            })

            mins_remaining -= allocable_mins
            part_num += 1
            consecutive_study_mins += allocable_mins
            current_time = projected_end

    # Calculate summary stats
    total_study = sum(t['duration'] for t in timetable if not t.get('is_break'))
    total_breaks = sum(t['duration'] for t in timetable if t.get('is_break'))
    tasks_covered = len(set(t['task_id'] for t in timetable if t.get('task_id')))

    db_timetable.clear()
    db_timetable.extend(timetable)
    save_data(data_store)

    return jsonify({
        "success": True,
        "timetable": timetable,
        "summary": {
            "total_study_mins": total_study,
            "total_break_mins": total_breaks,
            "tasks_covered": tasks_covered,
            "total_tasks": len([t for t in db_tasks if t['status'] == 'pending'])
        }
    })


# --- FILE SERVING FOR STUDY HUB ---
@app.route('/uploads/<path:filename>')
@login_required
def serve_upload(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/study_files', methods=['GET'])
@login_required
def api_study_files():
    """List all uploaded files for Study Hub."""
    upload_dir = app.config['UPLOAD_FOLDER']
    files = []
    if os.path.exists(upload_dir):
        for fname in os.listdir(upload_dir):
            fpath = os.path.join(upload_dir, fname)
            if os.path.isfile(fpath):
                ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
                # Skip system files
                if ext in ('xlsx',):
                    continue
                ftype = 'pdf' if ext == 'pdf' else 'image' if ext in ('png','jpg','jpeg','gif','webp') else 'other'
                stat = os.stat(fpath)
                files.append({
                    "name": fname,
                    "url": f"/uploads/{fname}",
                    "type": ftype,
                    "extension": ext,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "uploaded": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                })
    # Sort by upload date descending
    files.sort(key=lambda x: x['uploaded'], reverse=True)
    return jsonify({"success": True, "files": files})


# --- API ROUTES: FOCUS & REFLECTION ---
@app.route('/api/focus_session', methods=['POST'])
@login_required
def api_focus_session():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    data = request.json
    db_focus_sessions.append({
        "task_id": data.get('task_id'),
        "duration_mins": data.get('duration_mins', 0),
        "date": datetime.datetime.now().isoformat()
    })
    
    # Mark task as completed if provided
    task_id = data.get('task_id')
    for t in db_tasks:
        if str(t['id']) == str(task_id):
            t['status'] = 'completed'
            db_user['score'] += 10 # Productivity engine logic
            break
            
    save_data(data_store)
    return jsonify({"success": True})

@app.route('/api/reflection', methods=['POST'])
@login_required
def api_reflection():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    data = request.json
    db_reflections.append({
        "date": datetime.datetime.now().isoformat(),
        "well": data.get('well', ''),
        "wasted": data.get('wasted', ''),
        "focus": int(data.get('focus', 5)),
        "energy": int(data.get('energy', 5)),
        "mood": data.get('mood', 'Neutral'),
        "distraction": data.get('distraction', ''),
        "improvement": data.get('improvement', '')
    })
    # Basic streak logic
    db_user['streak'] += 1
    db_user['score'] += 5
    
    save_data(data_store)
    return jsonify({"success": True, "streak": db_user['streak'], "score": db_user['score']})

@app.route('/api/stats', methods=['GET'])
@login_required
def api_stats():

    data_store = load_data()
    db_user = data_store['user']
    db_tasks = data_store['tasks']
    db_calendar_events = data_store['calendar_events']
    db_weekly_timetable = data_store['weekly_timetable']
    db_timetable = data_store['timetable']
    db_reflections = data_store['reflections']
    db_focus_sessions = data_store['focus_sessions']
    db_notified_events = data_store['notified_events']
    planned = sum(t['estimated_time'] for t in db_tasks)
    actual = sum(f['duration_mins'] for f in db_focus_sessions)
    completed = len([t for t in db_tasks if t['status'] == 'completed'])
    total = len(db_tasks)
    
    # Advanced Analytics & Strict Fallback
    # Build chart data even with no reflections
    target_daily_mins_early = db_user.get('target_study_hours', 4) * 60
    day_names_early = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    today_early = datetime.datetime.now().date()
    monday_early = today_early - datetime.timedelta(days=today_early.weekday())
    wk_labels, wk_planned, wk_actual = [], [], []
    for i in range(7):
        d = monday_early + datetime.timedelta(days=i)
        wk_labels.append(day_names_early[i])
        wk_planned.append(target_daily_mins_early)
        wk_actual.append(sum(f.get('duration_mins', 0) for f in db_focus_sessions if f.get('date', '').startswith(d.isoformat())))
    
    if len(db_reflections) == 0:
        return jsonify({
            "success": True, "planned_mins": planned, "actual_mins": actual, "tasks_completed": completed, "tasks_total": total,
            "score": db_user['score'], "streak": db_user['streak'], "avg_focus": 0, "consistency": 0,
            "main_distraction": "Not enough data yet", "energy_correlation": "Not enough data yet", "insights": [],
            "target_daily_mins": target_daily_mins_early,
            "weekly_chart": {"labels": wk_labels, "planned": wk_planned, "actual": wk_actual},
            "monthly_chart": {"labels": [], "values": []}
        })
        
    focus_scores = [r.get('focus', 0) for r in db_reflections if 'focus' in r]
    avg_focus = sum(focus_scores) / len(focus_scores) * 10
    
    now = datetime.datetime.now().date()
    active_days = set()
    for f in db_focus_sessions:
        try:
            d = datetime.datetime.fromisoformat(f['date']).date()
            if (now - d).days <= 7: active_days.add(d)
        except: pass
    consistency = len(active_days) / 7.0 * 100

    distractions = [r.get('distraction', '') for r in db_reflections if r.get('distraction')]
    top_distraction = max(set(distractions), key=distractions.count) if distractions else ""
    distraction_msg = top_distraction if top_distraction else "None reported"

    energy_levels = [r.get('energy', 5) for r in db_reflections if 'energy' in r]
    avg_energy = sum(energy_levels) / len(energy_levels)
    energy_msg = "Stable energy overall."
    if avg_energy > 7: energy_msg = "Your energy limits are currently high."
    elif avg_energy < 4: energy_msg = "You've been experiencing low-energy days."

    # Rule-Based Insights Engine
    insights = []
    if avg_focus < 50 and top_distraction:
        insights.append(f"You are getting distracted frequently by {top_distraction}. Try putting this away during focus hours.")
    if avg_energy > 7 and avg_focus > 70:
        insights.append("You perform best when energy is high. Keep maintaining your sleep schedule.")
    if consistency < 40:
        insights.append("Your study consistency is low this week. Try to do at least one 25-minute pomodoro daily.")
        
    # Deadline Risk Detector
    pending_tasks = [t for t in db_tasks if t['status'] == 'pending' and t.get('deadline')]
    if pending_tasks:
        pending_tasks.sort(key=lambda x: x['deadline'])
        nearest_date = datetime.datetime.strptime(pending_tasks[0]['deadline'], "%Y-%m-%d").date()
        days_left = max((nearest_date - now).days, 1)
        required_time = sum(t['estimated_time'] for t in pending_tasks)
        
        # Calculate free minutes per day
        daily_free_mins = 0
        for slot in db_user.get('free_slots', []):
            try:
                start = datetime.datetime.strptime(slot['from'], "%H:%M")
                end = datetime.datetime.strptime(slot['to'], "%H:%M")
                daily_free_mins += max((end - start).seconds // 60, 0)
            except: pass
        if daily_free_mins == 0: daily_free_mins = db_user.get('target_study_hours', 4) * 60
        
        remaining_time = days_left * daily_free_mins
        if required_time > remaining_time:
            insights.append(f"🚨 DEADLINE RISK: You need {required_time//60}h to finish pending tasks but only have ~{remaining_time//60}h free before the next deadline.")

    # ── Weekly Bar Chart Data (Current Week: Mon-Sun) ──
    # Planned = target_study_hours from daily setup (same each day)
    # Actual = sum of focus session minutes per day
    target_daily_mins = db_user.get('target_study_hours', 4) * 60
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    # Get current week's Monday
    today = datetime.datetime.now().date()
    monday = today - datetime.timedelta(days=today.weekday())
    
    weekly_labels = []
    weekly_planned = []
    weekly_actual = []
    
    for i in range(7):
        d = monday + datetime.timedelta(days=i)
        d_iso = d.isoformat()
        weekly_labels.append(day_names[i])
        
        # Planned: target study hours for each day (only up to today)
        if d <= today:
            weekly_planned.append(target_daily_mins)
        else:
            weekly_planned.append(target_daily_mins)  # still show target
        
        # Actual: sum of focus sessions on that day
        day_actual = sum(
            f.get('duration_mins', 0) for f in db_focus_sessions
            if f.get('date', '').startswith(d_iso)
        )
        weekly_actual.append(day_actual)
    
    # ── Monthly Pie Chart Data ──
    # Group focus sessions by month for the last 6 months
    monthly_data = {}
    for f_session in db_focus_sessions:
        try:
            f_date = datetime.datetime.fromisoformat(f_session['date']).date()
            month_key = f_date.strftime("%b %Y")
            if month_key not in monthly_data:
                monthly_data[month_key] = 0
            monthly_data[month_key] += f_session.get('duration_mins', 0)
        except Exception:
            pass
    
    # If no focus data, build from tasks by deadline month
    if not monthly_data:
        for t in db_tasks:
            try:
                if t.get('deadline'):
                    t_date = datetime.datetime.strptime(t['deadline'], "%Y-%m-%d").date()
                    month_key = t_date.strftime("%b %Y")
                    if month_key not in monthly_data:
                        monthly_data[month_key] = 0
                    monthly_data[month_key] += t.get('estimated_time', 0)
            except Exception:
                pass
    
    # Sort by date and take last 6 months
    sorted_months = sorted(monthly_data.items(), key=lambda x: datetime.datetime.strptime(x[0], "%b %Y"))[-6:]
    pie_labels = [m[0] for m in sorted_months]
    pie_values = [m[1] for m in sorted_months]

    return jsonify({
        "success": True,
        "planned_mins": planned,
        "actual_mins": actual,
        "tasks_completed": completed,
        "tasks_total": total,
        "score": db_user['score'],
        "streak": db_user['streak'],
        "avg_focus": round(avg_focus, 1),
        "consistency": round(consistency, 1),
        "main_distraction": distraction_msg,
        "energy_correlation": energy_msg,
        "insights": insights,
        "target_daily_mins": target_daily_mins,
        "weekly_chart": {
            "labels": weekly_labels,
            "planned": weekly_planned,
            "actual": weekly_actual
        },
        "monthly_chart": {
            "labels": pie_labels,
            "values": pie_values
        }
    })
if __name__ == '__main__':
    app.run(debug=True, port=5000)
