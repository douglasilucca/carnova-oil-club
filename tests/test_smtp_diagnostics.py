import smtplib
from unittest.mock import MagicMock, patch

import pytest

from app import send_smtp_email


@pytest.fixture(autouse=True)
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.ionos.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "super-secret-password")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "user@example.com")


def make_server_mock():
    server = MagicMock()
    server.__enter__.return_value = server
    server.__exit__.return_value = False
    return server


def test_connect_timeout_returns_false(capsys):
    with patch("smtplib.SMTP", side_effect=TimeoutError("timed out")):
        result = send_smtp_email("member@example.com", "Subject", "Body")

    assert result is False
    output = capsys.readouterr().out
    assert "EMAIL ERROR [connect]" in output
    assert "TimeoutError" in output


def test_starttls_failure_returns_false(capsys):
    server = make_server_mock()
    server.starttls.side_effect = smtplib.SMTPException("starttls failed")
    with patch("smtplib.SMTP", return_value=server):
        result = send_smtp_email("member@example.com", "Subject", "Body")

    assert result is False
    output = capsys.readouterr().out
    assert "EMAIL ERROR [starttls]" in output


def test_authentication_failure_returns_false(capsys):
    server = make_server_mock()
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"authentication failed")
    with patch("smtplib.SMTP", return_value=server):
        result = send_smtp_email("member@example.com", "Subject", "Body")

    assert result is False
    output = capsys.readouterr().out
    assert "EMAIL ERROR [login]" in output
    assert "SMTPAuthenticationError" in output


def test_send_failure_returns_false(capsys):
    server = make_server_mock()
    server.send_message.side_effect = smtplib.SMTPException("send failed")
    with patch("smtplib.SMTP", return_value=server):
        result = send_smtp_email("member@example.com", "Subject", "Body")

    assert result is False
    output = capsys.readouterr().out
    assert "EMAIL ERROR [send]" in output


def test_successful_delivery_returns_true(capsys):
    server = make_server_mock()
    with patch("smtplib.SMTP", return_value=server):
        result = send_smtp_email("member@example.com", "Subject", "Body")

    assert result is True
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("user@example.com", "super-secret-password")
    server.send_message.assert_called_once()
    output = capsys.readouterr().out
    assert "EMAIL ERROR" not in output


def test_failure_logs_never_contain_secret_password(capsys):
    server = make_server_mock()
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"authentication failed")
    with patch("smtplib.SMTP", return_value=server):
        send_smtp_email("member@example.com", "Subject", "Body")

    output = capsys.readouterr().out
    assert "super-secret-password" not in output


def test_failure_logs_never_contain_activation_link(capsys):
    server = make_server_mock()
    server.send_message.side_effect = smtplib.SMTPException("send failed")
    activation_url = "https://carnovaoil.com/activate/super-secret-activation-token"
    with patch("smtplib.SMTP", return_value=server):
        send_smtp_email(
            "member@example.com",
            "Activate your account",
            f"Use this link: {activation_url}",
        )

    output = capsys.readouterr().out
    assert activation_url not in output
    assert "super-secret-activation-token" not in output
