import os
from datetime import date, datetime

import pytest
from werkzeug.security import generate_password_hash

from app import Admin, Member, ReferralSale, SalesRep, db
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, SECRET_KEY="portal-test", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_rep(name, email, enabled=True, active=True):
    rep = SalesRep(name=name, slug=name.lower().replace(" ", "-"), login_email=email, password_hash=generate_password_hash("correct-password"), portal_enabled=enabled, active=active)
    db.session.add(rep)
    db.session.commit()
    return rep


def make_sale(rep, email, commission, status="pending", changes=3):
    member = Member(name=email.split("@")[0].title(), email=email, member_id=f"COC-{rep.id}{commission}", expiration_date=date.today(), total_changes=changes, remaining_changes=changes, token=f"token-{rep.id}-{commission}")
    db.session.add(member)
    db.session.flush()
    sale = ReferralSale(sales_rep_id=rep.id, member_id=member.id, stripe_event_id=f"evt-{rep.id}-{commission}", stripe_checkout_session_id=f"cs-{rep.id}-{commission}", stripe_price_id="price", plan_name={3: "Bronze", 5: "Silver", 8: "Gold"}[changes], oil_changes=changes, commission_cents=commission, commission_status=status)
    db.session.add(sale)
    db.session.commit()
    return sale


def login(client, email, password="correct-password"):
    return client.post("/sales/login", data={"email": email, "password": password})


def test_unauthenticated_dashboard_redirects_and_valid_login_works(client):
    assert client.get("/sales/dashboard").status_code == 302
    with flask_app.app_context():
        make_rep("Douglas Test", "douglas@example.com")
    response = login(client, "douglas@example.com")
    assert response.status_code == 302
    assert response.location.endswith("/sales/dashboard")
    assert client.get("/sales/dashboard").status_code == 200


def test_invalid_inactive_and_disabled_logins_are_denied(client):
    with flask_app.app_context():
        make_rep("Inactive", "inactive@example.com", active=False)
        make_rep("Disabled", "disabled@example.com", enabled=False)
    assert login(client, "inactive@example.com").status_code == 200
    assert login(client, "disabled@example.com").status_code == 200
    assert login(client, "inactive@example.com", "wrong-password").status_code == 200


def test_dashboard_is_scoped_to_logged_in_rep_and_hides_revenue_and_stripe_ids(client):
    with flask_app.app_context():
        rep_a = make_rep("Douglas Test", "douglas@example.com")
        rep_b = make_rep("Other Rep", "other@example.com")
        make_sale(rep_a, "douglas-customer@example.com", 1000)
        make_sale(rep_b, "other-customer@example.com", 2000)
    login(client, "douglas@example.com")
    response = client.get("/sales/dashboard")
    assert response.status_code == 200
    assert b"Douglas-Customer" in response.data
    assert b"Other-customer" not in response.data
    assert b"Revenue" not in response.data
    assert b"sale_amount" not in response.data
    assert b"cs-" not in response.data and b"evt-" not in response.data and b"price" not in response.data
    assert b"other@example.com" not in response.data
    assert b"Other Rep" not in response.data
    assert b"Other Rep" not in client.get("/sales/dashboard?rep_id=2").data


def test_commission_totals_and_package_counts_use_persisted_sales(client):
    with flask_app.app_context():
        rep = make_rep("Douglas Test", "douglas@example.com")
        make_sale(rep, "bronze@example.com", 1000, changes=3)
        make_sale(rep, "silver@example.com", 1500, status="paid", changes=5)
        make_sale(rep, "gold@example.com", 2000, status="paid", changes=8)
    login(client, "douglas@example.com")
    page = client.get("/sales/dashboard").data
    assert b"$10.00" in page and b"$35.00" in page and b"$45.00" in page
    assert b"Bronze" in page and b"Silver" in page and b"Gold" in page


def test_referral_link_uses_logged_in_rep_slug_and_logout_clears_access(client):
    with flask_app.app_context():
        make_rep("Douglas Test", "douglas@example.com")
    login(client, "douglas@example.com")
    page = client.get("/sales/dashboard").data
    assert b"/r/douglas-test" in page
    assert client.post("/sales/logout").status_code == 302
    assert client.get("/sales/dashboard").status_code == 302


def test_sales_rep_cannot_mark_paid_and_admin_session_isolated(client):
    with flask_app.app_context():
        rep = make_rep("Douglas Test", "douglas@example.com")
        sale = make_sale(rep, "customer@example.com", 1000)
        sale_id = sale.id
    login(client, "douglas@example.com")
    assert client.post(f"/admin/referral-sales/{sale_id}/paid").status_code == 302
    with flask_app.app_context():
        assert db.session.get(ReferralSale, sale_id).commission_status == "pending"
    with client.session_transaction() as saved:
        saved.pop("sales_rep_id", None)
        saved["admin_id"] = 1
    assert client.get("/sales/dashboard").status_code == 302
    assert client.post(f"/admin/referral-sales/{sale_id}/paid").status_code == 302
    with flask_app.app_context():
        assert db.session.get(ReferralSale, sale_id).commission_status == "paid"


def test_admin_can_provision_hashed_portal_credentials(client):
    with flask_app.app_context():
        admin = Admin(email="admin@example.com", password_hash=generate_password_hash("admin-password"))
        db.session.add(admin)
        db.session.add(SalesRep(name="Douglas Test", slug="douglas-test"))
        db.session.commit()
        rep_id = SalesRep.query.one().id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    response = client.post(f"/admin/sales-reps/{rep_id}/portal-credentials", data={"login_email": "portal@example.com", "login_password": "portal-password", "portal_enabled": "1"})
    assert response.status_code == 302
    with flask_app.app_context():
        rep = db.session.get(SalesRep, rep_id)
        assert rep.login_email == "portal@example.com"
        assert rep.password_hash != "portal-password"
        assert rep.portal_enabled is True
        assert login(client, "portal@example.com", "portal-password").status_code == 302
