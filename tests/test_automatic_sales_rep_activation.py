import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from werkzeug.security import check_password_hash

from app import Member, ReferralSale, SalesRep, db, sales_rep_activation_hash
from app import app as flask_app


BRONZE_PRICE = "price_1Tx6veR1GwRFNmYeUO2goMjz"
SILVER_PRICE = "price_1TwiJER1GwRFNmYeeFbUdscR"
GOLD_PRICE = "price_1Tx70UR1GwRFNmYePYn1Xrdz"
MONTHLY_PRICE = "price_1TxtO7R1GwRFNmYeGo3km5vf"


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="automatic-activation-test",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def configure_checkout(monkeypatch, price_id, calls, fail_sms=False):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "auth_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
    monkeypatch.setattr(
        "app.stripe.Webhook.construct_event",
        lambda *_args, **_kwargs: {
            "id": configure_checkout.event.get("_event_id"),
            "type": "checkout.session.completed",
            "data": {"object": configure_checkout.event},
        },
    )
    monkeypatch.setattr(
        "app.stripe.checkout.Session.list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": price_id}}]},
    )

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            if fail_sms:
                raise RuntimeError("provider unavailable")
            return type("Message", (), {"sid": "SM-auto-activation"})()

    class Twilio:
        messages = Messages()

    monkeypatch.setattr("app.TwilioClient", lambda _sid, _token: Twilio())


def webhook_event(price_id, email, event_id="evt-auto-activation", session_id="cs-auto-activation", mode="payment", metadata=None):
    return {
        "id": session_id,
        "mode": mode,
        "payment_intent": f"pi-{session_id}",
        "customer": f"cus-{session_id}",
        "subscription": f"sub-{session_id}" if mode == "subscription" else None,
        "customer_details": {"email": email, "name": "New Customer", "phone": "5085550199"},
        "amount_total": 14900,
        "metadata": metadata or {},
        "_event_id": event_id,
    }


def test_each_new_plan_gets_one_activation_sms(client, monkeypatch):
    cases = [
        (BRONZE_PRICE, "bronze-activation@example.com", "payment"),
        (SILVER_PRICE, "silver-activation@example.com", "payment"),
        (GOLD_PRICE, "gold-activation@example.com", "payment"),
        (MONTHLY_PRICE, "monthly-activation@example.com", "subscription"),
    ]
    for index, (price_id, email, mode) in enumerate(cases):
        calls = []
        configure_checkout(monkeypatch, price_id, calls)
        configure_checkout.event = webhook_event(price_id, email, f"evt-{index}", f"cs-{index}", mode)
        response = client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"})
        assert response.status_code == 200
        assert len(calls) == 1
        with flask_app.app_context():
            rep = SalesRep.query.filter_by(login_email=email).one()
            assert rep.activation_sms_status == "sent"
            assert rep.activation_sms_sent_at is not None
            assert rep.activation_token_hash
            assert rep.activation_token_expires_at > datetime.utcnow()
            assert Member.query.filter_by(email=email).one().sales_rep.id == rep.id
            assert "Welcome to Carnova!" in calls[0]["body"]
            assert "qualifying sales" in calls[0]["body"]
            assert "password" not in calls[0]["body"].lower()


def test_activation_sms_token_is_hashed_and_activation_flow_works(client, monkeypatch):
    calls = []
    configure_checkout(monkeypatch, BRONZE_PRICE, calls)
    configure_checkout.event = webhook_event(BRONZE_PRICE, "activation-flow@example.com")
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    activation_url = re.search(r"https?://[^ ]+/sales/activate/([^ ]+)", calls[0]["body"]).group(0)
    raw_token = urlsplit(activation_url).path.rsplit("/", 1)[1]
    with flask_app.app_context():
        rep = SalesRep.query.filter_by(login_email="activation-flow@example.com").one()
        assert rep.activation_token_hash == sales_rep_activation_hash(raw_token)
        assert raw_token not in rep.activation_token_hash
        rep_id = rep.id
    response = client.post(
        activation_url.replace("http://localhost", ""),
        data={"password": "new-password", "password_confirmation": "new-password"},
    )
    assert response.status_code == 302
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert check_password_hash(rep.password_hash, "new-password")
        assert rep.portal_enabled is True
        assert rep.activation_token_hash is None


def test_twilio_failure_does_not_fail_purchase_or_remove_records(client, monkeypatch):
    calls = []
    with flask_app.app_context():
        seller = SalesRep(name="Seller", slug="seller", email="seller@example.com", login_email="seller@example.com")
        db.session.add(seller)
        db.session.commit()
        seller_id = seller.id
    configure_checkout(monkeypatch, BRONZE_PRICE, calls, fail_sms=True)
    configure_checkout.event = webhook_event(
        BRONZE_PRICE,
        "failed-activation@example.com",
        metadata={"sales_rep_id": str(seller_id)},
    )
    response = client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"})
    assert response.status_code == 200
    with flask_app.app_context():
        member = Member.query.filter_by(email="failed-activation@example.com").one()
        rep = SalesRep.query.filter_by(member_id=member.id).one()
        sale = ReferralSale.query.one()
        assert sale.sales_rep_id == seller_id
        assert sale.member_id == member.id
        assert rep.activation_sms_status == "failed"
        assert "provider unavailable" in rep.activation_sms_error
        assert rep.activation_token_hash


def test_duplicate_event_and_checkout_retry_do_not_resend_activation_sms(client, monkeypatch):
    calls = []
    configure_checkout(monkeypatch, BRONZE_PRICE, calls)
    configure_checkout.event = webhook_event(BRONZE_PRICE, "retry-activation@example.com", "evt-first", "cs-retry-activation")
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    configure_checkout.event = webhook_event(BRONZE_PRICE, "retry-activation@example.com", "evt-second", "cs-retry-activation")
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    configure_checkout.event = webhook_event(BRONZE_PRICE, "retry-activation@example.com", "evt-first", "cs-retry-activation")
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    assert len(calls) == 1
    with flask_app.app_context():
        assert SalesRep.query.count() == 1


def test_existing_sales_rep_during_purchase_is_not_sent_activation_sms(client, monkeypatch):
    calls = []
    with flask_app.app_context():
        db.session.add(SalesRep(
            name="Existing Portal Rep",
            slug="existing-portal-rep",
            email="existing-portal@example.com",
            login_email="existing-portal@example.com",
            password_hash="existing-hash",
            portal_enabled=True,
        ))
        db.session.commit()
    configure_checkout(monkeypatch, BRONZE_PRICE, calls)
    configure_checkout.event = webhook_event(BRONZE_PRICE, "existing-portal@example.com")
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    assert calls == []


def test_existing_activation_state_is_not_resent_when_linked(client, monkeypatch):
    calls = []
    with flask_app.app_context():
        member = Member(name="Existing", email="existing-activation@example.com", member_id="COC-P3-1", expiration_date=date.today() + timedelta(days=365), token="p3-existing")
        rep = SalesRep(name="Existing", slug="existing-activation", email=member.email, login_email=member.email, activation_token_hash="existing-token-hash", activation_token_expires_at=datetime.utcnow() + timedelta(hours=12), activation_sms_status="sent", member=member)
        db.session.add(rep)
        db.session.commit()
    configure_checkout(monkeypatch, BRONZE_PRICE, calls)
    configure_checkout.event = webhook_event(BRONZE_PRICE, "existing-activation@example.com")
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    assert calls == []
