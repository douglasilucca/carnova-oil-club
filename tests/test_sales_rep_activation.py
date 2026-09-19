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
