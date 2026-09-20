from datetime import date, timedelta

import pytest

from app import (
    MONTHLY_PRICE_ID,
    Member,
    ReferralSale,
    SalesRep,
    db,
    ensure_member_sales_rep,
    process_checkout_completed,
    process_invoice_payment_succeeded,
)
from app import app as flask_app


BRONZE_PRICE = "price_1Tx6veR1GwRFNmYeUO2goMjz"
SILVER_PRICE = "price_1TwiJER1GwRFNmYeeFbUdscR"
GOLD_PRICE = "price_1Tx70UR1GwRFNmYePYn1Xrdz"


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="automatic-sales-rep-test",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def checkout_event(price_id, email, name="John Smith", event_id="evt-auto", session_id="cs-auto", mode="payment", metadata=None):
    return {
        "id": session_id,
        "mode": mode,
        "payment_intent": f"pi-{session_id}",
        "customer": f"cus-{session_id}",
        "subscription": f"sub-{session_id}" if mode == "subscription" else None,
        "customer_details": {"email": email, "name": name, "phone": "5085550199"},
        "amount_total": 14900,
        "metadata": metadata or {},
        "event_id": event_id,
    }


def fulfill(price_id, email, name="John Smith", event_id="evt-auto", session_id="cs-auto", mode="payment", metadata=None):
    event = checkout_event(price_id, email, name, event_id, session_id, mode, metadata)
    flask_app.config["_test_line_items"] = {"data": [{"price": {"id": price_id}}]}
    return event


def test_new_bronze_silver_gold_and_monthly_purchases_create_sales_reps(client, monkeypatch):
    cases = [
        (BRONZE_PRICE, "bronze@example.com", "Bronze Buyer", "payment"),
        (SILVER_PRICE, "silver@example.com", "Silver Buyer", "payment"),
        (GOLD_PRICE, "gold@example.com", "Gold Buyer", "payment"),
        (MONTHLY_PRICE_ID, "monthly@example.com", "Monthly Buyer", "subscription"),
    ]
    for index, (price_id, email, name, mode) in enumerate(cases):
        monkeypatch.setattr(
            "app.stripe.checkout.Session.list_line_items",
            lambda *_args, price_id=price_id, **_kwargs: {"data": [{"price": {"id": price_id}}]},
        )
        member, created = process_checkout_completed(
            checkout_event(price_id, email, name, f"evt-{index}", f"cs-{index}", mode),
            event_id=f"evt-{index}",
        )
        assert created is True
        assert member.sales_rep is not None
        assert member.sales_rep.member_id == member.id
        assert member.sales_rep.email == email
        assert member.sales_rep.login_email == email
        assert member.sales_rep.portal_enabled is False
        assert member.sales_rep.password_hash is None
        assert member.sales_rep.activation_token_hash is None


def test_existing_unlinked_sales_rep_is_linked_without_changing_credentials(client, monkeypatch):
    with flask_app.app_context():
        rep = SalesRep(
            name="John Smith",
            slug="john-smith",
            email="john@example.com",
            login_email="john@example.com",
            password_hash="existing-hash",
            portal_enabled=True,
            active=True,
        )
        db.session.add(rep)
        db.session.commit()
        rep_id = rep.id
    monkeypatch.setattr(
        "app.stripe.checkout.Session.list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": BRONZE_PRICE}}]},
    )

    member, _ = process_checkout_completed(
        checkout_event(BRONZE_PRICE, "john@example.com", "John Smith"),
        event_id="evt-link-existing",
    )

    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert member.sales_rep.id == rep_id
        assert rep.slug == "john-smith"
        assert rep.login_email == "john@example.com"
        assert rep.password_hash == "existing-hash"
        assert rep.portal_enabled is True


def test_already_linked_member_is_a_no_op(client):
    with flask_app.app_context():
        member = Member(
            name="Already Linked",
            email="linked@example.com",
            member_id="COC-AUTO-1",
            expiration_date=date.today() + timedelta(days=365),
            token="linked-token",
        )
        rep = SalesRep(name="Already Linked", slug="already-linked", member=member)
        db.session.add(rep)
        db.session.commit()
        result = ensure_member_sales_rep(member, purchaser_email="linked@example.com")
        assert result["reason"] == "already_linked"
        assert SalesRep.query.count() == 1


def test_duplicate_checkout_fulfillment_does_not_create_duplicate_sales_rep(client, monkeypatch):
    monkeypatch.setattr(
        "app.stripe.checkout.Session.list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": BRONZE_PRICE}}]},
    )
    event = checkout_event(BRONZE_PRICE, "retry@example.com", session_id="cs-retry")
    first, created = process_checkout_completed(event, event_id="evt-retry-1")
    second, was_created = process_checkout_completed(event, event_id="evt-retry-2")
    assert created is True
    assert was_created is False
    assert first.id == second.id
    assert SalesRep.query.count() == 1


def test_ambiguous_existing_sales_reps_are_not_auto_linked(client):
    with flask_app.app_context():
        db.session.add_all([
            SalesRep(name="One", slug="one", email="ambiguous@example.com", login_email="one@example.com"),
            SalesRep(name="Two", slug="two", email="ambiguous@example.com", login_email="two@example.com"),
        ])
        member = Member(
            name="Ambiguous",
            email="ambiguous@example.com",
            member_id="COC-AUTO-2",
            expiration_date=date.today() + timedelta(days=365),
            token="ambiguous-token",
        )
        db.session.add(member)
        db.session.commit()
        result = ensure_member_sales_rep(member)
        assert result["reason"] == "deferred_for_admin_review"
        assert member.sales_rep is None
        assert SalesRep.query.count() == 2


def test_existing_sales_rep_linked_elsewhere_is_not_reassigned(client):
    with flask_app.app_context():
        original_member = Member(name="Original", email="same@example.com", member_id="COC-AUTO-3", expiration_date=date.today() + timedelta(days=365), token="original-token")
        new_member = Member(name="New", email="same@example.com", member_id="COC-AUTO-4", expiration_date=date.today() + timedelta(days=365), token="new-token")
        rep = SalesRep(name="Existing", slug="existing", email="same@example.com", login_email="same@example.com", member=original_member)
        db.session.add_all([original_member, new_member, rep])
        db.session.commit()
        result = ensure_member_sales_rep(new_member)
        assert result["reason"] == "existing_rep_linked_elsewhere"
        assert rep.member_id == original_member.id
        assert new_member.sales_rep is None


def test_referral_seller_remains_unchanged_when_buyer_gets_sales_rep(client, monkeypatch):
    with flask_app.app_context():
        seller = SalesRep(name="Maria", slug="maria", email="maria@example.com", login_email="maria@example.com")
        db.session.add(seller)
        db.session.commit()
        seller_id = seller.id
    monkeypatch.setattr(
        "app.stripe.checkout.Session.list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": BRONZE_PRICE}}]},
    )
    event = checkout_event(
        BRONZE_PRICE,
        "john@example.com",
        "John",
        metadata={"sales_rep_id": str(seller_id)},
    )
    member, _ = process_checkout_completed(event, event_id="evt-referral-buyer")
    db.session.commit()
    with flask_app.app_context():
        sale = ReferralSale.query.one()
        assert sale.sales_rep_id == seller_id
        assert sale.member_id == member.id
        assert member.sales_rep.member_id == member.id
        assert member.sales_rep.id != seller_id


def test_slug_collision_gets_unique_slug(client):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Existing John", slug="john-smith"))
        member = Member(name="John Smith", email="new-john@example.com", member_id="COC-AUTO-5", expiration_date=date.today() + timedelta(days=365), token="new-john-token")
        db.session.add(member)
        db.session.commit()
        result = ensure_member_sales_rep(member)
        assert result["created"] is True
        assert member.sales_rep.slug == "john-smith-2"


def test_invoice_renewal_does_not_create_sales_rep(client):
    with flask_app.app_context():
        member = Member(
            name="Renewal Member",
            email="renewal@example.com",
            member_id="COC-AUTO-6",
            expiration_date=date.today() - timedelta(days=1),
            total_changes=3,
            remaining_changes=0,
            token="renewal-token",
            stripe_subscription_id="sub-renewal",
            stripe_customer_id="cus-renewal",
            benefit_period_end=date.today() - timedelta(days=1),
        )
        db.session.add(member)
        db.session.commit()
        process_invoice_payment_succeeded({
            "id": "in-renewal",
            "subscription": "sub-renewal",
            "customer": "cus-renewal",
            "customer_details": {"email": "renewal@example.com"},
            "status_transitions": {"paid_at": 1758000000},
        })
        assert SalesRep.query.count() == 0
