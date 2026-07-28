import json
import os
from datetime import date, timedelta

import pytest
import stripe

from app import Member, Redemption, Vehicle, current_member_status, db, monthly_membership_defaults
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
    login_response = client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200

    with flask_app.app_context():
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

    with flask_app.app_context():
        member = Member.query.filter_by(member_id="COC-00002").first()
        member_id = member.member_id

    response = client.post(
        f"/members/{member_id}/vehicles/new",
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


def test_member_detail_shows_qr_for_public_verification(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context():
        member = Member(
            name="QR Member",
            email="qr@example.com",
            member_id="COC-00901",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="qr-member-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    response = client.get(f"/members/{member_id}")

    assert response.status_code == 200
    assert b"Scan to verify membership" in response.data
    assert f"/members/{member_id}/qr".encode() in response.data
    assert b"QR Member" in response.data


def test_member_qr_points_to_tokenized_public_card(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context():
        member = Member(
            name="QR Route Member",
            email="qr-route@example.com",
            member_id="COC-00902",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="qr-route-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    captured = {}

    class FakeImage:
        def save(self, stream, format):
            stream.write(b"PNG")

    def fake_make(public_url):
        captured["public_url"] = public_url
        return FakeImage()

    monkeypatch.setattr("app.qrcode.make", fake_make)

    response = client.get(f"/members/{member_id}/qr")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert captured["public_url"] == "https://cards.carnova.test/m/qr-route-token"
    assert "localhost" not in captured["public_url"].lower()


def test_public_member_card_works_and_uses_safe_fields(client):
    with flask_app.app_context():
        member = Member(
            name="Public Member",
            email="public@example.com",
            phone="555-123-4567",
            member_id="COC-00903",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=2,
            total_changes=3,
            token="public-member-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.flush()
        vehicle = Vehicle(member_id=member.id, make="Toyota", model="Camry", plate="ABC123", current_mileage="40210")
        db.session.add(vehicle)
        db.session.add(
            Redemption(
                member_id=member.id,
                vehicle_id=vehicle.id,
                vehicle=vehicle.display_name,
                mileage="40210",
            )
        )
        db.session.commit()

    response = client.get("/m/public-member-token")

    assert response.status_code == 200
    assert b"Public Member" in response.data
    assert b"public@example.com" not in response.data
    assert b"555-123-4567" not in response.data
    assert b"Toyota Camry" in response.data
    assert b"Monthly Membership" in response.data
    assert b"Service History" in response.data


def test_invalid_public_member_token_returns_404(client):
    response = client.get("/m/does-not-exist")

    assert response.status_code == 404


def test_webhook_created_monthly_member_blocks_second_vehicle_and_hides_add_button(client, monkeypatch):
    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_webhook_limit", "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_webhook_limit",
            "mode": "subscription",
            "customer": "cus_webhook_limit",
            "subscription": "sub_webhook_limit",
            "payment_intent": "pi_webhook_limit",
            "customer_details": {"email": "webhook-limit@example.com", "name": "Webhook Limit"},
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

    webhook_response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )
    assert webhook_response.status_code == 200

    with flask_app.app_context():
        member = Member.query.filter_by(email="webhook-limit@example.com").first()
        assert member is not None
        assert member.plan_name == "Monthly Membership"
        assert member.stripe_price_id == "price_1Txt07R1GwRFNmYeGo3km5vf"

    login_response = client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200

    with flask_app.app_context():
        member = Member.query.filter_by(email="webhook-limit@example.com").first()
        first_vehicle = Vehicle(member_id=member.id, make="Toyota", model="Camry", vin="1HGBH41JXMN109186")
        db.session.add(first_vehicle)
        db.session.commit()
        member_id = member.member_id

    first_vehicle_response = client.post(
        f"/members/{member_id}/vehicles/new",
        data={
            "make": "Honda",
            "model": "Civic",
            "vin": "1HGBH41JXMN109187",
            "plate": "ABC123",
        },
        follow_redirects=True,
    )
    assert first_vehicle_response.status_code == 200
    assert b"Monthly Membership allows only one registered vehicle." in first_vehicle_response.data
    assert Vehicle.query.filter_by(member_id=member.id).count() == 1

    member_detail_response = client.get(f"/members/{member_id}")
    assert member_detail_response.status_code == 200
    assert b"Vehicle Limit Reached" in member_detail_response.data
    assert b"Monthly Membership includes one registered vehicle only." in member_detail_response.data
    assert b"+ Add Vehicle" not in member_detail_response.data


def test_portal_session_is_created_for_monthly_member(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Portal User",
            email="portal@example.com",
            member_id="COC-00006",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="portal-token",
            plan_name="Monthly Membership",
            stripe_customer_id="cus_portal",
            stripe_subscription_id="sub_portal",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    login_response = client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200

    created = {}

    def fake_create(**kwargs):
        created.update(kwargs)
        return {"url": "https://billing.stripe.com/session"}

    monkeypatch.setattr("app.stripe.billing_portal.Session.create", fake_create)

    response = client.post(f"/members/{member_id}/billing/portal", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "https://billing.stripe.com/session"
    assert created["customer"] == "cus_portal"
    assert created["return_url"].endswith(f"/members/{member_id}")


def test_portal_session_rejected_without_stripe_customer_id(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Portal User",
            email="portal-missing@example.com",
            member_id="COC-00007",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="portal-token-missing",
            plan_name="Monthly Membership",
            stripe_subscription_id="sub_portal_missing",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    create_called = {"value": False}

    def fake_create(**kwargs):
        create_called["value"] = True
        return {"url": "https://billing.stripe.com/session"}

    monkeypatch.setattr("app.stripe.billing_portal.Session.create", fake_create)

    response = client.post(f"/members/{member_id}/billing/portal", follow_redirects=True)

    assert response.status_code == 200
    assert create_called["value"] is False
    assert b"not connected to stripe yet" in response.data.lower()


def test_portal_session_rejected_for_non_monthly_plan(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Bronze User",
            email="bronze@example.com",
            member_id="COC-00008",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="bronze-token",
            plan_name="Bronze",
            stripe_customer_id="cus_bronze",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    create_called = {"value": False}

    def fake_create(**kwargs):
        create_called["value"] = True
        return {"url": "https://billing.stripe.com/session"}

    monkeypatch.setattr("app.stripe.billing_portal.Session.create", fake_create)

    response = client.post(f"/members/{member_id}/billing/portal", follow_redirects=True)

    assert response.status_code == 200
    assert create_called["value"] is False
    assert b"monthly membership" in response.data.lower()


def test_portal_session_handles_stripe_api_error(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Portal Error",
            email="portal-error@example.com",
            member_id="COC-00009",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="portal-error-token",
            plan_name="Monthly Membership",
            stripe_customer_id="cus_error",
            stripe_subscription_id="sub_error",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    def fake_create(**kwargs):
        raise stripe.error.StripeError("boom")

    monkeypatch.setattr("app.stripe.billing_portal.Session.create", fake_create)

    response = client.post(f"/members/{member_id}/billing/portal", follow_redirects=True)

    assert response.status_code == 200
    assert b"billing portal right now" in response.data.lower()


@pytest.mark.parametrize("plan_name", ["Bronze", "Silver", "Gold"])
def test_bronze_silver_gold_plans_do_not_create_portal_sessions(client, monkeypatch, plan_name):
    with flask_app.app_context():
        member = Member(
            name=plan_name,
            email=f"{plan_name.lower()}@example.com",
            member_id=f"COC-0001{['0', '1', '2'][['Bronze', 'Silver', 'Gold'].index(plan_name)]}",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token=f"{plan_name.lower()}-token",
            plan_name=plan_name,
            stripe_customer_id="cus_plan",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    create_called = {"value": False}

    def fake_create(**kwargs):
        create_called["value"] = True
        return {"url": "https://billing.stripe.com/session"}

    monkeypatch.setattr("app.stripe.billing_portal.Session.create", fake_create)

    response = client.post(f"/members/{member_id}/billing/portal", follow_redirects=True)

    assert response.status_code == 200
    assert create_called["value"] is False
    assert b"monthly membership" in response.data.lower()


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


def test_checkout_session_recovers_missing_customer_id_for_portal_access(client, monkeypatch):
    def fake_construct_event(payload, signature, secret):
        return {"id": "evt_checkout_recover", "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_recover",
            "mode": "subscription",
            "customer": None,
            "subscription": None,
            "payment_intent": "pi_recover",
            "customer_details": {"email": "recover@example.com", "name": "Recover Customer"},
            "amount_total": 2000,
            "metadata": {},
            "shipping_details": {},
        }}}

    class FakeCheckoutSession:
        @staticmethod
        def list_line_items(session_id, limit=1, expand=None):
            return {"data": [{"price": {"id": "price_1Txt07R1GwRFNmYeGo3km5vf"}}]}

        @staticmethod
        def retrieve(session_id):
            return {"customer": "cus_recovered", "subscription": "sub_recovered"}

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)
    monkeypatch.setattr("app.stripe.checkout.Session", FakeCheckoutSession)

    webhook_response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )
    assert webhook_response.status_code == 200

    with flask_app.app_context():
        member = Member.query.filter_by(email="recover@example.com").first()
        assert member is not None
        assert member.plan_name == "Monthly Membership"
        assert member.stripe_customer_id == "cus_recovered"
        assert member.stripe_subscription_id == "sub_recovered"
        member_id = member.member_id

    login_response = client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200

    created = {}

    def fake_portal_create(**kwargs):
        created.update(kwargs)
        return {"url": "https://billing.stripe.com/session"}

    monkeypatch.setattr("app.stripe.billing_portal.Session.create", fake_portal_create)

    portal_response = client.post(f"/members/{member_id}/billing/portal", follow_redirects=False)
    assert portal_response.status_code == 302
    assert portal_response.headers["Location"] == "https://billing.stripe.com/session"
    assert created["customer"] == "cus_recovered"


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
