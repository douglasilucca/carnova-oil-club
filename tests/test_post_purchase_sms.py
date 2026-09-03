import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import stripe

from app import Member, PendingCheckout, ReferralSale, SalesRep, SmsDelivery, db
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, SECRET_KEY="sms-test", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    os.environ["BASE_URL"] = "https://example.test"
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def pending_checkout(price_id, phone="(508) 555-1234", sms_consent=True):
    rep = SalesRep(name="Douglas Test", slug="douglas-test")
    db.session.add(rep)
    db.session.flush()
    pending = PendingCheckout(
        public_token="pending-sms",
        sales_rep_id=rep.id,
        name="SMS Buyer",
        phone=phone,
        email="sms@example.com",
        sms_consent=sms_consent,
        stripe_price_id=price_id,
        stripe_checkout_session_id="cs-sms",
    )
    db.session.add(pending)
    db.session.commit()
    return SimpleNamespace(
        public_token=pending.public_token,
        stripe_checkout_session_id=pending.stripe_checkout_session_id,
        email=pending.email,
        name=pending.name,
        phone=pending.phone,
    )


def webhook_event(pending, price_id="price_1Tx6veR1GwRFNmYeUO2goMjz", event_id="evt-sms"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": pending.stripe_checkout_session_id,
            "mode": "payment",
            "payment_intent": "pi-sms",
            "customer_details": {"email": pending.email, "name": pending.name, "phone": "+19995551234"},
            "amount_total": 14900,
            "metadata": {"pending_checkout_token": pending.public_token, "sales_rep_id": "999"},
        }},
    }


class FakeMessage:
    sid = "SM_test_123"


class FakeMessages:
    def __init__(self, calls):
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeMessage()


class FakeTwilio:
    def __init__(self, _sid, _token, calls):
        self.messages = FakeMessages(calls)


@pytest.fixture
def stripe_line_items(monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]})


def test_new_bronze_purchase_sends_one_normalized_sms_with_member_url(client, monkeypatch):
    with flask_app.app_context():
        pending = pending_checkout("price_1Tx6veR1GwRFNmYeUO2goMjz")
    calls = []
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda sid, token: FakeTwilio(sid, token, calls))
    event = webhook_event(pending)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    with flask_app.app_context():
        member = Member.query.filter_by(email="sms@example.com").one()
        delivery = SmsDelivery.query.one()
        assert delivery.status == "sent"
        assert delivery.phone_number == "+15085551234"
        assert f"/m/{member.token}" in calls[0]["body"]
        assert calls[0]["to"] == "+15085551234"
        assert calls[0]["body"] == f"Carnova Oil Club: Your membership is ready! Access your membership and add it to Apple Wallet or Google Wallet: https://example.test/m/{member.token} Please keep this message for future access."


@pytest.mark.parametrize(
    "price_id,event_id",
    [
        ("price_1TwiJER1GwRFNmYeeFbUdscR", "evt-sms-silver"),
        ("price_1Tx70UR1GwRFNmYePYn1Xrdz", "evt-sms-gold"),
    ],
)
def test_new_silver_and_gold_purchases_send_one_sms(client, monkeypatch, price_id, event_id):
    with flask_app.app_context():
        pending = pending_checkout(price_id)
    calls = []
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda sid, token: FakeTwilio(sid, token, calls))
    event = webhook_event(pending, price_id=price_id, event_id=event_id)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": price_id}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    assert len(calls) == 1
    with flask_app.app_context():
        assert SmsDelivery.query.one().status == "sent"


def test_sms_not_sent_for_invalid_phone(client, monkeypatch):
    with flask_app.app_context():
        pending = pending_checkout("price_1Tx6veR1GwRFNmYeUO2goMjz", phone="not-a-phone")
    calls = []
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda sid, token: FakeTwilio(sid, token, calls))
    event = webhook_event(pending)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    with flask_app.app_context():
        assert calls == []
        assert SmsDelivery.query.one().status == "not_sent"


def test_invalid_signature_and_success_redirect_do_not_send_sms(client, monkeypatch):
    with flask_app.app_context():
        pending = pending_checkout("price_1Tx6veR1GwRFNmYeUO2goMjz")
    calls = []
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda sid, token: FakeTwilio(sid, token, calls))
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(stripe.error.SignatureVerificationError("bad", "sig")))
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "bad"}).status_code == 400
    assert client.get(f"/purchase/success/{pending.public_token}").status_code == 200
    assert calls == []
    with flask_app.app_context():
        assert Member.query.count() == 0
        assert SmsDelivery.query.count() == 0


def test_webhook_replay_sends_sms_once(client, monkeypatch):
    with flask_app.app_context():
        pending = pending_checkout("price_1Tx6veR1GwRFNmYeUO2goMjz")
    calls = []
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda sid, token: FakeTwilio(sid, token, calls))
    event = webhook_event(pending)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]})
    client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"})
    client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"})
    assert len(calls) == 1
    with flask_app.app_context():
        assert SmsDelivery.query.count() == 1


def test_twilio_failure_persists_without_rolling_back_membership(client, monkeypatch):
    with flask_app.app_context():
        pending = pending_checkout("price_1Tx6veR1GwRFNmYeUO2goMjz")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    def fail_client(_sid, _token):
        class FailedMessages:
            def create(self, **_kwargs):
                raise RuntimeError("provider unavailable")
        class Client:
            messages = FailedMessages()
        return Client()
    monkeypatch.setattr("app.TwilioClient", fail_client)
    event = webhook_event(pending)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    with flask_app.app_context():
        assert Member.query.filter_by(email="sms@example.com").count() == 1
        assert SmsDelivery.query.one().status == "failed"


def test_existing_member_repurchase_does_not_send_membership_ready_sms(client, monkeypatch):
    with flask_app.app_context():
        member = Member(name="Existing", email="existing@example.com", phone="+15085551234", member_id="COC-90000", expiration_date=date.today() + timedelta(days=365), total_changes=3, remaining_changes=3, token="existing-token")
        db.session.add(member)
        db.session.commit()
        member_id = member.id
    calls = []
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15085550000")
    monkeypatch.setattr("app.TwilioClient", lambda sid, token: FakeTwilio(sid, token, calls))
    event = {"id": "evt-repurchase", "type": "checkout.session.completed", "data": {"object": {"id": "cs-repurchase", "mode": "payment", "payment_intent": "pi-repurchase", "customer_details": {"email": member.email, "name": member.name, "phone": member.phone}, "amount_total": 14900, "metadata": {"member_id": str(member_id)}}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    assert calls == []
    with flask_app.app_context():
        assert SmsDelivery.query.count() == 0
