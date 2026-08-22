import json
from datetime import datetime, timezone

from app import app, init_db, run_all_reminders


def main():
    try:
        with app.app_context():
            init_db()
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
