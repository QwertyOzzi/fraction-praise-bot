"""Shared Firestore operations for a VK community bot.

No VK process may edit config/bot_state. Telegram owns the legacy profile/ACL
document. VK mappings, requests and review decisions live in separate docs.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession, ClientTimeout
from agent_actions import (
    _base, _create, _decode, _get, _replace, _sheet_update, _commit,
    _token, _resource_name, AgentOperationError, timestamp,
)

BLOCKED = {"pending", "needs_proof", "proof_submitted", "disabled",
           "disconnected", "retry_ready", "cooldown"}
SHEETS = {"frp": "fractionrp", "fractionrp": "fractionrp",
          "praise": "praise", "похвала": "praise"}
NICK = re.compile(r"^[A-Za-z0-9_]{3,30}$")
LINK_TTL = 600
OWNERS = {6060562962}


def db_url(client, path):
    return _base(client) + "/" + path


class Firebase:
    """Owner Firebase Auth user, same Firestore project as the existing bot."""
    def __init__(self):
        self.project_id = os.environ["FIREBASE_PROJECT_ID"]
        self.api_key = os.environ["FIREBASE_API_KEY"]
        self.email = os.environ["FIREBASE_EMAIL"]
        self.password = os.environ["FIREBASE_PASSWORD"]
        self._access = ""
        self._expiry = 0

    async def get_token(self):
        if self._access and time.time() < self._expiry - 60:
            return self._access
        url = ("https://identitytoolkit.googleapis.com/v1/accounts:"
               "signInWithPassword?key=" + self.api_key)
        async with ClientSession(timeout=ClientTimeout(total=15)) as s:
            async with s.post(url, json={
                "email": self.email, "password": self.password,
                "returnSecureToken": True
            }) as r:
                if r.status != 200:
                    raise AgentOperationError("Ошибка входа Firebase.")
                data = await r.json()
        self._access = data["idToken"]
        self._expiry = time.time() + int(data.get("expiresIn", 3600))
        return self._access

    def _parse_firestore_rows(self, values):
        results = []
        for row in values:
            f = row.get("mapValue", {}).get("fields", {})
            q = f.get("qty", {})
            try:
                qty = int(q.get("integerValue", q.get("doubleValue", q.get("stringValue", 0))))
            except (ValueError, TypeError):
                qty = 0
            pf = f.get("proof", {}).get("mapValue", {}).get("fields", {})
            links = []
            for entry in pf.get("links", {}).get("arrayValue", {}).get("values", []):
                p = entry.get("mapValue", {}).get("fields", {})
                links.append({"text": p.get("text", {}).get("stringValue", ""),
                              "url": p.get("url", {}).get("stringValue", "")})
            results.append({
                "id": f.get("id", {}).get("stringValue", ""),
                "profileId": f.get("profileId", {}).get("stringValue", ""),
                "nick": f.get("nick", {}).get("stringValue", ""),
                "fullName": f.get("fullName", {}).get("stringValue", ""),
                "rank": f.get("rank", {}).get("stringValue", ""),
                "status": f.get("status", {}).get("stringValue", "Выдано"),
                "qty": qty,
                "proof": {"text": pf.get("text", {}).get("stringValue", ""),
                          "links": links}
            })
        return results

    def _row_to_firestore(self, row):
        p = row.get("proof") if isinstance(row.get("proof"), dict) else {}
        return {"mapValue": {"fields": {
            **({"profileId": {"stringValue": str(row["profileId"])}}
               if row.get("profileId") else {}),
            "id": {"stringValue": str(row.get("id", ""))},
            "nick": {"stringValue": str(row.get("nick", ""))},
            "fullName": {"stringValue": str(row.get("fullName", ""))},
            "rank": {"stringValue": str(row.get("rank", ""))},
            "status": {"stringValue": str(row.get("status", "Выдано"))},
            "qty": {"integerValue": str(row.get("qty", 0))},
            "proof": {"mapValue": {"fields": {
                "text": {"stringValue": str(p.get("text", ""))},
                "links": {"arrayValue": {"values": [
                    {"mapValue": {"fields": {
                        "text": {"stringValue": str(link.get("text", ""))},
                        "url": {"stringValue": str(link.get("url", ""))}
                    }}} for link in p.get("links", [])[:20]
                ]}}
            }}}
        }}}

    async def get_document(self, path, *, optional=False):
        headers = await _token(self)
        async with ClientSession(timeout=ClientTimeout(total=15)) as s:
            return await _get(s, self, headers, path, optional=optional)

    async def list_documents(self, path, limit=100):
        headers = await _token(self)
        all_docs, page = [], ""
        async with ClientSession(timeout=ClientTimeout(total=20)) as s:
            while len(all_docs) < limit:
                params = {"pageSize": min(100, limit-len(all_docs))}
                if page:
                    params["pageToken"] = page
                async with s.get(db_url(self, path), headers=headers, params=params) as r:
                    if r.status == 404:
                        break
                    if r.status != 200:
                        raise AgentOperationError("Не удалось прочитать список Firebase.")
                    data = await r.json()
                all_docs += data.get("documents", [])
                page = data.get("nextPageToken", "")
                if not page:
                    break
        return all_docs

    async def sheet(self, kind):
        sheet = SHEETS.get(kind, kind)
        if sheet not in ("fractionrp", "praise"):
            raise AgentOperationError("Тип должен быть frp или praise.")
        doc = await self.get_document("sheets/" + sheet)
        return self._parse_firestore_rows(
            doc.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", []))

    async def commit(self, writes):
        headers = await _token(self)
        async with ClientSession(timeout=ClientTimeout(total=20)) as s:
            return await _commit(s, self, headers, writes)


async def create_link_code(client, tg_id):
    """Called only by the Telegram handler, never by unverified VK events."""
    code = secrets.token_urlsafe(10)
    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    payload = {
        "tg_id": int(tg_id),
        "expires_at": (datetime.now(timezone.utc) +
                       timedelta(seconds=LINK_TTL)).isoformat(),
        "used": False
    }
    result = await client_commit(client, [
        _create(client, "vk_link_codes/" + code_hash, payload)
    ])
    if result != "applied":
        raise AgentOperationError("Код привязки не сохранился.")
    return code


async def client_commit(client, writes):
    headers = await _token(client)
    async with ClientSession(timeout=ClientTimeout(total=18)) as s:
        return await _commit(s, client, headers, writes)


async def fetch_doc(client, path, optional=False):
    headers = await _token(client)
    async with ClientSession(timeout=ClientTimeout(total=18)) as s:
        return await _get(s, client, headers, path, optional=optional)


async def redeem_link(client, vk_id, code):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,50}", code):
        raise AgentOperationError("Неправильный формат кода.")
    if not isinstance(vk_id, int) or vk_id <= 0:
        raise AgentOperationError("Некорректный VK ID.")
    path = "vk_link_codes/" + hashlib.sha256(code.encode("ascii")).hexdigest()
    doc = await fetch_doc(client, path, optional=True)
    if not doc:
        raise AgentOperationError("Код не найден или истёк.")
    payload = _decode(doc)
    try:
        expiry = datetime.fromisoformat(payload["expires_at"])
        tg_id = int(payload["tg_id"])
    except (TypeError, KeyError, ValueError) as exc:
        raise AgentOperationError("Повреждённый код привязки.") from exc
    if payload.get("used") or datetime.now(timezone.utc) >= expiry:
        raise AgentOperationError("Код использован или истёк.")
    if (await fetch_doc(client, f"vk_accounts/{vk_id}", optional=True)
            or await fetch_doc(client, f"tg_vk_links/{tg_id}", optional=True)):
        raise AgentOperationError("Аккаунт уже привязан. Отвяжите старый через Telegram.")
    # Identity must exist in the Telegram owner-controlled state.
    snap = await bot_state(client)
    if str(tg_id) not in snap.get("users", {}):
        raise AgentOperationError("Сначала зарегистрируйтесь в Telegram-боте.")
    now = timestamp()
    payload["used"] = True
    result = await client_commit(client, [
        _replace(client, path, doc["updateTime"], payload),
        _create(client, f"vk_accounts/{vk_id}",
                {"vk_id": vk_id, "tg_id": tg_id, "created_at": now}),
        _create(client, f"tg_vk_links/{tg_id}",
                {"vk_id": vk_id, "tg_id": tg_id, "created_at": now})
    ])
    if result != "applied":
        raise AgentOperationError("Другая привязка изменилась. Повторите с новым кодом.")
    return tg_id


async def revoke_link(client, tg_id):
    reverse = await fetch_doc(client, f"tg_vk_links/{tg_id}", optional=True)
    if not reverse:
        return False
    p = _decode(reverse)
    vk_id = int(p["vk_id"])
    forward = await fetch_doc(client, f"vk_accounts/{vk_id}", optional=True)
    writes = [{"delete": _resource_name(client, f"tg_vk_links/{tg_id}"),
               "currentDocument": {"updateTime": reverse["updateTime"]}}]
    if forward and int(_decode(forward).get("tg_id", 0)) == int(tg_id):
        writes.append({"delete": _resource_name(client, f"vk_accounts/{vk_id}"),
                       "currentDocument": {"updateTime": forward["updateTime"]}})
    result = await client_commit(client, writes)
    if result != "applied":
        raise AgentOperationError("Привязка изменилась, повторите.")
    return True


async def bot_state(client):
    doc = await fetch_doc(client, "config/bot_state")
    try:
        state = json.loads(doc["fields"]["data"]["stringValue"])
        if not isinstance(state.get("users"), dict):
            raise ValueError()
        return state
    except (ValueError, KeyError, TypeError) as exc:
        raise AgentOperationError("Состояние пользователей недоступно.") from exc


def _acl_valid(state):
    secret = os.getenv("VK_ACL_SIGNING_KEY", "")
    if not secret or len(secret) < 32:
        return False
    admins = sorted({x for x in state.get("admins", []) if type(x) is int
                     and x > 0 and x not in OWNERS})
    roles = {str(x): (state.get("admin_roles", {}).get(str(x))
                      if state.get("admin_roles", {}).get(str(x)) in
                      ("leader", "deputy") else "deputy") for x in admins}
    signed = json.dumps({"admins": admins, "roles": roles}, ensure_ascii=True,
                        sort_keys=True, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(state.get("vk_acl_signature") or ""), digest)


async def actor_for_vk(client, vk_id):
    doc = await fetch_doc(client, f"vk_accounts/{vk_id}", optional=True)
    if not doc:
        return None
    linked = _decode(doc)
    tg_id = linked.get("tg_id")
    if type(tg_id) is not int or tg_id <= 0:
        return None
    reverse = await fetch_doc(client, f"tg_vk_links/{tg_id}", optional=True)
    if not reverse or _decode(reverse).get("vk_id") != vk_id:
        return None
    state = await bot_state(client)
    p = state.get("users", {}).get(str(tg_id))
    if not isinstance(p, dict) or not NICK.fullmatch(str(p.get("nick") or "")):
        return None
    if p.get("verification_status") in BLOCKED:
        return None
    role = "agent"
    if _acl_valid(state):
        if tg_id in OWNERS:
            role = "owner"
        elif tg_id in state.get("admins", []):
            role = "leader" if state.get("admin_roles", {}).get(str(tg_id)) == "leader" else "deputy"
    return {"vk_id": vk_id, "tg_id": tg_id, "profile": p, "role": role}


def is_staff(actor):
    return bool(actor and actor["role"] in ("owner", "leader", "deputy"))


async def submit_report(client, actor, kind, amount, reason, proof):
    sheet = SHEETS.get(kind)
    if not sheet or type(amount) is not int or not 1 <= amount <= 20:
        raise AgentOperationError("Неверный тип/количество (1–20).")
    if not reason or not proof or len(reason) > 400 or len(proof) > 1200:
        raise AgentOperationError("Нужны причина (до 400) и доказательство (до 1200).")
    request_id = "vr-" + uuid.uuid4().hex[:20]
    payload = {
        "id": request_id, "source": "vk", "status": "pending",
        "vk_id": actor["vk_id"], "tg_id": actor["tg_id"],
        "nick": actor["profile"]["nick"],
        "rank": str(actor["profile"].get("rank") or "8"),
        "sheet": sheet, "amount": amount,
        "reason": reason, "proof": proof, "created_at": timestamp()
    }
    outcome = await client_commit(client, [
        _create(client, "vk_reports/" + request_id, payload)
    ])
    if outcome != "applied":
        raise AgentOperationError("Отчёт не сохранён.")
    return payload


async def get_reports(client, limit=60):
    result = []
    for doc in await client.list_documents("vk_reports", limit):
        try:
            result.append(_decode(doc))
        except AgentOperationError:
            continue
    return sorted(result, key=lambda r: r.get("created_at", ""), reverse=True)


async def decide_report(client, request_id, reviewer, decision):
    if not re.fullmatch(r"vr-[a-f0-9]{20}", request_id or ""):
        raise AgentOperationError("Неверный ID отчёта.")
    if decision not in ("approve", "reject"):
        raise AgentOperationError("Используйте approve или reject.")
    path = "vk_reports/" + request_id
    for attempt in range(4):
        doc = await fetch_doc(client, path)
        p = _decode(doc)
        if p.get("status") != "pending":
            return "already_decided", p
        snap = await bot_state(client)
        holder = snap.get("users", {}).get(str(p.get("tg_id")))
        if not isinstance(holder, dict) or holder.get("verification_status") in BLOCKED:
            raise AgentOperationError("Профиль заявителя больше не подтверждён.")
        if str(holder.get("nick", "")).casefold() != str(p["nick"]).casefold():
            raise AgentOperationError("Игровой ник заявителя изменился.")
        sheet, amount = p.get("sheet"), p.get("amount")
        if sheet not in ("fractionrp", "praise") or type(amount) is not int or not 1 <= amount <= 20:
            raise AgentOperationError("Некорректный отчёт.")
        result = dict(p)
        result["status"] = "approved" if decision == "approve" else "rejected"
        result["reviewer_tg_id"] = reviewer["tg_id"]
        result["decided_at"] = timestamp()
        writes = []
        if decision == "approve":
            current = await fetch_doc(client, "sheets/" + sheet)
            rows = client._parse_firestore_rows(
                current.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", []))
            matching = [r for r in rows if r["nick"].casefold() == p["nick"].casefold()]
            if len(matching) > 1:
                raise AgentOperationError("Дубликаты ника в таблице, нужен ручной разбор.")
            proof = {"text": p["reason"], "links": []}
            if p["proof"].startswith(("https://", "http://")):
                proof["links"] = [{"text": "Доказательство VK", "url": p["proof"]}]
            if matching:
                row = matching[0]
                row["qty"] += amount
                row["status"] = "На выдаче"
                row["proof"] = proof
            else:
                rows.append({"id": str(uuid.uuid4().int)[:17],
                             "nick": p["nick"], "rank": p["rank"],
                             "fullName": p["nick"] + " (" + p["rank"] + ")",
                             "qty": amount, "status": "На выдаче", "proof": proof})
            writes.append(_sheet_update(client, sheet, current["updateTime"], rows))
        writes.extend([
            _replace(client, path, doc["updateTime"], result),
            _create(client, "agent_action_logs/vk-report-" + request_id,
                    {"id": "vk-report-" + request_id, "source": "vk",
                     "action": "report_" + ("approved" if decision == "approve" else "rejected"),
                     "actor_id": reviewer["tg_id"], "actor_nick": reviewer["profile"]["nick"],
                     "target_nick": p["nick"], "sheet": sheet, "amount": amount,
                     "request_id": request_id, "created_at": timestamp()})
        ])
        status = await client_commit(client, writes)
        if status == "applied":
            return status, result
        await asyncio.sleep(.09 * (attempt+1))
    raise AgentOperationError("Конфликт данных. Повторите проверку статуса отчёта.")


async def create_support(client, actor, message):
    if not 1 <= len(message) <= 4000:
        raise AgentOperationError("Текст обращения: 1–4000 символов.")
    now = timestamp()
    data = {
        "nick": {"stringValue": actor["profile"]["nick"]},
        "contact": {"stringValue": "VK ID " + str(actor["vk_id"])},
        "category": {"stringValue": "Обращение из VK"},
        "description": {"stringValue": message},
        "screenshot": {"stringValue": ""},
        "photo_id": {"stringValue": ""},
        "video_id": {"stringValue": ""},
        "status": {"stringValue": "Новое"},
        "source": {"stringValue": "vk"},
        "createdAt": {"stringValue": now},
        "timestamp": {"timestampValue": now}
    }
    headers = await _token(client)
    async with ClientSession(timeout=ClientTimeout(total=18)) as s:
        async with s.post(db_url(client, "support_tickets"), headers=headers,
                          json={"fields": data}) as r:
            if r.status != 200:
                raise AgentOperationError("Обращение не сохранилось.")
            response = await r.json()
    return response["name"].rsplit("/", 1)[-1]
