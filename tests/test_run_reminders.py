import json
from datetime import datetime, timezone

from flask import has_app_context

import run_reminders


def test_run_reminders_main_runs_once_in_app_context_and_prints_summary(monkeypatch, capsys):
    calls = {"run": 0, "reference_datetime": None}

    def fake_run_all_reminders(reference_date=None, reference_datetime=None):
        assert has_app_context()
        assert reference_date is None
        calls["run"] += 1
        calls["reference_datetime"] = reference_datetime
        return {
            "sent": 3,
            "skipped": 4,
            "failed": 1,
            "renewal": {"sent": 1, "skipped": 1, "failed": 0},
            "unused_benefit": {"sent": 1, "skipped": 1, "failed": 1},
            "appointment_morning": {"sent": 1, "skipped": 2, "failed": 0},
        }

    monkeypatch.setattr(run_reminders, "_missing_required_tables", lambda: [])
    monkeypatch.setattr(run_reminders, "run_all_reminders", fake_run_all_reminders)

    code = run_reminders.main()

    assert code == 0
    assert calls["run"] == 1
    assert isinstance(calls["reference_datetime"], datetime)
    assert calls["reference_datetime"].tzinfo == timezone.utc

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert payload["sent"] == 3
    assert payload["skipped"] == 4
    assert payload["failed"] == 1
    assert payload["appointment_morning"]["sent"] == 1


def test_run_reminders_main_returns_nonzero_when_schema_missing(monkeypatch, capsys):
    monkeypatch.setattr(run_reminders, "_missing_required_tables", lambda: ["member", "appointment"])
    monkeypatch.setattr(run_reminders, "run_all_reminders", lambda **_: {"sent": 0, "skipped": 0, "failed": 0})

    code = run_reminders.main()

    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "error"
    assert "Database schema is not initialized for reminders" in payload["message"]
    assert "member" in payload["message"]
    assert "appointment" in payload["message"]
