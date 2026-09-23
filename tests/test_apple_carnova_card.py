from datetime import date, timedelta
from io import BytesIO

import pytest
from werkzeug.security import generate_password_hash

from app import (
    AppleWalletDevice,
    AppleWalletPass,
    AppleWalletRegistration,
    CarnovaCard,
    Member,
    SalesRep,
    apple_wallet_card_payload,
    db,
    ensure_carnova_card,
)
from app import app as flask_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "apple-carnova-card-test-key-012345")
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="apple-carnova-card-test",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_member():
    member = Member(
        name="Apple Card Member",
        email="apple-card@example.com",
        member_id="COC-APPLE-CARD",
        expiration_date=date.today() + timedelta(days=365),
        remaining_changes=2,
        total_changes=3,
        token="apple-card-member-token",
    )
    db.session.add(member)
    db.session.flush()
    return member


def make_rep():
    rep = SalesRep(
        name="Apple Card Rep",
        slug="apple-card-rep",
        email="apple-card-rep@example.com",
        login_email="apple-card-rep@example.com",
        password_hash=generate_password_hash("correct-password"),
        portal_enabled=True,
    )
    db.session.add(rep)
    db.session.flush()
    return rep


def test_legacy_member_pass_identity_is_preserved_and_payload_uses_persisted_serial(client):
    with flask_app.app_context():
        member = make_member()
        pass_record = AppleWalletPass.create_for_member(member)
        original = (pass_record.id, pass_record.serial_number, pass_record.authentication_token_hash)
        card = ensure_carnova_card(member=member)["card"]
        db.session.commit()
        with flask_app.test_request_context("/"):
            payload = apple_wallet_card_payload(card, pass_record)
        persisted = db.session.get(AppleWalletPass, original[0])
        assert (persisted.id, persisted.serial_number, persisted.authentication_token_hash) == original
        assert payload["serialNumber"] == original[1]
        assert persisted.carnova_card_id == card.id


def test_member_sales_rep_payload_keeps_membership_qr_and_adds_sales_actions(client):
    with flask_app.app_context():
        member = make_member()
        rep = make_rep()
        card = ensure_carnova_card(member=member, sales_rep=rep)["card"]
        pass_record = AppleWalletPass.create_for_card(card)
        with flask_app.test_request_context("/"):
            payload = apple_wallet_card_payload(card, pass_record)
        fields = payload["generic"]["backFields"]
        keys = {field["key"] for field in fields}
        assert {"schedule_service", "manage_membership", "sales_link", "sales_portal", "sales_earnings"} <= keys
        assert payload["barcode"]["message"].endswith("/m/apple-card-member-token")
        assert any("/sales/share" in field.get("attributedValue", "") for field in fields)
        assert not any("/r/apple-card-rep" in field.get("attributedValue", "") for field in fields)


def test_sales_rep_only_pass_has_sales_actions_without_membership_fields(client):
    with flask_app.app_context():
        rep = make_rep()
        card = ensure_carnova_card(sales_rep=rep)["card"]
        pass_record = AppleWalletPass.create_for_card(card)
        with flask_app.test_request_context("/"):
            payload = apple_wallet_card_payload(card, pass_record)
        fields = payload["generic"]["backFields"]
        keys = {field["key"] for field in fields}
        assert {"sales_link", "sales_portal", "sales_earnings"} <= keys
        assert not {"schedule_service", "manage_membership", "vehicle", "expiration_date", "member_id"} & keys
        assert payload["barcode"]["message"].endswith(f"/card/{card.stable_card_token}")
        assert payload["serialNumber"] == pass_record.serial_number
        assert pass_record.member_id is None


def test_sales_rep_pass_keeps_identity_when_member_is_added(client):
    with flask_app.app_context():
        rep = make_rep()
        card = ensure_carnova_card(sales_rep=rep)["card"]
        pass_record = AppleWalletPass.create_for_card(card)
        device = AppleWalletDevice.create_or_update("device-apple-card", "push-token")
        AppleWalletRegistration.register(pass_record, device)
        db.session.commit()
        identity = (pass_record.id, pass_record.serial_number, pass_record.authentication_token_hash, device.id)
        member = make_member()
        ensure_carnova_card(member=member, sales_rep=rep)
        db.session.commit()
        db.session.refresh(pass_record)
        assert (pass_record.id, pass_record.serial_number, pass_record.authentication_token_hash, device.id) == identity
        assert pass_record.member_id == member.id
        assert AppleWalletRegistration.query.filter_by(pass_id=pass_record.id).count() == 1


def test_sales_rep_dashboard_apple_wallet_route_requires_authenticated_portal(client, monkeypatch):
    with flask_app.app_context():
        rep = make_rep()
        rep_id = rep.id
        db.session.commit()
    fake_bundle = BytesIO(b"fake pkpass")
    monkeypatch.setattr("app.apple_wallet_build_bundle", lambda **_kwargs: fake_bundle)
    assert client.get("/sales/apple-wallet").status_code == 302
    assert client.post("/sales/login", data={"email": "apple-card-rep@example.com", "password": "correct-password"}).status_code == 302
    response = client.get("/sales/apple-wallet")
    assert response.status_code == 200
    with flask_app.app_context():
        assert db.session.get(SalesRep, rep_id).carnova_card is not None
        assert CarnovaCard.query.count() == 1
