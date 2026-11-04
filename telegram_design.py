"""Stable, native Telegram HTML layout for the FBI assistant.

No experimental Rich Message API. The only promotional placement is an
optional, small inline URL button on the home and status cards.
"""
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

LEA_URL = "https://lea-script.space/"
LEA_LABEL = "🧩 ЛЕА ↗"
TITLE_RE = re.compile(r"^([^\n<]*)<b>([^<\n]+)</b>\n+")


def card_html(text: str) -> str:
    """Present escaped HTML in a compact Telegram-friendly card."""
    text = text.replace("\n━━━━━━━━━━━━━━\n\n", "\n\n")
    text = text.replace("\n━━━━━━━━━━━━━━\n", "\n").strip()
    match = TITLE_RE.match(text)
    if not match:
        return text
    heading = match.group(1) + "<b>" + match.group(2) + "</b>"
    body = text[match.end():].strip()
    # Profile details are visually separated using Telegram's supported quote.
    if body.startswith("👤"):
        profile, split, remainder = body.partition("\n\n")
        if profile:
            body = "<blockquote>" + profile + "</blockquote>"
            if split and remainder:
                body += "\n\n" + remainder
    return heading + ("\n\n" + body if body else "")


def add_lea_button(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    """Append an optional LEA footer without touching existing action buttons."""
    rows = [list(row) for row in markup.inline_keyboard] if markup else []
    rows.append([InlineKeyboardButton(text=LEA_LABEL, url=LEA_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_fbi_card(message, text: str, *, reply_markup=None, lea: bool = False):
    """One ordinary HTML message: reliably formatted, with inline controls."""
    if lea:
        if reply_markup is not None and not isinstance(reply_markup, InlineKeyboardMarkup):
            raise ValueError("LEA chip requires an inline keyboard")
        reply_markup = add_lea_button(reply_markup)
    return await message.answer(
        card_html(text),
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def edit_fbi_card(message, text: str, *, reply_markup=None, lea: bool = False):
    if lea:
        if reply_markup is not None and not isinstance(reply_markup, InlineKeyboardMarkup):
            raise ValueError("LEA chip requires an inline keyboard")
        reply_markup = add_lea_button(reply_markup)
    return await message.edit_text(
        card_html(text),
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def hide_old_reply_keyboard(message) -> None:
    """Remove the old full-screen keyboard, without leaving a chat message."""
    if getattr(message.chat, "type", None) != "private":
        return
    try:
        notification = await message.answer(
            "⌨️ Открыто новое компактное меню.",
            reply_markup=ReplyKeyboardRemove(),
        )
        try:
            await notification.delete()
        except Exception:
            pass
    except Exception:
        # An unsupported keyboard removal must not block authentication.
        pass
