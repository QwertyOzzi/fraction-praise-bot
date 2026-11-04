"""All-or-nothing natural-command balance changes in ONE Firestore sheet.

The model is never called here. Prepared targets, quantities, and admin identity
come from the bot's authenticated confirmation flow. Atomic Firestore updateTime
preconditions and persisted transaction IDs protect against duplicate callbacks.
"""
import asyncio
from datetime import datetime, timezone

class BalanceConflict(ValueError):
    """Inputs were changed after preview, or a balance would be negative."""


async def apply_batch(client, sheet: str, targets: list[dict], action: str,
                      amount: int, transaction_id: str, session_factory) -> str:
    """Return 'applied' or 'already_applied', otherwise raise.

    No partial updates are possible: all players share one Firestore document
    and are updated in a single conditional PATCH.
    """
    if sheet not in ("fractionrp", "praise"):
        raise ValueError("Unknown sheet")
    if action not in ("credit", "debit") or type(amount) is not int or not 1 <= amount <= 20:
        raise ValueError("Invalid operation")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 10:
        raise ValueError("Invalid target list")
    if (not isinstance(transaction_id, str) or len(transaction_id) > 70
            or not transaction_id.startswith("ai-")):
        raise ValueError("Invalid operation id")
    if len({str(t["nick"]).casefold() for t in targets}) != len(targets):
        raise ValueError("Duplicate agents")

    token = await client.get_token()
    if not token:
        raise RuntimeError("Firebase authentication unavailable")
    url = (f"https://firestore.googleapis.com/v1/projects/{client.project_id}"
           f"/databases/(default)/documents/sheets/{sheet}")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for attempt in range(5):
        async with session_factory() as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError(f"Cannot read Firebase (HTTP {response.status})")
                data = await response.json()
            revision = data.get("updateTime")
            if not revision:
                raise RuntimeError("Missing Firestore revision")
            applied_field = data.get("fields", {}).get("appliedNaturalIds", {}).get(
                "arrayValue", {}).get("values", [])
            previously_applied = [
                v["stringValue"] for v in applied_field
                if isinstance(v, dict) and isinstance(v.get("stringValue"), str)
            ]
            # Duplicate callbacks / retrying a completed request must not
            # apply the same command twice, including after a process restart.
            if transaction_id in previously_applied:
                return "already_applied"
            rows = client._parse_firestore_rows(
                data.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", [])
            )
            for prepared in targets:
                matches = [r for r in rows if
                           str(r.get("nick") or "").casefold() ==
                           str(prepared["nick"]).casefold()]
                if len(matches) != 1:
                    raise BalanceConflict(f"{prepared['nick']}: отсутствует или дублируется")
                row = matches[0]
                # Re-read the source of truth after the human confirmed.
                for key in ("id", "profileId", "qty"):
                    value = int(row.get(key) or 0) if key == "qty" else str(row.get(key) or "")
                    expected = (int(prepared.get(key) or 0) if key == "qty"
                                else str(prepared.get(key) or ""))
                    if value != expected:
                        raise BalanceConflict(
                            f"{prepared['nick']}: баланс или привязка изменились; проверьте таблицу"
                        )
                new_qty = int(row.get("qty") or 0) + (
                    amount if action == "credit" else -amount
                )
                if new_qty < 0:
                    raise BalanceConflict(
                        f"{prepared['nick']}: недостаточно баллов для списания {amount}"
                    )
                row["qty"] = new_qty
                if action == "credit":
                    row["status"] = "На выдаче"
                elif new_qty == 0:
                    row["status"] = "Выдано"
            # Retain other metadata (including appliedReportIds) with updateMask.
            # Keep enough recent IDs to cover all pending confirmation tokens.
            recent = [*previously_applied[-1499:], transaction_id]
            payload = {"fields": {
                "rows": {"arrayValue": {"values": [
                    client._row_to_firestore(row) for row in rows
                ]}},
                "updatedAt": {"timestampValue": datetime.now(timezone.utc).isoformat()},
                "appliedNaturalIds": {"arrayValue": {"values": [
                    {"stringValue": item} for item in recent
                ]}}
            }}
            params = [
                ("currentDocument.updateTime", revision),
                ("updateMask.fieldPaths", "rows"),
                ("updateMask.fieldPaths", "updatedAt"),
                ("updateMask.fieldPaths", "appliedNaturalIds")
            ]
            async with session.patch(url, headers=headers, params=params, json=payload) as response:
                if response.status == 200:
                    return "applied"
                if response.status not in (409, 412):
                    # This may be an ambiguous transport failure. The same
                    # transaction id remains recorded if the write succeeded.
                    raise RuntimeError(f"Firebase did not confirm write (HTTP {response.status})")
        await asyncio.sleep(min(0.8, 0.12 * (attempt + 1)))
    raise RuntimeError("Concurrent Firebase changes; check the table before retrying")
