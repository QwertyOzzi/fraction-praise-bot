"""Fine-grained Telegram permissions with backward compatible leadership roles.

Overrides are separately HMAC-signed in the owner-write-only Firestore state.
Existing signed leadership ACL remains untouched for safe migration.
"""
import hashlib
import hmac
import json

PERMISSIONS = (
    "agents.view", "agents.rank", "agents.delete", "department.manage",
    "reports.review", "points.grant", "points.debit", "stats.view",
    "news.publish", "events.manage", "messages.schedule",
)
ROLE_DEFAULTS = {
    "leader": frozenset((
        "agents.view", "agents.rank", "department.manage", "reports.review",
        "points.grant", "points.debit", "stats.view", "news.publish",
        "events.manage", "messages.schedule",
    )),
    "deputy": frozenset((
        "agents.view", "reports.review", "points.grant", "points.debit",
        "stats.view",
    )),
    "agent": frozenset(),
}
# Non-staff may be explicitly delegated only the limited debit capability.
AGENT_DELEGATABLE = frozenset(("points.debit",))


def normalize_overrides(raw):
    if not isinstance(raw, dict):
        raise ValueError("Permission overrides must be a dictionary")
    normalized = {}
    for uid, grants in raw.items():
        if not isinstance(uid, str) or not uid.isdecimal() or int(uid) <= 0:
            raise ValueError("Invalid permission subject")
        if not isinstance(grants, dict):
            raise ValueError("Invalid permission map")
        if set(grants).difference(PERMISSIONS):
            raise ValueError("Unknown permissions")
        if any(type(value) is not bool for value in grants.values()):
            raise ValueError("Permission values must be Boolean")
        normalized[uid] = {key: grants[key] for key in sorted(grants)}
    return normalized


def sign_overrides(raw, secret):
    payload = json.dumps(
        {"domain": "fbi-permission-overrides-v1",
         "overrides": normalize_overrides(raw)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def has_permission(uid, permission, *, owners, admins, roles, overrides, hydrated):
    if permission not in PERMISSIONS or type(uid) is not int or uid <= 0:
        return False
    if uid in owners:
        return True
    if not hydrated:
        return False
    is_staff = uid in admins
    role = roles.get(str(uid), "deputy") if is_staff else "agent"
    if role not in ROLE_DEFAULTS:
        return False
    if not is_staff and permission not in AGENT_DELEGATABLE:
        return False
    setting = overrides.get(str(uid), {}).get(permission)
    if type(setting) is bool:
        return setting
    return permission in ROLE_DEFAULTS[role]


def max_debit_for(uid, *, owners, admins, roles, overrides, hydrated):
    if not has_permission(uid, "points.debit", owners=owners, admins=admins,
                          roles=roles, overrides=overrides, hydrated=hydrated):
        return 0
    if uid in owners:
        return 20
    if uid not in admins:
        return 1
    return 20 if roles.get(str(uid)) == "leader" else 5
