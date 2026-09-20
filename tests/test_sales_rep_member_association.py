from datetime import date, timedelta

import pytest
from werkzeug.security import generate_password_hash

from app import Member, ReferralSale, SalesRep, db
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="member-association-test",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_rep(name, slug):
    rep = SalesRep(
        name=name,
        slug=slug,
        login_email=f"{slug}@example.com",
        password_hash=generate_password_hash("correct-password"),
        portal_enabled=True,
    )
    db.session.add(rep)
    db.session.flush()
    return rep


def make_member(member_id, email):
    member = Member(
        name=f"Member {member_id}",
        email=email,
        member_id=member_id,
        expiration_date=date.today() + timedelta(days=365),
        token=f"token-{member_id}",
    )
    db.session.add(member)
    db.session.flush()
    return member


def as_admin(client):
    with client.session_transaction() as saved:
        saved["admin_id"] = 1


def test_explicit_link_persists_on_both_sides(client):
    with flask_app.app_context():
        rep = make_rep("Identity Rep", "identity-rep")
        member = make_member("COC-ASSOC-1", "identity@example.com")
        rep.member = member
        db.session.commit()
        rep_id = rep.id
        member_id = member.id

        db.session.expire_all()
        loaded_rep = db.session.get(SalesRep, rep_id)
        loaded_member = db.session.get(Member, member_id)
        assert loaded_rep.member.id == member_id
        assert loaded_member.sales_rep.id == rep_id


def test_existing_unlinked_sales_rep_and_member_remain_valid(client):
    with flask_app.app_context():
        rep = make_rep("Unlinked Rep", "unlinked-rep")
        member = make_member("COC-ASSOC-2", "unlinked@example.com")
        db.session.commit()
        assert rep.member is None
        assert member.sales_rep is None


def test_same_member_cannot_be_linked_to_two_sales_reps(client):
    with flask_app.app_context():
        first_rep = make_rep("First Rep", "first-rep")
        second_rep = make_rep("Second Rep", "second-rep")
        member = make_member("COC-ASSOC-3", "shared@example.com")
        first_rep.member = member
        db.session.commit()
        first_rep_id = first_rep.id
        second_rep_id = second_rep.id
        member_id = member.id
    as_admin(client)

    response = client.post(f"/admin/sales-reps/{second_rep_id}/member", data={"member_id": str(member_id)})
    assert response.status_code == 302
    with flask_app.app_context():
        assert db.session.get(SalesRep, first_rep_id).member_id == member_id
        assert db.session.get(SalesRep, second_rep_id).member_id is None


def test_admin_can_link_change_and_unlink_member(client):
    with flask_app.app_context():
        rep = make_rep("Change Rep", "change-rep")
        first_member = make_member("COC-ASSOC-4", "first@example.com")
        second_member = make_member("COC-ASSOC-5", "second@example.com")
        db.session.commit()
        rep_id = rep.id
        first_id = first_member.id
        second_id = second_member.id
    as_admin(client)

    assert client.post(f"/admin/sales-reps/{rep_id}/member", data={"member_id": str(first_id)}).status_code == 302
    with flask_app.app_context():
        assert db.session.get(SalesRep, rep_id).member_id == first_id
    assert client.post(f"/admin/sales-reps/{rep_id}/member", data={"member_id": str(second_id)}).status_code == 302
    with flask_app.app_context():
        assert db.session.get(SalesRep, rep_id).member_id == second_id
    assert client.post(f"/admin/sales-reps/{rep_id}/member", data={"member_id": ""}).status_code == 302
    with flask_app.app_context():
        assert db.session.get(SalesRep, rep_id).member_id is None


def test_admin_rejects_invalid_member_id_without_changing_link(client):
    with flask_app.app_context():
        rep = make_rep("Invalid ID Rep", "invalid-id-rep")
        member = make_member("COC-ASSOC-6", "valid@example.com")
        rep.member = member
        db.session.commit()
        rep_id = rep.id
        member_id = member.id
    as_admin(client)

    response = client.post(f"/admin/sales-reps/{rep_id}/member", data={"member_id": "not-an-id"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"Select a valid Oil Club Member." in response.data
    with flask_app.app_context():
        assert db.session.get(SalesRep, rep_id).member_id == member_id

    response = client.post(f"/admin/sales-reps/{rep_id}/member", data={"member_id": "999999"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"That Oil Club Member was not found." in response.data
    with flask_app.app_context():
        assert db.session.get(SalesRep, rep_id).member_id == member_id


def test_identity_link_does_not_change_referral_sale_attribution(client):
    with flask_app.app_context():
        rep = make_rep("Attributed Rep", "attributed-rep")
        member = make_member("COC-ASSOC-7", "attributed@example.com")
        sale = ReferralSale(
            sales_rep=rep,
            member=member,
            stripe_event_id="evt-association",
            stripe_checkout_session_id="cs-association",
            stripe_price_id="price-association",
            plan_name="Bronze",
            oil_changes=3,
            commission_cents=1000,
        )
        db.session.add(sale)
        db.session.commit()
        sale_id = sale.id
        rep_id = rep.id
        member_id = member.id
    as_admin(client)

    assert client.post(f"/admin/sales-reps/{rep_id}/member", data={"member_id": str(member_id)}).status_code == 302
    with flask_app.app_context():
        sale = db.session.get(ReferralSale, sale_id)
        assert sale.sales_rep_id == rep_id
        assert sale.member_id == member_id
        assert sale.commission_cents == 1000
