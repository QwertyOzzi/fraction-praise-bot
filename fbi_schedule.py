"""FBI rank-scoped event notifications and scheduled Telegram posts.

Uses the already owner-protected Firestore fbi_announcements collection, with
a separate indexed scheduleMarker field. Every scheduled dispatch is claimed
*before* Telegram sending with a Firestore revision precondition: at most one
worker will attempt it even across Render restarts. On a crash between claim
and Telegram send delivery is uncertain and will NOT be blindly retried.
"""
import asyncio
import copy
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)
TZ_NAME = "Europe/Moscow"
TZ = ZoneInfo(TZ_NAME)
KINDS = ("event", "message")
NEWS_COLLECTION = "fbi_announcements"
MAX_ACTIVE = 20
MAX_TARGETS = 200
MAX_FUTURE_DAYS = 60
VALID_ID = re.compile(r"sched-[0-9a-f]{20}\Z")
BLOCKED_STATUSES = frozenset((
    "pending", "needs_proof", "proof_submitted", "disabled", "disconnected",
    "retry_ready", "cooldown"
))


class ScheduleError(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def from_iso(value):
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("naive date")
        return when.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ScheduleError("Некорректная дата в календаре.") from exc


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_moscow_date(date_text, time_text, *, now=None):
    """Time fields use explicit MSK (UTC+3); no server-local timezone guessing."""
    try:
        local = datetime.strptime(date_text.strip() + " " + time_text.strip(),
                                  "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
    except (ValueError, AttributeError) as exc:
        raise ScheduleError("Укажите дату ДД.ММ.ГГГГ и время ЧЧ:ММ по МСК.") from exc
    utc_time = local.astimezone(timezone.utc)
    now = now or utcnow()
    if not now + timedelta(minutes=2) <= utc_time <= now + timedelta(days=MAX_FUTURE_DAYS):
        raise ScheduleError("Выберите время не раньше чем через 2 минуты и не позже 60 дней.")
    return utc_time


def moscow_label(value):
    when = from_iso(value) if isinstance(value, str) else value
    return when.astimezone(TZ).strftime("%d.%m.%Y %H:%M МСК")


def parse_ranks(value):
    value = (value or "").strip().upper().replace("X", "10")
    match = re.fullmatch(r"(10|[1-9])(?:\s*[-–]\s*(10|[1-9]))?", value)
    if not match:
        raise ScheduleError("Ранги: 1-10, 5-9, 8-X или один ранг (например 7).")
    minimum = int(match.group(1))
    maximum = int(match.group(2) or minimum)
    if minimum > maximum:
        raise ScheduleError("Минимальный ранг не может превышать максимальный.")
    return minimum, maximum


def current_rank(value):
    raw = str(value or "").strip().upper()
    if raw == "X":
        return 10
    match = re.match(r"^(10|[1-9])(?:[^0-9]|$)", raw)
    return int(match.group(1)) if match else None


def eligible_recipients(users, lower, upper):
    """Dynamic rank filter, no private/group chats, disabled agents opt-out."""
    targets = []
    for key, profile in users.items():
        if not isinstance(profile, dict) or not str(key).isdecimal():
            continue
        uid = int(key)
        rank = current_rank(profile.get("rank"))
        status = str(profile.get("verification_status") or "")
        if (uid <= 0 or rank is None or not lower <= rank <= upper
                or status in BLOCKED_STATUSES
                or profile.get("event_notifications") is False
                or not profile.get("nick")):
            continue
        targets.append(uid)
    return sorted(set(targets))[:MAX_TARGETS]


def _job(name, date):
    return {"id": name, "due_at": utc_iso(date), "state": "pending"}


def prepare_schedule(kind, title, description, date_text, time_text,
                     ranks, *, creator_id, reminders=(60, 15), repeat="none",
                     now=None):
    now = now or utcnow()
    at = parse_moscow_date(date_text, time_text, now=now)
    title, description = (title or "").strip(), (description or "").strip()
    if kind not in KINDS or not 3 <= len(title) <= 80:
        raise ScheduleError("Название должно содержать 3–80 символов.")
    if not 3 <= len(description) <= 900:
        raise ScheduleError("Текст / место сбора: 3–900 символов.")
    if type(creator_id) is not int or creator_id <= 0:
        raise ScheduleError("Не подтверждён автор.")
    lo, hi = parse_ranks(ranks)
    jobs = []
    if kind == "event":
        if set(reminders).difference((15, 60, 1440)):
            raise ScheduleError("Недопустимое напоминание.")
        jobs.append(_job("announcement", now))
        for minutes in sorted(set(reminders), reverse=True):
            due = at - timedelta(minutes=minutes)
            if due > now:
                jobs.append(_job(f"before_{minutes}", due))
    else:
        if repeat not in ("none", "daily", "weekly"):
            raise ScheduleError("Повтор: один раз, ежедневно или еженедельно.")
        step = {"none": None, "daily": timedelta(days=1),
                "weekly": timedelta(days=7)}[repeat]
        occurrences = 1 if step is None else 4
        if step and at + step * (occurrences - 1) > now + timedelta(days=MAX_FUTURE_DAYS):
            raise ScheduleError("Повторения выходят за пределы 60 дней.")
        jobs = [_job(f"post_{i+1}", at + step * i if step else at)
                for i in range(occurrences)]
    return {
        "id": "sched-" + uuid.uuid4().hex[:20],
        "kind": kind,
        "title": title, "description": description,
        "at": utc_iso(at), "min_rank": lo, "max_rank": hi,
        "creator_id": creator_id, "timezone": TZ_NAME,
        "created_at": utc_iso(now),
        "status": "active", "jobs": jobs,
        "repeat": repeat if kind == "message" else "none",
    }


def job_message(item, job):
    """Plain text; no parse_mode to avoid malformed Telegram HTML."""
    ranks = f"{item['min_rank']}–{item['max_rank']}"
    if item["kind"] == "event":
        if job["id"] == "announcement":
            heading = "📢 FBI · Новое мероприятие"
        else:
            mins = int(job["id"].split("_", 1)[1])
            lead = "1 день" if mins == 1440 else f"{mins} мин."
            heading = f"⏰ FBI · До мероприятия осталось {lead}"
        return (
            f"{heading}\n\n"
            f"🏷 {item['title']}\n"
            f"📅 {moscow_label(item['at'])}\n"
            f"🎖 Ранги: {ranks}\n"
            f"📍 Информация: {item['description']}\n\n"
            "Уведомление руководства FBI"
        )
    return (
        f"📨 FBI · Автоматическое объявление\n\n"
        f"📌 {item['title']}\n\n{item['description']}\n\n"
        f"🎖 Для рангов {ranks}"
    )


def next_pending(item):
    jobs = [j for j in item.get("jobs", []) if j.get("state") == "pending"]
    return min(jobs, key=lambda j: j["due_at"]) if jobs else None


def display_schedule(item):
    heading = "🎯 Мероприятие" if item["kind"] == "event" else "📨 Автообъявление"
    status = {"active": "Активно", "cancelled": "Отменено",
              "completed": "Завершено"}.get(item.get("status"), "Неизвестно")
    pending = sum(j.get("state") == "pending" for j in item["jobs"])
    sent = sum(int(j.get("sent", 0)) for j in item["jobs"])
    failed = sum(int(j.get("failed", 0)) for j in item["jobs"])
    return (
        f"{heading}\n\n"
        f"Название: {item['title']}\n"
        f"Дата: {moscow_label(item['at'])}\n"
        f"Ранги: {item['min_rank']}–{item['max_rank']}\n"
        f"Описание: {item['description']}\n"
        f"Статус: {status}\n"
        f"Осталось отправок: {pending}\n"
        f"Доставлено сообщений: {sent}\n"
        f"Ошибок отправки: {failed}\n"
        f"ID: {item['id']}"
    )


def _base(client):
    return (f"https://firestore.googleapis.com/v1/projects/{client.project_id}"
            "/databases/(default)/documents")


def _resource(client, sid):
    return (f"projects/{client.project_id}/databases/(default)/documents/"
            f"{NEWS_COLLECTION}/{sid}")


async def _headers(client):
    token = await client.get_token()
    if not token:
        raise ScheduleError("Firebase недоступна.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _decode(doc):
    try:
        item = json.loads(doc["fields"]["payload"]["stringValue"])
        if (not isinstance(item, dict) or not VALID_ID.fullmatch(str(item.get("id")))
                or doc["name"].rsplit("/", 1)[-1] != item["id"]
                or item.get("kind") not in KINDS):
            raise ValueError("Invalid document")
        return item
    except (ValueError, TypeError, KeyError) as exc:
        raise ScheduleError("Повреждённая запись расписания.") from exc


async def get_schedule(client, sid):
    if not isinstance(sid, str) or not VALID_ID.fullmatch(sid):
        raise ScheduleError("Недопустимый ID.")
    headers = await _headers(client)
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        async with session.get(_base(client) + "/" + NEWS_COLLECTION + "/" + sid,
                               headers=headers) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise ScheduleError(f"Firebase не прочитала расписание (HTTP {resp.status}).")
            doc = await resp.json()
    if not doc.get("updateTime"):
        raise ScheduleError("У записи нет версии.")
    return _decode(doc), doc["updateTime"]


async def list_active(client):
    """Run a single-field indexed query, not newest-100 announcements."""
    headers = await _headers(client)
    query = {"structuredQuery": {
        "from": [{"collectionId": NEWS_COLLECTION}],
        "where": {"fieldFilter": {
            "field": {"fieldPath": "scheduleMarker"},
            "op": "EQUAL", "value": {"stringValue": "active"}
        }},
        "limit": 100
    }}
    async with ClientSession(timeout=ClientTimeout(total=18)) as session:
        async with session.post(_base(client) + ":runQuery",
                                headers=headers, json=query) as resp:
            if resp.status != 200:
                raise ScheduleError(f"Firebase не вернула расписание (HTTP {resp.status}).")
            data = await resp.json()
    results = []
    for item in data:
        doc = item.get("document", {})
        if not doc:
            continue
        try:
            payload = _decode(doc)
            if payload.get("status") == "active":
                results.append(payload)
        except ScheduleError:
            logger.warning("Skipping malformed scheduled document")
    return sorted(results, key=lambda row: row["at"])


async def create_schedule(client, item):
    if not VALID_ID.fullmatch(item["id"]):
        raise ScheduleError("Неправильный ID мероприятия.")
    active = await list_active(client)
    if len(active) >= MAX_ACTIVE:
        raise ScheduleError(f"Лимит: {MAX_ACTIVE} активных мероприятий и объявлений.")
    headers = await _headers(client)
    now = utc_iso(utcnow())
    write = {"update": {
        "name": _resource(client, item["id"]),
        "fields": {
            "payload": {"stringValue": json.dumps(item, ensure_ascii=False)},
            "scheduleMarker": {"stringValue": "active"},
            "createdAt": {"timestampValue": now},
            "updatedAt": {"timestampValue": now},
        }}, "currentDocument": {"exists": False}
    }
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        async with session.post(_base(client) + ":commit",
                                headers=headers, json={"writes": [write]}) as resp:
            if resp.status != 200:
                raise ScheduleError(f"Firebase не подтвердила создание (HTTP {resp.status}).")
    return item


async def _replace(client, item, revision):
    """Revision-checked update and durable active/completed index marker."""
    marker = "active" if item["status"] == "active" else "archived"
    write = {"update": {
        "name": _resource(client, item["id"]),
        "fields": {
            "payload": {"stringValue": json.dumps(item, ensure_ascii=False)},
            "scheduleMarker": {"stringValue": marker},
            "updatedAt": {"timestampValue": utc_iso(utcnow())}
        }},
        "updateMask": {"fieldPaths": ["payload", "scheduleMarker", "updatedAt"]},
        "currentDocument": {"updateTime": revision}
    }
    headers = await _headers(client)
    async with ClientSession(timeout=ClientTimeout(total=15)) as session:
        async with session.post(_base(client) + ":commit",
                                headers=headers, json={"writes": [write]}) as resp:
            if resp.status == 200:
                return True
            if resp.status in (409, 412):
                return False
            raise ScheduleError(f"Firebase не подтвердила изменение (HTTP {resp.status}).")


async def cancel_schedule(client, sid):
    for _ in range(3):
        result = await get_schedule(client, sid)
        if not result:
            return "missing"
        item, revision = result
        if item["status"] != "active":
            return "inactive"
        item["status"] = "cancelled"
        if await _replace(client, item, revision):
            return "cancelled"
    raise ScheduleError("Конфликт изменений. Повторите попытку.")


async def claim_schedule_job(client, sid, job_id, *, now=None):
    """At-most-once claim across all workers. Never retransmit claimed jobs."""
    now = now or utcnow()
    for _ in range(3):
        result = await get_schedule(client, sid)
        if not result:
            return None
        item, revision = result
        if item["status"] != "active":
            return None
        job = next((j for j in item["jobs"] if j["id"] == job_id), None)
        if not job or job["state"] != "pending" or from_iso(job["due_at"]) > now:
            return None
        old = copy.deepcopy(item)
        claim = uuid.uuid4().hex
        # Delayed announcements are not sent if their meaning has expired.
        expired = (item["kind"] == "event" and now >= from_iso(item["at"])
                   or item["kind"] == "message"
                   and now > from_iso(job["due_at"]) + timedelta(hours=6))
        job["state"] = "skipped" if expired else "claimed"
        job["claim_id"] = claim
        job["claimed_at"] = utc_iso(now)
        if item["kind"] == "message" and not any(
                j["state"] == "pending" for j in item["jobs"]):
            item["status"] = "completed"
        elif item["kind"] == "event" and now >= from_iso(item["at"]):
            item["status"] = "completed"
        if await _replace(client, item, revision):
            return None if expired else old, claim
    return None


async def finish_schedule_job(client, sid, job_id, claim, sent, failed):
    for _ in range(3):
        result = await get_schedule(client, sid)
        if not result:
            return False
        item, revision = result
        job = next((j for j in item["jobs"] if j["id"] == job_id), None)
        if not job or job["state"] != "claimed" or job.get("claim_id") != claim:
            return False
        job["state"] = "done"
        job["sent"] = int(sent)
        job["failed"] = int(failed)
        job["finished_at"] = utc_iso(utcnow())
        if item["kind"] == "message" and not any(
                j["state"] == "pending" for j in item["jobs"]):
            item["status"] = "completed"
        if await _replace(client, item, revision):
            return True
    return False


async def retire_expired_event(client, sid, *, now=None):
    result = await get_schedule(client, sid)
    if not result:
        return False
    item, revision = result
    now = now or utcnow()
    if (item["status"] == "active" and item["kind"] == "event"
            and now >= from_iso(item["at"])):
        item["status"] = "completed"
        return await _replace(client, item, revision)
    return False


async def scheduled_tick(client, users, deliver, *, now=None):
    """One scan; caller supplies async Telegram send(uid,text) function."""
    now = now or utcnow()
    items = await list_active(client)
    for item in items:
        if item["kind"] == "event" and now >= from_iso(item["at"]):
            # Claim expired event jobs first (skipped), then retire.
            for job in item["jobs"]:
                if job["state"] == "pending" and from_iso(job["due_at"]) <= now:
                    await claim_schedule_job(client, item["id"], job["id"], now=now)
            await retire_expired_event(client, item["id"], now=now)
            continue
        for job in item["jobs"]:
            if job["state"] != "pending" or from_iso(job["due_at"]) > now:
                continue
            claim_result = await claim_schedule_job(client, item["id"], job["id"], now=now)
            if not claim_result:
                continue
            claimed_item, claim_token = claim_result
            if not claimed_item:
                continue
            targets = eligible_recipients(users, item["min_rank"], item["max_rank"])
            text = job_message(item, job)
            success, failed = 0, 0
            for uid in targets:
                try:
                    await deliver(uid, text)
                    success += 1
                except Exception as exc:
                    failed += 1
                    logger.info("Scheduled message delivery failed (%s)", type(exc).__name__)
                await asyncio.sleep(0.075)
            finished = await finish_schedule_job(
                client, item["id"], job["id"], claim_token, success, failed)
            if not finished:
                logger.error("Scheduled delivery result not confirmed for %s:%s",
                             item["id"], job["id"])


async def scheduler_loop(client, users, deliver, *, stop_event=None):
    """Bounded worker, survives ordinary Firestore outage without spin/replay."""
    while stop_event is None or not stop_event.is_set():
        try:
            await scheduled_tick(client, users, deliver)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("FBI schedule scan unavailable: %s", type(exc).__name__)
        if stop_event is None:
            await asyncio.sleep(30)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
