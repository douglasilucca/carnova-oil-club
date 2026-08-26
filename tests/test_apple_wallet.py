import json
import hashlib
import os
import subprocess
import zipfile
from datetime import date, time, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app import (
    AppleWalletDevice,
    AppleWalletPass,
    AppleWalletRegistration,
    Appointment,
    Member,
    Vehicle,
    apple_wallet_build_bundle,
    apple_wallet_member_serial,
    apple_wallet_next_service_text,
    apple_wallet_payload,
    apple_wallet_require_auth_token,
    db,
    google_wallet_member_object_payload,
)
from app import app as flask_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "test-apple-pass-key-0123456789abcdef")
    flask_app.config.update(
        TESTING=True,
        SECRET_KEY="test-secret",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


def create_member(**overrides):
    values = {
        "name": "Apple Wallet Member",
        "email": "apple-wallet@example.com",
        "member_id": "COC-01001",
        "expiration_date": date.today() + timedelta(days=365),
        "remaining_changes": 2,
        "total_changes": 3,
        "token": "apple-wallet-token",
        "plan_name": "Monthly Membership",
        "subscription_status": "active",
    }
    values.update(overrides)
    member = Member(**values)
    db.session.add(member)
    db.session.commit()
    return member


def test_apple_wallet_serial_is_deterministic(client):
    with flask_app.app_context():
        member = create_member()
        first = apple_wallet_member_serial(member)
        second = apple_wallet_member_serial(member)

    assert first == second
    assert first.startswith("carnova-")
    assert len(first) == len("carnova-") + 24


def test_apple_wallet_requires_stable_encryption_key_when_missing(monkeypatch):
    monkeypatch.delenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="APPLE_PASS_TOKEN_ENCRYPTION_KEY"):
        AppleWalletPass.encryption_key()


def test_apple_wallet_payload_contains_member_balance_and_status(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member(name="Ava Member", remaining_changes=2)
        payload = apple_wallet_payload(member)

    assert payload["serialNumber"] == apple_wallet_member_serial(member)
    assert payload["generic"]["primaryFields"] == [
        {"key": "member_name", "label": "Member", "value": "Ava Member"}
    ]
    assert payload["generic"]["secondaryFields"] == [
        {"key": "remaining_changes", "label": "Oil Changes Left", "value": "2"},
        {"key": "status", "label": "Membership Status", "value": "Active"},
    ]


def test_apple_wallet_payload_reports_cancelled_membership(client):
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member(status="cancelled")
        payload = apple_wallet_payload(member)

    assert payload["generic"]["secondaryFields"][1]["value"] == "Cancelled"


def test_apple_wallet_next_service_uses_earliest_scheduled_or_confirmed(client):
    with flask_app.app_context():
        member = create_member()
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
                    appointment_date=date.today() + timedelta(days=1),
                    appointment_time=time(11, 0),
                    status="cancelled",
                ),
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today() + timedelta(days=3),
                    appointment_time=time(14, 30),
                    status="confirmed",
                ),
                Appointment(
                    member_id=member.id,
                    appointment_date=date.today() + timedelta(days=2),
                    appointment_time=time(10, 0),
                    status="completed",
                ),
            ]
        )
        db.session.commit()

        assert apple_wallet_next_service_text(member) == (
            f"{(date.today() + timedelta(days=3)).strftime('%b %d, %Y')} 02:30 PM"
        )


def test_apple_wallet_next_service_handles_only_cancelled_appointments(client):
    with flask_app.app_context():
        member = create_member()
        db.session.add(
            Appointment(
                member_id=member.id,
                appointment_date=date.today() + timedelta(days=1),
                appointment_time=time(9, 0),
                status="cancelled",
            )
        )
        db.session.commit()

        assert apple_wallet_next_service_text(member) == "No upcoming service"


def test_apple_wallet_barcode_uses_public_member_url(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        payload = apple_wallet_payload(member)

    assert payload["barcode"]["format"] == "PKBarcodeFormatQR"
    assert payload["barcode"]["message"] == "https://cards.carnova.test/m/apple-wallet-token"


def test_apple_wallet_payload_includes_member_schedule_service_url(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        payload = apple_wallet_payload(member)

    schedule_field = next(
        field for field in payload["generic"]["backFields"] if field["key"] == "schedule_service"
    )
    assert schedule_field == {
        "key": "schedule_service",
        "label": "Schedule Service",
        "value": "https://cards.carnova.test/m/apple-wallet-token/appointments/new",
    }


def test_apple_wallet_payload_includes_portal_back_fields(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        db.session.add(Vehicle(member_id=member.id, year="2022", make="Toyota", model="Camry"))
        db.session.commit()
        payload = apple_wallet_payload(member)

    assert payload["generic"]["backFields"] == [
        {"key": "vehicle", "label": "Vehicle", "value": "2022 Toyota Camry"},
        {"key": "expiration_date", "label": "Expiration", "value": member.expiration_date.strftime("%B %d, %Y")},
        {"key": "member_id", "label": "Member ID", "value": "COC-01001"},
        {
            "key": "schedule_service",
            "label": "Schedule Service",
            "value": "https://cards.carnova.test/m/apple-wallet-token/appointments/new",
        },
        {
            "key": "manage_membership",
            "label": "Manage Membership",
            "value": "https://cards.carnova.test/m/apple-wallet-token",
        },
    ]


def test_apple_wallet_logo_asset_is_wide_header_resource():
    logo_path = Path(__file__).resolve().parents[1] / "static" / "carnova-apple-wallet-logo.png"

    with Image.open(logo_path) as logo:
        assert logo.format == "PNG"
        assert logo.size == (320, 100)


def test_apple_wallet_bundle_contains_required_files_and_invokes_signing(client, monkeypatch, tmp_path):
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        cert = tmp_path / "test-cert.pem"
        key = tmp_path / "test-key.pem"
        wwdr = tmp_path / "test-wwdr.pem"
        for path in (cert, key, wwdr):
            path.write_text("test-only placeholder", encoding="ascii")

        monkeypatch.setattr(
            "app.apple_wallet_secret_paths",
            lambda: {"cert": str(cert), "key": str(key), "wwdr": str(wwdr)},
        )
        image_sources = []

        def fake_image_asset(source, target, _size):
            image_sources.append(source.name)
            target.write_bytes(b"test png")

        monkeypatch.setattr(
            "app.apple_wallet_create_image_asset",
            fake_image_asset,
        )
        signing_calls = []

        def fake_run(command, **kwargs):
            signing_calls.append((command, kwargs))
            output_path = command[command.index("-out") + 1]
            with open(output_path, "wb") as signature:
                signature.write(b"test signature")

        monkeypatch.setattr("app.subprocess.run", fake_run)
        monkeypatch.setattr("app.Path.exists", lambda path: True)

        bundle_path = apple_wallet_build_bundle(member)

    with zipfile.ZipFile(bundle_path) as bundle:
        names = set(bundle.namelist())
        pass_payload = json.loads(bundle.read("pass.json"))
        manifest = json.loads(bundle.read("manifest.json"))
        packaged_hashes = {
            name: hashlib.sha1(bundle.read(name)).hexdigest()
            for name in manifest
        }

    assert {"pass.json", "manifest.json", "signature"}.issubset(names)
    assert not any(name.endswith(".pkpass") for name in names)
    assert names == {
        "icon.png",
        "icon@2x.png",
        "logo.png",
        "logo@2x.png",
        "manifest.json",
        "pass.json",
        "signature",
    }
    assert image_sources == [
        "carnova-wallet-logo-v2.png",
        "carnova-wallet-logo-v2.png",
        "carnova-apple-wallet-logo.png",
        "carnova-apple-wallet-logo.png",
    ]
    assert set(manifest) == names - {"manifest.json", "signature"}
    assert manifest == packaged_hashes
    assert pass_payload["serialNumber"].startswith("carnova-")
    assert len(signing_calls) == 1
    command, kwargs = signing_calls[0]
    assert command[:4] == ["openssl", "cms", "-sign", "-binary"]
    assert command[command.index("-signer") + 1] == str(cert)
    assert command[command.index("-inkey") + 1] == str(key)
    assert command[command.index("-certfile") + 1] == str(wwdr)
    assert kwargs == {"check": True, "capture_output": True}


def test_apple_wallet_download_route_returns_pkpass(client, monkeypatch):
    with flask_app.app_context():
        member = create_member()
        bundle = BytesIO(b"fake pkpass")
        monkeypatch.setattr("app.apple_wallet_build_bundle", lambda _member: bundle)
        response = client.get(f"/m/{member.token}/apple-wallet")

    assert response.status_code == 200
    assert response.mimetype == "application/vnd.apple.pkpass"
    assert response.data == b"fake pkpass"
    assert "COC-01001-membership.pkpass" in response.headers["Content-Disposition"]


def test_apple_wallet_pass_has_stable_persisted_authentication_token(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    with flask_app.app_context():
        member = create_member()
        pass_record = AppleWalletPass.query.filter_by(member_id=member.id).first()
        if not pass_record:
            pass_record = AppleWalletPass.create_for_member(member)
        first = pass_record.authentication_token
        second = pass_record.authentication_token
        assert first == second
        assert first != member.token
        assert pass_record.authentication_token_hash
        assert pass_record.authentication_token_encrypted != first
        assert "ApplePass" not in pass_record.authentication_token_encrypted


def test_apple_wallet_payload_includes_web_service_and_authentication_token(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        payload = apple_wallet_payload(member)

    assert payload["webServiceURL"] == "https://cards.carnova.test/apple-wallet"
    assert payload["authenticationToken"]
    assert payload["authenticationToken"] == AppleWalletPass.query.filter_by(member_id=member.id).first().authentication_token


def test_apple_wallet_device_polling_without_authorization_succeeds(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        pass_record = AppleWalletPass.create_for_member(member)
        device = AppleWalletDevice.create_or_update("device-abc", "push-token-123")
        AppleWalletRegistration.register(pass_record, device)
        db.session.commit()

        response = client.get(
            f"/apple-wallet/v1/devices/{device.device_library_identifier}/registrations/{pass_record.pass_type_identifier}"
        )
        assert response.status_code == 200
        assert response.get_json()["serialNumbers"] == [pass_record.serial_number]


def test_apple_wallet_device_polling_initial_sync_without_passesUpdatedSince_succeeds(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        pass_record = AppleWalletPass.create_for_member(member)
        device = AppleWalletDevice.create_or_update("device-abc", "push-token-123")
        AppleWalletRegistration.register(pass_record, device)
        db.session.commit()

        response = client.get(
            f"/apple-wallet/v1/devices/{device.device_library_identifier}/registrations/{pass_record.pass_type_identifier}"
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert pass_record.serial_number in payload["serialNumbers"]
        assert payload["lastUpdated"] == pass_record.last_updated


def test_apple_wallet_device_polling_filters_by_passesUpdatedSince(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        pass_record = AppleWalletPass.create_for_member(member)
        device = AppleWalletDevice.create_or_update("device-abc", "push-token-123")
        AppleWalletRegistration.register(pass_record, device)
        db.session.commit()

        initial = client.get(
            f"/apple-wallet/v1/devices/{device.device_library_identifier}/registrations/{pass_record.pass_type_identifier}?passesUpdatedSince=0"
        )
        assert initial.status_code == 200
        assert pass_record.serial_number in initial.get_json()["serialNumbers"]

        pass_record.mark_updated()
        db.session.commit()

        filtered = client.get(
            f"/apple-wallet/v1/devices/{device.device_library_identifier}/registrations/{pass_record.pass_type_identifier}?passesUpdatedSince={pass_record.last_updated}"
        )
        assert filtered.status_code == 200
        assert filtered.get_json()["serialNumbers"] == []


def test_apple_wallet_device_polling_rejects_unknown_device(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    response = client.get("/apple-wallet/v1/devices/unknown-device/registrations/pass.com.carnovaoil.membership")
    assert response.status_code == 404


def test_apple_wallet_device_polling_is_isolated_per_device(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context(), flask_app.test_request_context("/"):
        member_one = create_member(member_id="COC-01001", token="member-one")
        member_two = create_member(member_id="COC-01002", token="member-two")
        pass_one = AppleWalletPass.create_for_member(member_one)
        pass_two = AppleWalletPass.create_for_member(member_two)
        device_one = AppleWalletDevice.create_or_update("device-one")
        device_two = AppleWalletDevice.create_or_update("device-two")
        AppleWalletRegistration.register(pass_one, device_one)
        AppleWalletRegistration.register(pass_two, device_two)
        db.session.commit()

        response = client.get(
            f"/apple-wallet/v1/devices/{device_one.device_library_identifier}/registrations/{pass_one.pass_type_identifier}"
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert pass_one.serial_number in payload["serialNumbers"]
        assert pass_two.serial_number not in payload["serialNumbers"]


def test_apple_wallet_latest_pass_requires_valid_applepass_auth(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context():
        member = create_member()
        pass_record = AppleWalletPass.create_for_member(member)
        token = pass_record.authentication_token
        pass_type = pass_record.pass_type_identifier
        serial = pass_record.serial_number

    monkeypatch.setattr("app.apple_wallet_build_bundle", lambda _member: BytesIO(b"fake pkpass"))

    valid = client.get(
        f"/apple-wallet/v1/passes/{pass_type}/{serial}",
        headers={"Authorization": f"ApplePass {token}"},
    )
    assert valid.status_code == 200

    invalid = client.get(
        f"/apple-wallet/v1/passes/{pass_type}/{serial}",
        headers={"Authorization": "ApplePass wrong-token"},
    )
    assert invalid.status_code == 401


def test_apple_wallet_registration_rejects_cross_member_access(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context():
        member_one = create_member(member_id="COC-01001", token="member-one")
        member_two = create_member(member_id="COC-01002", token="member-two")
        pass_one = AppleWalletPass.create_for_member(member_one)
        pass_two = AppleWalletPass.create_for_member(member_two)
        token = pass_one.authentication_token
        pass_type = pass_two.pass_type_identifier
        serial = pass_two.serial_number

    monkeypatch.setattr("app.apple_wallet_build_bundle", lambda _member: BytesIO(b"fake pkpass"))

    response = client.get(
        f"/apple-wallet/v1/passes/{pass_type}/{serial}",
        headers={"Authorization": f"ApplePass {token}"},
    )
    assert response.status_code == 403


def test_apple_wallet_update_tag_bumps_after_redeem_and_undo(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")
    with flask_app.app_context():
        member = create_member(remaining_changes=1, total_changes=1)
        pass_record = AppleWalletPass.create_for_member(member)
        before = pass_record.last_updated
        member.remaining_changes = 0
        member.status = "active"
        pass_record.mark_updated()
        after = pass_record.last_updated
        assert after > before

        member.remaining_changes = 1
        pass_record.mark_updated()
        assert pass_record.last_updated > after


def test_apple_wallet_update_failure_does_not_rollback_business_transaction(client, monkeypatch):
    monkeypatch.setenv("APPLE_PASS_TOKEN_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    with flask_app.app_context():
        member = create_member(remaining_changes=2)
        AppleWalletPass.create_for_member(member)
        member.remaining_changes = 1
        original = member.remaining_changes
        try:
            with flask_app.app_context():
                member.remaining_changes = 0
                db.session.commit()
                raise RuntimeError("simulated wallet update failure")
        except RuntimeError:
            pass
        assert member.remaining_changes == 0


def test_apple_wallet_download_route_rejects_invalid_token(client, monkeypatch):
    monkeypatch.setattr("app.apple_wallet_build_bundle", lambda _member: pytest.fail("builder should not run"))

    response = client.get("/m/does-not-exist/apple-wallet")

    assert response.status_code == 404


def test_google_wallet_payload_still_uses_public_member_card(client, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://cards.carnova.test")

    with flask_app.app_context(), flask_app.test_request_context("/"):
        member = create_member()
        payload = google_wallet_member_object_payload(member)

    assert payload["header"]["defaultValue"]["value"] == "Oil Club Premium"
    assert payload["subheader"]["defaultValue"]["value"] == "Apple Wallet Member"
    assert payload["textModulesData"][0]["body"] == "2 OIL CHANGES REMAINING"
    assert payload["linksModuleData"]["uris"][0]["uri"] == "https://cards.carnova.test/m/apple-wallet-token"
