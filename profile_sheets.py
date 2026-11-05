"""Atomic, owner-authenticated profile registration and table synchronization.

No balances, evidence, admin roles or other user records may be overwritten on
concurrent updates. Uses Firestore's documents:commit with updateTime guards.
"""
import asyncio
import hmac
import json
import re
import uuid
import logging
from datetime import datetime, timezone

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)
SHEETS = ("fractionrp", "praise")


async def register_profile(client, project_id, user_id, nick, rank, username,
                           expected_old_nick, signing_function, state_lock):
    """Returns (success, message, previous_nick, updated_profile)."""
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", nick or "") or "_" not in nick:
        return False, "Ник должен быть в формате Name_Surname.", "", None
    if rank not in ("X", "9", "8", "7", "6", "5", "4", "3", "2", "1"):
        return False, "Неизвестный ранг.", "", None
    base = (f"https://firestore.googleapis.com/v1/projects/{project_id}"
            "/databases/(default)/documents")
    user_id = str(user_id)
    for attempt in range(5):
        try:
            async with state_lock:
                token = await client.get_token()
                if not token:
                    return False, "Firebase не подтвердил вход.", "", None
                headers = {"Authorization": f"Bearer {token}"}
                async with ClientSession(timeout=ClientTimeout(total=22)) as session:
                    async def fetch(path):
                        async with session.get(base + "/" + path, headers=headers) as response:
                            if response.status != 200:
                                raise RuntimeError(f"Document read failed ({response.status})")
                            data = await response.json()
                            if not data.get("updateTime"):
                                raise RuntimeError("Document revision missing")
                            return data

                    paths = ["config/bot_state", "sheets/fractionrp", "sheets/praise"]
                    responses = await asyncio.gather(*(fetch(p) for p in paths))
                    docs = dict(zip(paths, responses))
                    value = docs[paths[0]].get("fields", {}).get(
                        "data", {}).get("stringValue")
                    if not value:
                        return False, "Профили Firebase недоступны.", "", None
                    state = json.loads(value)
                    users = state.get("users")
                    admins = state.get("admins", [])
                    roles = state.get("admin_roles", {})
                    if not isinstance(users, dict) or not isinstance(admins, list) or not isinstance(roles, dict):
                        return False, "Неверный формат базы.", "", None
                    signature = str(state.get("admin_acl_signature") or "")
                    if ((admins and not signature) or (signature and not
                            hmac.compare_digest(signature, signing_function(admins, roles)))):
                        return False, "Подпись прав администрации не совпала.", "", None

                    stored = users.get(user_id)
                    if stored is not None and not isinstance(stored, dict):
                        return False, "Профиль повреждён.", "", None
                    old = dict(stored or {})
                    if old.get("verification_status") == "disabled":
                        return False, "Доступ отключён руководством.", "", None
                    old_nick = str(old.get("nick") or "")
                    if old_nick.casefold() != str(expected_old_nick or "").casefold():
                        return False, "Профиль изменился. Откройте /start и повторите.", "", None
                    binding = str(old.get("profileId") or uuid.uuid4().hex)
                    if not re.fullmatch(r"[0-9a-f]{32}", binding):
                        return False, "Некорректная привязка профиля.", "", None
                    conflicting_users = []
                    for uid, entry in users.items():
                        if str(uid) == user_id or not isinstance(entry, dict):
                            continue
                        other_nick = str(entry.get("nick") or "").casefold()
                        if old_nick and other_nick == old_nick.casefold():
                            return False, "Конфликт текущей привязки агента.", "", None
                        if other_nick == nick.casefold():
                            conflicting_users.append(str(uid))
                        if entry.get("profileId") == binding:
                            return False, "Дублирующая привязка агента.", "", None

                    # A taken nickname must be reviewable both for newcomers
                    # AND for agents changing their existing nickname.
                    # Never modify the previous account, rows or balances here.
                    # The current verified nickname and rank stay intact until
                    # leadership approves the new binding.
                    requested_new_nick = old_nick.casefold() != nick.casefold()
                    # Rank changes always require leadership approval even
                    # when a registered agent keeps exactly the same nickname.
                    requested_rank_change = (
                        bool(old_nick)
                        and str(old.get("rank") or "") != rank
                    )
                    sheet_collision = False
                    if requested_new_nick:
                        for sheet in SHEETS:
                            doc = docs["sheets/" + sheet]
                            rows = client._parse_firestore_rows(doc.get("fields", {}).get(
                                "rows", {}).get("arrayValue", {}).get("values", []))
                            if any(str(row.get("nick") or "").casefold() == nick.casefold()
                                   and row.get("profileId") != binding for row in rows):
                                sheet_collision = True

                    if (requested_rank_change or
                            (requested_new_nick and (conflicting_users or sheet_collision))):
                        updated = dict(old)
                        updated.update({
                            "nick": old_nick,
                            "claimed_nick": nick,
                            "claimed_rank": rank,
                            "rank": old.get("rank", rank) if old_nick else rank,
                            "telegram_username": username[:32],
                            "profileId": binding,
                            "verification_status": "pending",
                            "claim_id": uuid.uuid4().hex[:16],
                            "registered_at": old.get("registered_at")
                                             or datetime.now(timezone.utc).isoformat()
                        })
                        updated.pop("disconnected_nick", None)
                        users[user_id] = updated
                        state["users"] = users
                        now = datetime.now(timezone.utc).isoformat()
                        # Only the bot state document is written; the sheets and
                        # conflicting user's registration remain untouched.
                        write = {
                            "update": {
                                "name": f"projects/{project_id}/databases/(default)/documents/config/bot_state",
                                "fields": {
                                    "data": {"stringValue": json.dumps(state, ensure_ascii=False)},
                                    "updatedAt": {"timestampValue": now}
                                }
                            },
                            "updateMask": {"fieldPaths": ["data", "updatedAt"]},
                            "currentDocument": {"updateTime": docs["config/bot_state"]["updateTime"]}
                        }
                        async with session.post(
                            base + ":commit",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"writes": [write]}
                        ) as response:
                            if response.status == 200:
                                return True, "Заявка на проверку ника сохранена.", old_nick, updated
                            if response.status not in (409, 412):
                                return False, f"Firebase отказал в записи (HTTP {response.status}).", "", None
                        continue
                    if conflicting_users:
                        # A contradictory duplicate of the user's current
                        # verified name still requires a manual database fix.
                        return False, "Конфликт существующих профилей.", "", None

                    updated = dict(old)
                    for stale in ("claimed_nick", "claimed_rank", "claim_id", "verification_status",
                                  "disconnected_nick", "cooldown_until",
                                  "proof_submitted", "proof_kind",
                                  "proof_file_id", "proof_link", "proof_caption"):
                        updated.pop(stale, None)
                    updated.update({
                        "nick": nick, "rank": rank, "telegram_username": username[:32],
                        "profileId": binding,
                        "registered_at": old.get("registered_at")
                                         or datetime.now(timezone.utc).isoformat()
                    })
                    sheet_rows = {}
                    for sheet in SHEETS:
                        doc = docs["sheets/" + sheet]
                        rows = client._parse_firestore_rows(doc.get("fields", {}).get(
                            "rows", {}).get("arrayValue", {}).get("values", []))
                        bound = [row for row in rows if row.get("profileId") == binding]
                        legacy = [row for row in rows if not row.get("profileId") and old_nick
                                  and str(row.get("nick") or "").casefold() == old_nick.casefold()]
                        if len(bound) > 1 or len(legacy) > 1 or (bound and legacy):
                            return False, f"Дубли в /{sheet}: нужна проверка администратора.", "", None
                        row = bound[0] if bound else (legacy[0] if legacy else None)
                        if any(str(r.get("nick") or "").casefold() == nick.casefold()
                               and r is not row for r in rows):
                            return False, f"Ник уже занят в /{sheet}.", "", None
                        if row is None:
                            row = {
                                "id": uuid.uuid4().hex, "nick": nick, "rank": rank,
                                "fullName": f"{nick} ({rank})", "qty": 0,
                                "status": "Выдано", "proof": {"text": "", "links": []}
                            }
                            rows.append(row)
                        row.update({
                            "nick": nick, "rank": rank, "fullName": f"{nick} ({rank})",
                            "profileId": binding
                        })
                        sheet_rows[sheet] = rows

                    users[user_id] = updated
                    state["users"] = users
                    now = datetime.now(timezone.utc).isoformat()
                    writes = []
                    for sheet in SHEETS:
                        writes.append({
                            "update": {
                                "name": f"projects/{project_id}/databases/(default)/documents/sheets/{sheet}",
                                "fields": {
                                    "rows": {"arrayValue": {"values": [
                                        client._row_to_firestore(row) for row in sheet_rows[sheet]
                                    ]}},
                                    "updatedAt": {"timestampValue": now}
                                }
                            },
                            "updateMask": {"fieldPaths": ["rows", "updatedAt"]},
                            "currentDocument": {"updateTime": docs["sheets/" + sheet]["updateTime"]}
                        })
                    writes.append({
                        "update": {
                            "name": f"projects/{project_id}/databases/(default)/documents/config/bot_state",
                            "fields": {
                                "data": {"stringValue": json.dumps(state, ensure_ascii=False)},
                                "updatedAt": {"timestampValue": now}
                            }
                        },
                        "updateMask": {"fieldPaths": ["data", "updatedAt"]},
                        "currentDocument": {"updateTime": docs["config/bot_state"]["updateTime"]}
                    })
                    async with session.post(base + ":commit",
                                            headers={**headers, "Content-Type": "application/json"},
                                            json={"writes": writes}) as response:
                        if response.status == 200:
                            return True, "Профиль и обе таблицы сохранены.", old_nick, updated
                        if response.status not in (409, 412):
                            logger.warning("Profile Firestore commit rejected HTTP %s", response.status)
                            return False, f"Firebase отказал в записи (HTTP {response.status}).", "", None
        except Exception as exc:
            logger.warning("Profile sheet synchronization failed: %s", type(exc).__name__)
            return False, "Ошибка связи с Firebase. Ничего не изменено.", "", None
        await asyncio.sleep(0.15 * (attempt + 1))
    return False, "Конфликт изменений, повторите действие.", "", None

async def resolve_nickname_claim(client, project_id, claimant_uid, claim_id,
                                 signing_function, state_lock, protected_ids,
                                 local_users=None):
    """Atomically approve a pending nickname claim without changing balances.

    Returns (success, message, claimant_profile, displaced_profiles).
    Existing owner or approved-admin registrations cannot be displaced.
    """
    base = (f"https://firestore.googleapis.com/v1/projects/{project_id}"
            "/databases/(default)/documents")
    claimant_uid = str(claimant_uid)
    for attempt in range(5):
        try:
            async with state_lock:
                token = await client.get_token()
                if not token:
                    return False, "Firebase недоступен.", None, {}
                headers = {"Authorization": f"Bearer {token}",
                           "Content-Type": "application/json"}
                async with ClientSession(timeout=ClientTimeout(total=22)) as session:
                    paths = ["config/bot_state", "sheets/fractionrp", "sheets/praise"]

                    async def fetch(path):
                        async with session.get(base + "/" + path, headers=headers) as response:
                            if response.status != 200:
                                raise RuntimeError("Firestore read failed")
                            data = await response.json()
                            if not data.get("updateTime"):
                                raise RuntimeError("Firestore version missing")
                            return data

                    docs = dict(zip(paths, await asyncio.gather(
                        *(fetch(path) for path in paths)
                    )))
                    raw = docs["config/bot_state"].get("fields", {}).get(
                        "data", {}).get("stringValue")
                    if not raw:
                        return False, "Отсутствует состояние бота.", None, {}
                    state = json.loads(raw)
                    users = state.get("users")
                    admins = state.get("admins", [])
                    roles = state.get("admin_roles", {})
                    if not isinstance(users, dict) or not isinstance(admins, list) or not isinstance(roles, dict):
                        return False, "Повреждены данные профилей.", None, {}
                    signature = str(state.get("admin_acl_signature") or "")
                    if ((admins and not signature) or
                            (signature and not hmac.compare_digest(
                                signature, signing_function(admins, roles)))):
                        return False, "Не удалось проверить подпись прав.", None, {}

                    applicant = users.get(claimant_uid)
                    if not isinstance(applicant, dict):
                        return False, "Профиль заявителя не найден.", None, {}
                    if (applicant.get("claim_id") != claim_id
                            or applicant.get("verification_status") not in (
                                "pending", "needs_proof", "proof_submitted")):
                        return False, "Заявка уже обработана или устарела.", None, {}
                    nick = str(applicant.get("claimed_nick") or "")
                    rank = str(applicant.get("claimed_rank") or applicant.get("rank") or "")
                    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", nick) or "_" not in nick:
                        return False, "Некорректный ник.", None, {}
                    if rank not in ("X", "9", "8", "7", "6", "5", "4", "3", "2", "1"):
                        return False, "Некорректный ранг.", None, {}
                    holders = [
                        (str(uid), row) for uid, row in users.items()
                        if str(uid) != claimant_uid and isinstance(row, dict)
                        and str(row.get("nick") or "").casefold() == nick.casefold()
                    ]
                    if len(holders) > 1:
                        return False, "Обнаружены дубли профилей. Требуется ручная проверка.", None, {}
                    protected = {str(item) for item in protected_ids} | {
                        str(item) for item in admins if type(item) is int
                    }
                    if any(uid in protected for uid, _ in holders):
                        return False, "Нельзя переназначить ник владельца или руководителя.", None, {}

                    displaced = {}
                    current_binding = str(applicant.get("profileId") or uuid.uuid4().hex)
                    if not re.fullmatch(r"[0-9a-f]{32}", current_binding):
                        return False, "Некорректная привязка профиля.", None, {}
                    old_verified_nick = str(applicant.get("nick") or "")
                    changing_existing_nick = (
                        bool(old_verified_nick)
                        and old_verified_nick.casefold() != nick.casefold()
                    )
                    # When a rank change accompanies a FREE nickname change,
                    # rename the owned rows in place. This preserves all FRP,
                    # praise and evidence rather than creating empty rows.
                    # If another agent already has the target nick, retain
                    # each old balance on its original row and transfer only
                    # the explicitly approved occupied nickname.
                    sheet_rows = {
                        sheet: client._parse_firestore_rows(
                            docs["sheets/" + sheet].get("fields", {}).get(
                                "rows", {}).get("arrayValue", {}).get("values", [])
                        )
                        for sheet in SHEETS
                    }
                    target_row_exists = any(
                        str(row.get("nick") or "").casefold() == nick.casefold()
                        for rows in sheet_rows.values() for row in rows
                    )
                    free_nickname_rename = (
                        changing_existing_nick and not holders
                        and not target_row_exists
                    )
                    target_binding = (
                        uuid.uuid4().hex if changing_existing_nick
                        and not free_nickname_rename else current_binding
                    )
                    if holders:
                        previous_uid, previous = holders[0]
                        displaced_profile = dict(previous)
                        displaced_profile["nick"] = ""
                        displaced_profile["disconnected_nick"] = nick
                        displaced_profile["verification_status"] = "disconnected"
                        displaced_profile["profileId"] = uuid.uuid4().hex
                        users[previous_uid] = displaced_profile
                        displaced[previous_uid] = displaced_profile

                    applicant = dict(applicant)
                    previous_rank = str(applicant.get("rank") or "")
                    if previous_rank and previous_rank != rank:
                        saved_history = applicant.get("rank_history", [])
                        if not isinstance(saved_history, list):
                            saved_history = []
                        applicant["rank_history"] = (saved_history + [{
                            "from": previous_rank, "to": rank,
                            "at": datetime.now(timezone.utc).isoformat(),
                            "actor": "leadership_claim_review"
                        }])[-50:]
                    applicant.update({
                        "nick": nick, "rank": rank, "profileId": target_binding,
                        "verification_status": "verified"
                    })
                    for key in ("claimed_nick", "claimed_rank", "claim_id"):
                        applicant.pop(key, None)
                    for key in ("proof_submitted", "proof_kind", "proof_file_id",
                                "proof_link", "proof_caption"):
                        applicant.pop(key, None)
                    users[claimant_uid] = applicant
                    state["users"] = users

                    now = datetime.now(timezone.utc).isoformat()
                    writes = []
                    for sheet in SHEETS:
                        doc = docs["sheets/" + sheet]
                        rows = sheet_rows[sheet]
                        matches = [row for row in rows
                                   if str(row.get("nick") or "").casefold() == nick.casefold()]
                        if len(matches) > 1:
                            return False, "Дубли в таблицах. Нужна ручная проверка.", None, {}
                        if changing_existing_nick:
                            old_bound = [
                                row for row in rows
                                if row.get("profileId") == current_binding
                                and str(row.get("nick") or "").casefold()
                                    == old_verified_nick.casefold()
                            ]
                            if len(old_bound) > 1:
                                return False, "Дубли прежней привязки. Нужна проверка.", None, {}
                            # Only detach the old profile ID when taking over
                            # an occupied nickname. For a free rename the
                            # original row is the one being renamed.
                            if not free_nickname_rename:
                                for old_row in old_bound:
                                    old_row.pop("profileId", None)
                        if matches:
                            row = matches[0]
                        elif free_nickname_rename and old_bound:
                            row = old_bound[0]
                        else:
                            row = {
                                "id": uuid.uuid4().hex, "nick": nick, "rank": rank,
                                "fullName": f"{nick} ({rank})", "qty": 0,
                                "status": "Выдано", "proof": {"text": "", "links": []}
                            }
                            rows.append(row)
                        row.update({
                            "nick": nick, "rank": rank,
                            "fullName": f"{nick} ({rank})",
                            "profileId": target_binding
                        })
                        writes.append({
                            "update": {
                                "name": f"projects/{project_id}/databases/(default)/documents/sheets/{sheet}",
                                "fields": {
                                    "rows": {"arrayValue": {"values": [
                                        client._row_to_firestore(item) for item in rows
                                    ]}},
                                    "updatedAt": {"timestampValue": now}
                                }
                            },
                            "updateMask": {"fieldPaths": ["rows", "updatedAt"]},
                            "currentDocument": {"updateTime": doc["updateTime"]}
                        })
                    writes.append({
                        "update": {
                            "name": f"projects/{project_id}/databases/(default)/documents/config/bot_state",
                            "fields": {
                                "data": {"stringValue": json.dumps(state, ensure_ascii=False)},
                                "updatedAt": {"timestampValue": now}
                            }
                        },
                        "updateMask": {"fieldPaths": ["data", "updatedAt"]},
                        "currentDocument": {"updateTime": docs["config/bot_state"]["updateTime"]}
                    })
                    async with session.post(base + ":commit", headers=headers,
                                            json={"writes": writes}) as response:
                        if response.status == 200:
                            # Update the in-process profiles before releasing the
                            # state lock, so a concurrent state sync cannot resurrect
                            # the previous nickname ownership.
                            if local_users is not None:
                                local_users[claimant_uid] = applicant
                                for displaced_uid, displaced_profile in displaced.items():
                                    local_users[displaced_uid] = displaced_profile
                            return True, "Ник закреплён. Балансы сохранены.", applicant, displaced
                        if response.status not in (409, 412):
                            return False, "Firebase не подтвердил решение.", None, {}
        except Exception as exc:
            logger.warning("Nickname claim resolution failed: %s", type(exc).__name__)
            return False, "Ошибка Firebase. Ничего не изменено.", None, {}
        await asyncio.sleep(0.15 * (attempt + 1))
    return False, "Конфликт изменений, повторите позже.", None, {}

