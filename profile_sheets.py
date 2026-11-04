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
    if rank not in ("X", "9", "8", "7", "6", "5", "4", "1"):
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
                    old_nick = str(old.get("nick") or "")
                    if old_nick.casefold() != str(expected_old_nick or "").casefold():
                        return False, "Профиль изменился. Откройте /start и повторите.", "", None
                    binding = str(old.get("profileId") or uuid.uuid4().hex)
                    if not re.fullmatch(r"[0-9a-f]{32}", binding):
                        return False, "Некорректная привязка профиля.", "", None
                    for uid, entry in users.items():
                        if str(uid) == user_id or not isinstance(entry, dict):
                            continue
                        other_nick = str(entry.get("nick") or "").casefold()
                        if other_nick == nick.casefold() or (old_nick and other_nick == old_nick.casefold()):
                            return False, "Ник принадлежит другому агенту.", "", None
                        if entry.get("profileId") == binding:
                            return False, "Дублирующая привязка агента.", "", None

                    updated = dict(old)
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
