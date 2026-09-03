import os
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import stripe

from app import Member, PendingCheckout, ReferralSale, SalesRep, SmsDelivery, db
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, SECRET_KEY="compliance-test", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def test_public_legal_pages_and_optional_consent_form(client, monkeypatch):
    assert client.get("/privacy").status_code == 200
    terms = client.get("/terms")
    assert terms.status_code == 200
    assert b"STOP" in terms.data and b"HELP" in terms.data
    db.session.add(SalesRep(name="Douglas Test", slug="douglas-test")) if False else None
    monkeypatch.setattr("app.stripe_plan_catalog", lambda: [{"name": "Bronze", "changes": 3, "price_id": "price_1Tx6veR1GwRFNmYeUO2goMjz", "price_display": "USD 149.00", "subscription": False}])
    page = client.get("/purchase").data
    assert b'name="sms_consent"' in page
    assert b'checked' not in page
    assert b"/privacy" in page and b"/terms" in page
    assert b"Message and data rates may apply" in page
    assert b"Consent is not required to purchase" in page


def test_privacy_contains_sms_non_sharing_language(client):
    page = client.get("/privacy").data
    assert b"Mobile phone numbers and SMS consent information are not sold" in page
    assert b"not shared with third parties or affiliates for their own marketing" in page
    assert b"Message and data rates may apply" in page


def _make_pending(consent):
    rep = SalesRep(name="Douglas Test", slug="douglas-test")
    db.session.add(rep)
    db.session.flush()
    pending = PendingCheckout(public_token=f"pending-{consent}", sales_rep_id=rep.id, name="Buyer", phone="+15085551234", email=f"buyer-{consent}@example.com", sms_consent=consent, stripe_price_id="price_1Tx6veR1GwRFNmYeUO2goMjz", stripe_checkout_session_id=f"cs-{consent}")
    db.session.add(pending)
    db.session.commit()
    return SimpleNamespace(
        public_token=pending.public_token,
        stripe_checkout_session_id=pending.stripe_checkout_session_id,
        email=pending.email,
        name=pending.name,
        phone=pending.phone,
        stripe_price_id=pending.stripe_price_id,
    )


def test_checked_and_unchecked_purchase_persist_consent(client, monkeypatch):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Douglas Test", slug="douglas-test"))
        db.session.commit()
    client.get("/r/douglas-test")
    session_ids = iter(("cs-form-unchecked", "cs-form-checked"))
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kwargs: type("Checkout", (), {"id": next(session_ids), "url": "https://checkout.test"})())
    data = {"name": "Buyer", "phone": "555-010-0100", "email": "buyer@example.com"}
    assert client.post("/purchase/price_1Tx6veR1GwRFNmYeUO2goMjz", data=data).status_code == 302
    data["email"] = "buyer2@example.com"
    data["sms_consent"] = "on"
    assert client.post("/purchase/price_1Tx6veR1GwRFNmYeUO2goMjz", data=data).status_code == 302
    with flask_app.app_context():
        assert sorted(p.sms_consent for p in PendingCheckout.query.order_by(PendingCheckout.id)) == [False, True]


def test_no_consent_fulfills_member_without_sms(client, monkeypatch):
    with flask_app.app_context():
        pending = _make_pending(False)
    calls = []
    monkeypatch.setattr("app.TwilioClient", lambda *_args: calls.append(True))
    event = {"id": "evt-no-consent", "type": "checkout.session.completed", "data": {"object": {"id": pending.stripe_checkout_session_id, "mode": "payment", "payment_intent": "pi-no-consent", "customer_details": {"email": pending.email, "name": pending.name, "phone": pending.phone}, "amount_total": 14900, "metadata": {"pending_checkout_token": pending.public_token}}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": pending.stripe_price_id}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    with flask_app.app_context():
        assert Member.query.filter_by(email=pending.email).count() == 1
        delivery = SmsDelivery.query.one()
        assert delivery.status == "no_consent"
        assert calls == []


def test_success_redirect_alone_sends_no_sms(client, monkeypatch):
    with flask_app.app_context():
        pending = _make_pending(True)
    calls = []
    monkeypatch.setattr("app.TwilioClient", lambda *_args: calls.append(True))
    assert client.get(f"/purchase/success/{pending.public_token}").status_code == 200
    assert calls == []
