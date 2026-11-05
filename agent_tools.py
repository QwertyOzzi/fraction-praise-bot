"""Agent-only point commands, audit and review helpers.

Never consult Telegram usernames as authorization. A verified Telegram sender
is required in bot.py before any of these helpers are used.
"""
import difflib
import html
import re
from datetime import datetime, timezone

from natural_commands import simplified

KINDS = {
    "frp": "fractionrp", "fractionrp": "fractionrp", "фрп": "fractionrp",
    "praise": "praise", "похвалы": "praise", "похвала": "praise",
}
MAX_AGENT_AMOUNT = 20


def parse_min_args(raw: str) -> dict:
    """Accept '/min frp Nick -2' and '/min praise Nick +2'."""
    m = re.fullmatch(
        r"\s*(frp|fractionrp|praise|фрп|похвала|похвалы)\s+"
        r"(.{2,45}?)\s+([+\-−–])\s*(\d{1,2})\s*", raw, re.I
    )
    if not m:
        raise ValueError("Формат: /min frp Nick_Name -2 или /min praise Nick_Name +2")
    nick = m[2].strip()
    if not re.fullmatch(r"[\w\s.\-]{2,40}", nick):
        raise ValueError("Укажите один игровой ник (без запятых).")
    amount = int(m[4])
    if not 1 <= amount <= MAX_AGENT_AMOUNT:
        raise ValueError("Количество должно быть от 1 до 20.")
    return {
        "action": "debit" if m[3] in "-−–" else "credit",
        "sheet": KINDS[m[1].casefold()], "amount": amount,
        "names": [nick]
    }


def active_profile(profile) -> bool:
    if not isinstance(profile, dict) or not profile.get("nick"):
        return False
    return profile.get("verification_status") not in (
        "pending", "needs_proof", "proof_submitted",
        "disabled", "disconnected", "cooldown", "retry_ready"
    )


def _similarity(query, nick):
    word = simplified(query)
    if len(word) < 3:
        return 0
    tokens = [simplified(token) for token in re.split(r"[_\s]+", nick)]
    candidate = max([difflib.SequenceMatcher(None, word, t).ratio()
                     for t in tokens if t] or [0])
    if any(t.startswith(word) for t in tokens):
        candidate = max(candidate, .9)
    if any(word == t for t in tokens):
        candidate = 1.0
    return candidate


def suggest_nicks(alias: str, rows: list[dict], *, limit: int = 4) -> list[dict]:
    """Uncertain aliases require a HUMAN to select their exact target."""
    candidates = []
    visited = set()
    for row in rows:
        nick = str(row.get("nick") or "")
        key = nick.casefold()
        if not nick or key in visited:
            continue
        visited.add(key)
        score = _similarity(alias, nick)
        if score >= .54:
            candidates.append((score, nick, row))
    candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [item[2] for item in candidates[:limit]]


def describe_command(plan: dict, target: dict) -> str:
    name = html.escape(str(target["nick"]))
    category = "FRP" if plan["sheet"] == "fractionrp" else "похвал"
    sign = "−" if plan["action"] == "debit" else "+"
    amount = int(plan["amount"])
    return (
        f"👤 <b>Агент:</b> <code>{name}</code>\n"
        f"📌 <b>Изменение:</b> {sign}{amount} {category}\n"
        f"📊 <b>Сейчас:</b> {int(target.get('qty') or 0)}"
    )


def audit_fields(actor_id: int, actor_nick: str, action: str,
                 sheet: str, target_nick: str, quantity: int,
                 operation_id: str, *, reviewer_id: int = 0,
                 reason: str = "") -> dict:
    """Separate log schema; NEVER write agent activity to admin audit_log."""
    if action not in ("debit", "requested", "approved", "rejected"):
        raise ValueError("Unknown agent operation")
    return {
        "source": {"stringValue": "agent_points"},
        "actorTelegramId": {"integerValue": str(actor_id)},
        "actorNick": {"stringValue": actor_nick[:70]},
        "action": {"stringValue": action},
        "sheet": {"stringValue": sheet[:30]},
        "targetNick": {"stringValue": target_nick[:70]},
        "quantity": {"integerValue": str(quantity)},
        "operationId": {"stringValue": operation_id[:80]},
        "reviewerTelegramId": {"integerValue": str(reviewer_id)},
        "reason": {"stringValue": reason[:500]},
        "createdAt": {"timestampValue": datetime.now(timezone.utc).isoformat()},
    }


def audit_preview(doc: dict) -> str:
    def field(name):
        value = doc.get("fields", {}).get(name, {})
        return value.get("stringValue") or value.get("integerValue") or ""
    codes = {"debit": "Списание", "requested": "Заявка +",
             "approved": "Одобрено +", "rejected": "Отклонено"}
    kind = "FRP" if field("sheet") == "fractionrp" else "похвал"
    return (
        f"• {html.escape(str(field('actorNick')))} → "
        f"<code>{html.escape(str(field('targetNick')))}</code> · "
        f"{codes.get(field('action'), 'Операция')} "
        f"{html.escape(str(field('quantity')))} {kind}"
    )
