import json
import os
from datetime import date, datetime, time, timedelta, timezone

import pytest
import stripe

from app import (
    Admin,
    Appointment,
    AppleWalletPass,
    Member,
    Redemption,
    ReminderLog,
    StripeEvent,
    Vehicle,
    current_member_status,
    db,
    google_wallet_class_payload,
    monthly_membership_defaults,
    google_wallet_api_call,
    google_wallet_member_object_payload,
    google_wallet_next_service_text,
    google_wallet_object_id,
    google_wallet_upsert_member_object,
    apple_wallet_payload,
    STRIPE_PLANS,
    run_all_reminders,
    run_appointment_reminders,
    resolve_appointment_reminder_timezone,
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
    assert b"Add to Apple Wallet" in response.data
    assert b"Add your membership to your Wallet" in response.data
    assert b"/wallet/add" in response.data
    assert b"/apple-wallet" in response.data
    assert b"/static/wallet/add-to-apple-wallet.png" in response.data
    assert b"/static/wallet/add-to-google-wallet.png" in response.data
    assert b"developer.apple.com" not in response.data
    assert b"gstatic.com" not in response.data


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


def test_wallet_origin_vehicle_flow_returns_to_scheduling_with_new_vehicle(client):
    with flask_app.app_context():
        member = Member(
            name="Multi Vehicle Member",
            email="multi-vehicle@example.com",
            member_id="COC-00922",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=5,
            token="multi-vehicle-token",
            plan_name="Prepaid Package",
        )
        first_vehicle = Vehicle(
            member=member,
            year="2022",
            make="Toyota",
            model="Camry",
            color="Black",
            plate="CARNOVA1",
            vin="1HGBH41JXMN109186",
            current_mileage="40210",
        )
        db.session.add_all([member, first_vehicle])
        db.session.commit()

    appointment_url = "/m/multi-vehicle-token/appointments/new"
    appointment_date = date.today() + timedelta(days=1)
    page_response = client.get(appointment_url)

    assert page_response.status_code == 200
    assert b"Toyota Camry" in page_response.data
    assert b"+ Add Vehicle" in page_response.data
    assert f"{appointment_url}?appointment_date={appointment_date.isoformat()}".encode() not in page_response.data

    register_response = client.get(
        "/m/multi-vehicle-token/vehicle/register",
        query_string={
            "return_to": appointment_url,
        },
    )
    assert register_response.status_code == 200
    assert b"Back to Scheduling" in register_response.data

    save_response = client.post(
        "/m/multi-vehicle-token/vehicle/register",
        data={
            "return_to": f"{appointment_url}?appointment_date={appointment_date.isoformat()}",
            "year": "2020",
            "make": "Honda",
            "model": "Civic",
            "color": "Blue",
            "plate": "CARNOVA2",
            "vin_last8": "2HGBH41J",
            "current_mileage": "30100",
        },
        follow_redirects=False,
    )

    assert save_response.status_code == 302
    assert save_response.headers["Location"] == f"{appointment_url}?appointment_date={appointment_date.isoformat()}"

    scheduling_response = client.get(save_response.headers["Location"])
    assert scheduling_response.status_code == 200
    assert b"Toyota Camry" in scheduling_response.data
    assert b"Honda Civic" in scheduling_response.data
    assert b'name="vehicle_id"' in scheduling_response.data

    with flask_app.app_context():
        member = Member.query.filter_by(token="multi-vehicle-token").first()
        assert Vehicle.query.filter_by(member_id=member.id).count() == 2


def test_wallet_next_service_ignores_past_and_ineligible_appointments(client):
    with flask_app.app_context():
        member = Member(
            name="Appointment Filter Member",
            email="appointment-filter@example.com",
            member_id="COC-00923",
            expiration_date=date.today() + timedelta(days=365),
            token="appointment-filter-token",
        )
        db.session.add(member)
        db.session.flush()
        db.session.add_all(
            [
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today() - timedelta(days=1),
                    appointment_time=time(9, 0),
                    status="scheduled",
                ),
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today(),
                    appointment_time=(datetime.now() - timedelta(hours=1)).time(),
                    status="confirmed",
                ),
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today() + timedelta(days=1),
                    appointment_time=time(9, 0),
                    status="cancelled",
                ),
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today() + timedelta(days=1),
                    appointment_time=time(10, 0),
                    status="completed",
                ),
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today() + timedelta(days=1),
                    appointment_time=time(11, 0),
                    status="no_show",
                ),
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today() + timedelta(days=2),
                    appointment_time=time(12, 0),
                    status="scheduled",
                ),
            ]
        )
        db.session.commit()

        assert google_wallet_next_service_text(member) == (
            f"{(date.today() + timedelta(days=2)).strftime('%b %d').replace(' 0', ' ')} | 12:00 PM"
        )


def test_public_appointment_requires_owned_vehicle(client):
    with flask_app.app_context():
        first_member = Member(
            name="Appointment Owner",
            email="appointment-owner@example.com",
            member_id="COC-00924",
            expiration_date=date.today() + timedelta(days=365),
            token="appointment-owner-token",
        )
        second_member = Member(
            name="Other Member",
            email="other-member@example.com",
            member_id="COC-00925",
            expiration_date=date.today() + timedelta(days=365),
            token="other-member-token",
        )
        db.session.add_all([first_member, second_member])
        db.session.flush()
        vehicle = Vehicle(member_id=second_member.id, make="Honda", model="Civic")
        db.session.add(vehicle)
        db.session.commit()
        foreign_vehicle_id = vehicle.id

    appointment_date = date.today() + timedelta(days=1)
    while appointment_date.weekday() == 6:
        appointment_date += timedelta(days=1)
    response = client.post(
        "/m/appointment-owner-token/appointments/new",
        data={
            "appointment_date": appointment_date.isoformat(),
            "appointment_time": "09:00",
            "vehicle_id": str(foreign_vehicle_id),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Please select one of your registered vehicles." in response.data
    with flask_app.app_context():
        assert Appointment.query.count() == 0


def test_public_appointment_without_vehicles_exposes_add_vehicle_and_rejects_missing_vehicle(client):
    with flask_app.app_context():
        member = Member(
            name="No Vehicle Member",
            email="no-vehicle@example.com",
            member_id="COC-00926",
            expiration_date=date.today() + timedelta(days=365),
            token="no-vehicle-token",
        )
        db.session.add(member)
        db.session.commit()

    page_response = client.get("/m/no-vehicle-token/appointments/new")
    assert page_response.status_code == 200
    assert b"+ Add Vehicle" in page_response.data

    appointment_date = date.today() + timedelta(days=1)
    while appointment_date.weekday() == 6:
        appointment_date += timedelta(days=1)
    post_response = client.post(
        "/m/no-vehicle-token/appointments/new",
        data={
            "appointment_date": appointment_date.isoformat(),
            "appointment_time": "09:00",
        },
        follow_redirects=True,
    )
    assert post_response.status_code == 200
    assert b"Please select one of your registered vehicles." in post_response.data


@pytest.mark.parametrize("remaining_changes, expiration_offset", [(0, 365), (1, -1)])
def test_public_appointment_rejects_inactive_membership(client, remaining_changes, expiration_offset):
    with flask_app.app_context():
        member = Member(
            name="Inactive Appointment Member",
            email=f"inactive-{remaining_changes}-{expiration_offset}@example.com",
            member_id=f"COC-{remaining_changes}{abs(expiration_offset):04d}",
            expiration_date=date.today() + timedelta(days=expiration_offset),
            remaining_changes=remaining_changes,
            total_changes=1,
            token=f"inactive-{remaining_changes}-{expiration_offset}-token",
        )
        db.session.add(member)
        db.session.commit()
        member_token = member.token

    response = client.post(
        f"/m/{member_token}/appointments/new",
        data={
            "appointment_date": (date.today() + timedelta(days=1)).isoformat(),
            "appointment_time": "09:00",
            "vehicle_id": "999999",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"This membership is not active and cannot schedule service." in response.data
    with flask_app.app_context():
        assert Appointment.query.count() == 0


@pytest.mark.parametrize("return_to", ["http://[", "https://evil.example/appointments", "/m/other-member-token/appointments/new"])
def test_public_vehicle_return_to_rejects_malformed_external_and_wrong_member_paths(client, return_to):
    with flask_app.app_context():
        member = Member(
            name="Return Path Member",
            email="return-path@example.com",
            member_id="COC-00927",
            expiration_date=date.today() + timedelta(days=365),
            token="return-path-token",
        )
        db.session.add(member)
        db.session.commit()

    response = client.get(
        "/m/return-path-token/vehicle/register",
        query_string={"return_to": return_to},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert b"Back to Membership Card" in response.data


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
    object_patch_calls = {"count": 0}

    def fake_access_token():
        token_fetches["count"] += 1
        return "cached-wallet-token"

    def fake_api_call(method, endpoint, payload=None, access_token=None):
        api_access_tokens.append((method, endpoint, access_token))
        if endpoint.endswith("/genericClass/issuer123.class123") and method == "PATCH":
            return 200, {}
        if endpoint.endswith("/genericObject/issuer123.carnova_coc-00912") and method == "PATCH":
            object_patch_calls["count"] += 1
            if object_patch_calls["count"] == 1:
                return 404, {}
            return 200, {}
        return 201, {}

    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "issuer123")
    monkeypatch.setenv("GOOGLE_WALLET_CLASS_ID", "class123")
    monkeypatch.setattr("app.GOOGLE_WALLET_ENSURED_CLASS_IDS", set())
    monkeypatch.setattr("app.google_wallet_access_token", fake_access_token)
    monkeypatch.setattr("app.google_wallet_api_call", fake_api_call)

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member.query.get(member_id)
        assert google_wallet_upsert_member_object(member) is True
        assert google_wallet_upsert_member_object(member) is True
    assert token_fetches["count"] == 2
    assert api_access_tokens == [
        ("PATCH", "https://walletobjects.googleapis.com/walletobjects/v1/genericClass/issuer123.class123", "cached-wallet-token"),
        ("PATCH", "https://walletobjects.googleapis.com/walletobjects/v1/genericObject/issuer123.carnova_coc-00912", "cached-wallet-token"),
        ("POST", "https://walletobjects.googleapis.com/walletobjects/v1/genericObject", "cached-wallet-token"),
        ("PATCH", "https://walletobjects.googleapis.com/walletobjects/v1/genericObject/issuer123.carnova_coc-00912", "cached-wallet-token"),
    ]


def test_google_wallet_class_payload_card_template_override_uses_remaining_changes_field(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "issuer123")
    monkeypatch.setenv("GOOGLE_WALLET_CLASS_ID", "class123")

    payload = google_wallet_class_payload()

    assert payload["id"] == "issuer123.class123"
    template = payload["classTemplateInfo"]["cardTemplateOverride"]["cardRowTemplateInfos"][0]
    field_path = template["oneItem"]["item"]["firstValue"]["fields"][0]["fieldPath"]
    assert field_path == "object.textModulesData['remaining_changes']"
    next_service_template = payload["classTemplateInfo"]["cardTemplateOverride"]["cardRowTemplateInfos"][1]
    assert next_service_template["twoItems"]["startItem"]["firstValue"]["fields"][0]["fieldPath"] == (
        "object.textModulesData['next_service']"
    )


def test_google_wallet_payload_includes_prominent_balance_logo_and_links(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "issuer123")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member(
            name="Wallet Payload Member",
            email="wallet-payload@example.com",
            member_id="COC-00915",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=5,
            token="wallet-payload-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )

        payload = google_wallet_member_object_payload(member)

    assert payload["id"] == "issuer123.carnova_coc-00915"
    assert payload["genericType"] == "GENERIC_OTHER"
    assert payload["hexBackgroundColor"] == "#101820"
    assert payload["cardTitle"]["defaultValue"]["value"] == "Carnova Oil Club"
    assert payload["header"]["defaultValue"]["value"] == "Oil Club Premium"
    assert payload["subheader"]["defaultValue"]["value"] == "Wallet Payload Member"
    assert payload["textModulesData"][0]["id"] == "remaining_changes"
    assert payload["textModulesData"][0]["body"] == "3 OIL CHANGES REMAINING"
    assert payload["textModulesData"][1]["body"] == "NOT SCHEDULED"

    assert payload["logo"]["sourceUri"]["uri"] == "https://cards.carnova.test/static/carnova-wallet-logo-v2.png"
    assert len(payload["linksModuleData"]["uris"]) == 1
    assert payload["linksModuleData"]["uris"][0]["id"] == "manage_package"
    assert payload["linksModuleData"]["uris"][0]["description"] == "Manage Your Package"
    assert payload["linksModuleData"]["uris"][0]["uri"] == "https://cards.carnova.test/m/wallet-payload-token"
    assert payload["appLinkData"]["displayText"]["defaultValue"]["value"] == "Schedule Oil Change"
    assert payload["appLinkData"]["webAppLinkInfo"]["appTarget"]["targetUri"]["uri"] == (
        "https://cards.carnova.test/m/wallet-payload-token/appointments/new"
    )
    assert payload["appLinkData"]["webAppLinkInfo"]["appTarget"]["targetUri"]["description"] == "Schedule Oil Change"


def test_google_wallet_next_service_text_uses_earliest_active_appointment(client):
    with flask_app.app_context():
        member = Member(
            name="Next Service Member",
            email="next-service@example.com",
            member_id="COC-00921",
            expiration_date=date.today() + timedelta(days=365),
            token="next-service-token",
        )
        db.session.add(member)
        db.session.commit()
        db.session.add(
            Appointment(
                member_id=member.id,
                appointment_date=date.today() + timedelta(days=5),
                appointment_time=time(10, 30),
                status="scheduled",
            )
        )
        db.session.add(
            Appointment(
                member_id=member.id,
                appointment_date=date.today() + timedelta(days=2),
                appointment_time=time(9, 0),
                status="cancelled",
            )
        )
        db.session.commit()

        assert google_wallet_next_service_text(member) == (
            f"{(date.today() + timedelta(days=5)).strftime('%b %d').replace(' 0', ' ')} | 10:30 AM"
        )


@pytest.mark.parametrize(
    ("base_url", "expected_logo_url", "expected_manage_url", "expected_schedule_url"),
    [
        (
            "https://example.com",
            "https://example.com/static/carnova-wallet-logo-v2.png",
            "https://example.com/m/wallet-payload-token",
            "https://example.com/m/wallet-payload-token/appointments/new",
        ),
        (
            "https://example.com/carnova",
            "https://example.com/carnova/static/carnova-wallet-logo-v2.png",
            "https://example.com/carnova/m/wallet-payload-token",
            "https://example.com/carnova/m/wallet-payload-token/appointments/new",
        ),
    ],
)
def test_google_wallet_payload_urls_respect_base_url_path_prefix(
    client,
    monkeypatch,
    base_url,
    expected_logo_url,
    expected_manage_url,
    expected_schedule_url,
):
    monkeypatch.setenv("BASE_URL", base_url)

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member(
            name="Wallet URL Prefix Member",
            email="wallet-prefix@example.com",
            member_id="COC-00917",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=5,
            token="wallet-payload-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )

        payload = google_wallet_member_object_payload(member)

    assert payload["logo"]["sourceUri"]["uri"] == expected_logo_url
    assert payload["linksModuleData"]["uris"][0]["uri"] == expected_manage_url
    assert payload["appLinkData"]["webAppLinkInfo"]["appTarget"]["targetUri"]["uri"] == expected_schedule_url


def test_google_wallet_payload_avoids_duplicate_manage_your_package_link(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member(
            name="Wallet No Duplicate Manage",
            email="wallet-no-duplicate-manage@example.com",
            member_id="COC-00919",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=5,
            token="wallet-no-duplicate-manage-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )

        payload = google_wallet_member_object_payload(member)

    manage_links = [
        link
        for link in payload["linksModuleData"]["uris"]
        if link.get("id") == "manage_package" or link.get("description") == "Manage Your Package"
    ]
    assert len(manage_links) == 1
    assert payload["appLinkData"]["displayText"]["defaultValue"]["value"] == "Schedule Oil Change"


def test_google_wallet_class_is_ensured_once_per_process_for_same_class_id(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Class Ensure Once",
            email="class-ensure-once@example.com",
            member_id="COC-00920",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=3,
            total_changes=3,
            token="class-ensure-once-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.id

    token_fetches = {"count": 0}
    class_patch_calls = {"count": 0}

    def fake_access_token():
        token_fetches["count"] += 1
        return "cached-wallet-token"

    def fake_api_call(method, endpoint, payload=None, access_token=None):
        if endpoint.endswith("/genericClass/issuer123.class123") and method == "PATCH":
            class_patch_calls["count"] += 1
            return 200, {}
        if endpoint.endswith("/genericObject/issuer123.carnova_coc-00920") and method == "PATCH":
            return 200, {}
        return 201, {}

    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "issuer123")
    monkeypatch.setenv("GOOGLE_WALLET_CLASS_ID", "class123")
    monkeypatch.setattr("app.GOOGLE_WALLET_ENSURED_CLASS_IDS", set())
    monkeypatch.setattr("app.google_wallet_access_token", fake_access_token)
    monkeypatch.setattr("app.google_wallet_api_call", fake_api_call)

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member.query.get(member_id)
        assert google_wallet_upsert_member_object(member) is True
        assert google_wallet_upsert_member_object(member) is True

    assert class_patch_calls["count"] == 1
    assert token_fetches["count"] == 2


@pytest.mark.parametrize(
    ("remaining_changes", "expected_text"),
    [
        (0, "0 OIL CHANGES REMAINING"),
        (1, "1 OIL CHANGE REMAINING"),
        (2, "2 OIL CHANGES REMAINING"),
        (3, "3 OIL CHANGES REMAINING"),
    ],
)
def test_google_wallet_payload_remaining_changes_text_singular_plural(
    client,
    monkeypatch,
    remaining_changes,
    expected_text,
):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member(
            name="Wallet Balance Grammar",
            email="wallet-balance-grammar@example.com",
            member_id="COC-00918",
            expiration_date=date.today() + timedelta(days=365),
            remaining_changes=remaining_changes,
            total_changes=5,
            token="wallet-balance-grammar-token",
            plan_name="Monthly Membership",
            subscription_status="active",
        )

        payload = google_wallet_member_object_payload(member)

    assert payload["textModulesData"][0]["id"] == "remaining_changes"
    assert payload["textModulesData"][0]["body"] == expected_text


def test_google_wallet_object_id_is_deterministic_for_existing_member(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_WALLET_ISSUER_ID", "issuer456")

    member = Member(
        name="Deterministic Wallet",
        email="deterministic-wallet@example.com",
        member_id="COC-00916",
        expiration_date=date.today() + timedelta(days=365),
        remaining_changes=3,
        total_changes=3,
        token="deterministic-wallet-token",
        plan_name="Monthly Membership",
        subscription_status="active",
    )

    first_object_id = google_wallet_object_id(member)
    member.remaining_changes = 1
    second_object_id = google_wallet_object_id(member)

    assert first_object_id == "issuer456.carnova_coc-00916"
    assert second_object_id == first_object_id


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

    called = {"count": 0, "remaining_changes": []}

    def fake_sync(updated_member):
        called["count"] += 1
        called["remaining_changes"].append(updated_member.remaining_changes)
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
    assert called["remaining_changes"] == [1]


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

    called = {"count": 0, "remaining_changes": []}

    def fake_sync(updated_member):
        called["count"] += 1
        called["remaining_changes"].append(updated_member.remaining_changes)
        return True

    monkeypatch.setattr("app.sync_member_google_wallet_object", fake_sync)

    response = client.post(f"/members/{member_id}/undo", follow_redirects=True)

    assert response.status_code == 200
    assert b"Last redemption was undone." in response.data
    assert called["count"] == 1
    assert called["remaining_changes"] == [1]


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


@pytest.mark.parametrize(
    ("price_id", "mode", "expected_changes"),
    [
        ("price_1Tx6veR1GwRFNmYeUO2goMjz", "payment", 3),
        ("price_1TwiJER1GwRFNmYeeFbUdscR", "payment", 5),
        ("price_1Tx70UR1GwRFNmYePYn1Xrdz", "payment", 8),
        ("price_1TxtO7R1GwRFNmYeGo3km5vf", "subscription", 3),
    ],
)
def test_existing_zero_credit_purchase_fulfills_plan_and_syncs_wallets(
    client, monkeypatch, price_id, mode, expected_changes
):
    with flask_app.app_context():
        member = Member(
            name="Existing Buyer",
            email="existing-buyer@example.com",
            member_id="COC-00020",
            expiration_date=date.today() + timedelta(days=30),
            remaining_changes=0,
            total_changes=3,
            token="existing-buyer-token",
            status="completed",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.id

    event = {
        "id": f"evt_purchase_{expected_changes}_{mode}",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": f"cs_purchase_{expected_changes}_{mode}",
            "mode": mode,
            "customer": "cus_existing_buyer",
            "subscription": "sub_existing_buyer" if mode == "subscription" else None,
            "payment_intent": "pi_existing_buyer",
            "customer_details": {"email": "existing-buyer@example.com", "name": "Existing Buyer"},
            "amount_total": 1000,
            "metadata": {"member_id": str(member_id)},
            "shipping_details": {},
        }},
    }
    calls = []

    monkeypatch.setattr("app.stripe.Webhook.construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(
        "app.stripe.checkout.Session.list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": price_id}}]},
    )

    def fake_google(updated_member):
        fresh = db.session.get(Member, member_id)
        calls.append(("google", fresh.remaining_changes))
        return True

    def fake_apple(updated_member):
        fresh = db.session.get(Member, member_id)
        calls.append(("apple", fresh.remaining_changes))
        return True

    monkeypatch.setattr("app.sync_member_google_wallet_object", fake_google)
    monkeypatch.setattr("app.apple_wallet_mark_pass_updated", fake_apple)

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"event": "verified"}),
        headers={"Stripe-Signature": "valid-signature"},
    )

    assert response.status_code == 200
    with flask_app.app_context():
        fulfilled = db.session.get(Member, member_id)
        assert fulfilled.remaining_changes == expected_changes
        assert fulfilled.total_changes == max(3, expected_changes)
    assert calls == [("google", expected_changes), ("apple", expected_changes)]


def test_checkout_purchase_wallet_failures_do_not_rollback_fulfillment(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Wallet Failure Buyer",
            email="wallet-failure@example.com",
            member_id="COC-00021",
            expiration_date=date.today() + timedelta(days=30),
            remaining_changes=0,
            total_changes=3,
            token="wallet-failure-token",
            status="completed",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.id

    event = {
        "id": "evt_wallet_failure",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_wallet_failure",
            "mode": "payment",
            "customer": "cus_wallet_failure",
            "payment_intent": "pi_wallet_failure",
            "customer_details": {"email": "wallet-failure@example.com"},
            "amount_total": 1000,
            "metadata": {"member_id": str(member_id)},
            "shipping_details": {},
        }},
    }
    monkeypatch.setattr("app.stripe.Webhook.construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(
        "app.stripe.checkout.Session.list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx70UR1GwRFNmYePYn1Xrdz"}}]},
    )
    monkeypatch.setattr("app.sync_member_google_wallet_object", lambda _member: (_ for _ in ()).throw(RuntimeError("google")))
    monkeypatch.setattr("app.apple_wallet_mark_pass_updated", lambda _member: (_ for _ in ()).throw(RuntimeError("apple")))

    response = client.post(
        "/stripe/webhook",
        data=json.dumps({"event": "verified"}),
        headers={"Stripe-Signature": "valid-signature"},
    )

    assert response.status_code == 200
    with flask_app.app_context():
        assert db.session.get(Member, member_id).remaining_changes == 8


def test_checkout_purchase_replay_and_invalid_signature_do_not_repeat_fulfillment(client, monkeypatch):
    with flask_app.app_context():
        member = Member(
            name="Replay Buyer",
            email="replay@example.com",
            member_id="COC-00022",
            expiration_date=date.today() + timedelta(days=30),
            remaining_changes=0,
            total_changes=3,
            token="replay-token",
            status="completed",
        )
        db.session.add(member)
        db.session.commit()
        member_id = member.id

    event = {
        "id": "evt_replay_purchase",
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_replay_purchase",
            "mode": "payment",
            "customer": "cus_replay",
            "payment_intent": "pi_replay",
            "customer_details": {"email": "replay@example.com"},
            "amount_total": 1000,
            "metadata": {"member_id": str(member_id)},
            "shipping_details": {},
        }},
    }
    calls = []
    monkeypatch.setattr("app.stripe.Webhook.construct_event", lambda *_args, **_kwargs: event)
    monkeypatch.setattr(
        "app.stripe.checkout.Session.list_line_items",
        lambda *_args, **_kwargs: {"data": [{"price": {"id": "price_1Tx70UR1GwRFNmYePYn1Xrdz"}}]},
    )
    monkeypatch.setattr("app.sync_member_google_wallet_object", lambda _member: calls.append("google"))
    monkeypatch.setattr("app.apple_wallet_mark_pass_updated", lambda _member: calls.append("apple"))

    first = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "valid"})
    second = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "valid"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == ["google", "apple"]

    monkeypatch.setattr("app.stripe.Webhook.construct_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad signature")))
    invalid = client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "forged"})
    assert invalid.status_code == 400
    assert calls == ["google", "apple"]


def test_successful_zero_credit_purchase_removes_apple_purchase_field(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = Member(
            name="Wallet Buyer",
            email="wallet-buyer@example.com",
            member_id="COC-00023",
            expiration_date=date.today() + timedelta(days=30),
            remaining_changes=0,
            total_changes=3,
            token="wallet-buyer-token",
            status="completed",
        )
        db.session.add(member)
        db.session.commit()
        pass_record = AppleWalletPass.create_for_member(member)
        assert any(field["key"] == "buy_more_oil_changes" for field in apple_wallet_payload(member)["generic"]["backFields"])
        member.remaining_changes = 8
        db.session.commit()
        assert not any(field["key"] == "buy_more_oil_changes" for field in apple_wallet_payload(member)["generic"]["backFields"])


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


def test_appointment_timezone_valid_america_new_york(monkeypatch):
    monkeypatch.setenv("APPOINTMENT_REMINDER_TIMEZONE", "America/New_York")
    tz = resolve_appointment_reminder_timezone()

    winter_utc = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
    summer_utc = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    winter_local = winter_utc.astimezone(tz)
    summer_local = summer_utc.astimezone(tz)

    assert winter_local.hour == 8
    assert summer_local.hour == 8


def test_appointment_timezone_invalid_falls_back_to_eastern(monkeypatch):
    monkeypatch.setenv("APPOINTMENT_REMINDER_TIMEZONE", "Mars/Phobos")
    tz = resolve_appointment_reminder_timezone()

    winter_utc = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
    summer_utc = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    winter_local = winter_utc.astimezone(tz)
    summer_local = summer_utc.astimezone(tz)

    assert winter_local.hour == 8
    assert summer_local.hour == 8


def test_appointment_morning_reminder_targets_8am_eastern_when_timezone_invalid(client, monkeypatch):
    appointment_date = date(2026, 1, 15)
    member = _create_member_for_reminders("appt-eastern-target@example.com", expiration_days=180)
    db.session.add(
        Appointment(
            member_id=member.id,
            appointment_date=appointment_date,
            appointment_time=time(10, 0),
            status="scheduled",
            service_type="Oil Change",
        )
    )
    db.session.commit()

    monkeypatch.setenv("APPOINTMENT_REMINDER_TIMEZONE", "Invalid/Timezone")
    monkeypatch.setenv("APPOINTMENT_REMINDER_MORNING_HOUR", "8")
    monkeypatch.setattr("app.send_smtp_email", lambda *args, **kwargs: True)

    before_summary = run_appointment_reminders(
        reference_datetime=datetime(2026, 1, 15, 12, 59, tzinfo=timezone.utc)
    )
    after_summary = run_appointment_reminders(
        reference_datetime=datetime(2026, 1, 15, 13, 1, tzinfo=timezone.utc)
    )

    assert before_summary["sent"] == 0
    assert before_summary["skip_reasons"]["before_morning_send_time"] == 1
    assert after_summary["sent"] == 1
