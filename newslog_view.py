"""Safe, read-only Telegram views for historical FBI announcements.

All formatting here is plain text; Telegram HTML must not interpret saved
news content or user names. Existing Firestore records are never modified.
"""
from datetime import datetime, timezone
import re

NEWS_ID_PATTERN = re.compile(r"news-[a-f0-9]{16,32}\Z")
NEWSLOG_PAGE_SIZE = 8
NEWSLOG_FETCH_LIMIT = 100

STATUS_LABELS = {
    "delivered": "✅ Доставлено",
    "partial": "⚠️ Доставлено частично",
    "sending": "⏳ Отправка / результат ещё не подтверждён",
}


def valid_news_id(value):
    return isinstance(value, str) and bool(NEWS_ID_PATTERN.fullmatch(value))


def eligible_news(records):
    """Only show properly identified announcement documents."""
    return [r for r in records if isinstance(r, dict) and valid_news_id(r.get("id"))]


def newslog_list(records, page=0):
    """Return (text, current page entries, page, total pages)."""
    items = eligible_news(records)
    pages = max(1, (len(items) + NEWSLOG_PAGE_SIZE - 1) // NEWSLOG_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    selected = items[page * NEWSLOG_PAGE_SIZE:(page + 1) * NEWSLOG_PAGE_SIZE]
    lines = [
        "📰 FBI · История рассылок",
        "",
        f"Всего: {len(items)} · Страница {page + 1}/{pages}",
        "Выберите новость, чтобы посмотреть полный текст и результат отправки.",
        "",
    ]
    if not items:
        lines.append("Новостей пока не публиковали.")
    else:
        for offset, item in enumerate(selected, page * NEWSLOG_PAGE_SIZE + 1):
            title = str(item.get("title") or "Новость").replace("\n", " ")[:60]
            state = {"delivered": "✅", "partial": "⚠️", "sending": "⏳"}.get(
                str(item.get("status") or ""), "❔"
            )
            lines.append(f"{offset}. {state} {title} (#{item['id'][-6:]})")
    return "\n".join(lines), selected, page, pages


def news_date(value):
    if not isinstance(value, str) or not value:
        return "Не указана"
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if point.tzinfo is None:
            return "Не указана"
        return point.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return "Не указана"


def _count(entry, field):
    value = entry.get(field)
    try:
        number = int(value)
        return max(0, number)
    except (ValueError, TypeError, OverflowError):
        return 0


def newslog_detail(entry, profiles=None, *, include_text=True):
    if not isinstance(entry, dict) or not valid_news_id(entry.get("id")):
        raise ValueError("Invalid news ID")
    profiles = profiles if isinstance(profiles, dict) else {}
    raw_actor = entry.get("actor_id")
    actor = str(raw_actor) if type(raw_actor) is int and raw_actor > 0 else ""
    current = profiles.get(actor, {})
    current_nick = str(current.get("nick") or "") if isinstance(current, dict) else ""
    actor_text = f"Telegram ID {actor}" if actor else "Не указан"
    if current_nick:
        actor_text += f" (текущий ник: {current_nick[:40]})"
    status = str(entry.get("status") or "")
    sent, failed, total = (_count(entry, key) for key in ("sent", "failed", "recipients"))
    lines = [
        "📰 FBI · Карточка рассылки",
        "",
        f"Заголовок: {str(entry.get('title') or 'Новость')[:90]}",
        f"ID: {entry['id']}",
        f"Опубликовано: {news_date(entry.get('created_at'))}",
        f"Автор: {actor_text}",
        f"Статус: {STATUS_LABELS.get(status, '❔ Неизвестен')}",
        f"Адресатов: {total}",
        f"✅ Доставлено: {sent}",
        f"⚠️ Ошибок доставки: {failed}",
    ]
    if entry.get("finished_at"):
        lines.append(f"Завершено: {news_date(entry.get('finished_at'))}")
    if include_text:
        lines.extend(("", "📝 Текст новости:", "", str(entry.get("message") or "(нет сохранённого текста)")))
    return "\n".join(lines)


def text_size_utf16(text):
    return len(text.encode("utf-16-le")) // 2


def split_telegram_text(text, max_units=3400):
    """Split very emoji-heavy full news text without dropping characters."""
    chunks, current, size = [], [], 0
    for ch in text:
        units = 2 if ord(ch) > 0xFFFF else 1
        if size + units > max_units and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(ch)
        size += units
    if current:
        chunks.append("".join(current))
    return chunks
