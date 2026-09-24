from datetime import date, timedelta

import pytest

from app import (
    AppleWalletPass,
    CarnovaCard,
    Member,
    ReferralSale,
    SalesRep,
    db,
    ensure_carnova_card,
    google_wallet_object_id,
)
from app import app as flask_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "carnova-card-test-key-0123456789")
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "issuer-card-test")
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="carnova-card-test",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_member(member_id="COC-CARD-1", email="card@example.com"):
    member = Member(
        name="Card Member",
        email=email,
        member_id=member_id,
        expiration_date=date.today() + timedelta(days=365),
        token=f"token-{member_id}",
    )
    db.session.add(member)
    db.session.flush()
    return member


def make_rep(slug="card-rep", email="rep@example.com"):
    rep = SalesRep(name="Card Rep", slug=slug, email=email, login_email=email)
    db.session.add(rep)
    db.session.flush()
    return rep


def test_sales_rep_only_card_has_stable_token_and_owner(client):
    with flask_app.app_context():
        rep = make_rep()
        result = ensure_carnova_card(sales_rep=rep)
        db.session.commit()
        card = result["card"]
        assert result["created"] is True
        assert card.sales_rep is rep
        assert card.member is None
        assert card.stable_card_token
        assert card.google_object_id is None
        assert rep.carnova_card.id == card.id


def test_member_and_sales_rep_create_one_card(client):
    with flask_app.app_context():
        member = make_member()
        rep = make_rep()
        result = ensure_carnova_card(member=member, sales_rep=rep)
        db.session.commit()
        assert result["created"] is True
        assert CarnovaCard.query.count() == 1
        assert member.carnova_card.id == rep.carnova_card.id
        assert result["card"].google_object_id == google_wallet_object_id(member)


def test_sales_rep_card_later_receives_member_without_new_card(client):
    with flask_app.app_context():
        rep = make_rep()
        first = ensure_carnova_card(sales_rep=rep)["card"]
        db.session.commit()
        member = make_member()
        result = ensure_carnova_card(member=member, sales_rep=rep)
        db.session.commit()
        assert result["card"].id == first.id
        assert result["card"].member_id == member.id
        assert CarnovaCard.query.count() == 1


def test_member_card_later_receives_sales_rep_without_new_card(client):
    with flask_app.app_context():
        member = make_member()
        first = ensure_carnova_card(member=member)["card"]
        db.session.commit()
        rep = make_rep()
        result = ensure_carnova_card(member=member, sales_rep=rep)
        db.session.commit()
        assert result["card"].id == first.id
        assert result["card"].sales_rep_id == rep.id
        assert CarnovaCard.query.count() == 1


def test_repeated_ensure_returns_same_card(client):
    with flask_app.app_context():
        member = make_member()
        rep = make_rep()
        first = ensure_carnova_card(member=member, sales_rep=rep)["card"]
        db.session.commit()
        second = ensure_carnova_card(member=member, sales_rep=rep)["card"]
        assert second.id == first.id
        assert CarnovaCard.query.count() == 1


def test_different_member_and_rep_cards_report_conflict_without_merge(client):
    with flask_app.app_context():
        member = make_member()
        rep = make_rep()
        member_card = ensure_carnova_card(member=member)["card"]
        rep_card = ensure_carnova_card(sales_rep=rep)["card"]
        db.session.commit()
        result = ensure_carnova_card(member=member, sales_rep=rep)
        assert result["reason"] == "identity_conflict"
        assert result["card"] is None
        assert CarnovaCard.query.count() == 2
        assert member_card.id != rep_card.id


def test_existing_apple_pass_is_mapped_without_recreation_or_identity_changes(client):
    with flask_app.app_context():
        member = make_member(member_id="COC-CARD-APPLE")
        pass_record = AppleWalletPass.create_for_member(member)
        original_pass_id = pass_record.id
        original_serial = pass_record.serial_number
        original_token_hash = pass_record.authentication_token_hash
        result = ensure_carnova_card(member=member)
        db.session.commit()
        assert db.session.get(AppleWalletPass, original_pass_id).carnova_card_id == result["card"].id
        assert AppleWalletPass.query.count() == 1
        persisted = db.session.get(AppleWalletPass, original_pass_id)
        assert persisted.serial_number == original_serial
        assert persisted.authentication_token_hash == original_token_hash


def test_carnova_card_does_not_change_referral_sale(client):
    with flask_app.app_context():
        rep = make_rep(slug="seller", email="seller-card@example.com")
        member = make_member(member_id="COC-CARD-SALE", email="buyer-card@example.com")
        sale = ReferralSale(
            sales_rep=rep,
            member=member,
            stripe_event_id="evt-card-sale",
            stripe_checkout_session_id="cs-card-sale",
            stripe_price_id="price-card",
            plan_name="Bronze",
            oil_changes=3,
            commission_cents=1000,
        )
        db.session.add(sale)
        ensure_carnova_card(member=member, sales_rep=rep)
        db.session.commit()
        persisted = db.session.get(ReferralSale, sale.id)
        assert persisted.sales_rep_id == rep.id
        assert persisted.member_id == member.id
        assert persisted.commission_cents == 1000


def test_carnova_card_conflict_does_not_rollback_outer_member_and_sale(client, monkeypatch):
    with flask_app.app_context():
        member = make_member("COC-CARD-OUTER", "outer@example.com")
        rep = make_rep("outer-rep", "outer-rep@example.com")
        db.session.add(ReferralSale(
            sales_rep=rep,
            member=member,
            stripe_event_id="evt-outer-card",
            stripe_checkout_session_id="cs-outer-card",
            stripe_price_id="price-outer",
            plan_name="Bronze",
            oil_changes=3,
            commission_cents=1000,
        ))
        original_flush = db.session.flush
        state = {"fail": True}

        def fail_once(*args, **kwargs):
            if state["fail"]:
                state["fail"] = False
                from sqlalchemy.exc import IntegrityError
                raise IntegrityError("simulated card conflict", {}, RuntimeError("conflict"))
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(db.session, "flush", fail_once)
        result = ensure_carnova_card(member=member, sales_rep=rep)
        monkeypatch.setattr(db.session, "flush", original_flush)
        assert result["reason"] in {"identity_conflict", "deferred_for_admin_review", "already_exists"}
        db.session.commit()
        assert Member.query.filter_by(member_id="COC-CARD-OUTER").one().id == member.id
        assert ReferralSale.query.filter_by(stripe_event_id="evt-outer-card").count() == 1


def test_neutral_card_page_does_not_set_referral_attribution(client):
    with flask_app.app_context():
        rep = make_rep("neutral-rep", "neutral-rep@example.com")
        card = ensure_carnova_card(sales_rep=rep)["card"]
        db.session.commit()
        token = card.stable_card_token
    response = client.get(f"/card/{token}")
    assert response.status_code == 200
    assert b"Buy with My Link" in response.data
    with client.session_transaction() as saved:
        assert "sales_rep_referral" not in saved
    assert client.get("/r/neutral-rep").status_code == 302
    with client.session_transaction() as saved:
        assert saved["sales_rep_referral"]["sales_rep_id"] == 1


def test_public_card_share_page_exposes_exact_referral_link_without_login(client):
    with flask_app.app_context():
        rep = make_rep("public-share-rep", "public-share@example.com")
        card = ensure_carnova_card(sales_rep=rep)["card"]
        db.session.commit()
        token = card.stable_card_token

    response = client.get(f"/card/{token}/share")

    assert response.status_code == 200
    assert b"/r/public-share-rep" in response.data
    assert b"Copy My Link" in response.data
    assert b"Share My Link" in response.data
    assert b"public-share@example.com" not in response.data
    assert b"correct-password" not in response.data
    assert b"commission" not in response.data.lower()
    assert b"earnings" not in response.data.lower()
    assert b"Add to Apple Wallet" not in response.data
    assert b"Add to Google Wallet" not in response.data
    assert b"Sales Rep Portal" not in response.data
    assert b"internal" not in response.data.lower()
    assert b"serial" not in response.data.lower()
    assert b"auth" not in response.data.lower()
    assert b"google_object_id" not in response.data
    assert client.get(f"/card/{token}/apple-wallet").status_code == 404
    assert client.post(f"/card/{token}/google-wallet").status_code == 404


def test_public_card_share_page_does_not_set_attribution_and_invalid_tokens_are_safe(client):
    with flask_app.app_context():
        rep = make_rep("share-safe-rep", "share-safe@example.com")
        card = ensure_carnova_card(sales_rep=rep)["card"]
        db.session.commit()
        token = card.stable_card_token

    response = client.get(f"/card/{token}/share")

    assert response.status_code == 200
    with client.session_transaction() as saved:
        assert "sales_rep_referral" not in saved
    assert client.get("/card/not-a-real-token/share").status_code == 404


def test_card_without_active_sales_rep_cannot_expose_share_page(client):
    with flask_app.app_context():
        rep = make_rep("inactive-share-rep", "inactive-share@example.com")
        rep.active = False
        card = ensure_carnova_card(sales_rep=rep)["card"]
        db.session.commit()
        token = card.stable_card_token

    response = client.get(f"/card/{token}/share")

    assert response.status_code == 404
