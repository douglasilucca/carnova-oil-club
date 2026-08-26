import json
import hashlib
import os
import subprocess
import zipfile
from datetime import date, time, timedelta
from io import BytesIO
from pathlib import Path

import pytest

from app import (
    Appointment,
    Member,
    apple_wallet_build_bundle,
    apple_wallet_member_serial,
    apple_wallet_next_service_text,
    apple_wallet_payload,
    db,
    google_wallet_member_object_payload,
)
from app import app as flask_app


@pytest.fixture
def client():
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
