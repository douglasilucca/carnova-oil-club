import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps

import qrcode
import stripe
from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from io import BytesIO, StringIO
import csv

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))

database_url = os.environ.get("DATABASE_URL", "sqlite:///oilclub.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@carnovaoil.com")
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
    token = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    redemptions = db.relationship("Redemption", backref="member", lazy=True, cascade="all, delete-orphan")


class Redemption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    redeemed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    note = db.Column(db.Text, default="")
    employee = db.Column(db.String(255), default="Staff")


def init_db():
    db.create_all()
    if not Admin.query.filter_by(email=ADMIN_EMAIL).first():
        db.session.add(Admin(email=ADMIN_EMAIL, password_hash=generate_password_hash(ADMIN_PASSWORD)))
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
    q = request.args.get("q", "").strip()
    query = Member.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            Member.member_id.ilike(like),
            Member.name.ilike(like),
            Member.email.ilike(like),
            Member.phone.ilike(like)
        ))
    members = query.order_by(Member.created_at.desc()).all()
    for member in members:
        member.status = current_member_status(member)
    db.session.commit()

    all_members = Member.query.all()
    stats = {
        "total_members": len(all_members),
        "active_members": sum(1 for m in all_members if current_member_status(m) == "active"),
        "remaining_changes": sum(m.remaining_changes for m in all_members),
        "redeemed_changes": sum(m.total_changes - m.remaining_changes for m in all_members),
    }
    return render_template("dashboard.html", members=members, stats=stats, q=q)


@app.route("/members/new", methods=["GET", "POST"])
@login_required
def new_member():
    if request.method == "POST":
        purchase = date.fromisoformat(request.form.get("purchase_date") or date.today().isoformat())
        expiration_text = request.form.get("expiration_date")
        expiration = date.fromisoformat(expiration_text) if expiration_text else purchase + timedelta(days=365)
        total = int(request.form.get("total_changes", 5))

        member = Member(
            member_id=next_member_id(),
            name=request.form["name"].strip(),
            email=request.form["email"].strip(),
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
    redemptions = Redemption.query.filter_by(member_id=member.id).order_by(Redemption.redeemed_at.desc()).all()
    public_url = request.url_root.rstrip("/") + url_for("member_public", token=member.token)
    return render_template("member_detail.html", member=member, redemptions=redemptions, public_url=public_url)


@app.route("/members/<member_id>/edit", methods=["GET", "POST"])
@login_required
def edit_member(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    if request.method == "POST":
        member.name = request.form["name"].strip()
        member.email = request.form["email"].strip()
        member.phone = request.form.get("phone", "").strip()
        member.expiration_date = date.fromisoformat(request.form["expiration_date"])
        member.status = request.form.get("status", "active")
        db.session.commit()
        flash("Member information updated.", "success")
        return redirect(url_for("member_detail", member_id=member.member_id))
    return render_template("member_edit.html", member=member)


@app.route("/members/<member_id>/redeem", methods=["POST"])
@login_required
def redeem(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    if member.status != "active":
        flash("This membership is not active.", "error")
    elif member.remaining_changes <= 0:
        flash("No oil changes remain.", "error")
    elif member.expiration_date < date.today():
        flash("This membership has expired.", "error")
    else:
        member.remaining_changes -= 1
        member.status = current_member_status(member)
        db.session.add(Redemption(
            member_id=member.id,
            note=request.form.get("note", "").strip(),
            employee=session.get("admin_email", "Staff")
        ))
        db.session.commit()
        flash("One oil change was redeemed successfully.", "success")
    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/members/<member_id>/undo", methods=["POST"])
@login_required
def undo(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    last = Redemption.query.filter_by(member_id=member.id).order_by(Redemption.redeemed_at.desc()).first()
    if last:
        db.session.delete(last)
        member.remaining_changes = min(member.total_changes, member.remaining_changes + 1)
        member.status = current_member_status(member)
        db.session.commit()
        flash("Last redemption was undone.", "success")
    else:
        flash("No redemption to undo.", "error")
    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/m/<token>")
def member_public(token):
    member = Member.query.filter_by(token=token).first_or_404()
    member.status = current_member_status(member)
    db.session.commit()
    redemptions = Redemption.query.filter_by(member_id=member.id).order_by(Redemption.redeemed_at.desc()).all()
    return render_template("member_public.html", member=member, redemptions=redemptions)


@app.route("/members/<member_id>/qr")
@login_required
def member_qr(member_id):
    member = Member.query.filter_by(member_id=member_id).first_or_404()
    public_url = request.url_root.rstrip("/") + url_for("member_public", token=member.token)
    image = qrcode.make(public_url)
    stream = BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    return send_file(stream, mimetype="image/png", download_name=f"{member.member_id}-qr.png")


@app.route("/export/members.csv")
@login_required
def export_members():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Member ID","Name","Email","Phone","Purchase Date","Expiration Date","Total","Remaining","Status"])
    for member in Member.query.order_by(Member.created_at.desc()).all():
        writer.writerow([member.member_id, member.name, member.email, member.phone, member.purchase_date, member.expiration_date, member.total_changes, member.remaining_changes, current_member_status(member)])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment; filename=carnova-oil-club-members.csv"})


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        return "Webhook secret not configured", 500

    try:
        event = stripe.Webhook.construct_event(
            request.data,
            request.headers.get("Stripe-Signature", ""),
            webhook_secret
        )
    except Exception:
        return "Invalid webhook", 400

    if event["type"] == "checkout.session.completed":
        obj = event["data"]["object"]
        details = obj.get("customer_details") or {}
        email = details.get("email") or obj.get("customer_email")
        payment_id = obj.get("payment_intent") or obj.get("id")

        if email and not Member.query.filter_by(stripe_payment_id=payment_id).first():
            member = Member(
                member_id=next_member_id(),
                name=details.get("name") or obj.get("customer_name") or "Stripe Customer",
                email=email,
                phone=details.get("phone") or "",
                purchase_date=date.today(),
                expiration_date=date.today() + timedelta(days=365),
                total_changes=5,
                remaining_changes=5,
                stripe_payment_id=payment_id,
                token=secrets.token_urlsafe(24),
            )
            db.session.add(member)
            db.session.commit()

    return "", 200


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
