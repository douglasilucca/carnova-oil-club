import json
from datetime import datetime, timezone

from sqlalchemy import inspect

from app import app, db, run_all_reminders


REQUIRED_TABLES = ("member", "appointment", "reminder_log")


def _missing_required_tables():
    existing_tables = set(inspect(db.engine).get_table_names())
    return [table_name for table_name in REQUIRED_TABLES if table_name not in existing_tables]


def main():
    try:
        with app.app_context():
            missing_tables = _missing_required_tables()
            if missing_tables:
                missing = ", ".join(missing_tables)
                raise RuntimeError(
                    "Database schema is not initialized for reminders; "
                    f"missing tables: {missing}. "
                    "Cron runner will not initialize database state."
                )

            summary = run_all_reminders(reference_datetime=datetime.now(timezone.utc))

        output = {
            "status": "ok",
            "sent": summary.get("sent", 0),
            "skipped": summary.get("skipped", 0),
            "failed": summary.get("failed", 0),
            "renewal": {
                "sent": summary.get("renewal", {}).get("sent", 0),
                "skipped": summary.get("renewal", {}).get("skipped", 0),
                "failed": summary.get("renewal", {}).get("failed", 0),
            },
            "unused_benefit": {
                "sent": summary.get("unused_benefit", {}).get("sent", 0),
                "skipped": summary.get("unused_benefit", {}).get("skipped", 0),
                "failed": summary.get("unused_benefit", {}).get("failed", 0),
            },
            "appointment_morning": {
                "sent": summary.get("appointment_morning", {}).get("sent", 0),
                "skipped": summary.get("appointment_morning", {}).get("skipped", 0),
                "failed": summary.get("appointment_morning", {}).get("failed", 0),
            },
        }
        print(json.dumps(output, separators=(",", ":")))
        return 0
    except Exception as error:
        print(json.dumps({"status": "error", "message": str(error)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
