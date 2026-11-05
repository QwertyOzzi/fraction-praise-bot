"""Read-only personnel cards and report-based activity metrics.

Metrics count saved Telegram reports, not game online time. Historical fields
are displayed only when actually present; we never invent a join date.
"""
from datetime import datetime, timedelta, timezone
from html import escape


def as_utc(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def report_summary(requests, uid, days=7, *, now=None):
    """Count only reports belonging to an exact Telegram identity."""
    now = as_utc(now) or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    result = {"total": 0, "approved": 0, "rejected": 0, "pending": 0,
              "earned_frp": 0, "earned_praise": 0, "last_report": None}
    for req in requests.values():
        if not isinstance(req, dict) or str(req.get("user_id")) != str(uid):
            continue
        when = as_utc(req.get("created_at"))
        if when is None or not since <= when <= now:
            continue
        status = str(req.get("status") or "")
        result["total"] += 1
        if result["last_report"] is None or when > result["last_report"]:
            result["last_report"] = when
        if status in ("approved", "rejected", "pending"):
            result[status] += 1
        if status == "approved":
            try:
                amount = int(req.get("quantity", 1))
            except (TypeError, ValueError):
                amount = 0
            if 1 <= amount <= 20:
                sheet = req.get("rep_type")
                if sheet == "fractionrp":
                    result["earned_frp"] += amount
                elif sheet == "praise":
                    result["earned_praise"] += amount
    return result


def personnel_details(profile, requests, uid, *, now=None):
    """HTML-safe, privacy-scoped details for the owner or approved staff."""
    if not isinstance(profile, dict):
        profile = {}
    department = escape(str(profile.get("department") or "Не назначен")[:70])
    joined = as_utc(profile.get("joined_at") or profile.get("registered_at"))
    joined_text = joined.strftime("%d.%m.%Y") if joined else "Нет данных"
    history = profile.get("rank_history", [])
    if not isinstance(history, list):
        history = []
    recent = []
    for event in history[-5:]:
        if not isinstance(event, dict):
            continue
        moment = as_utc(event.get("at"))
        date = moment.strftime("%d.%m.%Y") if moment else "без даты"
        previous = escape(str(event.get("from") or "?")[:16])
        current = escape(str(event.get("to") or "?")[:16])
        recent.append(f"  • {date}: {previous} → {current}")
    week = report_summary(requests, uid, 7, now=now)
    month = report_summary(requests, uid, 30, now=now)
    return (
        f"🏢 Отдел: <b>{department}</b>\n"
        f"📅 Дата регистрации: <b>{joined_text}</b>\n"
        f"📝 Отчёты за 7 дней: <b>{week['total']}</b> "
        f"(✅ {week['approved']} · ⏳ {week['pending']} · ❌ {week['rejected']})\n"
        f"📈 За 30 дней: <b>{month['total']}</b>, одобрено <b>{month['approved']}</b>\n"
        f"🎖️ Начислено по отчётам за 30 дней: "
        f"FRP <b>{month['earned_frp']}</b> · похвал <b>{month['earned_praise']}</b>\n"
        "🎗️ История рангов:\n"
        + ("\n".join(recent) if recent else "  Пока нет сохранённых изменений")
        + "\n\n<i>Активность означает только отчёты в боте, не время в игре.</i>"
    )


def ranked_reporters(requests, profiles, days=7, *, now=None, limit=10):
    """Rank verified profile records by approved reports in the period."""
    rows = []
    for uid, profile in profiles.items():
        if not isinstance(profile, dict) or not profile.get("nick"):
            continue
        if profile.get("verification_status") in (
            "pending", "needs_proof", "proof_submitted", "disabled",
            "disconnected", "retry_ready", "cooldown"
        ):
            continue
        stats = report_summary(requests, uid, days, now=now)
        rows.append({"uid": str(uid), "nick": str(profile["nick"]),
                     "rank": str(profile.get("rank") or "?"), **stats})
    return sorted(rows, key=lambda r: (-r["approved"], -r["total"], r["nick"].casefold()))[:limit]
