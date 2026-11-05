"""Offline preview for sheet-rows -> independent agent documents.

This utility NEVER accesses Firestore or connects to a network. It accepts the
JSON export downloaded through the website's "Бэкап и данные" section.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_directory_v2 import build_projection, MigrationConflict  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="No-write FBI roster migration preview")
    parser.add_argument("site_backup", type=Path,
                        help="Site JSON backup (fractionrp and praise arrays)")
    parser.add_argument("--profiles", type=Path,
                        help="Optional separately backed up bot_state users JSON object")
    parser.add_argument("--output", type=Path,
                        help="Optional local JSON file with proposed per-agent records")
    args = parser.parse_args()
    try:
        snapshot = json.loads(args.site_backup.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise MigrationConflict("Backup must be a JSON object")
        if args.profiles:
            profiles = json.loads(args.profiles.read_text(encoding="utf-8"))
            if isinstance(profiles, dict) and isinstance(profiles.get("users"), dict):
                profiles = profiles["users"]
        else:
            profiles = {}
        result = build_projection(
            snapshot.get("fractionrp"), snapshot.get("praise"), profiles)
    except (OSError, ValueError, MigrationConflict) as exc:
        parser.exit(status=2, message="Migration preview rejected: " + str(exc) + "\n")
    print(f"Validated {len(result)} unique candidate agent documents.")
    print("No cloud changes were made.")
    print("Before any live migration: verify backups, security rules, and row equivalence.")
    if args.output:
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Projection written locally to {args.output}")


if __name__ == "__main__":
    main()
