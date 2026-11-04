"""Telegram 10.3 Rich Message presentation, isolated from business logic.

All profile, report, administrator and Firebase operations stay in bot.py.
"""
import re
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputRichMessage

log = logging.getLogger(__name__)
LEA_URL = "https://lea-script.space/"
LEA_LABEL = "🧩 ЛЕА ↗"
TITLE_RE = re.compile(r"^([^\n<]*)<b>([^<\n]+)</b>\n(?:━+\n)?\n*")


def rich_card_html(text: str, *, lea: bool = False) -> str:
    """Build real Telegram headings and a small inline LEA button.

    Input text is already Telegram-escaped HTML, not raw untrusted user data.
    Keep the approved inline markup intact and only replace layout boundaries.
    """
    match = TITLE_RE.match(text)
    if match:
        heading = match.group(1) + match.group(2)
        body = text[match.end():].strip()
        result = [f"<h2>{heading}</h2>"]
    else:
        body = text.strip()
        result = []
    for paragraph in body.split("\n\n"):
        if paragraph.strip():
            result.append("<p>" + paragraph.strip().replace("\n", "<br/>") + "</p>")
    if lea:
        # Inline Rich Message button, not a large InlineKeyboard row or text ad.
        result.append(
            f'<p><tg-button type="url" url="{LEA_URL}">{LEA_LABEL}</tg-button></p>'
        )
    return "\n".join(result)


def plain_card_html(text: str, *, lea: bool = False) -> str:
    """Readable fallback if Telegram rejects Rich Messages."""
    clean = text.replace("\n━━━━━━━━━━━━━━\n\n", "\n\n")
    clean = clean.replace("\n━━━━━━━━━━━━━━\n", "\n")
    if lea:
        clean += f'\n\n<i>↗ <a href="{LEA_URL}">ЛЕА · помощник ФБР</a></i>'
    return clean


async def send_fbi_card(message, text: str, *, reply_markup=None, lea: bool = False):
    """Send a card with actual typography; fall back only on API validation errors.

    Never retry on network timeouts (the first send may have succeeded).
    """
    try:
        return await message.answer_rich(
            InputRichMessage(html=rich_card_html(text, lea=lea)),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        log.warning("Telegram rejected rich card; using HTML fallback: %s", type(exc).__name__)
        return await message.answer(
            plain_card_html(text, lea=lea),
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def edit_fbi_card(message, text: str, *, reply_markup=None):
    """Edit a Rich Message and preserve its existing callback keyboard."""
    try:
        return await message.edit_text(
            rich_message=InputRichMessage(html=rich_card_html(text)),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            raise
        log.warning("Telegram rejected rich edit; using HTML fallback: %s", type(exc).__name__)
        return await message.edit_text(
            plain_card_html(text),
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
