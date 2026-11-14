"""Telegram Bot API 10.4 profile widgets.

Isolated read-only UI: no new aiogram update types or external HTML URLs.
The existing Telegram inline keyboard requests a refresh; a low-frequency
Firestore poll updates the same widget message when its snapshot changes.

Registry documents are stored separately from config/bot_state to avoid
overwriting roles, agent settings, or the owner's signed access rules.
"""
import asyncio
import html
import json
import logging
import re
import zlib
from datetime import datetime, timezone

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)
COLLECTION = "fbi_profile_widgets"
INVALID_STATUSES = {
    "pending", "needs_proof", "proof_submitted", "disabled",
    "disconnected", "retry_ready", "cooldown",
}
REFRESH_INTERVAL = 90
PRIVATE_NICK = re.compile(r"^[A-Za-z0-9_]{3,30}$")

# All state is changed through Telegram Bot API; the HTML is immutable.
# Render dynamic values with textContent, never innerHTML.
PROFILE_WIDGET_HTML = r"""<style>
body{margin:0;padding:14px;color:var(--tg-theme-text-color);font-family:Arial,sans-serif}
h3{font-size:17px;margin:0 0 3px;font-weight:700}
.small{font-size:12px;color:var(--tg-theme-hint-color)}
.name{font-size:18px;font-weight:700;margin:15px 0 3px;overflow-wrap:anywhere}
.row{display:flex;gap:8px;margin:14px 0}
.stat{flex:1;padding:12px;background:var(--tg-theme-secondary-bg-color);border-radius:12px}
.num{font-size:27px;line-height:1.3;font-weight:700}
.label{font-size:12px;color:var(--tg-theme-hint-color)}
.footer{font-size:12px;color:var(--tg-theme-hint-color);border-top:1px solid var(--tg-theme-section-separator-color);padding-top:9px}
</style>
<h3>🛡️ FBI · Карточка агента</h3>
<div class="small" id="access">Загрузка профиля…</div>
<div class="name" id="nick">—</div>
<div class="small">Ранг: <b id="rank">—</b></div>
<div class="row">
<div class="stat"><div class="label">🎖️ FRP</div><div class="num" id="frp">—</div></div>
<div class="stat"><div class="label">🌟 Похвалы</div><div class="num" id="praise">—</div></div>
</div>
<div class="footer">Сверено с Firebase: <span id="updated">—</span></div>
<script>
(function(){
var W=window.TelegramWidget;
function text(id,value){document.getElementById(id).textContent=String(value==null?'—':value)}
function render(){
var d={};try{d=JSON.parse(W&&W.chatData||'{}')}catch(e){}
text('access',d.status||'Нет данных');
text('nick',d.nick||'—');
text('rank',d.rank||'—');
text('frp',d.frp);
text('praise',d.praise);
text('updated',d.updated||'—');
}
if(W){W.onEvent('chatDataChanged',render)}
render();
if(W&&typeof W.ready==='function'){W.ready()}
})();
</script>"""


class WidgetUnavailable(RuntimeError):
    """Firestore or Telegram refused a widget operation."""


def _validate_widget():
    plain = PROFILE_WIDGET_HTML.encode("utf-8")
    if b"\x00" in plain or len(plain) > 32768 or len(zlib.compress(plain, 9)) > 4096:
        raise ValueError("HTML widget exceeds Bot API 10.4 limits")


_validate_widget()


def _now_label():
    return datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")


def _base(client):
    return ("https://firestore.googleapis.com/v1/projects/"
            + client.project_id + "/databases/(default)/documents/")


def _registry_url(client, uid):
    return _base(client) + COLLECTION + "/" + str(uid)


async def _auth(client):
    token = await client.get_token()
    if not token:
        raise WidgetUnavailable("Firebase authentication unavailable")
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


async def _get_json(session, url, headers, *, missing_ok=False):
    async with session.get(url, headers=headers) as response:
        if response.status == 404 and missing_ok:
            return None
        if response.status != 200:
            # Approved document kinds only; never log URLs or auth headers.
            name = url.rsplit("/", 1)[-1]
            label = name if name in {"bot_state", "fractionrp", "praise"} else "document"
            raise WidgetUnavailable("Firebase %s read failed (HTTP %s)" %
                                    (label, response.status))
        return await response.json()


async def read_roster(client):
    """Read *fresh* cloud identity and both sheets or fail closed.

    A Firebase error must not be displayed as a 0 balance.
    """
    headers = await _auth(client)
    root = _base(client)
    paths = ("config/bot_state", "sheets/fractionrp", "sheets/praise")
    async with ClientSession(timeout=ClientTimeout(total=14)) as session:
        docs = await asyncio.gather(*(
            _get_json(session, root + path, headers) for path in paths
        ))
    try:
        raw = docs[0]["fields"]["data"]["stringValue"]
        profiles = json.loads(raw)["users"]
        if not isinstance(profiles, dict):
            raise ValueError("Invalid profiles")
        sheets = {}
        for key, doc in zip(("fractionrp", "praise"), docs[1:]):
            values = doc["fields"]["rows"].get("arrayValue", {}).get("values", [])
            sheets[key] = client._parse_firestore_rows(values)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise WidgetUnavailable("Firebase profile/sheet structure unavailable") from exc
    return {"users": profiles, "sheets": sheets}


def profile_snapshot(roster, uid):
    """One Telegram ID can only see its own current verified profile."""
    profile = roster["users"].get(str(uid))
    if (not isinstance(profile, dict)
            or not PRIVATE_NICK.fullmatch(str(profile.get("nick") or ""))
            or profile.get("verification_status") in INVALID_STATUSES):
        return {"status": "Доступ не подтверждён", "nick": "—",
                "rank": "—", "frp": "—", "praise": "—"}
    nick = profile["nick"]
    values = {"status": "Подтверждённый агент", "nick": nick,
              "rank": str(profile.get("rank") or "?")[:30]}
    for sheet, name in (("fractionrp", "frp"), ("praise", "praise")):
        matches = [r for r in roster["sheets"][sheet]
                   if str(r.get("nick") or "").casefold() == nick.casefold()]
        # A conflicting/missing row must be visible, not falsely reported as 0.
        values[name] = (str(matches[0].get("qty", 0))
                        if len(matches) == 1 else "—")
        if len(matches) > 1:
            values["status"] = "Конфликт данных: обратитесь к руководству"
    return values


def _stable_data(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _state_data(snapshot):
    value = dict(snapshot)
    value["updated"] = _now_label()
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > 1024:
        raise WidgetUnavailable("Widget state too large")
    return data


def safe_telegram_description(parsed):
    """Return only a short diagnostic, never chat IDs, URLs or API credentials."""
    if not isinstance(parsed, dict):
        return "non-JSON Telegram response"
    description = parsed.get("description")
    if not isinstance(description, str):
        return "no Telegram error details"
    # Bot API error descriptions are generally short and ASCII. Drop any
    # unexpected content and redact numeric identifiers, never leak a URL/token.
    if len(description) > 240 or re.search(r"https?://|api\.telegram|bot\d+:", description, re.I):
        return "Telegram rejected the request"
    safe = re.sub(r"[^a-zA-Z0-9 _.,:()'/-]", "", description).strip()
    safe = re.sub(r"\d{6,}", "[id]", safe)
    return safe[:140] or "Telegram rejected the request"


async def telegram_api(token, method, payload):
    """Small Bot API 10.4 bridge until aiogram supports this release.

    Never log the request URL or raw response (they may contain bot credentials).
    """
    url = "https://api.telegram.org/bot" + token + "/" + method
    async with ClientSession(timeout=ClientTimeout(total=12)) as session:
        async with session.post(url, json=payload) as response:
            try:
                parsed = await response.json(content_type=None)
            except (ValueError, TypeError):
                raise WidgetUnavailable("Telegram %s: malformed API response (HTTP %s)" %
                                        (method, response.status)) from None
            if response.status != 200 or not isinstance(parsed, dict) or not parsed.get("ok"):
                description = safe_telegram_description(parsed)
                if method in {"editMessageWidgetData", "editMessageText"} and "message is not modified" in description.lower():
                    return True
                code = parsed.get("error_code", response.status) if isinstance(parsed, dict) else response.status
                raise WidgetUnavailable(
                    "Telegram %s: %s (HTTP/API %s)" % (method, description, code)
                )
            return parsed.get("result")


def is_widget_blocked(error):
    """Telegram currently restricts HTML widget delivery for ordinary bots."""
    return isinstance(error, WidgetUnavailable) and (
        "INPUT_MEDIA_WIDGET_NOT_ALLOWED" in str(error)
    )


def fallback_card_html(state):
    """Accessible native message card using the same verified Firestore snapshot."""
    values = json.loads(state)
    def safe(key):
        return html.escape(str(values.get(key, "—")), quote=True)

    return (
        "🛡️ <b>FBI · Карточка сотрудника</b>\n\n"
        f"👤 <b>Агент:</b> <code>{safe('nick')}</code>\n"
        f"🎖️ <b>Ранг:</b> {safe('rank')}\n"
        f"🔐 <b>Статус:</b> {safe('status')}\n\n"
        f"🏅 <b>FRP:</b> {safe('frp')}\n"
        f"🌟 <b>Похвалы:</b> {safe('praise')}\n\n"
        f"🕒 Данные Firebase: {safe('updated')}\n\n"
        "<i>HTML Widgets временно ограничены Telegram. "
        "Эта карточка обновляется автоматически и по кнопке ниже.</i>"
    )


async def send_fallback_card(token, uid, state):
    """Native text message used only when Telegram rejects HTML widgets."""
    result = await telegram_api(token, "sendMessage", {
        "chat_id": int(uid),
        "text": fallback_card_html(state),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "protect_content": True,
        "reply_markup": refresh_keyboard(),
    })
    msg_id = result.get("message_id") if isinstance(result, dict) else None
    if type(msg_id) is not int or msg_id < 1:
        raise WidgetUnavailable("Telegram did not return a fallback message ID")
    return msg_id


async def edit_fallback_card(token, uid, msg_id, state):
    return await telegram_api(token, "editMessageText", {
        "chat_id": int(uid), "message_id": int(msg_id),
        "text": fallback_card_html(state), "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": refresh_keyboard(),
    })


def refresh_keyboard():
    return {"inline_keyboard": [[
        {"text": "🔄 Обновить карточку", "callback_data": "fbiwidget:refresh"},
        {"text": "🏠 Меню", "callback_data": "main:open"},
    ]]}


async def send_profile_widget(token, uid, state):
    result = await telegram_api(token, "sendWidget", {
        "chat_id": int(uid),
        "html": PROFILE_WIDGET_HTML,
        "chat_data": state,
        "caption": "Личный кабинет FBI · данные из Firebase",
        "is_shared": False,
        "allow_queries": False,
        "protect_content": True,
        "reply_markup": refresh_keyboard(),
    })
    msg_id = result.get("message_id") if isinstance(result, dict) else None
    if type(msg_id) is not int or msg_id < 1:
        raise WidgetUnavailable("Telegram did not return a widget message ID")
    return msg_id


async def update_profile_widget(token, uid, msg_id, state):
    return await telegram_api(token, "editMessageWidgetData", {
        "chat_id": int(uid), "message_id": int(msg_id), "chat_data": state
    })


def _decode_ref(doc):
    if not isinstance(doc, dict):
        return None
    try:
        item = json.loads(doc["fields"]["payload"]["stringValue"])
        if (type(item.get("message_id")) is int and item["message_id"] > 0
                and isinstance(item.get("last_data"), str)
                and item.get("kind", "widget") in ("widget", "text")):
            item["kind"] = item.get("kind", "widget")
            return item
    except (KeyError, ValueError, TypeError):
        pass
    return None


async def read_ref(client, uid):
    headers = await _auth(client)
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        doc = await _get_json(session, _registry_url(client, uid), headers, missing_ok=True)
    return _decode_ref(doc)


async def save_ref(client, uid, msg_id, last_data, kind="widget"):
    if kind not in ("widget", "text"):
        raise ValueError("Unsupported card kind")
    headers = await _auth(client)
    body = {"fields": {"payload": {"stringValue": json.dumps({
        "message_id": msg_id, "last_data": last_data, "kind": kind
    }, ensure_ascii=False, separators=(",", ":"))}}}
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        async with session.patch(_registry_url(client, uid), json=body, headers=headers) as response:
            if response.status != 200:
                raise WidgetUnavailable("Widget registry write failed (%s)" % response.status)


async def list_refs(client):
    headers = await _auth(client)
    refs = {}
    token = None
    async with ClientSession(timeout=ClientTimeout(total=12)) as session:
        for _ in range(10):
            url = _base(client) + COLLECTION + "?pageSize=100"
            if token:
                from urllib.parse import quote
                url += "&pageToken=" + quote(token, safe="")
            async with session.get(url, headers=headers) as response:
                if response.status == 404:
                    return {}
                if response.status != 200:
                    raise WidgetUnavailable("Widget registry read failed (%s)" % response.status)
                data = await response.json()
            for doc in data.get("documents", []):
                key = str(doc.get("name") or "").rsplit("/", 1)[-1]
                if key.isdecimal() and int(key) > 0:
                    ref = _decode_ref(doc)
                    if ref:
                        refs[int(key)] = ref
            token = data.get("nextPageToken")
            if not token:
                return refs
    logger.warning("Widget registry has more than 1000 entries, remaining page skipped")
    return refs


async def open_profile_widget(client, bot_token, uid):
    """Prefer HTML Widgets; use a live native card if Telegram disallows them."""
    roster = await read_roster(client)
    snapshot = profile_snapshot(roster, uid)
    if snapshot["status"] == "Доступ не подтверждён":
        raise WidgetUnavailable("Agent registration is not verified")
    state = _state_data(snapshot)
    try:
        ref = await read_ref(client, uid)
    except WidgetUnavailable:
        # The card can still be sent manually if the registry is unavailable.
        ref = None
        logger.warning("Widget registry unreadable for user %s", uid)

    kind = ref.get("kind", "widget") if ref else "widget"
    msg_id = ref["message_id"] if ref else None
    if ref:
        try:
            if kind == "text":
                await edit_fallback_card(bot_token, uid, msg_id, state)
            else:
                await update_profile_widget(bot_token, uid, msg_id, state)
        except WidgetUnavailable as exc:
            if not (kind == "widget" and is_widget_blocked(exc)):
                # Avoid duplicate cards on transient network errors.
                raise
            # Existing HTML widget can no longer be edited. Start one text card.
            msg_id = await send_fallback_card(bot_token, uid, state)
            kind = "text"
    else:
        try:
            msg_id = await send_profile_widget(bot_token, uid, state)
        except WidgetUnavailable as exc:
            if not is_widget_blocked(exc):
                raise
            logger.info("Telegram HTML Widgets unavailable; using native private card")
            msg_id = await send_fallback_card(bot_token, uid, state)
            kind = "text"

    try:
        await save_ref(client, uid, msg_id, state, kind=kind)
    except WidgetUnavailable:
        logger.warning("Widget registry unavailable for user %s", uid)
        return "untracked"

    if not ref:
        return "fallback" if kind == "text" else "created"
    return "updated"


async def refresh_profile_widget(client, bot_token, uid, msg_id):
    roster = await read_roster(client)
    state = _state_data(profile_snapshot(roster, uid))
    try:
        ref = await read_ref(client, uid)
    except WidgetUnavailable:
        ref = None
    # Do not modify a widget from a different user/chat or an obsolete message.
    if ref and ref["message_id"] != msg_id:
        raise WidgetUnavailable("This card is outdated. Open /widget again")
    kind = ref.get("kind", "widget") if ref else "widget"
    if kind == "text":
        await edit_fallback_card(bot_token, uid, msg_id, state)
    else:
        await update_profile_widget(bot_token, uid, msg_id, state)
    if ref:
        try:
            await save_ref(client, uid, msg_id, state, kind=kind)
        except WidgetUnavailable:
            logger.warning("Widget registry unavailable for user %s", uid)


async def widget_refresh_loop(client, bot_token):
    """Low-frequency, change-only edits of existing private widget messages."""
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        try:
            refs = await list_refs(client)
            if not refs:
                continue
            roster = await read_roster(client)
            for uid, ref in refs.items():
                try:
                    state = _state_data(profile_snapshot(roster, uid))
                    previous = json.loads(ref["last_data"])
                    current = json.loads(state)
                    previous.pop("updated", None)
                    current.pop("updated", None)
                    if _stable_data(previous) == _stable_data(current):
                        continue
                    kind = ref.get("kind", "widget")
                    if kind == "text":
                        await edit_fallback_card(bot_token, uid, ref["message_id"], state)
                    else:
                        await update_profile_widget(bot_token, uid, ref["message_id"], state)
                    await save_ref(client, uid, ref["message_id"], state, kind=kind)
                except (WidgetUnavailable, ValueError, TypeError) as exc:
                    logger.warning("Could not update profile widget for %s: %s",
                                   uid, type(exc).__name__)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Profile widget cycle failed: %s", type(exc).__name__)
