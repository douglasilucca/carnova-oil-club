import csv
import os
import secrets
from datetime import date, datetime, timedelta, time
from functools import wraps
from io import BytesIO, StringIO

import qrcode
import stripe
from flask import Flask, Response, flash, redirect, render_template, request, send_file, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))

database_url = os.environ.get("DATABASE_URL", "sqlite:///oilclub.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@carnovaoil.com").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ChangeMe123!")


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)


class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50), default="")
    purchase_date = db.Column(db.Date, nullable=False, default=date.today)
    expiration_date = db.Column(db.Date, nullable=False)
    total_changes = db.Column(db.Integer, nullable=False, default=5)
    remaining_changes = db.Column(db.Integer, nullable=False, default=5)
    status = db.Column(db.String(30), nullable=False, default="active")
    stripe_payment_id = db.Column(db.String(255), unique=True)
    price_paid_cents = db.Column(db.Integer, nullable=False, default=22900)
    token = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    redemptions = db.relationship(
        "Redemption", backref="member", lazy=True, cascade="all, delete-orphan"
    )



class Vehicle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    year = db.Column(db.String(4), default="")
    make = db.Column(db.String(100), nullable=False)
    model = db.Column(db.String(100), nullable=False)
    trim = db.Column(db.String(100), default="")
    vin = db.Column(db.String(17), default="")
    plate = db.Column(db.String(30), default="")
    color = db.Column(db.String(50), default="")
    current_mileage = db.Column(db.String(30), default="")
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    member = db.relationship("Member", backref=db.backref("vehicles", lazy=True, cascade="all, delete-orphan"))

    @property
    def display_name(self):
        parts = [self.year, self.make, self.model, self.trim]
        return " ".join(part for part in parts if part).strip()



class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicle.id"), nullable=True)
    appointment_date = db.Column(db.Date, nullable=False)
    appointment_time = db.Column(db.Time, nullable=False)
    status = db.Column(db.String(30), nullable=False, default="scheduled")
    service_type = db.Column(db.String(100), nullable=False, default="Oil Change")
    customer_notes = db.Column(db.Text, default="")
    internal_notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    member = db.relationship("Member", backref=db.backref("appointments", lazy=True))
    vehicle = db.relationship("Vehicle", foreign_keys=[vehicle_id])

    @property
    def starts_at(self):
        return datetime.combine(self.appointment_date, self.appointment_time)


class Redemption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    vehicle_id = db.Column(db.Integer, db.ForeignKey("vehicle.id"), nullable=True)
    redeemed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    note = db.Column(db.Text, default="")
    employee = db.Column(db.String(255), default="Staff")
    vehicle = db.Column(db.String(255), default="")
    mileage = db.Column(db.String(30), default="")
    vin_last8 = db.Column(db.String(20), default="")
    linked_vehicle = db.relationship("Vehicle", foreign_keys=[vehicle_id])


def add_missing_columns():
    """Small safe migration for existing Render databases."""
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    statements = []

    if "member" in tables:
        member_columns = {column["name"] for column in inspector.get_columns("member")}
        if "price_paid_cents" not in member_columns:
            statements.append(
                "ALTER TABLE member ADD COLUMN price_paid_cents INTEGER DEFAULT 22900 NOT NULL"
            )

    if "redemption" not in tables:
        for statement in statements:
            try:
                db.session.execute(text(statement))
                db.session.commit()
            except Exception:
                db.session.rollback()
        return

    existing = {column["name"] for column in inspector.get_columns("redemption")}

    if "vehicle" not in existing:
        statements.append("ALTER TABLE redemption ADD COLUMN vehicle VARCHAR(255)")
    if "mileage" not in existing:
        statements.append("ALTER TABLE redemption ADD COLUMN mileage VARCHAR(30)")
    if "vin_last8" not in existing:
        statements.append("ALTER TABLE redemption ADD COLUMN vin_last8 VARCHAR(20)")
    if "vehicle_id" not in existing:
        statements.append("ALTER TABLE redemption ADD COLUMN vehicle_id INTEGER")

    for statement in statements:
        try:
            db.session.execute(text(statement))
            db.session.commit()
        except Exception:
            db.session.rollback()


def init_db():
    db.create_all()
    add_missing_columns()

    admin = Admin.query.filter(db.func.lower(Admin.email) == ADMIN_EMAIL).first()
    if not admin:
        db.session.add(
            Admin(
                email=ADMIN_EMAIL,
                password_hash=generate_password_hash(ADMIN_PASSWORD),
            )
        )
        db.session.commit()


@app.before_request
def ensure_database():
    init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def next_member_id():
    last = Member.query.order_by(Member.id.desc()).first()
    number = 1 if not last else last.id + 1
    return f"COC-{number:05d}"


def current_member_status(member):
    if member.status == "cancelled":
        return "cancelled"
    if member.expiration_date < date.today():
        return "expired"
    if member.remaining_changes <= 0:
        return "completed"
    return "active"


def refresh_member_statuses():
    changed = False
    for member in Member.query.all():
        new_status = current_member_status(member)
        if member.status != new_status:
            member.status = new_status
            changed = True
    if changed:
        db.session.commit()


def appointment_slots_for_day(day):
    if day.weekday() == 6:
        return []

    start_hour = int(os.environ.get("APPOINTMENT_START_HOUR", "9"))
    end_hour = int(os.environ.get("APPOINTMENT_END_HOUR", "17"))
    slot_minutes = int(os.environ.get("APPOINTMENT_SLOT_MINUTES", "60"))

    booked = {
        appointment.appointment_time.strftime("%H:%M")
        for appointment in Appointment.query.filter_by(appointment_date=day).filter(
            Appointment.status.in_(["scheduled", "confirmed"])
        ).all()
    }

    slots = []
    current = datetime.combine(day, time(start_hour, 0))
    end = datetime.combine(day, time(end_hour, 0))

    while current < end:
        value = current.strftime("%H:%M")
        if value not in booked and current > datetime.now():
            slots.append(value)
        current += timedelta(minutes=slot_minutes)

    return slots


def send_appointment_email(appointment, subject_prefix="Appointment"):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USERNAME")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM_EMAIL", smtp_user or "")
    if not all([smtp_host, smtp_user, smtp_password, sender, appointment.member.email]):
        return False

    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = f"{subject_prefix} — Carnova Oil Club"
    message["From"] = sender
    message["To"] = appointment.member.email
    vehicle_name = appointment.vehicle.display_name if appointment.vehicle else "Vehicle not selected"
    message.set_content(
        f"""Hello {appointment.member.name},

Your Carnova Oil Club appointment is scheduled.

Date: {appointment.appointment_date.strftime('%B %d, %Y')}
Time: {appointment.appointment_time.strftime('%I:%M %p')}
Service: {appointment.service_type}
Vehicle: {vehicle_name}
Status: {appointment.status.title()}

Carnova of Southborough
251 Turnpike Rd, Southborough, MA 01772
(978) 258-0029
"""
    )

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_host, port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(message)
        return True
    except Exception:
        return False


@app.context_processor
def shared_template_values():
    return {"today": date.today(), "current_year": date.today().year}


@app.route("/")
def index():
    return redirect(url_for("dashboard" if session.get("admin_id") else "login"))


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        admin = Admin.query.filter(db.func.lower(Admin.email) == email).first()

        if admin and check_password_hash(admin.password_hash, password):
            session.clear()
            session["admin_id"] = admin.id
            session["admin_email"] = admin.email
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    refresh_member_statuses()
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = Member.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Member.member_id.ilike(like),
                Member.name.ilike(like),
                Member.email.ilike(like),
                Member.phone.ilike(like),
            )
        )
    if status_filter:
        query = query.filter(Member.status == status_filter)

    members = query.order_by(Member.created_at.desc()).all()
    all_members = Member.query.all()
    all_redemptions = Redemption.query.count()
    expiring_cutoff = date.today() + timedelta(days=30)

    total_revenue_cents = sum((member.price_paid_cents or 0) for member in all_members)
    estimated_service_cost_cents = stats_cost = int(os.environ.get("ESTIMATED_COST_PER_CHANGE_CENTS", "6500"))
    outstanding_cost_cents = sum(member.remaining_changes for member in all_members) * estimated_service_cost_cents

    stats = {
        "total_members": len(all_members),
        "active_members": sum(
            1 for member in all_members if current_member_status(member) == "active"
        ),
        "remaining_changes": sum(member.remaining_changes for member in all_members),
        "redeemed_changes": all_redemptions,
        "revenue": total_revenue_cents / 100,
        "outstanding_cost": outstanding_cost_cents / 100,
        "estimated_profit": (total_revenue_cents - outstanding_cost_cents) / 100,
        "expiring_soon": sum(
            1
            for member in all_members
            if current_member_status(member) == "active"
            and date.today() <= member.expiration_date <= expiring_cutoff
        ),
    }

    upcoming_appointments = (
        Appointment.query.filter(
            Appointment.appointment_date >= date.today(),
            Appointment.status.in_(["scheduled", "confirmed"]),
        )
        .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc())
        .limit(6)
        .all()
    )

    recent_redemptions = (
        Redemption.query.order_by(Redemption.redeemed_at.desc()).limit(8).all()
    )

    return render_template(
        "dashboard.html",
        members=members,
        stats=stats,
        q=q,
        status_filter=status_filter,
        recent_redemptions=recent_redemptions,
        upcoming_appointments=upcoming_appointments,
    )


@app.route("/members/new", methods=["GET", "POST"])
@login_required
def new_member():
    if request.method == "POST":
        try:
            purchase = date.fromisoformat(
                request.form.get("purchase_date") or date.today().isoformat()
            )
            expiration_text = request.form.get("expiration_date")
            expiration = (
                date.fromisoformat(expiration_text)
                if expiration_text
                else purchase + timedelta(days=365)
            )
            total = max(1, int(request.form.get("total_changes", 5)))
        except (ValueError, TypeError):
            flash("Please verify the dates and number of oil changes.", "error")
            return render_template("member_form.html")

        member = Member(
            member_id=next_member_id(),
            name=request.form["name"].strip(),
            email=request.form["email"].strip().lower(),
            phone=request.form.get("phone", "").strip(),
            purchase_date=purchase,
            expiration_date=expiration,
            total_changes=total,
            remaining_changes=total,
            token=secrets.token_urlsafe(24),
        )
        db.session.add(member)
        db.session.commit()
        flash(f"Member {member.member_id} created.", "success")
        return redirect(url_for("member_detail", member_id=member.member_id))

    return render_template("member_form.html")


@app.route("/members/<member_id>")
@login_required
def member_detail(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    member.status = current_member_status(member)
    db.session.commit()

    redemptions = (
        Redemption.query.filter_by(member_id=member.id)
        .order_by(Redemption.redeemed_at.desc())
        .all()
    )
    public_url = request.url_root.rstrip("/") + url_for(
        "member_public", token=member.token
    )
    vehicles = Vehicle.query.filter_by(member_id=member.id).order_by(Vehicle.created_at.desc()).all()
    return render_template(
        "member_detail.html",
        member=member,
        vehicles=vehicles,
        redemptions=redemptions,
        public_url=public_url,
    )


@app.route("/members/<member_id>/edit", methods=["GET", "POST"])
@login_required
def edit_member(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()

    if request.method == "POST":
        try:
            member.name = request.form["name"].strip()
            member.email = request.form["email"].strip().lower()
            member.phone = request.form.get("phone", "").strip()
            member.expiration_date = date.fromisoformat(
                request.form["expiration_date"]
            )
            member.status = request.form.get("status", "active")
            member.total_changes = max(
                member.total_changes,
                int(request.form.get("total_changes", member.total_changes)),
            )
            member.remaining_changes = min(
                member.total_changes,
                max(
                    0,
                    int(
                        request.form.get(
                            "remaining_changes", member.remaining_changes
                        )
                    ),
                ),
            )
            db.session.commit()
        except (ValueError, TypeError):
            db.session.rollback()
            flash("Please verify the information entered.", "error")
            return render_template("member_edit.html", member=member)

        flash("Member information updated.", "success")
        return redirect(url_for("member_detail", member_id=member.member_id))

    return render_template("member_edit.html", member=member)


@app.route("/members/<member_id>/redeem", methods=["POST"])
@login_required
def redeem(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    member.status = current_member_status(member)

    if member.status != "active":
        flash("This membership is not active.", "error")
    elif member.remaining_changes <= 0:
        flash("No oil changes remain.", "error")
    elif member.expiration_date < date.today():
        flash("This membership has expired.", "error")
    else:
        member.remaining_changes -= 1
        member.status = current_member_status(member)
        selected_vehicle = None
        vehicle_id = request.form.get("vehicle_id", "").strip()
        if vehicle_id.isdigit():
            selected_vehicle = Vehicle.query.filter_by(id=int(vehicle_id), member_id=member.id).first()

        vehicle_text = request.form.get("vehicle", "").strip()
        vin_last8 = request.form.get("vin_last8", "").strip().upper()[-8:]
        mileage = request.form.get("mileage", "").strip()

        if selected_vehicle:
            vehicle_text = selected_vehicle.display_name
            vin_last8 = (selected_vehicle.vin or "")[-8:].upper()
            if mileage:
                selected_vehicle.current_mileage = mileage

        db.session.add(
            Redemption(
                member_id=member.id,
                vehicle_id=selected_vehicle.id if selected_vehicle else None,
                note=request.form.get("note", "").strip(),
                employee=session.get("admin_email", "Staff"),
                vehicle=vehicle_text,
                mileage=mileage,
                vin_last8=vin_last8,
            )
        )
        db.session.commit()
        flash("Oil change redeemed successfully.", "success")

    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/members/<member_id>/undo", methods=["POST"])
@login_required
def undo(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    last = (
        Redemption.query.filter_by(member_id=member.id)
        .order_by(Redemption.redeemed_at.desc())
        .first()
    )

    if last:
        db.session.delete(last)
        member.remaining_changes = min(
            member.total_changes, member.remaining_changes + 1
        )
        member.status = current_member_status(member)
        db.session.commit()
        flash("Last redemption was undone.", "success")
    else:
        flash("No redemption to undo.", "error")

    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/members/<member_id>/vehicles/new", methods=["GET", "POST"])
@login_required
def new_vehicle(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()

    if request.method == "POST":
        vin = request.form.get("vin", "").strip().upper()
        if vin and len(vin) != 17:
            flash("VIN must contain exactly 17 characters.", "error")
            return render_template("vehicle_form.html", member=member, vehicle=None)

        vehicle = Vehicle(
            member_id=member.id,
            year=request.form.get("year", "").strip(),
            make=request.form.get("make", "").strip(),
            model=request.form.get("model", "").strip(),
            trim=request.form.get("trim", "").strip(),
            vin=vin,
            plate=request.form.get("plate", "").strip().upper(),
            color=request.form.get("color", "").strip(),
            current_mileage=request.form.get("current_mileage", "").strip(),
            notes=request.form.get("notes", "").strip(),
        )
        db.session.add(vehicle)
        db.session.commit()
        flash(f"{vehicle.display_name} added to {member.name}.", "success")
        return redirect(url_for("member_detail", member_id=member.member_id))

    return render_template("vehicle_form.html", member=member, vehicle=None)


@app.route("/members/<member_id>/vehicles/<int:vehicle_id>/edit", methods=["GET", "POST"])
@login_required
def edit_vehicle(member_id, vehicle_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    vehicle = Vehicle.query.filter_by(id=vehicle_id, member_id=member.id).first_or_404()

    if request.method == "POST":
        vin = request.form.get("vin", "").strip().upper()
        if vin and len(vin) != 17:
            flash("VIN must contain exactly 17 characters.", "error")
            return render_template("vehicle_form.html", member=member, vehicle=vehicle)

        vehicle.year = request.form.get("year", "").strip()
        vehicle.make = request.form.get("make", "").strip()
        vehicle.model = request.form.get("model", "").strip()
        vehicle.trim = request.form.get("trim", "").strip()
        vehicle.vin = vin
        vehicle.plate = request.form.get("plate", "").strip().upper()
        vehicle.color = request.form.get("color", "").strip()
        vehicle.current_mileage = request.form.get("current_mileage", "").strip()
        vehicle.notes = request.form.get("notes", "").strip()
        db.session.commit()
        flash("Vehicle information updated.", "success")
        return redirect(url_for("member_detail", member_id=member.member_id))

    return render_template("vehicle_form.html", member=member, vehicle=vehicle)


@app.route("/members/<member_id>/vehicles/<int:vehicle_id>/delete", methods=["POST"])
@login_required
def delete_vehicle(member_id, vehicle_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    vehicle = Vehicle.query.filter_by(id=vehicle_id, member_id=member.id).first_or_404()

    linked_services = Redemption.query.filter_by(vehicle_id=vehicle.id).count()
    if linked_services:
        flash("This vehicle cannot be deleted because it already has service history.", "error")
    else:
        db.session.delete(vehicle)
        db.session.commit()
        flash("Vehicle removed.", "success")

    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/history")
@login_required
def history():
    q = request.args.get("q", "").strip()
    query = Redemption.query.join(Member)

    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Member.member_id.ilike(like),
                Member.name.ilike(like),
                Member.email.ilike(like),
                Redemption.vehicle.ilike(like),
                Redemption.vin_last8.ilike(like),
                Redemption.mileage.ilike(like),
            )
        )

    redemptions = query.order_by(Redemption.redeemed_at.desc()).all()
    return render_template("history.html", redemptions=redemptions, q=q)


@app.route("/appointments")
@login_required
def appointments():
    status_filter = request.args.get("status", "").strip()
    date_filter = request.args.get("date", "").strip()

    query = Appointment.query.join(Member)
    if status_filter:
        query = query.filter(Appointment.status == status_filter)
    if date_filter:
        try:
            query = query.filter(Appointment.appointment_date == date.fromisoformat(date_filter))
        except ValueError:
            flash("Invalid date filter.", "error")

    appointment_list = query.order_by(
        Appointment.appointment_date.asc(),
        Appointment.appointment_time.asc(),
    ).all()

    return render_template(
        "appointments.html",
        appointments=appointment_list,
        status_filter=status_filter,
        date_filter=date_filter,
    )


@app.route("/appointments/<int:appointment_id>/status", methods=["POST"])
@login_required
def update_appointment_status(appointment_id):
    appointment = Appointment.query.get_or_404(appointment_id)
    new_status = request.form.get("status", "").strip()

    if new_status not in {"scheduled", "confirmed", "completed", "cancelled", "no_show"}:
        flash("Invalid appointment status.", "error")
        return redirect(url_for("appointments"))

    appointment.status = new_status
    appointment.internal_notes = request.form.get("internal_notes", appointment.internal_notes or "").strip()
    db.session.commit()

    if new_status == "confirmed":
        send_appointment_email(appointment, "Appointment Confirmed")
    elif new_status == "cancelled":
        send_appointment_email(appointment, "Appointment Cancelled")

    flash("Appointment updated.", "success")
    return redirect(request.referrer or url_for("appointments"))


@app.route("/m/<token>/appointments/new", methods=["GET", "POST"])
def public_new_appointment(token):
    member = Member.query.filter_by(token=token).first_or_404()
    vehicles = Vehicle.query.filter_by(member_id=member.id).order_by(Vehicle.created_at.desc()).all()

    selected_date_text = request.values.get("appointment_date", "").strip()
    selected_date = None
    available_slots = []

    if selected_date_text:
        try:
            selected_date = date.fromisoformat(selected_date_text)
            max_date = date.today() + timedelta(days=int(os.environ.get("APPOINTMENT_BOOKING_DAYS", "30")))
            if selected_date < date.today() or selected_date > max_date:
                flash("Please select a date within the available booking window.", "error")
                selected_date = None
            elif selected_date.weekday() == 6:
                flash("The service department is closed on Sundays.", "error")
            else:
                available_slots = appointment_slots_for_day(selected_date)
        except ValueError:
            flash("Please select a valid date.", "error")

    if request.method == "POST" and request.form.get("appointment_time"):
        if not selected_date:
            flash("Please select a valid appointment date.", "error")
            return redirect(url_for("public_new_appointment", token=member.token))

        appointment_time_text = request.form.get("appointment_time", "").strip()
        if appointment_time_text not in appointment_slots_for_day(selected_date):
            flash("That time is no longer available. Please choose another slot.", "error")
            return redirect(
                url_for(
                    "public_new_appointment",
                    token=member.token,
                    appointment_date=selected_date.isoformat(),
                )
            )

        selected_vehicle = None
        vehicle_id = request.form.get("vehicle_id", "").strip()
        if vehicle_id.isdigit():
            selected_vehicle = Vehicle.query.filter_by(
                id=int(vehicle_id), member_id=member.id
            ).first()

        appointment = Appointment(
            member_id=member.id,
            vehicle_id=selected_vehicle.id if selected_vehicle else None,
            appointment_date=selected_date,
            appointment_time=datetime.strptime(appointment_time_text, "%H:%M").time(),
            service_type=request.form.get("service_type", "Oil Change").strip() or "Oil Change",
            customer_notes=request.form.get("customer_notes", "").strip(),
            status="scheduled",
        )
        db.session.add(appointment)
        db.session.commit()
        send_appointment_email(appointment, "Appointment Scheduled")

        return redirect(
            url_for(
                "appointment_confirmation",
                token=member.token,
                appointment_id=appointment.id,
            )
        )

    max_date = date.today() + timedelta(days=int(os.environ.get("APPOINTMENT_BOOKING_DAYS", "30")))
    return render_template(
        "appointment_public_form.html",
        member=member,
        vehicles=vehicles,
        selected_date=selected_date,
        available_slots=available_slots,
        max_date=max_date,
    )


@app.route("/m/<token>/appointments/<int:appointment_id>/confirmation")
def appointment_confirmation(token, appointment_id):
    member = Member.query.filter_by(token=token).first_or_404()
    appointment = Appointment.query.filter_by(
        id=appointment_id, member_id=member.id
    ).first_or_404()
    return render_template(
        "appointment_confirmation.html",
        member=member,
        appointment=appointment,
    )


@app.route("/m/<token>/appointments/<int:appointment_id>/cancel", methods=["POST"])
def public_cancel_appointment(token, appointment_id):
    member = Member.query.filter_by(token=token).first_or_404()
    appointment = Appointment.query.filter_by(
        id=appointment_id, member_id=member.id
    ).first_or_404()

    if appointment.status in {"scheduled", "confirmed"} and appointment.starts_at > datetime.now():
        appointment.status = "cancelled"
        db.session.commit()
        send_appointment_email(appointment, "Appointment Cancelled")
        flash("Your appointment has been cancelled.", "success")
    else:
        flash("This appointment can no longer be cancelled online.", "error")

    return redirect(url_for("member_public", token=member.token))


@app.route("/scan")
@login_required
def scan_qr():
    return render_template("scan.html")


@app.route("/m/<token>")
def member_public(token):
    member = Member.query.filter_by(token=token).first_or_404()
    member.status = current_member_status(member)
    db.session.commit()
    redemptions = (
        Redemption.query.filter_by(member_id=member.id)
        .order_by(Redemption.redeemed_at.desc())
        .all()
    )
    vehicles = Vehicle.query.filter_by(member_id=member.id).order_by(Vehicle.created_at.desc()).all()
    upcoming_appointments = (
        Appointment.query.filter_by(member_id=member.id)
        .filter(
            Appointment.appointment_date >= date.today(),
            Appointment.status.in_(["scheduled", "confirmed"]),
        )
        .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc())
        .all()
    )
    return render_template(
        "member_public.html",
        member=member,
        vehicles=vehicles,
        redemptions=redemptions,
        upcoming_appointments=upcoming_appointments,
    )


@app.route("/members/<member_id>/qr")
@login_required
def member_qr(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    public_url = request.url_root.rstrip("/") + url_for(
        "member_public", token=member.token
    )
    image = qrcode.make(public_url)
    stream = BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    return send_file(
        stream,
        mimetype="image/png",
        download_name=f"{member.member_id}-qr.png",
    )


@app.route("/export/members.csv")
@login_required
def export_members():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Member ID",
            "Name",
            "Email",
            "Phone",
            "Purchase Date",
            "Expiration Date",
            "Total",
            "Remaining",
            "Status",
        ]
    )

    for member in Member.query.order_by(Member.created_at.desc()).all():
        writer.writerow(
            [
                member.member_id,
                member.name,
                member.email,
                member.phone,
                member.purchase_date,
                member.expiration_date,
                member.total_changes,
                member.remaining_changes,
                current_member_status(member),
            ]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=carnova-oil-club-members.csv"
        },
    )


@app.route("/export/history.csv")
@login_required
def export_history():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Service Date",
            "Member ID",
            "Customer",
            "Email",
            "Vehicle",
            "VIN Last 8",
            "Mileage",
            "Employee",
            "Notes",
        ]
    )

    for redemption in Redemption.query.order_by(
        Redemption.redeemed_at.desc()
    ).all():
        writer.writerow(
            [
                redemption.redeemed_at.strftime("%Y-%m-%d %H:%M"),
                redemption.member.member_id,
                redemption.member.name,
                redemption.member.email,
                redemption.vehicle or "",
                redemption.vin_last8 or "",
                redemption.mileage or "",
                redemption.employee or "",
                redemption.note or "",
            ]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=carnova-oil-club-history.csv"
        },
    )


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        return "Webhook secret not configured", 500

    try:
        event = stripe.Webhook.construct_event(
            request.data,
            request.headers.get("Stripe-Signature", ""),
            webhook_secret,
        )
    except Exception:
        return "Invalid webhook", 400

    if event["type"] == "checkout.session.completed":
        obj = event["data"]["object"]
        details = obj.get("customer_details") or {}
        shipping = obj.get("shipping_details") or {}
        email = details.get("email") or obj.get("customer_email")
        payment_id = obj.get("payment_intent") or obj.get("id")
        customer_name = (
            details.get("name")
            or shipping.get("name")
            or (obj.get("metadata") or {}).get("customer_name")
            or obj.get("customer_name")
        )
        customer_phone = details.get("phone") or shipping.get("phone") or ""

        stripe_secret = os.environ.get("STRIPE_SECRET_KEY")
        customer_id = obj.get("customer")
        if stripe_secret and customer_id and (not customer_name or not customer_phone):
            try:
                stripe.api_key = stripe_secret
                customer = stripe.Customer.retrieve(customer_id)
                customer_name = customer_name or customer.get("name")
                customer_phone = customer_phone or customer.get("phone") or ""
            except Exception:
                pass

        if email and not Member.query.filter_by(
            stripe_payment_id=payment_id
        ).first():
            member = Member(
                member_id=next_member_id(),
                name=customer_name or email.split("@")[0].replace(".", " ").title(),
                email=email.strip().lower(),
                phone=customer_phone,
                purchase_date=date.today(),
                expiration_date=date.today() + timedelta(days=365),
                total_changes=5,
                remaining_changes=5,
                stripe_payment_id=payment_id,
                price_paid_cents=int(obj.get("amount_total") or 22900),
                token=secrets.token_urlsafe(24),
            )
            db.session.add(member)
            db.session.commit()

    return "", 200


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
