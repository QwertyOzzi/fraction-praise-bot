"""Firebase-only audit collections, independent from leadership audit_log.

One immutable document per action (ID includes operation/phase), so retries do
not create duplicate entries. Fully separate from existing administrative logs.
"""
import re
from datetime import datetime, timezone

from agent_tools import audit_fields


async def save_agent_audit(client, session_factory, event_id: str, *,
                           actor_id: int, actor_nick: str, action: str,
                           sheet: str, target_nick: str, quantity: int,
                           reviewer_id: int = 0, reason: str = "") -> bool:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", event_id):
        raise ValueError("Invalid agent audit ID")
    token = await client.get_token()
    if not token:
        return False
    url = (f"https://firestore.googleapis.com/v1/projects/{client.project_id}"
           f"/databases/(default)/documents/agent_action_log/{event_id}")
    fields = audit_fields(actor_id, actor_nick, action, sheet, target_nick,
                          quantity, event_id, reviewer_id=reviewer_id, reason=reason)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with session_factory() as session:
        async with session.patch(url, headers=headers,
                                 json={"fields": fields}) as response:
            return response.status == 200


async def get_agent_audit(client, session_factory, *, count: int = 100) -> list[dict]:
    token = await client.get_token()
    if not token:
        raise RuntimeError("Firebase недоступен")
    url = (f"https://firestore.googleapis.com/v1/projects/{client.project_id}"
           "/databases/(default)/documents/agent_action_log")
    headers = {"Authorization": f"Bearer {token}"}
    async with session_factory() as session:
        async with session.get(
            url, headers=headers, params={"pageSize": min(count, 100)}
        ) as response:
            if response.status == 404:
                return []
            if response.status != 200:
                raise RuntimeError("История действий сейчас недоступна")
            payload = await response.json()
    docs = payload.get("documents", [])
    if not isinstance(docs, list):
        return []
    return sorted(
        [doc for doc in docs if isinstance(doc, dict)],
        key=lambda d: d.get("fields", {}).get("createdAt", {}).get("timestampValue", ""),
        reverse=True
    )


async def save_broadcast_log(client, session_factory, event_id: str,
                             *, author_id: int, category: str,
                             total: int, delivered: int, failed: int) -> bool:
    if not re.fullmatch(r"news-[a-f0-9]{20}", event_id):
        raise ValueError("Invalid news id")
    token = await client.get_token()
    if not token:
        return False
    url = (f"https://firestore.googleapis.com/v1/projects/{client.project_id}"
           f"/databases/(default)/documents/news_dispatch_log/{event_id}")
    fields = {
        "source": {"stringValue": "telegram_news"},
        "authorTelegramId": {"integerValue": str(author_id)},
        "category": {"stringValue": category},
        "recipientCount": {"integerValue": str(total)},
        "delivered": {"integerValue": str(delivered)},
        "failed": {"integerValue": str(failed)},
        "createdAt": {"timestampValue": datetime.now(timezone.utc).isoformat()},
    }
    async with session_factory() as session:
        async with session.patch(
            url, headers={"Authorization": f"Bearer {token}",
                          "Content-Type": "application/json"},
            json={"fields": fields}
        ) as response:
            return response.status == 200
