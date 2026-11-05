"""Prepare a read-only migration projection from legacy sheet arrays.

No Firestore writes are performed by this module. The output maps every existing
row to a stable candidate agent document. Conflicts STOP planning rather than
silently merging people or dropping outstanding FRP/praise.
"""
import copy
import hashlib
import re


class MigrationConflict(ValueError):
    pass


def _binding(value):
    value = str(value or "").strip()
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def candidate_document_id(nick, profile_id=""):
    binding = _binding(profile_id)
    if binding:
        return "profile-" + binding
    return "legacy-" + hashlib.sha256(nick.casefold().encode("utf-8")).hexdigest()[:32]


def build_projection(frp_rows, praise_rows, users=None):
    if not isinstance(frp_rows, list) or not isinstance(praise_rows, list):
        raise MigrationConflict("Both source sheets must contain arrays of rows")
    if users is None:
        users = {}
    if not isinstance(users, dict):
        raise MigrationConflict("Invalid profile directory")
    records = {}
    by_nick = {}
    for sheet, rows in (("fractionrp", frp_rows), ("praise", praise_rows)):
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise MigrationConflict("Invalid sheet row")
            nick = str(row.get("nick") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", nick):
                raise MigrationConflict("A row has a missing or invalid nick")
            key = nick.casefold()
            if key in seen:
                raise MigrationConflict(f"Duplicate nickname in {sheet}: {nick}")
            seen.add(key)
            candidate = candidate_document_id(nick, row.get("profileId"))
            if key in by_nick and by_nick[key] != candidate:
                first = records[by_nick[key]]
                if first["profileId"] and _binding(row.get("profileId")):
                    raise MigrationConflict(f"Conflicting profile IDs: {nick}")
                # Prefer the verified profile-bound identity over an unbound legacy row.
                if first["profileId"]:
                    candidate = by_nick[key]
                elif _binding(row.get("profileId")):
                    records[candidate] = records.pop(by_nick[key])
                    by_nick[key] = candidate
                else:
                    candidate = by_nick[key]
            if candidate in records and records[candidate]["nick"].casefold() != key:
                raise MigrationConflict("One profile ID is attached to multiple nicknames")
            data = records.setdefault(candidate, {
                "nick": nick, "profileId": _binding(row.get("profileId")),
                "sheetRows": {}
            })
            if data["profileId"] and _binding(row.get("profileId")) and (
                    data["profileId"] != _binding(row.get("profileId"))):
                raise MigrationConflict(f"Conflicting profile bindings: {nick}")
            data["profileId"] = data["profileId"] or _binding(row.get("profileId"))
            data["sheetRows"][sheet] = copy.deepcopy(row)
            by_nick[key] = candidate
    # Link registered profiles without granting or assuming owner identity.
    seen_profile_bindings = {}
    for uid, profile in users.items():
        if not isinstance(profile, dict) or not profile.get("nick"):
            continue
        key = str(profile["nick"]).strip().casefold()
        binding = _binding(profile.get("profileId"))
        if binding:
            if binding in seen_profile_bindings and seen_profile_bindings[binding] != str(uid):
                raise MigrationConflict("Duplicated Telegram profile binding")
            seen_profile_bindings[binding] = str(uid)
        if key not in by_nick:
            # Preserve registered users without current FRP/praise rows.
            candidate = candidate_document_id(str(profile["nick"]), binding)
            if candidate in records:
                raise MigrationConflict("Conflicting orphan profile")
            records[candidate] = {
                "nick": str(profile["nick"]), "profileId": binding, "sheetRows": {}
            }
            by_nick[key] = candidate
        candidate = by_nick[key]
        data = records[candidate]
        if data["profileId"] and binding and data["profileId"] != binding:
            raise MigrationConflict("Mismatched sheet and Telegram profile binding")
        data["profileId"] = data["profileId"] or binding
        if "telegramUid" in data:
            raise MigrationConflict("Multiple users share an active nickname")
        data["telegramUid"] = str(uid)
        data["rank"] = str(profile.get("rank") or "?")
        data["department"] = str(profile.get("department") or "")
        data["registeredAt"] = str(profile.get("registered_at") or "")
    # The exact full legacy rows (including id, quantity and proofs) are retained.
    return {key: records[key] for key in sorted(records)}
