"""
app.py - the LibraryHub web application (Flask).

How this file is organised
    1. Setup (configuration, security, template helpers)
    2. Small helper functions (validation, pagination, login check)
    3. Routes - one section per feature:
       login, dashboard, books, members, issue/return, overdue,
       reservations, categories, reports, settings, error pages

Run it with:   python app.py
"""
import csv
import io
import logging
import math
import os
import re
import sqlite3
from datetime import date, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash

from database import (BOOK_SELECT, LOAN_SELECT, MEMBER_SELECT, calculate_fine,
                      close_db, fine_status, get_book, get_db, get_loan,
                      get_member, get_settings, init_db, lending_problem,
                      like_pattern, reservation_position, set_setting,
                      transaction)

# --------------------------------------------------------------------------
# 1. Setup
# --------------------------------------------------------------------------
load_dotenv()  # reads the optional .env file (see .env.example)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEV_SECRET_KEY = "dev-only-secret-key-change-me"   # used ONLY if SECRET_KEY is not set

app = Flask(__name__, instance_path=os.path.join(BASE_DIR, "instance"))
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or DEV_SECRET_KEY
app.config["DATABASE"] = os.environ.get("DATABASE_PATH") or os.path.join(
    app.instance_path, "libraryhub.sqlite3")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PER_PAGE"] = 10
app.config["REPORT_PREVIEW_ROWS"] = 200

csrf = CSRFProtect(app)          # every POST form must carry a hidden csrf_token
app.teardown_appcontext(close_db)

logging.basicConfig(level=logging.INFO)
if app.config["SECRET_KEY"] == DEV_SECRET_KEY:
    app.logger.warning("SECRET_KEY is not set - using the development key. "
                       "Copy .env.example to .env and set your own value.")

_initialised_databases = set()


@app.before_request
def make_sure_database_exists():
    """Create the database file and tables automatically on first run."""
    path = app.config["DATABASE"]
    if path not in _initialised_databases:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        init_db(path)
        _initialised_databases.add(path)


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: http: data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'")
    return response


# ---- Template helpers (filters) -------------------------------------------
@app.template_filter("dmy")
def format_date(value):
    """'2026-10-28' -> '28 Oct 2026'"""
    if not value:
        return "—"
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(value)


@app.template_filter("rupees")
def format_rupees(value):
    return f"₹{float(value or 0):,.2f}"


@app.template_filter("member_code")
def member_code(member_id):
    return f"MEM-{int(member_id):04d}"


@app.template_filter("loan_code")
def loan_code(loan_id):
    return f"LN-{int(loan_id):05d}"


app.jinja_env.globals["fine_status"] = fine_status


@app.context_processor
def inject_globals():
    """Variables available in every template."""
    data = {"today": date.today().isoformat(), "current_year": date.today().year}
    if session.get("admin_id"):
        db = get_db()
        settings = get_settings(db)
        data["settings"] = settings
        data["library_name"] = settings["library_name"]
        data["admin_username"] = session.get("admin_username", "admin")
        params = {"today": data["today"], "rate": settings["fine_per_day"]}
        data["overdue_count"] = db.execute(
            "SELECT COUNT(*) FROM loans WHERE returned_date IS NULL AND due_date < ?",
            (data["today"],)).fetchone()[0]
        data["overdue_alerts"] = db.execute(
            f"{LOAN_SELECT} WHERE t.status = 'overdue' ORDER BY t.days_overdue DESC LIMIT 5",
            params).fetchall()
    else:
        data.update(library_name="LibraryHub", settings=None, admin_username="",
                    overdue_count=0, overdue_alerts=[])
    return data


# --------------------------------------------------------------------------
# 2. Helper functions
# --------------------------------------------------------------------------
def today_str():
    return date.today().isoformat()


def clean(value):
    """Trim spaces; None becomes an empty string."""
    return (value or "").strip()


def parse_date(text):
    """'2026-10-28' -> date object, or None if the text is not a valid date."""
    try:
        return date.fromisoformat(clean(text))
    except ValueError:
        return None


def to_int(text):
    try:
        return int(clean(text))
    except ValueError:
        return None


def to_money(text):
    """Parse a non-negative amount with at most 2 decimals; None if invalid."""
    try:
        amount = round(float(clean(text)), 2)
    except ValueError:
        return None
    if math.isnan(amount) or math.isinf(amount) or amount < 0:
        return None
    return amount


def is_safe_redirect(target):
    """Only allow redirects to pages inside this website."""
    return bool(target) and target.startswith("/") and not target.startswith("//") and "\\" not in target


def login_required(view):
    """Decorator: the page can only be opened by a logged-in admin."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        admin_id = session.get("admin_id")
        if admin_id:
            exists = get_db().execute("SELECT 1 FROM admins WHERE id = ?", (admin_id,)).fetchone()
            if exists:
                return view(*args, **kwargs)
            session.clear()
        target = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("login", next=target))
    return wrapped


def paginate(db, select_sql, params, order_by, page, per_page=None):
    """
    Run `select_sql` one page at a time.
    `order_by` is always written by us in the code (never typed by the user).
    """
    per_page = per_page or app.config["PER_PAGE"]
    total = db.execute(f"SELECT COUNT(*) FROM ({select_sql})", params).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = min(max(page, 1), pages)
    rows = db.execute(
        f"{select_sql} ORDER BY {order_by} LIMIT :_limit OFFSET :_offset",
        {**params, "_limit": per_page, "_offset": (page - 1) * per_page}).fetchall()
    return rows, {"page": page, "pages": pages, "total": total, "per_page": per_page}


def loan_params(settings):
    return {"today": today_str(), "rate": settings["fine_per_day"]}


def flash_form_errors(errors):
    flash("Please fix the highlighted fields and try again.", "error")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{6,18}$")
ISBN_RE = re.compile(r"^(\d{13}|\d{9}[\dX])$")


# ---- Validation of the book and member forms ------------------------------
def validate_book(db, raw, book_id=None):
    """Check the book form. Returns (clean_values, errors_dict)."""
    errors = {}
    raw["isbn"] = re.sub(r"[-\s]", "", raw["isbn"]).upper()

    if not raw["title"]:
        errors["title"] = "Title is required."
    elif len(raw["title"]) > 200:
        errors["title"] = "Title must be 200 characters or fewer."
    if not raw["author"]:
        errors["author"] = "Author is required."
    elif len(raw["author"]) > 120:
        errors["author"] = "Author must be 120 characters or fewer."

    if not raw["isbn"]:
        errors["isbn"] = "ISBN is required."
    elif not ISBN_RE.match(raw["isbn"]):
        errors["isbn"] = "ISBN must be 10 or 13 digits (hyphens are allowed)."
    else:
        duplicate = db.execute("SELECT id FROM books WHERE isbn = ? AND id IS NOT ?",
                               (raw["isbn"], book_id)).fetchone()
        if duplicate:
            errors["isbn"] = "A book with this ISBN already exists."

    category_id = None
    if raw["category_id"]:
        category_id = to_int(raw["category_id"])
        if category_id is None or not db.execute(
                "SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
            errors["category_id"] = "Please choose a valid category."

    year = None
    if raw["publication_year"]:
        year = to_int(raw["publication_year"])
        if year is None or not (1000 <= year <= date.today().year + 1):
            errors["publication_year"] = f"Enter a year between 1000 and {date.today().year + 1}."

    total = to_int(raw["total_copies"])
    if total is None or not (1 <= total <= 1000):
        errors["total_copies"] = "Total copies must be a whole number from 1 to 1000."
    elif book_id:
        stock = get_book(db, book_id)
        in_use = stock["issued_copies"] + stock["held_copies"]
        if stock and total < in_use:
            errors["total_copies"] = (f"Cannot be lower than {in_use} "
                                      "(copies currently issued or set aside for reservations).")

    if len(raw["publisher"]) > 120:
        errors["publisher"] = "Publisher must be 120 characters or fewer."
    if len(raw["shelf"]) > 30:
        errors["shelf"] = "Shelf/location must be 30 characters or fewer."
    if len(raw["description"]) > 2000:
        errors["description"] = "Description must be 2000 characters or fewer."
    if raw["cover_url"]:
        if len(raw["cover_url"]) > 500 or not re.match(r"^https?://\S+$", raw["cover_url"], re.I):
            errors["cover_url"] = "Cover image must be a full http:// or https:// link."

    values = {**raw, "category_id": category_id, "publication_year": year, "total_copies": total}
    return values, errors


def validate_member(db, raw, member_id=None):
    errors = {}
    if len(raw["full_name"]) < 2:
        errors["full_name"] = "Full name must be at least 2 characters."
    elif len(raw["full_name"]) > 100:
        errors["full_name"] = "Full name must be 100 characters or fewer."

    if not raw["email"]:
        errors["email"] = "Email is required."
    elif len(raw["email"]) > 120 or not EMAIL_RE.match(raw["email"]):
        errors["email"] = "Enter a valid email address."
    elif db.execute("SELECT 1 FROM members WHERE email = ? AND id IS NOT ?",
                    (raw["email"], member_id)).fetchone():
        errors["email"] = "Another member already uses this email."

    if raw["phone"] and not PHONE_RE.match(raw["phone"]):
        errors["phone"] = "Phone may contain digits, spaces, + and - (7 to 20 characters)."
    if len(raw["address"]) > 250:
        errors["address"] = "Address must be 250 characters or fewer."

    if member_id is None:
        registered = parse_date(raw["registered_on"])
        if registered is None:
            errors["registered_on"] = "Enter a valid date."
        elif registered > date.today():
            errors["registered_on"] = "Registration date cannot be in the future."
    if raw["status"] not in ("active", "inactive"):
        errors["status"] = "Choose active or inactive."
    return errors


# --------------------------------------------------------------------------
# 3A. Login / logout / account
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    db = get_db()
    if session.get("admin_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = clean(request.form.get("username"))
        password = request.form.get("password") or ""
        admin = db.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
        # The same message is shown for a wrong username or password (so attackers learn nothing).
        if admin and check_password_hash(admin["password_hash"], password):
            session.clear()                       # start a fresh session after login
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            flash(f"Welcome back, {admin['username']}!", "success")
            target = request.args.get("next")
            return redirect(target if is_safe_redirect(target) else url_for("dashboard"))
        flash("Invalid username or password.", "error")

    no_admin = db.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 0
    return render_template("login.html", no_admin=no_admin)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/account/password", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    admin = db.execute("SELECT * FROM admins WHERE id = ?", (session["admin_id"],)).fetchone()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    if not check_password_hash(admin["password_hash"], current):
        flash("Your current password is not correct.", "error")
    elif len(new) < 8:
        flash("The new password must be at least 8 characters long.", "error")
    elif new != confirm:
        flash("The new password and its confirmation do not match.", "error")
    else:
        db.execute("UPDATE admins SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(new), admin["id"]))
        flash("Password changed successfully.", "success")
    return redirect(url_for("settings"))


# --------------------------------------------------------------------------
# 3B. Dashboard
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    db = get_db()
    settings = get_settings(db)
    params = loan_params(settings)
    today = params["today"]

    def one(sql, args=()):
        return db.execute(sql, args).fetchone()[0]

    total_copies = one("SELECT COALESCE(SUM(total_copies), 0) FROM books")
    issued = one("SELECT COUNT(*) FROM loans WHERE returned_date IS NULL")
    stats = {
        "titles": one("SELECT COUNT(*) FROM books"),
        "copies": total_copies,
        "available": total_copies - issued,
        "members": one("SELECT COUNT(*) FROM members"),
        "active_members": one("SELECT COUNT(*) FROM members WHERE status = 'active'"),
        "issued": issued,
        "overdue": one("SELECT COUNT(*) FROM loans WHERE returned_date IS NULL AND due_date < ?", (today,)),
        "pending_reservations": one("SELECT COUNT(*) FROM reservations WHERE status = 'pending'"),
        "ready_reservations": one("SELECT COUNT(*) FROM reservations WHERE status = 'ready'"),
    }

    recent = db.execute("""
        SELECT * FROM (
            SELECT l.id AS loan_id, 'Issued' AS action, l.issue_date AS event_date,
                   b.title AS book_title, m.full_name AS member_name
              FROM loans l JOIN books b ON b.id = l.book_id JOIN members m ON m.id = l.member_id
            UNION ALL
            SELECT l.id, 'Returned', l.returned_date, b.title, m.full_name
              FROM loans l JOIN books b ON b.id = l.book_id JOIN members m ON m.id = l.member_id
             WHERE l.returned_date IS NOT NULL
        ) ORDER BY event_date DESC, loan_id DESC LIMIT 8""").fetchall()

    popular = db.execute("""
        SELECT b.id, b.title, b.author, COUNT(l.id) AS borrow_count
          FROM books b JOIN loans l ON l.book_id = b.id
         GROUP BY b.id ORDER BY borrow_count DESC, b.title LIMIT 5""").fetchall()

    category_rows = db.execute("""
        SELECT COALESCE(c.name, 'Uncategorized') AS name, COUNT(b.id) AS titles
          FROM books b LEFT JOIN categories c ON c.id = b.category_id
         GROUP BY c.id ORDER BY titles DESC, name""").fetchall()

    overdue_loans = db.execute(
        f"{LOAN_SELECT} WHERE t.status = 'overdue' ORDER BY t.days_overdue DESC LIMIT 5", params).fetchall()

    return render_template(
        "dashboard.html", stats=stats, recent=recent, popular=popular,
        category_labels=[r["name"] for r in category_rows],
        category_values=[r["titles"] for r in category_rows],
        overdue_loans=overdue_loans)


# --------------------------------------------------------------------------
# 3C. Books
# --------------------------------------------------------------------------
BOOK_FIELDS = ["title", "author", "isbn", "category_id", "publisher", "publication_year",
               "description", "shelf", "total_copies", "cover_url"]


def categories_list(db):
    return db.execute("SELECT * FROM categories ORDER BY name").fetchall()


@app.route("/books")
@login_required
def books():
    db = get_db()
    q = clean(request.args.get("q"))
    category = request.args.get("category", type=int)
    availability = request.args.get("availability", "")

    where, params = [], {}
    if q:
        where.append("(title LIKE :q ESCAPE '\\' OR author LIKE :q ESCAPE '\\' "
                     "OR isbn LIKE :isbn ESCAPE '\\')")
        params["q"] = like_pattern(q)
        params["isbn"] = like_pattern(re.sub(r"[-\s]", "", q))
    if category:
        where.append("category_id = :category")
        params["category"] = category
    if availability == "available":
        where.append("available_copies > 0")
    elif availability == "unavailable":
        where.append("available_copies <= 0")

    sql = f"SELECT * FROM ({BOOK_SELECT})"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows, page = paginate(db, sql, params, "title COLLATE NOCASE", request.args.get("page", 1, type=int))
    return render_template("books.html", books=rows, page=page, categories=categories_list(db),
                           q=q, category=category, availability=availability)


@app.route("/books/new", methods=["GET", "POST"])
@login_required
def book_new():
    db = get_db()
    raw, errors = {f: "" for f in BOOK_FIELDS}, {}
    raw["total_copies"] = "1"

    if request.method == "POST":
        raw = {f: clean(request.form.get(f)) for f in BOOK_FIELDS}
        values, errors = validate_book(db, raw)
        if not errors:
            try:
                db.execute(
                    "INSERT INTO books (title, author, isbn, category_id, publisher, publication_year, "
                    "description, shelf, total_copies, cover_url) "
                    "VALUES (:title, :author, :isbn, :category_id, :publisher, :publication_year, "
                    ":description, :shelf, :total_copies, :cover_url)", values)
            except sqlite3.IntegrityError:
                errors["isbn"] = "A book with this ISBN already exists."
            else:
                flash(f"Book “{values['title']}” was added.", "success")
                return redirect(url_for("books"))
        flash_form_errors(errors)
    return render_template("book_form.html", form=raw, errors=errors, editing=False,
                           book=None, categories=categories_list(db))


@app.route("/books/<int:book_id>")
@login_required
def book_detail(book_id):
    db = get_db()
    settings = get_settings(db)
    book = get_book(db, book_id)
    if book is None:
        return render_template("errors/404.html", message="That book does not exist."), 404
    params = {**loan_params(settings), "book_id": book_id}
    active_loans = db.execute(
        f"{LOAN_SELECT} WHERE t.book_id = :book_id AND t.returned_date IS NULL ORDER BY t.due_date", params).fetchall()
    history = db.execute(
        f"{LOAN_SELECT} WHERE t.book_id = :book_id AND t.returned_date IS NOT NULL "
        "ORDER BY t.returned_date DESC, t.id DESC LIMIT 10", params).fetchall()
    times_borrowed = db.execute("SELECT COUNT(*) FROM loans WHERE book_id = ?", (book_id,)).fetchone()[0]
    queue = db.execute(
        "SELECT r.*, m.full_name AS member_name FROM reservations r JOIN members m ON m.id = r.member_id "
        "WHERE r.book_id = ? AND r.status IN ('ready', 'pending') ORDER BY r.status DESC, r.id",
        (book_id,)).fetchall()
    return render_template("book_detail.html", book=book, active_loans=active_loans, history=history,
                           times_borrowed=times_borrowed, queue=queue)


@app.route("/books/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def book_edit(book_id):
    db = get_db()
    book = get_book(db, book_id)
    if book is None:
        return render_template("errors/404.html", message="That book does not exist."), 404

    errors = {}
    raw = {f: ("" if book[f] is None else str(book[f])) for f in BOOK_FIELDS}
    if request.method == "POST":
        raw = {f: clean(request.form.get(f)) for f in BOOK_FIELDS}
        values, errors = validate_book(db, raw, book_id)
        if not errors:
            values["id"] = book_id
            try:
                db.execute(
                    "UPDATE books SET title=:title, author=:author, isbn=:isbn, category_id=:category_id, "
                    "publisher=:publisher, publication_year=:publication_year, description=:description, "
                    "shelf=:shelf, total_copies=:total_copies, cover_url=:cover_url WHERE id=:id", values)
            except sqlite3.IntegrityError:
                errors["isbn"] = "A book with this ISBN already exists."
            else:
                flash(f"Book “{values['title']}” was updated.", "success")
                return redirect(url_for("book_detail", book_id=book_id))
        flash_form_errors(errors)
    return render_template("book_form.html", form=raw, errors=errors, editing=True,
                           book=book, categories=categories_list(db))


@app.route("/books/<int:book_id>/delete", methods=["POST"])
@login_required
def book_delete(book_id):
    db = get_db()
    book = get_book(db, book_id)
    if book is None:
        return render_template("errors/404.html", message="That book does not exist."), 404
    history = db.execute(
        "SELECT (SELECT COUNT(*) FROM loans WHERE book_id = ?) + "
        "(SELECT COUNT(*) FROM reservations WHERE book_id = ?)", (book_id, book_id)).fetchone()[0]
    if history:
        flash("This book has loan or reservation history, so it cannot be deleted "
              "(this protects your records).", "error")
        return redirect(url_for("book_detail", book_id=book_id))
    db.execute("DELETE FROM books WHERE id = ?", (book_id,))
    flash(f"Book “{book['title']}” was deleted.", "success")
    return redirect(url_for("books"))


# --------------------------------------------------------------------------
# 3D. Members
# --------------------------------------------------------------------------
MEMBER_FIELDS = ["full_name", "email", "phone", "address", "registered_on", "status"]


@app.route("/members")
@login_required
def members():
    db = get_db()
    q = clean(request.args.get("q"))
    status = request.args.get("status", "")
    params = {"today": today_str()}
    where = []
    if q:
        where.append("(full_name LIKE :q ESCAPE '\\' OR email LIKE :q ESCAPE '\\' "
                     "OR phone LIKE :q ESCAPE '\\' OR CAST(id AS TEXT) = :qid)")
        params["q"] = like_pattern(q)
        params["qid"] = re.sub(r"\D", "", q).lstrip("0") or "-1"
    if status in ("active", "inactive"):
        where.append("status = :status")
        params["status"] = status
    elif status == "overdue":
        where.append("overdue_loans > 0")

    sql = f"SELECT * FROM ({MEMBER_SELECT})"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows, page = paginate(db, sql, params, "full_name COLLATE NOCASE", request.args.get("page", 1, type=int))
    return render_template("members.html", members=rows, page=page, q=q, status=status)


@app.route("/members/new", methods=["GET", "POST"])
@login_required
def member_new():
    db = get_db()
    raw = {"full_name": "", "email": "", "phone": "", "address": "",
           "registered_on": today_str(), "status": "active"}
    errors = {}
    if request.method == "POST":
        raw = {f: clean(request.form.get(f)) for f in MEMBER_FIELDS}
        raw["status"] = "active"
        errors = validate_member(db, raw)
        if not errors:
            try:
                db.execute("INSERT INTO members (full_name, email, phone, address, registered_on, status) "
                           "VALUES (:full_name, :email, :phone, :address, :registered_on, :status)", raw)
            except sqlite3.IntegrityError:
                errors["email"] = "Another member already uses this email."
            else:
                flash(f"Member {raw['full_name']} was registered.", "success")
                return redirect(url_for("members"))
        flash_form_errors(errors)
    return render_template("member_form.html", form=raw, errors=errors, editing=False, member=None)


@app.route("/members/<int:member_id>")
@login_required
def member_detail(member_id):
    db = get_db()
    settings = get_settings(db)
    member = get_member(db, member_id, today_str())
    if member is None:
        return render_template("errors/404.html", message="That member does not exist."), 404
    params = {**loan_params(settings), "member_id": member_id}
    active = db.execute(f"{LOAN_SELECT} WHERE t.member_id = :member_id AND t.returned_date IS NULL "
                        "ORDER BY t.due_date", params).fetchall()
    history = db.execute(f"{LOAN_SELECT} WHERE t.member_id = :member_id ORDER BY t.id DESC", params).fetchall()
    reservations = db.execute(
        "SELECT r.*, b.title AS book_title FROM reservations r JOIN books b ON b.id = r.book_id "
        "WHERE r.member_id = ? ORDER BY r.id DESC", (member_id,)).fetchall()
    unpaid_total = sum(max(l["fine_due"] - l["fine_paid"], 0) for l in history if l["status"] == "returned")
    return render_template("member_detail.html", member=member, active=active, history=history,
                           reservations=reservations, unpaid_total=unpaid_total)


@app.route("/members/<int:member_id>/edit", methods=["GET", "POST"])
@login_required
def member_edit(member_id):
    db = get_db()
    member = get_member(db, member_id, today_str())
    if member is None:
        return render_template("errors/404.html", message="That member does not exist."), 404
    raw = {f: member[f] or "" for f in MEMBER_FIELDS}
    errors = {}
    if request.method == "POST":
        raw = {f: clean(request.form.get(f)) for f in MEMBER_FIELDS}
        raw["registered_on"] = member["registered_on"]      # registration date is fixed
        errors = validate_member(db, raw, member_id)
        if not errors:
            try:
                db.execute("UPDATE members SET full_name=:full_name, email=:email, phone=:phone, "
                           "address=:address, status=:status WHERE id=:id", {**raw, "id": member_id})
            except sqlite3.IntegrityError:
                errors["email"] = "Another member already uses this email."
            else:
                flash(f"Member {raw['full_name']} was updated.", "success")
                return redirect(url_for("member_detail", member_id=member_id))
        flash_form_errors(errors)
    return render_template("member_form.html", form=raw, errors=errors, editing=True, member=member)


@app.route("/members/<int:member_id>/toggle", methods=["POST"])
@login_required
def member_toggle(member_id):
    db = get_db()
    member = get_member(db, member_id, today_str())
    if member is None:
        return render_template("errors/404.html", message="That member does not exist."), 404
    new_status = "inactive" if member["status"] == "active" else "active"
    db.execute("UPDATE members SET status = ? WHERE id = ?", (new_status, member_id))
    flash(f"{member['full_name']}'s membership is now {new_status}.", "success")
    return redirect(url_for("member_detail", member_id=member_id))


@app.route("/members/<int:member_id>/delete", methods=["POST"])
@login_required
def member_delete(member_id):
    db = get_db()
    member = get_member(db, member_id, today_str())
    if member is None:
        return render_template("errors/404.html", message="That member does not exist."), 404
    history = db.execute(
        "SELECT (SELECT COUNT(*) FROM loans WHERE member_id = ?) + "
        "(SELECT COUNT(*) FROM reservations WHERE member_id = ?)", (member_id, member_id)).fetchone()[0]
    if history:
        flash("This member has transaction history and cannot be deleted. "
              "You can deactivate the membership instead.", "error")
        return redirect(url_for("member_detail", member_id=member_id))
    db.execute("DELETE FROM members WHERE id = ?", (member_id,))
    flash(f"Member {member['full_name']} was deleted.", "success")
    return redirect(url_for("members"))


# --------------------------------------------------------------------------
# 3E. Loans: list, issue, return, fines
# --------------------------------------------------------------------------
@app.route("/loans")
@login_required
def loans():
    db = get_db()
    settings = get_settings(db)
    q = clean(request.args.get("q"))
    status = request.args.get("status", "")
    params = loan_params(settings)
    where = []
    if q:
        where.append("(t.member_name LIKE :q ESCAPE '\\' OR t.book_title LIKE :q ESCAPE '\\' "
                     "OR CAST(t.id AS TEXT) = :qid)")
        params["q"] = like_pattern(q)
        params["qid"] = re.sub(r"\D", "", q).lstrip("0") or "-1"
    if status in ("active", "overdue", "returned"):
        where.append("t.status = :status")
        params["status"] = status
    elif status == "unpaid_fines":
        where.append("t.status = 'returned' AND t.fine_amount > 0 AND t.fine_paid + 0.005 < t.fine_amount")

    sql = LOAN_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    rows, page = paginate(db, sql, params, "t.id DESC", request.args.get("page", 1, type=int), per_page=15)
    return render_template("loans.html", loans=rows, page=page, q=q, status=status)


def issue_form_context(db, settings, form):
    members_ = db.execute("SELECT id, full_name, email FROM members WHERE status = 'active' "
                          "ORDER BY full_name COLLATE NOCASE").fetchall()
    books_ = db.execute(f"SELECT * FROM ({BOOK_SELECT}) ORDER BY title COLLATE NOCASE").fetchall()
    return {"members_list": members_, "books_list": books_, "form": form}


@app.route("/loans/issue", methods=["GET", "POST"])
@login_required
def issue_book():
    db = get_db()
    settings = get_settings(db)
    today = today_str()
    form = {
        "member_id": request.args.get("member_id", ""),
        "book_id": request.args.get("book_id", ""),
        "issue_date": today,
        "due_date": (date.today() + timedelta(days=settings["loan_days"])).isoformat(),
    }

    if request.method == "POST":
        form = {k: clean(request.form.get(k)) for k in ("member_id", "book_id", "issue_date", "due_date")}
        issue_date, due_date = parse_date(form["issue_date"]), parse_date(form["due_date"])
        member_id, book_id = to_int(form["member_id"]), to_int(form["book_id"])
        problem = None

        if member_id is None or book_id is None:
            problem = "Please choose both a member and a book."
        elif issue_date is None or due_date is None:
            problem = "Please enter valid issue and due dates."
        elif issue_date > date.today():
            problem = "The issue date cannot be in the future."
        elif due_date < issue_date:
            problem = "The due date cannot be before the issue date."
        elif (due_date - issue_date).days > 365:
            problem = "The due date must be within 365 days of the issue date."

        if problem is None:
            try:
                with transaction(db):
                    # Re-read everything INSIDE the transaction so the numbers are up to date.
                    member = get_member(db, member_id, today)
                    book = get_book(db, book_id)
                    problem, reservation = lending_problem(db, member, book, settings, today)
                    if problem is None:
                        cursor = db.execute(
                            "INSERT INTO loans (book_id, member_id, issue_date, due_date) VALUES (?, ?, ?, ?)",
                            (book_id, member_id, issue_date.isoformat(), due_date.isoformat()))
                        loan_id = cursor.lastrowid
                        if reservation is not None:
                            db.execute("UPDATE reservations SET status = 'fulfilled', closed_on = ?, loan_id = ? "
                                       "WHERE id = ?", (today, loan_id, reservation["id"]))
            except sqlite3.IntegrityError as error:
                app.logger.warning("Issue blocked by database rule: %s", error)
                problem = "The database refused this loan (no copy available)."

        if problem is None:
            flash(f"Loan {loan_code(loan_id)} created: “{book['title']}” issued to {member['full_name']}, "
                  f"due {format_date(due_date.isoformat())}.", "success")
            return redirect(url_for("loans"))
        flash(problem, "error")

    return render_template("issue_book.html", **issue_form_context(db, settings, form))


@app.route("/loans/<int:loan_id>/return", methods=["GET", "POST"])
@login_required
def return_book(loan_id):
    db = get_db()
    settings = get_settings(db)
    loan = get_loan(db, loan_id, today_str(), settings["fine_per_day"])
    if loan is None:
        return render_template("errors/404.html", message="That loan does not exist."), 404
    if loan["returned_date"]:
        flash("This book has already been returned.", "error")
        return redirect(url_for("loans"))

    return_date_text = clean(request.values.get("return_date")) or today_str()
    return_date = parse_date(return_date_text)
    if request.method == "POST":
        paid_now = to_money(request.form.get("amount_paid") or "0")
        problem = None
        if return_date is None:
            problem = "Please enter a valid return date."
        elif return_date > date.today():
            problem = "The return date cannot be in the future."
        elif return_date.isoformat() < loan["issue_date"]:
            problem = "The return date cannot be before the issue date."
        elif paid_now is None:
            problem = "Enter the amount collected as a number (0 or more)."

        if problem is None:
            days, fine = calculate_fine(loan["due_date"], return_date.isoformat(), settings["fine_per_day"])
            if paid_now > fine:
                problem = f"The amount collected cannot be more than the fine ({format_rupees(fine)})."

        if problem is None:
            with transaction(db):
                fresh = db.execute("SELECT returned_date FROM loans WHERE id = ?", (loan_id,)).fetchone()
                if fresh["returned_date"] is not None:        # someone else returned it a moment ago
                    problem = "This book has already been returned."
                else:
                    db.execute("UPDATE loans SET returned_date = ?, fine_amount = ? WHERE id = ?",
                               (return_date.isoformat(), fine, loan_id))
                    if paid_now > 0:
                        db.execute("INSERT INTO fine_payments (loan_id, amount, paid_on, note) VALUES (?, ?, ?, ?)",
                                   (loan_id, paid_now, today_str(), "Collected at return"))
        if problem is None:
            message = f"“{loan['book_title']}” returned by {loan['member_name']}."
            if fine > 0:
                message += f" Fine: {format_rupees(fine)} for {days} day(s), collected {format_rupees(paid_now)}."
            flash(message, "success")
            waiting = db.execute("SELECT COUNT(*) FROM reservations WHERE book_id = ? AND status = 'pending'",
                                 (loan["book_id"],)).fetchone()[0]
            if waiting:
                flash(f"{waiting} member(s) are waiting for this book. "
                      "Open Reservations and mark the first one as ready.", "info")
            return redirect(url_for("loans"))
        flash(problem, "error")

    preview_date = return_date or date.today()
    preview_days, preview_fine = calculate_fine(loan["due_date"], max(preview_date, date.fromisoformat(loan["issue_date"])).isoformat(),
                                                settings["fine_per_day"])
    return render_template("return_book.html", loan=loan, return_date=return_date_text,
                           preview_days=preview_days, preview_fine=preview_fine)


@app.route("/loans/<int:loan_id>/fine", methods=["POST"])
@login_required
def record_fine_payment(loan_id):
    db = get_db()
    settings = get_settings(db)
    loan = get_loan(db, loan_id, today_str(), settings["fine_per_day"])
    if loan is None:
        return render_template("errors/404.html", message="That loan does not exist."), 404
    back = url_for("loans", status="unpaid_fines")

    if loan["returned_date"] is None or loan["fine_amount"] <= 0:
        flash("There is no fine to pay for this loan.", "error")
        return redirect(back)

    with transaction(db):
        paid = db.execute("SELECT COALESCE(SUM(amount), 0) FROM fine_payments WHERE loan_id = ?",
                          (loan_id,)).fetchone()[0]
        balance = round(loan["fine_amount"] - paid, 2)
        if request.form.get("action") == "pay_all":
            amount = balance
        else:
            amount = to_money(request.form.get("amount"))
        if balance <= 0:
            problem = "This fine is already fully paid."
        elif amount is None or amount <= 0:
            problem = "Enter a payment amount greater than zero."
        elif amount > balance + 0.005:
            problem = f"The payment cannot be more than the balance ({format_rupees(balance)})."
        else:
            problem = None
            db.execute("INSERT INTO fine_payments (loan_id, amount, paid_on, note) VALUES (?, ?, ?, ?)",
                       (loan_id, min(amount, balance), today_str(), clean(request.form.get("note"))[:100]))
    if problem:
        flash(problem, "error")
    else:
        flash(f"Payment of {format_rupees(min(amount, balance))} recorded for {loan_code(loan_id)}.", "success")
    return redirect(back)


# --------------------------------------------------------------------------
# 3F. Overdue books
# --------------------------------------------------------------------------
@app.route("/overdue")
@login_required
def overdue():
    db = get_db()
    settings = get_settings(db)
    q = clean(request.args.get("q"))
    min_days = request.args.get("min_days", 0, type=int)
    params = {**loan_params(settings), "min_days": min_days}
    where = ["t.status = 'overdue'", "t.days_overdue >= :min_days"]
    if q:
        where.append("(t.member_name LIKE :q ESCAPE '\\' OR t.book_title LIKE :q ESCAPE '\\')")
        params["q"] = like_pattern(q)

    rows = db.execute(f"{LOAN_SELECT} WHERE {' AND '.join(where)} ORDER BY t.days_overdue DESC, t.id", params).fetchall()
    unpaid = db.execute(
        f"{LOAN_SELECT} WHERE t.status = 'returned' AND t.fine_amount > 0 AND t.fine_paid + 0.005 < t.fine_amount "
        "ORDER BY t.returned_date DESC", params).fetchall()
    totals = {
        "estimated": sum(r["fine_due"] for r in rows),
        "unpaid": sum(r["fine_due"] - r["fine_paid"] for r in unpaid),
    }
    return render_template("overdue.html", loans=rows, unpaid=unpaid, totals=totals, q=q, min_days=min_days)


# --------------------------------------------------------------------------
# 3G. Reservations (managed by the administrator for members)
# --------------------------------------------------------------------------
RESERVATION_SELECT = f"""
SELECT r.*, b.title AS book_title, b.free_copies, b.available_copies,
       m.full_name AS member_name, m.status AS member_status,
       CASE WHEN r.status = 'pending'
            THEN (SELECT COUNT(*) + 1 FROM reservations r2
                   WHERE r2.book_id = r.book_id AND r2.status = 'pending' AND r2.id < r.id)
       END AS position
FROM reservations r
JOIN ({BOOK_SELECT}) b ON b.id = r.book_id
JOIN members m ON m.id = r.member_id
"""


@app.route("/reservations")
@login_required
def reservations():
    db = get_db()
    status = request.args.get("status", "open")
    q = clean(request.args.get("q"))
    where, params = [], {}
    if status == "open":
        where.append("r.status IN ('pending', 'ready')")
    elif status in ("pending", "ready", "fulfilled", "cancelled"):
        where.append("r.status = :status")
        params["status"] = status
    if q:
        where.append("(m.full_name LIKE :q ESCAPE '\\' OR b.title LIKE :q ESCAPE '\\')")
        params["q"] = like_pattern(q)
    sql = RESERVATION_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    rows, page = paginate(db, sql, params, "r.book_id, r.id", request.args.get("page", 1, type=int), per_page=15)

    members_ = db.execute("SELECT id, full_name FROM members WHERE status = 'active' "
                          "ORDER BY full_name COLLATE NOCASE").fetchall()
    unavailable_books = db.execute(
        f"SELECT * FROM ({BOOK_SELECT}) WHERE free_copies <= 0 ORDER BY title COLLATE NOCASE").fetchall()
    return render_template("reservations.html", reservations=rows, page=page, status=status, q=q,
                           members_list=members_, books_list=unavailable_books)


@app.route("/reservations/new", methods=["POST"])
@login_required
def reservation_new():
    db = get_db()
    member_id = to_int(request.form.get("member_id"))
    book_id = to_int(request.form.get("book_id"))
    member = get_member(db, member_id, today_str()) if member_id else None
    book = get_book(db, book_id) if book_id else None

    if member is None or book is None:
        flash("Please choose a registered member and a book.", "error")
    elif member["status"] != "active":
        flash(f"{member['full_name']}'s membership is inactive.", "error")
    elif book["free_copies"] > 0:
        flash("Copies of this book are available now - issue it directly instead of reserving.", "error")
    elif db.execute("SELECT 1 FROM loans WHERE book_id = ? AND member_id = ? AND returned_date IS NULL",
                    (book_id, member_id)).fetchone():
        flash(f"{member['full_name']} already has a copy of this book.", "error")
    elif db.execute("SELECT 1 FROM reservations WHERE book_id = ? AND member_id = ? "
                    "AND status IN ('pending', 'ready')", (book_id, member_id)).fetchone():
        flash(f"{member['full_name']} already has an active reservation for this book.", "error")
    else:
        try:
            db.execute("INSERT INTO reservations (book_id, member_id, reserved_on) VALUES (?, ?, ?)",
                       (book_id, member_id, today_str()))
        except sqlite3.IntegrityError:
            flash("This member already has an active reservation for this book.", "error")
        else:
            flash(f"“{book['title']}” reserved for {member['full_name']}.", "success")
    return redirect(url_for("reservations"))


def get_reservation(db, reservation_id):
    return db.execute(RESERVATION_SELECT + " WHERE r.id = ?", (reservation_id,)).fetchone()


@app.route("/reservations/<int:reservation_id>/cancel", methods=["POST"])
@login_required
def reservation_cancel(reservation_id):
    db = get_db()
    reservation = get_reservation(db, reservation_id)
    if reservation is None:
        return render_template("errors/404.html", message="That reservation does not exist."), 404
    if reservation["status"] not in ("pending", "ready"):
        flash("Only open reservations can be cancelled.", "error")
    else:
        db.execute("UPDATE reservations SET status = 'cancelled', closed_on = ? WHERE id = ?",
                   (today_str(), reservation_id))
        flash("Reservation cancelled.", "success")
    return redirect(url_for("reservations"))


@app.route("/reservations/<int:reservation_id>/ready", methods=["POST"])
@login_required
def reservation_ready(reservation_id):
    db = get_db()
    with transaction(db):
        reservation = get_reservation(db, reservation_id)
        if reservation is None:
            problem = "That reservation does not exist."
        elif reservation["status"] != "pending":
            problem = "Only pending reservations can be marked ready."
        elif reservation["position"] != 1:
            problem = (f"This member is number {reservation['position']} in the queue. "
                       "Reservations are served first come, first served.")
        elif reservation["free_copies"] <= 0:
            problem = "No copy is free yet. Wait until a copy is returned."
        else:
            problem = None
            db.execute("UPDATE reservations SET status = 'ready', ready_on = ? WHERE id = ?",
                       (today_str(), reservation_id))
    if problem:
        flash(problem, "error")
    else:
        flash(f"Reservation marked ready: a copy of “{reservation['book_title']}” is now held "
              f"for {reservation['member_name']}.", "success")
    return redirect(url_for("reservations"))


# --------------------------------------------------------------------------
# 3H. Categories
# --------------------------------------------------------------------------
@app.route("/categories")
@login_required
def categories():
    db = get_db()
    rows = db.execute("""
        SELECT c.*, COUNT(b.id) AS book_count
          FROM categories c LEFT JOIN books b ON b.category_id = c.id
         GROUP BY c.id ORDER BY c.name COLLATE NOCASE""").fetchall()
    return render_template("categories.html", categories=rows)


def check_category_form(db, category_id=None):
    name = clean(request.form.get("name"))
    description = clean(request.form.get("description"))
    if not name:
        return None, None, "Category name is required."
    if len(name) > 60:
        return None, None, "Category name must be 60 characters or fewer."
    if len(description) > 200:
        return None, None, "Description must be 200 characters or fewer."
    if db.execute("SELECT 1 FROM categories WHERE name = ? AND id IS NOT ?", (name, category_id)).fetchone():
        return None, None, f"A category named “{name}” already exists."
    return name, description, None


@app.route("/categories/new", methods=["POST"])
@login_required
def category_new():
    db = get_db()
    name, description, problem = check_category_form(db)
    if problem:
        flash(problem, "error")
    else:
        db.execute("INSERT INTO categories (name, description) VALUES (?, ?)", (name, description))
        flash(f"Category “{name}” was added.", "success")
    return redirect(url_for("categories"))


@app.route("/categories/<int:category_id>/edit", methods=["POST"])
@login_required
def category_edit(category_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
        return render_template("errors/404.html", message="That category does not exist."), 404
    name, description, problem = check_category_form(db, category_id)
    if problem:
        flash(problem, "error")
    else:
        db.execute("UPDATE categories SET name = ?, description = ? WHERE id = ?", (name, description, category_id))
        flash(f"Category “{name}” was updated.", "success")
    return redirect(url_for("categories"))


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
@login_required
def category_delete(category_id):
    db = get_db()
    category = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if category is None:
        return render_template("errors/404.html", message="That category does not exist."), 404
    count = db.execute("SELECT COUNT(*) FROM books WHERE category_id = ?", (category_id,)).fetchone()[0]
    if count:
        flash(f"“{category['name']}” still has {count} book(s). Move or delete those books first.", "error")
    else:
        db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        flash(f"Category “{category['name']}” was deleted.", "success")
    return redirect(url_for("categories"))


# --------------------------------------------------------------------------
# 3I. Reports and CSV export
# --------------------------------------------------------------------------
REPORTS = {
    "inventory": "Total inventory",
    "availability": "Available / issued copies",
    "current_loans": "Current loans",
    "overdue": "Overdue books",
    "member_history": "Member borrowing history",
    "most_borrowed": "Most borrowed books",
    "fines": "Fine collection",
}
DATE_REPORTS = {"current_loans", "member_history", "most_borrowed", "fines"}


def build_report(db, name, args):
    """
    Build one report.  Returns a dictionary:
        title, headers, rows (list of lists), errors (list), summary (list of (label, value))
    """
    settings = get_settings(db)
    params = loan_params(settings)
    report = {"name": name, "title": REPORTS[name], "headers": [], "rows": [], "errors": [], "summary": []}

    # ---- validate the date range -------------------------------------------
    start_text, end_text = clean(args.get("start")), clean(args.get("end"))
    start = end = None
    if name in DATE_REPORTS:
        if start_text:
            start = parse_date(start_text)
            if start is None:
                report["errors"].append("The start date is not a valid date.")
        if end_text:
            end = parse_date(end_text)
            if end is None:
                report["errors"].append("The end date is not a valid date.")
        if start and end and start > end:
            report["errors"].append("The start date must not be after the end date.")
    member_id = to_int(args.get("member_id", ""))
    if name == "member_history":
        if member_id is None:
            report["errors"].append("Choose a member to see their borrowing history.")
        elif not db.execute("SELECT 1 FROM members WHERE id = ?", (member_id,)).fetchone():
            report["errors"].append("That member does not exist.")
    if report["errors"]:
        return report

    def date_filter(column):
        clause, extra = "", {}
        if start:
            clause += f" AND {column} >= :start"
            extra["start"] = start.isoformat()
        if end:
            clause += f" AND {column} <= :end"
            extra["end"] = end.isoformat()
        return clause, extra

    if name == "inventory":
        report["headers"] = ["Title", "Author", "ISBN", "Category", "Publisher", "Year", "Shelf", "Total copies"]
        for b in db.execute(f"SELECT * FROM ({BOOK_SELECT}) ORDER BY title COLLATE NOCASE"):
            report["rows"].append([b["title"], b["author"], b["isbn"], b["category_name"] or "Uncategorized",
                                   b["publisher"], b["publication_year"] or "", b["shelf"], b["total_copies"]])
        report["summary"] = [("Titles", len(report["rows"])),
                             ("Physical copies", sum(r[7] for r in report["rows"]))]

    elif name == "availability":
        report["headers"] = ["Title", "ISBN", "Total copies", "Issued", "Held for reservations",
                             "Available on shelf", "Free to lend"]
        for b in db.execute(f"SELECT * FROM ({BOOK_SELECT}) ORDER BY title COLLATE NOCASE"):
            report["rows"].append([b["title"], b["isbn"], b["total_copies"], b["issued_copies"],
                                   b["held_copies"], b["available_copies"], b["free_copies"]])
        report["summary"] = [("Total copies", sum(r[2] for r in report["rows"])),
                             ("Issued", sum(r[3] for r in report["rows"])),
                             ("Available on shelf", sum(r[5] for r in report["rows"]))]

    elif name == "current_loans":
        clause, extra = date_filter("t.issue_date")
        report["headers"] = ["Loan ID", "Member", "Book", "Issue date", "Due date", "Status", "Days overdue"]
        for l in db.execute(f"{LOAN_SELECT} WHERE t.returned_date IS NULL{clause} ORDER BY t.due_date, t.id",
                            {**params, **extra}):
            report["rows"].append([loan_code(l["id"]), l["member_name"], l["book_title"], l["issue_date"],
                                   l["due_date"], l["status"].title(), l["days_overdue"]])
        report["summary"] = [("Loans currently out", len(report["rows"]))]

    elif name == "overdue":
        report["headers"] = ["Loan ID", "Member", "Book", "Issue date", "Due date", "Days overdue",
                             "Estimated fine (INR)"]
        total = 0
        for l in db.execute(f"{LOAN_SELECT} WHERE t.status = 'overdue' ORDER BY t.days_overdue DESC, t.id", params):
            total += l["fine_due"]
            report["rows"].append([loan_code(l["id"]), l["member_name"], l["book_title"], l["issue_date"],
                                   l["due_date"], l["days_overdue"], f"{l['fine_due']:.2f}"])
        report["summary"] = [("Overdue loans", len(report["rows"])), ("Estimated fines", format_rupees(total))]

    elif name == "member_history":
        clause, extra = date_filter("t.issue_date")
        report["headers"] = ["Loan ID", "Book", "Issue date", "Due date", "Returned on", "Status",
                             "Fine (INR)", "Paid (INR)", "Fine status"]
        member = db.execute("SELECT full_name FROM members WHERE id = ?", (member_id,)).fetchone()
        report["title"] = f"Borrowing history - {member['full_name']}"
        for l in db.execute(f"{LOAN_SELECT} WHERE t.member_id = :member_id{clause} ORDER BY t.id DESC",
                            {**params, **extra, "member_id": member_id}):
            report["rows"].append([loan_code(l["id"]), l["book_title"], l["issue_date"], l["due_date"],
                                   l["returned_date"] or "", l["status"].title(), f"{l['fine_due']:.2f}",
                                   f"{l['fine_paid']:.2f}",
                                   fine_status(l["fine_due"], l["fine_paid"]).title()
                                   if l["status"] == "returned" else ""])
        report["summary"] = [("Loans in this period", len(report["rows"]))]

    elif name == "most_borrowed":
        clause, extra = date_filter("l.issue_date")
        report["headers"] = ["Rank", "Title", "Author", "ISBN", "Times borrowed"]
        for rank, b in enumerate(db.execute(
                f"SELECT b.title, b.author, b.isbn, COUNT(l.id) AS times FROM books b JOIN loans l ON l.book_id = b.id "
                f"WHERE 1 = 1{clause} GROUP BY b.id ORDER BY times DESC, b.title LIMIT 50", extra), start=1):
            report["rows"].append([rank, b["title"], b["author"], b["isbn"], b["times"]])
        report["summary"] = [("Books listed", len(report["rows"]))]

    elif name == "fines":
        clause, extra = date_filter("p.paid_on")
        report["headers"] = ["Paid on", "Loan ID", "Member", "Book", "Amount paid (INR)", "Note"]
        collected = 0
        for p in db.execute(
                "SELECT p.*, m.full_name, b.title FROM fine_payments p JOIN loans l ON l.id = p.loan_id "
                "JOIN members m ON m.id = l.member_id JOIN books b ON b.id = l.book_id "
                f"WHERE 1 = 1{clause} ORDER BY p.paid_on DESC, p.id DESC", extra):
            collected += p["amount"]
            report["rows"].append([p["paid_on"], loan_code(p["loan_id"]), p["full_name"], p["title"],
                                   f"{p['amount']:.2f}", p["note"]])
        outstanding = db.execute(
            "SELECT COALESCE(SUM(l.fine_amount - COALESCE((SELECT SUM(amount) FROM fine_payments "
            "WHERE loan_id = l.id), 0)), 0) FROM loans l WHERE l.returned_date IS NOT NULL AND l.fine_amount > 0"
        ).fetchone()[0]
        report["summary"] = [("Fines collected in this period", format_rupees(collected)),
                             ("Fines still unpaid (all time)", format_rupees(outstanding))]
    return report


@app.route("/reports")
@login_required
def reports():
    db = get_db()
    name = request.args.get("report", "inventory")
    if name not in REPORTS:
        name = "inventory"
    report = build_report(db, name, request.args)
    for message in report["errors"]:
        flash(message, "error")
    members_ = db.execute("SELECT id, full_name FROM members ORDER BY full_name COLLATE NOCASE").fetchall()
    limit = app.config["REPORT_PREVIEW_ROWS"]
    return render_template("reports.html", report=report, names=REPORTS, name=name,
                           date_report=name in DATE_REPORTS, members_list=members_,
                           start=clean(request.args.get("start")), end=clean(request.args.get("end")),
                           member_id=to_int(request.args.get("member_id", "")),
                           preview_rows=report["rows"][:limit], preview_limit=limit)


def csv_safe(value):
    """Stop spreadsheet programs from running text such as =CMD(...) typed by a user."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.route("/reports/export")
@login_required
def reports_export():
    db = get_db()
    name = request.args.get("report", "")
    if name not in REPORTS:
        flash("Unknown report.", "error")
        return redirect(url_for("reports"))
    report = build_report(db, name, request.args)
    if report["errors"]:
        for message in report["errors"]:
            flash(message, "error")
        return redirect(url_for("reports", **request.args.to_dict()))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(report["headers"])
    for row in report["rows"]:
        writer.writerow([csv_safe(cell) for cell in row])
    # utf-8-sig adds a "BOM" so Excel shows characters correctly.
    data = buffer.getvalue().encode("utf-8-sig")
    filename = f"libraryhub_{name}_{today_str()}.csv"
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --------------------------------------------------------------------------
# 3J. Settings
# --------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = get_db()
    current = get_settings(db)
    form = {"library_name": current["library_name"], "loan_days": current["loan_days"],
            "fine_per_day": f"{current['fine_per_day']:g}", "max_loans": current["max_loans"],
            "block_overdue": current["block_overdue"]}

    if request.method == "POST":
        form = {"library_name": clean(request.form.get("library_name")),
                "loan_days": clean(request.form.get("loan_days")),
                "fine_per_day": clean(request.form.get("fine_per_day")),
                "max_loans": clean(request.form.get("max_loans")),
                "block_overdue": request.form.get("block_overdue") == "on"}
        errors = {}
        if not form["library_name"] or len(form["library_name"]) > 60:
            errors["library_name"] = "Library name is required (60 characters at most)."
        loan_days = to_int(form["loan_days"])
        if loan_days is None or not (1 <= loan_days <= 365):
            errors["loan_days"] = "Loan duration must be a whole number from 1 to 365."
        fine = to_money(form["fine_per_day"])
        if fine is None or fine > 1000:
            errors["fine_per_day"] = "Fine per day must be a number from 0 to 1000."
        max_loans = to_int(form["max_loans"])
        if max_loans is None or not (1 <= max_loans <= 50):
            errors["max_loans"] = "Maximum loans must be a whole number from 1 to 50."

        if errors:
            flash_form_errors(errors)
            return render_template("settings.html", form=form, errors=errors)
        with transaction(db):
            set_setting(db, "library_name", form["library_name"])
            set_setting(db, "loan_days", loan_days)
            set_setting(db, "fine_per_day", fine)
            set_setting(db, "max_loans", max_loans)
            set_setting(db, "block_overdue", "1" if form["block_overdue"] else "0")
        flash("Settings saved. They apply to all new loans and returns.", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", form=form, errors={})


# --------------------------------------------------------------------------
# 3K. Error pages
# --------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(error):
    return render_template("errors/403.html", message="You do not have permission to do that."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("errors/404.html", message="The page you are looking for does not exist."), 404


@app.errorhandler(CSRFError)
def csrf_failed(error):
    return render_template("errors/403.html",
                           message="Your form expired or is invalid. Go back, refresh the page and try again."), 400


@app.errorhandler(500)
def server_error(error):
    app.logger.exception("Unhandled error: %s", error)
    return render_template("errors/500.html"), 500


if __name__ == "__main__":
    init_db(app.config["DATABASE"])
    _initialised_databases.add(app.config["DATABASE"])
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
