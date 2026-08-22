import json
import os
from datetime import date, datetime, time, timedelta

import pytest
import stripe

from app import (
    Admin,
    Appointment,
    Member,
    Redemption,
    ReminderLog,
    StripeEvent,
    Vehicle,
    current_member_status,
    db,
    monthly_membership_defaults,
    google_wallet_api_call,
    google_wallet_upsert_member_object,
    run_all_reminders,
    run_appointment_reminders,
    run_renewal_reminders,
    run_unused_benefit_reminders,
    send_unused_benefit_reminder_email,
)
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
    assert b"Add to Google Wallet" in response.data


def test_invalid_public_member_token_returns_404(client):
    response = client.get("/m/does-not-exist")

    assert response.status_code == 404


def test_public_google_wallet_add_redirects_to_save_url(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Wallet Member",
            email="wallet@example.com",
            member_id="COC-00910",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="wallet-member-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()

    monkeypatch.setattr("app.sync_member_google_wallet_save_url", lambda _member: "https://pay.google.com/gp/v/save/test-token")

    response = client.post("/m/wallet-member-token/wallet/add", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "https://pay.google.com/gp/v/save/test-token"


def test_public_google_wallet_add_failure_redirects_back_to_member_card(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Wallet Member Fallback",
            email="wallet-fallback@example.com",
            member_id="COC-00911",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="wallet-member-fallback-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()

    monkeypatch.setattr("app.sync_member_google_wallet_save_url", lambda _member: None)

    response = client.post("/m/wallet-member-fallback-token/wallet/add", follow_redirects=True)

    assert response.status_code == 200
    assert b"Google Wallet is unavailable right now" in response.data
    assert b"Wallet Member Fallback" in response.data


def test_public_google_wallet_add_rejects_unsafe_external_redirect(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Wallet Member Unsafe Redirect",
            email="wallet-unsafe@example.com",
            member_id="COC-00914",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="wallet-member-unsafe-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()

    monkeypatch.setattr("app.sync_member_google_wallet_save_url", lambda _member: "https://evil.example/save/token")

    response = client.post("/m/wallet-member-unsafe-token/wallet/add", follow_redirects=True)

    assert response.status_code == 200
    assert b"Google Wallet is unavailable right now" in response.data
    assert b"Wallet Member Unsafe Redirect" in response.data


def test_public_google_wallet_add_get_does_not_trigger_wallet_sync(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Wallet Member Read Only",
            email="wallet-read-only@example.com",
            member_id="COC-00913",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="wallet-member-read-only-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()

    called = {"value": False}

    def fake_save_url(_member):
        called["value"] = True
        return "https://pay.google.com/gp/v/save/test-token"

    monkeypatch.setattr("app.sync_member_google_wallet_save_url", fake_save_url)

    response = client.get("/m/wallet-member-read-only-token/wallet/add", follow_redirects=False)

    assert response.status_code == 405
    assert called["value"] is False


def test_google_wallet_upsert_reuses_single_token_for_patch_then_post(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Token Reuse Member",
            email="token-reuse@example.com",
            member_id="COC-00912",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="token-reuse-member",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.id

    token_fetches = {"count": 0}
    api_access_tokens = []

    def fake_access_token():
        token_fetches["count"] += 1
        return "cached-wallet-token"

    def fake_api_call(method, endpoint, payload=None, access_token=None):
        api_access_tokens.append(access_token)
        if method == "PATCH":
            return 404, {}
        return 201, {}

    monkeypatch.setattr("app.google_wallet_access_token", fake_access_token)
    monkeypatch.setattr("app.google_wallet_api_call", fake_api_call)

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member.query.get(member_id)
        assert google_wallet_upsert_member_object(member) is True
    assert token_fetches["count"] == 1
    assert api_access_tokens == ["cached-wallet-token", "cached-wallet-token"]


def test_google_wallet_api_call_uses_provided_token_without_fetch(client, monkeypatch):
    token_fetches = {"count": 0}
    seen = {"authorization": ""}

    class FakeResponse:
        def __init__(self, request_obj):
            self.request_obj = request_obj
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_access_token():
        token_fetches["count"] += 1
        return "should-not-be-used"

    def fake_urlopen(request_obj, timeout=10):
        seen["authorization"] = request_obj.headers.get("Authorization", "")
        return FakeResponse(request_obj)

    monkeypatch.setattr("app.google_wallet_access_token", fake_access_token)
    monkeypatch.setattr("app.urllib_request.urlopen", fake_urlopen)

    status, payload = google_wallet_api_call(
        "PATCH",
        "https://walletobjects.googleapis.com/walletobjects/v1/genericObject/test",
        payload={"id": "test"},
        access_token="provided-token",
    )

    assert status == 200
    assert payload == {}
    assert seen["authorization"] == "Bearer provided-token"
    assert token_fetches["count"] == 0


def test_google_wallet_api_call_fetches_oauth_bearer_token_when_not_provided(client, monkeypatch):
    token_fetches = {"count": 0}
    seen = {"authorization": ""}

    class FakeResponse:
        def __init__(self, request_obj):
            self.request_obj = request_obj
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_access_token():
        token_fetches["count"] += 1
        return "oauth-access-token"

    def fake_urlopen(request_obj, timeout=10):
        seen["authorization"] = request_obj.headers.get("Authorization", "")
        return FakeResponse(request_obj)

    monkeypatch.setattr("app.google_wallet_access_token", fake_access_token)
    monkeypatch.setattr("app.urllib_request.urlopen", fake_urlopen)

    status, payload = google_wallet_api_call(
        "PATCH",
        "https://walletobjects.googleapis.com/walletobjects/v1/genericObject/test",
        payload={"id": "test"},
    )

    assert status == 200
    assert payload == {}
    assert seen["authorization"] == "Bearer oauth-access-token"
    assert token_fetches["count"] == 1


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


def test_reset_all_customer_data_requires_exact_confirmation_and_preserves_admin_and_stripe_records(client):
    with flask_app.app_context():
        member = Member(
            name="Reset Member",
            email="reset@example.com",
            member_id="COC-00904",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=2,
            total_changes=3,
            token="reset-member-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.flush()

        vehicle = Vehicle(member_id=member.id, make="Toyota", model="Camry", plate="RESET1")
        db.session.add(vehicle)
        db.session.flush()

        db.session.add(
            Appointment(
                member_id=member.id,
                vehicle_id=vehicle.id,
                appointment_date=date.today() + timedelta(days=1),
                appointment_time=datetime.now().time(),
                service_type="Oil Change",
            )
        )
        db.session.add(
            Redemption(
                member_id=member.id,
                vehicle_id=vehicle.id,
                vehicle=vehicle.display_name,
                mileage="40210",
            )
        )
        db.session.add(
            ReminderLog(
                member_id=member.id,
                reminder_type="renewal",
                reminder_key="reset-test",
            )
        )
        db.session.add(StripeEvent(event_id="evt_keep", event_type="checkout.session.completed"))
        db.session.commit()

    login_response = client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )
    assert login_response.status_code == 200

    confirmation_response = client.get("/admin/reset-test-data")
    assert confirmation_response.status_code == 200
    assert b"DELETE ALL CUSTOMER DATA" in confirmation_response.data

    rejected_response = client.post(
        "/admin/reset-test-data",
        data={"confirmation_text": "DELETE ALL CUSTOMER DATA ",},
        follow_redirects=True,
    )
    assert rejected_response.status_code == 200
    assert b"Type DELETE ALL CUSTOMER DATA exactly" in rejected_response.data

    with flask_app.app_context():
        assert Member.query.count() == 1
        assert Vehicle.query.count() == 1
        assert Appointment.query.count() == 1
        assert Redemption.query.count() == 1
        assert ReminderLog.query.count() == 1
        assert StripeEvent.query.count() == 1
        assert Admin.query.count() == 1

    reset_response = client.post(
        "/admin/reset-test-data",
        data={"confirmation_text": "DELETE ALL CUSTOMER DATA"},
        follow_redirects=True,
    )
    assert reset_response.status_code == 200
    assert b"All customer data reset complete:" in reset_response.data

    with flask_app.app_context():
        assert Member.query.count() == 0
        assert Vehicle.query.count() == 0
        assert Appointment.query.count() == 0
        assert Redemption.query.count() == 0
        assert ReminderLog.query.count() == 0
        assert StripeEvent.query.count() == 1
        assert Admin.query.count() == 1


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


def test_redeem_calls_wallet_sync_after_commit(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Redeem Wallet",
            email="redeem-wallet@example.com",
            member_id="COC-02001",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=2,
            total_changes=2,
            token="redeem-wallet-token",
            plan_name="Bronze",
            subscription_status=None,
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    called = {"count": 0}

    def fake_sync(_member):
        called["count"] += 1
        return True

    monkeypatch.setattr("app.sync_member_google_wallet_object", fake_sync)

    response = client.post(
        f"/members/{member_id}/redeem",
        data={"vehicle": "2020 Toyota Camry", "mileage": "50210", "vin_last8": "ABCD1234"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Oil change redeemed successfully." in response.data
    assert called["count"] == 1


def test_undo_calls_wallet_sync_after_commit(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Undo Wallet",
            email="undo-wallet@example.com",
            member_id="COC-02002",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=0,
            total_changes=1,
            token="undo-wallet-token",
            plan_name="Bronze",
            subscription_status=None,
        )
        db.session.add(member)
        db.session.flush()
        db.session.add(Redemption(member_id=member.id, vehicle="Test Vehicle", mileage="12345"))
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    called = {"count": 0}

    def fake_sync(_member):
        called["count"] += 1
        return True

    monkeypatch.setattr("app.sync_member_google_wallet_object", fake_sync)

    response = client.post(f"/members/{member_id}/undo", follow_redirects=True)

    assert response.status_code == 200
    assert b"Last redemption was undone." in response.data
    assert called["count"] == 1


def test_edit_member_calls_wallet_sync_after_commit(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Edit Wallet",
            email="edit-wallet@example.com",
            member_id="COC-02003",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="edit-wallet-token",
            plan_name="Bronze",
            subscription_status=None,
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.member_id

    client.post(
        "/login",
        data={"email": "admin@carnovaoil.com", "password": "ChangeMe123!"},
        follow_redirects=True,
    )

    called = {"count": 0}

    def fake_sync(_member):
        called["count"] += 1
        return True

    monkeypatch.setattr("app.sync_member_google_wallet_object", fake_sync)

    response = client.post(
        f"/members/{member_id}/edit",
        data={
            "name": "Edit Wallet Updated",
            "email": "edit-wallet@example.com",
            "phone": "555-111-2222",
            "expiration_date": (date.today() + timedelta(days=300)).isoformat(),
            "total_changes": "4",
            "remaining_changes": "2",
            "status": "active",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Member information updated." in response.data
    assert called["count"] == 1


def test_invoice_paid_benefit_reset_calls_wallet_sync_after_commit(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Benefit Reset Wallet",
            email="benefit-reset-wallet@example.com",
            member_id="COC-02004",
            expiration_date=date.today() - timedelta(days=1),
            remaining_changes=0,
            total_changes=3,
            token="benefit-reset-wallet-token",
            plan_name="Monthly Membership",
            subscription_status="past_due",
            stripe_subscription_id="sub_benefit_reset",
            stripe_customer_id="cus_benefit_reset",
            benefit_period_start=date.today() - timedelta(days=366),
            benefit_period_end=date.today() - timedelta(days=1),
        )
        db.session.add(member)
        db.session.commit()

    called = {"count": 0}

    def fake_wallet_sync(updated_member):
        if updated_member.member_id == "COC-02004":
            called["count"] += 1
        return True

    monkeypatch.setattr("app.sync_member_google_wallet_object", fake_wallet_sync)

    now_ts = int(datetime.utcnow().timestamp())

    def fake_construct_event(payload, signature, secret):
        return {
            "id": "evt_benefit_reset_wallet",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_benefit_reset_wallet",
                    "subscription": "sub_benefit_reset",
                    "customer": "cus_benefit_reset",
                    "customer_details": {"email": "benefit-reset-wallet@example.com"},
                    "status_transitions": {"paid_at": now_ts},
                }
            },
        }

    monkeypatch.setattr("app.stripe.Webhook.construct_event", fake_construct_event)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"test": True}),
        headers={"Stripe-Signature": "test-signature"},
    )

    assert response.status_code == 200
    assert called["count"] == 1


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


def _create_member_for_reminders(
    email,
    expiration_days=365,
    status="active",
    remaining_changes=3,
    total_changes=3,
):
    member = Member(
        name=email.split("@")[0].replace(".", " ").title(),
        email=email,
        member_id=f"COC-{abs(hash(email)) % 100000:05d}",
        expiration_date=date.today() + timedelta(days=expiration_days),
        remaining_changes=remaining_changes,
        total_changes=total_changes,
        status=status,
        token=f"token-{abs(hash(email)) % 100000}",
        plan_name="Monthly Membership",
        subscription_status="active" if status == "active" else None,
    )
    db.session.add(member)
    db.session.commit()
    return member


def test_renewal_reminder_sent_at_30_days(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("renewal30@example.com", expiration_days=30)
    member.expiration_date = today + timedelta(days=30)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_renewal_reminders(reference_date=today)

    assert summary["sent"] == 1
    assert ReminderLog.query.filter_by(member_id=member.id, reminder_type="renewal").count() == 1


def test_renewal_reminder_sent_at_7_days(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("renewal7@example.com", expiration_days=7)
    member.expiration_date = today + timedelta(days=7)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_renewal_reminders(reference_date=today)

    assert summary["sent"] == 1
    assert ReminderLog.query.filter_by(member_id=member.id, reminder_type="renewal").count() == 1


def test_renewal_reminder_sent_at_1_day(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("renewal1@example.com", expiration_days=1)
    member.expiration_date = today + timedelta(days=1)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_renewal_reminders(reference_date=today)

    assert summary["sent"] == 1
    assert ReminderLog.query.filter_by(member_id=member.id, reminder_type="renewal").count() == 1


def test_duplicate_renewal_reminder_is_prevented(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("renewal-dup@example.com", expiration_days=7)
    member.expiration_date = today + timedelta(days=7)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    first_summary = run_renewal_reminders(reference_date=today)
    second_summary = run_renewal_reminders(reference_date=today)

    assert first_summary["sent"] == 1
    assert second_summary["sent"] == 0
    assert second_summary["skipped"] >= 1
    assert ReminderLog.query.filter_by(member_id=member.id, reminder_type="renewal").count() == 1


def test_unused_benefit_reminder_sent_after_120_days(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("unused120@example.com", expiration_days=180)
    old_redemption = Redemption(
        member_id=member.id,
        redeemed_at=datetime.combine(today - timedelta(days=121), datetime.min.time()),
        vehicle="Test Vehicle",
        mileage="12345",
    )
    db.session.add(old_redemption)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_unused_benefit_reminders(reference_date=today)

    assert summary["sent"] == 1
    assert ReminderLog.query.filter_by(member_id=member.id, reminder_type="unused_benefit").count() == 1


def test_unused_benefit_reminder_not_repeated_within_90_days(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("unused90@example.com", expiration_days=180)
    old_redemption = Redemption(
        member_id=member.id,
        redeemed_at=datetime.combine(today - timedelta(days=130), datetime.min.time()),
        vehicle="Test Vehicle",
        mileage="12345",
    )
    db.session.add(old_redemption)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    first_summary = run_unused_benefit_reminders(reference_date=today)
    second_summary = run_unused_benefit_reminders(reference_date=today + timedelta(days=30))

    assert first_summary["sent"] == 1
    assert second_summary["sent"] == 0
    assert second_summary["skipped"] >= 1
    assert ReminderLog.query.filter_by(member_id=member.id, reminder_type="unused_benefit").count() == 1


def test_unused_benefit_inactive_member_skipped(client, monkeypatch):
    _create_member_for_reminders("unused-inactive@example.com", status="cancelled", remaining_changes=3)
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_unused_benefit_reminders(reference_date=date.today())

    assert summary["sent"] == 0
    assert summary["skipped"] >= 1
    assert ReminderLog.query.filter_by(reminder_type="unused_benefit").count() == 0


def test_unused_benefit_member_with_zero_remaining_changes_skipped(client, monkeypatch):
    _create_member_for_reminders("unused-zero@example.com", remaining_changes=0, total_changes=3)
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_unused_benefit_reminders(reference_date=date.today())

    assert summary["sent"] == 0
    assert summary["skipped"] >= 1
    assert ReminderLog.query.filter_by(reminder_type="unused_benefit").count() == 0


def test_unused_benefit_email_with_vehicle_uses_schedule_cta(client, monkeypatch):
    member = _create_member_for_reminders("email-vehicle@example.com", expiration_days=180)
    db.session.add(
        Vehicle(
            member_id=member.id,
            year="2021",
            make="Toyota",
            model="Camry",
            plate="ABC123",
            color="Black",
        )
    )
    db.session.commit()

    captured = {}

    def fake_send_smtp_email(recipient, subject, text_body, html_body=None):
        captured["recipient"] = recipient
        captured["subject"] = subject
        captured["text_body"] = text_body
        captured["html_body"] = html_body
        return True

    monkeypatch.setattr("app.send_smtp_email", fake_send_smtp_email)

    assert send_unused_benefit_reminder_email(member) is True
    assert "Your Carnova Oil Club Benefits Are Waiting" in captured["html_body"]
    assert "Schedule My Oil Change" in captured["html_body"]
    assert f"/m/{member.token}/appointments/new" in captured["html_body"]
    assert "2021 Toyota Camry" in captured["html_body"]
    assert "License Plate" in captured["html_body"]
    assert "ABC123" in captured["html_body"]


def test_unused_benefit_email_without_vehicle_uses_register_cta(client, monkeypatch):
    member = _create_member_for_reminders("email-no-vehicle@example.com", expiration_days=180)

    captured = {}

    def fake_send_smtp_email(recipient, subject, text_body, html_body=None):
        captured["recipient"] = recipient
        captured["subject"] = subject
        captured["text_body"] = text_body
        captured["html_body"] = html_body
        return True

    monkeypatch.setattr("app.send_smtp_email", fake_send_smtp_email)

    assert send_unused_benefit_reminder_email(member) is True
    assert "Complete Your Membership Setup" in captured["html_body"]
    assert "Register My Vehicle" in captured["html_body"]
    assert f"/m/{member.token}/vehicle/register" in captured["html_body"]
    assert "Registration Required" in captured["html_body"]


def test_renewal_outside_window_increments_skip_reason(client, monkeypatch):
    today = date.today()
    _create_member_for_reminders("renewal-outside@example.com", expiration_days=20)
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_renewal_reminders(reference_date=today)

    assert summary["sent"] == 0
    assert summary["skip_reasons"]["outside_reminder_window"] == 1
    assert summary["skipped"] == 1


def test_inactive_renewal_member_increments_skip_reason(client, monkeypatch):
    today = date.today()
    _create_member_for_reminders("renewal-inactive@example.com", expiration_days=30, status="cancelled")
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_renewal_reminders(reference_date=today)

    assert summary["sent"] == 0
    assert summary["skip_reasons"]["inactive_member"] == 1
    assert summary["skipped"] == 1


def test_unused_benefit_recent_redemption_increments_skip_reason(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("unused-recent@example.com", expiration_days=180)
    db.session.add(
        Redemption(
            member_id=member.id,
            redeemed_at=datetime.combine(today - timedelta(days=20), datetime.min.time()),
            vehicle="Recent Vehicle",
            mileage="41000",
        )
    )
    db.session.commit()
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_unused_benefit_reminders(reference_date=today)

    assert summary["sent"] == 0
    assert summary["skip_reasons"]["used_within_last_120_days"] == 1
    assert summary["skipped"] == 1


def test_unused_benefit_90_day_cooldown_increments_skip_reason(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("unused-cooldown@example.com", expiration_days=180)
    db.session.add(
        ReminderLog(
            member_id=member.id,
            reminder_type="unused_benefit",
            reminder_key="unused-benefit:existing",
            sent_at=datetime.combine(today - timedelta(days=20), datetime.min.time()),
        )
    )
    db.session.commit()
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_unused_benefit_reminders(reference_date=today)

    assert summary["sent"] == 0
    assert summary["skip_reasons"]["reminder_sent_within_90_days"] == 1
    assert summary["skipped"] == 1


def test_zero_remaining_changes_increments_skip_reason(client, monkeypatch):
    _create_member_for_reminders("unused-zero-reason@example.com", expiration_days=180, remaining_changes=0, total_changes=3)
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_unused_benefit_reminders(reference_date=date.today())

    assert summary["sent"] == 0
    assert summary["skip_reasons"]["zero_remaining_changes"] == 1
    assert summary["skipped"] == 1


def test_reminder_summary_totals_match_reason_counts(client, monkeypatch):
    today = date.today()
    _create_member_for_reminders("totals-outside@example.com", expiration_days=20)
    _create_member_for_reminders("totals-zero@example.com", expiration_days=180, remaining_changes=0, total_changes=3)
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_all_reminders(reference_date=today)

    assert summary["renewal"]["skipped"] == sum(summary["renewal"]["skip_reasons"].values())
    assert summary["unused_benefit"]["skipped"] == sum(summary["unused_benefit"]["skip_reasons"].values())
    assert summary["appointment_morning"]["skipped"] == sum(summary["appointment_morning"]["skip_reasons"].values())
    assert summary["sent"] == summary["renewal"]["sent"] + summary["unused_benefit"]["sent"] + summary["appointment_morning"]["sent"]
    assert summary["failed"] == summary["renewal"]["failed"] + summary["unused_benefit"]["failed"] + summary["appointment_morning"]["failed"]
    assert summary["skipped"] == summary["renewal"]["skipped"] + summary["unused_benefit"]["skipped"] + summary["appointment_morning"]["skipped"]


def test_appointment_morning_reminder_sends_on_correct_day(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("appt-morning@example.com", expiration_days=180)
    db.session.add(
        Appointment(
            member_id=member.id,
            appointment_date=today,
            appointment_time=time(10, 30),
            status="scheduled",
            service_type="Oil Change",
        )
    )
    db.session.commit()

    monkeypatch.setenv("APPOINTMENT_REMINDER_TIMEZONE", "America/New_York")
    monkeypatch.setenv("APPOINTMENT_REMINDER_MORNING_HOUR", "8")
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    summary = run_appointment_reminders(reference_datetime=datetime.combine(today, time(8, 15)))

    assert summary["sent"] == 1
    log = ReminderLog.query.filter_by(member_id=member.id, reminder_type="appointment_morning").first()
    assert log is not None
    assert f"appointment:" in log.reminder_key


def test_appointment_morning_reminder_skips_cancelled_completed_no_show(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("appt-status@example.com", expiration_days=180)
    for status_value in ["cancelled", "completed", "no_show"]:
        db.session.add(
            Appointment(
                member_id=member.id,
                appointment_date=today,
                appointment_time=time(11, 0),
                status=status_value,
                service_type="Oil Change",
            )
        )
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)
    summary = run_appointment_reminders(reference_datetime=datetime.combine(today, time(9, 0)))

    assert summary["sent"] == 0
    assert summary["skip_reasons"]["status_not_eligible"] == 3
    assert ReminderLog.query.filter_by(reminder_type="appointment_morning").count() == 0


def test_appointment_morning_reminder_prevents_duplicates(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("appt-dup@example.com", expiration_days=180)
    appointment = Appointment(
        member_id=member.id,
        appointment_date=today,
        appointment_time=time(9, 45),
        status="confirmed",
        service_type="Oil Change",
    )
    db.session.add(appointment)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    first_summary = run_appointment_reminders(reference_datetime=datetime.combine(today, time(8, 30)))
    second_summary = run_appointment_reminders(reference_datetime=datetime.combine(today, time(9, 30)))

    assert first_summary["sent"] == 1
    assert second_summary["sent"] == 0
    assert second_summary["skip_reasons"]["duplicate_reminder"] == 1
    assert ReminderLog.query.filter_by(member_id=member.id, reminder_type="appointment_morning").count() == 1


def test_appointment_morning_reminders_keep_multiple_member_appointments_independent(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("appt-multi@example.com", expiration_days=180)
    first = Appointment(
        member_id=member.id,
        appointment_date=today,
        appointment_time=time(10, 0),
        status="scheduled",
        service_type="Oil Change",
    )
    second = Appointment(
        member_id=member.id,
        appointment_date=today,
        appointment_time=time(14, 0),
        status="confirmed",
        service_type="Oil Change",
    )
    db.session.add(first)
    db.session.add(second)
    db.session.commit()

    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)
    summary = run_appointment_reminders(reference_datetime=datetime.combine(today, time(8, 45)))

    assert summary["sent"] == 2
    keys = [
        item.reminder_key
        for item in ReminderLog.query.filter_by(member_id=member.id, reminder_type="appointment_morning").all()
    ]
    assert any(f"appointment:{first.id}:" in key for key in keys)
    assert any(f"appointment:{second.id}:" in key for key in keys)


def test_appointment_morning_reminder_email_failure_is_non_blocking(client, monkeypatch):
    today = date.today()
    first_member = _create_member_for_reminders("appt-fail-1@example.com", expiration_days=180)
    second_member = _create_member_for_reminders("appt-fail-2@example.com", expiration_days=180)

    db.session.add(
        Appointment(
            member_id=first_member.id,
            appointment_date=today,
            appointment_time=time(9, 0),
            status="scheduled",
            service_type="Oil Change",
        )
    )
    db.session.add(
        Appointment(
            member_id=second_member.id,
            appointment_date=today,
            appointment_time=time(10, 0),
            status="scheduled",
            service_type="Oil Change",
        )
    )
    db.session.commit()

    def fake_send_smtp_email(recipient, *_args, **_kwargs):
        return recipient != "appt-fail-1@example.com"

    monkeypatch.setattr("app.send_smtp_email", fake_send_smtp_email)

    summary = run_appointment_reminders(reference_datetime=datetime.combine(today, time(8, 50)))

    assert summary["sent"] == 1
    assert summary["failed"] == 1
    assert ReminderLog.query.filter_by(reminder_type="appointment_morning").count() == 1


def test_appointment_morning_reminder_email_includes_expected_details_and_links(client, monkeypatch):
    today = date.today()
    member = _create_member_for_reminders("appt-email-details@example.com", expiration_days=180)
    vehicle = Vehicle(
        member_id=member.id,
        year="2022",
        make="Honda",
        model="Civic",
        plate="XYZ123",
    )
    db.session.add(vehicle)
    db.session.flush()

    appointment = Appointment(
        member_id=member.id,
        vehicle_id=vehicle.id,
        appointment_date=today,
        appointment_time=time(13, 15),
        status="confirmed",
        service_type="Full Synthetic Oil Change",
    )
    db.session.add(appointment)
    db.session.commit()

    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    captured = {}

    def fake_send_smtp_email(recipient, subject, text_body, html_body=None):
        captured["recipient"] = recipient
        captured["subject"] = subject
        captured["text_body"] = text_body
        captured["html_body"] = html_body
        return True

    monkeypatch.setattr("app.send_smtp_email", fake_send_smtp_email)

    summary = run_appointment_reminders(reference_datetime=datetime.combine(today, time(8, 20)))

    assert summary["sent"] == 1
    assert captured["recipient"] == member.email
    assert appointment.appointment_date.strftime("%B %d, %Y") in captured["text_body"]
    assert appointment.appointment_time.strftime("%I:%M %p") in captured["text_body"]
    assert "Full Synthetic Oil Change" in captured["text_body"]
    assert "2022 Honda Civic" in captured["text_body"]
    assert f"/m/{member.token}" in captured["text_body"]
    assert f"/m/{member.token}/appointments/new" in captured["text_body"]
