"""Independent durable Firestore transactions for agent actions and FBI news.

These collections do not share bot_state requests or admin activity logs.
No unconfirmed agent message can mutate a balance. All balance changes and
their audit events use a single Firestore commit with revision preconditions.
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from natural_aliases import (
    MEMORY_FIELD, decode_alias_book, encode_alias_book, learn_confirmed_aliases,
)
from natural_commands import simplified

logger = logging.getLogger(__name__)

SHEETS = ("fractionrp", "praise")
MAX_AMOUNT = 20
COLLECTION_ACTIONS = "agent_action_logs"
COLLECTION_REQUESTS = "agent_add_requests"
COLLECTION_NEWS = "fbi_announcements"


class AgentOperationError(ValueError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def _resource_name(client, path=""):
    """Firestore document resource name, without HTTP origin or API version.

    Used INSIDE Write.update.name, never as a network request URL.
    """
    name = (f"projects/{client.project_id}"
            "/databases/(default)/documents")
    return name + ("/" + path if path else "")


def _base(client):
    """HTTP endpoint prefix, used only for REST requests."""
    return "https://firestore.googleapis.com/v1/" + _resource_name(client)


def _url(client, path):
    return _base(client) + "/" + path


def _valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{3,90}", value))


def _valid_sheet(sheet, qty):
    if sheet not in SHEETS or type(qty) is not int or not 1 <= qty <= MAX_AMOUNT:
        raise AgentOperationError("Разрешено от 1 до 20 FRP или похвал.")


def _doc_fields(payload, when=None):
    when = when or timestamp()
    return {
        "payload": {"stringValue": json.dumps(payload, ensure_ascii=False,
                                                separators=(",", ":"))},
        "createdAt": {"timestampValue": when}
    }


async def _token(client):
    token = await client.get_token()
    if not token:
        raise AgentOperationError("Firebase недоступен. Операция не выполнена.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def _get(session, client, headers, path, *, optional=False):
    async with session.get(_url(client, path), headers=headers) as response:
        if response.status == 404 and optional:
            return None
        if response.status != 200:
            raise AgentOperationError("Firebase не подтвердил чтение записи.")
        doc = await response.json()
    if not doc.get("updateTime"):
        raise AgentOperationError("Отсутствует версия Firestore.")
    return doc


def _decode(doc):
    if not isinstance(doc, dict):
        raise AgentOperationError("Запись Firestore не найдена.")
    try:
        result = json.loads(doc["fields"]["payload"]["stringValue"])
        if not isinstance(result, dict):
            raise ValueError("non-object")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentOperationError("Данные Firestore повреждены.") from exc


def _create(client, path, payload, when=None):
    return {
        "update": {
            "name": _resource_name(client, path),
            "fields": _doc_fields(payload, when)
        },
        "currentDocument": {"exists": False}
    }


def _replace(client, path, revision, payload):
    return {
        "update": {
            "name": _resource_name(client, path),
            "fields": {
                "payload": {"stringValue": json.dumps(payload, ensure_ascii=False)},
                "updatedAt": {"timestampValue": timestamp()}
            }
        },
        "updateMask": {"fieldPaths": ["payload", "updatedAt"]},
        "currentDocument": {"updateTime": revision}
    }


def _sheet_update(client, sheet, revision, rows, *, aliases=None):
    fields = {
        "rows": {"arrayValue": {"values": [
            client._row_to_firestore(row) for row in rows
        ]}},
        "updatedAt": {"timestampValue": timestamp()}
    }
    mask = ["rows", "updatedAt"]
    if aliases is not None:
        fields[MEMORY_FIELD] = {"stringValue": encode_alias_book(aliases)}
        mask.append(MEMORY_FIELD)
    return {
        "update": {"name": _resource_name(client, f"sheets/{sheet}"), "fields": fields},
        "updateMask": {"fieldPaths": mask},
        "currentDocument": {"updateTime": revision}
    }


async def _commit(session, client, headers, writes):
    async with session.post(_base(client) + ":commit", headers=headers,
                            json={"writes": writes}) as response:
        if response.status == 200:
            return "applied"
        if response.status in (409, 412):
            return "conflict"
        # Never print raw response data, bearer headers or document contents.
        # An HTTP 403 often means the NEW collection rules are not published
        # in Firebase Console, even when firestore.rules is committed to GitHub.
        status = response.status
        provider_code = ""
        try:
            body = await response.json()
            code = body.get("error", {}).get("status", "") if isinstance(body, dict) else ""
            if isinstance(code, str) and re.fullmatch(r"[A-Z_]{2,40}", code):
                provider_code = code
        except (ValueError, TypeError, AttributeError):
            pass
        logger.warning(
            "Firestore agent/news transaction failed: HTTP %s, provider_status=%s",
            status, provider_code or "unknown"
        )
        if status in (401, 403):
            raise AgentOperationError(
                f"Firestore HTTP {status}: доступ запрещён. "
                "Проверьте опубликованные правила для fbi_announcements, "
                "agent_action_logs и agent_add_requests, а также isOwner()."
            )
        if status == 400:
            raise AgentOperationError(
                "Firestore HTTP 400: неверный формат транзакции. "
                "Проверьте логи Render."
            )
        raise AgentOperationError(
            f"Firestore HTTP {status}: запись не подтверждена. "
            "Проверьте журнал, прежде чем повторять операцию."
        )


def _find_target(client, doc, target):
    rows = client._parse_firestore_rows(
        doc.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", [])
    )
    if len(rows) > 3000:
        raise AgentOperationError("Слишком большая таблица для пакетной операции.")
    same = [row for row in rows
            if str(row.get("nick") or "").casefold()
               == str(target.get("nick") or "").casefold()]
    if len(same) != 1:
        raise AgentOperationError("Ник исчез или дублируется. Уточните агента.")
    row = same[0]
    if any(str(row.get(k) or "") != str(target.get(k) or "")
           for k in ("id", "profileId")):
        raise AgentOperationError("Привязка агента изменилась. Создайте новую операцию.")
    return rows, row


def _log(*, op_id, action, actor_id, actor_nick, target, sheet, amount,
         before=None, after=None, request_id="", reviewer_id=None, reason=""):
    return {
        "id": op_id, "source": "agent", "action": action,
        "actor_id": int(actor_id), "actor_nick": str(actor_nick)[:45],
        "target_nick": str(target.get("nick") or "")[:45],
        "target_profileId": str(target.get("profileId") or "")[:100],
        "sheet": sheet, "amount": int(amount),
        "before": before, "after": after,
        "request_id": request_id, "reviewer_id": reviewer_id,
        "reason": reason[:700], "created_at": timestamp()
    }


async def commit_agent_debit(client, session_factory, *,
                             op_id, actor_id, actor_nick,
                             sheet, target, amount, alias="", raw="", reason=""):
    """One debit and a separate agent audit log are committed ATOMICALLY."""
    _valid_sheet(sheet, amount)
    if not isinstance(reason, str) or len(reason) > 200:
        raise AgentOperationError("Некорректная причина списания.")
    if not _valid_id(op_id) or not op_id.startswith("agent-"):
        raise AgentOperationError("Некорректный ID операции.")
    if not isinstance(target, dict):
        raise AgentOperationError("Агент не выбран.")
    headers = await _token(client)
    log_path = f"{COLLECTION_ACTIONS}/{op_id}"
    for retry in range(4):
        async with session_factory() as session:
            existing = await _get(session, client, headers, log_path, optional=True)
            if existing:
                logged = _decode(existing)
                if (logged.get("actor_id") == actor_id
                        and logged.get("action") == "debit"
                        and logged.get("sheet") == sheet):
                    return "already_applied", logged
                raise AgentOperationError("Операция уже использована.")
            doc = await _get(session, client, headers, f"sheets/{sheet}")
            rows, row = _find_target(client, doc, target)
            before = int(row.get("qty") or 0)
            if before != int(target.get("qty") or 0):
                raise AgentOperationError("Баланс изменился. Подтвердите новую операцию.")
            if before < amount:
                raise AgentOperationError("Недостаточно баллов для списания.")
            row["qty"] = before - amount
            if row["qty"] == 0:
                row["status"] = "Выдано"
            now = timestamp()
            old_memory = decode_alias_book(
                doc.get("fields", {}).get(MEMORY_FIELD, {}).get("stringValue", ""),
                sheet=sheet
            )
            new_memory = old_memory
            if (isinstance(alias, str) and 4 <= len(simplified(alias)) <= 50
                    and simplified(alias) in simplified(raw)):
                new_memory = learn_confirmed_aliases(
                    old_memory, [alias], [target], sheet, now=now
                )
            audit = _log(
                op_id=op_id, action="debit", actor_id=actor_id,
                actor_nick=actor_nick, target=target, sheet=sheet,
                amount=-amount, before=before, after=before - amount,
                reason=reason
            )
            writes = [
                _sheet_update(client, sheet, doc["updateTime"], rows,
                              aliases=new_memory if new_memory != old_memory else None),
                _create(client, log_path, audit, now)
            ]
            result = await _commit(session, client, headers, writes)
            if result == "applied":
                return result, audit
        await asyncio.sleep(0.08 * (retry + 1))
    raise AgentOperationError("Изменения в Firebase конфликтуют. Повторите после проверки.")


async def create_add_request(client, session_factory, *,
                             actor_id, actor_nick, sheet, target, amount,
                             reason="", evidence_kind="", evidence_id="",
                             evidence_caption="", raw="", target_alias=""):
    """Persist pending request and its agent log, with NO balance update."""
    _valid_sheet(sheet, amount)
    if not reason and not evidence_kind:
        raise AgentOperationError("При начислении обязательна причина или доказательство.")
    if len(reason) > 700 or len(evidence_caption) > 500:
        raise AgentOperationError("Слишком длинное описание.")
    if evidence_kind not in ("", "photo", "video", "document", "link"):
        raise AgentOperationError("Недопустимый тип доказательства.")
    if evidence_kind and not evidence_id:
        raise AgentOperationError("Нет доказательства.")
    request_id = "ar-" + uuid.uuid4().hex[:20]
    event_id = "agent-request-" + request_id
    now = timestamp()
    req = {
        "id": request_id, "status": "pending", "actor_id": int(actor_id),
        "actor_nick": str(actor_nick)[:45], "sheet": sheet,
        "target": target, "amount": amount, "reason": reason,
        "evidence_kind": evidence_kind, "evidence_id": evidence_id,
        "evidence_caption": evidence_caption,
        "raw": raw[:400], "target_alias": str(target_alias)[:60],
        "created_at": now
    }
    log = _log(op_id=event_id, action="credit_requested",
               actor_id=actor_id, actor_nick=actor_nick,
               target=target, sheet=sheet, amount=amount,
               request_id=request_id, reason=reason)
    headers = await _token(client)
    async with session_factory() as session:
        status = await _commit(session, client, headers, [
            _create(client, f"{COLLECTION_REQUESTS}/{request_id}", req, now),
            _create(client, f"{COLLECTION_ACTIONS}/{event_id}", log, now)
        ])
    if status != "applied":
        raise AgentOperationError("Конфликт сохранения заявки. Повторите.")
    return req


async def get_add_request(client, session_factory, request_id):
    if not _valid_id(request_id) or not request_id.startswith("ar-"):
        raise AgentOperationError("Некорректная заявка.")
    headers = await _token(client)
    async with session_factory() as session:
        doc = await _get(session, client, headers, f"{COLLECTION_REQUESTS}/{request_id}")
    return _decode(doc)


async def decide_add_request(client, session_factory, *,
                             request_id, reviewer_id, decision):
    """Request update, optional credit and agent audit are one Firestore commit."""
    if not _valid_id(request_id) or not request_id.startswith("ar-"):
        raise AgentOperationError("Некорректная заявка.")
    if decision not in ("approve", "reject"):
        raise AgentOperationError("Неизвестное решение.")
    headers = await _token(client)
    req_path = f"{COLLECTION_REQUESTS}/{request_id}"
    event_id = "agent-decision-" + request_id
    for retry in range(5):
        async with session_factory() as session:
            doc = await _get(session, client, headers, req_path)
            request = _decode(doc)
            if request.get("status") != "pending":
                return "already_decided", request
            target, sheet, amount = (request.get("target"),
                                      request.get("sheet"), request.get("amount"))
            _valid_sheet(sheet, amount)
            if not isinstance(target, dict):
                raise AgentOperationError("Повреждённая заявка.")
            req = dict(request)
            req["status"] = "approved" if decision == "approve" else "rejected"
            req["reviewer_id"] = int(reviewer_id)
            req["decided_at"] = timestamp()
            writes = []
            before = after = None
            if decision == "approve":
                sheet_doc = await _get(session, client, headers, f"sheets/{sheet}")
                rows, row = _find_target(client, sheet_doc, target)
                before = int(row.get("qty") or 0)
                after = before + amount
                row["qty"] = after
                row["status"] = "На выдаче"
                alias = request.get("raw", "")
                memory = decode_alias_book(
                    sheet_doc.get("fields", {}).get(MEMORY_FIELD, {}).get("stringValue", ""),
                    sheet=sheet
                )
                # Memorize the approved agent alias only when it was present
                # literally in the input. Never bind a model-invented nickname.
                from natural_commands import simplified as norm
                short_name = str(request.get("target_alias") or "")
                new_memory = memory
                if short_name and norm(short_name) in norm(alias):
                    new_memory = learn_confirmed_aliases(
                        memory, [short_name], [target], sheet
                    )
                writes.append(_sheet_update(
                    client, sheet, sheet_doc["updateTime"], rows,
                    aliases=new_memory if memory != new_memory else None
                ))
            writes.extend([
                _replace(client, req_path, doc["updateTime"], req),
                _create(client, f"{COLLECTION_ACTIONS}/{event_id}",
                        _log(op_id=event_id,
                             action="credit_approved" if decision == "approve"
                             else "credit_rejected",
                             actor_id=request["actor_id"],
                             actor_nick=request["actor_nick"],
                             target=target, sheet=sheet, amount=amount,
                             before=before, after=after,
                             request_id=request_id, reviewer_id=reviewer_id,
                             reason=str(request.get("reason") or "")))
            ])
            result = await _commit(session, client, headers, writes)
            if result == "applied":
                return result, req
        await asyncio.sleep(0.09 * (retry + 1))
    raise AgentOperationError("Заявка изменена другим руководителем. Обновите список.")


async def list_collection(client, session_factory, collection, *, limit=40):
    if collection not in (COLLECTION_REQUESTS, COLLECTION_ACTIONS, COLLECTION_NEWS):
        raise AgentOperationError("Недоступная коллекция.")
    headers = await _token(client)
    url = _url(client, collection)
    async with session_factory() as session:
        async with session.get(url, headers=headers, params={"pageSize": min(limit, 100),
                                                         "orderBy": "createdAt desc"}) as response:
            if response.status == 404:
                return []
            if response.status != 200:
                raise AgentOperationError("Не удалось открыть журнал Firebase.")
            data = await response.json()
    result = []
    for doc in data.get("documents", []):
        try:
            item = _decode(doc)
            result.append(item)
        except AgentOperationError:
            continue
    return sorted(result, key=lambda r: r.get("created_at", ""), reverse=True)


async def create_announcement(client, session_factory, *, actor_id, title,
                              message, category, recipients, news_id=None):
    if category not in ("news", "update"):
        raise AgentOperationError("Некорректная категория новости.")
    if not 1 <= len(message.strip()) <= 3000 or len(title) > 90:
        raise AgentOperationError("Текст новости должен содержать от 1 до 3000 символов.")
    if news_id is None:
        news_id = "news-" + uuid.uuid4().hex[:20]
    if not _valid_id(news_id) or not re.fullmatch(r"news-[a-f0-9]{16,32}", news_id):
        raise AgentOperationError("Некорректный идентификатор рассылки.")
    payload = {
        "id": news_id, "actor_id": int(actor_id), "title": title[:90],
        "message": message[:3000], "category": category,
        "status": "sending", "recipients": int(recipients),
        "sent": 0, "failed": 0, "created_at": timestamp()
    }
    headers = await _token(client)
    async with session_factory() as session:
        status = await _commit(session, client, headers,
                               [_create(client, f"{COLLECTION_NEWS}/{news_id}", payload)])
    if status == "applied":
        return payload
    # A previous request may have committed successfully but its HTTP response
    # was lost. Retry with the SAME news ID, verify exact contents and reuse it
    # instead of making a second announcement or blindly sending duplicates.
    if status == "conflict":
        async with session_factory() as session:
            existing = await _get(
                session, client, headers,
                f"{COLLECTION_NEWS}/{news_id}", optional=True
            )
        if existing:
            persisted = _decode(existing)
            keys = ("id", "actor_id", "title", "message", "category", "recipients")
            if all(persisted.get(key) == payload.get(key) for key in keys):
                # A stored record might already have been broadcast before a
                # Render restart. Never automatically resend this message.
                return {**persisted, "_already_persisted": True}
    raise AgentOperationError(
        "Не удалось подтвердить сохранение новости. "
        "Проверьте /newslog перед новой попыткой."
    )


async def finish_announcement(client, session_factory, news_id, *, sent, failed):
    if not _valid_id(news_id) or not news_id.startswith("news-"):
        raise AgentOperationError("Некорректный ID новости.")
    headers = await _token(client)
    path = f"{COLLECTION_NEWS}/{news_id}"
    async with session_factory() as session:
        doc = await _get(session, client, headers, path)
        payload = _decode(doc)
        if payload.get("status") != "sending":
            return payload
        payload["sent"] = int(sent)
        payload["failed"] = int(failed)
        payload["status"] = "delivered" if not failed else "partial"
        payload["finished_at"] = timestamp()
        result = await _commit(session, client, headers,
                               [_replace(client, path, doc["updateTime"], payload)])
    if result != "applied":
        raise AgentOperationError("Не удалось подтвердить статус рассылки.")
    return payload


async def get_announcement(client, session_factory, news_id):
    """Fetch an announcement by validated ID, not an untrusted Firestore path."""
    if not isinstance(news_id, str) or not re.fullmatch(r"news-[a-f0-9]{16,32}", news_id):
        raise AgentOperationError("Некорректный ID рассылки.")
    headers = await _token(client)
    async with session_factory() as session:
        doc = await _get(session, client, headers, f"{COLLECTION_NEWS}/{news_id}",
                         optional=True)
    if doc is None:
        return None
    payload = _decode(doc)
    if payload.get("id") != news_id:
        raise AgentOperationError("ID новости в Firestore не совпадает.")
    return payload
