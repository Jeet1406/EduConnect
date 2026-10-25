import csv
import io
from datetime import date, datetime, timedelta
from calendar import Calendar
from pathlib import Path
from uuid import uuid4

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "EduConnect.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads" / "resources"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_RESOURCE_EXTENSIONS = {"pdf"}

app = Flask(__name__, template_folder="Templates")

# ===========================================================================
# Session & Cookie Configuration (Prevents logout on refresh)
# ===========================================================================
app.config["SECRET_KEY"] = "educonnect-dev-secret-key-change-later"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_NAME"] = "educonnect_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False  # Set to True only when deploying with HTTPS

# Database configuration
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DATABASE_PATH.as_posix()}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # 15 MB cap on any single upload

ADMIN_SETUP_KEY = "Change this to a secure key for admin account creation" # Change this to a secure key for admin account creation

# Time limit in hours to accept/respond to a session booking or reschedule proposal
RESPONSE_TIME_LIMIT_HOURS = 24

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Session status lifecycle & Priority Queue Rankings
# ---------------------------------------------------------------------------
STATUS_PENDING_MENTOR = "pending_mentor"
STATUS_CONFIRMED = "confirmed"
STATUS_DECLINED = "declined"
STATUS_RESCHEDULE_BY_MENTOR = "reschedule_by_mentor"
STATUS_RESCHEDULE_BY_STUDENT = "reschedule_by_student"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_NOT_RESPONDED = "not_responded"  # Auto-expired booking/reschedule request
STATUS_NOT_COMPLETED = "not_completed"  # Mentor marked the session as not completed

# Still on active calendar/queue (not completed, not cancelled, not expired)
LIVE_STATUSES = {
    STATUS_PENDING_MENTOR,
    STATUS_CONFIRMED,
    STATUS_RESCHEDULE_BY_MENTOR,
    STATUS_RESCHEDULE_BY_STUDENT,
}

# Awaiting user response
AWAITING_STATUSES = {
    STATUS_PENDING_MENTOR,
    STATUS_RESCHEDULE_BY_MENTOR,
    STATUS_RESCHEDULE_BY_STUDENT,
}

STATUS_LABELS = {
    STATUS_PENDING_MENTOR: "Awaiting mentor confirmation",
    STATUS_CONFIRMED: "Confirmed",
    STATUS_DECLINED: "Declined by mentor",
    STATUS_RESCHEDULE_BY_MENTOR: "Mentor proposed a new time",
    STATUS_RESCHEDULE_BY_STUDENT: "Student proposed a new time",
    STATUS_COMPLETED: "Completed",
    STATUS_CANCELLED: "Cancelled",
    STATUS_NOT_RESPONDED: "Not responded (Expired)",
    STATUS_NOT_COMPLETED: "Not completed",
}

# Priority Queue weights: Urgent is 1 (highest priority), Low is 4 (lowest priority)
PRIORITY_ORDER = {
    "urgent": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}


class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True)
    goal = db.Column(db.String(250), nullable=False)
    learning_mode = db.Column(db.String(30), nullable=False)


class Mentor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True)
    expertise = db.Column(db.String(250), nullable=False)
    mentoring_format = db.Column(db.String(30), nullable=False)
    description = db.Column(db.Text, nullable=True)  # Detailed bio & specialities


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)


class Mentorship(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    mentor_id = db.Column(db.Integer, db.ForeignKey("mentor.id"), nullable=False)
    progress = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="active")
    started_on = db.Column(db.DateTime, server_default=db.func.now())

    student = db.relationship("Student", backref="mentorships")
    mentor = db.relationship("Mentor", backref="mentorships")


class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mentorship_id = db.Column(db.Integer, db.ForeignKey("mentorship.id"), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    mode = db.Column(db.String(50), nullable=False, default="Google Meet")
    topic = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(30), nullable=False, default=STATUS_PENDING_MENTOR)
    priority = db.Column(db.String(20), nullable=False, default="medium")  # low, medium, high, urgent
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    proposed_at = db.Column(db.DateTime, nullable=True)
    proposed_by = db.Column(db.String(10), nullable=True)  # "student" or "mentor"

    mentorship = db.relationship("Mentorship", backref="sessions")

    @property
    def priority_rank(self):
        """Priority queue rank: lower number = higher priority."""
        return PRIORITY_ORDER.get(self.priority, 3)

    @property
    def is_awaiting_completion(self):
        """Confirmed session whose end time has passed but the mentor hasn't declared an outcome."""
        if self.status != STATUS_CONFIRMED:
            return False
        return datetime.now() >= self.scheduled_at + timedelta(minutes=self.duration_minutes)

    @property
    def status_label(self):
        if self.is_awaiting_completion:
            return "Not declared by mentor"
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_live(self):
        return self.status in LIVE_STATUSES

    def needs_action_from(self, role):
        if role == "mentor":
            return self.status in {STATUS_PENDING_MENTOR, STATUS_RESCHEDULE_BY_STUDENT}
        if role == "student":
            return self.status in {STATUS_RESCHEDULE_BY_MENTOR, STATUS_DECLINED}
        return False


class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mentorship_id = db.Column(db.Integer, db.ForeignKey("mentorship.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    resource_type = db.Column(db.String(20), nullable=False, default="link")
    url = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, server_default=db.func.now())

    mentorship = db.relationship("Mentorship", backref="resources")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient_role = db.Column(db.String(10), nullable=False)
    recipient_id = db.Column(db.Integer, nullable=False)
    message = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    session_id = db.Column(db.Integer, db.ForeignKey("session.id"), nullable=True)

    session_row = db.relationship("Session")


def migrate_sqlite_schema():
    """Add newly introduced columns safely if upgrading an existing SQLite database."""
    with db.engine.begin() as conn:
        session_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(session)"))}
        if session_cols:
            if "proposed_at" not in session_cols:
                conn.execute(text("ALTER TABLE session ADD COLUMN proposed_at DATETIME"))
            if "proposed_by" not in session_cols:
                conn.execute(text("ALTER TABLE session ADD COLUMN proposed_by VARCHAR(10)"))
            if "priority" not in session_cols:
                conn.execute(text("ALTER TABLE session ADD COLUMN priority VARCHAR(20) DEFAULT 'medium'"))
            conn.execute(text(
                "UPDATE session SET status = :new WHERE status = :old"
            ), {"new": STATUS_CONFIRMED, "old": "upcoming"})

        mentor_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(mentor)"))}
        if mentor_cols and "description" not in mentor_cols:
            conn.execute(text("ALTER TABLE mentor ADD COLUMN description TEXT"))

        notif_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(notification)"))}
        if notif_cols and "session_id" not in notif_cols:
            conn.execute(text("ALTER TABLE notification ADD COLUMN session_id INTEGER"))


with app.app_context():
    db.create_all()
    migrate_sqlite_schema()


def cleanup_expired_sessions():
    """
    1. Past sessions: A confirmed session whose scheduled time + duration has passed stays 'confirmed'
       until the mentor marks it completed / not completed. Both parties get a one-time reminder.
    2. Overdue requests: Any pending/reschedule session that has exceeded RESPONSE_TIME_LIMIT_HOURS
       or whose scheduled date has arrived without response is marked 'not_responded' and
       removed from live queues, but preserved in Session History.
    """
    now = datetime.now()
    updated = False

    # 1. Process awaiting sessions (unresponded / timed out requests)
    awaiting_sessions = Session.query.filter(Session.status.in_(AWAITING_STATUSES)).all()
    for s in awaiting_sessions:
        created_time = s.created_at or now
        time_limit_expired = now >= (created_time + timedelta(hours=RESPONSE_TIME_LIMIT_HOURS))
        meeting_time_passed = now >= s.scheduled_at

        if time_limit_expired or meeting_time_passed:
            s.status = STATUS_NOT_RESPONDED
            s.proposed_at = None
            s.proposed_by = None
            updated = True
            if s.mentorship:
                add_notification("student", s.mentorship.student_id,
                                 f"Session '{s.topic}' expired: not responded in time.", s.id)
                add_notification("mentor", s.mentorship.mentor_id,
                                 f"Session request '{s.topic}' expired: not responded in time.", s.id)

    # 2. Confirmed sessions whose meeting time has passed: the mentor must declare the outcome.
    #    We do NOT auto-complete; we remind the mentor and tell the student it's undeclared (once each).
    confirmed_sessions = Session.query.filter_by(status=STATUS_CONFIRMED).all()
    for s in confirmed_sessions:
        end_time = s.scheduled_at + timedelta(minutes=s.duration_minutes)
        if now >= end_time and s.mentorship:
            when = s.scheduled_at.strftime("%a %d %b, %I:%M %p")
            mentor_msg = (f"Session '{s.topic}' ({when}) has ended. "
                          f"Please mark it as completed or not completed.")
            student_msg = (f"Session '{s.topic}' ({when}) has ended, but it is still "
                           f"not declared by your mentor.")
            if not notification_exists("mentor", s.mentorship.mentor_id, s.id, mentor_msg):
                add_notification("mentor", s.mentorship.mentor_id, mentor_msg, s.id)
                updated = True
            if not notification_exists("student", s.mentorship.student_id, s.id, student_msg):
                add_notification("student", s.mentorship.student_id, student_msg, s.id)
                updated = True

    if updated:
        db.session.commit()


@app.before_request
def auto_cleanup_before_request():
    """Run automated lifecycle cleanup on each dashboard and action request."""
    if request.endpoint in {"student_page", "mentor_page", "book_session", "confirm_session",
                            "update_session_priority", "propose_reschedule", "accept_reschedule"}:
        cleanup_expired_sessions()


def admin_required():
    return session.get("current_role") == "admin" and bool(session.get("current_user"))


def add_notification(role, recipient_id, message, session_id=None):
    db.session.add(Notification(
        recipient_role=role,
        recipient_id=recipient_id,
        message=message,
        session_id=session_id,
    ))


def notification_exists(role, recipient_id, session_id, message):
    return Notification.query.filter_by(
        recipient_role=role, recipient_id=recipient_id, session_id=session_id, message=message
    ).first() is not None


def get_owned_mentorship(mentorship_id):
    mentorship = db.session.get(Mentorship, mentorship_id)
    if mentorship is None:
        return None

    role = session.get("current_role")
    current_user = session.get("current_user") or {}
    uid = current_user.get("id")

    if role == "student" and mentorship.student_id == uid:
        return mentorship
    if role == "mentor" and mentorship.mentor_id == uid:
        return mentorship
    return None


def get_owned_session(session_id):
    session_row = db.session.get(Session, session_id)
    if session_row is None:
        return None, None

    mentorship = get_owned_mentorship(session_row.mentorship_id)
    if mentorship is None:
        return None, None
    return session_row, mentorship


def parse_datetime(date_str, time_str):
    try:
        return datetime.strptime(f"{date_str.strip()} {time_str.strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def back_to_dashboard():
    if session.get("current_role") == "student":
        tab = request.form.get("return_tab", "sessions")
        return redirect(url_for("student_page", tab=tab))
    return redirect(url_for("mentor_page"))


def build_calendar_weeks(sessions):
    today = date.today()
    sessions_by_day = {}
    for s in sessions:
        sessions_by_day.setdefault(s.scheduled_at.date(), []).append(s)

    weeks, week = [], []
    for day in Calendar(firstweekday=6).itermonthdates(today.year, today.month):
        week.append({
            "day": day.day,
            "in_month": day.month == today.month,
            "is_today": day == today,
            "sessions": sessions_by_day.get(day, []),
        })
        if len(week) == 7:
            weeks.append(week)
            week = []
    return weeks, today.strftime("%B %Y")


@app.route("/")
def index():
    return redirect(url_for("home_page"))


@app.route("/Home")
def home_page():
    return render_template(
        "Home.html",
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("current_user") and session.get("current_role"):
        if session["current_role"] == "student":
            return redirect(url_for("student_page"))
        elif session["current_role"] == "mentor":
            return redirect(url_for("mentor_page"))

    error_message = None

    if request.method == "POST":
        role = request.form.get("role", "").strip()
        email = request.form.get("email", "").strip()
        full_name = request.form.get("full_name", "").strip()

        if role not in {"student", "mentor"} or not email or not full_name:
            error_message = "Please select a role and enter your full name and email."
        else:
            model = Student if role == "student" else Mentor
            user = model.query.filter_by(email=email).first()

            if user is None:
                error_message = "No account found. Please register first."
            elif user.full_name != full_name:
                error_message = "Name and email do not match. Please try again."
            else:
                session.clear()
                session.permanent = True  # Persist across refreshes
                session["current_role"] = role
                session["current_user"] = {
                    "id": user.id,
                    "full_name": user.full_name,
                    "email": user.email,
                }

                if role == "student":
                    return redirect(url_for("student_page"))
                return redirect(url_for("mentor_page"))

    return render_template("Login.html", error_message=error_message)


@app.route("/logout")
def logout_page():
    session.clear()
    return redirect(url_for("home_page"))


@app.route("/register", methods=["GET", "POST"])
def register_page():
    student_success_message = None
    student_error_message = None
    mentor_success_message = None
    mentor_error_message = None

    if request.method == "POST":
        role = request.form.get("role", "").strip()

        if role == "student":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            goal = request.form.get("goal", "").strip()
            learning_mode = request.form.get("learning_mode", "").strip()

            if full_name and email and goal and learning_mode:
                existing = Student.query.filter_by(email=email).first()
                if existing:
                    student_error_message = "A student with this email already exists. Please log in instead."
                else:
                    student = Student(
                        full_name=full_name,
                        email=email,
                        goal=goal,
                        learning_mode=learning_mode,
                    )
                    db.session.add(student)
                    db.session.commit()
                    student_success_message = "Account created. You can now log in as a student."
            else:
                student_error_message = "Please fill in all student form fields."

        elif role == "mentor":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip()
            expertise = request.form.get("expertise", "").strip()
            mentoring_format = request.form.get("mentoring_format", "").strip()
            description = request.form.get("description", "").strip()

            if full_name and email and expertise and mentoring_format:
                existing = Mentor.query.filter_by(email=email).first()
                if existing:
                    mentor_error_message = "A mentor with this email already exists. Please log in instead."
                else:
                    mentor = Mentor(
                        full_name=full_name,
                        email=email,
                        expertise=expertise,
                        mentoring_format=mentoring_format,
                        description=description,
                    )
                    db.session.add(mentor)
                    db.session.commit()
                    mentor_success_message = "Account created. You can now log in as a mentor."
            else:
                mentor_error_message = "Please select a role and fill in all required fields."

    selected_role = request.values.get("role", "student")
    if selected_role not in {"student", "mentor"}:
        selected_role = "student"

    return render_template(
        "Register.html",
        selected_role=selected_role,
        student_success_message=student_success_message,
        student_error_message=student_error_message,
        mentor_success_message=mentor_success_message,
        mentor_error_message=mentor_error_message,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/choose-role", methods=["POST"])
def choose_role():
    selected_role = request.form.get("role")
    return redirect(url_for("register_page", role=selected_role))


@app.route("/students")
def students_list_page():
    if not admin_required():
        return redirect(url_for("admin_login_page"))

    students = Student.query.order_by(Student.id.asc()).all()
    return render_template(
        "StudentsList.html",
        students=students,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/mentors")
def mentors_list_page():
    if not admin_required():
        return redirect(url_for("admin_login_page"))

    mentors = Mentor.query.order_by(Mentor.id.asc()).all()
    return render_template(
        "MentorsList.html",
        mentors=mentors,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/student")
def student_page():
    if session.get("current_role") != "student" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    user_id = int(session["current_user"]["id"])
    student = db.session.get(Student, user_id)
    if student is None:
        session.clear()
        return redirect(url_for("login_page"))

    cleanup_expired_sessions()

    my_mentorships = Mentorship.query.filter_by(student_id=student.id).all()
    mentorship_ids = [m.id for m in my_mentorships]
    connected_mentor_ids = {m.mentor_id for m in my_mentorships}

    mentor_search = request.args.get("mentor_search", "").strip()
    available_mentors_query = (
        Mentor.query.filter(~Mentor.id.in_(connected_mentor_ids))
        if connected_mentor_ids
        else Mentor.query
    )

    if mentor_search:
        term = f"%{mentor_search}%"
        available_mentors_query = available_mentors_query.filter(
            or_(
                Mentor.full_name.ilike(term),
                Mentor.expertise.ilike(term),
                Mentor.mentoring_format.ilike(term),
                Mentor.description.ilike(term),
            )
        )

    available_mentors = available_mentors_query.order_by(Mentor.full_name).all()

    # Query all sessions
    all_sessions_raw = (
        Session.query.filter(Session.mentorship_id.in_(mentorship_ids))
        .all()
        if mentorship_ids
        else []
    )

    # Priority Queue sort: Urgent (1) -> High (2) -> Medium (3) -> Low (4), then earliest scheduled_at
    all_sessions = sorted(
        all_sessions_raw,
        key=lambda s: (s.priority_rank, s.scheduled_at)
    )

    # Active upcoming sessions (scheduled time is in the future)
    now = datetime.now()
    confirmed_sessions = [s for s in all_sessions if s.status == STATUS_CONFIRMED and s.scheduled_at > now]
    completed_sessions = [s for s in all_sessions if s.status == STATUS_COMPLETED]
    awaiting_sessions = [s for s in all_sessions if s.status in AWAITING_STATUSES]
    cancelled_sessions = [s for s in all_sessions if s.status in {STATUS_CANCELLED, STATUS_DECLINED, STATUS_NOT_RESPONDED, STATUS_NOT_COMPLETED}]
    action_sessions = [s for s in all_sessions if s.needs_action_from("student")]

    total_hours = round(sum(s.duration_minutes for s in completed_sessions) / 60, 1)
    next_session = confirmed_sessions[0] if confirmed_sessions else None

    # Sessions tab filter
    status_filter = request.args.get("filter", "all")
    if status_filter == "action":
        filtered_sessions = action_sessions
    elif status_filter == "confirmed":
        filtered_sessions = confirmed_sessions
    elif status_filter == "completed":
        filtered_sessions = completed_sessions
    elif status_filter == "cancelled":
        filtered_sessions = cancelled_sessions
    else:
        status_filter = "all"
        filtered_sessions = all_sessions

    # Session history list is ordered by date & time (latest first), not by priority
    filtered_sessions = sorted(filtered_sessions, key=lambda s: s.scheduled_at, reverse=True)

    selected_id = request.args.get("session_id", type=int)
    selected_session = next((s for s in all_sessions if s.id == selected_id), None) if selected_id else None
    if selected_session is None and filtered_sessions:
        selected_session = filtered_sessions[0]

    calendar_weeks, month_label = build_calendar_weeks([s for s in all_sessions if s.is_live and s.scheduled_at > now])

    all_resources = (
        Resource.query.filter(Resource.mentorship_id.in_(mentorship_ids))
        .order_by(Resource.uploaded_at.desc())
        .all()
        if mentorship_ids
        else []
    )
    query = request.args.get("q", "").strip()
    type_filter = request.args.get("type", "all")
    filtered_resources = all_resources
    if query:
        filtered_resources = [r for r in filtered_resources if query.lower() in r.title.lower()]
    if type_filter in {"pdf", "video", "code", "link"}:
        filtered_resources = [r for r in filtered_resources if r.resource_type == type_filter]

    notifications = (
        Notification.query.filter_by(recipient_role="student", recipient_id=student.id)
        .order_by(Notification.created_at.desc())
        .limit(8)
        .all()
    )
    unread_count = Notification.query.filter_by(
        recipient_role="student", recipient_id=student.id, is_read=False
    ).count()

    avg_progress = round(sum(m.progress for m in my_mentorships) / len(my_mentorships)) if my_mentorships else 0

    return render_template(
        "Student.html",
        student=student,
        my_mentorships=my_mentorships,
        available_mentors=available_mentors,
        mentor_search=mentor_search,
        active_tab=request.args.get("tab", "overview"),
        sessions=filtered_sessions,
        all_sessions=all_sessions,
        status_filter=status_filter,
        selected_session=selected_session,
        next_session=next_session,
        action_sessions=action_sessions,
        action_count=len(action_sessions),
        total_count=len(all_sessions),
        confirmed_count=len(confirmed_sessions),
        awaiting_count=len(awaiting_sessions),
        completed_count=len(completed_sessions),
        cancelled_count=len(cancelled_sessions),
        total_hours=total_hours,
        calendar_weeks=calendar_weeks,
        month_label=month_label,
        resources=filtered_resources,
        recent_resources=all_resources[:3],
        resources_total=len(all_resources),
        query=query,
        type_filter=type_filter,
        avg_progress=avg_progress,
        notifications=notifications,
        unread_count=unread_count,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/student/sessions")
def student_sessions_page():
    args = request.args.to_dict()
    args["tab"] = "sessions"
    return redirect(url_for("student_page", **args))


@app.route("/student/resources")
def student_resources_page():
    args = request.args.to_dict()
    args["tab"] = "resources"
    return redirect(url_for("student_page", **args))


@app.route("/student/connect/<int:mentor_id>", methods=["POST"])
def student_connect_mentor(mentor_id):
    if session.get("current_role") != "student" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    student_id = session["current_user"]["id"]
    student_name = session["current_user"]["full_name"]
    mentor = db.session.get(Mentor, mentor_id)

    if mentor is None:
        flash("That mentor no longer exists.", "error")
        return redirect(url_for("student_page"))

    existing = Mentorship.query.filter_by(student_id=student_id, mentor_id=mentor_id).first()
    if existing is not None:
        flash(f"You're already connected with {mentor.full_name}.", "error")
        return redirect(url_for("student_page"))

    mentorship = Mentorship(student_id=student_id, mentor_id=mentor_id, progress=0, status="active")
    db.session.add(mentorship)
    add_notification("mentor", mentor_id, f"{student_name} connected with you as a mentee.")
    db.session.commit()
    flash(f"Connected with {mentor.full_name}.", "success")
    return redirect(url_for("student_page"))


@app.route("/session/book/<int:mentorship_id>", methods=["POST"])
def book_session(mentorship_id):
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    mentorship = get_owned_mentorship(mentorship_id)
    if mentorship is None:
        flash("Couldn't book that session — you don't have access to that mentorship.", "error")
        return back_to_dashboard()

    duration = request.form.get("duration", "60").strip()
    mode = request.form.get("mode", "Google Meet").strip() or "Google Meet"
    topic = request.form.get("topic", "").strip()
    scheduled_at = parse_datetime(request.form.get("date", ""), request.form.get("time", ""))
    priority = request.form.get("priority", "medium").strip().lower()
    if priority not in {"low", "medium", "high", "urgent"}:
        priority = "medium"

    if scheduled_at is None or not topic or not duration.isdigit():
        flash("Couldn't book that session — please fill in all required fields.", "error")
        return back_to_dashboard()

    if scheduled_at <= datetime.now():
        flash("Session time must be in the future.", "error")
        return back_to_dashboard()

    booked_by_student = session["current_role"] == "student"
    new_session = Session(
        mentorship_id=mentorship.id,
        scheduled_at=scheduled_at,
        duration_minutes=int(duration),
        mode=mode,
        topic=topic,
        status=STATUS_PENDING_MENTOR if booked_by_student else STATUS_CONFIRMED,
        priority=priority,
    )
    db.session.add(new_session)
    db.session.flush()

    actor_name = session["current_user"]["full_name"]
    when = scheduled_at.strftime("%a %d %b, %I:%M %p")
    if booked_by_student:
        add_notification(
            "mentor", mentorship.mentor_id,
            f"{actor_name} requested a session: {topic} — {when}. Confirm within 24h.",
            new_session.id,
        )
        add_notification(
            "student", mentorship.student_id,
            f"Request sent to {mentorship.mentor.full_name} for '{topic}' — {when}. Awaiting response.",
            new_session.id,
        )
        flash(f"Booking request sent to {mentorship.mentor.full_name} (24h response limit).", "success")
    else:
        add_notification(
            "student", mentorship.student_id,
            f"{actor_name} scheduled a session with you: {topic} — {when}.",
            new_session.id,
        )
        flash(f"Session with {mentorship.student.full_name} confirmed for {when}.", "success")

    db.session.commit()
    return back_to_dashboard()


@app.route("/session/<int:session_id>/priority", methods=["POST"])
def update_session_priority(session_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None:
        flash("Couldn't find that session, or it's not yours to modify.", "error")
        return redirect(url_for("mentor_page"))

    new_priority = request.form.get("priority", "medium").strip().lower()
    if new_priority in {"low", "medium", "high", "urgent"}:
        session_row.priority = new_priority
        db.session.commit()
        flash(f"Priority for '{session_row.topic}' updated to {new_priority.capitalize()}.", "success")
    else:
        flash("Invalid priority selected.", "error")

    return redirect(url_for("mentor_page"))


@app.route("/session/<int:session_id>/confirm", methods=["POST"])
def confirm_session(session_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None:
        flash("Couldn't find that session, or it's not yours to confirm.", "error")
        return back_to_dashboard()
    if session_row.status != STATUS_PENDING_MENTOR:
        flash(f"That session is already '{session_row.status_label}' — nothing to confirm.", "error")
        return back_to_dashboard()

    session_row.status = STATUS_CONFIRMED
    session_row.proposed_at = None
    session_row.proposed_by = None
    add_notification(
        "student", mentorship.student_id,
        f"{session['current_user']['full_name']} confirmed your session: {session_row.topic}.",
        session_row.id,
    )
    db.session.commit()
    flash(f"Confirmed '{session_row.topic}' with {mentorship.student.full_name}.", "success")
    return back_to_dashboard()


@app.route("/session/<int:session_id>/decline", methods=["POST"])
def decline_session(session_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None:
        flash("Couldn't find that session, or it's not yours to decline.", "error")
        return back_to_dashboard()
    if session_row.status not in {STATUS_PENDING_MENTOR, STATUS_RESCHEDULE_BY_STUDENT}:
        flash(f"That session is already '{session_row.status_label}' — nothing to decline.", "error")
        return back_to_dashboard()

    session_row.status = STATUS_DECLINED
    session_row.proposed_at = None
    session_row.proposed_by = None
    reason = request.form.get("reason", "").strip()
    tail = f" Reason: {reason}" if reason else " You can pick another slot."
    add_notification(
        "student", mentorship.student_id,
        f"{session['current_user']['full_name']} could not take '{session_row.topic}'.{tail}",
        session_row.id,
    )
    db.session.commit()
    flash(f"Declined '{session_row.topic}'.", "success")
    return back_to_dashboard()


@app.route("/session/<int:session_id>/propose", methods=["POST"])
def propose_reschedule(session_id):
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None:
        flash("Couldn't find that session, or it's not yours to reschedule.", "error")
        return back_to_dashboard()
    if not session_row.is_live:
        flash(f"That session is '{session_row.status_label}' — it can't be rescheduled.", "error")
        return back_to_dashboard()

    new_time = parse_datetime(request.form.get("date", ""), request.form.get("time", ""))
    if new_time is None or new_time <= datetime.now():
        flash("Couldn't propose a new time — please enter a valid future date and time.", "error")
        return back_to_dashboard()

    role = session["current_role"]
    actor_name = session["current_user"]["full_name"]
    when = new_time.strftime("%a %d %b, %I:%M %p")

    session_row.proposed_at = new_time
    session_row.proposed_by = role
    session_row.created_at = datetime.now()  # Reset 24-hour response timer for counter-offer

    if role == "mentor":
        session_row.status = STATUS_RESCHEDULE_BY_MENTOR
        add_notification(
            "student", mentorship.student_id,
            f"{actor_name} proposed a new time for '{session_row.topic}': {when}. Respond within 24h.",
            session_row.id,
        )
    else:
        session_row.status = STATUS_RESCHEDULE_BY_STUDENT
        add_notification(
            "mentor", mentorship.mentor_id,
            f"{actor_name} proposed a new time for '{session_row.topic}': {when}. Respond within 24h.",
            session_row.id,
        )
    db.session.commit()
    flash(f"Proposed {when} for '{session_row.topic}'. Waiting for response.", "success")
    return back_to_dashboard()


@app.route("/session/<int:session_id>/accept-proposal", methods=["POST"])
def accept_reschedule(session_id):
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    role = session["current_role"]

    expected = {
        "student": STATUS_RESCHEDULE_BY_MENTOR,
        "mentor": STATUS_RESCHEDULE_BY_STUDENT,
    }[role]

    if session_row is None:
        flash("Couldn't find that session, or it's not yours.", "error")
        return back_to_dashboard()
    if session_row.status != expected or session_row.proposed_at is None:
        flash("There's no pending proposal on that session for you to accept.", "error")
        return back_to_dashboard()

    session_row.scheduled_at = session_row.proposed_at
    session_row.proposed_at = None
    session_row.proposed_by = None
    session_row.status = STATUS_CONFIRMED

    actor_name = session["current_user"]["full_name"]
    when = session_row.scheduled_at.strftime("%a %d %b, %I:%M %p")
    target_role = "mentor" if role == "student" else "student"
    target_id = mentorship.mentor_id if role == "student" else mentorship.student_id
    add_notification(
        target_role, target_id,
        f"{actor_name} accepted your new time for '{session_row.topic}': {when}.",
        session_row.id,
    )
    db.session.commit()
    flash(f"'{session_row.topic}' confirmed for {when}.", "success")
    return back_to_dashboard()


@app.route("/session/<int:session_id>/reject-proposal", methods=["POST"])
def reject_reschedule(session_id):
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    role = session["current_role"]
    expected = {
        "student": STATUS_RESCHEDULE_BY_MENTOR,
        "mentor": STATUS_RESCHEDULE_BY_STUDENT,
    }[role]

    if session_row is None:
        flash("Couldn't find that session, or it's not yours.", "error")
        return back_to_dashboard()
    if session_row.status != expected:
        flash("There's no pending proposal on that session to decline.", "error")
        return back_to_dashboard()

    session_row.proposed_at = None
    session_row.proposed_by = None
    session_row.status = STATUS_PENDING_MENTOR

    actor_name = session["current_user"]["full_name"]
    original = session_row.scheduled_at.strftime("%a %d %b, %I:%M %p")
    target_role = "mentor" if role == "student" else "student"
    target_id = mentorship.mentor_id if role == "student" else mentorship.student_id
    add_notification(
        target_role, target_id,
        f"{actor_name} declined the new time for '{session_row.topic}' and kept {original}.",
        session_row.id,
    )
    db.session.commit()
    flash(f"Kept the original time — {original}.", "success")
    return back_to_dashboard()


@app.route("/session/<int:session_id>/cancel", methods=["POST"])
def cancel_session(session_id):
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None:
        flash("Couldn't find that session, or it's not yours to cancel.", "error")
        return back_to_dashboard()
    if not session_row.is_live:
        flash(f"That session is already '{session_row.status_label}' — nothing to cancel.", "error")
        return back_to_dashboard()

    session_row.status = STATUS_CANCELLED
    session_row.proposed_at = None
    session_row.proposed_by = None

    actor_name = session["current_user"]["full_name"]
    if session["current_role"] == "student":
        add_notification("mentor", mentorship.mentor_id,
                         f"{actor_name} cancelled the session: {session_row.topic}.", session_row.id)
    else:
        add_notification("student", mentorship.student_id,
                         f"{actor_name} cancelled the session: {session_row.topic}.", session_row.id)
    db.session.commit()
    flash(f"Cancelled '{session_row.topic}'.", "success")
    return back_to_dashboard()


@app.route("/session/<int:session_id>/complete", methods=["POST"])
def complete_session(session_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None:
        flash("Couldn't find that session, or it's not yours.", "error")
        return redirect(url_for("mentor_page"))
    if session_row.status != STATUS_CONFIRMED:
        flash(f"That session is '{session_row.status_label}' — only a confirmed session can be marked complete.", "error")
        return redirect(url_for("mentor_page"))

    session_row.status = STATUS_COMPLETED
    notes = request.form.get("notes", "").strip()
    if notes:
        session_row.notes = notes
    add_notification(
        "student", mentorship.student_id,
        f"{session['current_user']['full_name']} marked '{session_row.topic}' as completed.",
        session_row.id,
    )
    db.session.commit()
    flash(f"Marked '{session_row.topic}' as completed.", "success")
    return redirect(url_for("mentor_page"))


@app.route("/session/<int:session_id>/not-complete", methods=["POST"])
def not_complete_session(session_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None:
        flash("Couldn't find that session, or it's not yours.", "error")
        return redirect(url_for("mentor_page"))
    if session_row.status != STATUS_CONFIRMED:
        flash(f"That session is '{session_row.status_label}' — only a confirmed session can be marked not completed.", "error")
        return redirect(url_for("mentor_page"))

    session_row.status = STATUS_NOT_COMPLETED
    notes = request.form.get("notes", "").strip()
    if notes:
        session_row.notes = notes
    add_notification(
        "student", mentorship.student_id,
        f"{session['current_user']['full_name']} marked '{session_row.topic}' as not completed.",
        session_row.id,
    )
    db.session.commit()
    flash(f"Marked '{session_row.topic}' as not completed.", "success")
    return redirect(url_for("mentor_page"))


def save_uploaded_pdf(file_storage):
    if file_storage is None or not file_storage.filename:
        return None, None

    original_name = secure_filename(file_storage.filename)
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if extension not in ALLOWED_RESOURCE_EXTENSIONS:
        return None, "error:type"

    stored_name = f"{uuid4().hex}.{extension}"
    file_storage.save(UPLOAD_DIR / stored_name)
    return url_for("static", filename=f"uploads/resources/{stored_name}"), None


@app.route("/resource/add/<int:mentorship_id>", methods=["POST"])
def add_resource(mentorship_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    mentorship = get_owned_mentorship(mentorship_id)
    if mentorship is None:
        flash("Couldn't share that resource — that mentorship isn't yours.", "error")
        return redirect(url_for("mentor_page"))

    title = request.form.get("title", "").strip()
    resource_type = request.form.get("resource_type", "link").strip()
    if resource_type not in {"pdf", "video", "code", "link"}:
        resource_type = "link"
    url_value = request.form.get("url", "").strip()
    uploaded_file = request.files.get("file")

    if not title:
        flash("Couldn't share that resource — a title is required.", "error")
        return redirect(url_for("mentor_page"))

    if resource_type == "pdf":
        saved_url, error = save_uploaded_pdf(uploaded_file)
        if error == "error:type":
            flash("Couldn't share that resource — only PDF files can be uploaded here.", "error")
            return redirect(url_for("mentor_page"))
        if saved_url is None:
            flash("Couldn't share that resource — choose a PDF file to upload.", "error")
            return redirect(url_for("mentor_page"))
        url_value = saved_url
    elif not url_value:
        flash("Couldn't share that resource — a link is required for this type.", "error")
        return redirect(url_for("mentor_page"))

    db.session.add(Resource(
        mentorship_id=mentorship.id,
        title=title,
        resource_type=resource_type,
        url=url_value,
    ))
    mentor_name = session["current_user"]["full_name"]
    add_notification("student", mentorship.student_id, f"{mentor_name} shared a new resource: {title}.")
    db.session.commit()
    flash(f"Shared '{title}' with {mentorship.student.full_name}.", "success")
    return redirect(url_for("mentor_page"))


@app.route("/notifications/mark-read", methods=["POST"])
def mark_notifications_read():
    role = session.get("current_role")
    current_user = session.get("current_user") or {}
    uid = current_user.get("id")

    if role in {"student", "mentor"} and uid:
        Notification.query.filter_by(recipient_role=role, recipient_id=uid, is_read=False).update({"is_read": True})
        db.session.commit()

    if role == "student":
        return redirect(url_for("student_page", tab=request.form.get("return_tab", "overview")))
    return redirect(url_for("mentor_page"))


@app.route("/mentor")
def mentor_page():
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    user_id = int(session["current_user"]["id"])
    mentor = db.session.get(Mentor, user_id)
    if mentor is None:
        session.clear()
        return redirect(url_for("login_page"))

    cleanup_expired_sessions()

    expertise_tags = [tag.strip() for tag in mentor.expertise.split(",") if tag.strip()]
    my_mentorships = Mentorship.query.filter_by(mentor_id=mentor.id).all()
    mentorship_ids = [m.id for m in my_mentorships]

    all_sessions_raw = (
        Session.query.filter(Session.mentorship_id.in_(mentorship_ids))
        .all()
        if mentorship_ids
        else []
    )

    # Priority Queue sort: Urgent (1) -> High (2) -> Medium (3) -> Low (4), then earliest scheduled_at
    all_sessions = sorted(
        all_sessions_raw,
        key=lambda s: (s.priority_rank, s.scheduled_at)
    )

    now = datetime.now()
    pending_requests = [s for s in all_sessions if s.needs_action_from("mentor")]
    # Confirmed sessions stay here until the mentor declares them completed / not completed.
    # Ended-but-undeclared sessions are listed first so they get attention.
    confirmed_all = [s for s in all_sessions if s.status == STATUS_CONFIRMED]
    upcoming_sessions = (
        [s for s in confirmed_all if s.is_awaiting_completion]
        + [s for s in confirmed_all if not s.is_awaiting_completion and s.scheduled_at > now]
    )
    # Session history ordered by date & time (latest first), not by priority
    history_sessions = sorted(all_sessions_raw, key=lambda s: s.scheduled_at, reverse=True)

    notifications = (
        Notification.query.filter_by(recipient_role="mentor", recipient_id=mentor.id)
        .order_by(Notification.created_at.desc())
        .limit(8)
        .all()
    )
    unread_count = Notification.query.filter_by(
        recipient_role="mentor", recipient_id=mentor.id, is_read=False
    ).count()

    return render_template(
        "Mentor.html",
        mentor=mentor,
        expertise_tags=expertise_tags,
        my_mentorships=my_mentorships,
        upcoming_sessions=upcoming_sessions,
        pending_requests=pending_requests,
        pending_count=len(pending_requests),
        all_sessions=all_sessions,
        history_sessions=history_sessions,
        notifications=notifications,
        unread_count=unread_count,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/mentor/update-progress/<int:mentorship_id>", methods=["POST"])
def mentor_update_progress(mentorship_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    mentorship = db.session.get(Mentorship, mentorship_id)

    if mentorship is None or mentorship.mentor_id != session["current_user"]["id"]:
        flash("Couldn't update progress — that student isn't yours.", "error")
        return redirect(url_for("mentor_page"))

    raw_progress = request.form.get("progress", "").strip()
    status = request.form.get("status", "").strip()

    if raw_progress.isdigit():
        mentorship.progress = max(0, min(100, int(raw_progress)))

    if status in {"active", "completed"}:
        mentorship.status = status

    mentor_name = session["current_user"]["full_name"]
    add_notification(
        "student",
        mentorship.student_id,
        f"{mentor_name} updated your progress to {mentorship.progress}%.",
    )
    db.session.commit()
    flash(f"Updated {mentorship.student.full_name}'s progress to {mentorship.progress}%.", "success")
    return redirect(url_for("mentor_page"))


@app.route("/admin/register", methods=["GET", "POST"])
def admin_register_page():
    error_message = None
    success_message = None

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        setup_key = request.form.get("setup_key", "")

        if not full_name or not email or not password or not setup_key:
            error_message = "Please fill in every field, including the setup key."
        elif setup_key != ADMIN_SETUP_KEY:
            error_message = "That setup key is not correct."
        elif Admin.query.filter_by(email=email).first():
            error_message = "An admin with this email already exists. Please log in instead."
        else:
            admin = Admin(
                full_name=full_name,
                email=email,
                password_hash=generate_password_hash(password),
            )
            db.session.add(admin)
            db.session.commit()
            success_message = "Admin account created. You can now log in."

    return render_template(
        "AdminLogin.html",
        mode="register",
        error_message=error_message,
        success_message=success_message,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login_page():
    error_message = None

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        admin = Admin.query.filter_by(email=email).first()

        if admin is None or not check_password_hash(admin.password_hash, password):
            error_message = "Incorrect email or password."
        else:
            session.clear()
            session.permanent = True
            session["current_role"] = "admin"
            session["current_user"] = {
                "id": admin.id,
                "full_name": admin.full_name,
                "email": admin.email,
            }
            return redirect(url_for("admin_dashboard_page"))

    return render_template(
        "AdminLogin.html",
        mode="login",
        error_message=error_message,
        success_message=None,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/admin")
def admin_dashboard_page():
    if not admin_required():
        return redirect(url_for("admin_login_page"))

    students = Student.query.order_by(Student.id.desc()).all()
    mentors = Mentor.query.order_by(Mentor.id.desc()).all()
    all_sessions = Session.query.order_by(Session.scheduled_at.desc()).all()

    unmatched_count = sum(1 for s in students if not s.mentorships)
    awaiting_declaration_count = sum(1 for s in all_sessions if s.is_awaiting_completion)
    completed_count = sum(1 for s in all_sessions if s.status == STATUS_COMPLETED)

    active_tab = request.args.get("tab", "students")
    if active_tab not in {"students", "mentors", "sessions"}:
        active_tab = "students"

    return render_template(
        "AdminDashboard.html",
        students=students,
        mentors=mentors,
        all_sessions=all_sessions,
        student_count=len(students),
        mentor_count=len(mentors),
        unmatched_count=unmatched_count,
        awaiting_declaration_count=awaiting_declaration_count,
        session_count=len(all_sessions),
        completed_count=completed_count,
        active_tab=active_tab,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/admin/students/<int:student_id>/edit", methods=["POST"])
def admin_edit_student(student_id):
    if not admin_required():
        return redirect(url_for("admin_login_page"))

    student = db.session.get(Student, student_id)
    if student is None:
        flash("That student no longer exists.", "error")
        return redirect(url_for("admin_dashboard_page", tab="students"))

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    goal = request.form.get("goal", "").strip()
    learning_mode = request.form.get("learning_mode", "").strip()

    if not (full_name and email and goal) or learning_mode not in {"Online", "Offline", "Hybrid"}:
        flash("Couldn't update the student — please fill in every field.", "error")
    elif Student.query.filter(Student.email == email, Student.id != student.id).first():
        flash("Another student already uses that email.", "error")
    else:
        student.full_name = full_name
        student.email = email
        student.goal = goal
        student.learning_mode = learning_mode
        db.session.commit()
        flash(f"Updated {student.full_name}.", "success")
    return redirect(url_for("admin_dashboard_page", tab="students"))


@app.route("/admin/mentors/<int:mentor_id>/edit", methods=["POST"])
def admin_edit_mentor(mentor_id):
    if not admin_required():
        return redirect(url_for("admin_login_page"))

    mentor = db.session.get(Mentor, mentor_id)
    if mentor is None:
        flash("That mentor no longer exists.", "error")
        return redirect(url_for("admin_dashboard_page", tab="mentors"))

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip()
    expertise = request.form.get("expertise", "").strip()
    mentoring_format = request.form.get("mentoring_format", "").strip()
    description = request.form.get("description", "").strip()

    if not (full_name and email and expertise) or mentoring_format not in {"One to one", "Group", "Both"}:
        flash("Couldn't update the mentor — please fill in every required field.", "error")
    elif Mentor.query.filter(Mentor.email == email, Mentor.id != mentor.id).first():
        flash("Another mentor already uses that email.", "error")
    else:
        mentor.full_name = full_name
        mentor.email = email
        mentor.expertise = expertise
        mentor.mentoring_format = mentoring_format
        mentor.description = description
        db.session.commit()
        flash(f"Updated {mentor.full_name}.", "success")
    return redirect(url_for("admin_dashboard_page", tab="mentors"))


@app.route("/admin/export/<kind>.csv")
def admin_export_csv(kind):
    if not admin_required():
        return redirect(url_for("admin_login_page"))

    buffer = io.StringIO()
    writer = csv.writer(buffer)

    if kind == "students":
        writer.writerow(["ID", "Name", "Email", "Goal", "Learning mode", "Mentors connected"])
        for s in Student.query.order_by(Student.id).all():
            writer.writerow([s.id, s.full_name, s.email, s.goal, s.learning_mode, len(s.mentorships)])
    elif kind == "mentors":
        writer.writerow(["ID", "Name", "Email", "Expertise", "Format", "Students connected", "Description"])
        for m in Mentor.query.order_by(Mentor.id).all():
            writer.writerow([m.id, m.full_name, m.email, m.expertise, m.mentoring_format,
                             len(m.mentorships), m.description or ""])
    elif kind == "sessions":
        writer.writerow(["ID", "Date & time", "Duration (min)", "Topic", "Student", "Mentor",
                         "Mode", "Priority", "Status"])
        for s in Session.query.order_by(Session.scheduled_at.desc()).all():
            writer.writerow([s.id, s.scheduled_at.strftime("%Y-%m-%d %H:%M"), s.duration_minutes, s.topic,
                             s.mentorship.student.full_name, s.mentorship.mentor.full_name,
                             s.mode, s.priority, s.status_label])
    else:
        flash("Unknown export type.", "error")
        return redirect(url_for("admin_dashboard_page"))

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=educonnect_{kind}.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True)