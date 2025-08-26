from __future__ import annotations
import os
import sqlite3
from datetime import datetime, date
from functools import wraps
from typing import Optional, List, Dict
from io import StringIO
import csv

from flask import (
    Flask, g, request, redirect, url_for, render_template_string,
    session, flash, jsonify, send_from_directory, abort, make_response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# -------------------------- App Setup --------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('HMS_SECRET', 'dev-secret-change-me')
app.config['DATABASE'] = os.path.join(os.path.dirname(__file__), 'hostel.db')
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB uploads
ALLOWED_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "pdf"}  # pdf bhi allow

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# -------------------------- DB Helpers --------------------------
def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

SCHEMA_SQL_CORE = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    occupied INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    phone TEXT,
    guardian TEXT,
    department TEXT,
    batch TEXT,
    semester TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    room_id INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
    FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    method TEXT,
    paid_on TEXT NOT NULL,
    note TEXT,
    proof_path TEXT,
    status TEXT NOT NULL DEFAULT 'Pending',
    created_at TEXT NOT NULL,
    FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER,
    title TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE SET NULL
);
"""

def column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    cur = db.execute(f"PRAGMA table_info({table})")
    return any(r['name'] == column for r in cur.fetchall())

def init_db():
    db = get_db()
    db.executescript(SCHEMA_SQL_CORE)
    # Migrate (add columns if old DB)
    # (Safe guards if someone used the older schema)
    if not column_exists(db, 'students', 'department'):
        db.execute("ALTER TABLE students ADD COLUMN department TEXT")
    if not column_exists(db, 'students', 'batch'):
        db.execute("ALTER TABLE students ADD COLUMN batch TEXT")
    if not column_exists(db, 'students', 'semester'):
        db.execute("ALTER TABLE students ADD COLUMN semester TEXT")
    if not column_exists(db, 'students', 'user_id'):
        db.execute("ALTER TABLE students ADD COLUMN user_id INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL")
    if not column_exists(db, 'payments', 'proof_path'):
        db.execute("ALTER TABLE payments ADD COLUMN proof_path TEXT")
    if not column_exists(db, 'payments', 'status'):
        db.execute("ALTER TABLE payments ADD COLUMN status TEXT NOT NULL DEFAULT 'Pending'")

    # Seed admin if none exists
    cur = db.execute('SELECT COUNT(*) as c FROM users')
    if cur.fetchone()['c'] == 0:
        db.execute(
            'INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)',
            ('admin', generate_password_hash('admin123'), 'admin', datetime.utcnow().isoformat())
        )
    db.commit()

# ------------------------- Auth Utils ---------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.path))
        if session.get('role') != 'admin':
            flash('Admins only.', 'error')
            return redirect(url_for('portal'))
        return f(*args, **kwargs)
    return wrapper

# ------------------------- UI Template --------------------------
BASE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ title or 'UTECH Hostel MS' }}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    :root{
      --brown1:#4e342e;
      --brown2:#6d4c41;
      --brown3:#8d6e63;
      --cream:#ffffff;
      --text:#1f1f1f;
    }
    /* Liquid animated gradient background */
    body {
      padding-top: 76px;
      min-height: 100vh;
      background: linear-gradient(-45deg, var(--brown1), var(--brown2), var(--brown3), var(--cream));
      background-size: 400% 400%;
      animation: gradientBG 22s ease infinite;
    }
    @keyframes gradientBG {
      0% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
      100% { background-position: 0% 50%; }
    }
    /* Glassy cards */
    .card {
      border-radius: 1rem;
      background: rgba(255,255,255,0.9);
      backdrop-filter: blur(6px);
      box-shadow: 0 10px 25px rgba(0,0,0,0.08);
    }
    .form-control, .btn, .form-select { border-radius: 0.9rem; }
    .table thead th { white-space: nowrap; }
    /* Navbar */
    .navbar {
      background: linear-gradient(90deg, rgba(78,52,46,0.95), rgba(109,76,65,0.95));
      box-shadow: 0 6px 18px rgba(0,0,0,0.15);
    }
    .brand-text { font-weight: 700; letter-spacing: .2px; }
    .logo-img { height: 42px; width: auto; margin-right: 10px; border-radius: 6px; background:#fff; padding:3px; }
    footer {
      margin-top: 40px;
      color: #fff;
      background: linear-gradient(90deg, rgba(78,52,46,0.95), rgba(109,76,65,0.95));
      padding: 20px 0;
    }
    .footer-inner { font-size: .95rem; }
    /* Buttons theme */
    .btn-primary {
      background: linear-gradient(90deg, #6d4c41, #8d6e63);
      border: none;
    }
    .btn-outline-light { border-color:#fff; }
    .badge-successish{ background:#43a047; }
    .badge-pending { background:#f9a825; color:#000; }
    .badge-rejected { background:#c62828; }
    /* Responsive */
    @media (max-width: 576px) {
      body { padding-top: 82px; }
      .navbar-brand span { display: none; }
      .logo-img { height: 36px; }
    }
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark fixed-top">
  <div class="container-fluid">
    <a class="navbar-brand d-flex align-items-center gap-2" href="{{ url_for('dashboard') if session.get('role')=='admin' else url_for('portal') }}">
      <img src="{{ url_for('logo') }}" alt="BBSUTSD Logo" class="logo-img">
      <span class="brand-text">UTECH Hostel MS</span>
    </a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav" aria-controls="nav" aria-expanded="false" aria-label="Toggle navigation">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div class="collapse navbar-collapse" id="nav">
      {% if session.get('user_id') %}
      <ul class="navbar-nav me-auto">
        {% if session.get('role') == 'admin' %}
          <li class="nav-item"><a class="nav-link" href="{{ url_for('dashboard') }}">Dashboard</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('rooms') }}">Rooms</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('students') }}">Students</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('allocations') }}">Allocations</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('payments') }}">Payments</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('issues') }}">Issues</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('users') }}">Users</a></li>
          <li class="nav-item dropdown">
            <a class="nav-link dropdown-toggle" role="button" data-bs-toggle="dropdown">Export</a>
            <ul class="dropdown-menu">
              <li><a class="dropdown-item" href="{{ url_for('export_students_csv') }}">Students CSV</a></li>
              <li><a class="dropdown-item" href="{{ url_for('export_payments_csv') }}">Payments CSV</a></li>
            </ul>
          </li>
        {% else %}
          <li class="nav-item"><a class="nav-link" href="{{ url_for('portal') }}">Portal</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('portal_rooms') }}">Available Rooms</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('portal_payments') }}">My Payments</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('issues') }}">Complaints</a></li>
        {% endif %}
      </ul>
      <span class="navbar-text me-3">{{ session.get('username') }} ({{ session.get('role') }})</span>
      <a class="btn btn-outline-light btn-sm" href="{{ url_for('logout') }}">Logout</a>
      {% else %}
        <ul class="navbar-nav ms-auto">
          <li class="nav-item"><a class="nav-link" href="{{ url_for('login') }}">Login</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('register') }}">Student Register</a></li>
        </ul>
      {% endif %}
    </div>
  </div>
</nav>

<main class="container">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      <div class="mt-2">
        {% for cat, msg in messages %}
          <div class="alert alert-{{ 'danger' if cat=='error' else cat }} alert-dismissible fade show" role="alert">
            {{ msg }}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
          </div>
        {% endfor %}
      </div>
    {% endif %}
  {% endwith %}
  {{ body|safe }}
</main>

<footer>
  <div class="container">
    <div class="d-flex flex-column flex-md-row justify-content-between align-items-center footer-inner">
      <div class="d-flex align-items-center gap-2">
        <img src="{{ url_for('logo') }}" class="logo-img" alt="logo">
        <span>The Benazir Bhutto Shaheed University of Technology and Skill Development Khairpur Mirs, 66020, Sindh, Pakistan.</span>
      </div>
      <div class="mt-3 mt-md-0">&copy; {{ now.year }} UTECH Hostel MS</div>
    </div>
  </div>
</footer>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

# --------------------------- Before Request ---------------------
@app.before_request
def before_request():
    init_db()

# ---------------------------- Static Logo -----------------------
@app.route('/logo')
def logo():
    # Serve local file 'bbsutsd_logo.png' from same folder if it exists
    here = os.path.dirname(__file__)
    logo_path = os.path.join(here, 'bbsutsd_logo.png')
    if os.path.exists(logo_path):
        return send_from_directory(here, 'bbsutsd_logo.png')
    # Fallback tiny SVG if file missing
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
      <rect width="100%" height="100%" fill="#ffffff"/>
      <circle cx="32" cy="32" r="26" fill="#6d4c41"/>
      <text x="32" y="38" text-anchor="middle" font-size="20" font-family="Arial" fill="#fff">U</text>
    </svg>
    """
    resp = make_response(svg)
    resp.headers['Content-Type'] = 'image/svg+xml'
    return resp

# --------------------------- Dashboard --------------------------
@app.route('/')
@login_required
def dashboard():
    if session.get('role') != 'admin':
        return redirect(url_for('portal'))
    db = get_db()
    stats = {
        'rooms': db.execute('SELECT COUNT(*) c FROM rooms').fetchone()['c'],
        'students': db.execute('SELECT COUNT(*) c FROM students').fetchone()['c'],
        'allocations_active': db.execute("SELECT COUNT(*) c FROM allocations WHERE status='active'").fetchone()['c'],
        'issues_open': db.execute("SELECT COUNT(*) c FROM issues WHERE status='open'").fetchone()['c'],
        'income_30d': db.execute("SELECT IFNULL(SUM(amount),0) s FROM payments WHERE paid_on >= date('now','-30 day')").fetchone()['s'],
    }
    body = render_template_string("""
    <div class='row g-4'>
      <div class='col-md-3'>
        <div class='card'><div class='card-body'><h6>Rooms</h6><h3>{{ stats.rooms }}</h3></div></div>
      </div>
      <div class='col-md-3'>
        <div class='card'><div class='card-body'><h6>Students</h6><h3>{{ stats.students }}</h3></div></div>
      </div>
      <div class='col-md-3'>
        <div class='card'><div class='card-body'><h6>Active Allocations</h6><h3>{{ stats.allocations_active }}</h3></div></div>
      </div>
      <div class='col-md-3'>
        <div class='card'><div class='card-body'><h6>Income (30d)</h6><h3>PKR {{ '%.0f' % stats.income_30d }}</h3></div></div>
      </div>
    </div>
    <div class='mt-4 d-flex flex-wrap gap-2'>
      <a class='btn btn-primary' href='{{ url_for("rooms") }}'>Manage Rooms</a>
      <a class='btn btn-primary' href='{{ url_for("students") }}'>Manage Students</a>
      <a class='btn btn-primary' href='{{ url_for("allocations") }}'>Manage Allocations</a>
      <a class='btn btn-primary' href='{{ url_for("payments") }}'>Record Payments</a>
      <a class='btn btn-primary' href='{{ url_for("issues") }}'>Issues</a>
    </div>
    """, stats=stats)
    return render_template_string(BASE, title='Dashboard', body=body, now=datetime.utcnow())

# --------------------------- Auth -------------------------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        row = get_db().execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if row and check_password_hash(row['password_hash'], password):
            session['user_id'] = row['id']
            session['username'] = row['username']
            session['role'] = row['role']
            next_url = request.args.get('next')
            if next_url:
                return redirect(next_url)
            return redirect(url_for('dashboard' if row['role']=='admin' else 'portal'))
        flash('Invalid credentials', 'error')
    body = """
    <div class='row justify-content-center'>
      <div class='col-md-5'>
        <div class='card'>
          <div class='card-body'>
            <h4 class='mb-3'>Sign in</h4>
            <form method='post'>
              <div class='mb-3'>
                <label class='form-label'>Username</label>
                <input name='username' class='form-control' required>
              </div>
              <div class='mb-3'>
                <label class='form-label'>Password</label>
                <input type='password' name='password' class='form-control' required>
              </div>
              <button class='btn btn-primary w-100'>Login</button>
              
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_template_string(BASE, title='Login', body=body, now=datetime.utcnow())

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        name = request.form['name'].strip()
        email = request.form.get('email','').strip() or None
        phone = request.form.get('phone','').strip()
        guardian = request.form.get('guardian','').strip()
        department = request.form.get('department','').strip()
        batch = request.form.get('batch','').strip()
        semester = request.form.get('semester','').strip()
        db = get_db()
        try:
            db.execute('INSERT INTO users (username,password_hash,role,created_at) VALUES (?,?,?,?)',
                       (username, generate_password_hash(password), 'student', datetime.utcnow().isoformat()))
            user_id = db.execute('SELECT last_insert_rowid() AS lid').fetchone()['lid']
            db.execute('INSERT INTO students (user_id,name,email,phone,guardian,department,batch,semester,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       (user_id, name, email, phone, guardian, department, batch, semester, datetime.utcnow().isoformat()))
            db.commit()
            flash('Registration successful. Please login.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError as e:
            flash('Username or email already exists.', 'error')
    body = """
    <div class='row justify-content-center'>
      <div class='col-lg-8'>
        <div class='card'>
          <div class='card-body'>
            <h4 class='mb-3'>Student Registration</h4>
            <form method='post'>
              <div class='row g-3'>
                <div class='col-md-4'><label class='form-label'>Username</label><input name='username' class='form-control' required></div>
                <div class='col-md-4'><label class='form-label'>Password</label><input type='password' name='password' class='form-control' required></div>
                <div class='col-md-4'><label class='form-label'>Full Name</label><input name='name' class='form-control' required></div>
                <div class='col-md-4'><label class='form-label'>Email</label><input type='email' name='email' class='form-control'></div>
                <div class='col-md-4'><label class='form-label'>Phone</label><input name='phone' class='form-control'></div>
                <div class='col-md-4'><label class='form-label'>Guardian</label><input name='guardian' class='form-control'></div>
                <div class='col-md-4'><label class='form-label'>Department</label><input name='department' class='form-control' placeholder='e.g., Computer Systems'></div>
                <div class='col-md-4'><label class='form-label'>Batch</label><input name='batch' class='form-control' placeholder='e.g., 2022'></div>
                <div class='col-md-4'><label class='form-label'>Semester</label><input name='semester' class='form-control' placeholder='e.g., 5th'></div>
              </div>
              <div class='mt-3'><button class='btn btn-primary'>Register</button></div>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render_template_string(BASE, title='Register', body=body, now=datetime.utcnow())

@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

# -------------------------- Users (Admins) ----------------------
@app.route('/users', methods=['GET','POST'])
@admin_required
def users():
    db = get_db()
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        role = request.form.get('role','admin')
        try:
            db.execute('INSERT INTO users (username,password_hash,role,created_at) VALUES (?,?,?,?)',
                       (username, generate_password_hash(password), role, datetime.utcnow().isoformat()))
            db.commit()
            flash('User created', 'success')
        except sqlite3.IntegrityError:
            flash('Username already exists', 'error')
    rows = db.execute('SELECT id, username, role, created_at FROM users ORDER BY id').fetchall()
    body = render_template_string("""
    <div class='d-flex justify-content-between align-items-center mb-3'>
      <h4>Users</h4>
      <button class='btn btn-primary' data-bs-toggle='collapse' data-bs-target='#newUser'>+ New</button>
    </div>
    <div id='newUser' class='collapse mb-4'>
      <form method='post' class='card card-body'>
        <div class='row g-3'>
          <div class='col-md-4'><input name='username' class='form-control' placeholder='Username' required></div>
          <div class='col-md-4'><input type='password' name='password' class='form-control' placeholder='Password' required></div>
          <div class='col-md-3'>
            <select name='role' class='form-select'>
              <option value='admin'>Admin</option>
              <option value='student'>Student</option>
            </select>
          </div>
          <div class='col-md-1'><button class='btn btn-success w-100'>Save</button></div>
        </div>
      </form>
    </div>
    <table class='table table-striped table-hover'>
      <thead><tr><th>ID</th><th>Username</th><th>Role</th><th>Created</th></tr></thead>
      <tbody>
        {% for u in rows %}
        <tr><td>{{u.id}}</td><td>{{u.username}}</td><td>{{u.role}}</td><td>{{u.created_at[:19].replace('T',' ')}}</td></tr>
        {% endfor %}
      </tbody>
    </table>
    """, rows=rows)
    return render_template_string(BASE, title='Users', body=body, now=datetime.utcnow())

# ---------------------------- Rooms -----------------------------
@app.route('/rooms', methods=['GET','POST'])
@admin_required
def rooms():
    db = get_db()
    if request.method == 'POST':
        if request.form.get('_method') == 'DELETE':
            db.execute('DELETE FROM rooms WHERE id=?', (request.form['id'],))
            db.commit()
            flash('Room deleted', 'success')
        else:
            number = request.form['number'].strip()
            rtype = request.form['type'].strip() or 'Standard'
            cap = int(request.form.get('capacity', 1))
            try:
                db.execute('INSERT INTO rooms (number,type,capacity,occupied) VALUES (?,?,?,0)', (number, rtype, cap))
                db.commit()
                flash('Room added', 'success')
            except sqlite3.IntegrityError:
                flash('Room number already exists', 'error')
    rows = db.execute('SELECT * FROM rooms ORDER BY number').fetchall()
    body = render_template_string("""
    <div class='d-flex justify-content-between align-items-center mb-3'>
      <h4>Rooms</h4>
      <button class='btn btn-primary' data-bs-toggle='collapse' data-bs-target='#newRoom'>+ New</button>
    </div>
    <div id='newRoom' class='collapse mb-4'>
      <form method='post' class='card card-body'>
        <div class='row g-3'>
          <div class='col-md-3'><input name='number' class='form-control' placeholder='Room Number' required></div>
          <div class='col-md-3'><input name='type' class='form-control' placeholder='Type (Standard/Deluxe etc)'></div>
          <div class='col-md-3'><input type='number' name='capacity' class='form-control' placeholder='Capacity' min='1' value='1'></div>
          <div class='col-md-3'><button class='btn btn-success w-100'>Save</button></div>
        </div>
      </form>
    </div>
    <table class='table table-striped table-hover'>
      <thead><tr><th>#</th><th>Type</th><th>Capacity</th><th>Occupied</th><th>Vacant</th><th></th></tr></thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>{{ r.number }}</td>
          <td>{{ r.type }}</td>
          <td>{{ r.capacity }}</td>
          <td>{{ r.occupied }}</td>
          <td>{{ r.capacity - r.occupied }}</td>
          <td>
            <form method='post' style='display:inline'>
              <input type='hidden' name='_method' value='DELETE'>
              <input type='hidden' name='id' value='{{ r.id }}'>
              <button class='btn btn-sm btn-outline-danger' onclick="return confirm('Delete room?')">Delete</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    """, rows=rows)
    return render_template_string(BASE, title='Rooms', body=body, now=datetime.utcnow())

# --------------------------- Students (Admin) -------------------
@app.route('/students', methods=['GET','POST'])
@admin_required
def students():
    db = get_db()
    if request.method == 'POST':
        if request.form.get('_method') == 'DELETE':
            db.execute('DELETE FROM students WHERE id=?', (request.form['id'],))
            db.commit()
            flash('Student removed', 'success')
        else:
            name = request.form['name'].strip()
            email = request.form.get('email','').strip() or None
            phone = request.form.get('phone','').strip()
            guardian = request.form.get('guardian','').strip()
            department = request.form.get('department','').strip()
            batch = request.form.get('batch','').strip()
            semester = request.form.get('semester','').strip()
            try:
                db.execute('INSERT INTO students (name,email,phone,guardian,department,batch,semester,created_at) VALUES (?,?,?,?,?,?,?,?)',
                           (name, email, phone, guardian, department, batch, semester, datetime.utcnow().isoformat()))
                db.commit()
                flash('Student added', 'success')
            except sqlite3.IntegrityError:
                flash('Email already exists', 'error')
    qs = request.args.get('q','').strip()
    if qs:
        rows = db.execute("""SELECT * FROM students 
                             WHERE name LIKE ? OR email LIKE ? OR department LIKE ? OR batch LIKE ? OR semester LIKE ?
                             ORDER BY id DESC""", (f'%{qs}%', f'%{qs}%', f'%{qs}%', f'%{qs}%', f'%{qs}%')).fetchall()
    else:
        rows = db.execute('SELECT * FROM students ORDER BY id DESC').fetchall()
    body = render_template_string("""
    <div class='d-flex justify-content-between align-items-center mb-3'>
      <h4>Students</h4>
      <div class='d-flex gap-2'>
        <form class='d-flex' method='get'>
          <input class='form-control me-2' type='search' name='q' value='{{ request.args.get("q","") }}' placeholder='Search name/email/department/batch/semester'>
          <button class='btn btn-outline-secondary'>Search</button>
        </form>
        <button class='btn btn-primary' data-bs-toggle='collapse' data-bs-target='#newStudent'>+ New</button>
      </div>
    </div>
    <div id='newStudent' class='collapse mb-4'>
      <form method='post' class='card card-body'>
        <div class='row g-3'>
          <div class='col-md-3'><input name='name' class='form-control' placeholder='Full Name' required></div>
          <div class='col-md-3'><input name='email' type='email' class='form-control' placeholder='Email (optional)'></div>
          <div class='col-md-2'><input name='phone' class='form-control' placeholder='Phone'></div>
          <div class='col-md-2'><input name='guardian' class='form-control' placeholder='Guardian'></div>
          <div class='col-md-2'><input name='department' class='form-control' placeholder='Department'></div>
          <div class='col-md-2'><input name='batch' class='form-control' placeholder='Batch'></div>
          <div class='col-md-2'><input name='semester' class='form-control' placeholder='Semester'></div>
          <div class='col-md-1'><button class='btn btn-success w-100'>Save</button></div>
        </div>
      </form>
    </div>
    <div class='table-responsive'>
    <table class='table table-striped table-hover'>
      <thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Phone</th><th>Guardian</th><th>Dept</th><th>Batch</th><th>Sem</th><th>Joined</th><th></th></tr></thead>
      <tbody>
        {% for s in rows %}
        <tr>
          <td>{{ s.id }}</td>
          <td>{{ s.name }}</td>
          <td>{{ s.email or '' }}</td>
          <td>{{ s.phone }}</td>
          <td>{{ s.guardian }}</td>
          <td>{{ s.department }}</td>
          <td>{{ s.batch }}</td>
          <td>{{ s.semester }}</td>
          <td>{{ (s.created_at or '')[:19].replace('T',' ') }}</td>
          <td>
            <form method='post' style='display:inline'>
              <input type='hidden' name='_method' value='DELETE'>
              <input type='hidden' name='id' value='{{ s.id }}'>
              <button class='btn btn-sm btn-outline-danger' onclick="return confirm('Delete student?')">Delete</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    """, rows=rows)
    return render_template_string(BASE, title='Students', body=body, now=datetime.utcnow())

# -------------------------- Allocations (Admin) -----------------
@app.route('/allocations', methods=['GET','POST'])
@admin_required
def allocations():
    db = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'allocate':
            student_id = int(request.form['student_id'])
            room_id = int(request.form['room_id'])
            start_date = request.form.get('start_date') or date.today().isoformat()
            room = db.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
            if room['occupied'] >= room['capacity']:
                flash('Room is full', 'error')
            else:
                db.execute('INSERT INTO allocations (student_id,room_id,start_date,status,created_at) VALUES (?,?,?,?,?)',
                           (student_id, room_id, start_date, 'active', datetime.utcnow().isoformat()))
                db.execute('UPDATE rooms SET occupied = occupied + 1 WHERE id=?', (room_id,))
                db.commit()
                flash('Allocated successfully', 'success')
        elif action == 'release':
            alloc_id = int(request.form['alloc_id'])
            alloc = db.execute('SELECT * FROM allocations WHERE id=?', (alloc_id,)).fetchone()
            if alloc and alloc['status'] == 'active':
                db.execute("UPDATE allocations SET status='released', end_date=?, created_at=created_at WHERE id=?",
                           (date.today().isoformat(), alloc_id))
                db.execute('UPDATE rooms SET occupied = CASE WHEN occupied>0 THEN occupied-1 ELSE 0 END WHERE id=?', (alloc['room_id'],))
                db.commit()
                flash('Released successfully', 'success')
    allocs = db.execute(
        '''SELECT a.id, s.name as student, r.number as room, a.start_date, a.end_date, a.status
           FROM allocations a
           JOIN students s ON s.id=a.student_id
           JOIN rooms r ON r.id=a.room_id
           ORDER BY a.id DESC''').fetchall()
    students = db.execute('SELECT id, name FROM students ORDER BY name').fetchall()
    rooms = db.execute('SELECT id, number, capacity, occupied FROM rooms ORDER BY number').fetchall()
    body = render_template_string("""
    <div class='d-flex justify-content-between align-items-center mb-3'>
      <h4>Allocations</h4>
    </div>
    <form method='post' class='card card-body mb-4'>
      <input type='hidden' name='action' value='allocate'>
      <div class='row g-3 align-items-end'>
        <div class='col-md-4'>
          <label class='form-label'>Student</label>
          <select name='student_id' class='form-select' required>
            <option value='' disabled selected>-- select --</option>
            {% for s in students %}<option value='{{s.id}}'>{{s.name}}</option>{% endfor %}
          </select>
        </div>
        <div class='col-md-3'>
          <label class='form-label'>Room</label>
          <select name='room_id' class='form-select' required>
            <option value='' disabled selected>-- select --</option>
            {% for r in rooms %}
              <option value='{{r.id}}'>{{r.number}} ({{r.occupied}}/{{r.capacity}})</option>
            {% endfor %}
          </select>
        </div>
        <div class='col-md-3'>
          <label class='form-label'>Start Date</label>
          <input type='date' name='start_date' class='form-control'>
        </div>
        <div class='col-md-2'>
          <button class='btn btn-success w-100'>Allocate</button>
        </div>
      </div>
    </form>
    <div class='table-responsive'>
    <table class='table table-striped table-hover'>
      <thead><tr><th>ID</th><th>Student</th><th>Room</th><th>Start</th><th>End</th><th>Status</th><th></th></tr></thead>
      <tbody>
        {% for a in allocs %}
        <tr>
          <td>{{a.id}}</td>
          <td>{{a.student}}</td>
          <td>{{a.room}}</td>
          <td>{{a.start_date}}</td>
          <td>{{a.end_date or ''}}</td>
          <td><span class='badge {{ 'badge-successish' if a.status=='active' else 'bg-secondary' }}'>{{a.status}}</span></td>
          <td>
            {% if a.status=='active' %}
            <form method='post' style='display:inline'>
              <input type='hidden' name='action' value='release'>
              <input type='hidden' name='alloc_id' value='{{a.id}}'>
              <button class='btn btn-sm btn-outline-warning'>Release</button>
            </form>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    """, allocs=allocs, students=students, rooms=rooms)
    return render_template_string(BASE, title='Allocations', body=body, now=datetime.utcnow())

# --------------------------- Payments ---------------------------
def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTS

@app.route('/uploads/<path:fname>')
@login_required
def uploads(fname):
    # Permission: students can only access their own files via links we render to them
    # We serve directly; filenames include no sensitive info.
    return send_from_directory(app.config['UPLOAD_FOLDER'], fname)

@app.route('/payments', methods=['GET','POST'])
@admin_required
def payments():
    db = get_db()
    if request.method == 'POST':
        student_id = int(request.form['student_id'])
        amount = float(request.form['amount'])
        method = request.form.get('method','Cash')
        paid_on = request.form.get('paid_on') or date.today().isoformat()
        note = request.form.get('note','')
        status = request.form.get('status','Pending')
        proof_path = None

        if 'proof' in request.files:
            file = request.files['proof']
            if file and file.filename and allowed_file(file.filename):
                fname = f"proof_{student_id}_{int(datetime.utcnow().timestamp())}_{secure_filename(file.filename)}"
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                file.save(fpath)
                proof_path = fname

        db.execute('INSERT INTO payments (student_id, amount, method, paid_on, note, proof_path, status, created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (student_id, amount, method, paid_on, note, proof_path, status, datetime.utcnow().isoformat()))
        db.commit()
        flash('Payment recorded', 'success')

    students = db.execute('SELECT id, name FROM students ORDER BY name').fetchall()
    rows = db.execute(
        '''SELECT p.id, s.name as student, p.amount, p.method, p.paid_on, p.note, p.proof_path, p.status
           FROM payments p JOIN students s ON s.id=p.student_id
           ORDER BY p.id DESC''').fetchall()
    body = render_template_string("""
    <div class='d-flex justify-content-between align-items-center mb-3'>
      <h4>Payments</h4>
    </div>
    <form method='post' enctype='multipart/form-data' class='card card-body mb-4'>
      <div class='row g-3'>
        <div class='col-md-3'>
          <label class='form-label'>Student</label>
          <select name='student_id' class='form-select' required>
            <option value='' disabled selected>-- select --</option>
            {% for s in students %}<option value='{{s.id}}'>{{s.name}}</option>{% endfor %}
          </select>
        </div>
        <div class='col-md-2'><label class='form-label'>Amount</label><input type='number' step='0.01' name='amount' class='form-control' required></div>
        <div class='col-md-2'><label class='form-label'>Method</label><input name='method' class='form-control' value='Cash'></div>
        <div class='col-md-2'><label class='form-label'>Paid On</label><input type='date' name='paid_on' class='form-control'></div>
        <div class='col-md-3'><label class='form-label'>Note</label><input name='note' class='form-control' placeholder='Month, Fee head'></div>
        <div class='col-md-3'><label class='form-label'>Proof (img/pdf)</label><input type='file' name='proof' class='form-control'></div>
        <div class='col-md-2'>
          <label class='form-label'>Status</label>
          <select name='status' class='form-select'>
            <option>Pending</option>
            <option>Approved</option>
            <option>Rejected</option>
          </select>
        </div>
      </div>
      <div class='mt-3'><button class='btn btn-success'>Save Payment</button></div>
    </form>
    <div class='table-responsive'>
    <table class='table table-striped table-hover'>
      <thead><tr><th>ID</th><th>Student</th><th>Amount</th><th>Method</th><th>Paid On</th><th>Note</th><th>Proof</th><th>Status</th></tr></thead>
      <tbody>
        {% for p in rows %}
        <tr>
          <td>{{p.id}}</td>
          <td>{{p.student}}</td>
          <td>PKR {{ '%.0f' % p.amount }}</td>
          <td>{{p.method}}</td>
          <td>{{p.paid_on}}</td>
          <td>{{p.note}}</td>
          <td>
            {% if p.proof_path %}
              <a class='btn btn-sm btn-outline-secondary' target='_blank' href='{{ url_for("uploads", fname=p.proof_path) }}'>View</a>
            {% endif %}
          </td>
          <td>
            <span class='badge {{ "badge-pending" if p.status=="Pending" else ("badge-successish" if p.status=="Approved" else "badge-rejected") }}'>{{p.status}}</span>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    """, students=students, rows=rows)
    return render_template_string(BASE, title='Payments', body=body, now=datetime.utcnow())

# ----------------------------- Issues ---------------------------
# NOTE: This route now supports both admin and student usage.
# - Admin (role=='admin') -> can view all issues, close issues, and log (with optional student selection).
# - Student (role=='student') -> can view their own issues and log new issues. They cannot close issues.
@app.route('/issues', methods=['GET','POST'])
@login_required
def issues():
    db = get_db()
    role = session.get('role')
    # Handle POST actions
    if request.method == 'POST':
        # Close action: only admin may perform
        if request.form.get('action') == 'close':
            if role != 'admin':
                flash('Not authorized', 'error')
            else:
                iid = int(request.form['id'])
                db.execute("UPDATE issues SET status='closed' WHERE id=?", (iid,))
                db.commit()
                flash('Issue closed', 'success')
        else:
            # Logging a new issue: both admin and student can create
            if role == 'student':
                # Ensure we link to the current student's row
                stu = None
                uid = session.get('user_id')
                if uid:
                    stu = db.execute('SELECT * FROM students WHERE user_id=?', (uid,)).fetchone()
                student_id = stu['id'] if stu else None
            else:
                # Admin may optionally select a student_id from form (or leave blank)
                student_id = request.form.get('student_id') or None
                if student_id == '':
                    student_id = None
            title = request.form['title'].strip()
            detail = request.form.get('detail','').strip()
            db.execute('INSERT INTO issues (student_id,title,detail,status,created_at) VALUES (?,?,?,?,?)',
                       (student_id, title, detail, 'open', datetime.utcnow().isoformat()))
            db.commit()
            flash('Issue logged', 'success')

    # Render views: admin sees all, student sees only their own
    if role == 'admin':
        students = db.execute('SELECT id, name FROM students ORDER BY name').fetchall()
        rows = db.execute(
            '''SELECT i.id, i.title, i.detail, i.status, i.created_at, s.name as student
               FROM issues i LEFT JOIN students s ON s.id=i.student_id
               ORDER BY i.id DESC''').fetchall()
        body = render_template_string("""
        <div class='d-flex justify-content-between align-items-center mb-3'>
          <h4>Issues & Complaints</h4>
        </div>
        <form method='post' class='card card-body mb-4'>
          <div class='row g-3'>
            <div class='col-md-3'>
              <label class='form-label'>Student (optional)</label>
              <select name='student_id' class='form-select'>
                <option value=''>—</option>
                {% for s in students %}<option value='{{s.id}}'>{{s.name}}</option>{% endfor %}
              </select>
            </div>
            <div class='col-md-3'><label class='form-label'>Title</label><input name='title' class='form-control' required></div>
            <div class='col-md-6'><label class='form-label'>Detail</label><input name='detail' class='form-control'></div>
          </div>
          <div class='mt-3'><button class='btn btn-success'>Log Issue</button></div>
        </form>
        <div class='table-responsive'>
        <table class='table table-striped table-hover'>
          <thead><tr><th>ID</th><th>Title</th><th>Student</th><th>Status</th><th>Created</th><th></th></tr></thead>
          <tbody>
            {% for i in rows %}
            <tr>
              <td>{{i.id}}</td>
              <td>{{i.title}}</td>
              <td>{{i.student or ''}}</td>
              <td><span class='badge {{ 'badge-pending' if i.status=='open' else 'bg-secondary' }}'>{{i.status}}</span></td>
              <td>{{i.created_at[:19].replace('T',' ')}}</td>
              <td>
                {% if i.status=='open' %}
                <form method='post' style='display:inline'>
                  <input type='hidden' name='action' value='close'>
                  <input type='hidden' name='id' value='{{i.id}}'>
                  <button class='btn btn-sm btn-outline-success'>Close</button>
                </form>
                {% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        </div>
        """, students=students, rows=rows)
        return render_template_string(BASE, title='Issues', body=body, now=datetime.utcnow())
    else:
        # Student view: show student's own issues and a simpler form
        stu = None
        uid = session.get('user_id')
        if uid:
            stu = db.execute('SELECT * FROM students WHERE user_id=?', (uid,)).fetchone()
        if stu:
            rows = db.execute('SELECT id, title, detail, status, created_at FROM issues WHERE student_id=? ORDER BY id DESC', (stu['id'],)).fetchall()
        else:
            rows = []
        body = render_template_string("""
        <div class='d-flex justify-content-between align-items-center mb-3'>
          <h4>My Complaints</h4>
        </div>
        <form method='post' class='card card-body mb-4'>
          <div class='row g-3'>
            <div class='col-md-4'><label class='form-label'>Title</label><input name='title' class='form-control' required></div>
            <div class='col-md-8'><label class='form-label'>Detail</label><input name='detail' class='form-control'></div>
          </div>
          <div class='mt-3'><button class='btn btn-primary'>Submit Complaint</button></div>
        </form>
        <div class='table-responsive'>
        <table class='table table-striped table-hover'>
          <thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>
            {% for i in rows %}
            <tr>
              <td>{{i.id}}</td>
              <td>{{i.title}}</td>
              <td><span class='badge {{ 'badge-pending' if i.status=='open' else 'bg-secondary' }}'>{{i.status}}</span></td>
              <td>{{i.created_at[:19].replace('T',' ')}}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        </div>
        """, rows=rows)
        return render_template_string(BASE, title='My Complaints', body=body, now=datetime.utcnow())

# --------------------------- Reports API ------------------------
@app.route('/api/report/occupancy')
@login_required
def report_occupancy():
    db = get_db()
    data = db.execute('SELECT number as room, capacity, occupied, (capacity-occupied) as vacant FROM rooms ORDER BY number').fetchall()
    return jsonify([dict(r) for r in data])

@app.route('/api/report/income')
@login_required
def report_income():
    db = get_db()
    data = db.execute("""
        SELECT substr(paid_on,1,7) as month, SUM(amount) as total
        FROM payments
        GROUP BY substr(paid_on,1,7)
        ORDER BY month
    """).fetchall()
    return jsonify([dict(r) for r in data])

# --------------------------- CSV Exports ------------------------
@app.route('/export/students.csv')
@admin_required
def export_students_csv():
    db = get_db()
    rows = db.execute("""SELECT id,name,email,phone,guardian,department,batch,semester,created_at FROM students ORDER BY id""").fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Name","Email","Phone","Guardian","Department","Batch","Semester","Created"])
    for r in rows:
        writer.writerow([r['id'], r['name'], r['email'] or '', r['phone'] or '', r['guardian'] or '',
                         r['department'] or '', r['batch'] or '', r['semester'] or '',
                         (r['created_at'] or '')])
    resp = make_response(output.getvalue())
    resp.headers['Content-Disposition'] = 'attachment; filename=students.csv'
    resp.headers['Content-Type'] = 'text/csv'
    return resp

@app.route('/export/payments.csv')
@admin_required
def export_payments_csv():
    db = get_db()
    rows = db.execute("""SELECT p.id, s.name as student, p.amount, p.method, p.paid_on, p.note, p.status, p.created_at
                         FROM payments p JOIN students s ON s.id=p.student_id
                         ORDER BY p.id""").fetchall()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID","Student","Amount","Method","Paid On","Note","Status","Created"])
    for r in rows:
        writer.writerow([r['id'], r['student'], r['amount'], r['method'], r['paid_on'], r['note'] or '', r['status'], r['created_at']])
    resp = make_response(output.getvalue())
    resp.headers['Content-Disposition'] = 'attachment; filename=payments.csv'
    resp.headers['Content-Type'] = 'text/csv'
    return resp

# ========================= Student Portal =======================

def current_student_row() -> Optional[sqlite3.Row]:
    if session.get('role') != 'student':
        return None
    uid = session.get('user_id')
    if not uid:
        return None
    return get_db().execute('SELECT * FROM students WHERE user_id=?', (uid,)).fetchone()

@app.route('/portal')
@login_required
def portal():
    if session.get('role') != 'student':
        return redirect(url_for('dashboard'))
    db = get_db()
    stu = current_student_row()
    # Summary
    alloc = None
    if stu:
        alloc = db.execute("""SELECT a.id, r.number as room, a.start_date, a.end_date, a.status
                              FROM allocations a JOIN rooms r ON r.id=a.room_id
                              WHERE a.student_id=? ORDER BY a.id DESC LIMIT 1""", (stu['id'],)).fetchone()
    payments = []
    if stu:
        payments = db.execute("""SELECT id, amount, method, paid_on, note, proof_path, status 
                                 FROM payments WHERE student_id=? ORDER BY id DESC LIMIT 10""", (stu['id'],)).fetchall()
    body = render_template_string("""
    <div class='row g-4'>
      <div class='col-lg-4'>
        <div class='card h-100'>
          <div class='card-body'>
            <h5 class='mb-3'>My Profile</h5>
            {% if stu %}
              <div><strong>{{ stu.name }}</strong></div>
              <div>{{ stu.email or '' }}</div>
              <div>{{ stu.phone or '' }}</div>
              <div>Dept: {{ stu.department or '-' }}</div>
              <div>Batch: {{ stu.batch or '-' }} | Sem: {{ stu.semester or '-' }}</div>
              <div class='mt-3'>
                <a class='btn btn-primary btn-sm' href='{{ url_for("portal_edit_profile") }}'>Edit Profile</a>
              </div>
            {% else %}
              <div class='text-muted'>No profile found. Contact admin.</div>
            {% endif %}
          </div>
        </div>
      </div>
      <div class='col-lg-4'>
        <div class='card h-100'>
          <div class='card-body'>
            <h5 class='mb-3'>Room Status</h5>
            {% if alloc %}
              <div>Room: <strong>{{ alloc.room }}</strong></div>
              <div>Start: {{ alloc.start_date }}</div>
              <div>Status: <span class='badge {{ "badge-successish" if alloc.status=="active" else "bg-secondary" }}'>{{ alloc.status }}</span></div>
            {% else %}
              <div class='mb-2'>No current allocation.</div>
              <a class='btn btn-outline-secondary btn-sm' href='{{ url_for("portal_rooms") }}'>See Available Rooms</a>
            {% endif %}
          </div>
        </div>
      </div>
      <div class='col-lg-4'>
        <div class='card h-100'>
          <div class='card-body'>
            <h5 class='mb-3'>Recent Payments</h5>
            {% if payments %}
              <ul class='list-group'>
                {% for p in payments %}
                  <li class='list-group-item d-flex justify-content-between align-items-center'>
                    <span>PKR {{ '%.0f' % p.amount }} — {{ p.paid_on }}</span>
                    <span class='badge {{ "badge-pending" if p.status=="Pending" else ("badge-successish" if p.status=="Approved" else "badge-rejected") }}'>{{ p.status }}</span>
                  </li>
                {% endfor %}
              </ul>
            {% else %}
              <div class='text-muted'>No payments yet.</div>
            {% endif %}
            <div class='mt-3'><a class='btn btn-primary btn-sm' href='{{ url_for("portal_payments") }}'>Add Payment</a></div>
          </div>
        </div>
      </div>
    </div>
    """, stu=stu, alloc=alloc, payments=payments)
    return render_template_string(BASE, title='Student Portal', body=body, now=datetime.utcnow())

@app.route('/portal/rooms')
@login_required
def portal_rooms():
    if session.get('role') != 'student':
        return redirect(url_for('dashboard'))
    rows = get_db().execute('SELECT number, type, capacity, occupied, (capacity-occupied) as vacant FROM rooms ORDER BY number').fetchall()
    body = render_template_string("""
    <div class='d-flex justify-content-between align-items-center mb-3'>
      <h4>Available Rooms</h4>
    </div>
    <div class='table-responsive'>
    <table class='table table-striped table-hover'>
      <thead><tr><th>Room</th><th>Type</th><th>Capacity</th><th>Occupied</th><th>Vacant</th></tr></thead>
      <tbody>
        {% for r in rows %}
        <tr class='{{ "table-success" if r.vacant>0 else "" }}'>
          <td>{{ r.number }}</td>
          <td>{{ r.type }}</td>
          <td>{{ r.capacity }}</td>
          <td>{{ r.occupied }}</td>
          <td>{{ r.vacant }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    <div class='alert alert-info'>For room allocation, please contact hostel admin.</div>
    """, rows=rows)
    return render_template_string(BASE, title='Available Rooms', body=body, now=datetime.utcnow())

@app.route('/portal/payments', methods=['GET','POST'])
@login_required
def portal_payments():
    if session.get('role') != 'student':
        return redirect(url_for('dashboard'))
    db = get_db()
    stu = current_student_row()
    if not stu:
        flash('Profile not found.', 'error')
        return redirect(url_for('portal'))
    if request.method == 'POST':
        amount = float(request.form['amount'])
        method = request.form.get('method','Challan')
        paid_on = request.form.get('paid_on') or date.today().isoformat()
        note = request.form.get('note','')
        proof_path = None
        if 'proof' in request.files:
            file = request.files['proof']
            if file and file.filename and allowed_file(file.filename):
                fname = f"proof_{stu['id']}_{int(datetime.utcnow().timestamp())}_{secure_filename(file.filename)}"
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                file.save(fpath)
                proof_path = fname
        db.execute('INSERT INTO payments (student_id, amount, method, paid_on, note, proof_path, status, created_at) VALUES (?,?,?,?,?,?,?,?)',
                   (stu['id'], amount, method, paid_on, note, proof_path, 'Pending', datetime.utcnow().isoformat()))
        db.commit()
        flash('Payment submitted. Awaiting admin review.', 'success')
        return redirect(url_for('portal_payments'))

    rows = db.execute("""SELECT id, amount, method, paid_on, note, proof_path, status 
                         FROM payments WHERE student_id=? ORDER BY id DESC""", (stu['id'],)).fetchall()
    body = render_template_string("""
    <div class='d-flex justify-content-between align-items-center mb-3'>
      <h4>My Payments</h4>
    </div>
    <form method='post' enctype='multipart/form-data' class='card card-body mb-4'>
      <div class='row g-3'>
        <div class='col-md-3'><label class='form-label'>Amount</label><input type='number' step='0.01' name='amount' class='form-control' required></div>
        <div class='col-md-3'><label class='form-label'>Method</label><input name='method' class='form-control' value='Challan'></div>
        <div class='col-md-3'><label class='form-label'>Paid On</label><input type='date' name='paid_on' class='form-control'></div>
        <div class='col-md-3'><label class='form-label'>Note</label><input name='note' class='form-control' placeholder='Fee head / Month'></div>
        <div class='col-md-6'><label class='form-label'>Upload Proof (img/pdf)</label><input type='file' name='proof' class='form-control'></div>
      </div>
      <div class='mt-3'><button class='btn btn-primary'>Submit Payment</button></div>
    </form>
    <div class='table-responsive'>
    <table class='table table-striped table-hover'>
      <thead><tr><th>ID</th><th>Amount</th><th>Method</th><th>Paid On</th><th>Note</th><th>Proof</th><th>Status</th></tr></thead>
      <tbody>
        {% for p in rows %}
        <tr>
          <td>{{p.id}}</td>
          <td>PKR {{ '%.0f' % p.amount }}</td>
          <td>{{p.method}}</td>
          <td>{{p.paid_on}}</td>
          <td>{{p.note}}</td>
          <td>{% if p.proof_path %}<a class='btn btn-sm btn-outline-secondary' target='_blank' href='{{ url_for("uploads", fname=p.proof_path) }}'>View</a>{% endif %}</td>
          <td><span class='badge {{ "badge-pending" if p.status=="Pending" else ("badge-successish" if p.status=="Approved" else "badge-rejected") }}'>{{p.status}}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    </div>
    """, rows=rows)
    return render_template_string(BASE, title='My Payments', body=body, now=datetime.utcnow())

@app.route('/portal/profile', methods=['GET','POST'])
@login_required
def portal_edit_profile():
    if session.get('role') != 'student':
        return redirect(url_for('dashboard'))
    db = get_db()
    stu = current_student_row()
    if not stu:
        flash('Profile not found.', 'error')
        return redirect(url_for('portal'))
    if request.method == 'POST':
        email = request.form.get('email','').strip() or None
        phone = request.form.get('phone','').strip()
        guardian = request.form.get('guardian','').strip()
        department = request.form.get('department','').strip()
        batch = request.form.get('batch','').strip()
        semester = request.form.get('semester','').strip()
        try:
            db.execute("""UPDATE students SET email=?, phone=?, guardian=?, department=?, batch=?, semester=? WHERE id=?""",
                       (email, phone, guardian, department, batch, semester, stu['id']))
            db.commit()
            flash('Profile updated', 'success')
            return redirect(url_for('portal'))
        except sqlite3.IntegrityError:
            flash('Email already in use.', 'error')
    body = render_template_string("""
    <div class='row justify-content-center'>
      <div class='col-lg-8'>
        <div class='card'>
          <div class='card-body'>
            <h4 class='mb-3'>Edit Profile</h4>
            <form method='post'>
              <div class='row g-3'>
                <div class='col-md-6'><label class='form-label'>Email</label><input type='email' name='email' value='{{ stu.email or "" }}' class='form-control'></div>
                <div class='col-md-6'><label class='form-label'>Phone</label><input name='phone' value='{{ stu.phone or "" }}' class='form-control'></div>
                <div class='col-md-6'><label class='form-label'>Guardian</label><input name='guardian' value='{{ stu.guardian or "" }}' class='form-control'></div>
                <div class='col-md-6'><label class='form-label'>Department</label><input name='department' value='{{ stu.department or "" }}' class='form-control'></div>
                <div class='col-md-6'><label class='form-label'>Batch</label><input name='batch' value='{{ stu.batch or "" }}' class='form-control'></div>
                <div class='col-md-6'><label class='form-label'>Semester</label><input name='semester' value='{{ stu.semester or "" }}' class='form-control'></div>
              </div>
              <div class='mt-3'><button class='btn btn-primary'>Save Changes</button></div>
            </form>
          </div>
        </div>
      </div>
    </div>
    """, stu=stu)
    return render_template_string(BASE, title='Edit Profile', body=body, now=datetime.utcnow())

# --------------------------- Entry ------------------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    # Run
    app.run(debug=True, host="127.0.0.1", port=5000)
