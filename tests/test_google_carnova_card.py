from datetime import date, timedelta

import pytest
from werkzeug.security import generate_password_hash

from app import (
    CarnovaCard,
    Member,
    SalesRep,
    db,
    ensure_carnova_card,
    google_wallet_card_object_id,
    google_wallet_card_object_payload,
    google_wallet_member_object_payload,
)
from app import app as flask_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "issuer-google-test")
    monkeypatch.setenv("GOOGLE_WALLET_CLASS_ID", "class-google-test")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    flask_app.config.update(TESTING=True, SECRET_KEY="google-card-test", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_member():
    member = Member(name="Google Member", email="google-member@example.com", member_id="COC-GOOGLE-1", expiration_date=date.today() + timedelta(days=365), remaining_changes=2, total_changes=3, token="google-member-token")
    db.session.add(member)
    db.session.flush()
    return member


def make_rep():
    rep = SalesRep(name="Google Rep", slug="google-rep", email="google-rep@example.com", login_email="google-rep@example.com", password_hash=generate_password_hash("correct-password"), portal_enabled=True)
    db.session.add(rep)
    db.session.flush()
    return rep


def test_legacy_member_object_id_and_payload_remain_unchanged(client):
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = make_member()
        legacy_id = "issuer-google-test.carnova_coc-google-1"
        assert google_wallet_card_object_id(ensure_carnova_card(member=member)["card"]) == legacy_id
        assert google_wallet_member_object_payload(member)["id"] == legacy_id
        db.session.commit()
        assert member.carnova_card.google_object_id == legacy_id
        assert "sales_link" not in str(google_wallet_member_object_payload(member))


def test_member_sales_rep_payload_has_membership_and_sales_actions(client):
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = make_member()
        rep = make_rep()
        card = ensure_carnova_card(member=member, sales_rep=rep)["card"]
        payload = google_wallet_card_object_payload(card)
        link_ids = {link["id"] for link in payload["linksModuleData"]["uris"]}
        assert {"manage_package", "sales_link", "sales_portal", "sales_earnings"} <= link_ids
        assert "appLinkData" in payload
        assert payload["barcode"]["value"].endswith("/m/google-member-token")


def test_sales_rep_only_has_stable_neutral_id_and_no_membership_fields(client):
    with flask_app.app_context(), flask_app.test_request_context("/"):
        rep = make_rep()
        card = ensure_carnova_card(sales_rep=rep)["card"]
        first_id = google_wallet_card_object_id(card)
        payload = google_wallet_card_object_payload(card)
        second_id = google_wallet_card_object_id(card)
        link_ids = {link["id"] for link in payload["linksModuleData"]["uris"]}
        assert first_id == second_id == card.google_object_id
        assert ".carnova_card_" in first_id
        assert {"sales_link", "sales_portal", "sales_earnings"} <= link_ids
        assert "remaining_changes" not in {module["id"] for module in payload["textModulesData"]}
        assert "appLinkData" not in payload
        assert payload["barcode"]["value"].endswith(f"/card/{card.stable_card_token}")


def test_sales_rep_to_member_keeps_neutral_google_object_id(client):
    with flask_app.app_context(), flask_app.test_request_context("/"):
        rep = make_rep()
        card = ensure_carnova_card(sales_rep=rep)["card"]
        original_id = google_wallet_card_object_id(card)
        member = make_member()
        ensure_carnova_card(member=member, sales_rep=rep)
        db.session.commit()
        assert card.google_object_id == original_id
        payload = google_wallet_card_object_payload(card)
        assert payload["id"] == original_id
        assert "remaining_changes" in {module["id"] for module in payload["textModulesData"]}


def test_member_to_sales_rep_keeps_legacy_google_object_id(client):
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = make_member()
        card = ensure_carnova_card(member=member)["card"]
        original_id = google_wallet_card_object_id(card)
        rep = make_rep()
        ensure_carnova_card(member=member, sales_rep=rep)
        db.session.commit()
        assert card.google_object_id == original_id == "issuer-google-test.carnova_coc-google-1"
        assert google_wallet_card_object_payload(card)["id"] == original_id


def test_authenticated_sales_rep_google_wallet_route_and_unauthenticated_redirect(client, monkeypatch):
    with flask_app.app_context():
        rep = make_rep()
        rep_email = rep.login_email
        db.session.commit()
    monkeypatch.setattr("app.google_wallet_is_configured", lambda: True)
    monkeypatch.setattr("app.sync_carnova_card_google_wallet", lambda _card: True)
    monkeypatch.setattr("app.google_wallet_save_url_for_object", lambda _object_id: "https://pay.google.com/gp/v/save/test")
    assert client.post("/sales/google-wallet").status_code == 302
    assert client.post("/sales/login", data={"email": rep_email, "password": "correct-password"}).status_code == 302
    response = client.post("/sales/google-wallet")
    assert response.status_code == 302
    assert response.location == "https://pay.google.com/gp/v/save/test"


def test_google_sync_failure_does_not_change_card_identity(client, monkeypatch):
    with flask_app.app_context():
        rep = make_rep()
        card = ensure_carnova_card(sales_rep=rep)["card"]
        original_id = google_wallet_card_object_id(card)
        monkeypatch.setattr("app.google_wallet_upsert_card_object", lambda _card: (_ for _ in ()).throw(RuntimeError("google unavailable")))
        from app import sync_carnova_card_google_wallet
        assert sync_carnova_card_google_wallet(card) is False
        assert card.google_object_id == original_id
