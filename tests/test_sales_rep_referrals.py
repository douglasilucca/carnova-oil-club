import os
from datetime import date, datetime, timedelta

import pytest
import stripe

from app import Member, PendingCheckout, ReferralSale, SalesRep, Vehicle, STRIPE_PLANS, db
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, SECRET_KEY="test-secret", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_dummy"
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_member():
    member = Member(name="Buyer", email="buyer@example.com", member_id="COC-10000", expiration_date=date.today() + timedelta(days=365), total_changes=0, remaining_changes=0, token="buyer-token", status="completed")
    db.session.add(member)
    db.session.commit()
    return member


def test_referral_slug_capture_and_inactive_or_invalid_rejection(client):
    with flask_app.app_context():
        db.session.add_all([SalesRep(name="Diego", slug="diego"), SalesRep(name="Inactive", slug="inactive", active=False)])
        db.session.commit()
    response = client.get("/r/diego")
    assert response.status_code == 302
    with client.session_transaction() as saved:
        assert saved["sales_rep_referral"]["sales_rep_id"] == 1
    assert client.get("/r/missing").status_code == 404
    assert client.get("/r/inactive").status_code == 404


def test_referral_lands_on_new_customer_sales_page(client):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Diego", slug="diego"))
        db.session.commit()
    response = client.get("/r/diego", follow_redirects=True)
    assert response.status_code == 200
    assert b"JOIN THE CARNOVA OIL CLUB" in response.data
    assert b"Your Carnova Oil Club Specialist: <strong>Diego</strong>" in response.data


def test_purchase_page_uses_rep_name_and_polished_package_copy(client, monkeypatch):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Douglas Test", slug="douglas-test"))
        db.session.commit()
    monkeypatch.setattr("app.stripe_plan_catalog", lambda: [
        {"name": "Bronze", "changes": 3, "price_id": "price_1Tx6veR1GwRFNmYeUO2goMjz", "price_display": "USD 149.00", "subscription": False},
        {"name": "Silver", "changes": 5, "price_id": "price_1TwiJER1GwRFNmYeeFbUdscR", "price_display": "USD 229.00", "subscription": False},
        {"name": "Gold", "changes": 8, "price_id": "price_1Tx70UR1GwRFNmYePYn1Xrdz", "price_display": "USD 329.00", "subscription": False},
    ])
    response = client.get("/r/douglas-test", follow_redirects=True)
    assert response.status_code == 200
    assert b"Your Carnova Oil Club Specialist: <strong>Douglas Test</strong>" in response.data
    assert b"Your Carnova Oil Club Specialist: <strong>douglas-test</strong>" not in response.data
    assert b"BRONZE" in response.data and b"3 Synthetic Oil Changes" in response.data
    assert b"$149" in response.data and b"Only $49.67 per oil change" in response.data
    assert b"SILVER" in response.data and b"5 Synthetic Oil Changes" in response.data
    assert b"$229" in response.data and b"Only $45.80 per oil change" in response.data
    assert b"MOST POPULAR" in response.data
    assert b"GOLD" in response.data and b"8 Synthetic Oil Changes" in response.data
    assert b"$329" in response.data and b"Only $41.13 per oil change" in response.data
    assert b"BEST VALUE" in response.data
    for field in (b'name="name"', b'name="phone"', b'name="email"'):
        assert field in response.data
    for field in (b'name="vehicle_year"', b'name="vehicle_make"', b'name="vehicle_model"'):
        assert field not in response.data
    assert b"/purchase/price_1Tx6veR1GwRFNmYeUO2goMjz" in response.data
    assert b"/purchase/price_1TwiJER1GwRFNmYeeFbUdscR" in response.data
    assert b"/purchase/price_1Tx70UR1GwRFNmYePYn1Xrdz" in response.data


def test_latest_valid_referral_wins(client):
    with flask_app.app_context():
        db.session.add_all([SalesRep(name="Diego", slug="diego"), SalesRep(name="Marco", slug="marco")])
        db.session.commit()
    client.get("/r/diego")
    client.get("/r/marco")
    with client.session_transaction() as saved:
        assert saved["sales_rep_referral"]["sales_rep_id"] == 2


def test_expired_attribution_is_cleared(client):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Diego", slug="diego"))
        db.session.commit()
    with client.session_transaction() as saved:
        saved["sales_rep_referral"] = {"sales_rep_id": 1, "captured_at": (datetime.utcnow() - timedelta(days=31)).timestamp()}
    with flask_app.app_context():
        make_member()
    assert client.get("/m/buyer-token/buy").status_code == 200
    with client.session_transaction() as saved:
        assert "sales_rep_referral" not in saved


def test_checkout_metadata_uses_server_rep_and_not_browser_values(client, monkeypatch):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Diego", slug="diego"))
        db.session.commit()
        make_member()
    client.get("/r/diego")
    captured = {}
    class Checkout:
        url = "https://checkout.test"
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kwargs: captured.setdefault("kwargs", kwargs) and Checkout())
    response = client.post("/m/buyer-token/buy/price_1Tx6veR1GwRFNmYeUO2goMjz", data={"price_id": "attacker", "commission_cents": "999999"})
    assert response.status_code == 302
    assert captured["kwargs"]["metadata"] == {"member_id": "1", "plan_price_id": "price_1Tx6veR1GwRFNmYeUO2goMjz", "sales_rep_id": "1"}


def test_new_customer_checkout_stores_validated_data_and_opaque_metadata(client, monkeypatch):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Diego", slug="diego"))
        db.session.commit()
    client.get("/r/diego")
    captured = {}
    class Checkout:
        id = "cs-new"
        url = "https://checkout.test/new"
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kwargs: captured.setdefault("kwargs", kwargs) and Checkout())
    response = client.post("/purchase/price_1Tx6veR1GwRFNmYeUO2goMjz", data={"name": "New Buyer", "phone": "555-0100", "email": "new@example.com", "plan_key": "attacker", "sales_rep_id": "999"})
    assert response.status_code == 302
    with flask_app.app_context():
        pending = PendingCheckout.query.one()
        assert pending.email == "new@example.com"
        assert pending.vehicle_year == ""
        assert pending.vehicle_make == ""
        assert pending.vehicle_model == ""
        assert pending.stripe_price_id == "price_1Tx6veR1GwRFNmYeUO2goMjz"
        assert captured["kwargs"]["metadata"] == {"pending_checkout_token": pending.public_token, "sales_rep_id": "1"}


def test_abandoned_new_customer_checkout_does_not_leave_pending_record(client, monkeypatch):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Diego", slug="diego"))
        db.session.commit()
    client.get("/r/diego")
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kwargs: (_ for _ in ()).throw(stripe.error.InvalidRequestError("cancelled", "request")))
    response = client.post("/purchase/price_1Tx6veR1GwRFNmYeUO2goMjz", data={"name": "New Buyer", "phone": "555-0100", "email": "new@example.com"})
    assert response.status_code == 302
    with flask_app.app_context():
        assert PendingCheckout.query.count() == 0


@pytest.mark.parametrize("price_id,changes,commission", [("price_1Tx6veR1GwRFNmYeUO2goMjz", 3, 1000), ("price_1TwiJER1GwRFNmYeeFbUdscR", 5, 1500), ("price_1Tx70UR1GwRFNmYePYn1Xrdz", 8, 2000)])
def test_new_customer_webhook_creates_member_vehicle_and_referral_sale(client, monkeypatch, price_id, changes, commission):
    with flask_app.app_context():
        rep = SalesRep(name="Diego", slug="diego")
        db.session.add(rep)
        db.session.flush()
        pending = PendingCheckout(public_token=f"pending-{changes}", sales_rep_id=rep.id, name="New Buyer", phone="555-0100", email="new@example.com", stripe_price_id=price_id, stripe_checkout_session_id=f"cs-new-{changes}")
        db.session.add(pending)
        db.session.commit()
        pending_token = pending.public_token
    event = {"id": f"evt-new-{changes}", "type": "checkout.session.completed", "data": {"object": {"id": f"cs-new-{changes}", "mode": "payment", "payment_intent": f"pi-new-{changes}", "customer_details": {"email": "new@example.com", "name": "New Buyer", "phone": "555-0100"}, "amount_total": 4999, "metadata": {"pending_checkout_token": pending_token, "sales_rep_id": "999"}}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": price_id}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    with flask_app.app_context():
        member = Member.query.filter_by(email="new@example.com").one()
        sale = ReferralSale.query.one()
        pending = PendingCheckout.query.filter_by(public_token=pending_token).one()
        assert member.total_changes == changes
        assert Vehicle.query.filter_by(member_id=member.id).count() == 0
        assert sale.member_id == member.id
        assert sale.sales_rep_id == 1
        assert sale.commission_cents == commission
        assert pending.status == "fulfilled"
    assert client.get(f"/purchase/success/{pending_token}").status_code == 302


@pytest.mark.parametrize("price_id,commission", [("price_1Tx6veR1GwRFNmYeUO2goMjz", 1000), ("price_1TwiJER1GwRFNmYeeFbUdscR", 1500), ("price_1Tx70UR1GwRFNmYePYn1Xrdz", 2000), ("price_1TxtO7R1GwRFNmYeGo3km5vf", 0)])
def test_verified_checkout_creates_expected_commission_once(client, monkeypatch, price_id, commission):
    with flask_app.app_context():
        rep = SalesRep(name="Diego", slug="diego")
        db.session.add(rep)
        member = make_member()
        rep_id = rep.id
        member_id = member.id
    event = {"id": f"evt-{price_id}", "type": "checkout.session.completed", "data": {"object": {"id": f"cs-{price_id}", "mode": "subscription" if price_id.startswith("price_1Txt") else "payment", "payment_intent": f"pi-{price_id}", "customer_details": {"email": member.email, "name": member.name}, "amount_total": 4999, "metadata": {"member_id": str(member_id), "sales_rep_id": str(rep_id)}}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": price_id}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    with flask_app.app_context():
        sale = ReferralSale.query.one()
        assert sale.commission_cents == commission
        assert sale.stripe_checkout_session_id == f"cs-{price_id}"


def test_invalid_signature_creates_no_sale(client, monkeypatch):
    with flask_app.app_context():
        db.session.add(SalesRep(name="Diego", slug="diego"))
        db.session.commit()
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(stripe.error.SignatureVerificationError("bad", "sig")))
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "bad"}).status_code == 400
    with flask_app.app_context():
        assert ReferralSale.query.count() == 0


def test_verified_purchase_without_referral_creates_no_sale(client, monkeypatch):
    with flask_app.app_context():
        member = make_member()
        member_id = member.id
    event = {"id": "evt-no-referral", "type": "checkout.session.completed", "data": {"object": {"id": "cs-no-referral", "mode": "payment", "payment_intent": "pi-no-referral", "customer_details": {"email": "buyer@example.com", "name": "Buyer"}, "amount_total": 4999, "metadata": {"member_id": str(member_id)}}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(stripe.checkout.Session, "list_line_items", lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx6veR1GwRFNmYeUO2goMjz"}}]})
    assert client.post("/stripe/webhook", data=b"payload", headers={"Stripe-Signature": "valid"}).status_code == 200
    with flask_app.app_context():
        assert ReferralSale.query.count() == 0


def test_marking_commission_paid_preserves_sale_history(client, monkeypatch):
    with flask_app.app_context():
        rep = SalesRep(name="Diego", slug="diego")
        db.session.add(rep)
        member = make_member()
        sale = ReferralSale(sales_rep=rep, member=member, stripe_event_id="evt-paid", stripe_checkout_session_id="cs-paid", stripe_price_id="price", plan_name="Bronze", oil_changes=3, commission_cents=1000)
        db.session.add(sale)
        db.session.commit()
        sale_id = sale.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    response = client.post(f"/admin/referral-sales/{sale_id}/paid")
    assert response.status_code == 302
    with flask_app.app_context():
        paid = db.session.get(ReferralSale, sale_id)
        assert paid.commission_status == "paid"
        assert paid.commission_cents == 1000
        assert paid.stripe_checkout_session_id == "cs-paid"
