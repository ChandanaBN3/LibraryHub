# LibraryHub — Library Management System

A complete, working Library Management web application built for the **Advanced Python Programming** mini-project.
It runs locally, needs no paid services, and stores everything in a real **SQLite** database.

> **Scope note:** LibraryHub is an *administrator* system. Only library staff log in. Reservations are created and
> managed by the admin on behalf of members — members do not have their own login.

---

## 1. Features

| Area | What you can do |
|---|---|
| **Login** | Admin login/logout, passwords hashed with Werkzeug, every page and every POST action protected, CSRF protection on all forms |
| **Dashboard** | Live statistics (titles, copies, available copies, members, issued, overdue, pending reservations), category chart, most-borrowed books, recent activity, overdue alerts, quick actions |
| **Books** | Add / view / edit / delete, search (title, author, ISBN), filter (category, availability), pagination, duplicate-ISBN check, delete blocked when history exists |
| **Members** | Register / view / edit, activate / deactivate, search & filter, profile with active loans, overdue loans and full history, delete blocked when history exists |
| **Issue books** | Pick member + book + dates, auto loan ID (`LN-00001`), borrowing limit, optional "no borrowing while overdue" rule, inactive members blocked |
| **Returns & fines** | Fine shown *before* you confirm, no fine for on-time returns, paid / partly paid / unpaid, later payments recorded, no double returns, no double fines |
| **Overdue** | Dedicated page, status calculated from today's date, estimated fines, unpaid-fine list, search and filter |
| **Reservations** | Queue per book, cancel, "Mark ready", fulfil on issue, duplicate reservations blocked, clear priority rule |
| **Reports** | Inventory, available/issued, current loans, overdue, member history, most borrowed, fine collection — date filters, **CSV export** that opens in Excel |
| **Categories** | Add / edit / list / delete (blocked while books use it) |
| **Settings** | Library name, loan days, fine per day (default **₹2**), max loans per member, overdue restriction — saved in SQLite and used immediately |

### The reservation priority rule (first come, first served)
1. When a copy is returned, the admin presses **Mark ready** for the **first** person in the queue. That copy is now *held*.
2. A held copy can only be issued to that member. Nobody else (and nobody further back in the queue) can take it.
3. Copies that are free but needed by people waiting in the queue cannot be given to walk-in borrowers.
4. When the held copy is issued to the member, the reservation becomes **Fulfilled**.

### The fine policy
`fine = days overdue × fine-per-day`. The default is **₹2 per day** and the library can change it any time in
**Settings**. Returning on or before the due date costs nothing.

---

## 2. Technology stack
- **Backend:** Python 3 + Flask, Jinja2 templates
- **Database:** SQLite (file `instance/libraryhub.sqlite3`, created automatically)
- **Frontend:** HTML5, CSS3, plain JavaScript, Chart.js (doughnut chart), Font Awesome icons
- **Security:** Werkzeug password hashing, Flask-WTF CSRF, parameterized SQL, escaped templates, Content-Security-Policy
- Chart.js and Font Awesome are **bundled** in `static/vendor/`, so the site works **without internet**.

## 3. Project structure
```
LibraryHub/
├── app.py              # Flask app: all pages (routes), validation, reports
├── database.py         # SQLite schema, connection/transaction helpers, lending + fine rules
├── seed.py             # creates the admin account (+ optional demo data)
├── requirements.txt    # Python packages
├── .env.example        # copy to .env to set SECRET_KEY
├── pytest.ini          # test settings
├── templates/          # HTML pages (Jinja2)
│   ├── base.html            # sidebar + top bar shared by every page
│   ├── _macros.html         # reusable pieces: form fields, pagination, badges
│   ├── login.html  dashboard.html
│   ├── books.html  book_form.html  book_detail.html
│   ├── members.html  member_form.html  member_detail.html
│   ├── loans.html  issue_book.html  return_book.html  overdue.html
│   ├── reservations.html  categories.html  reports.html  settings.html
│   └── errors/  403.html  404.html  500.html
├── static/
│   ├── css/style.css   js/app.js   images/book-placeholder.svg
│   └── vendor/         # bundled Chart.js and Font Awesome
├── tests/test_app.py   # 143 automated tests
└── instance/           # the SQLite database lives here
```

---

## 4. Setup on Windows (VS Code)

**You need:** Python 3.10 or newer from <https://www.python.org/downloads/> (tick **"Add python.exe to PATH"** in the installer).
Check it with `python --version`.

1. Unzip `LibraryHub.zip`, then in VS Code choose **File → Open Folder…** and open the `LibraryHub` folder.
2. Open the terminal: **Terminal → New Terminal**.
3. Run these commands one by one:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python seed.py
python app.py
```

4. Open **<http://127.0.0.1:5000>** in your browser.
5. Log in with the demo account:

| Username | Password |
|---|---|
| `admin` | `Admin@12345` |

This is a **demo** password created by `seed.py`. Change it in **Settings → Change admin password**.

> If VS Code asks *"We noticed a new environment… select it for this workspace?"* click **Yes**.
> Stop the server any time with **Ctrl + C** in the terminal.

### Optional: set your own secret key
```bat
copy .env.example .env
```
Open `.env` and replace the `SECRET_KEY` value with long random text. (Without a `.env`, the app uses a development
key and prints a warning — fine for a college demo, not for real use.)

### What does `python seed.py` do?
| Command | Effect |
|---|---|
| `python seed.py` | Creates the admin account **and** fictional demo data (books, members, loans, a reservation queue) |
| `python seed.py --no-demo` | Creates **only** the admin account — start with an empty library |
| `python seed.py --reset` | **Deletes the database** after you type `YES`, then recreates it |

Running `python seed.py` again is safe: it never overwrites existing data. The demo data is clearly labelled as
**"Demo data"** in the site's banner.

### How to reset the demo data safely
```bat
python seed.py --reset
```
Type `YES` when asked. This is the only command that deletes data. To keep a backup first, copy
`instance\libraryhub.sqlite3` somewhere else.

---

## 5. Run the tests
```bat
python -m pytest
```
The tests use a **temporary** database, so your real data is never touched. They cover login and protected pages,
book/member CRUD and search, issuing, availability, returns, fine calculation, duplicate returns, reservations
(including the priority rule), categories, CSV export, settings, validation and missing records.

---

## 6. Common errors and fixes

| Problem | Fix |
|---|---|
| `'python' is not recognized` | Reinstall Python and tick **Add python.exe to PATH**, or try `py -m venv .venv` and `py app.py` |
| PowerShell: *running scripts is disabled* when activating | Run `Set-ExecutionPolicy -Scope Process Bypass`, then activate again — or use **Command Prompt** instead of PowerShell |
| `ModuleNotFoundError: No module named 'flask'` | The virtual environment is not active. Run `.venv\Scripts\activate` (you should see `(.venv)`) then `pip install -r requirements.txt` |
| `Address already in use` / port 5000 busy | Close the other server (Ctrl + C) or stop the program using port 5000 |
| Login says *No admin account exists yet* | Run `python seed.py` once |
| *Invalid username or password* | Demo login is `admin` / `Admin@12345`. If you changed it and forgot it: `python seed.py --reset` |
| *Your form expired or is invalid* | Refresh the page and submit again (the secret key changed or the page was open too long) |
| `database is locked` | Close other programs that have the `.sqlite3` file open (e.g. DB Browser) |
| Page looks unstyled | Make sure you opened the site at `http://127.0.0.1:5000`, not by double-clicking an HTML file |

---

## 7. How to explain this project in your viva

**One-sentence summary:** "LibraryHub is a Flask web application where a librarian logs in to manage books and members,
issue and return books, calculate overdue fines, handle reservations and export reports — all stored in SQLite."

**Architecture (MVC-style):**
- **Model / data:** `database.py` — tables and business rules. SQLite stores books, members, loans, reservations, fine payments, settings.
- **Controller:** `app.py` — each `@app.route` function receives a request, validates input, talks to the database, and chooses a page.
- **View:** `templates/*.html` — Jinja2 pages that show the data; `static/` holds CSS and JavaScript.

**Good design points you can mention:**
1. **Available copies are calculated, not stored** — `total copies − copies currently issued`. A stored counter could drift out of sync with the loan records; a calculated one cannot.
2. **Transactions** — issuing a book and fulfilling a reservation happen together or not at all (`BEGIN IMMEDIATE … COMMIT/ROLLBACK`).
3. **Database safety nets** — foreign keys are switched on for every connection, and SQLite triggers refuse to over-issue a book even if Python code had a bug.
4. **History is preserved** — books/members with loans can't be deleted; membership is deactivated instead.
5. **Security** — hashed passwords, CSRF tokens on every form, parameterized SQL (no SQL injection), escaped output (no XSS), login required on every route.
6. **Overdue is dynamic** — never stored; it's computed by comparing the due date with today's date, so it is always correct.
7. **Fines** — `days overdue × rate`, rate configurable in Settings; each payment is its own row so partial payments are possible.
8. **Testing** — 143 automated tests with a temporary database.

**Likely questions:**
- *Why SQLite?* Zero setup, a single file, perfect for a local mini-project; the same SQL works on MySQL/PostgreSQL with small changes.
- *What is CSRF?* An attack where another website tricks a logged-in admin's browser into submitting a form; the hidden token in each form prevents it.
- *Why Flask instead of Django?* Flask is small and explicit, so every part of the request → database → page flow is visible and easy to explain.
- *How do you stop two admins issuing the last copy at the same moment?* The issue runs inside a `BEGIN IMMEDIATE` transaction and a database trigger double-checks stock.

---

## 8. Honest limitations
- Single administrator role (no separate librarian/student logins, no self-service member portal).
- Reservations are entered by the admin; there are no email/SMS notifications.
- Designed to run locally with Flask's built-in server — not hardened for public internet hosting.
