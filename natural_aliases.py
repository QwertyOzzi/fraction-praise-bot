"""Confirmed per-agent nickname associations, persisted in Firestore sheet metadata.

Aliases are learned ONLY inside the successful atomic balance write. Never use
a previous association unless it still identifies exactly one current sheet row.
Never store Telegram IDs or send the memory to Gemini.
"""
import asyncio
import json
from datetime import datetime, timezone

from natural_commands import resolve_names, simplified

MEMORY_FIELD = "confirmedNaturalAliases"
MAX_ENTRIES = 500
MAX_RECORD_SIZE = 100_000


class AliasMemoryError(ValueError):
    pass


def _clean_record(record, *, sheet):
    if not isinstance(record, dict) or record.get("blocked"):
        return {"blocked": True} if isinstance(record, dict) and record.get("blocked") is True else None
    nick = record.get("nick")
    binding = record.get("profileId")
    row_id = record.get("id")
    origin = record.get("sheet")
    timestamp = record.get("at", "")
    if (not isinstance(nick, str) or not 2 <= len(nick) <= 40
            or not isinstance(binding, str) or len(binding) > 100
            or not isinstance(row_id, str) or len(row_id) > 100
            or origin not in ("fractionrp", "praise")
            or not isinstance(timestamp, str) or len(timestamp) > 45
            or (not binding and (origin != sheet or not row_id))):
        return None
    return {"nick": nick, "profileId": binding, "id": row_id,
            "sheet": origin, "at": timestamp}


def decode_alias_book(raw, *, sheet):
    """Malformed memory is ignored, never interpreted as a command or permission."""
    if not isinstance(raw, str) or len(raw) > MAX_RECORD_SIZE:
        return {}
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != 1:
        return {}
    aliases = doc.get("aliases")
    if not isinstance(aliases, dict):
        return {}
    result = {}
    for key, entry in list(aliases.items())[:MAX_ENTRIES]:
        if (not isinstance(key, str) or not 4 <= len(key) <= 50
                or simplified(key) != key):
            continue
        record = _clean_record(entry, sheet=sheet)
        if record is not None:
            result[key] = record
    return result


def encode_alias_book(book):
    if len(book) > MAX_ENTRIES:
        # Oldest entries are evicted first. Never let an unbounded dictionary
        # increase the Firestore document beyond its practical limit.
        sorted_keys = sorted(book, key=lambda k: book[k].get("at", ""))
        book = {k: book[k] for k in sorted_keys[-MAX_ENTRIES:]}
    return json.dumps({"version": 1, "aliases": book},
                      ensure_ascii=False, separators=(",", ":"))


def same_identity(old, new):
    if old.get("blocked") or new.get("blocked"):
        return False
    if old.get("nick", "").casefold() != new.get("nick", "").casefold():
        return False
    first, second = old.get("profileId"), new.get("profileId")
    if first or second:
        return bool(first and second and first == second)
    return (old.get("sheet") == new.get("sheet")
            and old.get("id") and old["id"] == new.get("id"))


def merge_alias_books(*books):
    """Combine independently learned FRP/Praise aliases; conflicts are blocked."""
    result = {}
    for book in books:
        for key, record in book.items():
            if key in result and not same_identity(result[key], record):
                result[key] = {"blocked": True}
            else:
                result[key] = record
    return result


def _verified_record(target, sheet, now):
    nick, binding, row_id = (str(target.get("nick") or ""),
                             str(target.get("profileId") or ""),
                             str(target.get("id") or ""))
    if not nick or (not binding and not row_id):
        return None
    return {"nick": nick, "profileId": binding, "id": row_id,
            "sheet": sheet, "at": now}


def learn_confirmed_aliases(existing, aliases, targets, sheet, *, now=None):
    """Pure merge of aliases after an approved operation; no unverified hints."""
    if sheet not in ("fractionrp", "praise") or len(aliases) != len(targets):
        raise ValueError("Alias confirmation does not match agent targets")
    stamp = now or datetime.now(timezone.utc).isoformat()
    output = dict(existing)
    for alias, target in zip(aliases, targets):
        if not isinstance(alias, str):
            continue
        key = simplified(alias.strip())
        if not 4 <= len(key) <= 50 or len(alias) > 60:
            continue
        record = _verified_record(target, sheet, stamp)
        if record is None or key == simplified(record["nick"]):
            continue
        previous = output.get(key)
        if previous and not same_identity(previous, record):
            output[key] = {"blocked": True}
        elif previous and previous.get("blocked"):
            output[key] = {"blocked": True}
        else:
            output[key] = record
    return decode_alias_book(encode_alias_book(output), sheet=sheet)


def _exact_rows(alias, rows):
    key = simplified(alias)
    return [r for r in rows if str(r.get("nick") or "")
            and simplified(str(r["nick"])) == key]


def _saved_identity(alias, record, rows, sheet):
    if record.get("blocked"):
        return None
    matching = [
        r for r in rows
        if str(r.get("nick") or "").casefold() == record.get("nick", "").casefold()
        and ((record.get("profileId")
              and str(r.get("profileId") or "") == record["profileId"])
             or (not record.get("profileId") and record.get("sheet") == sheet
                 and record.get("id")
                 and str(r.get("id") or "") == record["id"]))
    ]
    if len(matching) != 1:
        return None
    return matching[0]


def _target_from_row(row):
    return {"nick": str(row["nick"]), "qty": int(row.get("qty") or 0),
            "profileId": str(row.get("profileId") or ""),
            "id": str(row.get("id") or ""),
            "rank": str(row.get("rank") or "")}


def resolve_with_memory(aliases, rows, memory, sheet):
    """Exact nick > confirmed association > safe fuzzy search.

    Stale or conflicting learned associations fail CLOSED, never fall through to
    fuzzy matching of a recycled nickname. The entire batch must resolve.
    """
    results, errors, seen = [], [], set()
    for alias in aliases:
        key = simplified(alias)
        exact = _exact_rows(alias, rows)
        record = memory.get(key)
        if len(exact) > 1:
            errors.append(f"{alias}: дубли игрового ника")
            continue
        if record and record.get("blocked"):
            if len(exact) != 1:
                errors.append(f"{alias}: сокращение неоднозначно; укажите полный ник")
                continue
        selected = exact[0] if len(exact) == 1 else None
        if record and not record.get("blocked"):
            remembered = _saved_identity(alias, record, rows, sheet)
            if remembered is None and selected is None:
                errors.append(f"{alias}: прежняя ассоциация устарела; укажите полный ник")
                continue
            if selected is None:
                # A learned short name must not shadow another agent whose
                # actual surname is exactly the same alias.
                token_matches = [
                    r for r in rows if str(r.get("nick") or "")
                    and any(simplified(token) == key
                            for token in str(r["nick"]).split("_"))
                ]
                if any(r is not remembered for r in token_matches):
                    errors.append(f"{alias}: совпадение с другим агентом; нужен полный ник")
                    continue
                selected = remembered
        if selected is None:
            matches, err = resolve_names([alias], rows)
            if err:
                errors.extend(err)
                continue
            # resolve_names already checks duplicate entries.
            selected = matches[0]
        nick_key = str(selected["nick"]).casefold()
        if nick_key in seen:
            errors.append(f"{alias}: один агент указан повторно")
        else:
            seen.add(nick_key)
            results.append(_target_from_row(selected))
    return (results if not errors else []), errors


async def load_alias_memory(client, session_factory):
    """Read both sheet memories: aliases learned for FRP also work for praise."""
    token = await client.get_token()
    if not token:
        raise AliasMemoryError("Firebase недоступен")
    base = (f"https://firestore.googleapis.com/v1/projects/{client.project_id}"
            "/databases/(default)/documents/sheets/")
    headers = {"Authorization": f"Bearer {token}"}
    async with session_factory() as session:
        async def fetch(sheet):
            async with session.get(base + sheet, headers=headers) as response:
                if response.status != 200:
                    raise AliasMemoryError("Не удалось проверить память агентов в Firebase")
                content = await response.json()
            raw = content.get("fields", {}).get(MEMORY_FIELD, {}).get("stringValue", "")
            return decode_alias_book(raw, sheet=sheet)
        books = await asyncio.gather(fetch("fractionrp"), fetch("praise"))
    return merge_alias_books(*books)
