"""
seed.py - prepares the LibraryHub database.

    python seed.py              create the admin account + DEMO data (first-time setup)
    python seed.py --no-demo    create ONLY the admin account (start with an empty library)
    python seed.py --reset      delete the database file and start again (asks first)

Running it a second time is safe: it never overwrites existing data.
The demo data is fictional and is clearly marked as "demo data" inside the website.
"""
import argparse
import os
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from app import app
from database import connect, init_db, set_setting

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Admin@12345"      # DEMO password - change it in Settings after logging in!

CATEGORIES = [
    ("Computer Science", "Programming, algorithms and software engineering"),
    ("Fiction", "Novels and short stories"),
    ("Science", "Physics, biology and general science"),
    ("History", "World and regional history"),
    ("Self-Help", "Personal development and productivity"),
    ("Mathematics", "Pure and applied mathematics"),
]

# (title, author, isbn, category, publisher, year, shelf, copies, description)
BOOKS = [
    ("Introduction to Algorithms", "Thomas H. Cormen", "9780262046305", "Computer Science", "MIT Press", 2022, "CS-01", 3, "A comprehensive textbook on algorithms."),
    ("Clean Code", "Robert C. Martin", "9780132350884", "Computer Science", "Prentice Hall", 2008, "CS-02", 2, "A handbook of agile software craftsmanship."),
    ("Fluent Python", "Luciano Ramalho", "9781492056355", "Computer Science", "O'Reilly", 2022, "CS-03", 2, "Clear, concise and effective Python programming."),
    ("Database System Concepts", "Abraham Silberschatz", "9780078022159", "Computer Science", "McGraw-Hill", 2019, "CS-04", 2, "Fundamentals of database systems."),
    ("To Kill a Mockingbird", "Harper Lee", "9780061120084", "Fiction", "Harper", 2006, "FI-01", 2, "A classic novel about justice and childhood."),
    ("The Alchemist", "Paulo Coelho", "9780062315007", "Fiction", "HarperOne", 2014, "FI-02", 3, "A fable about following your dream."),
    ("A Brief History of Time", "Stephen Hawking", "9780553380163", "Science", "Bantam", 1998, "SC-01", 1, "From the Big Bang to black holes."),
    ("The Selfish Gene", "Richard Dawkins", "9780198788607", "Science", "Oxford University Press", 2016, "SC-02", 2, "A gene-centred view of evolution."),
    ("Sapiens", "Yuval Noah Harari", "9780062316097", "History", "Harper", 2015, "HI-01", 2, "A brief history of humankind."),
    ("India After Gandhi", "Ramachandra Guha", "9780330540209", "History", "Picador", 2008, "HI-02", 1, "The history of the world's largest democracy."),
    ("Atomic Habits", "James Clear", "9780735211292", "Self-Help", "Avery", 2018, "SH-01", 3, "Tiny changes, remarkable results."),
    ("Deep Work", "Cal Newport", "9781455586691", "Self-Help", "Grand Central", 2016, "SH-02", 1, "Rules for focused success."),
    ("Discrete Mathematics and Its Applications", "Kenneth H. Rosen", "9781259676512", "Mathematics", "McGraw-Hill", 2018, "MA-01", 2, "Standard text for discrete mathematics."),
    ("Calculus: Early Transcendentals", "James Stewart", "9781285741550", "Mathematics", "Cengage", 2015, "MA-02", 2, "A widely used calculus textbook."),
]

MEMBERS = [
    ("Aarav Sharma", "aarav.sharma@example.com", "9876500001", "12 Lake View Road, Bengaluru"),
    ("Diya Patel", "diya.patel@example.com", "9876500002", "45 Green Park, Ahmedabad"),
    ("Rohan Iyer", "rohan.iyer@example.com", "9876500003", "7 Temple Street, Chennai"),
    ("Ananya Reddy", "ananya.reddy@example.com", "9876500004", "88 Hill Colony, Hyderabad"),
    ("Kabir Singh", "kabir.singh@example.com", "9876500005", "23 Market Lane, Jaipur"),
    ("Meera Nair", "meera.nair@example.com", "9876500006", "5 Beach Road, Kochi"),
    ("Vikram Joshi", "vikram.joshi@example.com", "9876500007", "19 Station Road, Pune"),
    ("Sneha Das", "sneha.das@example.com", "9876500008", "31 Park Street, Kolkata"),
]


def iso(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def add_admin(conn):
    exists = conn.execute("SELECT 1 FROM admins WHERE username = ?", (ADMIN_USERNAME,)).fetchone()
    if exists:
        print(f"- Admin '{ADMIN_USERNAME}' already exists (password not changed).")
        return
    conn.execute("INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                 (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD)))
    print(f"- Admin created: username '{ADMIN_USERNAME}', password '{ADMIN_PASSWORD}'")


def add_demo_data(conn):
    if conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] or conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]:
        print("- Library already has books or members, so demo data was NOT added.")
        return False

    category_id = {}
    for name, description in CATEGORIES:
        category_id[name] = conn.execute("INSERT INTO categories (name, description) VALUES (?, ?)",
                                         (name, description)).lastrowid
    book_id = {}
    for title, author, isbn, cat, publisher, year, shelf, copies, description in BOOKS:
        book_id[title] = conn.execute(
            "INSERT INTO books (title, author, isbn, category_id, publisher, publication_year, shelf, total_copies, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (title, author, isbn, category_id[cat], publisher, year, shelf, copies, description)).lastrowid
    member_id = {}
    for i, (name, email, phone, address) in enumerate(MEMBERS):
        member_id[name] = conn.execute(
            "INSERT INTO members (full_name, email, phone, address, registered_on) VALUES (?, ?, ?, ?, ?)",
            (name, email, phone, address, iso(200 - i * 15))).lastrowid
    conn.execute("UPDATE members SET status = 'inactive' WHERE full_name = 'Sneha Das'")

    rate = 2
    def loan(book, member, issued_ago, due_in, returned_ago=None):
        """due_in = days from TODAY until due (negative = already overdue)."""
        issue, due = iso(issued_ago), (date.today() + timedelta(days=due_in)).isoformat()
        returned, fine = None, 0
        if returned_ago is not None:
            returned = iso(returned_ago)
            late = (date.fromisoformat(returned) - date.fromisoformat(due)).days
            fine = max(late, 0) * rate
        return conn.execute(
            "INSERT INTO loans (book_id, member_id, issue_date, due_date, returned_date, fine_amount) VALUES (?, ?, ?, ?, ?, ?)",
            (book_id[book], member_id[member], issue, due, returned, fine)).lastrowid

    # Past (returned) loans - some on time, some late with fines
    loan("Clean Code", "Aarav Sharma", 60, -46, 50)
    loan("Atomic Habits", "Diya Patel", 55, -41, 45)
    loan("The Alchemist", "Rohan Iyer", 50, -36, 30)           # 6 days late -> fine 12 (unpaid)
    pay_late = loan("Sapiens", "Ananya Reddy", 45, -31, 20)     # 11 days late -> fine 22 (paid)
    part_late = loan("Fluent Python", "Kabir Singh", 40, -26, 15)   # 11 days late -> fine 22 (part paid)
    loan("Introduction to Algorithms", "Meera Nair", 35, -21, 22)
    loan("Atomic Habits", "Vikram Joshi", 30, -16, 18)
    loan("Clean Code", "Diya Patel", 25, -11, 12)
    for loan_id, amount in ((pay_late, 22), (part_late, 10)):
        conn.execute("INSERT INTO fine_payments (loan_id, amount, paid_on, note) VALUES (?, ?, ?, 'Demo payment')",
                     (loan_id, amount, iso(10)))

    # Open loans - on time and overdue
    loan("Introduction to Algorithms", "Aarav Sharma", 5, 9)
    loan("Introduction to Algorithms", "Diya Patel", 4, 10)
    loan("A Brief History of Time", "Rohan Iyer", 20, -6)       # overdue 6 days  (only copy -> all out)
    loan("Deep Work", "Ananya Reddy", 25, -11)                  # overdue 11 days (only copy -> all out)
    loan("Sapiens", "Kabir Singh", 3, 11)
    loan("The Alchemist", "Meera Nair", 2, 12)
    loan("Atomic Habits", "Vikram Joshi", 6, 8)
    loan("Database System Concepts", "Kabir Singh", 8, 6)

    # Reservations: Deep Work has no free copy, so two members are queued for it
    conn.execute("INSERT INTO reservations (book_id, member_id, reserved_on) VALUES (?, ?, ?)",
                 (book_id["Deep Work"], member_id["Aarav Sharma"], iso(3)))
    conn.execute("INSERT INTO reservations (book_id, member_id, reserved_on) VALUES (?, ?, ?)",
                 (book_id["Deep Work"], member_id["Meera Nair"], iso(1)))
    conn.execute("INSERT INTO reservations (book_id, member_id, reserved_on) VALUES (?, ?, ?)",
                 (book_id["A Brief History of Time"], member_id["Vikram Joshi"], iso(2)))
    set_setting(conn, "demo_data", "1")
    print(f"- Demo data added: {len(CATEGORIES)} categories, {len(BOOKS)} books, {len(MEMBERS)} members, loans and reservations.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Prepare the LibraryHub database.")
    parser.add_argument("--no-demo", action="store_true", help="create only the admin account")
    parser.add_argument("--reset", action="store_true", help="delete the database and start again")
    args = parser.parse_args()

    path = app.config["DATABASE"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if args.reset and os.path.exists(path):
        answer = input(f"This DELETES ALL DATA in {path}. Type YES to continue: ")
        if answer.strip() != "YES":
            print("Cancelled. Nothing was changed.")
            return 1
        os.remove(path)
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        print("- Old database deleted.")

    init_db(path)
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        add_admin(conn)
        if not args.no_demo:
            add_demo_data(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(f"\nDone. Database: {path}\nNow run:  python app.py   and open http://127.0.0.1:5000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
