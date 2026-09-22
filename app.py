from datetime import date, datetime
from calendar import Calendar
from pathlib import Path

from flask import Flask, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "EduConnect.db"

app = Flask(__name__, template_folder="Templates")
app.config["SECRET_KEY"] = "educonnect-dev-secret-key-change-later"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DATABASE_PATH.as_posix()}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Only people who know this key can create an admin account. Change this to
# your own private value before deploying anywhere, and don't share it
# beyond the one or two people you want as admins.
ADMIN_SETUP_KEY = "CHANGE-THIS-TO-YOUR-OWN-SECRET-KEY"

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Session status lifecycle
#
# pending_mentor         student booked; mentor must confirm or decline
# confirmed              both sides agreed; Join button is live
# declined               mentor refused; student can rebook
# reschedule_by_mentor   mentor proposed a new time; student must accept
# reschedule_by_student  student proposed a new time; mentor must accept
# completed / cancelled  terminal
# ---------------------------------------------------------------------------
STATUS_PENDING_MENTOR = "pending_mentor"
STATUS_CONFIRMED = "confirmed"
STATUS_DECLINED = "declined"
STATUS_RESCHEDULE_BY_MENTOR = "reschedule_by_mentor"
STATUS_RESCHEDULE_BY_STUDENT = "reschedule_by_student"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

# Still on the calendar — not finished, not called off.
LIVE_STATUSES = {
    STATUS_PENDING_MENTOR,
    STATUS_CONFIRMED,
    STATUS_RESCHEDULE_BY_MENTOR,
    STATUS_RESCHEDULE_BY_STUDENT,
}
# Waiting on somebody to press a button.
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
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    # A reschedule that has been offered but not yet answered. The original
    # scheduled_at stays untouched until the other side accepts, so declining
    # costs nothing.
    proposed_at = db.Column(db.DateTime, nullable=True)
    proposed_by = db.Column(db.String(10), nullable=True)  # "student" or "mentor"

    mentorship = db.relationship("Mentorship", backref="sessions")

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_live(self):
        return self.status in LIVE_STATUSES

    def needs_action_from(self, role):
        """True when this role is the one holding up the session."""
        if role == "mentor":
            return self.status in {STATUS_PENDING_MENTOR, STATUS_RESCHEDULE_BY_STUDENT}
        if role == "student":
            return self.status in {STATUS_RESCHEDULE_BY_MENTOR, STATUS_DECLINED}
        return False


class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mentorship_id = db.Column(db.Integer, db.ForeignKey("mentorship.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    resource_type = db.Column(db.String(20), nullable=False, default="link")  # pdf / video / code / link
    url = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, server_default=db.func.now())

    mentorship = db.relationship("Mentorship", backref="resources")


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient_role = db.Column(db.String(10), nullable=False)  # "student" or "mentor"
    recipient_id = db.Column(db.Integer, nullable=False)
    message = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    is_read = db.Column(db.Boolean, nullable=False, default=False)

    # Lets the dashboard render Confirm / Decline / Accept buttons directly
    # on the notification instead of making the user hunt for the session.
    session_id = db.Column(db.Integer, db.ForeignKey("session.id"), nullable=True)

    session_row = db.relationship("Session")


def migrate_sqlite_schema():
    """SQLite won't add columns to an existing table on create_all(), so add
    the new session/notification columns by hand if the database predates them.
    Safe to run on every boot — each step checks before it acts."""
    with db.engine.begin() as conn:
        session_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(session)"))}
        if session_cols:
            if "proposed_at" not in session_cols:
                conn.execute(text("ALTER TABLE session ADD COLUMN proposed_at DATETIME"))
            if "proposed_by" not in session_cols:
                conn.execute(text("ALTER TABLE session ADD COLUMN proposed_by VARCHAR(10)"))
            # Sessions booked under the old model were implicitly agreed.
            conn.execute(text(
                "UPDATE session SET status = :new WHERE status = :old"
            ), {"new": STATUS_CONFIRMED, "old": "upcoming"})

        notif_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(notification)"))}
        if notif_cols and "session_id" not in notif_cols:
            conn.execute(text("ALTER TABLE notification ADD COLUMN session_id INTEGER"))


with app.app_context():
    db.create_all()
    migrate_sqlite_schema()


def admin_required():
    """Returns True if the current session belongs to a logged-in admin."""
    return session.get("current_role") == "admin" and bool(session.get("current_user"))


def add_notification(role, recipient_id, message, session_id=None):
    """Create a notification for a student or mentor. Does not commit —
    caller is expected to commit alongside whatever else it just changed."""
    db.session.add(Notification(
        recipient_role=role,
        recipient_id=recipient_id,
        message=message,
        session_id=session_id,
    ))


def get_owned_mentorship(mentorship_id):
    """Return the Mentorship row only if the logged-in student or mentor
    is actually part of it — prevents editing someone else's connection."""
    mentorship = Mentorship.query.get(mentorship_id)
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
    """Return (Session, Mentorship) only if the logged-in student or mentor
    owns the mentorship the session belongs to."""
    session_row = Session.query.get(session_id)
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
    """Both roles land back on their own dashboard after acting on a session.
    For students this preserves whichever tab the action was taken from
    (each session/resource form carries a hidden `return_tab` field) so a
    Confirm or Cancel click doesn't bounce them back to Overview."""
    if session.get("current_role") == "student":
        tab = request.form.get("return_tab", "sessions")
        return redirect(url_for("student_page", tab=tab))
    return redirect(url_for("mentor_page"))


def build_calendar_weeks(sessions):
    """Real calendar grid for the current month, marking days that actually
    have sessions on them."""
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
    """One merged dashboard: overview, sessions and resources as tabs.
    Everything below is a live query — nothing on this page is hardcoded."""
    if session.get("current_role") != "student" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    student = Student.query.get(session["current_user"]["id"])
    if student is None:
        session.clear()
        return redirect(url_for("login_page"))

    my_mentorships = Mentorship.query.filter_by(student_id=student.id).all()
    mentorship_ids = [m.id for m in my_mentorships]
    connected_mentor_ids = {m.mentor_id for m in my_mentorships}
    available_mentors = (
        Mentor.query.filter(~Mentor.id.in_(connected_mentor_ids)).order_by(Mentor.full_name).all()
        if connected_mentor_ids
        else Mentor.query.order_by(Mentor.full_name).all()
    )

    all_sessions = (
        Session.query.filter(Session.mentorship_id.in_(mentorship_ids))
        .order_by(Session.scheduled_at.desc())
        .all()
        if mentorship_ids
        else []
    )

    confirmed_sessions = [s for s in all_sessions if s.status == STATUS_CONFIRMED]
    completed_sessions = [s for s in all_sessions if s.status == STATUS_COMPLETED]
    awaiting_sessions = [s for s in all_sessions if s.status in AWAITING_STATUSES]
    cancelled_sessions = [s for s in all_sessions if s.status in {STATUS_CANCELLED, STATUS_DECLINED}]
    action_sessions = [s for s in all_sessions if s.needs_action_from("student")]

    total_hours = round(sum(s.duration_minutes for s in completed_sessions) / 60, 1)
    next_session = min(confirmed_sessions, key=lambda s: s.scheduled_at) if confirmed_sessions else None

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

    selected_id = request.args.get("session_id", type=int)
    selected_session = next((s for s in all_sessions if s.id == selected_id), None) if selected_id else None
    if selected_session is None and filtered_sessions:
        selected_session = filtered_sessions[0]

    calendar_weeks, month_label = build_calendar_weeks([s for s in all_sessions if s.is_live])

    # Resources tab — pulled from the Resource table, filtered server-side.
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
        active_tab=request.args.get("tab", "overview"),
        # sessions
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
        # resources
        resources=filtered_resources,
        recent_resources=all_resources[:3],
        resources_total=len(all_resources),
        query=query,
        type_filter=type_filter,
        # shared
        avg_progress=avg_progress,
        notifications=notifications,
        unread_count=unread_count,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/student/sessions")
def student_sessions_page():
    """Kept so old links and bookmarks still work — the sessions view now
    lives as a tab on the main dashboard."""
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
    mentor = Mentor.query.get(mentor_id)

    if mentor is not None:
        existing = Mentorship.query.filter_by(student_id=student_id, mentor_id=mentor_id).first()
        if existing is None:
            mentorship = Mentorship(student_id=student_id, mentor_id=mentor_id, progress=0, status="active")
            db.session.add(mentorship)
            add_notification("mentor", mentor_id, f"{student_name} connected with you as a mentee.")
            db.session.commit()

    return redirect(url_for("student_page"))


# ---------------------------------------------------------------------------
# Session booking and the confirm / reschedule handshake
# ---------------------------------------------------------------------------

@app.route("/session/book/<int:mentorship_id>", methods=["POST"])
def book_session(mentorship_id):
    """A student booking asks the mentor to confirm. A mentor scheduling
    directly is already confirmed on their side, so it goes straight through."""
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    mentorship = get_owned_mentorship(mentorship_id)
    if mentorship is not None:
        duration = request.form.get("duration", "60").strip()
        mode = request.form.get("mode", "Google Meet").strip() or "Google Meet"
        topic = request.form.get("topic", "").strip()
        scheduled_at = parse_datetime(request.form.get("date", ""), request.form.get("time", ""))

        if scheduled_at is not None and topic and duration.isdigit():
            booked_by_student = session["current_role"] == "student"
            new_session = Session(
                mentorship_id=mentorship.id,
                scheduled_at=scheduled_at,
                duration_minutes=int(duration),
                mode=mode,
                topic=topic,
                status=STATUS_PENDING_MENTOR if booked_by_student else STATUS_CONFIRMED,
            )
            db.session.add(new_session)
            db.session.flush()  # need new_session.id for the notification link

            actor_name = session["current_user"]["full_name"]
            when = scheduled_at.strftime("%a %d %b, %I:%M %p")
            if booked_by_student:
                add_notification(
                    "mentor", mentorship.mentor_id,
                    f"{actor_name} requested a session: {topic} — {when}. Confirm or propose another time.",
                    new_session.id,
                )
            else:
                add_notification(
                    "student", mentorship.student_id,
                    f"{actor_name} scheduled a session with you: {topic} — {when}.",
                    new_session.id,
                )

            db.session.commit()

    return back_to_dashboard()


@app.route("/session/<int:session_id>/confirm", methods=["POST"])
def confirm_session(session_id):
    """Mentor accepts the student's requested slot as-is."""
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is not None and session_row.status == STATUS_PENDING_MENTOR:
        session_row.status = STATUS_CONFIRMED
        session_row.proposed_at = None
        session_row.proposed_by = None
        add_notification(
            "student", mentorship.student_id,
            f"{session['current_user']['full_name']} confirmed your session: {session_row.topic}.",
            session_row.id,
        )
        db.session.commit()

    return back_to_dashboard()


@app.route("/session/<int:session_id>/decline", methods=["POST"])
def decline_session(session_id):
    """Mentor turns the request down. The student can book a fresh slot."""
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is not None and session_row.status in {STATUS_PENDING_MENTOR, STATUS_RESCHEDULE_BY_STUDENT}:
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

    return back_to_dashboard()


@app.route("/session/<int:session_id>/propose", methods=["POST"])
def propose_reschedule(session_id):
    """Either side offers a different time. The existing scheduled_at is left
    alone until the other side accepts, so nothing is lost if they decline."""
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is None or not session_row.is_live:
        return back_to_dashboard()

    new_time = parse_datetime(request.form.get("date", ""), request.form.get("time", ""))
    if new_time is not None:
        role = session["current_role"]
        actor_name = session["current_user"]["full_name"]
        when = new_time.strftime("%a %d %b, %I:%M %p")

        session_row.proposed_at = new_time
        session_row.proposed_by = role

        if role == "mentor":
            session_row.status = STATUS_RESCHEDULE_BY_MENTOR
            add_notification(
                "student", mentorship.student_id,
                f"{actor_name} proposed a new time for '{session_row.topic}': {when}. Accept or keep the original.",
                session_row.id,
            )
        else:
            session_row.status = STATUS_RESCHEDULE_BY_STUDENT
            add_notification(
                "mentor", mentorship.mentor_id,
                f"{actor_name} proposed a new time for '{session_row.topic}': {when}. Accept or decline.",
                session_row.id,
            )
        db.session.commit()

    return back_to_dashboard()


@app.route("/session/<int:session_id>/accept-proposal", methods=["POST"])
def accept_reschedule(session_id):
    """The side that did NOT propose accepts — the new time becomes real and
    the session is confirmed."""
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    role = session["current_role"]

    # Only the other party may accept; you can't rubber-stamp your own proposal.
    expected = {
        "student": STATUS_RESCHEDULE_BY_MENTOR,
        "mentor": STATUS_RESCHEDULE_BY_STUDENT,
    }[role]

    if session_row is not None and session_row.status == expected and session_row.proposed_at is not None:
        session_row.scheduled_at = session_row.proposed_at
        session_row.proposed_at = None
        session_row.proposed_by = None
        session_row.status = STATUS_CONFIRMED

        actor_name = session["current_user"]["full_name"]
        when = session_row.scheduled_at.strftime("%a %d %b, %I:%M %p")
        if role == "student":
            add_notification(
                "mentor", mentorship.mentor_id,
                f"{actor_name} accepted your new time for '{session_row.topic}': {when}.",
                session_row.id,
            )
        else:
            add_notification(
                "student", mentorship.student_id,
                f"{actor_name} accepted your new time for '{session_row.topic}': {when}.",
                session_row.id,
            )
        db.session.commit()

    return back_to_dashboard()


@app.route("/session/<int:session_id>/reject-proposal", methods=["POST"])
def reject_reschedule(session_id):
    """Declining a proposed time drops the offer and hands the session back to
    the mentor to confirm the original slot or suggest something else. Nothing
    is cancelled — the original scheduled_at was never overwritten."""
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    role = session["current_role"]
    expected = {
        "student": STATUS_RESCHEDULE_BY_MENTOR,
        "mentor": STATUS_RESCHEDULE_BY_STUDENT,
    }[role]

    if session_row is not None and session_row.status == expected:
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

    return back_to_dashboard()


@app.route("/session/<int:session_id>/cancel", methods=["POST"])
def cancel_session(session_id):
    if session.get("current_role") not in {"student", "mentor"} or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is not None and session_row.is_live:
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

    return back_to_dashboard()


@app.route("/session/<int:session_id>/complete", methods=["POST"])
def complete_session(session_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    session_row, mentorship = get_owned_session(session_id)
    if session_row is not None and session_row.status == STATUS_CONFIRMED:
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

    return redirect(url_for("mentor_page"))


@app.route("/resource/add/<int:mentorship_id>", methods=["POST"])
def add_resource(mentorship_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    mentorship = get_owned_mentorship(mentorship_id)
    if mentorship is not None:
        title = request.form.get("title", "").strip()
        resource_type = request.form.get("resource_type", "link").strip()
        url_value = request.form.get("url", "").strip()

        if title and url_value:
            db.session.add(Resource(
                mentorship_id=mentorship.id,
                title=title,
                resource_type=resource_type if resource_type in {"pdf", "video", "code", "link"} else "link",
                url=url_value,
            ))
            mentor_name = session["current_user"]["full_name"]
            add_notification("student", mentorship.student_id, f"{mentor_name} shared a new resource: {title}.")
            db.session.commit()

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

    # Only the logged-in mentor's own record is fetched here — this is the
    # mentor viewing their own profile, not a listing of other users' data.
    mentor = Mentor.query.get(session["current_user"]["id"])
    if mentor is None:
        session.clear()
        return redirect(url_for("login_page"))

    expertise_tags = [tag.strip() for tag in mentor.expertise.split(",") if tag.strip()]
    my_mentorships = Mentorship.query.filter_by(mentor_id=mentor.id).all()
    mentorship_ids = [m.id for m in my_mentorships]

    all_sessions = (
        Session.query.filter(Session.mentorship_id.in_(mentorship_ids))
        .order_by(Session.scheduled_at)
        .all()
        if mentorship_ids
        else []
    )

    # Requests sitting on the mentor's desk: new bookings to confirm, plus
    # reschedules the student has proposed.
    pending_requests = [s for s in all_sessions if s.needs_action_from("mentor")]
    upcoming_sessions = [s for s in all_sessions if s.status == STATUS_CONFIRMED]

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
        notifications=notifications,
        unread_count=unread_count,
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


@app.route("/mentor/update-progress/<int:mentorship_id>", methods=["POST"])
def mentor_update_progress(mentorship_id):
    if session.get("current_role") != "mentor" or not session.get("current_user"):
        return redirect(url_for("login_page"))

    mentorship = Mentorship.query.get(mentorship_id)

    # Only allow a mentor to update progress on their own mentorship rows —
    # never someone else's student.
    if mentorship is not None and mentorship.mentor_id == session["current_user"]["id"]:
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

    return render_template(
        "AdminDashboard.html",
        students=students,
        mentors=mentors,
        student_count=len(students),
        mentor_count=len(mentors),
        current_user=session.get("current_user"),
        current_role=session.get("current_role"),
    )


if __name__ == "__main__":
    app.run(debug=True)