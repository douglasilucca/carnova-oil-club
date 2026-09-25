from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pytest
from werkzeug.security import check_password_hash

from app import SalesRep, db, sales_rep_activation_hash
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, SECRET_KEY="activation-test", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def create_rep(client, monkeypatch, phone="+15551234567"):
    calls = []

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return type("Message", (), {"sid": "SM-activation"})()

    class Client:
        messages = Messages()

    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
    monkeypatch.setattr("app.TwilioClient", lambda _sid, _token: Client())
    response = client.post(
        "/admin/sales-reps",
        data={"name": "Douglas Test", "slug": "douglas-test", "phone": phone, "email": "douglas@example.com"},
    )
    assert response.status_code == 302
    with flask_app.app_context():
        rep = SalesRep.query.one()
        token = urlsplit(calls[0]["body"].split("activation link: ", 1)[1]).path.rsplit("/", 1)[1]
        return token, rep.id


def test_creation_uses_email_and_stores_only_hashed_activation_token(client, monkeypatch):
    token, _ = create_rep(client, monkeypatch)
    with flask_app.app_context():
        rep = SalesRep.query.one()
        assert rep.login_email == "douglas@example.com"
        assert rep.activation_token_hash == sales_rep_activation_hash(token)
        assert rep.password_hash is None
        assert rep.portal_enabled is False
        assert rep.activation_token_expires_at > datetime.utcnow()
        assert rep.activation_sms_status == "sent"


def test_invalid_and_expired_activation_tokens_are_rejected(client, monkeypatch):
    token, rep_id = create_rep(client, monkeypatch)
    invalid = client.get("/sales/activate/not-the-token")
    assert invalid.status_code == 200
    assert b"invalid or has expired" in invalid.data
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        rep.activation_token_expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
    expired = client.get(f"/sales/activate/{token}")
    assert expired.status_code == 200
    assert b"invalid or has expired" in expired.data


def test_activation_sets_hashed_password_enables_login_and_is_single_use(client, monkeypatch):
    token, rep_id = create_rep(client, monkeypatch)
    response = client.post(
        f"/sales/activate/{token}",
        data={"password": "new-password", "password_confirmation": "new-password"},
    )
    assert response.status_code == 302
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert check_password_hash(rep.password_hash, "new-password")
        assert rep.portal_enabled is True
        assert rep.activation_token_hash is None
        assert rep.activation_token_expires_at is None
        login_email = rep.login_email
    assert client.post("/sales/login", data={"email": login_email, "password": "new-password"}).status_code == 302
    assert b"invalid or has expired" in client.get(f"/sales/activate/{token}").data


def test_sales_rep_creation_survives_twilio_failure_and_records_it(client, monkeypatch):
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")

    class FailedMessages:
        def create(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    class FailedClient:
        messages = FailedMessages()

    monkeypatch.setattr("app.TwilioClient", lambda _sid, _token: FailedClient())
    response = client.post(
        "/admin/sales-reps",
        data={"name": "Failed SMS", "slug": "failed-sms", "phone": "+15551234567", "email": "failed@example.com"},
    )
    assert response.status_code == 302
    with flask_app.app_context():
        rep = SalesRep.query.filter_by(email="failed@example.com").one()
        assert rep.activation_sms_status == "failed"
        assert "provider unavailable" in rep.activation_sms_error


def test_activation_recovery_actions_require_admin_and_are_post_only(client):
    with flask_app.app_context():
        rep = SalesRep(name="Recovery Rep", slug="recovery-rep", email="recovery@example.com", phone="+15551234567")
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    for suffix in ("sms", "email", "link"):
        assert client.post(f"/admin/sales-reps/{rep_id}/activation/{suffix}").status_code == 302
        assert client.get(f"/admin/sales-reps/{rep_id}/activation/{suffix}").status_code == 405


def test_activation_recovery_requires_destinations_and_preserves_sms_status(client):
    with flask_app.app_context():
        rep = SalesRep(name="No Destinations", slug="no-destinations")
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    sms_response = client.post(f"/admin/sales-reps/{rep_id}/activation/sms", follow_redirects=True)
    email_response = client.post(f"/admin/sales-reps/{rep_id}/activation/email", follow_redirects=True)
    assert b"no valid phone" in sms_response.data
    assert b"no email address" in email_response.data
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert rep.activation_token_hash is None
        assert rep.activation_sms_status == "pending"


def test_activation_email_uses_configured_sender_and_fresh_activation_url(client, monkeypatch):
    with flask_app.app_context():
        rep = SalesRep(name="Email Recovery", slug="email-recovery", email="email-recovery@example.com")
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    calls = []
    monkeypatch.setattr("app.send_smtp_email", lambda *args: calls.append(args) or True)
    response = client.post(f"/admin/sales-reps/{rep_id}/activation/email", follow_redirects=True)
    assert response.status_code == 200
    assert b"Activation email sent successfully" in response.data
    assert calls[0][0] == "email-recovery@example.com"
    assert calls[0][1] == "Activate Your Carnova Oil Club Sales Rep Account"
    assert "/sales/activate/" in calls[0][2]
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert rep.activation_token_expires_at > datetime.utcnow()
        assert rep.activation_token_hash not in calls[0][2]


def test_sms_rotation_token_activates_and_raw_token_is_not_logged(client, monkeypatch, caplog):
    with flask_app.app_context():
        rep = SalesRep(name="SMS Recovery", slug="sms-recovery", email="sms-recovery@example.com", phone="+15551234567")
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    calls = []
    monkeypatch.setattr("app.send_sales_rep_activation_sms", lambda _rep, url: calls.append(url))
    client.post(f"/admin/sales-reps/{rep_id}/activation/sms")
    raw_token = calls[0].rsplit("/", 1)[-1]
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert rep.activation_token_hash == sales_rep_activation_hash(raw_token)
        assert raw_token not in rep.activation_token_hash
    assert raw_token not in caplog.text
    assert client.get(f"/sales/activate/{raw_token}").status_code == 200


def test_copy_activation_link_rotates_hashed_token_and_uses_base_url(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://staging.carnova.test")
    with flask_app.app_context():
        rep = SalesRep(name="Recovery Rep", slug="recovery-rep", email="recovery@example.com")
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    first = client.post(f"/admin/sales-reps/{rep_id}/activation/link")
    assert first.status_code == 200
    assert b"https://staging.carnova.test/sales/activate/" in first.data
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        first_hash = rep.activation_token_hash
        assert first_hash
        assert b"activation_token_hash" not in first.data
    second = client.post(f"/admin/sales-reps/{rep_id}/activation/link")
    assert second.status_code == 200
    with flask_app.app_context():
        assert db.session.get(SalesRep, rep_id).activation_token_hash != first_hash


def test_activation_sms_email_failures_preserve_rep_state(client, monkeypatch):
    with flask_app.app_context():
        rep = SalesRep(name="Recovery Rep", slug="recovery-rep", email="recovery@example.com", phone="+15551234567")
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    monkeypatch.setattr("app.send_sales_rep_activation_sms", lambda _rep, _url: None)
    monkeypatch.setattr("app.send_sales_rep_activation_email", lambda _rep, _url: False)
    assert client.post(f"/admin/sales-reps/{rep_id}/activation/sms").status_code == 302
    assert client.post(f"/admin/sales-reps/{rep_id}/activation/email").status_code == 302
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert rep.password_hash is None
        assert rep.portal_enabled is False
        assert rep.activation_token_hash


def test_activated_rep_is_not_reset_by_recovery_actions(client, monkeypatch):
    with flask_app.app_context():
        rep = SalesRep(name="Active Rep", slug="active-rep", email="active@example.com", password_hash="existing-hash", portal_enabled=True)
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    monkeypatch.setattr("app.send_sales_rep_activation_sms", lambda *_args: pytest.fail("SMS should not be sent"))
    response = client.post(f"/admin/sales-reps/{rep_id}/activation/link", follow_redirects=True)
    assert b"Portal Active" in response.data
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert rep.password_hash == "existing-hash"
        assert rep.portal_enabled is True
