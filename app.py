import csv
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timedelta, time, tzinfo
from functools import wraps
from io import BytesIO, StringIO
from pathlib import Path
from urllib import error as urllib_error, parse, request as urllib_request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.auth import jwt as google_jwt
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
import qrcode
import stripe
from flask import Flask, Response, flash, has_request_context, redirect, render_template, request, send_file, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text
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
    stripe_customer_id = db.Column(db.String(255), index=True)
    stripe_subscription_id = db.Column(db.String(255), unique=True, index=True)
    stripe_price_id = db.Column(db.String(255), index=True)
    plan_name = db.Column(db.String(100), nullable=False, default="Prepaid Package")
    subscription_status = db.Column(db.String(30))
    benefit_period_start = db.Column(db.Date)
    benefit_period_end = db.Column(db.Date)
    price_paid_cents = db.Column(db.Integer, nullable=False, default=22900)
    token = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    redemptions = db.relationship(
        "Redemption", backref="member", lazy=True, cascade="all, delete-orphan"
    )



class StripeEvent(db.Model):
    """Stores processed Stripe event IDs so webhook retries are idempotent."""
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    event_type = db.Column(db.String(100), nullable=False)
    processed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ReminderLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False, index=True)
    reminder_type = db.Column(db.String(50), nullable=False, index=True)
    reminder_key = db.Column(db.String(100), nullable=False, index=True)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("member_id", "reminder_type", "reminder_key", name="uq_reminder_member_type_key"),
    )

    member = db.relationship("Member", backref=db.backref("reminder_logs", lazy=True, cascade="all, delete-orphan"))


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
        member_column_definitions = {
            "price_paid_cents": "INTEGER DEFAULT 22900 NOT NULL",
            "stripe_customer_id": "VARCHAR(255)",
            "stripe_subscription_id": "VARCHAR(255)",
            "stripe_price_id": "VARCHAR(255)",
            "plan_name": "VARCHAR(100) DEFAULT 'Prepaid Package' NOT NULL",
            "subscription_status": "VARCHAR(30)",
            "benefit_period_start": "DATE",
            "benefit_period_end": "DATE",
        }
        for column_name, definition in member_column_definitions.items():
            if column_name not in member_columns:
                statements.append(
                    f"ALTER TABLE member ADD COLUMN {column_name} {definition}"
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


@app.route("/admin/reset-test-data", methods=["GET", "POST"])
@login_required
def reset_test_data():
    confirmation_required = "DELETE ALL CUSTOMER DATA"

    if request.method == "POST":
        confirmation_text = request.form.get("confirmation_text", "")
        if confirmation_text != confirmation_required:
            flash(f'Type {confirmation_required} exactly to confirm the reset.', "error")
            return render_template(
                "reset_test_data.html",
                confirmation_required=confirmation_required,
            )

        try:
            deleted_counts = {}
            db.session.rollback()
            with db.session.begin():
                deleted_counts["appointments"] = db.session.query(Appointment).delete(synchronize_session=False)
                deleted_counts["redemptions"] = db.session.query(Redemption).delete(synchronize_session=False)
                deleted_counts["reminder_logs"] = db.session.query(ReminderLog).delete(synchronize_session=False)
                deleted_counts["vehicles"] = db.session.query(Vehicle).delete(synchronize_session=False)
                deleted_counts["members"] = db.session.query(Member).delete(synchronize_session=False)
        except Exception as error:
            db.session.rollback()
            print("Reset test data failed:", error)
            flash("Could not reset test data right now.", "error")
            return render_template(
                "reset_test_data.html",
                confirmation_required=confirmation_required,
            )

        flash(
            "All customer data reset complete: "
            f'{deleted_counts["appointments"]} appointments, '
            f'{deleted_counts["redemptions"]} redemptions, '
            f'{deleted_counts["reminder_logs"]} reminder logs, '
            f'{deleted_counts["vehicles"]} vehicles, '
            f'{deleted_counts["members"]} members deleted.',
            "success",
        )
        return redirect(url_for("dashboard"))

    return render_template("reset_test_data.html", confirmation_required=confirmation_required)


def next_member_id():
    last = Member.query.order_by(Member.id.desc()).first()
    number = 1 if not last else last.id + 1
    return f"COC-{number:05d}"


def resolve_public_base_url():
    for env_name in ("BASE_URL", "APP_URL", "PUBLIC_URL", "RENDER_EXTERNAL_URL"):
        value = os.environ.get(env_name, "").strip().rstrip("/")
        if value:
            if "://" not in value:
                scheme = "http" if value.startswith(("localhost", "127.0.0.1", "[::1]")) else "https"
                value = f"{scheme}://{value}"
            return value
    if has_request_context():
        return request.url_root.rstrip("/")
    return ""


def member_public_url(member):
    return f"{resolve_public_base_url()}{url_for('member_public', token=member.token)}"


def apple_wallet_secret_paths():
    cert_path = os.environ.get("APPLE_PASS_CERT_PATH") or "/etc/secrets/apple_pass_cert.pem"
    key_path = os.environ.get("APPLE_PASS_KEY_PATH") or "/etc/secrets/apple_pass_key.pem"
    wwdr_path = os.environ.get("APPLE_PASS_WWDR_PATH") or "/etc/secrets/apple_wwdr.pem"
    return {
        "cert": cert_path,
        "key": key_path,
        "wwdr": wwdr_path,
    }


def apple_wallet_member_serial(member):
    source = f"{member.id}:{member.member_id}:{member.token}:{member.email}".encode("utf-8")
    digest = hashlib.sha256(source).hexdigest()
    return f"carnova-{digest[:24]}"


def apple_wallet_next_service_text(member):
    appointment = (
        Appointment.query.filter_by(member_id=member.id)
        .filter(
            Appointment.appointment_date >= date.today(),
            Appointment.status.in_(["scheduled", "confirmed"]),
        )
        .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc())
        .first()
    )
    if not appointment:
        return "No upcoming service"
    return (
        f"{appointment.appointment_date.strftime('%b %d, %Y')} "
        f"{appointment.appointment_time.strftime('%I:%M %p')}"
    )


def apple_wallet_payload(member):
    public_url = member_public_url(member)
    status_text = current_member_status(member).replace("_", " ").title()
    return {
        "formatVersion": 1,
        "passTypeIdentifier": os.environ.get("APPLE_PASS_TYPE_ID", ""),
        "serialNumber": apple_wallet_member_serial(member),
        "teamIdentifier": os.environ.get("APPLE_TEAM_ID", ""),
        "organizationName": "Carnova Oil Club",
        "description": "Carnova Oil Club Membership",
        "logoText": "Carnova Oil Club Premium",
        "foregroundColor": "rgb(255,255,255)",
        "backgroundColor": "rgb(19,16,12)",
        "labelColor": "rgb(230,190,95)",
        "barcode": {
            "format": "PKBarcodeFormatQR",
            "message": public_url,
            "messageEncoding": "iso-8859-1",
        },
        "generic": {
            "primaryFields": [
                {"key": "member_name", "label": "Member", "value": member.name},
            ],
            "secondaryFields": [
                {"key": "remaining_changes", "label": "Oil Changes Left", "value": str(member.remaining_changes)},
                {"key": "status", "label": "Membership Status", "value": status_text},
            ],
            "auxiliaryFields": [
                {"key": "next_service", "label": "Next Service", "value": apple_wallet_next_service_text(member)},
            ],
            "backFields": [
                {"key": "member_id", "label": "Member ID", "value": member.member_id},
                {"key": "plan_name", "label": "Plan", "value": member.plan_name or "Prepaid Package"},
                {"key": "expiration_date", "label": "Expires", "value": member.expiration_date.strftime("%B %d, %Y")},
                {"key": "public_url", "label": "Digital Card", "value": public_url},
            ],
        },
    }


def apple_wallet_create_image_asset(source_path, target_path, size):
    from PIL import Image

    image = Image.open(source_path).convert("RGBA")
    image = image.resize(size, Image.LANCZOS)
    image.save(target_path, format="PNG")


def apple_wallet_build_bundle(member):
    secret_paths = apple_wallet_secret_paths()
    missing = [path for path in secret_paths.values() if not os.path.exists(path)]
    if missing:
        raise FileNotFoundError("Apple Wallet signing files are not configured in the runtime environment.")

    base_dir = Path(tempfile.mkdtemp(prefix="apple-wallet-pass-"))
    source_logo = Path(__file__).resolve().parent / "static" / "carnova-wallet-logo-v2.png"
    if not source_logo.exists():
        source_logo = Path(__file__).resolve().parent / "static" / "carnova-logo.png"
    if not source_logo.exists():
        raise FileNotFoundError("Apple Wallet logo asset is missing from static assets.")

    apple_wallet_create_image_asset(source_logo, base_dir / "icon.png", (29, 29))
    apple_wallet_create_image_asset(source_logo, base_dir / "icon@2x.png", (58, 58))
    apple_wallet_create_image_asset(source_logo, base_dir / "logo.png", (160, 50))
    apple_wallet_create_image_asset(source_logo, base_dir / "logo@2x.png", (320, 100))

    pass_json = apple_wallet_payload(member)
    (base_dir / "pass.json").write_text(json.dumps(pass_json, indent=2), encoding="utf-8")

    manifest = {}
    for file_name in sorted(path.name for path in base_dir.iterdir() if path.is_file() and path.name not in {"manifest.json", "signature"}):
        file_path = base_dir / file_name
        manifest[file_name] = hashlib.sha1(file_path.read_bytes()).hexdigest()

    (base_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    signature_path = base_dir / "signature"
    subprocess.run(
        [
            "openssl",
            "cms",
            "-sign",
            "-binary",
            "-in",
            str(base_dir / "manifest.json"),
            "-signer",
            secret_paths["cert"],
            "-inkey",
            secret_paths["key"],
            "-certfile",
            secret_paths["wwdr"],
            "-outform",
            "DER",
            "-out",
            str(signature_path),
            "-md",
            "sha256",
        ],
        check=True,
        capture_output=True,
    )

    bundle_path = base_dir / f"{member.member_id}.pkpass"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for file_path in sorted(base_dir.iterdir()):
            if file_path == bundle_path:
                continue
            if file_path.name in {"manifest.json", "signature"}:
                bundle.write(file_path, arcname=file_path.name)
            elif file_path.is_file():
                bundle.write(file_path, arcname=file_path.name)

    return bundle_path


def monthly_membership_defaults(reference_date=None):
    today = reference_date or date.today()
    return {
        "plan_name": "Monthly Membership",
        "total_changes": 3,
        "remaining_changes": 3,
        "subscription_status": "active",
        "benefit_period_start": today,
        "benefit_period_end": today + timedelta(days=365),
        "expiration_date": today + timedelta(days=365),
    }


def normalize_plan_name(value):
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def is_monthly_membership(member):
    if not member:
        return False

    normalized_plan = normalize_plan_name(getattr(member, "plan_name", None))
    if normalized_plan in {"monthly membership", "carnova monthly membership"}:
        return True

    stripe_price_id = getattr(member, "stripe_price_id", None)
    if stripe_price_id in MONTHLY_PRICE_IDS:
        return True

    if getattr(member, "stripe_subscription_id", None):
        return True

    return False


def is_monthly_membership_customer(member):
    if not member:
        return False

    normalized_plan = normalize_plan_name(getattr(member, "plan_name", None))
    if normalized_plan in {"monthly membership", "carnova monthly membership"}:
        return True

    stripe_price_id = getattr(member, "stripe_price_id", None)
    if stripe_price_id in MONTHLY_PRICE_IDS:
        return True

    return False


def can_access_billing_portal(member):
    if not member:
        return False
    if not is_monthly_membership_customer(member):
        return False
    return bool(getattr(member, "stripe_customer_id", None))


def create_billing_portal_session(member, return_url):
    if not member:
        flash("That member could not be found.", "error")
        return None

    if not is_monthly_membership_customer(member):
        flash("The billing portal is only available for Monthly Membership customers.", "error")
        return None

    if not getattr(member, "stripe_customer_id", None):
        flash("This member is not connected to Stripe yet, so the billing portal is unavailable.", "error")
        return None

    stripe_secret = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe_secret:
        flash("Stripe is not configured for billing portal access right now.", "error")
        return None

    stripe.api_key = stripe_secret
    try:
        session = stripe.billing_portal.Session.create(
            customer=member.stripe_customer_id,
            return_url=return_url,
        )
    except stripe.error.StripeError as error:
        print("Stripe billing portal error:", error)
        flash("We couldn't open the billing portal right now. Please try again later.", "error")
        return None

    session_url = session.get("url")
    if not session_url or not session_url.startswith("https://billing.stripe.com/"):
        flash("We couldn't open the billing portal right now. Please try again later.", "error")
        return None

    return session_url


app.jinja_env.globals["is_monthly_membership"] = is_monthly_membership
app.jinja_env.globals["is_monthly_membership_customer"] = is_monthly_membership_customer
app.jinja_env.globals["can_access_billing_portal"] = can_access_billing_portal


def current_member_status(member):
    # Subscription payment state takes priority over ordinary package status.
    if member.stripe_subscription_id:
        if member.subscription_status in {"past_due", "unpaid", "incomplete", "incomplete_expired", "paused"}:
            return "past_due"
        if member.subscription_status in {"canceled", "cancelled"}:
            return "cancelled"

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
    smtp_user = os.environ.get("SMTP_USER")
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
    except Exception as e:
        print("EMAIL ERROR:", e)
        return False


def send_smtp_email(recipient, subject, text_body, html_body=None):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM_EMAIL", smtp_user or "")

    if not all([smtp_host, smtp_user, smtp_password, sender, recipient]):
        print("EMAIL ERROR: Missing SMTP configuration")
        return False

    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_host, port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(message)
        return True
    except Exception as error:
        print("EMAIL ERROR:", error)
        return False


def reminder_already_sent(member_id, reminder_type, reminder_key):
    return ReminderLog.query.filter_by(
        member_id=member_id,
        reminder_type=reminder_type,
        reminder_key=reminder_key,
    ).first() is not None


def remember_sent_reminder(member_id, reminder_type, reminder_key):
    db.session.add(
        ReminderLog(
            member_id=member_id,
            reminder_type=reminder_type,
            reminder_key=reminder_key,
        )
    )
    db.session.commit()


def send_renewal_reminder_email(member, days_until_expiration):
    with app.test_request_context("/"):
        public_card_url = member_public_url(member)
    expiration_text = member.expiration_date.strftime("%B %d, %Y")
    subject = f"Carnova Oil Club Renewal Reminder - {days_until_expiration} Day{'s' if days_until_expiration != 1 else ''} Left"
    text_body = f"""Hello {member.name},

Your Carnova Oil Club membership is nearing renewal.

Plan: {member.plan_name or 'Prepaid Package'}
Expiration Date: {expiration_text}
Remaining Oil Changes: {member.remaining_changes}

View your digital membership card:
{public_card_url}
"""
    html_body = f"""<!DOCTYPE html>
<html lang=\"en\">
  <body style=\"font-family:Arial,Helvetica,sans-serif;background:#f6f7f9;color:#101820;padding:20px;\">
    <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:600px;margin:auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;\">
      <tr><td style=\"padding:24px;\">
        <h1 style=\"margin:0 0 12px;font-size:24px;\">Membership Renewal Reminder</h1>
        <p style=\"margin:0 0 14px;\">Hello {member.name}, your membership is nearing renewal.</p>
        <p style=\"margin:0 0 8px;\"><strong>Plan:</strong> {member.plan_name or 'Prepaid Package'}</p>
        <p style=\"margin:0 0 8px;\"><strong>Expiration Date:</strong> {expiration_text}</p>
        <p style=\"margin:0 0 20px;\"><strong>Remaining Oil Changes:</strong> {member.remaining_changes}</p>
        <p style=\"margin:0;\"><a href=\"{public_card_url}\" style=\"display:inline-block;background:#087b78;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:700;\">View My Membership Card</a></p>
      </td></tr>
    </table>
  </body>
</html>"""
    return send_smtp_email(member.email, subject, text_body, html_body)


def member_primary_vehicle(member):
    return Vehicle.query.filter_by(member_id=member.id).order_by(Vehicle.created_at.desc()).first()


def send_unused_benefit_reminder_email(member):
        vehicle = member_primary_vehicle(member)
        has_vehicle = vehicle is not None

        with app.test_request_context("/"):
                appointment_link = f"{resolve_public_base_url()}{url_for('public_new_appointment', token=member.token)}"
                register_vehicle_link = f"{resolve_public_base_url()}{url_for('public_register_vehicle', token=member.token)}"

        if has_vehicle:
                headline = "Your Carnova Oil Club Benefits Are Waiting"
                vehicle_line = f"{vehicle.year} {vehicle.make} {vehicle.model}".strip()
                plate_line = vehicle.plate or "Not provided"
                button_label = "Schedule My Oil Change"
                action_url = appointment_link
                closing_text = "Don't let your membership benefits go unused. Schedule your next oil change today and keep your vehicle running at its best."
                vehicle_status_label = "Registered Vehicle"
                vehicle_status_value = vehicle_line
                vehicle_plate_label = "License Plate"
                vehicle_plate_value = plate_line
        else:
                headline = "Complete Your Membership Setup"
                button_label = "Register My Vehicle"
                action_url = register_vehicle_link
                closing_text = "Register your vehicle today so you can begin using your Carnova Oil Club benefits."
                vehicle_status_label = "Vehicle Status"
                vehicle_status_value = "Registration Required"
                vehicle_plate_label = "License Plate"
                vehicle_plate_value = "Registration Required"

        subject = "Use Your Carnova Oil Club Benefits"
        text_body = f"""Hello {member.name},

You still have unused oil change benefits waiting.

Remaining Oil Changes: {member.remaining_changes}
{vehicle_status_label}: {vehicle_status_value}
{vehicle_plate_label}: {vehicle_plate_value}

{button_label}:
{action_url}

{closing_text}

Carnova of Southborough
251 Turnpike Rd
Southborough, MA 01772
Phone: (978) 258-0029
Email: info@carnovaoil.com
"""
        html_body = f"""<!DOCTYPE html>
<html lang=\"en\">
    <body style=\"margin:0;padding:0;background:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#101820;\">
        <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"padding:18px 10px;background:#f4f6f8;\">
            <tr>
                <td align=\"center\">
                    <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:620px;background:#ffffff;border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;\">
                        <tr>
                            <td style=\"padding:20px 22px;background:#0f172a;border-bottom:3px solid #d5a836;\">
                                <p style=\"margin:0 0 8px;font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#9cc9c6;font-weight:700;\">Carnova Oil Club</p>
                                <h1 style=\"margin:0;font-size:24px;line-height:1.3;color:#ffffff;\">{headline}</h1>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:22px;\">
                                <p style=\"margin:0 0 16px;font-size:15px;line-height:1.6;\">Hello {member.name},</p>
                                <p style=\"margin:0 0 18px;font-size:14px;line-height:1.65;color:#334155;\">You still have valuable oil change benefits ready to use.</p>

                                <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:0 0 14px;border-collapse:separate;border-spacing:0 10px;\">
                                    <tr>
                                        <td style=\"background:#f8fafc;border:1px solid #e5e7eb;border-left:4px solid #d5a836;border-radius:10px;padding:12px 14px;\">
                                            <p style=\"margin:0 0 5px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;font-weight:700;\">Remaining Oil Changes</p>
                                            <p style=\"margin:0;font-size:22px;font-weight:800;color:#0f172a;\">{member.remaining_changes}</p>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td style=\"background:#f8fafc;border:1px solid #e5e7eb;border-left:4px solid #0ea5a2;border-radius:10px;padding:12px 14px;\">
                                            <p style=\"margin:0 0 5px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;font-weight:700;\">{vehicle_status_label}</p>
                                            <p style=\"margin:0;font-size:16px;font-weight:700;color:#0f172a;\">{vehicle_status_value}</p>
                                            <p style=\"margin:6px 0 0;font-size:13px;color:#334155;\"><strong>{vehicle_plate_label}:</strong> {vehicle_plate_value}</p>
                                        </td>
                                    </tr>
                                </table>

                                <table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:0 0 16px;\">
                                    <tr>
                                        <td style=\"border-radius:8px;background:#0ea5a2;\">
                                            <a href=\"{action_url}\" style=\"display:inline-block;padding:13px 20px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;\">{button_label}</a>
                                        </td>
                                    </tr>
                                </table>

                                <p style=\"margin:0;font-size:14px;line-height:1.65;color:#334155;\">{closing_text}</p>
                            </td>
                        </tr>
                        <tr>
                            <td style=\"padding:16px 22px;background:#f8fafc;border-top:1px solid #e5e7eb;\">
                                <p style=\"margin:0;font-size:13px;font-weight:700;color:#0f172a;\">Carnova of Southborough</p>
                                <p style=\"margin:6px 0 0;font-size:13px;line-height:1.6;color:#334155;\">251 Turnpike Rd<br>Southborough, MA 01772<br>Phone: (978) 258-0029<br>Email: info@carnovaoil.com</p>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
</html>"""
        return send_smtp_email(member.email, subject, text_body, html_body)


class EasternFallbackTz(tzinfo):
    """US Eastern fallback with DST rules when IANA zone data is unavailable."""

    std_offset = timedelta(hours=-5)
    dst_offset = timedelta(hours=-4)

    @staticmethod
    def _first_sunday_on_or_after(day):
        days_to_go = (6 - day.weekday()) % 7
        return day + timedelta(days=days_to_go)

    def _dst_bounds(self, year):
        march_8 = datetime(year, 3, 8)
        november_1 = datetime(year, 11, 1)
        dst_start = self._first_sunday_on_or_after(march_8).replace(hour=2)
        dst_end = self._first_sunday_on_or_after(november_1).replace(hour=2)
        return dst_start, dst_end

    def _is_dst(self, dt):
        if dt is None:
            return False
        naive = dt.replace(tzinfo=None)
        dst_start, dst_end = self._dst_bounds(naive.year)
        return dst_start <= naive < dst_end

    def utcoffset(self, dt):
        return self.dst_offset if self._is_dst(dt) else self.std_offset

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self._is_dst(dt) else "EST"


def resolve_appointment_reminder_timezone():
    default_tz_name = "America/New_York"
    configured_tz_name = os.environ.get("APPOINTMENT_REMINDER_TIMEZONE", default_tz_name).strip() or default_tz_name

    try:
        return ZoneInfo(configured_tz_name)
    except ZoneInfoNotFoundError:
        if configured_tz_name != default_tz_name:
            print(
                f"Invalid APPOINTMENT_REMINDER_TIMEZONE: {configured_tz_name}. "
                f"Falling back to {default_tz_name}."
            )

    for fallback_name in (default_tz_name, "US/Eastern", "EST5EDT"):
        try:
            return ZoneInfo(fallback_name)
        except ZoneInfoNotFoundError:
            continue

    print("Timezone data unavailable; using DST-aware Eastern fallback timezone.")
    return EasternFallbackTz()


def resolve_appointment_reminder_morning_hour():
    value = os.environ.get("APPOINTMENT_REMINDER_MORNING_HOUR", "8").strip()
    try:
        hour = int(value)
    except (TypeError, ValueError):
        hour = 8
    if hour < 0 or hour > 23:
        hour = 8
    return hour


def send_appointment_reminder_email(appointment):
    if not appointment or not appointment.member:
        return False

    member = appointment.member
    if not member.email:
        return False

    with app.test_request_context("/"):
        portal_url = member_public_url(member)
        schedule_url = f"{resolve_public_base_url()}{url_for('public_new_appointment', token=member.token)}"

    vehicle_name = appointment.vehicle.display_name if appointment.vehicle else "Vehicle not selected"
    appointment_date_text = appointment.appointment_date.strftime("%B %d, %Y")
    appointment_time_text = appointment.appointment_time.strftime("%I:%M %p")
    subject = "Appointment Reminder - Today"

    text_body = f"""Hello {member.name},

This is a reminder for your Carnova Oil Club appointment today.

Date: {appointment_date_text}
Time: {appointment_time_text}
Service: {appointment.service_type}
Vehicle: {vehicle_name}

View your customer portal:
{portal_url}

Schedule or reschedule service:
{schedule_url}
"""

    html_body = f"""<!DOCTYPE html>
<html lang=\"en\">
  <body style=\"font-family:Arial,Helvetica,sans-serif;background:#f6f7f9;color:#101820;padding:20px;\">
    <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:600px;margin:auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;\">
      <tr><td style=\"padding:24px;\">
        <h1 style=\"margin:0 0 12px;font-size:24px;\">Appointment Reminder</h1>
        <p style=\"margin:0 0 14px;\">Hello {member.name}, this is a reminder for your appointment today.</p>
        <p style=\"margin:0 0 8px;\"><strong>Date:</strong> {appointment_date_text}</p>
        <p style=\"margin:0 0 8px;\"><strong>Time:</strong> {appointment_time_text}</p>
        <p style=\"margin:0 0 8px;\"><strong>Service:</strong> {appointment.service_type}</p>
        <p style=\"margin:0 0 20px;\"><strong>Vehicle:</strong> {vehicle_name}</p>
        <p style=\"margin:0 0 12px;\"><a href=\"{portal_url}\" style=\"display:inline-block;background:#087b78;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:700;\">Open Customer Portal</a></p>
        <p style=\"margin:0;\"><a href=\"{schedule_url}\" style=\"display:inline-block;background:#0f172a;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:700;\">Schedule or Reschedule</a></p>
      </td></tr>
    </table>
  </body>
</html>"""

    return send_smtp_email(member.email, subject, text_body, html_body)


def run_appointment_reminders(reference_datetime=None):
    reminder_tz = resolve_appointment_reminder_timezone()
    morning_hour = resolve_appointment_reminder_morning_hour()

    if reference_datetime is None:
        now_local = datetime.now(reminder_tz)
    elif reference_datetime.tzinfo is None:
        now_local = reference_datetime.replace(tzinfo=reminder_tz)
    else:
        now_local = reference_datetime.astimezone(reminder_tz)

    today = now_local.date()
    morning_cutoff = datetime.combine(today, time(morning_hour, 0), tzinfo=reminder_tz)

    summary = {
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "skip_reasons": {
            "status_not_eligible": 0,
            "before_morning_send_time": 0,
            "missing_email": 0,
            "duplicate_reminder": 0,
        },
        "sent_details": [],
        "failed_details": [],
    }

    def mark_skip(reason):
        summary["skipped"] += 1
        summary["skip_reasons"][reason] += 1

    appointments = Appointment.query.filter_by(appointment_date=today).all()

    for appointment in appointments:
        if appointment.status not in {"scheduled", "confirmed"}:
            mark_skip("status_not_eligible")
            continue

        if now_local < morning_cutoff:
            mark_skip("before_morning_send_time")
            continue

        member = appointment.member
        if not member or not member.email:
            mark_skip("missing_email")
            continue

        reminder_key = f"appointment:{appointment.id}:morning:{today.isoformat()}"
        if reminder_already_sent(member.id, "appointment_morning", reminder_key):
            mark_skip("duplicate_reminder")
            continue

        failure_message = None
        try:
            email_sent = send_appointment_reminder_email(appointment)
        except Exception as error:
            email_sent = False
            failure_message = str(error)

        if email_sent:
            try:
                remember_sent_reminder(member.id, "appointment_morning", reminder_key)
            except Exception as error:
                db.session.rollback()
                summary["failed"] += 1
                summary["failed_details"].append(
                    {
                        "member_name": member.name,
                        "reminder_type": "appointment_morning",
                        "email": member.email,
                        "status": "failed",
                        "error": f"Could not record reminder log: {error}",
                    }
                )
                print("REMINDER FAILED", member.member_id)
                continue

            summary["sent"] += 1
            summary["sent_details"].append(
                {
                    "member_name": member.name,
                    "reminder_type": "appointment_morning",
                    "email": member.email,
                    "status": "sent",
                }
            )
            print("APPOINTMENT REMINDER SENT", member.member_id)
        else:
            summary["failed"] += 1
            summary["failed_details"].append(
                {
                    "member_name": member.name,
                    "reminder_type": "appointment_morning",
                    "email": member.email,
                    "status": "failed",
                    "error": failure_message or "Email send returned False",
                }
            )
            print("REMINDER FAILED", member.member_id)

    return summary


def run_renewal_reminders(reference_date=None):
    today = reference_date or date.today()
    summary = {
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "skip_reasons": {
            "outside_reminder_window": 0,
            "inactive_member": 0,
            "missing_expiration_date": 0,
            "duplicate_reminder": 0,
            "missing_email": 0,
        },
        "sent_details": [],
        "failed_details": [],
    }
    renewal_offsets = {30, 7, 1}

    def mark_skip(reason):
        summary["skipped"] += 1
        summary["skip_reasons"][reason] += 1

    for member in Member.query.all():
        if member.status != "active":
            mark_skip("inactive_member")
            continue

        if not member.expiration_date:
            mark_skip("missing_expiration_date")
            continue

        days_until_expiration = (member.expiration_date - today).days
        if days_until_expiration not in renewal_offsets:
            mark_skip("outside_reminder_window")
            continue

        if not member.email:
            mark_skip("missing_email")
            continue

        reminder_key = f"renewal:{member.expiration_date.isoformat()}:{days_until_expiration}"
        if reminder_already_sent(member.id, "renewal", reminder_key):
            mark_skip("duplicate_reminder")
            continue

        failure_message = None
        try:
            email_sent = send_renewal_reminder_email(member, days_until_expiration)
        except Exception as error:
            email_sent = False
            failure_message = str(error)

        if email_sent:
            try:
                remember_sent_reminder(member.id, "renewal", reminder_key)
            except Exception as error:
                db.session.rollback()
                summary["failed"] += 1
                summary["failed_details"].append(
                    {
                        "member_name": member.name,
                        "reminder_type": "renewal",
                        "email": member.email,
                        "status": "failed",
                        "error": f"Could not record reminder log: {error}",
                    }
                )
                print("REMINDER FAILED", member.member_id)
                continue

            summary["sent"] += 1
            summary["sent_details"].append(
                {
                    "member_name": member.name,
                    "reminder_type": "renewal",
                    "email": member.email,
                    "status": "sent",
                }
            )
            print("RENEWAL REMINDER SENT", member.member_id)
        else:
            summary["failed"] += 1
            summary["failed_details"].append(
                {
                    "member_name": member.name,
                    "reminder_type": "renewal",
                    "email": member.email,
                    "status": "failed",
                    "error": failure_message or "Email send returned False",
                }
            )
            print("REMINDER FAILED", member.member_id)

    return summary


def run_unused_benefit_reminders(reference_date=None):
    today = reference_date or date.today()
    summary = {
        "sent": 0,
        "skipped": 0,
        "failed": 0,
        "skip_reasons": {
            "inactive_member": 0,
            "zero_remaining_changes": 0,
            "used_within_last_120_days": 0,
            "reminder_sent_within_90_days": 0,
            "missing_email": 0,
        },
        "sent_details": [],
        "failed_details": [],
    }
    inactive_cutoff = datetime.combine(today - timedelta(days=120), time.max)
    resend_cutoff = datetime.combine(today - timedelta(days=90), time.min)

    def mark_skip(reason):
        summary["skipped"] += 1
        summary["skip_reasons"][reason] += 1

    for member in Member.query.all():
        if member.status != "active":
            mark_skip("inactive_member")
            continue

        if member.remaining_changes <= 0:
            mark_skip("zero_remaining_changes")
            continue

        if not member.email:
            mark_skip("missing_email")
            continue

        latest_redemption = (
            Redemption.query.filter_by(member_id=member.id)
            .order_by(Redemption.redeemed_at.desc())
            .first()
        )

        if latest_redemption and latest_redemption.redeemed_at > inactive_cutoff:
            mark_skip("used_within_last_120_days")
            continue

        recent_unused = (
            ReminderLog.query.filter_by(member_id=member.id, reminder_type="unused_benefit")
            .filter(ReminderLog.sent_at >= resend_cutoff)
            .first()
        )
        if recent_unused:
            mark_skip("reminder_sent_within_90_days")
            continue

        reminder_key = f"unused-benefit:{today.toordinal() // 90}"
        if reminder_already_sent(member.id, "unused_benefit", reminder_key):
            mark_skip("reminder_sent_within_90_days")
            continue

        failure_message = None
        try:
            email_sent = send_unused_benefit_reminder_email(member)
        except Exception as error:
            email_sent = False
            failure_message = str(error)

        if email_sent:
            try:
                remember_sent_reminder(member.id, "unused_benefit", reminder_key)
            except Exception as error:
                db.session.rollback()
                summary["failed"] += 1
                summary["failed_details"].append(
                    {
                        "member_name": member.name,
                        "reminder_type": "unused_benefit",
                        "email": member.email,
                        "status": "failed",
                        "error": f"Could not record reminder log: {error}",
                    }
                )
                print("REMINDER FAILED", member.member_id)
                continue

            summary["sent"] += 1
            summary["sent_details"].append(
                {
                    "member_name": member.name,
                    "reminder_type": "unused_benefit",
                    "email": member.email,
                    "status": "sent",
                }
            )
            print("UNUSED BENEFIT REMINDER SENT", member.member_id)
        else:
            summary["failed"] += 1
            summary["failed_details"].append(
                {
                    "member_name": member.name,
                    "reminder_type": "unused_benefit",
                    "email": member.email,
                    "status": "failed",
                    "error": failure_message or "Email send returned False",
                }
            )
            print("REMINDER FAILED", member.member_id)

    return summary


def run_all_reminders(reference_date=None, reference_datetime=None):
    refresh_member_statuses()
    renewal_summary = run_renewal_reminders(reference_date=reference_date)
    unused_summary = run_unused_benefit_reminders(reference_date=reference_date)
    appointment_summary = run_appointment_reminders(reference_datetime=reference_datetime)
    return {
        "sent": renewal_summary["sent"] + unused_summary["sent"] + appointment_summary["sent"],
        "skipped": renewal_summary["skipped"] + unused_summary["skipped"] + appointment_summary["skipped"],
        "failed": renewal_summary["failed"] + unused_summary["failed"] + appointment_summary["failed"],
        "renewal": renewal_summary,
        "unused_benefit": unused_summary,
        "appointment_morning": appointment_summary,
        "sent_details": renewal_summary["sent_details"] + unused_summary["sent_details"] + appointment_summary["sent_details"],
        "failed_details": renewal_summary["failed_details"] + unused_summary["failed_details"] + appointment_summary["failed_details"],
    }


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
    today = date.today()

    def add_months(base_date, month_delta):
        month_index = (base_date.year * 12 + (base_date.month - 1)) + month_delta
        year = month_index // 12
        month = month_index % 12 + 1
        return date(year, month, 1)

    month_start = date(today.year, today.month, 1)
    next_month_start = add_months(month_start, 1)

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
    expiring_cutoff = today + timedelta(days=30)

    total_revenue_cents = sum((member.price_paid_cents or 0) for member in all_members)
    estimated_service_cost_cents = stats_cost = int(os.environ.get("ESTIMATED_COST_PER_CHANGE_CENTS", "6500"))
    outstanding_cost_cents = sum(member.remaining_changes for member in all_members) * estimated_service_cost_cents

    monthly_revenue_cents = sum(
        (member.price_paid_cents or 0)
        for member in Member.query.filter(
            Member.purchase_date >= month_start,
            Member.purchase_date < next_month_start,
        ).all()
    )
    monthly_oil_changes = Redemption.query.filter(
        Redemption.redeemed_at >= datetime.combine(month_start, time.min),
        Redemption.redeemed_at < datetime.combine(next_month_start, time.min),
    ).count()

    upcoming_renewals = (
        Member.query.filter(
            Member.status == "active",
            Member.expiration_date >= today,
            Member.expiration_date <= expiring_cutoff,
        )
        .order_by(Member.expiration_date.asc())
        .limit(10)
        .all()
    )

    recent_members = Member.query.order_by(Member.created_at.desc()).limit(10).all()

    first_chart_month = add_months(month_start, -11)
    chart_months = [add_months(first_chart_month, i) for i in range(12)]
    chart_labels = [month.strftime("%b %Y") for month in chart_months]
    chart_keys = [(month.year, month.month) for month in chart_months]

    revenue_by_month = {key: 0 for key in chart_keys}
    new_members_by_month = {key: 0 for key in chart_keys}

    members_for_charts = Member.query.filter(
        Member.purchase_date >= first_chart_month,
        Member.purchase_date < next_month_start,
    ).all()
    for member in members_for_charts:
        key = (member.purchase_date.year, member.purchase_date.month)
        if key in revenue_by_month:
            revenue_by_month[key] += member.price_paid_cents or 0
            new_members_by_month[key] += 1

    monthly_revenue_chart = [round(revenue_by_month[key] / 100, 2) for key in chart_keys]
    monthly_new_members_chart = [new_members_by_month[key] for key in chart_keys]

    stats = {
        "total_members": len(all_members),
        "active_members": Member.query.filter_by(status="active").count(),
        "monthly_revenue": monthly_revenue_cents / 100,
        "monthly_oil_changes": monthly_oil_changes,
        "upcoming_renewals": len(upcoming_renewals),
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
        Redemption.query.order_by(Redemption.redeemed_at.desc()).limit(10).all()
    )

    return render_template(
        "dashboard.html",
        members=members,
        stats=stats,
        q=q,
        status_filter=status_filter,
        recent_redemptions=recent_redemptions,
        recent_members=recent_members,
        upcoming_renewals=upcoming_renewals,
        chart_labels=chart_labels,
        monthly_revenue_chart=monthly_revenue_chart,
        monthly_new_members_chart=monthly_new_members_chart,
        upcoming_appointments=upcoming_appointments,
    )


@app.route("/admin/run-reminders", methods=["POST"])
@login_required
def run_reminders_now():
    summary = run_all_reminders()
    return render_template("reminder_summary.html", summary=summary)


@app.route("/members/new", methods=["GET", "POST"])
@login_required
def new_member():
    if request.method == "POST":
        try:
            purchase = date.fromisoformat(
                request.form.get("purchase_date") or date.today().isoformat()
            )
            expiration_text = request.form.get("expiration_date")
            membership_plan = request.form.get("membership_plan", "").strip()
            today = date.today()

            if membership_plan == "monthly_membership":
                defaults = monthly_membership_defaults(today)
                expiration = defaults["expiration_date"]
                total = defaults["total_changes"]
                plan_name = defaults["plan_name"]
                subscription_status = defaults["subscription_status"]
                benefit_period_start = defaults["benefit_period_start"]
                benefit_period_end = defaults["benefit_period_end"]
            else:
                expiration = (
                    date.fromisoformat(expiration_text)
                    if expiration_text
                    else purchase + timedelta(days=365)
                )
                total = max(1, int(request.form.get("total_changes", 5)))
                plan_name = "Prepaid Package"
                subscription_status = None
                benefit_period_start = None
                benefit_period_end = None
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
            status="active",
            plan_name=plan_name,
            subscription_status=subscription_status,
            benefit_period_start=benefit_period_start,
            benefit_period_end=benefit_period_end,
            token=secrets.token_urlsafe(24),
        )
        db.session.add(member)
        db.session.commit()
        flash(f"Member {member.member_id} created.", "success")
        return redirect(url_for("member_detail", member_id=member.member_id))

    return render_template("member_form.html")


@app.route("/members/<member_id>/billing/portal", methods=["POST"])
@login_required
def member_billing_portal(member_id):
    member = Member.query.filter_by(member_id=member_id).first()
    if not member:
        flash("That member could not be found.", "error")
        return redirect(url_for("dashboard"))

    return_url = request.url_root.rstrip("/") + url_for("member_detail", member_id=member.member_id)
    session_url = create_billing_portal_session(member, return_url)
    if session_url:
        return redirect(session_url)
    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/m/<token>/billing/portal", methods=["POST"])
def public_member_billing_portal(token):
    member = Member.query.filter_by(token=token).first()
    if not member:
        flash("That member could not be found.", "error")
        return redirect(url_for("login"))

    return_url = member_public_url(member)
    session_url = create_billing_portal_session(member, return_url)
    if session_url:
        return redirect(session_url)
    return redirect(url_for("member_public", token=member.token))


@app.route("/m/<token>/wallet/add", methods=["POST"])
def public_member_google_wallet_add(token):
    member = Member.query.filter_by(token=token).first_or_404()
    save_url = sync_member_google_wallet_save_url(member)
    if google_wallet_save_url_is_safe(save_url):
        return redirect(save_url)

    if save_url:
        print(f"Google Wallet save URL validation failed for {member.member_id}")

    flash("Google Wallet is unavailable right now. Please try again later.", "error")
    return redirect(url_for("member_public", token=member.token))


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
    public_url = member_public_url(member)
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
            old_wallet_values = {
                "name": member.name,
                "plan_name": member.plan_name,
                "status": member.status,
                "expiration_date": member.expiration_date,
                "total_changes": member.total_changes,
                "remaining_changes": member.remaining_changes,
            }

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

            new_wallet_values = {
                "name": member.name,
                "plan_name": member.plan_name,
                "status": member.status,
                "expiration_date": member.expiration_date,
                "total_changes": member.total_changes,
                "remaining_changes": member.remaining_changes,
            }
            if old_wallet_values != new_wallet_values:
                sync_member_google_wallet_object(member)
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
    elif member.plan_name == "Monthly Membership" and not Vehicle.query.filter_by(member_id=member.id).first():
        flash("This monthly membership requires at least one registered vehicle before redeeming an oil change.", "error")
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
        sync_member_google_wallet_object(member)
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
        sync_member_google_wallet_object(member)
        flash("Last redemption was undone.", "success")
    else:
        flash("No redemption to undo.", "error")

    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/members/<member_id>/vehicles/new", methods=["GET", "POST"])
@login_required
def new_vehicle(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()

    if request.method == "POST":
        if is_monthly_membership(member):
            existing_vehicle_count = Vehicle.query.filter_by(member_id=member.id).count()
            if existing_vehicle_count >= 1:
                flash("Monthly Membership allows only one registered vehicle.", "error")
                return redirect(url_for("member_detail", member_id=member.member_id))

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
    sync_member_google_wallet_object(appointment.member)

    if new_status == "confirmed":
        send_appointment_email(appointment, "Appointment Confirmed")
    elif new_status == "cancelled":
        send_appointment_email(appointment, "Appointment Cancelled")

    flash("Appointment updated.", "success")
    return redirect(request.referrer or url_for("appointments"))


@app.route("/m/<token>/appointments/new", methods=["GET", "POST"])
def public_new_appointment(token):
    member = Member.query.filter_by(token=token).first_or_404()
    member.status = current_member_status(member)
    if member.status != "active":
        flash("This membership is not active and cannot schedule service.", "error")
        return redirect(url_for("member_public", token=member.token))

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

        vehicle_id = request.form.get("vehicle_id", "").strip()
        selected_vehicle = None
        if vehicle_id.isdigit():
            selected_vehicle = Vehicle.query.filter_by(
                id=int(vehicle_id), member_id=member.id
            ).first()
        if not selected_vehicle:
            flash("Please select one of your registered vehicles.", "error")
            return redirect(
                url_for(
                    "public_new_appointment",
                    token=member.token,
                    appointment_date=selected_date.isoformat(),
                )
            )

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
        sync_member_google_wallet_object(member)
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


@app.route("/m/<token>/vehicle/register", methods=["GET", "POST"])
def public_register_vehicle(token):
    member = Member.query.filter_by(token=token).first_or_404()
    appointment_path = url_for("public_new_appointment", token=member.token)
    return_to = request.values.get("return_to", "").strip()
    try:
        parsed_return_to = parse.urlsplit(return_to)
    except ValueError:
        parsed_return_to = None
    query_values = parse.parse_qs(parsed_return_to.query) if parsed_return_to else {}
    appointment_dates = query_values.get("appointment_date", [])
    if (
        not parsed_return_to
        or parsed_return_to.scheme
        or parsed_return_to.netloc
        or parsed_return_to.fragment
        or parsed_return_to.path != appointment_path
        or set(query_values) - {"appointment_date"}
        or len(appointment_dates) > 1
    ):
        return_to = ""
    elif appointment_dates:
        try:
            parsed_date = date.fromisoformat(appointment_dates[0])
        except ValueError:
            return_to = ""
        else:
            return_to = f"{appointment_path}?{parse.urlencode({'appointment_date': parsed_date.isoformat()})}"
    else:
        return_to = appointment_path

    if is_monthly_membership(member) and Vehicle.query.filter_by(member_id=member.id).first():
        flash("Monthly Membership allows only one registered vehicle.", "error")
        return redirect(return_to or url_for("member_public", token=member.token))

    form_values = {
        "year": "",
        "make": "",
        "model": "",
        "color": "",
        "plate": "",
        "vin_last8": "",
        "current_mileage": "",
    }

    if request.method == "POST":
        form_values = {
            "year": request.form.get("year", "").strip(),
            "make": request.form.get("make", "").strip(),
            "model": request.form.get("model", "").strip(),
            "color": request.form.get("color", "").strip(),
            "plate": request.form.get("plate", "").strip().upper(),
            "vin_last8": request.form.get("vin_last8", "").strip().upper(),
            "current_mileage": request.form.get("current_mileage", "").strip(),
        }

        year = form_values["year"]
        make = form_values["make"]
        model = form_values["model"]
        color = form_values["color"]
        plate = form_values["plate"]
        vin_last8 = form_values["vin_last8"]
        current_mileage = form_values["current_mileage"]

        if not all([year, make, model, color, plate, vin_last8, current_mileage]):
            flash("Please complete every vehicle registration field.", "error")
        elif not (year.isdigit() and len(year) == 4):
            flash("Year must be a 4-digit number.", "error")
        elif len(vin_last8) != 8 or not vin_last8.isalnum():
            flash("Last 8 VIN digits must be exactly 8 letters or numbers.", "error")
        else:
            vehicle = Vehicle(
                member_id=member.id,
                year=year,
                make=make,
                model=model,
                color=color,
                plate=plate,
                vin=vin_last8,
                current_mileage=current_mileage,
            )
            db.session.add(vehicle)
            db.session.commit()
            flash("Vehicle registered successfully.", "success")
            if return_to:
                return redirect(return_to)
            return redirect(url_for("member_public", token=member.token))

    return render_template(
        "public_vehicle_register.html",
        member=member,
        form_values=form_values,
        return_to=return_to,
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
        sync_member_google_wallet_object(member)
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
    vehicles = Vehicle.query.filter_by(member_id=member.id).order_by(Vehicle.created_at.desc()).all()
    has_vehicle = bool(vehicles)
    primary_vehicle = vehicles[0] if vehicles else None
    redemptions = (
        Redemption.query.filter_by(member_id=member.id)
        .order_by(Redemption.redeemed_at.desc())
        .all()
    )
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
        has_vehicle=has_vehicle,
        primary_vehicle=primary_vehicle,
        redemptions=redemptions,
        upcoming_appointments=upcoming_appointments,
    )


@app.route("/m/<token>/apple-wallet")
def member_apple_wallet(token):
    member = Member.query.filter_by(token=token).first_or_404()
    try:
        bundle_path = apple_wallet_build_bundle(member)
    except FileNotFoundError:
        return "Apple Wallet is not configured for this environment.", 503
    except subprocess.CalledProcessError:
        return "Apple Wallet pass signing failed.", 500

    return send_file(
        bundle_path,
        mimetype="application/vnd.apple.pkpass",
        as_attachment=True,
        download_name=f"{member.member_id}-membership.pkpass",
    )


@app.route("/members/<member_id>/qr")
@login_required
def member_qr(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    public_url = member_public_url(member)
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

def send_membership_confirmation_email(member):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM_EMAIL", smtp_user or "")

    if not all([smtp_host, smtp_user, smtp_password, sender, member.email]):
        print("EMAIL ERROR: Missing SMTP configuration")
        return False

    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "Welcome to Carnova Oil Club"
    message["From"] = sender
    message["To"] = member.email
    public_card_url = member_public_url(member)
    expiration_text = member.expiration_date.strftime('%B %d, %Y')

    message.set_content(f"""Hello {member.name},

Thank you for joining Carnova Oil Club.

Membership Details
- Customer Name: {member.name}
- Membership ID: {member.member_id}
- Plan Name: {member.plan_name}
- Total Oil Changes: {member.total_changes}
- Remaining Oil Changes: {member.remaining_changes}
- Expiration Date: {expiration_text}

View your digital membership card:
{public_card_url}

On your membership page, you can view your membership status, remaining oil changes, registered vehicle, expiration date, QR code, and service history.

Please save this email and add your membership page to your phone home screen for quick access.

Carnova of Southborough
251 Turnpike Rd
Southborough, MA 01772
Phone: (978) 258-0029
""")

    message.add_alternative(
    f"""<!DOCTYPE html>
<html lang=\"en\">
<body style=\"margin:0;padding:0;background-color:#f4f6f8;font-family:Arial,Helvetica,sans-serif;color:#1f2937;\">
    <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"background-color:#f4f6f8;padding:20px 12px;\">
        <tr>
            <td align=\"center\">
                <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"max-width:600px;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;\">
                    <tr>
                        <td style=\"background:#0f172a;color:#ffffff;padding:22px 24px;\">
                            <h1 style=\"margin:0;font-size:22px;line-height:1.3;font-weight:700;\">Welcome to Carnova Oil Club</h1>
                            <p style=\"margin:8px 0 0;font-size:14px;line-height:1.5;color:#cbd5e1;\">Your membership is active and ready to use.</p>
                        </td>
                    </tr>
                    <tr>
                        <td style=\"padding:24px;\">
                            <p style=\"margin:0 0 16px;font-size:15px;line-height:1.6;\">Hello {member.name},</p>
                            <p style=\"margin:0 0 18px;font-size:15px;line-height:1.6;\">Thank you for joining Carnova Oil Club. Here are your membership details:</p>

                            <table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;margin:0 0 20px;\">
                                <tr>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;font-weight:600;width:45%;\">Customer Name</td>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;\">{member.name}</td>
                                </tr>
                                <tr>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;font-weight:600;\">Membership ID</td>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;\">{member.member_id}</td>
                                </tr>
                                <tr>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;font-weight:600;\">Plan Name</td>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;\">{member.plan_name}</td>
                                </tr>
                                <tr>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;font-weight:600;\">Total Oil Changes</td>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;\">{member.total_changes}</td>
                                </tr>
                                <tr>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;font-weight:600;\">Remaining Oil Changes</td>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;\">{member.remaining_changes}</td>
                                </tr>
                                <tr>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;font-weight:600;\">Expiration Date</td>
                                    <td style=\"padding:10px 0;border-bottom:1px solid #e5e7eb;font-size:14px;\">{expiration_text}</td>
                                </tr>
                            </table>

                            <table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" style=\"margin:0 0 18px;\">
                                <tr>
                                    <td align=\"center\" style=\"border-radius:8px;background:#0ea5e9;\">
                                        <a href=\"{public_card_url}\" style=\"display:inline-block;padding:14px 22px;font-size:15px;font-weight:700;color:#ffffff;text-decoration:none;\">View My Digital Membership Card</a>
                                    </td>
                                </tr>
                            </table>

                            <p style=\"margin:0 0 10px;font-size:14px;line-height:1.7;\">Your membership page lets you view your membership status, remaining oil changes, registered vehicle, expiration date, QR code, and service history.</p>
                            <p style=\"margin:0 0 20px;font-size:14px;line-height:1.7;\">Please save this email and add your membership page to your phone home screen for quick access anytime.</p>
                        </td>
                    </tr>
                    <tr>
                        <td style=\"padding:18px 24px;background:#f8fafc;border-top:1px solid #e5e7eb;\">
                            <p style=\"margin:0;font-size:14px;font-weight:700;color:#111827;\">Carnova of Southborough</p>
                            <p style=\"margin:6px 0 0;font-size:13px;line-height:1.6;color:#4b5563;\">251 Turnpike Rd<br>Southborough, MA 01772<br>Phone: (978) 258-0029</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>""",
        subtype="html",
    )

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(smtp_host, port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(message)
        print("MEMBERSHIP EMAIL SENT")
        return True

    except Exception as e:
        print("EMAIL ERROR:", e)
        return False


def member_public_url(member):
    return f"{resolve_public_base_url()}{url_for('member_public', token=member.token)}"


GOOGLE_WALLET_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
GOOGLE_WALLET_ENSURED_CLASS_IDS = set()


def google_wallet_is_configured():
    required_env = [
        "GOOGLE_WALLET_ISSUER_ID",
        "GOOGLE_WALLET_CLASS_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]
    for env_name in required_env:
        if not os.environ.get(env_name, "").strip():
            return False
    return True


def google_wallet_class_id():
    issuer_id = os.environ.get("GOOGLE_WALLET_ISSUER_ID", "").strip()
    class_id = os.environ.get("GOOGLE_WALLET_CLASS_ID", "").strip()
    if not class_id:
        return ""
    if "." in class_id:
        return class_id
    if not issuer_id:
        return ""
    return f"{issuer_id}.{class_id}"


def google_wallet_object_id(member):
    issuer_id = os.environ.get("GOOGLE_WALLET_ISSUER_ID", "").strip()
    safe_member_id = re.sub(r"[^a-zA-Z0-9._-]", "_", (member.member_id or "").lower())
    return f"{issuer_id}.carnova_{safe_member_id}"


def google_wallet_member_state(member):
    status_value = current_member_status(member)
    if status_value == "active":
        return "ACTIVE"
    if status_value == "expired":
        return "EXPIRED"
    return "INACTIVE"


def google_wallet_public_https_url(path):
    base_url = resolve_public_base_url()
    if not base_url:
        return ""

    parsed_base = parse.urlsplit(base_url)
    if parsed_base.scheme != "https" or not parsed_base.netloc:
        return ""

    parsed_target = parse.urlsplit(path or "")
    if parsed_target.scheme or parsed_target.netloc:
        return ""

    base_path = parsed_base.path.rstrip("/")
    target_path = "/" + (parsed_target.path or "").lstrip("/")

    if base_path and (target_path == base_path or target_path.startswith(f"{base_path}/")):
        final_path = target_path
    elif base_path:
        final_path = f"{base_path}{target_path}"
    else:
        final_path = target_path

    return parse.urlunsplit(("https", parsed_base.netloc, final_path, parsed_target.query, parsed_target.fragment))


def google_wallet_remaining_changes_text(remaining_changes):
    noun = "OIL CHANGE" if remaining_changes == 1 else "OIL CHANGES"
    return f"{remaining_changes} {noun} REMAINING"


def google_wallet_next_service_text(member):
    appointment = (
        Appointment.query.filter_by(member_id=member.id)
        .filter(
            db.or_(
                Appointment.appointment_date > date.today(),
                db.and_(
                    Appointment.appointment_date == date.today(),
                    Appointment.appointment_time >= datetime.now().time(),
                ),
            ),
            Appointment.status.in_(["scheduled", "confirmed"]),
        )
        .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc())
        .first()
    )
    if not appointment:
        return "NOT SCHEDULED"

    date_text = appointment.appointment_date.strftime("%b %d").replace(" 0", " ")
    time_text = appointment.appointment_time.strftime("%I:%M %p").lstrip("0")
    return f"{date_text} | {time_text}"


def google_wallet_class_payload():
    return {
        "id": google_wallet_class_id(),
        "classTemplateInfo": {
            "cardTemplateOverride": {
                "cardRowTemplateInfos": [
                    {
                        "oneItem": {
                            "item": {
                                "firstValue": {
                                    "fields": [
                                        {
                                            "fieldPath": "object.textModulesData['remaining_changes']",
                                        }
                                    ]
                                }
                            }
                        }
                    },
                    {
                        "twoItems": {
                            "startItem": {
                                "firstValue": {
                                    "fields": [
                                        {
                                            "fieldPath": "object.textModulesData['next_service']",
                                        }
                                    ]
                                }
                            },
                            "endItem": {
                                "firstValue": {
                                    "fields": [
                                        {
                                            "fieldPath": "object.textModulesData['membership_status']",
                                        }
                                    ]
                                }
                            },
                        }
                    },
                ]
            }
        },
    }


def google_wallet_member_object_payload(member):
    expiration_end = f"{member.expiration_date.isoformat()}T23:59:59Z"
    logo_url = google_wallet_public_https_url(url_for("static", filename="carnova-wallet-logo-v2.png"))
    manage_package_url = google_wallet_public_https_url(url_for("member_public", token=member.token))
    schedule_oil_change_url = google_wallet_public_https_url(url_for("public_new_appointment", token=member.token))

    payload = {
        "id": google_wallet_object_id(member),
        "classId": google_wallet_class_id(),
        "genericType": "GENERIC_OTHER",
        "state": google_wallet_member_state(member),
        "cardTitle": {"defaultValue": {"language": "en-US", "value": "Carnova Oil Club"}},
        "header": {"defaultValue": {"language": "en-US", "value": "Oil Club Premium"}},
        "subheader": {"defaultValue": {"language": "en-US", "value": member.name}},
        "hexBackgroundColor": "#101820",
        "textModulesData": [
            {
                "id": "remaining_changes",
                "header": "Oil Changes Left",
                "body": google_wallet_remaining_changes_text(member.remaining_changes),
            },
            {
                "id": "next_service",
                "header": "Next Service",
                "body": google_wallet_next_service_text(member),
            },
            {
                "id": "total_changes",
                "header": "Package Total Oil Changes",
                "body": str(member.total_changes),
            },
            {
                "id": "membership_status",
                "header": "Membership Status",
                "body": current_member_status(member).title(),
            },
            {
                "id": "expiration_date",
                "header": "Expiration Date",
                "body": member.expiration_date.strftime("%B %d, %Y"),
            },
        ],
        "barcode": {
            "type": "QR_CODE",
            "value": member_public_url(member),
            "alternateText": member.member_id,
        },
        "validTimeInterval": {
            "end": {
                "date": expiration_end,
            }
        },
    }

    if logo_url:
        payload["logo"] = {
            "sourceUri": {
                "uri": logo_url,
            },
            "contentDescription": {
                "defaultValue": {
                    "language": "en-US",
                    "value": "Carnova Oil logo",
                }
            },
        }

    if manage_package_url:
        payload["linksModuleData"] = {
            "uris": [
                {
                    "uri": manage_package_url,
                    "description": "Manage Your Package",
                    "id": "manage_package",
                }
            ]
        }

    if schedule_oil_change_url:
        payload["appLinkData"] = {
            "displayText": {
                "defaultValue": {
                    "language": "en-US",
                    "value": "Schedule Oil Change",
                }
            },
            "webAppLinkInfo": {
                "appTarget": {
                    "targetUri": {
                        "uri": schedule_oil_change_url,
                        "description": "Schedule Oil Change",
                    }
                }
            },
        }

    return payload


def ensure_google_wallet_class(access_token):
    class_id_value = google_wallet_class_id()
    if not class_id_value:
        return False

    if class_id_value in GOOGLE_WALLET_ENSURED_CLASS_IDS:
        return True

    class_payload = google_wallet_class_payload()
    base_url = "https://walletobjects.googleapis.com/walletobjects/v1"
    class_id = parse.quote(class_id_value, safe="")
    class_url = f"{base_url}/genericClass/{class_id}"

    class_patch_status, _ = google_wallet_api_call("PATCH", class_url, class_payload, access_token=access_token)
    if class_patch_status in {200, 201}:
        GOOGLE_WALLET_ENSURED_CLASS_IDS.add(class_id_value)
        return True

    if class_patch_status == 404:
        class_create_status, _ = google_wallet_api_call(
            "POST",
            f"{base_url}/genericClass",
            class_payload,
            access_token=access_token,
        )
        if class_create_status in {200, 201, 409}:
            GOOGLE_WALLET_ENSURED_CLASS_IDS.add(class_id_value)
            return True

        print(f"Google Wallet class create failed: status={class_create_status}")
        return False

    print(f"Google Wallet class update failed: status={class_patch_status}")
    return False


def google_wallet_access_token():
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    credentials = service_account.Credentials.from_service_account_file(
        credentials_path,
        scopes=[GOOGLE_WALLET_SCOPE],
    )
    credentials.refresh(GoogleAuthRequest())
    return credentials.token


def google_wallet_service_account_email():
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    credentials = service_account.Credentials.from_service_account_file(credentials_path)
    return credentials.service_account_email


def google_wallet_api_call(method, endpoint, payload=None, access_token=None):
    body = None
    token_value = access_token or google_wallet_access_token()
    headers = {
        "Authorization": f"Bearer {token_value}",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib_request.Request(endpoint, data=body, headers=headers, method=method)
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            raw = response.read().decode("utf-8") if response else ""
            parsed = json.loads(raw) if raw else {}
            return response.status, parsed
    except urllib_error.HTTPError as error:
        raw_error = error.read().decode("utf-8") if hasattr(error, "read") else ""
        try:
            parsed_error = json.loads(raw_error) if raw_error else {}
        except Exception:
            parsed_error = {"error": raw_error}
        return error.code, parsed_error


def google_wallet_upsert_member_object(member, access_token=None):
    object_id = parse.quote(google_wallet_object_id(member), safe="")
    payload = google_wallet_member_object_payload(member)
    base_url = "https://walletobjects.googleapis.com/walletobjects/v1"
    object_url = f"{base_url}/genericObject/{object_id}"
    token_value = access_token or google_wallet_access_token()

    ensure_google_wallet_class(token_value)

    patch_status, _ = google_wallet_api_call("PATCH", object_url, payload, access_token=token_value)
    if patch_status in {200, 201}:
        return True

    if patch_status != 404:
        print(f"Google Wallet update failed for {member.member_id}: status={patch_status}")
        return False

    create_status, _ = google_wallet_api_call("POST", f"{base_url}/genericObject", payload, access_token=token_value)
    if create_status in {200, 201, 409}:
        return True

    print(f"Google Wallet create failed for {member.member_id}: status={create_status}")
    return False


def google_wallet_save_url(member):
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    signer = service_account.Credentials.from_service_account_file(credentials_path).signer
    issuer_email = google_wallet_service_account_email()
    origins = [resolve_public_base_url()] if resolve_public_base_url() else []

    payload = {
        "iss": issuer_email,
        "aud": "google",
        "typ": "savetowallet",
        "payload": {
            "genericObjects": [
                {
                    "id": google_wallet_object_id(member),
                }
            ]
        },
    }
    if origins:
        payload["origins"] = origins

    token = google_jwt.encode(signer, payload)
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return f"https://pay.google.com/gp/v/save/{token}"


def google_wallet_save_url_is_safe(url):
    if not url or not isinstance(url, str):
        return False

    parsed = parse.urlsplit(url)
    if parsed.scheme != "https":
        return False
    if parsed.netloc.lower() != "pay.google.com":
        return False
    if not parsed.path.startswith("/gp/v/save/"):
        return False
    return True


def sync_member_google_wallet_object(member):
    if not google_wallet_is_configured() or not member:
        return False
    try:
        return google_wallet_upsert_member_object(member)
    except Exception as error:
        print(f"Google Wallet sync error for {member.member_id}: {error}")
        return False


def sync_member_google_wallet_save_url(member):
    if not google_wallet_is_configured() or not member:
        return None
    try:
        if not google_wallet_upsert_member_object(member):
            return None
        return google_wallet_save_url(member)
    except Exception as error:
        print(f"Google Wallet save URL error for {member.member_id}: {error}")
        return None

MONTHLY_PRICE_ID = "price_1TxtO7R1GwRFNmYeGo3km5vf"
MONTHLY_PRICE_ID_ALT = "price_1Txt07R1GwRFNmYeGo3km5vf"
MONTHLY_PRICE_IDS = {MONTHLY_PRICE_ID, MONTHLY_PRICE_ID_ALT}

STRIPE_PLANS = {
    "price_1Tx6veR1GwRFNmYeUO2goMjz": {
        "name": "Bronze",
        "changes": 3,
        "valid_days": 365,
        "subscription": False,
    },
    "price_1TwiJER1GwRFNmYeeFbUdscR": {
        "name": "Silver",
        "changes": 5,
        "valid_days": 548,
        "subscription": False,
    },
    "price_1Tx70UR1GwRFNmYePYn1Xrdz": {
        "name": "Gold",
        "changes": 8,
        "valid_days": 730,
        "subscription": False,
    },
    MONTHLY_PRICE_ID: {
        "name": "Monthly Membership",
        "changes": 3,
        "valid_days": 365,
        "subscription": True,
    },
}


def stripe_object_id(value):
    """Return an ID whether Stripe supplied a string or expanded object."""
    if isinstance(value, str):
        return value
    if value and hasattr(value, "get"):
        return value.get("id")
    return None


def invoice_subscription_id(invoice):
    """Support both legacy and newer Stripe invoice payload shapes."""
    direct = stripe_object_id(invoice.get("subscription"))
    if direct:
        return direct
    parent = invoice.get("parent") or {}
    subscription_details = parent.get("subscription_details") or {}
    return stripe_object_id(subscription_details.get("subscription"))


def find_subscription_member(subscription_id=None, customer_id=None, payment_id=None, email=None):
    if subscription_id:
        member = Member.query.filter_by(stripe_subscription_id=subscription_id).first()
        if member:
            return member
    if payment_id:
        member = Member.query.filter_by(stripe_payment_id=payment_id).first()
        if member:
            return member
    if customer_id:
        member = (
            Member.query.filter_by(stripe_customer_id=customer_id)
            .order_by(Member.created_at.desc())
            .first()
        )
        if member:
            return member
    if email:
        normalized_email = email.strip().lower()
        if normalized_email:
            return (
                Member.query.filter_by(email=normalized_email)
                .filter(
                    Member.stripe_subscription_id.is_(None),
                    Member.stripe_customer_id.is_(None),
                    Member.stripe_payment_id.is_(None),
                )
                .order_by(Member.created_at.desc())
                .first()
            )
    return None


def normalize_subscription_status(status, cancel_at_period_end=False):
    if not status:
        return None
    if status in {"active", "trialing"}:
        return "active"
    if status in {"past_due", "unpaid", "incomplete", "incomplete_expired", "paused"}:
        return "past_due"
    if status in {"canceled", "cancelled"}:
        return "cancelled"
    if cancel_at_period_end and status == "active":
        return "active"
    return None


def sync_subscription_details(member, subscription_id=None, customer_id=None, status=None, cancel_at_period_end=False):
    member.stripe_subscription_id = subscription_id or member.stripe_subscription_id
    member.stripe_customer_id = customer_id or member.stripe_customer_id
    normalized_status = normalize_subscription_status(status, cancel_at_period_end=cancel_at_period_end)
    if normalized_status is not None:
        member.subscription_status = normalized_status
    member.status = current_member_status(member)
    return member


def log_stripe_event(event_type, member, subscription_id, status):
    member_id = member.member_id if member else "none"
    subscription_value = subscription_id or "none"
    print(f"Stripe event={event_type} member={member_id} subscription={subscription_value} status={status}")


def advance_annual_benefit_period(member, paid_on):
    """Refresh annual credits once the current 12-month benefit period ends."""
    period_end = member.benefit_period_end or member.expiration_date
    if not period_end or paid_on < period_end:
        return False

    # Advance in 365-day blocks in case more than one anniversary passed.
    next_start = period_end
    next_end = next_start + timedelta(days=365)
    while paid_on >= next_end:
        next_start = next_end
        next_end = next_start + timedelta(days=365)

    member.benefit_period_start = next_start
    member.benefit_period_end = next_end
    member.expiration_date = next_end
    member.total_changes = 3
    member.remaining_changes = 3
    return True


def mark_stripe_event_processed(event):
    event_id = event.get("id")
    if not event_id:
        return
    db.session.add(StripeEvent(event_id=event_id, event_type=event.get("type", "unknown")))


def send_ga4_purchase_event(checkout_session):
    try:
        print("GA4: function started")
        measurement_id = os.environ.get("GA4_MEASUREMENT_ID", "").strip()
        api_secret = os.environ.get("GA4_API_SECRET", "").strip()
        print("GA4: environment variables loaded")
        if not measurement_id or not api_secret:
            print("GA4 purchase event skipped: missing GA4_MEASUREMENT_ID or GA4_API_SECRET")
            return

        metadata = checkout_session.get("metadata") or {}
        client_id = (
            str(metadata.get("ga_client_id") or "").strip()
            or str(stripe_object_id(checkout_session.get("customer")) or "").strip()
            or str(checkout_session.get("id") or "").strip()
        )
        if not client_id:
            print("GA4 purchase event skipped: missing client_id")
            return

        amount_total = checkout_session.get("amount_total") or 0
        currency = str(checkout_session.get("currency") or "USD").upper()
        plan_name = (
            str(metadata.get("plan_name") or "").strip()
            or str(metadata.get("membership_plan_name") or "").strip()
            or "Carnova Oil Club Membership"
        )

        payload = {
            "client_id": client_id,
            "events": [
                {
                    "name": "purchase",
                    "params": {
                        "transaction_id": str(checkout_session.get("id") or ""),
                        "value": float(amount_total) / 100.0,
                        "currency": currency,
                        "engagement_time_msec": 1,
                        "items": [
                            {
                                "item_name": plan_name,
                                "quantity": 1,
                                "price": float(amount_total) / 100.0,
                            }
                        ],
                    },
                }
            ],
        }

        endpoint = (
            "https://www.google-analytics.com/mp/collect?"
            + parse.urlencode(
                {
                    "measurement_id": measurement_id,
                    "api_secret": api_secret,
                }
            )
        )
        req = urllib_request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        print("GA4: sending purchase event")
        with urllib_request.urlopen(req, timeout=5):
            pass
        print("GA4: purchase sent successfully")
    except Exception as error:
        print("GA4 purchase event error:", error)


def process_checkout_completed(obj):
    details = obj.get("customer_details") or {}
    shipping = obj.get("shipping_details") or {}
    metadata = obj.get("metadata") or {}

    email = details.get("email") or obj.get("customer_email")
    customer_name = (
        details.get("name")
        or shipping.get("name")
        or metadata.get("customer_name")
        or obj.get("customer_name")
    )
    customer_phone = details.get("phone") or shipping.get("phone") or ""
    customer_id = stripe_object_id(obj.get("customer"))
    subscription_id = stripe_object_id(obj.get("subscription"))
    payment_id = stripe_object_id(obj.get("payment_intent")) or obj.get("id")

    stripe_secret = os.environ.get("STRIPE_SECRET_KEY")
    if stripe_secret:
        stripe.api_key = stripe_secret

    # Some checkout payloads can omit customer/subscription IDs.
    # Recover them directly from Stripe so portal access fields are persisted.
    if stripe_secret and (not customer_id or (obj.get("mode") == "subscription" and not subscription_id)):
        try:
            checkout_session = stripe.checkout.Session.retrieve(obj.get("id"))
            customer_id = customer_id or stripe_object_id(checkout_session.get("customer"))
            subscription_id = subscription_id or stripe_object_id(checkout_session.get("subscription"))
        except Exception as error:
            print("Error retrieving Stripe checkout session:", error)

    if customer_id and stripe_secret and (not customer_name or not customer_phone):
        try:
            customer = stripe.Customer.retrieve(customer_id)
            customer_name = customer_name or customer.get("name")
            customer_phone = customer_phone or customer.get("phone") or ""
            email = email or customer.get("email")
        except Exception as error:
            print("Error retrieving Stripe customer:", error)

    line_items = stripe.checkout.Session.list_line_items(
        obj.get("id"), limit=1, expand=["data.price"]
    )
    if not line_items.get("data"):
        raise ValueError("Checkout session has no line items")
    price_id = line_items["data"][0]["price"]["id"]

    if obj.get("mode") == "subscription":
        if price_id not in MONTHLY_PRICE_IDS:
            print("Ignoring Stripe checkout session for unsupported subscription price:", price_id)
            return None, False
        selected_plan = STRIPE_PLANS.get(MONTHLY_PRICE_ID)
    else:
        selected_plan = STRIPE_PLANS.get(price_id)

    if not selected_plan:
        print("Ignoring Stripe checkout session for unsupported price:", price_id)
        return None, False
    if not email:
        raise ValueError("Stripe checkout did not include a customer email")

    today = date.today()
    expiration = today + timedelta(days=selected_plan["valid_days"])
    subscription_status = "active" if selected_plan["subscription"] else None

    # Webhook retries and duplicate checkout events must not create duplicate members.
    existing = find_subscription_member(
        subscription_id=subscription_id,
        customer_id=customer_id,
        payment_id=payment_id,
        email=email,
    )
    if existing:
        normalized_email = email.strip().lower() if email else existing.email
        existing.stripe_payment_id = payment_id or existing.stripe_payment_id
        existing.stripe_customer_id = customer_id or existing.stripe_customer_id
        existing.stripe_subscription_id = subscription_id or existing.stripe_subscription_id
        existing.stripe_price_id = price_id or existing.stripe_price_id
        existing.plan_name = selected_plan["name"]
        existing.subscription_status = subscription_status or existing.subscription_status
        existing.name = customer_name or existing.name or (normalized_email or "").split("@")[0].replace(".", " ").title()
        existing.email = normalized_email or existing.email
        existing.phone = customer_phone or existing.phone
        existing.purchase_date = existing.purchase_date or today
        existing.expiration_date = existing.expiration_date or expiration
        existing.total_changes = max(existing.total_changes or 0, selected_plan["changes"])
        existing.remaining_changes = max(existing.remaining_changes or 0, selected_plan["changes"])
        existing.benefit_period_start = today if selected_plan["subscription"] else existing.benefit_period_start
        existing.benefit_period_end = expiration if selected_plan["subscription"] else existing.benefit_period_end
        existing.price_paid_cents = int(obj.get("amount_total") or existing.price_paid_cents or 0)
        existing.status = current_member_status(existing)
        db.session.flush()
        return existing, False

    if subscription_id and stripe_secret:
        try:
            subscription = stripe.Subscription.retrieve(subscription_id)
            subscription_status = subscription.get("status") or subscription_status
        except Exception as error:
            print("Error retrieving Stripe subscription:", error)

    member = Member(
        member_id=next_member_id(),
        name=customer_name or email.split("@")[0].replace(".", " ").title(),
        email=email.strip().lower(),
        phone=customer_phone,
        purchase_date=today,
        expiration_date=expiration,
        total_changes=selected_plan["changes"],
        remaining_changes=selected_plan["changes"],
        status="active",
        stripe_payment_id=payment_id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        stripe_price_id=price_id,
        plan_name=selected_plan["name"],
        subscription_status=subscription_status,
        benefit_period_start=today if selected_plan["subscription"] else None,
        benefit_period_end=expiration if selected_plan["subscription"] else None,
        price_paid_cents=int(obj.get("amount_total") or 0),
        token=secrets.token_urlsafe(24),
    )
    member.status = current_member_status(member)
    db.session.add(member)
    db.session.flush()
    return member, True


def process_invoice_payment_succeeded(obj):
    subscription_id = invoice_subscription_id(obj)
    customer_id = stripe_object_id(obj.get("customer"))
    customer_details = obj.get("customer_details") or {}
    email = customer_details.get("email") or obj.get("customer_email")
    member = find_subscription_member(
        subscription_id=subscription_id,
        customer_id=customer_id,
        email=email,
    )
    if not member:
        print("No member found for successful invoice:", obj.get("id"))
        return None, False

    sync_subscription_details(member, subscription_id=subscription_id, customer_id=customer_id, status="active")
    paid_timestamp = obj.get("status_transitions", {}).get("paid_at") or obj.get("created")
    paid_on = datetime.utcfromtimestamp(paid_timestamp).date() if paid_timestamp else date.today()
    benefits_reset = advance_annual_benefit_period(member, paid_on)
    member.status = current_member_status(member)
    log_stripe_event("invoice.paid", member, subscription_id, member.subscription_status)
    return member, benefits_reset


def process_invoice_payment_failed(obj):
    subscription_id = invoice_subscription_id(obj)
    customer_id = stripe_object_id(obj.get("customer"))
    customer_details = obj.get("customer_details") or {}
    email = customer_details.get("email") or obj.get("customer_email")
    member = find_subscription_member(
        subscription_id=subscription_id,
        customer_id=customer_id,
        email=email,
    )
    if member:
        sync_subscription_details(member, subscription_id=subscription_id, customer_id=customer_id, status="past_due")
        log_stripe_event("invoice.payment_failed", member, subscription_id, member.subscription_status)


def process_subscription_updated(obj):
    subscription_id = stripe_object_id(obj.get("id"))
    customer_id = stripe_object_id(obj.get("customer"))
    customer_details = obj.get("customer_details") or {}
    email = customer_details.get("email") or obj.get("customer_email")
    member = find_subscription_member(
        subscription_id=subscription_id,
        customer_id=customer_id,
        email=email,
    )
    if not member:
        print("No member found for subscription update:", subscription_id)
        return

    sync_subscription_details(
        member,
        subscription_id=subscription_id,
        customer_id=customer_id,
        status=obj.get("status") or member.subscription_status,
        cancel_at_period_end=bool(obj.get("cancel_at_period_end")),
    )
    log_stripe_event("customer.subscription.updated", member, subscription_id, member.subscription_status)


def process_subscription_deleted(obj):
    subscription_id = stripe_object_id(obj.get("id"))
    customer_id = stripe_object_id(obj.get("customer"))
    customer_details = obj.get("customer_details") or {}
    email = customer_details.get("email") or obj.get("customer_email")
    member = find_subscription_member(
        subscription_id=subscription_id,
        customer_id=customer_id,
        email=email,
    )
    if member:
        sync_subscription_details(member, subscription_id=subscription_id, customer_id=customer_id, status="canceled")
        log_stripe_event("customer.subscription.deleted", member, subscription_id, member.subscription_status)


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
    except Exception as error:
        print("Invalid Stripe webhook:", error)
        return "Invalid webhook", 400

    event_id = event.get("id")
    if event_id and StripeEvent.query.filter_by(event_id=event_id).first():
        return "", 200

    event_type = event.get("type")
    obj = event["data"]["object"]
    member = None
    wallet_sync_member = None
    ga4_checkout_session = None

    try:
        if event_type == "checkout.session.completed":
            member, _was_created = process_checkout_completed(obj)
            ga4_checkout_session = obj
        elif event_type in {"invoice.payment_succeeded", "invoice.paid"}:
            invoice_member, benefits_reset = process_invoice_payment_succeeded(obj)
            if benefits_reset:
                wallet_sync_member = invoice_member
        elif event_type == "invoice.payment_failed":
            process_invoice_payment_failed(obj)
        elif event_type == "customer.subscription.created":
            process_subscription_updated(obj)
        elif event_type == "customer.subscription.updated":
            process_subscription_updated(obj)
        elif event_type == "customer.subscription.deleted":
            process_subscription_deleted(obj)
        else:
            return "", 200

        mark_stripe_event_processed(event)
        db.session.commit()
    except ValueError as error:
        db.session.rollback()
        print("Stripe webhook data error:", error)
        return str(error), 400
    except Exception as error:
        db.session.rollback()
        print("Stripe webhook processing error:", error)
        return "Webhook processing failed", 500

    if ga4_checkout_session:
        send_ga4_purchase_event(ga4_checkout_session)

    if wallet_sync_member:
        sync_member_google_wallet_object(wallet_sync_member)

    if member:
        print("MEMBERSHIP EMAIL TRIGGERED")
        if not send_membership_confirmation_email(member):
            print("MEMBERSHIP EMAIL FAILED")

    return "", 200

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
