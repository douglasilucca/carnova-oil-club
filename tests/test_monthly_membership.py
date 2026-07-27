import json
import os
from datetime import date, timedelta

import pytest

from app import Member, Vehicle, current_member_status, db, monthly_membership_defaults
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test")
    os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def test_monthly_plan_defaults_are_correct():
    monthly_plan = {
        "name": "Monthly Membership",
        "changes": 3,
        "valid_days": 365,
        "subscription": True,
    }
    assert monthly_plan["changes"] == 3
    assert monthly_plan["valid_days"] == 365
    assert monthly_plan["subscription"] is True


def test_monthly_membership_defaults_are_set_for_manual_creation():
    defaults = monthly_membership_defaults(date.today())
    assert defaults["plan_name"] == "Monthly Membership"
    assert defaults["total_changes"] == 3
    assert defaults["remaining_changes"] == 3
    assert defaults["subscription_status"] == "active"
    assert defaults["benefit_period_end"] == defaults["benefit_period_start"] + timedelta(days=365)


def test_monthly_membership_rejects_second_vehicle(client):
    with flask_app.app_context():
        login_response = client.post(
            "/login",
            data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
            follow_redirects=True,
        )
        assert login_response.status_code == 200

        member = Member(
            name="Test",
            email="test@example.com",
            member_id="COC-00002",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()

        first_vehicle = Vehicle(member_id=member.id, make="Toyota", model="Camry", vin="1HGBH41JXMN109186")
        db.session.add(first_vehicle)
        db.session.commit()

        response = client.post(
            f"/members/{member.member_id}/vehicles/new",
            data={
                "make": "Honda",
                "model": "Civic",
                "vin": "1HGBH41JXMN109187",
                "plate": "ABC123",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"Monthly Membership allows only one registered vehicle." in response.data
        assert Vehicle.query.filter_by(member_id=member.id).count() == 1


def test_current_member_status_blocks_past_due_and_cancelled_members():
    member = Member(
        name="Test",
        email="test@example.com",
        member_id="COC-00001",
        expiration_date=date.today() + timedelta(days=30),
        remaining_changes=1,
        total_changes=3,
        token="token",
        stripe_subscription_id="sub_test",
        subscription_status="past_due",
    )
    assert current_member_status(member) == "past_due"

    member.subscription_status = "canceled"
    assert current_member_status(member) == "cancelled"


def test_checkout_session_completed_creates_monthly_member(client, monkeypatch):
    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_checkout", "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_test",
            "mode": "subscription",
            "customer": "cus_test",
            "subscription": "sub_test",
            "payment_intent": "pi_test",
            "customer_details": {"email": "monthly@example.com", "name": "Monthly Customer"},
            "amount_total": 2000,
            "metadata": {},
            "shipping_details": {},
        }}}

    class FakeLineItems:
        @staticmethod
        def list_line_items(session_id, limit=1, expand=None):
            return {"data": [{"price": {"id": "price_1Txt07R1GwRFNmYeGo3km5vf"}}]}

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)
    monkeypatch.setattr("app.stripe.checkout.Session", FakeLineItems)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    member = Member.query.filter_by(email="monthly@example.com").first()
    assert member is not None
    assert member.plan_name == "Monthly Membership"
    assert member.stripe_customer_id == "cus_test"
    assert member.stripe_subscription_id == "sub_test"
    assert member.subscription_status == "active"
    assert member.total_changes == 3
    assert member.remaining_changes == 3


def test_subscription_updated_marks_member_past_due(client, monkeypatch):
    member = Member(
        name="Past Due",
        email="pastdue@example.com",
        member_id="COC-00003",
        expiration_date=date.today() + timedelta(days=365),
        remaining_changes=3,
        total_changes=3,
        token="token",
        stripe_subscription_id="sub_past_due",
        stripe_customer_id="cus_past_due",
        plan_name="Monthly Membership",
        subscription_status="active",
    )
    db.session.add(member)
    db.session.commit()

    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_update", "type": "customer.subscription.updated", "data": {"object": {
            "id": "sub_past_due",
            "customer": "cus_past_due",
            "status": "past_due",
            "cancel_at_period_end": False,
        }}}

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    member = Member.query.get(member.id)
    assert member.subscription_status == "past_due"
    assert member.status == "past_due"


def test_invoice_paid_restores_active_status(client, monkeypatch):
    member = Member(
        name="Active Again",
        email="activeagain@example.com",
        member_id="COC-00004",
        expiration_date=date.today() + timedelta(days=365),
        remaining_changes=1,
        total_changes=1,
        token="token",
        stripe_subscription_id="sub_paid",
        stripe_customer_id="cus_paid",
        plan_name="Monthly Membership",
        subscription_status="past_due",
    )
    db.session.add(member)
    db.session.commit()

    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_paid", "type": "invoice.paid", "data": {"object": {
            "id": "in_paid",
            "subscription": "sub_paid",
            "customer": "cus_paid",
            "customer_details": {"email": "activeagain@example.com"},
            "status_transitions": {"paid_at": int(date.today().toordinal())},
        }}}

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    member = Member.query.get(member.id)
    assert member.subscription_status == "active"
    assert member.status == "active"


def test_subscription_deleted_marks_cancelled(client, monkeypatch):
    member = Member(
        name="Cancelled",
        email="cancelled@example.com",
        member_id="COC-00005",
        expiration_date=date.today() + timedelta(days=365),
        remaining_changes=3,
        total_changes=3,
        token="token",
        stripe_subscription_id="sub_deleted",
        stripe_customer_id="cus_deleted",
        plan_name="Monthly Membership",
        subscription_status="active",
    )
    db.session.add(member)
    db.session.commit()

    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_deleted", "type": "customer.subscription.deleted", "data": {"object": {
            "id": "sub_deleted",
            "customer": "cus_deleted",
            "status": "canceled",
        }}}

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    member = Member.query.get(member.id)
    assert member.subscription_status == "cancelled"
    assert member.status == "cancelled"


def test_duplicate_webhook_event_is_idempotent(client, monkeypatch):
    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_duplicate", "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_duplicate",
            "mode": "subscription",
            "customer": "cus_duplicate",
            "subscription": "sub_duplicate",
            "payment_intent": "pi_duplicate",
            "customer_details": {"email": "duplicate@example.com", "name": "Duplicate"},
            "amount_total": 2000,
            "metadata": {},
            "shipping_details": {},
        }}}

    class FakeLineItems:
        @staticmethod
        def list_line_items(session_id, limit=1, expand=None):
            return {"data": [{"price": {"id": "price_1Txt07R1GwRFNmYeGo3km5vf"}}]}

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)
    monkeypatch.setattr("app.stripe.checkout.Session", FakeLineItems)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )
    second_response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    assert second_response.status_code == 200
    assert Member.query.filter_by(email="duplicate@example.com").count() == 1


def test_bronze_silver_gold_plans_remain_unchanged(client, monkeypatch):
    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_gold", "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_gold",
            "mode": "payment",
            "customer": "cus_gold",
            "subscription": None,
            "payment_intent": "pi_gold",
            "customer_details": {"email": "gold@example.com", "name": "Gold Member"},
            "amount_total": 1000,
            "metadata": {},
            "shipping_details": {},
        }}}

    class FakeLineItems:
        @staticmethod
        def list_line_items(session_id, limit=1, expand=None):
            return {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]}

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)
    monkeypatch.setattr("app.stripe.checkout.Session", FakeLineItems)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    member = Member.query.filter_by(email="gold@example.com").first()
    assert member is not None
    assert member.plan_name == "Bronze"
    assert member.subscription_status is None
