"""
Automated tests for LibraryHub.   Run them with:   python -m pytest -v

Every test gets its OWN temporary database, so your real data is never touched.
The tests are grouped by feature (login, books, members, lending, fines, ...).
"""
import html as html_lib
import os
import re
import sys
from datetime import date, timedelta

import pytest
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module                                   # noqa: E402
from database import calculate_fine, connect, fine_status, init_db  # noqa: E402

PASSWORD = "Test@12345"


def iso(offset_days=0):
    """Today +/- some days as 'YYYY-MM-DD' (negative = in the past)."""
    return (date.today() + timedelta(days=offset_days)).isoformat()


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------
class RecordingClient(FlaskClient):
    """A test browser that remembers the last response (so tests can read flash messages from it)."""
    last_response = None

    def open(self, *args, **kwargs):
        self.last_response = super().open(*args, **kwargs)
        return self.last_response


@pytest.fixture
def flask_app(tmp_path):
    flask_app = app_module.app
    flask_app.test_client_class = RecordingClient
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                            DATABASE=str(tmp_path / "test.sqlite3"))
    init_db(flask_app.config["DATABASE"])
    conn = connect(flask_app.config["DATABASE"])
    conn.execute("INSERT INTO admins (username, password_hash) VALUES (?, ?)",
                 ("tester", generate_password_hash(PASSWORD)))
    conn.close()
    return flask_app


@pytest.fixture
def anon(flask_app):
    """A browser that is NOT logged in."""
    return flask_app.test_client()


@pytest.fixture
def client(flask_app):
    """A browser that IS logged in as the admin."""
    browser = flask_app.test_client()
    response = browser.post("/login", data={"username": "tester", "password": PASSWORD})
    assert response.status_code == 302
    return browser


@pytest.fixture
def db(flask_app):
    """Direct database access for arranging test data and checking results."""
    conn = connect(flask_app.config["DATABASE"])
    yield conn
    conn.close()


def make_category(db, name="Science"):
    return db.execute("INSERT INTO categories (name) VALUES (?)", (name,)).lastrowid


def make_book(db, title="Test Book", copies=1, isbn=None, category_id=None, author="Some Author"):
    isbn = isbn or f"97800000{db.execute('SELECT COUNT(*) FROM books').fetchone()[0] + 1:05d}"
    return db.execute(
        "INSERT INTO books (title, author, isbn, total_copies, category_id) VALUES (?, ?, ?, ?, ?)",
        (title, author, isbn, copies, category_id)).lastrowid


def make_member(db, name="Test Member", status="active", email=None):
    email = email or f"{name.lower().replace(' ', '.')}@example.com"
    return db.execute(
        "INSERT INTO members (full_name, email, registered_on, status) VALUES (?, ?, ?, ?)",
        (name, email, iso(-100), status)).lastrowid


def make_loan(db, book_id, member_id, issued=-5, due=9, returned=None, fine=0):
    """Create a loan directly. issued/due/returned are day offsets from today."""
    return db.execute(
        "INSERT INTO loans (book_id, member_id, issue_date, due_date, returned_date, fine_amount) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (book_id, member_id, iso(issued), iso(due), iso(returned) if returned is not None else None, fine)).lastrowid


def stock(db, book_id):
    from database import get_book
    return get_book(db, book_id)


def flashed(client):
    """
    Flash messages the admin would have seen:
    those still waiting in the session (after a redirect) plus those already
    drawn on the last page (when a form is shown again with an error).
    """
    with client.session_transaction() as session:
        messages = [message for _, message in session.get("_flashes", [])]
    if client.last_response is not None and client.last_response.mimetype == "text/html":
        page = client.last_response.get_data(as_text=True)
        messages += [html_lib.unescape(m) for m in re.findall(r'<div class="msg">(.*?)</div>', page, re.S)]
    return messages


def issue(client, member_id, book_id, issue_date=None, due_date=None):
    return client.post("/loans/issue", data={
        "member_id": member_id, "book_id": book_id,
        "issue_date": issue_date or iso(0), "due_date": due_date or iso(14)})


# --------------------------------------------------------------------------
# Login, logout, protected pages, security basics
# --------------------------------------------------------------------------
PROTECTED_GET_PAGES = ["/", "/books", "/books/new", "/members", "/members/new", "/loans", "/loans/issue",
                       "/overdue", "/reservations", "/categories", "/reports", "/settings"]


@pytest.mark.parametrize("url", PROTECTED_GET_PAGES)
def test_pages_redirect_to_login_when_logged_out(anon, url):
    response = anon.get(url)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_post_routes_are_protected_too(anon, db):
    book_id = make_book(db)
    assert anon.post(f"/books/{book_id}/delete").status_code == 302
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1       # book still there
    assert anon.get("/reports/export?report=inventory").status_code == 302


def test_login_page_is_public(anon):
    assert anon.get("/login").status_code == 200


def test_login_with_correct_password(anon):
    response = anon.post("/login", data={"username": "tester", "password": PASSWORD})
    assert response.status_code == 302
    assert anon.get("/").status_code == 200


def test_login_rejects_wrong_password_and_unknown_user(anon):
    for username, password in (("tester", "wrong"), ("nobody", PASSWORD), ("", "")):
        response = anon.post("/login", data={"username": username, "password": password})
        assert response.status_code == 200
        assert b"Invalid username or password" in response.data
    assert anon.get("/").status_code == 302


def test_passwords_are_hashed_not_plain_text(db):
    stored = db.execute("SELECT password_hash FROM admins WHERE username = 'tester'").fetchone()[0]
    assert stored != PASSWORD and PASSWORD not in stored
    assert stored.startswith(("scrypt:", "pbkdf2:"))


def test_logout_ends_the_session(client):
    assert client.get("/").status_code == 200
    assert client.get("/logout").status_code == 405           # logging out needs POST
    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_login_does_not_redirect_to_other_websites(anon):
    response = anon.post("/login?next=https://evil.example.com", data={"username": "tester", "password": PASSWORD})
    assert response.headers["Location"].endswith("/")
    anon2 = app_module.app.test_client()
    response = anon2.post("/login?next=//evil.example.com", data={"username": "tester", "password": PASSWORD})
    assert "evil" not in response.headers["Location"]


def test_csrf_token_is_required_when_enabled(flask_app, client):
    flask_app.config["WTF_CSRF_ENABLED"] = True
    response = client.post("/categories/new", data={"name": "Sneaky"})
    assert response.status_code == 400
    assert b"expired or is invalid" in response.data


def test_forms_contain_csrf_token_when_enabled(flask_app, client):
    flask_app.config["WTF_CSRF_ENABLED"] = True
    assert b'name="csrf_token"' in client.get("/books/new").data


def test_user_text_is_escaped_in_pages(client, db):
    make_book(db, title="<script>alert('x')</script>")
    html = client.get("/books").data.decode()
    assert "<script>alert('x')</script>" not in html
    assert "&lt;script&gt;" in html


def test_sql_injection_text_is_harmless(client, db):
    make_book(db, title="Normal")
    response = client.get("/books", query_string={"q": "x' OR '1'='1"})
    assert response.status_code == 200 and b"No books found" in response.data
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1


def test_security_headers_present(client):
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_change_password(client, anon):
    client.post("/account/password", data={"current_password": "bad", "new_password": "NewPass#123", "confirm_password": "NewPass#123"})
    assert "not correct" in flashed(client)[-1]
    client.post("/account/password", data={"current_password": PASSWORD, "new_password": "short", "confirm_password": "short"})
    assert "at least 8" in flashed(client)[-1]
    client.post("/account/password", data={"current_password": PASSWORD, "new_password": "NewPass#123", "confirm_password": "NewPass#123"})
    assert anon.post("/login", data={"username": "tester", "password": "NewPass#123"}).status_code == 302


def test_unknown_url_gives_404_page(client, anon):
    assert client.get("/no-such-page").status_code == 404
    assert anon.get("/no-such-page").status_code == 404      # also works for logged-out visitors


# --------------------------------------------------------------------------
# Books
# --------------------------------------------------------------------------
def book_form(**overrides):
    data = {"title": "Python Basics", "author": "A. Writer", "isbn": "978-0-13-235088-4", "category_id": "",
            "publisher": "Pub", "publication_year": "2020", "description": "Intro", "shelf": "A-1",
            "total_copies": "3", "cover_url": ""}
    data.update(overrides)
    return data


def test_add_book(client, db):
    response = client.post("/books/new", data=book_form())
    assert response.status_code == 302
    row = db.execute("SELECT * FROM books").fetchone()
    assert row["title"] == "Python Basics" and row["isbn"] == "9780132350884"       # hyphens removed
    assert row["total_copies"] == 3
    assert stock(db, row["id"])["available_copies"] == 3                              # calculated, not stored


@pytest.mark.parametrize("changes, expected", [
    ({"title": ""}, b"Title is required"),
    ({"author": ""}, b"Author is required"),
    ({"isbn": "12345"}, b"ISBN must be 10 or 13 digits"),
    ({"isbn": ""}, b"ISBN is required"),
    ({"total_copies": "0"}, b"Total copies must be a whole number"),
    ({"total_copies": "abc"}, b"Total copies must be a whole number"),
    ({"total_copies": "-2"}, b"Total copies must be a whole number"),
    ({"publication_year": "20x0"}, b"Enter a year between"),
    ({"publication_year": "3000"}, b"Enter a year between"),
    ({"cover_url": "javascript:alert(1)"}, b"full http"),
    ({"category_id": "999"}, b"valid category"),
])
def test_add_book_validation(client, db, changes, expected):
    response = client.post("/books/new", data=book_form(**changes))
    assert response.status_code == 200 and expected in response.data
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def test_duplicate_isbn_is_rejected(client, db):
    client.post("/books/new", data=book_form())
    response = client.post("/books/new", data=book_form(title="Another", isbn="9780132350884"))
    assert b"A book with this ISBN already exists" in response.data
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1


def test_edit_book(client, db):
    book_id = make_book(db, title="Old Title", copies=2)
    page = client.get(f"/books/{book_id}/edit")
    assert page.status_code == 200 and b"Old Title" in page.data
    response = client.post(f"/books/{book_id}/edit", data=book_form(title="New Title", total_copies="5"))
    assert response.status_code == 302
    assert db.execute("SELECT title, total_copies FROM books WHERE id = ?", (book_id,)).fetchone()[:] == ("New Title", 5)


def test_edit_cannot_use_another_books_isbn_but_can_keep_its_own(client, db):
    first = make_book(db, isbn="9780132350884")
    second = make_book(db, isbn="9780262046305")
    assert client.post(f"/books/{first}/edit", data=book_form(isbn="9780132350884")).status_code == 302
    assert b"already exists" in client.post(f"/books/{second}/edit", data=book_form(isbn="9780132350884")).data


def test_cannot_reduce_copies_below_issued(client, db):
    book_id, member_id = make_book(db, copies=3), make_member(db)
    make_loan(db, book_id, member_id)
    make_loan(db, book_id, make_member(db, "Second Person"))
    response = client.post(f"/books/{book_id}/edit", data=book_form(total_copies="1"))
    assert b"Cannot be lower than 2" in response.data
    assert db.execute("SELECT total_copies FROM books WHERE id = ?", (book_id,)).fetchone()[0] == 3


def test_database_itself_blocks_negative_availability(db):
    """Even if the Python checks were bypassed, SQLite refuses to over-issue or shrink below issued."""
    import sqlite3
    book_id, m1, m2 = make_book(db, copies=1), make_member(db), make_member(db, "Other One")
    make_loan(db, book_id, m1)
    with pytest.raises(sqlite3.IntegrityError):
        make_loan(db, book_id, m2)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE books SET total_copies = 0 WHERE id = ?", (book_id,))
    assert stock(db, book_id)["available_copies"] == 0


def test_foreign_keys_are_enforced(db):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO loans (book_id, member_id, issue_date, due_date) VALUES (999, 999, '2026-01-01', '2026-01-15')")


def test_view_book_and_missing_book(client, db):
    book_id = make_book(db, title="Viewable")
    assert b"Viewable" in client.get(f"/books/{book_id}").data
    assert client.get("/books/9999").status_code == 404
    assert client.get("/books/9999/edit").status_code == 404
    assert client.post("/books/9999/delete").status_code == 404


def test_search_books_by_title_author_and_isbn(client, db):
    make_book(db, title="Learning Flask", author="Miguel", isbn="9781111111111")
    make_book(db, title="Cooking Basics", author="Julia Child", isbn="9782222222222")
    assert b"Learning Flask" in client.get("/books?q=flask").data
    assert b"Cooking Basics" not in client.get("/books?q=flask").data
    assert b"Cooking Basics" in client.get("/books?q=julia").data
    assert b"Cooking Basics" in client.get("/books?q=978-2222").data
    assert b"No books found" in client.get("/books?q=zzzz").data


def test_search_treats_percent_literally(client, db):
    make_book(db, title="100% Pure")
    make_book(db, title="Other")
    html = client.get("/books?q=%25").data
    assert b"100% Pure" in html and b"Other" not in html


def test_filter_books_by_category_and_availability(client, db):
    sci, fic = make_category(db, "Science"), make_category(db, "Fiction")
    in_stock = make_book(db, title="Shelf Book", category_id=sci)
    out = make_book(db, title="Borrowed Book", category_id=fic)
    make_loan(db, out, make_member(db))
    assert b"Shelf Book" in client.get(f"/books?category={sci}").data
    assert b"Borrowed Book" not in client.get(f"/books?category={sci}").data
    assert b"Borrowed Book" in client.get("/books?availability=unavailable").data
    assert b"Shelf Book" not in client.get("/books?availability=unavailable").data
    assert b"Shelf Book" in client.get("/books?availability=available").data
    assert in_stock


def test_books_are_paginated(client, db):
    for i in range(25):
        make_book(db, title=f"Book {i:02d}")
    page1, page3 = client.get("/books").data, client.get("/books?page=3").data
    assert b"Showing 1" in page1 and b"of 25 books" in page1
    assert b"Book 24" in page3 and b"Book 00" not in page3
    assert client.get("/books?page=999").status_code == 200            # out of range -> last page


def test_delete_book_without_history(client, db):
    book_id = make_book(db)
    assert client.post(f"/books/{book_id}/delete").status_code == 302
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0


def test_delete_book_with_loan_history_is_blocked(client, db):
    book_id = make_book(db)
    make_loan(db, book_id, make_member(db), issued=-30, due=-16, returned=-20)
    client.post(f"/books/{book_id}/delete")
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
    assert "cannot be deleted" in flashed(client)[-1]


def test_delete_book_with_reservation_history_is_blocked(client, db):
    book_id, member_id = make_book(db), make_member(db)
    db.execute("INSERT INTO reservations (book_id, member_id, reserved_on, status) VALUES (?, ?, ?, 'cancelled')",
               (book_id, member_id, iso(-3)))
    client.post(f"/books/{book_id}/delete")
    assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------
def member_form(**overrides):
    data = {"full_name": "Priya Menon", "email": "priya@example.com", "phone": "+91 98765 43210",
            "address": "1 Test Street", "registered_on": iso(0)}
    data.update(overrides)
    return data


def test_register_member(client, db):
    assert client.post("/members/new", data=member_form()).status_code == 302
    row = db.execute("SELECT * FROM members").fetchone()
    assert row["full_name"] == "Priya Menon" and row["status"] == "active"


@pytest.mark.parametrize("changes, expected", [
    ({"full_name": "A"}, b"at least 2 characters"),
    ({"email": ""}, b"Email is required"),
    ({"email": "not-an-email"}, b"valid email"),
    ({"phone": "abc"}, b"Phone may contain"),
    ({"registered_on": "2999-01-01"}, b"cannot be in the future"),
    ({"registered_on": "31-02-2020"}, b"valid date"),
])
def test_register_member_validation(client, db, changes, expected):
    response = client.post("/members/new", data=member_form(**changes))
    assert response.status_code == 200 and expected in response.data
    assert db.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0


def test_duplicate_member_email_rejected_case_insensitively(client, db):
    client.post("/members/new", data=member_form())
    response = client.post("/members/new", data=member_form(full_name="Other", email="PRIYA@example.com"))
    assert b"already uses this email" in response.data
    assert db.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 1


def test_edit_member_and_registration_date_is_fixed(client, db):
    member_id = make_member(db, "Old Name")
    response = client.post(f"/members/{member_id}/edit", data=member_form(full_name="New Name", status="inactive", registered_on="2000-01-01"))
    assert response.status_code == 302
    row = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    assert row["full_name"] == "New Name" and row["status"] == "inactive" and row["registered_on"] == iso(-100)


def test_toggle_membership(client, db):
    member_id = make_member(db)
    client.post(f"/members/{member_id}/toggle")
    assert db.execute("SELECT status FROM members WHERE id = ?", (member_id,)).fetchone()[0] == "inactive"
    client.post(f"/members/{member_id}/toggle")
    assert db.execute("SELECT status FROM members WHERE id = ?", (member_id,)).fetchone()[0] == "active"


def test_member_profile_shows_loans_and_history(client, db):
    member_id, book_id = make_member(db, "Profile Person"), make_book(db, title="Active Book")
    old_book = make_book(db, title="Old Book")
    make_loan(db, book_id, member_id, issued=-20, due=-6)                          # overdue
    make_loan(db, old_book, member_id, issued=-60, due=-46, returned=-40, fine=12)
    html = client.get(f"/members/{member_id}").data.decode()
    assert "Profile Person" in html and "Active Book" in html and "Old Book" in html
    assert "Overdue" in html and "Unpaid" in html


def test_search_and_filter_members(client, db):
    make_member(db, "Alice Active")
    make_member(db, "Ian Inactive", status="inactive")
    assert b"Alice Active" in client.get("/members?q=alice").data
    assert b"Ian Inactive" not in client.get("/members?q=alice").data
    assert b"Ian Inactive" in client.get("/members?status=inactive").data
    assert b"Alice Active" not in client.get("/members?status=inactive").data
    assert b"Alice Active" in client.get("/members?q=ALICE.ACTIVE@example").data


def test_delete_member_without_history_and_with_history(client, db):
    free, busy = make_member(db, "Free Person"), make_member(db, "Busy Person")
    make_loan(db, make_book(db), busy, issued=-30, due=-16, returned=-20)
    assert client.post(f"/members/{free}/delete").status_code == 302
    client.post(f"/members/{busy}/delete")
    names = [r[0] for r in db.execute("SELECT full_name FROM members")]
    assert names == ["Busy Person"]
    assert "cannot be deleted" in flashed(client)[-1]


def test_missing_member_gives_404(client):
    for url in ("/members/999", "/members/999/edit"):
        assert client.get(url).status_code == 404
    assert client.post("/members/999/toggle").status_code == 404
    assert client.post("/members/999/delete").status_code == 404


# --------------------------------------------------------------------------
# Issuing books
# --------------------------------------------------------------------------
def test_issue_book_reduces_availability_and_creates_loan(client, db):
    book_id, member_id = make_book(db, copies=2), make_member(db)
    response = issue(client, member_id, book_id)
    assert response.status_code == 302
    loan = db.execute("SELECT * FROM loans").fetchone()
    assert loan["id"] == 1 and loan["returned_date"] is None
    assert loan["issue_date"] == iso(0) and loan["due_date"] == iso(14)
    assert stock(db, book_id)["available_copies"] == 1
    assert "LN-00001" in flashed(client)[-1]


def test_cannot_issue_when_no_copies_left(client, db):
    book_id, first, second = make_book(db, copies=1), make_member(db), make_member(db, "Second Person")
    issue(client, first, book_id)
    issue(client, second, book_id)
    assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 1
    assert "No copies" in flashed(client)[-1]
    assert stock(db, book_id)["available_copies"] == 0           # never negative


def test_cannot_issue_to_inactive_member(client, db):
    book_id, member_id = make_book(db), make_member(db, status="inactive")
    issue(client, member_id, book_id)
    assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0
    assert "inactive" in flashed(client)[-1]


def test_borrowing_limit_from_settings(client, db):
    member_id = make_member(db)
    client.post("/settings", data={"library_name": "Lib", "loan_days": "14", "fine_per_day": "2", "max_loans": "2", "block_overdue": "on"})
    books = [make_book(db, title=f"B{i}") for i in range(3)]
    issue(client, member_id, books[0]); issue(client, member_id, books[1])
    issue(client, member_id, books[2])
    assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 2
    assert "limit is 2" in flashed(client)[-1]


def test_overdue_members_blocked_only_when_setting_is_on(client, db):
    member_id, old_book, new_book = make_member(db), make_book(db, title="Old"), make_book(db, title="New")
    make_loan(db, old_book, member_id, issued=-30, due=-10)
    issue(client, member_id, new_book)
    assert "overdue" in flashed(client)[-1] and db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 1
    client.post("/settings", data={"library_name": "Lib", "loan_days": "14", "fine_per_day": "2", "max_loans": "3"})   # box unticked
    issue(client, member_id, new_book)
    assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 2


def test_same_member_cannot_borrow_two_copies_of_same_book(client, db):
    book_id, member_id = make_book(db, copies=3), make_member(db)
    issue(client, member_id, book_id)
    issue(client, member_id, book_id)
    assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 1
    assert "already has a copy" in flashed(client)[-1]


@pytest.mark.parametrize("issue_date, due_date, message", [
    ("", "", "valid issue and due dates"),
    ("garbage", iso(5), "valid issue and due dates"),
    (iso(1), iso(10), "cannot be in the future"),
    (iso(0), iso(-1), "before the issue date"),
    (iso(0), iso(400), "within 365 days"),
])
def test_issue_date_validation(client, db, issue_date, due_date, message):
    book_id, member_id = make_book(db), make_member(db)
    client.post("/loans/issue", data={"member_id": member_id, "book_id": book_id, "issue_date": issue_date, "due_date": due_date})
    assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0
    assert message in flashed(client)[-1]


def test_issue_requires_valid_member_and_book(client, db):
    book_id, member_id = make_book(db), make_member(db)
    for member, book in (("", book_id), (member_id, ""), (999, book_id), (member_id, 999), ("abc", "xyz")):
        issue(client, member, book)
        assert db.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0


def test_default_due_date_uses_loan_days_setting(client):
    client.post("/settings", data={"library_name": "Lib", "loan_days": "21", "fine_per_day": "2", "max_loans": "3"})
    assert iso(21).encode() in client.get("/loans/issue").data


# --------------------------------------------------------------------------
# Returns and fines
# --------------------------------------------------------------------------
@pytest.mark.parametrize("due, returned, expected_days, expected_fine", [
    ("2026-10-10", "2026-10-10", 0, 0),          # returned on the due date -> no fine
    ("2026-10-10", "2026-10-05", 0, 0),          # early -> no fine, never negative
    ("2026-10-10", "2026-10-11", 1, 2),
    ("2026-10-10", "2026-10-20", 10, 20),
    ("2026-02-27", "2026-03-02", 3, 6),          # across a month end
])
def test_calculate_fine(due, returned, expected_days, expected_fine):
    assert calculate_fine(due, returned, 2) == (expected_days, expected_fine)


def test_calculate_fine_with_decimal_rate():
    assert calculate_fine("2026-01-01", "2026-01-04", 1.5) == (3, 4.5)


def test_fine_status_labels():
    assert fine_status(0, 0) == "none"
    assert fine_status(20, 0) == "unpaid"
    assert fine_status(20, 5) == "partial"
    assert fine_status(20, 20) == "paid"


def test_return_on_time_restores_availability_without_fine(client, db):
    book_id, member_id = make_book(db), make_member(db)
    loan_id = make_loan(db, book_id, member_id, issued=-5, due=9)
    assert stock(db, book_id)["available_copies"] == 0
    response = client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": "0"})
    assert response.status_code == 302
    loan = db.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    assert loan["returned_date"] == iso(0) and loan["fine_amount"] == 0
    assert stock(db, book_id)["available_copies"] == 1
    assert db.execute("SELECT COUNT(*) FROM fine_payments").fetchone()[0] == 0


def test_overdue_return_charges_fine_using_setting(client, db):
    client.post("/settings", data={"library_name": "Lib", "loan_days": "14", "fine_per_day": "3", "max_loans": "3"})
    book_id, member_id = make_book(db), make_member(db)
    loan_id = make_loan(db, book_id, member_id, issued=-20, due=-5)               # 5 days late
    page = client.get(f"/loans/{loan_id}/return").data.decode()
    assert "₹15.00" in page and "5 day(s) late" in page                             # fine shown BEFORE confirming
    client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": "15"})
    assert db.execute("SELECT fine_amount FROM loans WHERE id = ?", (loan_id,)).fetchone()[0] == 15


@pytest.mark.parametrize("paid, expected", [("20", "paid"), ("5", "partial"), ("0", "unpaid")])
def test_fine_payment_status_recorded_at_return(client, db, paid, expected):
    book_id, member_id = make_book(db), make_member(db)
    loan_id = make_loan(db, book_id, member_id, issued=-20, due=-10)               # 10 days -> Rs 20
    client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": paid})
    total_paid = db.execute("SELECT COALESCE(SUM(amount), 0) FROM fine_payments").fetchone()[0]
    assert fine_status(20, total_paid) == expected


def test_cannot_collect_more_than_the_fine(client, db):
    loan_id = make_loan(db, make_book(db), make_member(db), issued=-20, due=-10)
    client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": "50"})
    assert "cannot be more than the fine" in flashed(client)[-1]
    assert db.execute("SELECT returned_date FROM loans WHERE id = ?", (loan_id,)).fetchone()[0] is None


def test_duplicate_return_is_prevented(client, db):
    book_id, member_id = make_book(db), make_member(db)
    loan_id = make_loan(db, book_id, member_id, issued=-20, due=-10)
    client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": "0"})
    client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": "20"})   # second attempt
    assert "already been returned" in flashed(client)[-1]
    assert db.execute("SELECT COUNT(*) FROM fine_payments").fetchone()[0] == 0                   # no double charge
    assert db.execute("SELECT fine_amount FROM loans WHERE id = ?", (loan_id,)).fetchone()[0] == 20
    assert stock(db, book_id)["available_copies"] == 1                                            # not 2


def test_return_date_validation(client, db):
    loan_id = make_loan(db, make_book(db), make_member(db), issued=-10, due=4)
    for bad, message in (("garbage", "valid return date"), (iso(1), "cannot be in the future"), (iso(-11), "before the issue date")):
        client.post(f"/loans/{loan_id}/return", data={"return_date": bad, "amount_paid": "0"})
        assert message in flashed(client)[-1]
    assert db.execute("SELECT returned_date FROM loans WHERE id = ?", (loan_id,)).fetchone()[0] is None
    client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": "abc"})
    assert "number" in flashed(client)[-1]


def test_return_page_for_missing_or_returned_loan(client, db):
    assert client.get("/loans/999/return").status_code == 404
    loan_id = make_loan(db, make_book(db), make_member(db), issued=-10, due=4, returned=-1)
    assert client.get(f"/loans/{loan_id}/return").status_code == 302


def test_admin_can_record_part_then_full_fine_payment(client, db):
    loan_id = make_loan(db, make_book(db), make_member(db), issued=-30, due=-20, returned=-10, fine=20)
    client.post(f"/loans/{loan_id}/fine", data={"amount": "5"})
    assert "partly paid" not in flashed(client)[-1] and "₹5.00" in flashed(client)[-1]
    assert b"Partly paid" in client.get("/loans?status=unpaid_fines").data
    client.post(f"/loans/{loan_id}/fine", data={"action": "pay_all"})
    assert db.execute("SELECT SUM(amount) FROM fine_payments").fetchone()[0] == 20
    assert b"No loans found" in client.get("/loans?status=unpaid_fines").data
    client.post(f"/loans/{loan_id}/fine", data={"action": "pay_all"})                 # nothing left to pay
    assert "already fully paid" in flashed(client)[-1]
    assert db.execute("SELECT SUM(amount) FROM fine_payments").fetchone()[0] == 20


def test_fine_payment_validation(client, db):
    loan_id = make_loan(db, make_book(db), make_member(db), issued=-30, due=-20, returned=-10, fine=20)
    for bad in ("0", "-5", "abc", "21"):
        client.post(f"/loans/{loan_id}/fine", data={"amount": bad})
        assert db.execute("SELECT COUNT(*) FROM fine_payments").fetchone()[0] == 0
    no_fine = make_loan(db, make_book(db, title="Other"), make_member(db, "Other One"), issued=-30, due=-20, returned=-21)
    client.post(f"/loans/{no_fine}/fine", data={"amount": "5"})
    assert "no fine" in flashed(client)[-1].lower()
    assert client.post("/loans/999/fine", data={"amount": "5"}).status_code == 404


def test_loans_list_filters_and_search(client, db):
    book, member = make_book(db, title="Out Book"), make_member(db, "Lender Person")
    make_loan(db, book, member, issued=-20, due=-5)
    done = make_loan(db, make_book(db, title="Done Book"), make_member(db, "Other Person"), issued=-9, due=5, returned=-2)
    assert b"Out Book" in client.get("/loans?status=overdue").data and b"Done Book" not in client.get("/loans?status=overdue").data
    assert b"Done Book" in client.get("/loans?status=returned").data
    assert b"Lender Person" in client.get("/loans?q=lender").data and b"Done Book" not in client.get("/loans?q=lender").data
    assert b"Done Book" in client.get(f"/loans?q={done}").data


# --------------------------------------------------------------------------
# Overdue page
# --------------------------------------------------------------------------
def test_overdue_page_calculates_status_and_fine_dynamically(client, db):
    member, book = make_member(db, "Late Reader"), make_book(db, title="Late Book")
    make_loan(db, book, member, issued=-20, due=-7)
    make_loan(db, make_book(db, title="Fine Book"), make_member(db, "Ok Reader"), issued=-3, due=11)
    html = client.get("/overdue").data.decode()
    assert "Late Reader" in html and "Late Book" in html and "7d" in html and "₹14.00" in html
    assert "Ok Reader" not in html
    assert "Late Book" in client.get("/overdue?q=late").data.decode()
    assert "No overdue books" in client.get("/overdue?min_days=30").data.decode()
    assert b"No overdue books" in client.get("/overdue?q=zzz").data


def test_overdue_page_lists_unpaid_fines(client, db):
    make_loan(db, make_book(db, title="Returned Late"), make_member(db), issued=-30, due=-20, returned=-10, fine=20)
    html = client.get("/overdue").data.decode()
    assert "Unpaid fines on returned books" in html and "Returned Late" in html


def test_overdue_status_changes_with_the_calendar(client, db):
    """A loan due yesterday is overdue; one due today is not."""
    member = make_member(db)
    make_loan(db, make_book(db, title="Due Yesterday"), member, issued=-14, due=-1)
    due_today = make_loan(db, make_book(db, title="Due Today"), make_member(db, "Second Person"), issued=-14, due=0)
    html = client.get("/overdue").data.decode()
    assert "Due Yesterday" in html and "Due Today" not in html
    assert due_today


# --------------------------------------------------------------------------
# Reservations
# --------------------------------------------------------------------------
def reserve(client, member_id, book_id):
    return client.post("/reservations/new", data={"member_id": member_id, "book_id": book_id})


def test_reservation_rejected_while_copies_are_free(client, db):
    book_id, member_id = make_book(db, copies=2), make_member(db)
    reserve(client, member_id, book_id)
    assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0
    assert "available now" in flashed(client)[-1]


def test_create_and_cancel_reservation(client, db):
    book_id, holder, waiting = make_book(db), make_member(db, "Holder One"), make_member(db, "Waiting Two")
    make_loan(db, book_id, holder)
    reserve(client, waiting, book_id)
    row = db.execute("SELECT * FROM reservations").fetchone()
    assert row["status"] == "pending" and row["reserved_on"] == iso(0)
    assert b"Waiting Two" in client.get("/reservations").data
    client.post(f"/reservations/{row['id']}/cancel")
    row = db.execute("SELECT * FROM reservations").fetchone()
    assert row["status"] == "cancelled" and row["closed_on"] == iso(0)           # history is kept
    client.post(f"/reservations/{row['id']}/cancel")                              # cancelling twice is refused
    assert "Only open reservations" in flashed(client)[-1]


def test_duplicate_active_reservation_is_prevented(client, db):
    book_id, holder, waiting = make_book(db), make_member(db, "Holder One"), make_member(db, "Waiting Two")
    make_loan(db, book_id, holder)
    reserve(client, waiting, book_id)
    reserve(client, waiting, book_id)
    assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
    assert "already has an active reservation" in flashed(client)[-1]


def test_member_may_reserve_again_after_cancelling(client, db):
    book_id, holder, waiting = make_book(db), make_member(db, "Holder One"), make_member(db, "Waiting Two")
    make_loan(db, book_id, holder)
    reserve(client, waiting, book_id)
    client.post(f"/reservations/{db.execute('SELECT id FROM reservations').fetchone()[0]}/cancel")
    reserve(client, waiting, book_id)
    assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 2


def test_cannot_reserve_a_book_you_already_hold_or_as_inactive_member(client, db):
    book_id, holder = make_book(db), make_member(db, "Holder One")
    make_loan(db, book_id, holder)
    reserve(client, holder, book_id)
    assert "already has a copy" in flashed(client)[-1]
    inactive = make_member(db, "Inactive Person", status="inactive")
    reserve(client, inactive, book_id)
    assert "inactive" in flashed(client)[-1]
    assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


def test_reservation_priority_first_come_first_served(client, db):
    """Return a copy -> first in queue is marked ready -> nobody else can borrow it -> they collect it."""
    book_id = make_book(db, title="Popular Book")
    holder, first, second, outsider = (make_member(db, n) for n in ("Holder One", "First Person", "Second Person", "Outsider Person"))
    loan_id = make_loan(db, book_id, holder, issued=-5, due=9)
    reserve(client, first, book_id)
    reserve(client, second, book_id)
    first_res, second_res = [r[0] for r in db.execute("SELECT id FROM reservations ORDER BY id")]

    client.post(f"/reservations/{first_res}/ready")                              # no free copy yet
    assert "No copy is free" in flashed(client)[-1]

    client.post(f"/loans/{loan_id}/return", data={"return_date": iso(0), "amount_paid": "0"})
    assert stock(db, book_id)["available_copies"] == 1 and stock(db, book_id)["free_copies"] == 1

    issue(client, outsider, book_id)                                             # walk-in tries to take the copy
    assert "needed by members waiting" in flashed(client)[-1]
    issue(client, second, book_id)                                               # second in line cannot jump the queue
    assert "number 2 in the reservation queue" in flashed(client)[-1]
    assert db.execute("SELECT COUNT(*) FROM loans WHERE returned_date IS NULL").fetchone()[0] == 0

    client.post(f"/reservations/{second_res}/ready")                             # only the first may be marked ready
    assert "number 2 in the queue" in flashed(client)[-1]
    client.post(f"/reservations/{first_res}/ready")
    assert db.execute("SELECT status FROM reservations WHERE id = ?", (first_res,)).fetchone()[0] == "ready"
    assert stock(db, book_id)["free_copies"] == 0 and stock(db, book_id)["available_copies"] == 1

    issue(client, outsider, book_id)                                             # copy is now held
    assert "set aside" in flashed(client)[-1]
    issue(client, first, book_id)                                                # the right member collects it
    row = db.execute("SELECT * FROM reservations WHERE id = ?", (first_res,)).fetchone()
    assert row["status"] == "fulfilled" and row["loan_id"] is not None
    assert db.execute("SELECT COUNT(*) FROM loans WHERE returned_date IS NULL").fetchone()[0] == 1


def test_reservation_queue_positions_are_shown(client, db):
    book_id = make_book(db)
    make_loan(db, book_id, make_member(db, "Holder One"))
    for name in ("Queue One", "Queue Two"):
        reserve(client, make_member(db, name), book_id)
    html = client.get("/reservations").data.decode()
    assert "#1" in html and "#2" in html and html.index("Queue One") < html.index("Queue Two")


def test_reservation_missing_records(client, db):
    assert client.post("/reservations/999/cancel").status_code == 404
    assert client.post("/reservations/999/ready").status_code == 302
    assert "does not exist" in flashed(client)[-1]
    reserve(client, 999, 999)
    assert "choose a registered member" in flashed(client)[-1]


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
def test_add_edit_list_categories(client, db):
    client.post("/categories/new", data={"name": "Poetry", "description": "Verses"})
    cat_id = db.execute("SELECT id FROM categories").fetchone()[0]
    assert b"Poetry" in client.get("/categories").data
    client.post(f"/categories/{cat_id}/edit", data={"name": "Modern Poetry", "description": "Updated"})
    assert db.execute("SELECT name FROM categories").fetchone()[0] == "Modern Poetry"


def test_category_validation(client, db):
    client.post("/categories/new", data={"name": "", "description": ""})
    assert "required" in flashed(client)[-1]
    client.post("/categories/new", data={"name": "Art"})
    client.post("/categories/new", data={"name": "art"})                             # duplicate ignoring case
    assert "already exists" in flashed(client)[-1]
    assert db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 1
    assert client.post("/categories/999/edit", data={"name": "X"}).status_code == 404


def test_category_with_books_cannot_be_deleted(client, db):
    used, unused = make_category(db, "Used"), make_category(db, "Unused")
    make_book(db, category_id=used)
    client.post(f"/categories/{used}/delete")
    assert "still has 1 book" in flashed(client)[-1]
    client.post(f"/categories/{unused}/delete")
    assert [r[0] for r in db.execute("SELECT name FROM categories")] == ["Used"]
    assert client.post("/categories/999/delete").status_code == 404


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def test_settings_are_saved_in_database_and_shown(client, db):
    response = client.post("/settings", data={"library_name": "City Library", "loan_days": "10", "fine_per_day": "5", "max_loans": "4", "block_overdue": "on"})
    assert response.status_code == 302
    values = dict(db.execute("SELECT key, value FROM settings").fetchall())
    assert values["library_name"] == "City Library" and values["loan_days"] == "10"
    assert values["fine_per_day"] == "5.0" and values["max_loans"] == "4" and values["block_overdue"] == "1"
    assert b"City Library" in client.get("/").data
    assert b'value="10"' in client.get("/settings").data                           # still there after reload


@pytest.mark.parametrize("field, value", [("library_name", ""), ("loan_days", "0"), ("loan_days", "400"), ("loan_days", "x"),
                                          ("fine_per_day", "-1"), ("fine_per_day", "abc"), ("max_loans", "0"), ("max_loans", "99")])
def test_invalid_settings_are_rejected_and_not_saved(client, db, field, value):
    data = {"library_name": "Lib", "loan_days": "14", "fine_per_day": "2", "max_loans": "3"}
    data[field] = value
    response = client.post("/settings", data=data)
    assert response.status_code == 200 and b"error-text" in response.data
    assert db.execute("SELECT value FROM settings WHERE key = 'loan_days'").fetchone()[0] == "14"


def test_default_settings_exist_on_first_run(client, db):
    values = dict(db.execute("SELECT key, value FROM settings").fetchall())
    assert values["fine_per_day"] == "2" and values["loan_days"] == "14" and values["max_loans"] == "3"


# --------------------------------------------------------------------------
# Dashboard (numbers must come from the database)
# --------------------------------------------------------------------------
def test_dashboard_statistics_match_database(client, db):
    cat = make_category(db, "Chart Cat")
    b1, b2 = make_book(db, "One", copies=3, category_id=cat), make_book(db, "Two", copies=2)
    m1, m2 = make_member(db, "Alpha Person"), make_member(db, "Beta Person")
    make_loan(db, b1, m1, issued=-20, due=-5)                       # overdue
    make_loan(db, b2, m2, issued=-2, due=12)
    make_loan(db, b1, m2, issued=-40, due=-26, returned=-30)         # history only
    b3 = make_book(db, "Single", copies=1)
    make_loan(db, b3, make_member(db, "Gamma Person"))
    db.execute("INSERT INTO reservations (book_id, member_id, reserved_on) VALUES (?, ?, ?)", (b3, m1, iso(-1)))
    html = client.get("/").data.decode()
    import re
    def stat(label):
        return int(re.search(r'class="value">(\d+)</div><div class="label">' + re.escape(label), html).group(1))
    assert stat("Book titles") == 3
    assert stat("Physical copies") == 6
    assert stat("Available copies") == 3            # 6 copies - 3 open loans
    assert stat("Registered members") == 3
    assert stat("Currently issued") == 3
    assert stat("Overdue loans") == 1
    assert stat("Pending reservations") == 1
    assert "Overdue alerts" in html and "Chart Cat" in html and "Recent activity" in html


def test_dashboard_with_empty_database(client):
    html = client.get("/").data.decode()
    assert "No books yet" in html and "Nothing has been borrowed yet" in html and "No overdue books" in html


# --------------------------------------------------------------------------
# Reports and CSV export
# --------------------------------------------------------------------------
REPORT_NAMES = ["inventory", "availability", "current_loans", "overdue", "most_borrowed", "fines"]


@pytest.fixture
def busy_library(db):
    cat = make_category(db)
    b1, b2 = make_book(db, "Report Book", copies=2, category_id=cat), make_book(db, "Second Book")
    m1, m2 = make_member(db, "Report Member"), make_member(db, "Other Member")
    make_loan(db, b1, m1, issued=-20, due=-5)
    done = make_loan(db, b2, m1, issued=-40, due=-26, returned=-20, fine=12)
    db.execute("INSERT INTO fine_payments (loan_id, amount, paid_on) VALUES (?, 7, ?)", (done, iso(-3)))
    return {"member": m1, "books": (b1, b2)}


@pytest.mark.parametrize("name", REPORT_NAMES)
def test_every_report_page_and_csv_export_works(client, busy_library, name):
    assert client.get(f"/reports?report={name}").status_code == 200
    response = client.get(f"/reports/export?report={name}")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"] and ".csv" in response.headers["Content-Disposition"]
    assert response.data.startswith(b"\xef\xbb\xbf")                         # BOM so Excel reads it correctly
    assert len(response.data.decode("utf-8-sig").strip().splitlines()) >= 2   # header + at least one row


def test_member_history_report(client, busy_library):
    member_id = busy_library["member"]
    html = client.get(f"/reports?report=member_history&member_id={member_id}").data.decode()
    assert "Report Member" in html and "Report Book" in html
    csv_text = client.get(f"/reports/export?report=member_history&member_id={member_id}").data.decode("utf-8-sig")
    assert "Loan ID,Book" in csv_text and "Second Book" in csv_text
    assert "Choose a member" in client.get("/reports?report=member_history").data.decode()
    assert client.get("/reports/export?report=member_history", follow_redirects=False).status_code == 302
    assert "does not exist" in client.get("/reports?report=member_history&member_id=999").data.decode()


def test_report_date_range_filter(client, busy_library):
    html = client.get(f"/reports?report=fines&start={iso(-10)}&end={iso(0)}").data.decode()
    assert "Second Book" in html
    html = client.get(f"/reports?report=fines&start={iso(-2)}&end={iso(0)}").data.decode()
    assert "Nothing to report" in html
    html = client.get(f"/reports?report=current_loans&start={iso(-3)}").data.decode()
    assert "Nothing to report" in html
    assert "Report Book" in client.get(f"/reports?report=current_loans&start={iso(-30)}&end={iso(0)}").data.decode()


@pytest.mark.parametrize("query, message", [("start=2026-13-45", "not a valid date"), ("end=banana", "not a valid date"),
                                            (f"start={iso(0)}&end={iso(-5)}", "must not be after")])
def test_report_date_validation(client, busy_library, query, message):
    assert message in client.get(f"/reports?report=fines&{query}").data.decode()
    response = client.get(f"/reports/export?report=fines&{query}")
    assert response.status_code == 302                                         # no broken file is downloaded


def test_empty_reports_are_handled(client):
    for name in REPORT_NAMES:
        response = client.get(f"/reports?report={name}")
        assert response.status_code == 200 and b"Nothing to report" in response.data
        csv_text = client.get(f"/reports/export?report={name}").data.decode("utf-8-sig")
        assert len(csv_text.strip().splitlines()) == 1                           # header row only


def test_unknown_report_name(client):
    assert client.get("/reports?report=hack").status_code == 200                # falls back to inventory
    assert client.get("/reports/export?report=hack").status_code == 302


def test_csv_export_neutralises_spreadsheet_formulas(client, db):
    make_book(db, title='=HYPERLINK("http://evil.example","click")', author="+cmd|calc")
    text = client.get("/reports/export?report=inventory").data.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "'+cmd" in text


def test_csv_quotes_commas_and_non_ascii(client, db):
    make_book(db, title="Hello, World – “Quotes”", author="Zoë Müller")
    import csv, io
    rows = list(csv.reader(io.StringIO(client.get("/reports/export?report=inventory").data.decode("utf-8-sig"))))
    assert rows[1][0] == "Hello, World – “Quotes”" and rows[1][1] == "Zoë Müller"


def test_inventory_report_numbers(client, db):
    book_id, member_id = make_book(db, "Counted", copies=4), make_member(db)
    make_loan(db, book_id, member_id)
    make_loan(db, book_id, make_member(db, "Other One"))
    rows = client.get("/reports/export?report=availability").data.decode("utf-8-sig").splitlines()
    assert rows[1].startswith("Counted,") and rows[1].split(",")[2:] == ["4", "2", "0", "2", "2"]


# --------------------------------------------------------------------------
# Whole-site smoke test with the demo data from seed.py
# --------------------------------------------------------------------------
def test_every_page_opens_with_demo_data(client, flask_app):
    import seed
    conn = connect(flask_app.config["DATABASE"])
    seed.add_demo_data(conn)
    conn.close()
    urls = list(PROTECTED_GET_PAGES) + ["/books/1", "/books/1/edit", "/members/1", "/members/1/edit", "/overdue?min_days=7",
                                        "/loans?status=unpaid_fines", "/loans/12/return", "/reservations?status=all"]
    urls += [f"/reports?report={name}" for name in REPORT_NAMES] + ["/reports?report=member_history&member_id=1"]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, f"{url} returned {response.status_code}"
    assert b"Demo data" in client.get("/").data
    assert client.get("/").data.count(b'class="stat"') >= 7


def test_seed_is_safe_to_run_twice(flask_app):
    import seed
    conn = connect(flask_app.config["DATABASE"])
    assert seed.add_demo_data(conn) is True
    before = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0], conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0]
    assert seed.add_demo_data(conn) is False
    after = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0], conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0]
    conn.close()
    assert before == after


def test_init_db_does_not_erase_existing_data(flask_app):
    conn = connect(flask_app.config["DATABASE"])
    make_book(conn, "Keep Me")
    conn.close()
    init_db(flask_app.config["DATABASE"])
    init_db(flask_app.config["DATABASE"])
    conn = connect(flask_app.config["DATABASE"])
    assert conn.execute("SELECT title FROM books").fetchone()[0] == "Keep Me"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()
