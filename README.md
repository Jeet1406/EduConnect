# EduConnect

**A mentorship platform that connects students with mentors and manages the whole journey in one place: finding a mentor, booking sessions, tracking progress, sharing resources and rating the experience.**

Built with Flask, SQLite and Tailwind CSS.

---

## Table of contents

1. [The problem](#the-problem)
2. [Features](#features)
3. [How a session works](#how-a-session-works)
4. [Tech stack](#tech-stack)
5. [Project structure](#project-structure)
6. [Getting started](#getting-started)
7. [Configuration](#configuration)
8. [Data model](#data-model)
9. [Routes](#routes)
10. [Notifications](#notifications)
11. [Security notes and limitations](#security-notes-and-limitations)
12. [Future improvements](#future-improvements)

---

## The problem

Students who want career or skill guidance usually rely on scattered chats, random tutorials or paid coaching. Mentoring that does happen informally has no structure:

- Bookings happen over chat, so sessions are missed or double-booked.
- Nobody confirms whether a session actually took place.
- Materials and progress are not tracked.
- There is no way to judge a mentor's quality before connecting.

EduConnect replaces this with one structured, trackable and rated mentoring workflow.

---

## Features

### Student
- Register and log in as a student, with a learning goal and preferred mode (Online / Offline / Hybrid).
- **Find a mentor**: search by name, skill, format or description. Results are ordered **best-rated first**.
- **Connect** with a mentor and see their progress bar for you.
- **Book sessions** with topic, date, time, duration, mode (Google Meet / Zoom / In-person) and priority.
- **Reschedule**: propose a new time, accept or decline the mentor's proposal, or cancel.
- **Rate mentors** from 1 to 5 stars with an optional short review (after a completed session).
- **Resources library**: search and filter PDFs, videos, code and links shared by mentors.
- **Calendar** of upcoming sessions, session summary and history with filters.
- **Notifications** for every important event, with a "mark all read" option.
- Profile page.

### Mentor
- Register and log in as a mentor, with expertise, mentoring format and a description.
- **Session requests** queue sorted by priority: confirm, decline (with a reason), propose a new time, or accept a student's proposed time.
- Change a session's **priority** (Urgent / High / Medium / Low) from a dropdown.
- **Mark sessions Completed or Not completed**, with notes on what was covered.
- Schedule sessions directly for a student (these are confirmed immediately).
- **Update student progress** (0-100%) and mentorship status (Active / Completed).
- **Share resources**: upload PDFs (up to 15 MB) or share video, code and general links.
- See their **average rating** and recent student reviews on their profile.
- Notifications, including reminders to declare ended sessions.

### Admin
- Separate admin registration (protected by a setup key) and login.
- Dashboard with stats: total students, total mentors, unmatched students, and sessions awaiting mentor declaration.
- **Students**, **Mentors** and **Sessions** tabs with search and sortable columns.
- **Edit** any student or mentor from a popup form, with validation and duplicate-email checks.
- Mentor ratings shown in the mentors table.
- **Export CSV** for students, mentors (with ratings) and sessions.

### Smart behaviours
- **Priority queue**: urgent sessions rise to the top of the mentor's queue.
- **24-hour response window**: booking requests and reschedule proposals expire automatically if nobody responds.
- **No silent auto-completion**: a session is never marked completed without the mentor's confirmation (see below).
- **Date-ordered history**: the session archive on both student and mentor pages is ordered by date and time, latest first.

---

## How a session works

```
Student requests           Mentor confirms             Session time passes
pending_mentor  ───────►   confirmed  ───────────────► "Not declared by mentor"
      │                        │                              │
      │ no reply in 24h        │ cancel                       ├── mentor: Mark complete ──► completed
      ▼                        ▼                              └── mentor: Not completed ──► not_completed
 not_responded             cancelled
```

| Status | Meaning |
|---|---|
| `pending_mentor` | Student requested; waiting for the mentor (24 h limit) |
| `confirmed` | Both sides agreed on the time |
| `reschedule_by_mentor` / `reschedule_by_student` | A new time was proposed and awaits the other side (24 h limit) |
| `declined` | The mentor could not take the slot |
| `cancelled` | Cancelled by either side |
| `not_responded` | The request or proposal expired without a reply |
| `completed` | The mentor confirmed the session happened |
| `not_completed` | The mentor declared the session did not happen |

**When the session's end time passes and the mentor has not responded:**
- The session **stays `confirmed`** and shows **"Not declared by mentor"**.
- The mentor gets a notification asking them to mark it completed or not completed.
- The student gets a notification that the session is still not declared by the mentor.
- Each reminder is sent once per session, not on every page load.

### Ratings
- A student can rate a mentor only if they are **connected** and have at least **one completed session** with them.
- One rating per student per mentor; rating again updates the existing one.
- Mentor search order: **average rating** (highest first), then **number of ratings**, then **name A-Z**. Unrated mentors appear last.

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask |
| Database | SQLite via Flask-SQLAlchemy |
| Templates | Jinja2 |
| Styling | Tailwind CSS (CDN), Google Fonts (Plus Jakarta Sans, Fraunces) |
| Auth | Flask sessions (signed cookies); admin passwords hashed with Werkzeug |

---

## Project structure

```
EduConnect/
├── app.py                  # Flask app: models, routes, session lifecycle
├── EduConnect.db           # SQLite database (created automatically)
├── Templates/              # Jinja2 templates (note the capital T)
│   ├── Home.html
│   ├── Login.html
│   ├── Register.html
│   ├── Student.html        # Student dashboard (overview, sessions, resources, notifications, profile)
│   ├── Mentor.html         # Mentor dashboard (requests, upcoming, students, history, notifications, profile)
│   ├── AdminLogin.html     # Admin login and registration
│   ├── AdminDashboard.html # Admin dashboard
│   ├── StudentsList.html   # Used by the /students route
│   └── MentorsList.html    # Used by the /mentors route
└── static/
    ├── logo.jpg            # Logo shown in every header
    └── uploads/
        └── resources/      # Uploaded PDFs (created automatically)
```

`app.py` uses `template_folder="Templates"`, so keep that folder name and capitalisation, especially on Linux and macOS.

---

## Getting started

### Requirements
- Python 3.10 or newer
- An internet connection (Tailwind CSS and Google Fonts load from a CDN)

### Setup (Windows PowerShell)

```powershell
# 1. Go to the project folder
cd path\to\EduConnect

# 2. Create and activate a virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install flask flask-sqlalchemy

# 4. Run the app
python app.py
```

On macOS or Linux, activate with `source venv/bin/activate`.

Open **http://127.0.0.1:5000** in your browser.

On first run the app creates `EduConnect.db` and the upload folder. When you upgrade from an older database, `migrate_sqlite_schema()` adds any missing columns automatically, and new tables (such as `Rating`) are created on startup. No manual migration is needed.

### Trying it out

1. Go to **Home** and register a **mentor** and a **student** (Register page).
2. Log in as the student, find the mentor under **Find a mentor** and click **Connect**.
3. In **My Sessions**, book a session. Log in as the mentor and **Confirm** it.
4. After the session time passes, the mentor sees the reminder and marks it **Completed**.
5. The student can now **rate** the mentor from the dashboard.
6. For the admin area, open `/admin/register`, enter the setup key from `app.py`, then log in at `/admin/login`.

---

## Configuration

All settings are at the top of `app.py`.

| Setting | Purpose | Default |
|---|---|---|
| `SECRET_KEY` | Signs session cookies | development value, **change before deploying** |
| `ADMIN_SETUP_KEY` | Key required to create an admin account | development value, **change before deploying** |
| `RESPONSE_TIME_LIMIT_HOURS` | How long a request or proposal can wait before expiring | `24` |
| `MAX_CONTENT_LENGTH` | Maximum upload size | 15 MB |
| `PERMANENT_SESSION_LIFETIME` | How long a login lasts | 7 days |
| `SESSION_COOKIE_SECURE` | Send cookies only over HTTPS | `False` (set `True` when deploying with HTTPS) |
| `ALLOWED_RESOURCE_EXTENSIONS` | Allowed upload types | `pdf` |

---

## Data model

| Table | Key fields | Notes |
|---|---|---|
| **Student** | name, email (unique), goal, learning_mode | |
| **Mentor** | name, email (unique), expertise, mentoring_format, description | |
| **Admin** | name, email (unique), password_hash | |
| **Mentorship** | student_id, mentor_id, progress (0-100), status, started_on | Links one student to one mentor |
| **Session** | mentorship_id, scheduled_at, duration_minutes, mode, topic, status, priority, notes, proposed_at, proposed_by | The booking and its lifecycle |
| **Resource** | mentorship_id, title, resource_type (pdf / video / code / link), url | Shared by a mentor with a student |
| **Rating** | student_id, mentor_id, stars (1-5), review, updated_at | Unique per student and mentor |
| **Notification** | recipient_role, recipient_id, message, is_read, session_id | One feed per user |

```
Student ──< Mentorship >── Mentor
               │
               ├──< Session
               └──< Resource
Student ──< Rating >── Mentor
```

---

## Routes

### Public
| Route | Description |
|---|---|
| `/` , `/Home` | Landing page |
| `/register`, `/choose-role` | Student and mentor registration |
| `/login`, `/logout` | Login and logout |

### Student
| Route | Description |
|---|---|
| `GET /student?tab=overview\|sessions\|resources\|notifications\|profile` | Student dashboard |
| `POST /student/connect/<mentor_id>` | Connect with a mentor |
| `POST /mentor/<mentor_id>/rate` | Rate a mentor (1-5 stars, optional review) |

### Mentor
| Route | Description |
|---|---|
| `GET /mentor?tab=overview\|notifications\|profile` | Mentor dashboard |
| `POST /mentor/update-progress/<mentorship_id>` | Update a student's progress and status |
| `POST /resource/add/<mentorship_id>` | Share a resource |

### Sessions (student and mentor, depending on the action)
| Route | Who | Description |
|---|---|---|
| `POST /session/book/<mentorship_id>` | Both | Book a session (student: needs confirmation; mentor: confirmed) |
| `POST /session/<id>/confirm` | Mentor | Confirm a request |
| `POST /session/<id>/decline` | Mentor | Decline, with an optional reason |
| `POST /session/<id>/propose` | Both | Propose a new time |
| `POST /session/<id>/accept-proposal` | Both | Accept the other side's proposed time |
| `POST /session/<id>/reject-proposal` | Both | Keep the original time |
| `POST /session/<id>/cancel` | Both | Cancel a live session |
| `POST /session/<id>/priority` | Mentor | Change priority |
| `POST /session/<id>/complete` | Mentor | Mark completed (with notes) |
| `POST /session/<id>/not-complete` | Mentor | Mark not completed |

### Notifications
| Route | Description |
|---|---|
| `POST /notifications/mark-read` | Mark all notifications as read |

### Admin
| Route | Description |
|---|---|
| `/admin/register`, `/admin/login` | Admin sign-up (setup key required) and login |
| `GET /admin?tab=students\|mentors\|sessions` | Admin dashboard |
| `POST /admin/students/<id>/edit` | Edit a student |
| `POST /admin/mentors/<id>/edit` | Edit a mentor |
| `GET /admin/export/students.csv`, `mentors.csv`, `sessions.csv` | CSV exports |
| `/students`, `/mentors` | Admin-only list pages (rendered with `StudentsList.html` and `MentorsList.html`) |

---

## Notifications

Users get in-app notifications for:

- A new booking request, confirmation, decline or cancellation.
- A proposed, accepted or declined reschedule.
- An expired request or proposal.
- A session that ended and still needs the mentor's declaration (to the mentor), and the matching notice that it is not declared (to the student).
- A session marked completed or not completed.
- A new resource, a progress update, a new connection, and a new or updated rating.

The bell icon shows the unread count, and each user sees their latest eight notifications.

---

## Security notes and limitations

This project is built for learning and demonstration. Before using it for real users, address these:

- **Student and mentor login has no password.** It checks the full name and email together. Add password hashing, as the admin accounts already have.
- **Secrets are in the source.** Move `SECRET_KEY` and `ADMIN_SETUP_KEY` into environment variables, and use a long random key for each.
- **No CSRF protection** on forms. Add Flask-WTF or CSRF tokens.
- **`debug=True`** is enabled in `app.py`. Turn it off in production and serve with a production server such as Gunicorn or Waitress.
- **SQLite** is fine for a single server; use PostgreSQL or MySQL for heavier use.
- **Tailwind via CDN** is meant for development. Build the CSS for production.
- **Notifications are in-app only**; there are no emails or push messages.
- Expired requests are processed when someone opens a dashboard or performs a session action, not by a background job.
- The `/students` and `/mentors` admin routes need `StudentsList.html` and `MentorsList.html`. Make sure those templates exist in `Templates/`, or remove the routes.
- Mentor-only controls (priority, completion) are enforced on the server as well as hidden in the UI.

---

## Future improvements

- Passwords or OAuth login for students and mentors.
- Email or push notifications and automatic reminders before sessions.
- Real video-call link generation (Google Meet or Zoom).
- Mentor availability slots, so students pick from free times.
- Weighted ratings that favour mentors with more reviews.
- Admin actions to deactivate users, or to add them directly.
- Analytics: sessions per month, mentor load, completion rate.
- Chat between student and mentor.
- Automated tests and a deployment guide.

---

## Author

Built by **Jeet** as a full-stack Flask project.
