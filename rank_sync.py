"""Atomic, identity-checked rank synchronization for a registered FBI agent.

The Telegram profile and existing FRP / Praise rows must agree. The transaction
does not create, delete, award or deduct any sheet row or modify privileges.
"""
import asyncio
import hmac
import json
from datetime import datetime, timezone


SHEETS = ("fractionrp", "praise")
RANKS = {"X", "9", "8", "7", "6", "5", "4", "3", "2", "1"}


class RankSyncError(ValueError):
    """Validation failure or a Firestore transaction that was not committed."""


def prepare_rank_sync(state, sheets, *, uid, nick, expected_rank, new_rank,
                      signing_function, actor_id, timestamp):
    """Prepare rank changes without mutating inputs; fail on identity mismatch.

    Returns (updated state, {sheet: updated rows}, updated profile). Sheet
    row balances and metadata remain untouched. Absent sheet rows are not
    recreated (a site deletion must not silently be undone by a rank edit).
    """
    if new_rank not in RANKS:
        raise RankSyncError("Некорректный новый ранг.")
    if not isinstance(state, dict):
        raise RankSyncError("Неверный формат базы Firebase.")
    users, admins, roles = (state.get("users"), state.get("admins", []),
                            state.get("admin_roles", {}))
    if not isinstance(users, dict) or not isinstance(admins, list) or not isinstance(roles, dict):
        raise RankSyncError("Неверная структура профилей или администрации.")
    signature = str(state.get("admin_acl_signature") or "")
    if ((admins and not signature)
            or (signature and not hmac.compare_digest(
                signature, signing_function(admins, roles)))):
        raise RankSyncError("Подпись прав администрации не совпала.")
    uid = str(uid)
    source_profile = users.get(uid)
    if not isinstance(source_profile, dict):
        raise RankSyncError("Агент больше не зарегистрирован в боте.")
    if (str(source_profile.get("nick") or "").casefold() != str(nick).casefold()
            or not nick):
        raise RankSyncError("Ник изменился. Обновите карточку и повторите.")
    if str(source_profile.get("rank") or "") != expected_rank:
        raise RankSyncError("Ранг уже изменился. Обновите карточку.")
    if source_profile.get("verification_status") in (
            "disabled", "disconnected", "cooldown", "retry_ready",
            "pending", "needs_proof", "proof_submitted"):
        raise RankSyncError("Регистрация агента не подтверждена.")
    # A duplicate Telegram profile must not be silently selected.
    if any(
        str(other_uid) != uid and isinstance(other, dict)
        and str(other.get("nick") or "").casefold() == nick.casefold()
        for other_uid, other in users.items()
    ):
        raise RankSyncError("Этот ник привязан к нескольким пользователям.")

    binding = str(source_profile.get("profileId") or "")
    updated = dict(source_profile)
    if expected_rank != new_rank:
        history = updated.get("rank_history", [])
        if not isinstance(history, list):
            history = []
        updated["rank_history"] = (history + [{
            "from": expected_rank, "to": new_rank, "at": timestamp,
            "actor_id": str(actor_id)
        }])[-50:]
    updated["rank"] = new_rank
    next_state = dict(state)
    next_users = dict(users)
    next_users[uid] = updated
    next_state["users"] = next_users

    next_sheets = {}
    for sheet in SHEETS:
        rows = sheets.get(sheet)
        if not isinstance(rows, list):
            raise RankSyncError("Неверный формат таблицы Firebase.")
        targets = [row for row in rows
                   if isinstance(row, dict)
                   and str(row.get("nick") or "").casefold() == nick.casefold()]
        if len(targets) > 1:
            raise RankSyncError(f"Дубли ника в /{sheet}. Нужна ручная проверка.")
        # A row bound to this profile under another nickname is a stale binding.
        if binding and any(
                isinstance(row, dict) and str(row.get("profileId") or "") == binding
                and str(row.get("nick") or "").casefold() != nick.casefold()
                for row in rows):
            raise RankSyncError(f"Конфликт привязки в /{sheet}.")
        if targets:
            row = targets[0]
            row_binding = str(row.get("profileId") or "")
            if row_binding and row_binding != binding:
                raise RankSyncError(f"Другая привязка ника в /{sheet}.")
            changed = []
            for item in rows:
                if item is row:
                    changed.append({**item, "rank": new_rank,
                                    "fullName": f"{nick} ({new_rank})"})
                else:
                    changed.append(item)
            next_sheets[sheet] = changed
    return next_state, next_sheets, updated


async def update_rank_across_firestore(
    client, project_id, uid, nick, expected_rank, new_rank, actor_id,
    signing_function, state_lock, session_factory,
):
    """Commit bot_state + both existing sheet ranks together, with revisions.

    Firestore commit is all-or-nothing. On 409/412, re-read and retry only if
    identity and expected previous rank still match.
    """
    if new_rank not in RANKS:
        raise RankSyncError("Некорректный новый ранг.")
    base = (f"https://firestore.googleapis.com/v1/projects/{project_id}"
            "/databases/(default)/documents")
    paths = ("config/bot_state", "sheets/fractionrp", "sheets/praise")
    async with state_lock:
        token = await client.get_token()
        if not token:
            raise RankSyncError("Firebase не подтвердил вход.")
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        for attempt in range(5):
            async with session_factory() as session:
                async def fetch(path):
                    async with session.get(base + "/" + path, headers=headers) as response:
                        if response.status != 200:
                            raise RankSyncError(
                                f"Не удалось прочитать {path} (HTTP {response.status})."
                            )
                        document = await response.json()
                    if not document.get("updateTime"):
                        raise RankSyncError("Не найдена версия документа Firebase.")
                    return document

                docs = dict(zip(paths, await asyncio.gather(
                    *(fetch(path) for path in paths)
                )))
                try:
                    raw = docs["config/bot_state"]["fields"]["data"]["stringValue"]
                    state = json.loads(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    raise RankSyncError("Некорректный профиль в Firebase.") from exc
                rows = {}
                for sheet in SHEETS:
                    field = docs["sheets/" + sheet].get("fields", {}).get("rows", {})
                    values = field.get("arrayValue", {}).get("values", [])
                    rows[sheet] = client._parse_firestore_rows(values)

                now = datetime.now(timezone.utc).isoformat()
                next_state, sheet_updates, updated = prepare_rank_sync(
                    state, rows, uid=str(uid), nick=nick, expected_rank=expected_rank,
                    new_rank=new_rank, signing_function=signing_function,
                    actor_id=actor_id, timestamp=now,
                )
                writes = []
                for sheet, new_rows in sheet_updates.items():
                    path = "sheets/" + sheet
                    writes.append({
                        "update": {
                            "name": f"projects/{project_id}/databases/(default)/documents/{path}",
                            "fields": {
                                "rows": {"arrayValue": {"values": [
                                    client._row_to_firestore(row) for row in new_rows
                                ]}},
                                "updatedAt": {"timestampValue": now}
                            }
                        },
                        "updateMask": {"fieldPaths": ["rows", "updatedAt"]},
                        "currentDocument": {"updateTime": docs[path]["updateTime"]}
                    })
                writes.append({
                    "update": {
                        "name": f"projects/{project_id}/databases/(default)/documents/config/bot_state",
                        "fields": {
                            "data": {"stringValue": json.dumps(next_state, ensure_ascii=False)},
                            "updatedAt": {"timestampValue": now},
                        }
                    },
                    "updateMask": {"fieldPaths": ["data", "updatedAt"]},
                    "currentDocument": {
                        "updateTime": docs["config/bot_state"]["updateTime"]
                    }
                })
                async with session.post(base + ":commit", headers=headers,
                                        json={"writes": writes}) as response:
                    if response.status == 200:
                        return updated
                    if response.status not in (409, 412):
                        raise RankSyncError(
                            f"Firebase не сохранил ранг (HTTP {response.status})."
                        )
            await asyncio.sleep(0.12 * (attempt + 1))
    raise RankSyncError("Конфликт одновременных изменений. Повторите позже.")
