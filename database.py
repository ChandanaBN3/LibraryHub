"""
database.py - everything that talks to SQLite for LibraryHub.

What is in this file
    1. The database schema (all CREATE TABLE statements).
    2. connect / get_db / transaction helpers.
    3. init_db(): creates the database safely on first run (never erases data).
    4. Small "business rule" helpers that are shared by the website and tests:
       stock counting, lending rules, fine calculation.

Important design idea
    We do NOT store "available copies" in the books table.  Instead it is
    calculated from the loans table (total copies - books currently issued).
    That way the number can never get out of sync with the real loan records.
"""
import math
import sqlite3
from contextlib import contextmanager
from datetime import date

from flask import current_app, g

# --------------------------------------------------------------------------
# 1. Schema
# --------------------------------------------------------------------------
# "IF NOT EXISTS" means running this script again never deletes anything.
SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    author           TEXT NOT NULL,
    isbn             TEXT NOT NULL UNIQUE,
    category_id      INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    publisher        TEXT NOT NULL DEFAULT '',
    publication_year INTEGER,
    description      TEXT NOT NULL DEFAULT '',
    shelf            TEXT NOT NULL DEFAULT '',
    total_copies     INTEGER NOT NULL DEFAULT 1 CHECK (total_copies >= 1),
    cover_url        TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone         TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',
    registered_on TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive'))
);

-- One row per book lent out.  returned_date stays NULL while the book is out.
CREATE TABLE IF NOT EXISTS loans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id       INTEGER NOT NULL REFERENCES books(id)   ON DELETE RESTRICT,
    member_id     INTEGER NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    issue_date    TEXT NOT NULL,
    due_date      TEXT NOT NULL,
    returned_date TEXT,
    fine_amount   REAL NOT NULL DEFAULT 0 CHECK (fine_amount >= 0),
    CHECK (due_date >= issue_date),
    CHECK (returned_date IS NULL OR returned_date >= issue_date)
);

-- Each payment of a fine is its own row, so partial payments are possible.
CREATE TABLE IF NOT EXISTS fine_payments (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL REFERENCES loans(id) ON DELETE RESTRICT,
    amount  REAL NOT NULL CHECK (amount > 0),
    paid_on TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id)   ON DELETE RESTRICT,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    reserved_on TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'ready', 'fulfilled', 'cancelled')),
    ready_on    TEXT,
    closed_on   TEXT,
    loan_id     INTEGER REFERENCES loans(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indexes make searching and the dashboard queries fast.
CREATE INDEX IF NOT EXISTS idx_books_title       ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_category    ON books(category_id);
CREATE INDEX IF NOT EXISTS idx_loans_book        ON loans(book_id);
CREATE INDEX IF NOT EXISTS idx_loans_member      ON loans(member_id);
CREATE INDEX IF NOT EXISTS idx_loans_open        ON loans(returned_date, due_date);
CREATE INDEX IF NOT EXISTS idx_fines_loan        ON fine_payments(loan_id);
CREATE INDEX IF NOT EXISTS idx_reservations_book ON reservations(book_id, status);

-- A member can have only ONE open (pending/ready) reservation per book.
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_reservation
    ON reservations(book_id, member_id) WHERE status IN ('pending', 'ready');

-- Safety nets enforced by the database itself (in addition to the Python checks):
-- 1) You can never lend more copies than the library owns.
CREATE TRIGGER IF NOT EXISTS trg_loans_no_overissue
BEFORE INSERT ON loans
WHEN NEW.returned_date IS NULL
 AND (SELECT COUNT(*) FROM loans WHERE book_id = NEW.book_id AND returned_date IS NULL)
     >= (SELECT total_copies FROM books WHERE id = NEW.book_id)
BEGIN
    SELECT RAISE(ABORT, 'No copies available for this book');
END;

-- 2) total_copies can never drop below the number of copies currently lent out.
CREATE TRIGGER IF NOT EXISTS trg_books_copies_not_below_issued
BEFORE UPDATE OF total_copies ON books
WHEN NEW.total_copies < (SELECT COUNT(*) FROM loans
                         WHERE book_id = NEW.id AND returned_date IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'Total copies cannot be lower than copies currently issued');
END;
"""

# Settings are stored as text in the "settings" table.  These are the defaults.
DEFAULT_SETTINGS = {
    "library_name": "LibraryHub",
    "loan_days": "14",          # how many days a book can be borrowed
    "fine_per_day": "2",        # fine in rupees for every overdue day
    "max_loans": "3",           # maximum books a member can hold at once
    "block_overdue": "1",       # 1 = members with overdue books cannot borrow more
    "demo_data": "0",           # 1 = the database contains demo data from seed.py
}

# Bump this number and add a step in migrate() when the schema changes later.
SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# 2. Connections and transactions
# --------------------------------------------------------------------------
def connect(db_path):
    """Open a SQLite connection with foreign keys switched ON (every time!)."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    # isolation_level=None -> we control transactions ourselves (see transaction()).
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """Return the connection for the current web request (opened once per request)."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db):
    """
    Run several SQL statements as ONE unit: either all succeed or none do.

        with transaction(db):
            db.execute(...)
            db.execute(...)

    BEGIN IMMEDIATE locks the database for writing straight away, so two people
    clicking "Issue" at the same moment cannot both take the last copy.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


# --------------------------------------------------------------------------
# 3. Safe initialisation
# --------------------------------------------------------------------------
def init_db(db_path):
    """Create tables if they do not exist yet.  Existing data is never touched."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        migrate(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    finally:
        conn.close()


def migrate(conn):
    """Upgrade an older database to the newest schema version (keeps all data)."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # Example for the future:
    # if version < 2:
    #     conn.execute("ALTER TABLE books ADD COLUMN language TEXT NOT NULL DEFAULT ''")
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# --------------------------------------------------------------------------
# 4. Settings
# --------------------------------------------------------------------------
def get_settings(db):
    """Return settings as a dictionary with proper Python types."""
    raw = dict(DEFAULT_SETTINGS)
    for row in db.execute("SELECT key, value FROM settings"):
        raw[row["key"]] = row["value"]
    return {
        "library_name": raw["library_name"],
        "loan_days": int(raw["loan_days"]),
        "fine_per_day": float(raw["fine_per_day"]),
        "max_loans": int(raw["max_loans"]),
        "block_overdue": raw["block_overdue"] == "1",
        "demo_data": raw["demo_data"] == "1",
    }


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------
# 5. Reusable SQL
# --------------------------------------------------------------------------
# A book together with live stock numbers:
#   issued_copies    - copies currently lent out
#   held_copies      - copies set aside for a reservation marked "ready"
#   queue_length     - reservations still "pending" (waiting in line)
#   available_copies - total - issued           (physically on the shelf)
#   free_copies      - available - held         (can actually be lent right now)
BOOK_SELECT = """
SELECT s.*,
       s.total_copies - s.issued_copies                  AS available_copies,
       s.total_copies - s.issued_copies - s.held_copies  AS free_copies
FROM (
    SELECT b.*, c.name AS category_name,
           (SELECT COUNT(*) FROM loans l
             WHERE l.book_id = b.id AND l.returned_date IS NULL)  AS issued_copies,
           (SELECT COUNT(*) FROM reservations r
             WHERE r.book_id = b.id AND r.status = 'ready')       AS held_copies,
           (SELECT COUNT(*) FROM reservations r
             WHERE r.book_id = b.id AND r.status = 'pending')     AS queue_length
    FROM books b LEFT JOIN categories c ON c.id = b.category_id
) s
"""

# A member together with loan counts.  Needs the named parameter :today
MEMBER_SELECT = """
SELECT m.*,
       (SELECT COUNT(*) FROM loans l
         WHERE l.member_id = m.id AND l.returned_date IS NULL) AS active_loans,
       (SELECT COUNT(*) FROM loans l
         WHERE l.member_id = m.id AND l.returned_date IS NULL
           AND l.due_date < :today)                            AS overdue_loans
FROM members m
"""

# A loan together with book/member names and fine information.
# Needs the named parameters :today and :rate (fine per day).
#   status         - 'returned', 'overdue' or 'active' (calculated from today's date)
#   days_overdue   - for open loans: days past the due date (0 if on time)
#   fine_due       - fine that applies: stored fine if returned, estimate if still out
#   fine_paid      - total of all payments recorded for this loan
LOAN_SELECT = """
SELECT t.*,
       CASE WHEN t.returned_date IS NOT NULL THEN t.fine_amount
            ELSE ROUND(t.days_overdue * :rate, 2) END AS fine_due
FROM (
    SELECT l.*, b.title AS book_title, b.isbn AS book_isbn, m.full_name AS member_name,
           CASE WHEN l.returned_date IS NOT NULL THEN 'returned'
                WHEN l.due_date < :today THEN 'overdue'
                ELSE 'active' END AS status,
           CASE WHEN l.returned_date IS NULL AND l.due_date < :today
                THEN CAST(julianday(:today) - julianday(l.due_date) AS INTEGER)
                ELSE 0 END AS days_overdue,
           COALESCE((SELECT SUM(p.amount) FROM fine_payments p WHERE p.loan_id = l.id), 0) AS fine_paid
    FROM loans l
    JOIN books b   ON b.id = l.book_id
    JOIN members m ON m.id = l.member_id
) t
"""


def like_pattern(text):
    """Turn user text into a safe LIKE pattern (so % and _ typed by the user are literal)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def get_book(db, book_id):
    return db.execute(f"SELECT * FROM ({BOOK_SELECT}) WHERE id = ?", (book_id,)).fetchone()


def get_member(db, member_id, today):
    return db.execute(f"SELECT * FROM ({MEMBER_SELECT}) WHERE id = :id",
                      {"id": member_id, "today": today}).fetchone()


def get_loan(db, loan_id, today, rate):
    return db.execute(f"{LOAN_SELECT} WHERE t.id = :id",
                      {"id": loan_id, "today": today, "rate": rate}).fetchone()


# --------------------------------------------------------------------------
# 6. Business rules
# --------------------------------------------------------------------------
def calculate_fine(due_date, return_date, fine_per_day):
    """
    Return (days_overdue, fine_amount).
    Returned on or before the due date -> 0 days and no fine.
    Dates are ISO strings like '2026-10-28'.
    """
    days = (date.fromisoformat(return_date) - date.fromisoformat(due_date)).days
    days = max(days, 0)
    return days, round(days * fine_per_day, 2)


def fine_status(fine_amount, paid_amount):
    """'none' (no fine), 'unpaid', 'partial' or 'paid'."""
    if fine_amount <= 0:
        return "none"
    if paid_amount + 0.005 >= fine_amount:
        return "paid"
    if paid_amount > 0:
        return "partial"
    return "unpaid"


def reservation_position(db, reservation):
    """Place in the queue for a pending reservation (1 = first in line)."""
    ahead = db.execute(
        "SELECT COUNT(*) FROM reservations "
        "WHERE book_id = ? AND status = 'pending' AND id < ?",
        (reservation["book_id"], reservation["id"]),
    ).fetchone()[0]
    return ahead + 1


def lending_problem(db, member, book, settings, today):
    """
    Decide whether `member` may borrow `book` right now.

    Returns (error_message, reservation).
      error_message - text to show the admin, or None when lending is allowed
      reservation   - the member's open reservation for this book (to be marked
                      "fulfilled" when the loan is created), or None

    RESERVATION PRIORITY RULE (first come, first served):
      * A copy set aside for a "ready" reservation can only go to that member.
      * Free copies go to the reservation queue in order.  A pending member can
        borrow only if their position in the queue is not larger than the
        number of free copies.
      * A member with no reservation can borrow only copies that are free AND
        not needed by people waiting in the queue.
    """
    if member is None:
        return "Please choose a registered member.", None
    if book is None:
        return "Please choose a book.", None
    if member["status"] != "active":
        return f"{member['full_name']}'s membership is inactive. Activate it before issuing books.", None
    if member["active_loans"] >= settings["max_loans"]:
        return (f"{member['full_name']} already has {member['active_loans']} book(s) issued. "
                f"The limit is {settings['max_loans']}."), None
    if settings["block_overdue"] and member["overdue_loans"] > 0:
        return (f"{member['full_name']} has {member['overdue_loans']} overdue book(s). "
                "Return them before borrowing more."), None

    already = db.execute(
        "SELECT 1 FROM loans WHERE book_id = ? AND member_id = ? AND returned_date IS NULL",
        (book["id"], member["id"])).fetchone()
    if already:
        return f"{member['full_name']} already has a copy of this book.", None

    reservation = db.execute(
        "SELECT * FROM reservations WHERE book_id = ? AND member_id = ? "
        "AND status IN ('pending', 'ready')", (book["id"], member["id"])).fetchone()

    if reservation is not None and reservation["status"] == "ready":
        return None, reservation            # a copy is already set aside for them

    if reservation is not None:             # pending: must be near the front of the queue
        position = reservation_position(db, reservation)
        if book["free_copies"] >= position:
            return None, reservation
        if book["available_copies"] <= 0:
            return "No copies are available. The reservation stays in the queue.", None
        return (f"This member is number {position} in the reservation queue and only "
                f"{book['free_copies']} copy(ies) are free."), None

    if book["available_copies"] <= 0:
        return "No copies of this book are available. You can create a reservation instead.", None
    if book["free_copies"] <= 0:
        return "The remaining copies are set aside for members with a 'ready' reservation.", None
    if book["free_copies"] <= book["queue_length"]:
        return "The free copies are needed by members waiting in the reservation queue.", None
    return None, None


def total_pages(total, per_page):
    return max(1, math.ceil(total / per_page))
