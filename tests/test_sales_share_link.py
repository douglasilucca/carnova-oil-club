import pytest
from werkzeug.security import generate_password_hash

from app import SalesRep, db
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config.update(TESTING=True, SECRET_KEY="share-link-test", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def make_rep(name, email, slug):
    rep = SalesRep(name=name, slug=slug, login_email=email, password_hash=generate_password_hash("correct-password"), portal_enabled=True, active=True)
    db.session.add(rep)
    db.session.commit()
    return rep


def login(client, email, password="correct-password"):
    return client.post("/sales/login", data={"email": email, "password": password})


def test_unauthenticated_visitor_is_redirected_to_login(client):
    response = client.get("/sales/share")
    assert response.status_code == 302
    assert response.location.endswith("/sales/login")


def test_authenticated_rep_sees_own_referral_link(client):
    with flask_app.app_context():
        make_rep("Douglas Test", "douglas@example.com", "douglas-test")
    login(client, "douglas@example.com")
    response = client.get("/sales/share")
    assert response.status_code == 200
    assert b"/r/douglas-test" in response.data
    assert b"My Sales Link" in response.data
    assert b"Copy My Link" in response.data
    assert b"Share My Link" in response.data


def test_rep_a_cannot_see_rep_b_link(client):
    with flask_app.app_context():
        make_rep("Rep A", "repa@example.com", "rep-a")
        make_rep("Rep B", "repb@example.com", "rep-b")
    login(client, "repa@example.com")
    response = client.get("/sales/share")
    assert b"/r/rep-a" in response.data
    assert b"/r/rep-b" not in response.data


def test_opening_share_page_does_not_set_referral_attribution(client):
    with flask_app.app_context():
        make_rep("Douglas Test", "douglas@example.com", "douglas-test")
    login(client, "douglas@example.com")
    client.get("/sales/share")
    with client.session_transaction() as saved:
        assert "sales_rep_referral" not in saved


def test_existing_referral_route_still_sets_attribution(client):
    with flask_app.app_context():
        make_rep("Douglas Test", "douglas@example.com", "douglas-test")
    response = client.get("/r/douglas-test")
    assert response.status_code == 302
    with client.session_transaction() as saved:
        assert saved["sales_rep_referral"]["sales_rep_id"] == 1
