from datetime import date, timedelta

import pytest
from werkzeug.security import generate_password_hash

from app import (
    AppleWalletDevice,
    AppleWalletPass,
    AppleWalletRegistration,
    CarnovaCard,
    Member,
    SalesRep,
    db,
    ensure_carnova_card,
)
from app import app as flask_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "refresh-test-key-0123456789abcdef")
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="apple-wallet-refresh-test",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def as_admin(client):
    with client.session_transaction() as saved:
        saved["admin_id"] = 1


def make_rep(name="Refresh Rep", slug="refresh-rep"):
    rep = SalesRep(name=name, slug=slug, login_email=f"{slug}@example.com", password_hash=generate_password_hash("correct-password"), portal_enabled=True)
    db.session.add(rep)
    db.session.flush()
    return rep


def make_member(member_id="COC-REFRESH-1", token="refresh-member-token"):
    member = Member(name="Refresh Member", email="refresh-member@example.com", member_id=member_id, expiration_date=date.today() + timedelta(days=365), remaining_changes=2, total_changes=3, token=token)
    db.session.add(member)
    db.session.flush()
    return member


def make_rep_with_pass():
    rep = make_rep()
    card = ensure_carnova_card(sales_rep=rep)["card"]
    pass_record = AppleWalletPass.create_for_card(card)
    db.session.commit()
    return rep, card, pass_record


# --- single Sales Rep pass refresh -----------------------------------------

def test_refresh_requires_admin_authentication(client):
    with flask_app.app_context():
        rep, card, pass_record = make_rep_with_pass()
        rep_id = rep.id
    response = client.post(f"/admin/sales-reps/{rep_id}/apple-wallet/refresh")
    assert response.status_code == 302
    assert response.location.endswith("/login")


def test_refresh_get_is_not_allowed(client):
    with flask_app.app_context():
        rep, card, pass_record = make_rep_with_pass()
        rep_id = rep.id
    as_admin(client)
    response = client.get(f"/admin/sales-reps/{rep_id}/apple-wallet/refresh")
    assert response.status_code == 405


def test_refresh_advances_change_tag_and_preserves_identity(client, monkeypatch):
    with flask_app.app_context():
        rep, card, pass_record = make_rep_with_pass()
        rep_id = rep.id
        original_serial = pass_record.serial_number
        original_token = pass_record.authentication_token
        original_token_hash = pass_record.authentication_token_hash
        original_web_service_url = pass_record.web_service_url
        original_type_id = pass_record.pass_type_identifier
        before = pass_record.last_updated
        pass_count_before = AppleWalletPass.query.count()

    monkeypatch.setattr("app.apple_wallet_send_push_for_pass", lambda pass_record: True)
    as_admin(client)
    response = client.post(f"/admin/sales-reps/{rep_id}/apple-wallet/refresh")
    assert response.status_code == 302

    with flask_app.app_context():
        assert AppleWalletPass.query.count() == pass_count_before
        refreshed = db.session.get(AppleWalletPass, pass_record.id)
        assert refreshed.serial_number == original_serial
        assert refreshed.authentication_token == original_token
        assert refreshed.authentication_token_hash == original_token_hash
        assert refreshed.web_service_url == original_web_service_url
        assert refreshed.pass_type_identifier == original_type_id
        assert refreshed.last_updated > before


def test_refresh_never_creates_a_pass_when_none_exists(client):
    with flask_app.app_context():
        rep = make_rep()
        db.session.commit()
        rep_id = rep.id
        pass_count_before = AppleWalletPass.query.count()
    as_admin(client)
    response = client.post(f"/admin/sales-reps/{rep_id}/apple-wallet/refresh")
    assert response.status_code == 302
    with flask_app.app_context():
        assert AppleWalletPass.query.count() == pass_count_before


def test_refresh_invokes_apns_push_for_registered_devices(client, monkeypatch):
    with flask_app.app_context():
        rep, card, pass_record = make_rep_with_pass()
        device = AppleWalletDevice.create_or_update("refresh-device", "refresh-push-token")
        AppleWalletRegistration.register(pass_record, device)
        db.session.commit()
        rep_id = rep.id
        pass_id = pass_record.id

    calls = []
    monkeypatch.setattr("app.apple_wallet_send_push_for_pass", lambda record: calls.append(record.id) or True)
    as_admin(client)
    client.post(f"/admin/sales-reps/{rep_id}/apple-wallet/refresh")
    assert calls == [pass_id]


def test_refresh_change_tag_persists_even_if_apns_push_fails(client, monkeypatch):
    with flask_app.app_context():
        rep, card, pass_record = make_rep_with_pass()
        rep_id = rep.id
        pass_id = pass_record.id
        before = pass_record.last_updated

    def failing_push(record):
        raise RuntimeError("APNs unavailable")

    monkeypatch.setattr("app.apple_wallet_send_push_for_pass", failing_push)
    as_admin(client)
    response = client.post(f"/admin/sales-reps/{rep_id}/apple-wallet/refresh")
    assert response.status_code == 302
    with flask_app.app_context():
        refreshed = db.session.get(AppleWalletPass, pass_id)
        assert refreshed.last_updated > before


def test_refresh_unknown_rep_returns_404(client):
    as_admin(client)
    response = client.post("/admin/sales-reps/999999/apple-wallet/refresh")
    assert response.status_code == 404


# --- refresh-all --------------------------------------------------------

def test_refresh_all_requires_admin_authentication(client):
    response = client.post("/admin/apple-wallet/refresh-all")
    assert response.status_code == 302
    assert response.location.endswith("/login")


def test_refresh_all_get_is_not_allowed(client):
    as_admin(client)
    response = client.get("/admin/apple-wallet/refresh-all")
    assert response.status_code == 405


def test_refresh_all_advances_every_existing_pass_and_creates_none(client, monkeypatch):
    with flask_app.app_context():
        rep_one, card_one, pass_one = make_rep_with_pass()
        member = make_member()
        db.session.commit()
        pass_two = AppleWalletPass.create_for_member(member)
        before_one = pass_one.last_updated
        before_two = pass_two.last_updated
        pass_ids = [pass_one.id, pass_two.id]
        pass_count_before = AppleWalletPass.query.count()

    monkeypatch.setattr("app.apple_wallet_send_push_for_pass", lambda record: True)
    as_admin(client)
    response = client.post("/admin/apple-wallet/refresh-all")
    assert response.status_code == 302

    with flask_app.app_context():
        assert AppleWalletPass.query.count() == pass_count_before
        refreshed_one = db.session.get(AppleWalletPass, pass_ids[0])
        refreshed_two = db.session.get(AppleWalletPass, pass_ids[1])
        assert refreshed_one.last_updated > before_one
        assert refreshed_two.last_updated > before_two
        assert refreshed_one.serial_number == pass_one.serial_number
        assert refreshed_two.serial_number == pass_two.serial_number


def test_refresh_all_continues_after_one_pass_push_fails(client, monkeypatch):
    with flask_app.app_context():
        rep_one, card_one, pass_one = make_rep_with_pass()
        member = make_member()
        db.session.commit()
        pass_two = AppleWalletPass.create_for_member(member)
        before_one = pass_one.last_updated
        before_two = pass_two.last_updated
        pass_ids = [pass_one.id, pass_two.id]

    def flaky_push(record):
        if record.id == pass_ids[0]:
            raise RuntimeError("APNs unavailable for this device")
        return True

    monkeypatch.setattr("app.apple_wallet_send_push_for_pass", flaky_push)
    as_admin(client)
    response = client.post("/admin/apple-wallet/refresh-all")
    assert response.status_code == 302

    with flask_app.app_context():
        refreshed_one = db.session.get(AppleWalletPass, pass_ids[0])
        refreshed_two = db.session.get(AppleWalletPass, pass_ids[1])
        assert refreshed_one.last_updated > before_one
        assert refreshed_two.last_updated > before_two


def test_refresh_all_with_no_existing_passes_creates_none(client):
    as_admin(client)
    response = client.post("/admin/apple-wallet/refresh-all")
    assert response.status_code == 302
    with flask_app.app_context():
        assert AppleWalletPass.query.count() == 0


def test_refresh_all_preserves_registrations_and_devices(client, monkeypatch):
    with flask_app.app_context():
        rep, card, pass_record = make_rep_with_pass()
        device = AppleWalletDevice.create_or_update("stable-device", "stable-push-token")
        registration = AppleWalletRegistration.register(pass_record, device)
        db.session.commit()
        device_id = device.id
        registration_id = registration.id

    monkeypatch.setattr("app.apple_wallet_send_push_for_pass", lambda record: True)
    as_admin(client)
    client.post("/admin/apple-wallet/refresh-all")

    with flask_app.app_context():
        assert db.session.get(AppleWalletDevice, device_id) is not None
        still_registered = db.session.get(AppleWalletRegistration, registration_id)
        assert still_registered is not None
        assert still_registered.is_active is True
