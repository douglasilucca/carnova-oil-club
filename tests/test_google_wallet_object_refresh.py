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
    google_wallet_upsert_card_object,
)
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="google-refresh-test",
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


def make_rep(slug="refresh-rep"):
    rep = SalesRep(
        name=slug.replace("-", " ").title(),
        slug=slug,
        login_email=f"{slug}@example.com",
        password_hash=generate_password_hash("correct-password"),
        portal_enabled=True,
    )
    db.session.add(rep)
    db.session.flush()
    return rep


def make_card(slug="refresh-rep", google_object_id=None):
    rep = make_rep(slug)
    card = ensure_carnova_card(sales_rep=rep)["card"]
    card.google_object_id = google_object_id
    db.session.commit()
    return rep, card


def make_member_card(member_id="COC-GOOGLE-REFRESH", google_object_id=None):
    member = Member(
        name="Google Refresh Member",
        email=f"{member_id.lower()}@example.com",
        member_id=member_id,
        expiration_date=date.today() + timedelta(days=365),
        remaining_changes=2,
        total_changes=3,
        token=f"{member_id.lower()}-token",
    )
    db.session.add(member)
    db.session.flush()
    card = ensure_carnova_card(member=member)["card"]
    card.google_object_id = google_object_id
    db.session.commit()
    return card


def test_single_refresh_requires_admin_and_is_post_only(client):
    with flask_app.app_context():
        rep, card = make_card(google_object_id="issuer.carnova_card_refresh")
        rep_id = rep.id
    assert client.post(f"/admin/sales-reps/{rep_id}/google-wallet/refresh").status_code == 302
    as_admin(client)
    assert client.get(f"/admin/sales-reps/{rep_id}/google-wallet/refresh").status_code == 405


def test_single_refresh_syncs_only_existing_object_and_preserves_id(client, monkeypatch):
    with flask_app.app_context():
        rep, card = make_card(google_object_id="issuer.carnova_card_refresh")
        rep_id = rep.id
        card_id = card.id
        original_id = card.google_object_id
    calls = []
    monkeypatch.setattr("app.sync_carnova_card_google_wallet", lambda value: calls.append(value.id) or True)
    as_admin(client)
    response = client.post(f"/admin/sales-reps/{rep_id}/google-wallet/refresh", follow_redirects=True)
    assert response.status_code == 200
    assert calls == [card_id]
    with flask_app.app_context():
        refreshed = db.session.get(CarnovaCard, card_id)
        assert refreshed.google_object_id == original_id


def test_single_refresh_without_object_does_not_sync_or_create_id(client, monkeypatch):
    with flask_app.app_context():
        rep, card = make_card()
        rep_id = rep.id
        card_id = card.id
        before_cards = CarnovaCard.query.count()
    calls = []
    monkeypatch.setattr("app.sync_carnova_card_google_wallet", lambda value: calls.append(value.id) or True)
    as_admin(client)
    response = client.post(f"/admin/sales-reps/{rep_id}/google-wallet/refresh", follow_redirects=True)
    assert b"No Google Wallet object has been created" in response.data
    assert calls == []
    with flask_app.app_context():
        assert CarnovaCard.query.count() == before_cards
        assert db.session.get(CarnovaCard, card_id).google_object_id is None


def test_single_refresh_reports_sync_failure_without_changing_id(client, monkeypatch):
    with flask_app.app_context():
        rep, card = make_card(google_object_id="issuer.carnova_card_failure")
        rep_id = rep.id
        card_id = card.id
    monkeypatch.setattr("app.sync_carnova_card_google_wallet", lambda value: False)
    as_admin(client)
    response = client.post(f"/admin/sales-reps/{rep_id}/google-wallet/refresh", follow_redirects=True)
    assert b"Google Wallet refresh failed" in response.data
    with flask_app.app_context():
        assert db.session.get(CarnovaCard, card_id).google_object_id == "issuer.carnova_card_failure"


def test_refresh_all_requires_admin_and_is_post_only(client):
    assert client.post("/admin/google-wallet/refresh-all").status_code == 302
    as_admin(client)
    assert client.get("/admin/google-wallet/refresh-all").status_code == 405


def test_refresh_all_selects_only_existing_ids_and_reports_counts(client, monkeypatch):
    with flask_app.app_context():
        _, eligible_one = make_card("eligible-one", "issuer.carnova_card_one")
        _, eligible_two = make_card("eligible-two", "issuer.carnova_card_two")
        _, without_id = make_card("without-id")
        eligible_ids = [eligible_one.id, eligible_two.id]
        without_id_value = without_id.id
        before_cards = CarnovaCard.query.count()
    calls = []
    monkeypatch.setattr("app.sync_carnova_card_google_wallet", lambda card: calls.append(card.id) or card.id == eligible_ids[0])
    as_admin(client)
    response = client.post("/admin/google-wallet/refresh-all", follow_redirects=True)
    assert b"2 eligible, 1 successful, 1 failed" in response.data
    assert calls == eligible_ids
    with flask_app.app_context():
        assert CarnovaCard.query.count() == before_cards
        assert db.session.get(CarnovaCard, without_id_value).google_object_id is None
        assert db.session.get(CarnovaCard, eligible_one.id).google_object_id == "issuer.carnova_card_one"
        assert db.session.get(CarnovaCard, eligible_two.id).google_object_id == "issuer.carnova_card_two"


def test_refresh_all_with_no_existing_ids_does_not_sync_or_create(client, monkeypatch):
    with flask_app.app_context():
        make_card("without-id-one")
        make_card("without-id-two")
        before_cards = CarnovaCard.query.count()
    calls = []
    monkeypatch.setattr("app.sync_carnova_card_google_wallet", lambda card: calls.append(card.id) or True)
    as_admin(client)
    response = client.post("/admin/google-wallet/refresh-all", follow_redirects=True)
    assert b"0 eligible, 0 successful, 0 failed" in response.data
    assert calls == []
    with flask_app.app_context():
        assert CarnovaCard.query.count() == before_cards


def test_refresh_all_continues_after_sync_failure(client, monkeypatch):
    with flask_app.app_context():
        _, first = make_card("first", "issuer.carnova_card_first")
        _, second = make_card("second", "issuer.carnova_card_second")
        ids = [first.id, second.id]
    calls = []

    def sync(card):
        calls.append(card.id)
        return card.id == ids[1]

    monkeypatch.setattr("app.sync_carnova_card_google_wallet", sync)
    as_admin(client)
    response = client.post("/admin/google-wallet/refresh-all", follow_redirects=True)
    assert b"2 eligible, 1 successful, 1 failed" in response.data
    assert calls == ids


def test_google_upsert_patches_existing_object_without_post(client, monkeypatch):
    with flask_app.app_context():
        _, card = make_card("patch", "issuer.carnova_card_patch")
        object_id = google_wallet_card_object_id(card)
        card_id = card.id
    calls = []
    monkeypatch.setattr("app.ensure_google_wallet_class", lambda token: True)

    def api_call(method, endpoint, payload=None, access_token=None):
        calls.append((method, endpoint, payload["id"]))
        return 200, {}

    monkeypatch.setattr("app.google_wallet_api_call", api_call)
    with flask_app.test_request_context("/"):
        assert google_wallet_upsert_card_object(db.session.get(CarnovaCard, card_id), access_token="test-token") is True
    assert calls == [("PATCH", calls[0][1], object_id)]


def test_google_upsert_posts_only_after_patch_404_with_same_id(client, monkeypatch):
    with flask_app.app_context():
        _, card = make_card("fallback", "issuer.carnova_card_fallback")
        object_id = google_wallet_card_object_id(card)
        card_id = card.id
    calls = []
    monkeypatch.setattr("app.ensure_google_wallet_class", lambda token: True)

    def api_call(method, endpoint, payload=None, access_token=None):
        calls.append((method, endpoint, payload["id"]))
        return (404, {}) if method == "PATCH" else (200, {})

    monkeypatch.setattr("app.google_wallet_api_call", api_call)
    with flask_app.test_request_context("/"):
        assert google_wallet_upsert_card_object(db.session.get(CarnovaCard, card_id), access_token="test-token") is True
    assert [call[0] for call in calls] == ["PATCH", "POST"]
    assert all(call[2] == object_id for call in calls)


def test_admin_templates_keep_wallet_refresh_controls_and_current_sales_link(client):
    with flask_app.app_context():
        rep, card = make_card("template", "issuer.carnova_card_template")
        rep_id = rep.id
    as_admin(client)
    detail = client.get(f"/admin/sales-reps/{rep_id}")
    reps = client.get("/admin/sales-reps")
    assert b"Refresh Google Wallet" in detail.data
    assert b"No Apple Wallet pass has been created" in detail.data
    assert b"Refresh All Existing Google Wallet Cards" in reps.data
    assert b"Refresh All Apple Wallet Passes" in reps.data
