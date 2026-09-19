from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from werkzeug.security import generate_password_hash

from app import (
    Admin,
    COMMISSION_CENTS_BY_CHANGES,
    Member,
    ReferralSale,
    SalesRep,
    calculate_monthly_sales_bonus,
    db,
)
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, SECRET_KEY="bonus-test", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def test_monthly_bonus_boundaries():
    month = datetime(2026, 9, 15)
    expected = {
        0: (0, 0, 10),
        9: (9, 0, 10),
        10: (10, 5000, 20),
        19: (19, 5000, 20),
        20: (20, 15000, 30),
        29: (29, 15000, 30),
        30: (30, 30000, 40),
        39: (39, 30000, 40),
        40: (40, 50000, None),
        45: (45, 50000, None),
    }
    for count, (expected_count, expected_bonus, next_threshold) in expected.items():
        sales = [SimpleNamespace(commission_status="pending", created_at=month) for _ in range(count)]
        result = calculate_monthly_sales_bonus(sales, month=month)
        assert result["qualifying_sales"] == expected_count
        assert result["bonus_cents"] == expected_bonus
        assert result["next_threshold"] == next_threshold
        assert result["additional_sales_needed"] == (0 if next_threshold is None else next_threshold - count)
        assert result["max_reached"] is (next_threshold is None)


def test_only_current_month_qualifying_sales_count():
    month = datetime(2026, 9, 15)
    sales = [
        SimpleNamespace(commission_status="pending", created_at=datetime(2026, 9, 1)),
        SimpleNamespace(commission_status="paid", created_at=datetime(2026, 9, 30)),
        SimpleNamespace(commission_status="pending", created_at=datetime(2026, 8, 31)),
        SimpleNamespace(commission_status="cancelled", created_at=datetime(2026, 9, 10)),
        SimpleNamespace(commission_status="refunded", created_at=datetime(2026, 9, 11)),
        SimpleNamespace(commission_status="failed", created_at=datetime(2026, 9, 12)),
        SimpleNamespace(commission_status="duplicate", created_at=datetime(2026, 9, 13)),
    ]
    result = calculate_monthly_sales_bonus(sales, month=month)
    assert result["qualifying_sales"] == 2
    assert result["bonus_cents"] == 0


def test_existing_per_sale_commissions_remain_unchanged():
    assert COMMISSION_CENTS_BY_CHANGES == {3: 1000, 5: 1500, 8: 2000}


def add_sale(rep, index, created_at, status="pending", commission_cents=1000):
    member = Member(
        name=f"Customer {index}",
        email=f"customer-{index}@example.com",
        member_id=f"COC-BONUS-{index}",
        expiration_date=created_at.date(),
        token=f"bonus-token-{index}",
    )
    db.session.add(member)
    db.session.flush()
    sale = ReferralSale(
        sales_rep_id=rep.id,
        member_id=member.id,
        stripe_event_id=f"evt-bonus-{index}",
        stripe_checkout_session_id=f"cs-bonus-{index}",
        stripe_price_id="price-bonus",
        plan_name="Bronze",
        oil_changes=3,
        commission_cents=commission_cents,
        commission_status=status,
        created_at=created_at,
    )
    db.session.add(sale)


def make_rep():
    rep = SalesRep(
        name="Bonus Rep",
        slug="bonus-rep",
        login_email="bonus@example.com",
        password_hash=generate_password_hash("correct-password"),
        portal_enabled=True,
    )
    db.session.add(rep)
    db.session.flush()
    return rep


def test_sales_rep_dashboard_displays_bonus_and_progress(client):
    with flask_app.app_context():
        rep = make_rep()
        month = datetime.utcnow().replace(day=10, hour=12, minute=0, second=0, microsecond=0)
        for index in range(12):
            add_sale(rep, index, month + timedelta(minutes=index))
        db.session.commit()
    assert client.post("/sales/login", data={"email": "bonus@example.com", "password": "correct-password"}).status_code == 302
    response = client.get("/sales/dashboard")
    assert response.status_code == 200
    assert b"12 qualifying sales this month" in response.data
    assert b"$50.00" in response.data
    assert b"8 more sales to unlock $150" in response.data


def test_admin_sales_rep_detail_displays_bonus_and_progress(client):
    with flask_app.app_context():
        db.session.add(Admin(email="admin@example.com", password_hash=generate_password_hash("admin-password")))
        rep = make_rep()
        month = datetime.utcnow().replace(day=10, hour=12, minute=0, second=0, microsecond=0)
        for index in range(20):
            add_sale(rep, index, month + timedelta(minutes=index))
        db.session.commit()
        rep_id = rep.id
    with client.session_transaction() as saved:
        saved["admin_id"] = 1
    response = client.get(f"/admin/sales-reps/{rep_id}")
    assert response.status_code == 200
    assert b"20 qualifying sales this month" in response.data
    assert b"$150.00" in response.data
    assert b"10 more sales to unlock $300" in response.data
