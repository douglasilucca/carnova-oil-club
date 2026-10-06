import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import stripe

from app import (
    Appointment,
    Member,
    PendingCheckout,
    SalesRep,
    SmsDelivery,
    STRIPE_PLANS,
    db,
    process_checkout_completed,
)
from app import app as flask_app


BRONZE_PRICE = "price_1Tx6veR1GwRFNmYeUO2goMjz"
SILVER_PRICE = "price_1TwiJER1GwRFNmYeeFbUdscR"
GOLD_PRICE = "price_1Tx70UR1GwRFNmYePYn1Xrdz"
MONTHLY_PRICE = "price_1TxtO7R1GwRFNmYeGo3km5vf"


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="required-phone-test",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    os.environ["BASE_URL"] = "https://example.test"
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_member(*, phone="+15085550100", token="card-access-token", consent=True):
    member = Member(
        name="Phone Buyer",
        email="phone-buyer@example.test",
        phone=phone,
        member_id="COC-PHONE-1",
        expiration_date=date.today() + timedelta(days=365),
        total_changes=3,
        remaining_changes=2,
        plan_name="Bronze",
        token=token,
    )
    db.session.add(member)
    db.session.flush()
    pending = PendingCheckout(
        public_token="pending-phone-checkout",
        member_id=member.id,
        name=member.name,
        phone=phone,
        email=member.email,
        sms_consent=consent,
        stripe_price_id=BRONZE_PRICE,
        stripe_checkout_session_id="cs-phone-checkout",
        status="fulfilled",
    )
    db.session.add(pending)
    db.session.commit()
    return member


def admin_login(client):
    with client.session_transaction() as saved:
        saved["admin_id"] = 1


def test_new_customer_checkout_requires_stripe_phone_collection(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(id="cs-new", url="https://checkout.test"),
    )

    response = client.post(
        f"/purchase/{BRONZE_PRICE}",
        data={"name": "New Buyer", "phone": "5085550100", "email": "new@example.test"},
    )

    assert response.status_code == 302
    assert calls[0]["phone_number_collection"] == {"enabled": True}
    assert calls[0]["line_items"] == [{"price": BRONZE_PRICE, "quantity": 1}]
    assert calls[0]["mode"] == "payment"


def test_existing_member_checkout_requires_phone_collection(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Existing Buyer", email="existing@example.test", member_id="COC-PHONE-2",
            phone="+15085550101", expiration_date=date.today() + timedelta(days=365), token="existing-phone-token",
        )
        db.session.add(member)
        db.session.commit()
        member_token = member.token
    calls = []
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(id="cs-existing", url="https://checkout.test"),
    )

    response = client.post(f"/m/{member_token}/buy/{SILVER_PRICE}")

    assert response.status_code == 302
    assert calls[0]["phone_number_collection"] == {"enabled": True}
    assert calls[0]["mode"] == "payment"


def test_monthly_checkout_requires_phone_collection(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Monthly Buyer", email="monthly@example.test", member_id="COC-PHONE-3",
            phone="+15085550102", expiration_date=date.today() + timedelta(days=365), token="monthly-phone-token",
        )
        db.session.add(member)
        db.session.commit()
        member_token = member.token
    calls = []
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        lambda **kwargs: calls.append(kwargs) or SimpleNamespace(id="cs-monthly", url="https://checkout.test"),
    )

    response = client.post(f"/m/{member_token}/buy/{MONTHLY_PRICE}")

    assert response.status_code == 302
    assert calls[0]["phone_number_collection"] == {"enabled": True}
    assert calls[0]["mode"] == "subscription"
    assert calls[0]["line_items"] == [{"price": MONTHLY_PRICE, "quantity": 1}]


def make_checkout_object(*, email, phone=None, pending_phone="+15085550103"):
    pending = PendingCheckout(
        public_token="pending-webhook-phone",
        name="Webhook Buyer",
        phone=pending_phone,
        email=email,
        sms_consent=False,
        stripe_price_id=BRONZE_PRICE,
        stripe_checkout_session_id="cs-webhook-phone",
    )
    db.session.add(pending)
    db.session.commit()
    details = {"email": email, "name": "Webhook Buyer"}
    if phone:
        details["phone"] = phone
    obj = {
        "id": "cs-webhook-phone",
        "mode": "payment",
        "payment_intent": "pi-webhook-phone",
        "customer_details": details,
        "metadata": {"pending_checkout_token": pending.public_token},
        "amount_total": 14900,
    }
    return pending, obj


def test_stripe_customer_phone_is_preferred_for_new_member(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    with flask_app.app_context():
        _, obj = make_checkout_object(email="stripe-phone@example.test", phone="+14155550123")
        monkeypatch.setattr(
            stripe.checkout.Session,
            "list_line_items",
            lambda *_args, **_kwargs: {"data": [{"price": {"id": BRONZE_PRICE}}]},
        )
        member, created = process_checkout_completed(obj)
        db.session.commit()
        assert created is True
        assert member.phone == "+14155550123"
        assert member.sales_rep.phone == "+14155550123"


def test_legacy_pending_phone_fallback_remains_supported(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    with flask_app.app_context():
        _, obj = make_checkout_object(email="legacy-phone@example.test")
        monkeypatch.setattr(
            stripe.checkout.Session,
            "list_line_items",
            lambda *_args, **_kwargs: {"data": [{"price": {"id": BRONZE_PRICE}}]},
        )
        member, created = process_checkout_completed(obj)
        db.session.commit()
        assert created is True
        assert member.phone == "+15085550103"


def test_existing_linked_sales_rep_phone_is_not_overwritten(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    with flask_app.app_context():
        member = Member(
            name="Returning Buyer", email="returning@example.test", phone="+15085550105",
            member_id="COC-PHONE-4", expiration_date=date.today() + timedelta(days=365),
            token="returning-phone-token",
        )
        rep = SalesRep(
            name="Existing Rep", slug="existing-phone-rep", phone="+15085550106",
            email=member.email, login_email=member.email, member=member,
        )
        db.session.add_all([member, rep])
        db.session.commit()
        member_id = member.id
    event = {
        "id": "cs-existing-linked-phone",
        "mode": "payment",
        "payment_intent": "pi-existing-linked-phone",
        "customer_details": {"email": "returning@example.test", "name": "Returning Buyer", "phone": "+14155550123"},
        "metadata": {"member_id": str(member_id)},
        "amount_total": 14900,
    }
    monkeypatch.setattr(
        stripe.checkout.Session,
        "list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": BRONZE_PRICE}}]},
    )

    with flask_app.app_context():
        member, created = process_checkout_completed(event)
        db.session.commit()
        assert created is False
        assert member.phone == "+14155550123"
        assert member.sales_rep.phone == "+15085550106"


def test_checkout_phone_collection_does_not_change_prices_or_credits():
    assert set(STRIPE_PLANS) == {BRONZE_PRICE, SILVER_PRICE, GOLD_PRICE, MONTHLY_PRICE}
    assert [STRIPE_PLANS[key]["changes"] for key in (BRONZE_PRICE, SILVER_PRICE, GOLD_PRICE)] == [3, 5, 8]
    assert STRIPE_PLANS[MONTHLY_PRICE]["changes"] == 3


def test_member_page_has_both_wallet_actions(client):
    with flask_app.app_context():
        member = make_member()
        token = member.token

    response = client.get(f"/m/{token}")

    assert response.status_code == 200
    assert f"/m/{token}/apple-wallet".encode() in response.data
    assert f"/m/{token}/wallet/add".encode() in response.data


def test_purchase_succeeds_after_membership_sms_provider_failure(client, monkeypatch):
    with flask_app.app_context():
        pending, obj = make_checkout_object(
            email="sms-failure@example.test",
            phone="+15085550104",
        )
        pending.sms_consent = True
        db.session.commit()
        obj["customer"] = "cus-test"
        obj["metadata"] = {"pending_checkout_token": pending.public_token}
        event = {
            "id": "evt-sms-failure",
            "type": "checkout.session.completed",
            "data": {"object": obj},
        }
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "test-account")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(
        stripe.checkout.Session,
        "list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": BRONZE_PRICE}}]},
    )
    monkeypatch.setattr("app.send_automatic_sales_rep_activation", lambda _member: None)
    monkeypatch.setattr("app.sync_member_google_wallet_object", lambda _member: None)
    monkeypatch.setattr("app.apple_wallet_mark_pass_updated", lambda _member: None)
    monkeypatch.setattr("app.send_membership_confirmation_email", lambda _member: True)

    class FailedTwilio:
        class Messages:
            def create(self, **_kwargs):
                raise RuntimeError("provider unavailable")
        messages = Messages()

    monkeypatch.setattr("app.TwilioClient", lambda *_args: FailedTwilio())

    response = client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"})

    assert response.status_code == 200
    with flask_app.app_context():
        assert Member.query.filter_by(email="sms-failure@example.test").count() == 1
        delivery = SmsDelivery.query.one()
        assert delivery.status == "failed"
        assert delivery.attempts == 1


def test_admin_resend_requires_authentication_and_post(client):
    with flask_app.app_context():
        member = make_member()
        member_id = member.member_id

    unauthenticated = client.post(f"/members/{member_id}/card-sms/resend")
    assert unauthenticated.status_code == 302
    assert unauthenticated.location.endswith("/login")
    admin_login(client)
    assert client.get(f"/members/{member_id}/card-sms/resend").status_code == 405


def test_admin_resend_sends_consenting_member_card_url_and_retries_existing_row(client, monkeypatch, caplog, capsys):
    with flask_app.app_context():
        member = make_member()
        rep = SalesRep(
            name="Phone Buyer Rep", slug="phone-buyer-rep", phone=member.phone,
            email=member.email, login_email=member.email, member=member,
            activation_sms_status="failed", activation_sms_error="activation failure",
        )
        existing_delivery = SmsDelivery(
            member_id=member.id, purpose="membership_ready", phone_number=member.phone,
            provider="twilio", status="failed", attempts=1, last_error="provider error",
        )
        db.session.add_all([rep, existing_delivery])
        db.session.commit()
        member_id = member.member_id
        member_pk = member.id
        original_values = (member.plan_name, member.total_changes, member.remaining_changes)
        rep_id = rep.id
    sent = []

    class FakeTwilio:
        class Messages:
            def create(self, **kwargs):
                sent.append(kwargs)
                return SimpleNamespace(sid="SM-fake")
        messages = Messages()

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "test-account")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda *_args: FakeTwilio())
    monkeypatch.setattr("app.sync_member_google_wallet_object", lambda *_args: pytest.fail("Wallet sync must not run"))
    monkeypatch.setattr("app.apple_wallet_mark_pass_updated", lambda *_args: pytest.fail("Wallet sync must not run"))
    admin_login(client)

    response = client.post(f"/members/{member_id}/card-sms/resend")

    assert response.status_code == 302
    assert len(sent) == 1
    assert sent[0]["to"] == "+15085550100"
    assert "/m/card-access-token" in sent[0]["body"]
    assert "card-access-token" not in caplog.text
    assert "card-access-token" not in capsys.readouterr().out
    with flask_app.app_context():
        assert Member.query.count() == 1
        member = db.session.get(Member, member_pk)
        delivery = SmsDelivery.query.one()
        rep = db.session.get(SalesRep, rep_id)
        assert delivery.status == "sent"
        assert delivery.attempts == 2
        assert (member.plan_name, member.total_changes, member.remaining_changes) == original_values
        assert rep.activation_sms_status == "failed"
        assert rep.activation_sms_error == "activation failure"


def test_admin_resend_no_consent_does_not_call_provider(client, monkeypatch):
    with flask_app.app_context():
        member = make_member(consent=False)
        member_id = member.member_id
        delivery = SmsDelivery(
            member_id=member.id, purpose="membership_ready", phone_number=member.phone,
            provider="twilio", status="failed", attempts=1, last_error="prior failure",
        )
        db.session.add(delivery)
        db.session.commit()
    calls = []
    monkeypatch.setattr("app.TwilioClient", lambda *_args: calls.append(True))
    admin_login(client)

    response = client.post(f"/members/{member_id}/card-sms/resend")

    assert response.status_code == 302
    assert calls == []
    with flask_app.app_context():
        delivery = SmsDelivery.query.one()
        assert delivery.status == "no_consent"
        assert delivery.attempts == 2


def test_admin_resend_missing_phone_does_not_call_provider_and_persists_attempt(client, monkeypatch):
    with flask_app.app_context():
        member = make_member(phone="")
        member_id = member.member_id
    calls = []
    monkeypatch.setattr("app.TwilioClient", lambda *_args: calls.append(True))
    admin_login(client)

    response = client.post(f"/members/{member_id}/card-sms/resend")

    assert response.status_code == 302
    assert calls == []
    with flask_app.app_context():
        delivery = SmsDelivery.query.one()
        assert delivery.status == "not_sent"
        assert delivery.attempts == 1
        assert delivery.last_error == "Invalid US phone number"


def test_admin_resend_provider_failure_is_non_fatal(client, monkeypatch):
    with flask_app.app_context():
        member = make_member()
        member_id = member.member_id
    class FailedTwilio:
        class Messages:
            def create(self, **_kwargs):
                raise RuntimeError("provider unavailable")
        messages = Messages()
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "test-account")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda *_args: FailedTwilio())
    admin_login(client)

    response = client.post(f"/members/{member_id}/card-sms/resend")

    assert response.status_code == 302
    with flask_app.app_context():
        assert Member.query.count() == 1
        delivery = SmsDelivery.query.one()
        assert delivery.status == "failed"
        assert delivery.attempts == 1
        assert delivery.last_error == "RuntimeError"


def test_admin_copy_link_is_authenticated_post_and_does_not_rotate_member_token(client, caplog):
    with flask_app.app_context():
        member = make_member(token="copy-link-secret-token")
        member_id = member.member_id
        original_token = member.token

    assert client.post(f"/members/{member_id}/card-access-link").status_code == 302
    admin_login(client)
    assert client.get(f"/members/{member_id}/card-access-link").status_code == 405
    response = client.post(f"/members/{member_id}/card-access-link")

    assert response.status_code == 200
    assert b"CARD ACCESS &amp; SMS" in response.data
    assert b"https://example.test/m/copy-link-secret-token" in response.data
    assert "copy-link-secret-token" not in caplog.text
    with flask_app.app_context():
        assert Member.query.one().token == original_token


def test_admin_recovery_page_shows_phone_and_sms_status(client):
    with flask_app.app_context():
        member = make_member()
        member_id = member.member_id
    admin_login(client)

    response = client.get(f"/members/{member_id}")

    assert response.status_code == 200
    assert b"CARD ACCESS &amp; SMS" in response.data
    assert b"Available" in response.data
    assert b"Not Sent" in response.data
    assert b"Resend Card SMS" in response.data
    assert b"Generate / Copy Card Access Link" in response.data


def test_weekday_appointment_slots_remain_available(client):
    from app import appointment_slots_for_day

    appointment_day = date.today() + timedelta(days=1)
    while appointment_day.weekday() >= 5:
        appointment_day += timedelta(days=1)
    with flask_app.app_context():
        assert appointment_slots_for_day(appointment_day)
