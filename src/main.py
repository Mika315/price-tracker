import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, upsert_tracker
from tracker import run_all_trackers

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "trackers.json")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print("No trackers.json found. Add trackers via the dashboard.")
        return []
    with open(CONFIG_PATH) as f:
        return json.load(f)


if __name__ == "__main__":
    init_db()
    uid = os.getenv("IMPORT_USER_ID", "").strip()
    if not uid:
        print(
            "Set IMPORT_USER_ID to a user id from the users table (create an account in the app first).",
            file=sys.stderr,
        )
        sys.exit(1)
    for t in load_config():
        t = dict(t)
        t["user_id"] = uid
        upsert_tracker(t)
    run_all_trackers()