import asyncio
import os
import json
import html
import logging
import uuid
import hashlib
import hmac
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from aiohttp import web, ClientSession, ClientTimeout
from urllib.parse import urlsplit
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RELEASE_TAG = "durable-admin-recovery-v6"

# Ключи доступа:
# 1. Личный мастер-ключ владельца (мгновенный вход без ожидания)
OWNER_TELEGRAM_IDS = {6060562962}
# 2. Ключ для лидеров и заместителей (требует подтверждения владельца)
DEPUTY_KEY = os.getenv("DEPUTY_KEY", "").strip()
ADMIN_KEY = ""  # legacy compatibility; owner access is Telegram ID-based

# Информация о Лидере (назначается Тех. Администратором через сайт)
LEADER_INFO: Dict[str, str] = {
    "nick": "Samuel_Norton",
    "rank": "10 (Лидер)",
    "contact": "",
    "tech_nick": "Gopon (Тех. Администратор)"
}

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", "8080"))

# Storage files for persistence
USERS_FILE = "users_db.json"
ADMINS_FILE = "admins_db.json"
REQUESTS_FILE = "requests_db.json"
OWNER_FILE = "owner_config.json"

def load_json(filepath: str, default: Any) -> Any:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default

def save_json(filepath: str, data: Any):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving {filepath}: {e}")

# Firebase credentials with reliable defaults & file persistence
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "fraction-praise").strip()
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY", "").strip()
FIREBASE_EMAIL = os.getenv("FIREBASE_EMAIL", "goponos12@gmail.com").strip()

FIREBASE_PASSWORD = os.getenv("FIREBASE_PASSWORD", "").strip()

WEBAPP_URL = "https://qwertyozzi.github.io/fraction-praise-tracker/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

users_db: Dict[str, Any] = load_json(USERS_FILE, {})
admins_db: List[int] = load_json(ADMINS_FILE, [])
pending_admin_requests: Dict[int, Dict[str, Any]] = {}
admin_roles: Dict[str, str] = load_json("admin_roles.json", {})
requests_db: Dict[str, Any] = load_json(REQUESTS_FILE, {})
owner_cfg: Dict[str, Any] = {"owner_ids": list(OWNER_TELEGRAM_IDS)}
completed_cleanup_ids = set()
_cloud_state_hydrated = False
_explicit_revoked_admins: set[int] = set()
_pending_legacy_admins: Dict[int, str] = {}
_pending_legacy_nonce: str = ""

# ADMIN_CHAT_ID is a notification destination, NEVER an authorization grant.
# A positive chat ID can also be a Telegram user ID; treating it as an admin
# would let an environment configuration accidentally bypass owner approval.

def sign_admin_permissions(admin_ids: List[int], roles: Dict[str, str]) -> str:
    """Sign permission grants, so untrusted Firestore edits cannot grant admin."""
    valid_ids = sorted({n for n in admin_ids if type(n) is int and n > 0
                        and n not in OWNER_TELEGRAM_IDS})
    signed_payload = {
        "admins": valid_ids,
        "roles": {str(n): roles.get(str(n), "deputy") for n in valid_ids}
    }
    secret = (os.getenv("ADMIN_ACL_SIGNING_KEY", "").strip() or BOT_TOKEN).encode("utf-8")
    msg = json.dumps(signed_payload, ensure_ascii=True, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def is_admin(user_id: int) -> bool:
    # Fail closed while Firebase has not confirmed the signed access list.
    return user_id in OWNER_TELEGRAM_IDS or (_cloud_state_hydrated and user_id in admins_db)

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_TELEGRAM_IDS


def is_leader(user_id: int) -> bool:
    return is_owner(user_id) or (is_admin(user_id) and admin_roles.get(str(user_id)) == "leader")


def role_name(user_id: int) -> str:
    if is_owner(user_id): return "Владелец"
    if not is_admin(user_id): return "Агент"
    return "Лидер" if is_leader(user_id) else "Заместитель"

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIREBASE REST CLIENT                                            ║
# ╚══════════════════════════════════════════════════════════════════╝
class FirebaseClient:
    def __init__(self, project_id: str, api_key: str, email: str, password: str):
        self.project_id = project_id
        self.api_key = api_key
        self.email = email
        self.password = password
        self.id_token: Optional[str] = None
        self.token_expiry = 0

    async def get_token(self) -> Optional[str]:
        if not self.api_key or not self.email or not self.password:
            return None
        
        now = datetime.now().timestamp()
        if self.id_token and now < self.token_expiry - 60:
            return self.id_token

        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={self.api_key}"
        payload = {"email": self.email, "password": self.password, "returnSecureToken": True}
        
        try:
            async with ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.id_token = data.get("idToken")
                        expires_in = int(data.get("expiresIn", 3600))
                        self.token_expiry = now + expires_in
                        logger.info("Firebase Auth successful, token acquired")
                        return self.id_token
                    else:
                        err_text = await resp.text()
                        logger.warning(f"Firebase Auth failed ({resp.status}): {err_text}")
                        return None
        except Exception as e:
            logger.error(f"Error getting Firebase token: {e}")
            return None

    async def get_sheet(self, sheet_name: str) -> List[Dict[str, Any]]:
        url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/sheets/{sheet_name}"
        headers = {}
        token = await self.get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fields = data.get("fields", {})
                        rows_field = fields.get("rows", {}).get("arrayValue", {}).get("values", [])
                        return self._parse_firestore_rows(rows_field)
                    else:
                        logger.warning(f"Failed to fetch {sheet_name} from Firestore: {resp.status}")
                        raise RuntimeError(f"Firestore read failed: {resp.status}")
        except Exception as e:
            logger.error(f"Error fetching sheet {sheet_name}: {e}")
            raise

    async def update_sheet(self, sheet_name: str, rows: List[Dict[str, Any]]) -> bool:
        token = await self.get_token()
        url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/sheets/{sheet_name}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        body = {
            "fields": {
                "rows": {
                    "arrayValue": {
                        "values": [self._row_to_firestore(r) for r in rows]
                    }
                },
                "updatedAt": {
                    "timestampValue": datetime.utcnow().isoformat() + "Z"
                }
            }
        }

        try:
            async with ClientSession() as session:
                async with session.patch(url, headers=headers, params=[("updateMask.fieldPaths", "rows"), ("updateMask.fieldPaths", "updatedAt")], json=body) as resp:
                    if resp.status == 200:
                        logger.info(f"Successfully updated {sheet_name} in Firestore")
                        return True
                    else:
                        err_text = await resp.text()
                        logger.error(f"Failed to update {sheet_name} ({resp.status}): {err_text}")
                        return False
        except Exception as e:
            logger.error(f"Error updating sheet {sheet_name}: {e}")
            return False

    async def credit_report(self, sheet_name: str, nick: str, rank: str, reason: str, proof_url: str, report_id: str, quantity: int = 1) -> bool:
        """Optimistic Firestore concurrency control for a single credit."""
        if sheet_name not in ("fractionrp", "praise"):
            raise ValueError("Unknown sheet")
        if not isinstance(quantity, int) or not 1 <= quantity <= 20:
            raise ValueError("Quantity must be between 1 and 20")
        token = await self.get_token()
        if not token:
            raise RuntimeError("Firebase authentication unavailable")
        url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/sheets/{sheet_name}"
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(5):
            async with ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Firestore read failed: {response.status}")
                    data = await response.json()
                    revision = data.get("updateTime")
                    applied_field = data.get("fields", {}).get("appliedReportIds", {}).get("arrayValue", {}).get("values", [])
                    applied_ids = [v["stringValue"] for v in applied_field if isinstance(v, dict) and isinstance(v.get("stringValue"), str)]
                    if report_id in applied_ids:
                        return True
                    if not revision:
                        raise RuntimeError("Firestore version missing")
                    rows = self._parse_firestore_rows(data.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", []))
                existing = next((row for row in rows if row.get("nick", "").casefold() == nick.casefold()), None)
                proof_links = [{"text": reason, "url": proof_url}] if proof_url.startswith(("https://", "http://")) else []
                proof = {"text": reason, "links": proof_links}
                if existing:
                    existing["qty"] = int(existing.get("qty") or 0) + quantity
                    existing["status"] = "На выдаче"
                    existing["proof"] = proof
                else:
                    rows.append({"id": str(int(datetime.now().timestamp() * 1000000)),
                                 "fullName": f"{nick} ({rank})", "nick": nick,
                                 "rank": rank, "status": "На выдаче", "qty": quantity, "proof": proof})
                payload = {"fields": {"rows": {"arrayValue": {"values": [self._row_to_firestore(row) for row in rows]}},
                                      "updatedAt": {"timestampValue": datetime.now(timezone.utc).isoformat()},
                                      "appliedReportIds": {"arrayValue": {"values": [{"stringValue": report} for report in [*applied_ids, report_id]]}}}}
                async with session.patch(url, headers={**headers, "Content-Type": "application/json"},
                                         params={"currentDocument.updateTime": revision}, json=payload) as response:
                    if response.status == 200:
                        return True
                    if response.status not in (409, 412):
                        raise RuntimeError(f"Firestore write failed: {response.status}")
            await asyncio.sleep(0.1 * (attempt + 1))
        raise RuntimeError("Concurrent modifications: retry limit reached")

    async def remove_zero_balance_agent(self, sheet_name: str, nick: str) -> bool:
        """Delete an empty sheet entry using Firestore revision protection.

        Return True if absent or deleted. Do not delete entries with a positive balance.
        """
        if sheet_name not in ("fractionrp", "praise"):
            raise ValueError("Unknown sheet")
        token = await self.get_token()
        if not token:
            raise RuntimeError("Firebase authentication unavailable")
        url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/sheets/{sheet_name}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for attempt in range(5):
            async with ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Firestore read failed: {response.status}")
                    data = await response.json()
                    revision = data.get("updateTime")
                    if not revision:
                        raise RuntimeError("Missing Firestore revision")
                    rows = self._parse_firestore_rows(data.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", []))
                matches = [row for row in rows if str(row.get("nick", "")).casefold() == nick.casefold()]
                if not matches:
                    return True
                if any(int(row.get("qty") or 0) != 0 for row in matches):
                    return False
                new_rows = [row for row in rows if str(row.get("nick", "")).casefold() != nick.casefold()]
                body = {"fields": {
                    "rows": {"arrayValue": {"values": [self._row_to_firestore(row) for row in new_rows]}},
                    "updatedAt": {"timestampValue": datetime.now(timezone.utc).isoformat()}
                }}
                params = [("currentDocument.updateTime", revision),
                          ("updateMask.fieldPaths", "rows"), ("updateMask.fieldPaths", "updatedAt")]
                async with session.patch(url, headers=headers, params=params, json=body) as response:
                    if response.status == 200:
                        return True
                    if response.status not in (409, 412):
                        raise RuntimeError(f"Firestore delete entry failed: {response.status}")
            await asyncio.sleep(0.1 * (attempt + 1))
        raise RuntimeError("Concurrent writes prevented deletion")


    async def debit_one(self, sheet_name: str, nick: str) -> int:
        """Consume one point with an updateTime precondition; return remaining points."""
        if sheet_name not in ("fractionrp", "praise"):
            raise ValueError("Unknown sheet")
        token = await self.get_token()
        if not token:
            raise RuntimeError("Firebase authentication unavailable")
        url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/sheets/{sheet_name}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for attempt in range(5):
            async with ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Firestore read failed: {response.status}")
                    data = await response.json()
                    revision = data.get("updateTime")
                    if not revision:
                        raise RuntimeError("Firestore version missing")
                    rows = self._parse_firestore_rows(data.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", []))
                item = next((row for row in rows if row.get("nick", "").casefold() == nick.casefold()), None)
                if not item:
                    raise ValueError("Player not found")
                remaining = int(item.get("qty") or 0)
                if remaining <= 0:
                    return 0
                remaining -= 1
                item["qty"] = remaining
                if remaining == 0:
                    item["status"] = "Выдано"
                payload = {"fields": {
                    "rows": {"arrayValue": {"values": [self._row_to_firestore(row) for row in rows]}},
                    "updatedAt": {"timestampValue": datetime.now(timezone.utc).isoformat()}
                }}
                # Keep appliedReportIds and any other metadata untouched.
                params = [("currentDocument.updateTime", revision),
                          ("updateMask.fieldPaths", "rows"), ("updateMask.fieldPaths", "updatedAt")]
                async with session.patch(url, headers=headers, params=params, json=payload) as response:
                    if response.status == 200:
                        return remaining
                    if response.status not in (409, 412):
                        raise RuntimeError(f"Firestore write failed: {response.status}")
            await asyncio.sleep(0.1 * (attempt + 1))
        raise RuntimeError("Concurrent changes, try again")

    def _parse_firestore_rows(self, values: list) -> List[Dict[str, Any]]:
        rows = []
        for v in values:
            if not isinstance(v, dict):
                continue
            mv = v.get("mapValue", {}).get("fields", {})

            # Defensive qty extraction
            qty_val = 0
            if "qty" in mv:
                q_field = mv["qty"]
                if "integerValue" in q_field:
                    try:
                        qty_val = int(q_field["integerValue"])
                    except (ValueError, TypeError):
                        qty_val = 0
                elif "doubleValue" in q_field:
                    try:
                        qty_val = int(float(q_field["doubleValue"]))
                    except (ValueError, TypeError):
                        qty_val = 0
                elif "stringValue" in q_field:
                    try:
                        qty_val = int(q_field["stringValue"])
                    except (ValueError, TypeError):
                        qty_val = 0

            # Defensive proof extraction
            proof_data = {"text": "", "links": []}
            if "proof" in mv:
                p_field = mv["proof"]
                if "mapValue" in p_field:
                    proof_mv = p_field.get("mapValue", {}).get("fields", {})
                    proof_data["text"] = proof_mv.get("text", {}).get("stringValue", "")
                    raw_links = proof_mv.get("links", {}).get("arrayValue", {}).get("values", [])
                    for l in raw_links:
                        if isinstance(l, dict):
                            lmv = l.get("mapValue", {}).get("fields", {})
                            proof_data["links"].append({
                                "text": lmv.get("text", {}).get("stringValue", ""),
                                "url": lmv.get("url", {}).get("stringValue", "")
                            })
                elif "stringValue" in p_field:
                    proof_data["text"] = p_field.get("stringValue", "")

            rows.append({
                "id": mv.get("id", {}).get("stringValue", ""),
                "fullName": mv.get("fullName", {}).get("stringValue", ""),
                "nick": mv.get("nick", {}).get("stringValue", ""),
                "rank": mv.get("rank", {}).get("stringValue", ""),
                "status": mv.get("status", {}).get("stringValue", "Выдано"),
                "qty": qty_val,
                "proof": proof_data
            })
        return rows

    def _row_to_firestore(self, r: Dict[str, Any]) -> dict:
        links_values = []
        proof_obj = r.get("proof", {})
        if isinstance(proof_obj, dict):
            for l in proof_obj.get("links", []):
                links_values.append({
                    "mapValue": {
                        "fields": {
                            "text": {"stringValue": str(l.get("text", ""))},
                            "url": {"stringValue": str(l.get("url", ""))}
                        }
                    }
                })
            proof_text = str(proof_obj.get("text", ""))
        else:
            proof_text = str(proof_obj)

        return {
            "mapValue": {
                "fields": {
                    "id": {"stringValue": str(r.get("id", ""))},
                    "fullName": {"stringValue": str(r.get("fullName", ""))},
                    "nick": {"stringValue": str(r.get("nick", ""))},
                    "rank": {"stringValue": str(r.get("rank", ""))},
                    "status": {"stringValue": str(r.get("status", "Выдано"))},
                    "qty": {"integerValue": str(r.get("qty", 0))},
                    "proof": {
                        "mapValue": {
                            "fields": {
                                "text": {"stringValue": proof_text},
                                "links": {"arrayValue": {"values": links_values}}
                            }
                        }
                    }
                }
            }
        }

    async def add_support_ticket(self, ticket_data: Dict[str, Any]) -> Optional[str]:
        token = await self.get_token()
        if not token:
            logger.error("Ticket write denied: Firebase authentication unavailable")
            return None
        url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/support_tickets"
        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {token}"

        now_iso = datetime.now(timezone.utc).isoformat()
        body = {
            "fields": {
                "nick": {"stringValue": str(ticket_data.get("nick", "Аноним"))},
                "contact": {"stringValue": str(ticket_data.get("contact", "Не указан"))},
                "category": {"stringValue": str(ticket_data.get("category", "Баг / Ошибка"))},
                "description": {"stringValue": str(ticket_data.get("description", ""))},
                "screenshot": {"stringValue": str(ticket_data.get("screenshot", ""))},
                "photo_id": {"stringValue": str(ticket_data.get("photo_id", ""))},
                "status": {"stringValue": str(ticket_data.get("status", "Новое"))},
                "source": {"stringValue": str(ticket_data.get("source", "telegram"))},
                "createdAt": {"stringValue": str(ticket_data.get("createdAt", now_iso))},
                "timestamp": {"timestampValue": now_iso}
            }
        }
        try:
            async with ClientSession() as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        doc_name = data.get("name", "")
                        doc_id = doc_name.split("/")[-1] if doc_name else ""
                        logger.info(f"Support ticket saved to Firestore: {doc_id}")
                        return doc_id
                    else:
                        err_text = await resp.text()
                        logger.warning(f"Failed to save support ticket to Firestore ({resp.status}): {err_text}")
                        return None
        except Exception as e:
            logger.error(f"Error saving support ticket: {e}")
            return None

firebase_client = FirebaseClient(FIREBASE_PROJECT_ID, FIREBASE_API_KEY, FIREBASE_EMAIL, FIREBASE_PASSWORD)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  CLOUD STATE PERSISTENCE (FIRESTORE SYNC ДЛЯ RENDER)             ║
# ╚══════════════════════════════════════════════════════════════════╝
async def sync_state_from_cloud():
    """Restore approved admins from the owner-restricted Firestore state document."""
    global _cloud_state_hydrated, _pending_legacy_nonce
    try:
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/bot_state"
        token = await firebase_client.get_token()
        if not token:
            logger.warning("Firebase authentication unavailable; cannot load cloud state")
            return
        headers = {"Authorization": f"Bearer {token}"}
        async with ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw = data.get("fields", {}).get("data", {}).get("stringValue", "{}")
                    cloud_data = json.loads(raw)
                    completed_cleanup_ids.update(
                        item for item in cloud_data.get("completed_cleanup_ids", [])
                        if isinstance(item, str)
                    )

                    cloud_users = cloud_data.get("users", {})
                    if not isinstance(cloud_users, dict):
                        raise RuntimeError("Invalid Firebase user list")
                    for uid, udata in cloud_users.items():
                        users_db[str(uid)] = udata

                    # config/bot_state must be OWNER-WRITE-ONLY in deployed Firebase rules.
                    # Never use owner_ids or an arbitrary user rank to grant permissions.
                    cloud_admins = cloud_data.get("admins", [])
                    cloud_roles = cloud_data.get("admin_roles", {})
                    if not isinstance(cloud_admins, list) or not isinstance(cloud_roles, dict):
                        raise RuntimeError("Invalid Firebase admin state")
                    filtered = sorted({
                        item for item in cloud_admins if type(item) is int
                        and item > 0 and item not in OWNER_TELEGRAM_IDS
                    })
                    filtered_roles = {
                        str(item): cloud_roles.get(str(item), "deputy")
                        if cloud_roles.get(str(item)) in ("leader", "deputy")
                        else "deputy" for item in filtered
                    }
                    signature = cloud_data.get("admin_acl_signature", "")
                    expected = sign_admin_permissions(filtered, filtered_roles)
                    if filtered and not signature:
                        # Legacy unsigned state: show it to the owner for confirmation.
                        # It must never grant access automatically.
                        _pending_legacy_admins.clear()
                        _pending_legacy_admins.update({
                            item: filtered_roles[str(item)] for item in filtered
                        })
                        _pending_legacy_nonce = uuid.uuid4().hex[:16]
                        logger.error("Unsigned historic admin list found; owner /recoveradmins approval required")
                        return
                    if signature and not hmac.compare_digest(str(signature), expected):
                        logger.error("Admin ACL signature mismatch. Refusing privilege restoration and cloud writes")
                        return
                    # The authenticated, signed cloud ACL is authoritative.
                    # Do not merge potentially stale local admin JSON into it:
                    # doing so could resurrect a previously revoked administrator.
                    admins_db[:] = filtered
                    admin_roles.clear()
                    admin_roles.update(filtered_roles)
                    save_json("admin_roles.json", admin_roles)

                    owner_cfg["owner_ids"] = list(OWNER_TELEGRAM_IDS)

                    cloud_requests = cloud_data.get("requests", {})
                    for rid, rdata in cloud_requests.items():
                        requests_db[str(rid)] = rdata

                    save_json(USERS_FILE, users_db)
                    save_json(ADMINS_FILE, admins_db)
                    save_json(OWNER_FILE, owner_cfg)
                    save_json(REQUESTS_FILE, requests_db)
                    _cloud_state_hydrated = True
                    logger.info(f"Loaded cloud bot state: {len(users_db)} users, {len(admins_db)} admins, {len(owner_cfg.get('owner_ids', []))} owners")
                elif resp.status == 404:
                    # Fail closed: deleted/missing cloud ACL must not resurrect stale local admins.
                    # Owners retain their hardcoded Telegram identity to bootstrap the system.
                    admins_db.clear()
                    admin_roles.clear()
                    save_json(ADMINS_FILE, admins_db)
                    save_json("admin_roles.json", admin_roles)
                    _cloud_state_hydrated = True
                    logger.warning("Cloud ACL missing: revoked all non-owner local administrator permissions")
                else:
                    raise RuntimeError(f"Cloud state read failed: {resp.status}")

        # Также загружаем настройки системы (Лидер, назначенный через сайт, и ключ замов)
        settings_url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/settings"
        settings_token = await firebase_client.get_token()
        settings_headers = {"Authorization": f"Bearer {settings_token}"} if settings_token else {}
        async with ClientSession() as session:
            async with session.get(settings_url, headers=settings_headers) as resp:
                if resp.status == 200:
                    s_data = await resp.json()
                    fields = s_data.get("fields", {})
                    global DEPUTY_KEY, LEADER_INFO
                    # Never load shared administrator credentials from client-readable Firestore documents.
                    if "leaderNick" in fields:
                        LEADER_INFO["nick"] = fields["leaderNick"].get("stringValue", "").strip()
                    if "leaderRank" in fields:
                        LEADER_INFO["rank"] = fields["leaderRank"].get("stringValue", "").strip()
                    if "leaderContact" in fields:
                        LEADER_INFO["contact"] = fields["leaderContact"].get("stringValue", "").strip()
                    if "techAdminNick" in fields:
                        LEADER_INFO["tech_nick"] = fields["techAdminNick"].get("stringValue", "").strip()
                    logger.info("Loaded cloud settings")
    except Exception as e:
        logger.warning(f"Could not load state from Firestore: {e}")

_state_sync_lock = asyncio.Lock()

def notify_state_changed():
    """Безопасная фоновая синхронизация с облаком Firestore."""
    try:
        loop = asyncio.get_running_loop()
        if loop and loop.is_running():
            loop.create_task(sync_state_to_cloud())
    except RuntimeError:
        pass

async def sync_state_to_cloud():
    """Save state after confirming cloud auth state was successfully hydrated."""
    async with _state_sync_lock:
        try:
            if not _cloud_state_hydrated:
                logger.error("Refusing to overwrite cloud state: initial read was not confirmed")
                return False
            token = await firebase_client.get_token()
            if not token:
                return False

            url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/bot_state"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            payload_data = {
                "users": users_db,
                "admins": admins_db,
                "admin_roles": admin_roles,
                "admin_acl_signature": sign_admin_permissions(admins_db, admin_roles),
                "completed_cleanup_ids": sorted(completed_cleanup_ids),
                "owners": owner_cfg.get("owner_ids", []),
                "requests": requests_db
            }

            body = {
                "fields": {
                    "data": {
                        "stringValue": json.dumps(payload_data, ensure_ascii=False)
                    },
                    "updatedAt": {
                        "timestampValue": datetime.now(timezone.utc).isoformat()
                    }
                }
            }

            async with ClientSession() as session:
                async with session.patch(url, headers=headers, json=body) as resp:
                    if resp.status == 200:
                        logger.info("Bot state successfully synced to cloud Firestore")
                        return True
                    else:
                        logger.warning(f"Failed to sync bot state to cloud: {resp.status}")
                        return False
        except Exception as e:
            logger.warning(f"Error syncing bot state to cloud: {e}")
            return False

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FSM STATES                                                      ║
# ╚══════════════════════════════════════════════════════════════════╝
class RegistrationStates(StatesGroup):
    waiting_for_nick = State()
    waiting_for_rank = State()

class ReportStates(StatesGroup):
    waiting_for_type = State()
    waiting_for_quantity = State()
    waiting_for_reason = State()
    waiting_for_proof = State()
    confirm = State()

class GrantStates(StatesGroup):
    waiting_for_details = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

# ╔══════════════════════════════════════════════════════════════════╗
# ║  KEYBOARDS                                                       ║
# ╚══════════════════════════════════════════════════════════════════╝
MENU_BUTTONS = [
    "📝 Подать отчет на FRP / Похвалу",
    "📊 Мой статус",
    "🌐 Открыть таблицу",
    "⚙️ Мой никнейм",
    "💬 Поддержка / Баг-репорт",
    "📋 Очередь на выдачу (Админ)",
    "👥 Список админов",
    "📌 Ожидающие заявки",
    "➕ Начислить FRP / Похвалу",
    "🧑‍💼 Управление агентами"
]

def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    admin_mode = is_admin(user_id)
    buttons = [
        [KeyboardButton(text="📝 Подать отчет на FRP / Похвалу")],
        [
            KeyboardButton(text="📊 Мой статус"),
            KeyboardButton(text="🌐 Открыть таблицу", web_app=WebAppInfo(url=WEBAPP_URL)),
        ],
        [
            KeyboardButton(text="⚙️ Мой никнейм"),
            KeyboardButton(text="💬 Поддержка / Баг-репорт")
        ]
    ]
    if admin_mode:
        buttons.append([KeyboardButton(text="📋 Очередь на выдачу (Админ)"), KeyboardButton(text="👥 Список админов")])
        buttons.append([KeyboardButton(text="📌 Ожидающие заявки"), KeyboardButton(text="➕ Начислить FRP / Похвалу")])
        buttons.append([KeyboardButton(text="🧑‍💼 Управление агентами")])

    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AIOGRAM DISPATCHER                                              ║
# ╚══════════════════════════════════════════════════════════════════╝
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("version"))
async def cmd_version(message: types.Message):
    commit = os.getenv("RENDER_GIT_COMMIT", "").strip()
    version_text = f"Версия бота: {RELEASE_TAG}"
    if commit:
        version_text += f"\nRender commit: {commit[:12]}"
    await message.answer(version_text)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id)
    if isinstance(user_data, dict) and user_data.get("nick"):
        new_username = message.from_user.username or ""
        if user_data.get("telegram_username", "") != new_username:
            user_data["telegram_username"] = new_username
            save_json(USERS_FILE, users_db)
            notify_state_changed()

    if not user_data or not user_data.get("nick"):
        await message.answer(
            "👋 Приветствую в боте автоматизации выдачи похвалы и отметок <b>Arizona RP</b>!\n\n"
            "Для начала работы введите ваш игровой никнейм (например: <code>Samuel_Norton</code>):",
            parse_mode="HTML"
        )
        await state.set_state(RegistrationStates.waiting_for_nick)
    else:
        nick_esc = html.escape(user_data['nick'])
        rank_esc = html.escape(str(user_data.get('rank', '?')))
        await message.answer(
            f"👋 С возвращением, <b>{nick_esc}</b> [{rank_esc}]!\n\n"
            "Выберите нужное действие в меню ниже:",
            reply_markup=get_main_menu(message.from_user.id),
            parse_mode="HTML"
        )

# Регистрация ника
@dp.message(RegistrationStates.waiting_for_nick)
async def process_nick(message: types.Message, state: FSMContext):
    text_val = (message.text or "").strip()

    # Если пользователь ввёл ключ напрямую или нажал команду /admin
    if text_val.startswith("/admin") or (DEPUTY_KEY and text_val == DEPUTY_KEY):
        if not text_val.startswith("/admin"):
            message.text = f"/admin {text_val}"
        await cmd_admin_auth(message, state)
        return

    # Если пользователь нажал кнопку меню или команду
    if text_val.startswith("/") or text_val in MENU_BUTTONS:
        await state.clear()
        if text_val.startswith("/setpass"):
            await cmd_set_firebase_pass(message)
            return
        elif text_val == "📋 Очередь на выдачу (Админ)" or text_val == "/queue":
            await cmd_queue(message)
            return
        elif text_val == "👥 Список админов" or text_val == "/admins":
            await cmd_list_admins(message)
            return
        elif text_val in ("📌 Ожидающие заявки", "/pending"):
            await cmd_pending(message)
            return
        elif text_val in ("➕ Начислить FRP / Похвалу", "/grant"):
            await cmd_grant_menu(message, state)
            return
        elif text_val in ("🧑‍💼 Управление агентами", "/agents"):
            await cmd_agent_panel(message, state)
            return
        elif text_val == "📊 Мой статус":
            await cmd_my_status(message)
            return
        elif text_val == "📝 Подать отчет на FRP / Похвалу":
            await start_report(message, state)
            return
        elif text_val == "⚙️ Мой никнейм":
            await cmd_change_nick(message, state)
            return
        elif text_val == "💬 Поддержка / Баг-репорт" or text_val in ["/bug", "/support"]:
            await cmd_support(message, state)
            return
        else:
            await message.answer("Действие отменено.", reply_markup=get_main_menu(message.from_user.id))
            return

    clean_nick = text_val.replace(" ", "_")

    # Валидация ника
    if len(clean_nick) < 3 or len(clean_nick) > 30 or "_" not in clean_nick or clean_nick.startswith("/"):
        await message.answer(
            "⚠️ <b>Некорректный игровой никнейм!</b>\n\n"
            "Игровой ник должен быть в формате <code>Имя_Фамилия</code> (например: <code>Gomez_Addams</code> или <code>Samuel_Norton</code>).\n\n"
            "Пожалуйста, введите правильный ник или отправьте /cancel для отмены:",
            parse_mode="HTML"
        )
        return

    nick = clean_nick
    await state.update_data(nick=nick)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 (Лидер)", callback_data="rank_X"), InlineKeyboardButton(text="9 (Зам)", callback_data="rank_9")],
        [InlineKeyboardButton(text="8 ранг", callback_data="rank_8"), InlineKeyboardButton(text="7 ранг", callback_data="rank_7")],
        [InlineKeyboardButton(text="6 ранг", callback_data="rank_6"), InlineKeyboardButton(text="5 ранг", callback_data="rank_5")],
        [InlineKeyboardButton(text="4 ранг", callback_data="rank_4"), InlineKeyboardButton(text="1-3 ранг", callback_data="rank_1")]
    ])
    await message.answer(f"Отлично, ник: <code>{html.escape(nick)}</code>!\nТеперь укажите ваш текущий ранг:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(RegistrationStates.waiting_for_rank)

@dp.callback_query(RegistrationStates.waiting_for_rank, F.data.startswith("rank_"))
async def process_rank(callback: types.CallbackQuery, state: FSMContext):
    rank = callback.data.replace("rank_", "")
    data = await state.get_data()
    nick = data["nick"]

    user_id = str(callback.from_user.id)
    users_db[user_id] = {
        "nick": nick,
        "rank": rank,
        "telegram_username": callback.from_user.username or "",
        "registered_at": datetime.now().isoformat()
    }
    save_json(USERS_FILE, users_db)
    notify_state_changed()

    await state.clear()
    await callback.message.edit_text(f"✅ Профиль успешно настроен!\n\n👤 <b>Ник:</b> <code>{html.escape(nick)}</code>\n🎖️ <b>Ранг:</b> {html.escape(rank)}", parse_mode="HTML")
    await callback.message.answer("Теперь вы можете подавать отчеты через меню:", reply_markup=get_main_menu(callback.from_user.id))

# Смена ника
@dp.message(F.text == "⚙️ Мой никнейм")
async def cmd_change_nick(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id, {})
    current_nick = user_data.get("nick", "Не указан")
    current_rank = user_data.get("rank", "Не указан")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_nick_change")]
    ])
    await message.answer(
        f"Ваш текущий профиль:\n👤 <b>Ник:</b> <code>{html.escape(current_nick)}</code>\n🎖️ <b>Ранг:</b> {html.escape(str(current_rank))}\n\n"
        "Если хотите изменить ник, напишите новый (в формате <code>Имя_Фамилия</code>):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(RegistrationStates.waiting_for_nick)

@dp.callback_query(F.data == "cancel_nick_change")
async def cancel_nick_change(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Изменение ника отменено.")

# Просмотр своего статуса
@dp.message(F.text == "📊 Мой статус")
async def cmd_my_status(message: types.Message):
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id)
    if not user_data or not user_data.get("nick"):
        await message.answer("Сначала укажите ник через /start!")
        return

    nick = user_data["nick"].lower()
    await message.answer("🔄 Запрашиваю актуальные данные из таблицы...")

    try:
        frp_rows = await firebase_client.get_sheet("fractionrp")
        praise_rows = await firebase_client.get_sheet("praise")
    except Exception as e:
        logger.error(f"Error fetching sheets: {e}")
        frp_rows, praise_rows = [], []

    frp_item = next((r for r in frp_rows if r.get("nick", "").lower() == nick), None)
    praise_item = next((r for r in praise_rows if r.get("nick", "").lower() == nick), None)

    frp_qty = frp_item.get("qty", 0) if frp_item else 0
    frp_status = frp_item.get("status", "Выдано") if frp_item else "Выдано"

    praise_qty = praise_item.get("qty", 0) if praise_item else 0
    praise_status = praise_item.get("status", "Выдано") if praise_item else "Выдано"

    nick_esc = html.escape(user_data['nick'])
    rank_esc = html.escape(str(user_data.get('rank', '?')))
    frp_status_esc = html.escape(str(frp_status))
    praise_status_esc = html.escape(str(praise_status))

    text = (
        f"📊 <b>Статус агента {nick_esc} [{rank_esc}]:</b>\n\n"
        f"🎖️ <b>/fractionrp (Отметки):</b>\n"
        f"• Количество: <b>{frp_qty}</b> шт.\n"
        f"• Статус: <b>{frp_status_esc}</b>\n\n"
        f"🌟 <b>/praise (Похвала):</b>\n"
        f"• Количество: <b>{praise_qty}</b> шт.\n"
        f"• Статус: <b>{praise_status_esc}</b>\n\n"
        f"🌐 Таблицу в реальном времени можно посмотреть по кнопке ниже:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть сайт таблицы", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

# ╔══════════════════════════════════════════════════════════════════╗
# ║  ПОДАЧА ОТЧЕТА НА ВЫДАЧУ                                         ║
# ╚══════════════════════════════════════════════════════════════════╝
@dp.message(F.text == "📝 Подать отчет на FRP / Похвалу")
async def start_report(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id)
    if not user_data or not user_data.get("nick"):
        await message.answer("⚠️ Сначала укажите ник через /start!")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎖️ /fractionrp (Отметка FRP)", callback_data="type_fractionrp")],
        [InlineKeyboardButton(text="🌟 /praise (Похвала)", callback_data="type_praise")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])
    await message.answer(
        f"👤 Подача отчета от агента: <code>{html.escape(user_data['nick'])}</code> [{html.escape(str(user_data.get('rank', '?')))}]\n\n"
        "Выберите тип выдачи:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(ReportStates.waiting_for_type)

async def show_report_reason_menu(message: types.Message, state: FSMContext, edit: bool = False):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Вербовка", callback_data="reason_вербовка")],
        [InlineKeyboardButton(text="📑 Отчёт на форуме", callback_data="reason_отчет")],
        [InlineKeyboardButton(text="🎯 Другое задание", callback_data="reason_другое")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])
    if edit:
        await message.edit_text("Выберите причину / категорию выдачи:", reply_markup=kb)
    else:
        await message.answer("Выберите причину / категорию выдачи:", reply_markup=kb)
    await state.set_state(ReportStates.waiting_for_reason)


@dp.callback_query(ReportStates.waiting_for_type, F.data.in_(["type_fractionrp", "type_praise"]))
async def process_report_type(callback: types.CallbackQuery, state: FSMContext):
    rep_type = "fractionrp" if callback.data == "type_fractionrp" else "praise"
    await state.update_data(rep_type=rep_type, quantity=1)
    # Both FRP and praise support a configurable quantity.
    label = "FRP" if rep_type == "fractionrp" else "похвал"
    icon = "🎖️" if rep_type == "fractionrp" else "🌟"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(n), callback_data=f"repqty_{n}") for n in (1, 2, 3, 5, 10)],
        [InlineKeyboardButton(text="✏️ Другое число (1–20)", callback_data="repqty_custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])
    await callback.message.edit_text(
        f"{icon} Сколько {label} указать в заявке? Выберите число от 1 до 20:",
        reply_markup=kb
    )
    await state.set_state(ReportStates.waiting_for_quantity)

@dp.callback_query(ReportStates.waiting_for_quantity, F.data.startswith("repqty_"))
async def process_report_quantity_button(callback: types.CallbackQuery, state: FSMContext):
    value = callback.data[len("repqty_"):]
    if value == "custom":
        report = await state.get_data()
        label = "FRP" if report.get("rep_type") == "fractionrp" else "похвал"
        await callback.message.edit_text(f"Введите количество {label} числом от 1 до 20:")
        await callback.answer()
        return
    if not value.isdecimal() or not 1 <= int(value) <= 20:
        await callback.answer("Укажите число от 1 до 20", show_alert=True)
        return
    await state.update_data(quantity=int(value))
    await show_report_reason_menu(callback.message, state, edit=True)
    await callback.answer()


@dp.message(ReportStates.waiting_for_quantity)
async def process_report_quantity_text(message: types.Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value.isdecimal() or not 1 <= int(value) <= 20:
        await message.answer("⚠️ Укажите целое число от 1 до 20 или отправьте /cancel.")
        return
    await state.update_data(quantity=int(value))
    await show_report_reason_menu(message, state)


@dp.callback_query(ReportStates.waiting_for_reason, F.data.startswith("reason_"))
async def process_report_reason(callback: types.CallbackQuery, state: FSMContext):
    reason = callback.data.replace("reason_", "")
    await state.update_data(reason=reason)

    await callback.message.edit_text(
        "🔗 Отправьте <b>ссылку на доказательства</b> (ссылка на ваш отчет на форуме Arizona RP):\n\n"
        "<i>(Если ссылок несколько, отправьте ссылку на тему/пост)</i>",
        parse_mode="HTML"
    )
    await state.set_state(ReportStates.waiting_for_proof)

@dp.message(ReportStates.waiting_for_proof)
async def process_report_proof(message: types.Message, state: FSMContext):
    proof = (message.text or "").strip()
    if not proof.startswith(("https://", "http://")) or len(proof) > 1000:
        await message.answer("⚠️ Отправьте ссылку http(s) длиной не более 1000 символов.")
        return
    data = await state.get_data()
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id, {})

    rep_type_label = "/fractionrp (Отметка FRP)" if data["rep_type"] == "fractionrp" else "/praise (Похвала)"
    
    await state.update_data(proof=proof)

    text = (
        "📋 <b>Проверьте данные вашего отчета:</b>\n\n"
        f"• <b>Агент:</b> <code>{html.escape(user_data.get('nick', ''))}</code> [{html.escape(str(user_data.get('rank', '?')))}]\n"
        f"• <b>Тип:</b> {rep_type_label} (+{data.get('quantity', 1)})\n"
        f"• <b>Причина:</b> {html.escape(data['reason'].capitalize())}\n"
        f"• <b>Доказательства:</b> {html.escape(proof)}\n\n"
        "Отправить отчет на проверку руководству?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить руководству", callback_data="send_report_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True, parse_mode="HTML")
    await state.set_state(ReportStates.confirm)

@dp.callback_query(F.data == "cancel_report")
async def cancel_report(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Подача отчета отменена.")

def pending_report_keyboard(req_id: str, req: Dict[str, Any]) -> InlineKeyboardMarkup:
    qty = int(req.get("quantity", 1))
    controls = []
    if req.get("rep_type") in ("fractionrp", "praise"):
        controls.append([
            InlineKeyboardButton(text="➖ 1", callback_data=f"qtyminus_{req_id}"),
            InlineKeyboardButton(text=f"{'🎖️' if req.get('rep_type') == 'fractionrp' else '🌟'} {qty}", callback_data=f"qtyshow_{req_id}"),
            InlineKeyboardButton(text="➕ 1", callback_data=f"qtyplus_{req_id}")
        ])
    controls.append([
        InlineKeyboardButton(text=f"✅ Одобрить (+{qty})", callback_data=f"approve_{req_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{req_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=controls)


def pending_report_text(req_id: str, req: Dict[str, Any]) -> str:
    qty = int(req.get("quantity", 1))
    return (
        f"📨 <b>Заявка #{html.escape(req_id)} ожидает решения</b>\n\n"
        f"👤 <b>Агент:</b> <code>{html.escape(str(req.get('nick') or ''))}</code> "
        f"[{html.escape(str(req.get('rank') or '?'))}]\n"
        f"📌 <b>Тип:</b> /{html.escape(str(req.get('rep_type') or ''))} (+{qty})\n"
        f"📝 <b>Причина:</b> {html.escape(str(req.get('reason') or ''))}\n"
        f"🔗 <b>Доказательства:</b> {html.escape(str(req.get('proof') or ''))}\n\n"
        + "Измените количество кнопками ➖/➕, затем одобрите или отклоните заявку."
    )


async def send_pending_report_to_admin(chat_id: int, req_id: str, req: Dict[str, Any]) -> None:
    sent = await bot.send_message(chat_id, pending_report_text(req_id, req),
                                  reply_markup=pending_report_keyboard(req_id, req),
                                  disable_web_page_preview=True, parse_mode="HTML")
    refs = req.setdefault("admin_messages", [])
    refs.append({"chat_id": chat_id, "message_id": sent.message_id})
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id,
                                   disable_notification=True)
    except Exception as exc:
        logger.info(f"Could not pin pending report in {chat_id}: {exc}")


async def refresh_pending_report_messages(req_id: str, req: Dict[str, Any]) -> None:
    for info in req.get("admin_messages", []):
        try:
            await bot.edit_message_text(
                pending_report_text(req_id, req), chat_id=int(info["chat_id"]),
                message_id=int(info["message_id"]),
                reply_markup=pending_report_keyboard(req_id, req),
                parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as exc:
            logger.info(f"Could not refresh pending report: {exc}")


async def close_pending_report_messages(req_id: str, req: Dict[str, Any], result: str) -> None:
    text = pending_report_text(req_id, req) + f"\n\n{html.escape(result)}"
    for info in req.get("admin_messages", []):
        try:
            await bot.edit_message_text(text, chat_id=int(info["chat_id"]),
                                        message_id=int(info["message_id"]),
                                        parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc:
            logger.info(f"Could not close report notification: {exc}")
        try:
            await bot.unpin_chat_message(chat_id=int(info["chat_id"]),
                                         message_id=int(info["message_id"]))
        except Exception as exc:
            logger.info(f"Could not unpin completed report: {exc}")


@dp.callback_query(F.data.startswith("qtyshow_"))
async def show_report_quantity(callback: types.CallbackQuery):
    await callback.answer("Используйте ➖/➕, чтобы изменить количество.")


@dp.callback_query(F.data.startswith("qtyplus_") | F.data.startswith("qtyminus_"))
async def adjust_report_quantity(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    plus = callback.data.startswith("qtyplus_")
    req_id = callback.data.split("_", 1)[1]
    req = requests_db.get(req_id)
    if not req or req.get("status") != "pending" or req.get("rep_type") not in ("fractionrp", "praise"):
        await callback.answer("Заявка уже закрыта или не найдена.", show_alert=True)
        return
    new_qty = max(1, min(20, int(req.get("quantity", 1)) + (1 if plus else -1)))
    req["quantity"] = new_qty
    save_json(REQUESTS_FILE, requests_db)
    notify_state_changed()
    await refresh_pending_report_messages(req_id, req)
    await callback.answer(f"Новое количество: {new_qty}")


@dp.callback_query(ReportStates.confirm, F.data == "send_report_confirm")
async def send_report_confirmed(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = str(callback.from_user.id)
    user_data = users_db.get(user_id, {})
    await state.clear()

    req_id = uuid.uuid4().hex[:12]
    requests_db[req_id] = {
        "req_id": req_id,
        "user_id": user_id,
        "nick": user_data.get("nick"),
        "rank": user_data.get("rank"),
        "rep_type": data["rep_type"],
        "reason": data["reason"],
        "proof": data["proof"],
        "quantity": int(data.get("quantity", 1)),
        "admin_messages": [],
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    save_json(REQUESTS_FILE, requests_db)
    notify_state_changed()

    await callback.message.edit_text(
        f"✅ <b>Отчет #{req_id} успешно отправлен руководству!</b>\n\n"
        "Как только лидер или заместитель проверит его, вам придет уведомление.",
        parse_mode="HTML"
    )

    # Every pending request gets its own pinned administrator message.
    recipients = set(owner_cfg.get("owner_ids", []) + admins_db)
    for adm_id in recipients:
        try:
            await send_pending_report_to_admin(adm_id, req_id, requests_db[req_id])
        except Exception as exc:
            logger.warning(f"Could not notify admin {adm_id}: {exc}")
    save_json(REQUESTS_FILE, requests_db)
    await sync_state_to_cloud()

# ╔══════════════════════════════════════════════════════════════════╗
# ║  ОДОБРЕНИЕ / ОТКЛОНЕНИЕ ОТЧЕТА АДМИНОМ                          ║
# ╚══════════════════════════════════════════════════════════════════╝
@dp.callback_query(F.data.startswith("approve_"))
async def approve_report(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    req_id = callback.data.replace("approve_", "")
    req = requests_db.get(req_id)
    if not req or req.get("status") != "pending":
        await callback.answer("Заявка уже была обработана ранее!", show_alert=True)
        return

    quantity = int(req.get("quantity", 1))
    # Lock this request before the first network await so other callbacks cannot
    # approve/reject or change quantity while the credit is being applied.
    req["status"] = "processing"
    try:
        success = await firebase_client.credit_report(
            req["rep_type"], req["nick"], req.get("rank", "8"),
            req["reason"], req["proof"], req_id, quantity=quantity
        )
    except Exception as exc:
        req["status"] = "pending"
        logger.error(f"Error updating sheet on approve: {exc}")
        await callback.answer("⚠️ Нет подтверждения Firebase; проверьте таблицу и повторите.", show_alert=True)
        return
    if not success:
        req["status"] = "pending"
        await callback.answer("⚠️ Firebase не обновился. Заявка остаётся ожидающей.", show_alert=True)
        return

    req["status"] = "approved"
    req["approved_by"] = callback.from_user.full_name
    save_json(REQUESTS_FILE, requests_db)
    notify_state_changed()
    try:
        await callback.answer("✅ База данных обновлена!", show_alert=False)
    except Exception as exc:
        logger.info(f"Could not answer approval callback: {exc}")
    await close_pending_report_messages(
        req_id, req, f"✅ ОДОБРЕНО: +{quantity} ({callback.from_user.full_name})"
    )
    try:
        await bot.send_message(int(req["user_id"]),
                               f"✅ Отчет #{req_id} одобрен. Начислено +{quantity} балл(ов).")
    except Exception as exc:
        logger.warning(f"Could not notify reporter: {exc}")


@dp.callback_query(F.data.startswith("reject_"))
async def reject_report(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    req_id = callback.data.replace("reject_", "")
    req = requests_db.get(req_id)
    if not req or req.get("status") != "pending":
        await callback.answer("Заявка уже была обработана ранее!", show_alert=True)
        return

    req["status"] = "rejected"
    req["rejected_by"] = callback.from_user.full_name
    save_json(REQUESTS_FILE, requests_db)
    notify_state_changed()

    await close_pending_report_messages(req_id, req, f"❌ ОТКЛОНЕНО ({callback.from_user.full_name})")

    try:
        await bot.send_message(
            int(req["user_id"]),
            f"❌ <b>Ваш отчет #{req_id} на /{req['rep_type']} был отклонен руководством.</b>\n"
            "Проверьте правильность ссылок или обратитесь к лидеру.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error notifying user on reject: {e}")

# ╔══════════════════════════════════════════════════════════════════╗
# ║  АДМИНСКАЯ ПАНЕЛЬ И ПОДТВЕРЖДЕНИЕ ВХОДА ДЛЯ ЗАМЕСТИТЕЛЕЙ         ║
# ╚══════════════════════════════════════════════════════════════════╝

# Авторизация: /admin ключ
@dp.message(Command("security"))
async def cmd_security_overview(message: types.Message):
    """Owner-only overview of the actual in-memory Telegram ACL."""
    if not is_owner(message.from_user.id) or getattr(message.chat, "type", "") != "private":
        await message.answer("⛔ Проверка прав доступна только владельцу в личных сообщениях.")
        return
    all_admins = sorted({
        uid for uid in admins_db if type(uid) is int and uid not in OWNER_TELEGRAM_IDS
    })
    members = []
    for uid in all_admins[:50]:
        info = users_db.get(str(uid), {})
        nick = html.escape(str(info.get("nick", "Не зарегистрирован")))
        role = "👑 Лидер" if is_leader(uid) else "⭐ Заместитель"
        members.append(f"• {nick} (ID <code>{uid}</code>) — {role}")
    if len(all_admins) > 50:
        members.append(f"…и ещё {len(all_admins)-50} администраторов")
    if not members:
        members.append("• Никого, кроме владельца")
    state_label = "✅ Firebase загружена" if _cloud_state_hydrated else "⚠️ Firebase НЕ подтверждена"
    await message.answer(
        f"🔐 <b>Аудит доступа бота</b>\n"
        f"Версия: <code>{html.escape(RELEASE_TAG)}</code>\n"
        f"{state_label}\n"
        f"Владелец: <code>{next(iter(OWNER_TELEGRAM_IDS))}</code>\n"
        f"Администраторов: <b>{len(all_admins)}</b>\n\n"
        + "\n".join(members)
        + "\n\nДля снятия лишних прав используйте /admins. "
          "Никто не получает доступ только по рангу, ключу или ADMIN_CHAT_ID.",
        parse_mode="HTML"
    )


@dp.message(Command("recoveradmins"))
async def cmd_recover_admins(message: types.Message):
    if not is_owner(message.from_user.id) or getattr(message.chat, "type", "") != "private":
        await message.answer("⛔ Только владелец в личном чате может восстанавливать права.")
        return
    if not _pending_legacy_admins:
        await message.answer("Старый список администраторов для восстановления не найден. "
                             "Если облачная запись уже была затёрта, потребуется резервная копия.")
        return
    lines = []
    for uid, role in sorted(_pending_legacy_admins.items()):
        profile = users_db.get(str(uid), {})
        nick = html.escape(str(profile.get("nick", "Неизвестно")))
        lines.append(f"• {nick} — ID {uid} — {role}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Восстановить перечисленных", callback_data=f"aclrestore:{_pending_legacy_nonce}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="aclrestorecancel")]
    ])
    await message.answer(
        "🔐 <b>Найден архивный неподписанный список:</b>\n\n"
        + "\n".join(lines) + "\n\nПроверьте каждого человека перед подтверждением.",
        reply_markup=kb, parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("aclrestore:"))
async def callback_recover_admins(callback: types.CallbackQuery):
    global _cloud_state_hydrated, _pending_legacy_nonce
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    nonce = callback.data.removeprefix("aclrestore:")
    if not nonce or nonce != _pending_legacy_nonce or not _pending_legacy_admins:
        await callback.answer("Запрос устарел.", show_alert=True)
        return
    previous_admins = list(admins_db)
    previous_roles = dict(admin_roles)
    for uid, role in _pending_legacy_admins.items():
        if uid not in admins_db:
            admins_db.append(uid)
        admin_roles[str(uid)] = role
    admins_db.sort()
    _cloud_state_hydrated = True
    if not await sync_state_to_cloud():
        admins_db[:] = previous_admins
        admin_roles.clear()
        admin_roles.update(previous_roles)
        _cloud_state_hydrated = False
        await callback.answer("⚠️ Восстановление не сохранено. Проверьте Firebase.", show_alert=True)
        return
    save_json(ADMINS_FILE, admins_db)
    save_json("admin_roles.json", admin_roles)
    _pending_legacy_admins.clear()
    _pending_legacy_nonce = ""
    await callback.message.edit_text("✅ Список администраторов подтверждён и сохранён.")
    await callback.answer()


@dp.callback_query(F.data == "aclrestorecancel")
async def callback_recover_cancel(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text("Восстановление отменено. Исходный список пока не изменён.")
    await callback.answer()


@dp.message(Command("admin"))
async def cmd_admin_auth(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    user_data = users_db.get(str(uid), {})
    nick = user_data.get("nick", message.from_user.full_name)
    rank = user_data.get("rank", "?")

    args = (message.text or "").split(maxsplit=1)
    # Direct admin invite is available ONLY to the owner and only for known users.
    # The Telegram Bot API cannot resolve arbitrary @usernames to user IDs.
    if len(args) > 1 and args[1].strip().startswith("@"):
        if not is_owner(uid):
            await message.answer("⛔ Назначать администраторов по @username может только владелец.")
            return
        if getattr(message.chat, "type", "") != "private":
            await message.answer("⚠️ Используйте эту команду в личном чате с ботом.")
            return
        handle = args[1].strip()[1:]
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", handle):
            await message.answer("⚠️ Укажите Telegram-юзернейм: /admin @username")
            return
        candidates = [(int(user_id), entry) for user_id, entry in users_db.items()
                      if str(user_id).isdigit() and isinstance(entry, dict)
                      and (entry.get("telegram_username") or "").casefold() == handle.casefold()]
        if len(candidates) != 1:
            await message.answer(
                "⚠️ Пользователь не найден или ник неоднозначен. "
                "Попросите его открыть бота и отправить /start, затем повторите."
            )
            return
        target_id, target_profile = candidates[0]
        # Cached usernames can be reassigned. Verify the live Telegram identity
        # before allowing owner-approved privilege escalation.
        try:
            live_chat = await bot.get_chat(target_id)
        except Exception as exc:
            logger.warning(f"Could not verify Telegram username for {target_id}: {exc}")
            await message.answer("⚠️ Не удалось проверить Telegram-юзернейм. Попросите человека написать /start.")
            return
        if (getattr(live_chat, "username", "") or "").casefold() != handle.casefold():
            await message.answer("⚠️ Telegram-юзернейм изменился. Попросите человека написать /start.")
            return
        if target_id == uid:
            await message.answer("ℹ️ Владельца нельзя переназначить.")
            return
        nonce = uuid.uuid4().hex[:16]
        pending_admin_requests[target_id] = {
            "expires": datetime.now(timezone.utc).timestamp() + 600,
            "nonce": nonce, "source": "owner", "role": None
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Лидер", callback_data=f"adm_role_{target_id}_{nonce}_leader")],
            [InlineKeyboardButton(text="⭐ Заместитель", callback_data=f"adm_role_{target_id}_{nonce}_deputy")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_deny_{target_id}_{nonce}")]
        ])
        await message.answer(
            f"👤 <b>Назначить администратором?</b>\n"
            f"Telegram: @{html.escape(handle)}\n"
            f"Игровой ник: {html.escape(str(target_profile.get('nick', '—')))}\n"
            f"ID: <code>{target_id}</code>\n\n"
            "Права будут выданы только после подтверждения.",
            reply_markup=kb, parse_mode="HTML"
        )
        return
    if len(args) < 2 and uid not in OWNER_TELEGRAM_IDS:
        await message.answer(
            "ℹ️ <b>Авторизация в системе руководства:</b>\n\n"
            "• <b>Для заместителей и лидеров:</b>\n"
            "Используйте команду /admin с вашим персональным ключом.\n"
            "<i>(После отправки запрос поступит Главному Лидеру на одобрение)</i>\n\n"
            "• <b>Для Главного Руководителя:</b>\n"
            "Используйте ваш личный мастер-ключ: <code>/admin мастер_ключ</code>",
            parse_mode="HTML"
        )
        return

    key = args[1].strip().strip('"').strip("'") if len(args) > 1 else ""
    
    # 1. Мастер-ключи Владельца (мгновенный вход без подтверждений)
    if uid in OWNER_TELEGRAM_IDS:
        if uid not in admins_db:
            admins_db.append(uid)
            save_json(ADMINS_FILE, admins_db)
        notify_state_changed()

        lead_nick = html.escape(LEADER_INFO.get("nick", "Не назначен"))
        await message.answer(
            "🛠️ <b>Вы успешно авторизованы как ГЛАВНЫЙ ТЕХНИЧЕСКИЙ АДМИНИСТРАТОР!</b>\n\n"
            f"👑 <b>Действующий Лидер фракции:</b> <code>{lead_nick}</code>\n"
            "<i>(Лидера вы можете в любой момент назначить или сменить через Панель управления на сайте)</i>\n\n"
            "🔑 Ключ заместителя задаётся только в настройках Render.\n"
            "Когда заместитель вводит этот ключ, вам в личные сообщения бота приходит запрос с кнопками «Одобрить» / «Отклонить».\n\n"
            "Вам доступны:\n"
            "• Подтверждение доступа для новых заместителей\n"
            "• Очередь на выдачу в игре (<code>/queue</code>)\n"
            "• Просмотр и снятие заместителей (<code>/admins</code>)\n"
            "• Мониторинг всех заявок и обращений",
            reply_markup=get_main_menu(uid),
            parse_mode="HTML"
        )
        return

    # Never accept shared credentials in groups or channels: anyone present
    # can read the message and reuse the key. Grant requests require a DM.
    if uid not in OWNER_TELEGRAM_IDS and getattr(message.chat, "type", "") != "private":
        await message.answer("⛔ Авторизация доступна только в личном чате с ботом. Не отправляйте ключ в группу.")
        return

    # 2. Ключ заместителя (всегда требует подтверждения владельца)
    if DEPUTY_KEY and key == DEPUTY_KEY:
        if is_admin(uid):
            await message.answer(
                "ℹ️ <b>Вы уже являетесь администратором бота!</b>\n"
                "Все функции руководства доступны в меню ниже.",
                reply_markup=get_main_menu(uid),
                parse_mode="HTML"
            )
            return

        # Key alone never grants access: the owner must approve a fresh request.
        nonce = uuid.uuid4().hex[:16]
        pending_admin_requests[uid] = {"expires": datetime.now(timezone.utc).timestamp() + 600,
                                       "nonce": nonce, "source": "deputy", "role": "deputy"}

        # Уведомляем заместителя о том, что запрос отправлен
        await message.answer(
            "⏳ <b>Ключ заместителя принят! Запрос отправлен Главному Лидеру.</b>\n\n"
            "Пожалуйста, ожидайте подтверждения. Как только Главный Руководитель нажмёт кнопку «Одобрить доступ», вам придёт уведомление и откроется админ-меню.",
            parse_mode="HTML"
        )

        # Кому слать запрос: владельцам или существующим админам
        recipients = list(OWNER_TELEGRAM_IDS)

        if not recipients:
            await message.answer(
                "⚠️ <b>Внимание:</b> Главный Лидер ещё не авторизовался в боте по мастер-ключу.\n"
                "Попросите его сначала зайти в бота и ввести свой мастер-ключ, после чего повторите команду.",
                parse_mode="HTML"
            )
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить доступ", callback_data=f"adm_grant_{uid}_{nonce}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_deny_{uid}_{nonce}")
            ]
        ])

        username_str = f"(@{message.from_user.username})" if message.from_user.username else ""
        req_msg = (
            "👑 <b>Запрос на получение прав Администратора / Заместителя!</b>\n\n"
            f"👤 <b>Агент:</b> <code>{html.escape(nick)}</code> [{html.escape(str(rank))}]\n"
            f"🆔 <b>Telegram ID:</b> <code>{uid}</code>\n"
            f"📱 <b>Пользователь:</b> {html.escape(message.from_user.full_name)} {html.escape(username_str)}\n"
            f"🔑 <b>Введённый ключ:</b> выданный ключ\n\n"
            "Предоставить пользователю права руководства (проверка отчетов, очередь на выдачу, списание баллов)?"
        )

        for rec_id in recipients:
            try:
                await bot.send_message(rec_id, req_msg, reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Could not send admin request to {rec_id}: {e}")
        return

    # 3. Неверный ключ
    await message.answer(
        "❌ <b>Неверный ключ доступа!</b>\n\n"
        f"• Если вы заместитель: введите <code>/admin [KEY REDACTED]</code>\n"
        "• Если вы владелец: используйте личный мастер-ключ.",
        parse_mode="HTML"
    )

# Обработка одобрения доступа заму владельцем
def verify_admin_request(data: str, prefix: str):
    parts = data.removeprefix(prefix).split("_")
    if len(parts) != 2:
        return None
    raw_id, nonce = parts
    if not raw_id.isdecimal() or not re.fullmatch(r"[a-f0-9]{16}", nonce):
        return None
    target_id = int(raw_id)
    pending = pending_admin_requests.get(target_id)
    if (not pending or pending.get("nonce") != nonce or
            pending.get("expires", 0) < datetime.now(timezone.utc).timestamp()):
        pending_admin_requests.pop(target_id, None)
        return None
    return target_id


@dp.callback_query(F.data.startswith("adm_role_"))
async def callback_admin_select_role(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец может назначать руководство.", show_alert=True)
        return
    parts = callback.data.removeprefix("adm_role_").split("_")
    if len(parts) != 3:
        await callback.answer("Неверный запрос.", show_alert=True)
        return
    target, nonce, role = parts
    if not target.isdecimal() or role not in ("leader", "deputy"):
        await callback.answer("Неверная роль.", show_alert=True)
        return
    target_id = int(target)
    if verify_admin_request(f"adm_grant_{target_id}_{nonce}", "adm_grant_") != target_id:
        await callback.answer("Запрос истёк.", show_alert=True)
        return
    request = pending_admin_requests.get(target_id, {})
    if request.get("source") != "owner":
        await callback.answer("Недопустимый запрос.", show_alert=True)
        return
    request["role"] = role
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить назначение", callback_data=f"adm_grant_{target_id}_{nonce}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_deny_{target_id}_{nonce}")]
    ])
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("👑 Лидер" if role == "leader" else "⭐ Заместитель")


@dp.callback_query(F.data.startswith("adm_grant_"))
async def callback_admin_grant(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец может выдавать права!", show_alert=True)
        return
    target_id = verify_admin_request(callback.data, "adm_grant_")
    if target_id is None:
        await callback.answer("Запрос недействителен или истёк.", show_alert=True)
        return
    role = pending_admin_requests.get(target_id, {}).get("role")
    if role not in ("leader", "deputy"):
        await callback.answer("Сначала выберите роль.", show_alert=True)
        return
    was_admin = target_id in admins_db
    previous_role = admin_roles.get(str(target_id))
    if not was_admin:
        admins_db.append(target_id)
    admin_roles[str(target_id)] = role
    if not await sync_state_to_cloud():
        if not was_admin:
            admins_db.remove(target_id)
        if previous_role is None:
            admin_roles.pop(str(target_id), None)
        else:
            admin_roles[str(target_id)] = previous_role
        await callback.answer(
            "⚠️ Права НЕ подтверждены Firebase. Попробуйте ещё раз после проверки связи.",
            show_alert=True
        )
        return
    pending_admin_requests.pop(target_id, None)
    save_json(ADMINS_FILE, admins_db)
    save_json("admin_roles.json", admin_roles)
    target_user = users_db.get(str(target_id), {})
    try:
        await callback.message.edit_text(
            callback.message.text + "\n\n✅ <b>ПРАВА ВЫДАНЫ</b>",
            parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning(f"Could not refresh admin grant message: {exc}")
    try:
        await bot.send_message(
            target_id,
            f"🎉 <b>Ваша роль: {role_name(target_id)}.</b>\n"
            "Откройте /start для обновления меню.",
            reply_markup=get_main_menu(target_id), parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning(f"Could not notify new admin {target_id}: {exc}")
    await callback.answer(f"✅ Доступ для {html.escape(str(target_user.get('nick', target_id)))} выдан.")


@dp.callback_query(F.data.startswith("adm_deny_"))
async def callback_admin_deny(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только владелец может отклонять заявки!", show_alert=True)
        return
    target_id = verify_admin_request(callback.data, "adm_deny_")
    if target_id is None:
        await callback.answer("Запрос недействителен или истёк.", show_alert=True)
        return
    pending_admin_requests.pop(target_id, None)
    try:
        await callback.message.edit_text(callback.message.text + "\n\n❌ <b>ОТКЛОНЕНО</b>",
                                         parse_mode="HTML")
    except Exception as exc:
        logger.warning(f"Could not refresh rejected admin request: {exc}")
    await callback.answer("Запрос отклонён.")


# Команда настройки пароля Firebase прямо из Telegram
@dp.message(Command("setpass"))
async def cmd_set_firebase_pass(message: types.Message):
    await message.answer("Настройте FIREBASE_PASSWORD в Render Environment. Передавать пароль боту запрещено.")

@dp.message(F.text == "👥 Список админов")
@dp.message(Command("admins"))
async def cmd_list_admins(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступно только руководству фракции.")
        return

    all_admins = list(set(owner_cfg.get("owner_ids", []) + admins_db))
    lead_nick = html.escape(LEADER_INFO.get("nick", "Не назначен"))
    lead_rank = html.escape(str(LEADER_INFO.get("rank", "10")))
    tech_nick = html.escape(LEADER_INFO.get("tech_nick", "Gopon"))

    text = (
        f"👥 <b>Руководство фракции и системы:</b>\n\n"
        f"🛠️ <b>Главный Тех. Администратор:</b>\n"
        f"• <b>{tech_nick}</b> 👑 <i>(Создатель системы)</i>\n\n"
        f"👑 <b>Действующий Лидер фракции:</b>\n"
        f"• <b>{lead_nick}</b> [{lead_rank}]\n"
        f"<i>(Лидер назначается Тех. Администратором через Панель управления на сайте)</i>\n\n"
        f"⭐ <b>Авторизованные Заместители в боте ({len(all_admins)} чел.):</b>\n"
    )
    kb_rows = []

    for i, adm_id in enumerate(all_admins, 1):
        adm_user = users_db.get(str(adm_id), {})
        nick = adm_user.get("nick", "Ник не привязан")
        rank = adm_user.get("rank", "")
        rank_str = f" [{rank}]" if rank else ""
        username = f"(@{adm_user.get('telegram_username')})" if adm_user.get("telegram_username") else ""
        is_me = (adm_id == message.from_user.id)
        is_own = adm_id in owner_cfg.get("owner_ids", [])
        
        role_label = " 👑 <b>(Владелец)</b>" if is_own else (" 👑 <b>(Лидер)</b>" if is_leader(adm_id) else " ⭐ <i>(Заместитель)</i>")
        me_label = " <i>(Вы)</i>" if is_me else ""

        text += f"{i}. <b>{html.escape(nick)}</b>{html.escape(rank_str)} {html.escape(username)}{role_label}{me_label}\n   ID: <code>{adm_id}</code>\n\n"

        # Снимать замов может только Главный Владелец
        if not is_me and not is_own and is_owner(message.from_user.id):
            kb_rows.append([InlineKeyboardButton(text=f"❌ Снять {nick}", callback_data=f"remove_admin_{adm_id}")])

    text += (
        "💡 <b>Как добавить нового заместителя:</b>\n"
        "Передайте ему ключ для входа:\n"
        f"<code>/admin [KEY REDACTED]</code>\n\n"
        "После отправки команды вам в этот чат придёт запрос с кнопками <b>[Одобрить доступ]</b> / <b>[Отклонить]</b>."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("remove_admin_"))
async def process_remove_admin(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Только Главный Руководитель может снимать заместителей!", show_alert=True)
        return

    target_id = int(callback.data.replace("remove_admin_", ""))
    if target_id == callback.from_user.id:
        await callback.answer("Нельзя снять самого себя!", show_alert=True)
        return

    if target_id in owner_cfg.get("owner_ids", []):
        await callback.answer("Нельзя снять Главного Владельца!", show_alert=True)
        return

    if target_id in admins_db:
        previous_role = admin_roles.get(str(target_id))
        admins_db.remove(target_id)
        admin_roles.pop(str(target_id), None)
        if not await sync_state_to_cloud():
            admins_db.append(target_id)
            if previous_role:
                admin_roles[str(target_id)] = previous_role
            await callback.answer(
                "⚠️ Удаление прав НЕ сохранено в Firebase. Повторите позже.",
                show_alert=True
            )
            return
        save_json("admin_roles.json", admin_roles)
        save_json(ADMINS_FILE, admins_db)
        await callback.answer("Заместитель успешно снят!", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await cmd_list_admins(callback.message)
    else:
        await callback.answer("Администратор не найден в списке.", show_alert=True)

@dp.message(Command("pending"))
@dp.message(F.text == "📌 Ожидающие заявки")
async def cmd_pending(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступно только руководству.")
        return
    pending = [(rid, req) for rid, req in requests_db.items() if req.get("status") == "pending"]
    if not pending:
        await message.answer("✅ Заявок, ожидающих решения, нет.")
        return
    await message.answer(f"📌 Ожидают решения: {len(pending)}. Отправляю и закрепляю сообщения.")
    for rid, req in pending[:30]:
        existing = next((ref for ref in req.get("admin_messages", [])
                         if int(ref["chat_id"]) == message.chat.id), None)
        if existing:
            try:
                await bot.pin_chat_message(chat_id=message.chat.id,
                                           message_id=int(existing["message_id"]),
                                           disable_notification=True)
                continue
            except Exception:
                pass
        try:
            await send_pending_report_to_admin(message.chat.id, rid, req)
        except Exception as exc:
            logger.warning(f"Could not restore pinned report {rid}: {exc}")
    save_json(REQUESTS_FILE, requests_db)
    await sync_state_to_cloud()
    if len(pending) > 30:
        await message.answer("⚠️ Показаны первые 30 заявок. Остальные доступны после их обработки.")


def parse_grant_args(raw: str):
    """Parse 'frp nick1,nick2 2'; refuse duplicates or partially invalid batches."""
    parts = raw.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Формат: /grant frp Nick_One,Nick_Two 2")
    kind, rest = parts
    sheet = {"frp": "fractionrp", "fractionrp": "fractionrp",
             "praise": "praise"}.get(kind.casefold())
    if not sheet:
        raise ValueError("Тип должен быть frp или praise.")
    try:
        nick_text, amount_text = rest.rsplit(maxsplit=1)
    except ValueError as exc:
        raise ValueError("Укажите никнеймы и количество, например: /grant frp Nick_One,Nick_Two 2") from exc
    if not amount_text.isdecimal() or not 1 <= int(amount_text) <= 20:
        raise ValueError("Количество для каждого агента должно быть от 1 до 20.")
    nicks = [name.strip() for name in nick_text.split(",")]
    if not 1 <= len(nicks) <= 10:
        raise ValueError("Можно указать от 1 до 10 агентов.")
    if any(not re.fullmatch(r"[A-Za-z0-9_]{2,30}", name) for name in nicks):
        raise ValueError("Неверный ник: используйте 2–30 латинских букв, цифр или _.")
    if len({name.casefold() for name in nicks}) != len(nicks):
        raise ValueError("Одинаковые ники в одной команде повторять нельзя.")
    return sheet, nicks, int(amount_text)


async def perform_manual_grant(message: types.Message, raw: str):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только лидер или заместитель может начислять баллы.")
        return
    try:
        sheet, nicks, qty = parse_grant_args(raw)
    except ValueError as exc:
        await message.answer("⚠️ " + html.escape(str(exc)))
        return

    results = []
    for nick in nicks:
        registered = next(
            (u for u in users_db.values() if isinstance(u, dict)
             and str(u.get("nick", "")).casefold() == nick.casefold()), None
        )
        rank = str(registered.get("rank", "8")) if registered else "8"
        grant_id = "manual-" + uuid.uuid4().hex
        try:
            success = await firebase_client.credit_report(
                sheet, nick, rank, f"Ручное начисление администратором {message.from_user.id}",
                "", grant_id, quantity=qty
            )
            if success:
                results.append(f"✅ {html.escape(nick)}: +{qty}")
            else:
                results.append(f"⚠️ {html.escape(nick)}: не подтверждено")
        except Exception as exc:
            logger.error(f"Manual grant {grant_id} failed for {nick}: {exc}")
            results.append(f"⚠️ {html.escape(nick)}: проверьте баланс перед повтором")

    await message.answer(
        f"📊 <b>Начисление /{sheet} ({len(nicks)} агентов):</b>\n\n"
        + "\n".join(results)
        + "\n\n<i>Если запись не подтверждена, сначала проверьте таблицу, "
          "чтобы избежать двойного начисления.</i>",
        parse_mode="HTML"
    )

@dp.message(Command("grant"))
async def cmd_grant(message: types.Message):
    parts = (message.text or "").split(maxsplit=1)
    await perform_manual_grant(message, parts[1] if len(parts) > 1 else "")


@dp.message(F.text == "➕ Начислить FRP / Похвалу")
async def cmd_grant_menu(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для руководства.")
        return
    await state.set_state(GrantStates.waiting_for_details)
    await message.answer(
        "➕ Введите тип, ник и количество (1–20):\n"
        "<code>frp Gomez_Addams 2</code>\n"
        "<code>praise Gomez_Addams 3</code>\n\n"
        "Или используйте /grant praise Gomez_Addams 3. Для отмены: /cancel.",
        parse_mode="HTML"
    )


@dp.message(GrantStates.waiting_for_details)
async def process_grant_details(message: types.Message, state: FSMContext):
    if (message.text or "").strip() == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_main_menu(message.from_user.id))
        return
    parts = (message.text or "").split(maxsplit=1)
    raw = parts[1] if parts and parts[0] == "/grant" and len(parts) > 1 else (message.text or "")
    await state.clear()
    await perform_manual_grant(message, raw)


# Button-driven administration for all registered and sheet-listed agents.
# Only in-memory navigation state is stored here; balances remain authoritative in Firebase.
agent_search_terms: Dict[int, str] = {}
agent_action_tokens: Dict[str, Dict[str, Any]] = {}


class AgentPanelStates(StatesGroup):
    waiting_for_search = State()


async def remove_agent_everywhere(nick: str) -> bool:
    """Remove an unneeded agent only when its two outstanding balances are zero."""
    if not _cloud_state_hydrated:
        raise RuntimeError("Cannot delete agents before cloud state is loaded")
    if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", nick):
        raise ValueError("Invalid agent nickname")
    matches = [(uid, entry) for uid, entry in users_db.items()
               if isinstance(entry, dict)
               and str(entry.get("nick", "")).casefold() == nick.casefold()]
    if any(uid.isdecimal() and (int(uid) in OWNER_TELEGRAM_IDS or int(uid) in admins_db)
           for uid, _ in matches):
        raise ValueError("Нельзя удалить руководство через очистку агентов.")
    # Verify balances before touching any persistent data.
    rows_by_sheet = await asyncio.gather(
        firebase_client.get_sheet("fractionrp"), firebase_client.get_sheet("praise")
    )
    for rows in rows_by_sheet:
        if any(str(row.get("nick", "")).casefold() == nick.casefold() and
               int(row.get("qty") or 0) != 0 for row in rows):
            raise ValueError("У агента есть ненулевой баланс. Очистка отменена.")

    # Each sheet uses updateTime to avoid overwriting concurrent grants.
    for sheet in ("fractionrp", "praise"):
        if not await firebase_client.remove_zero_balance_agent(sheet, nick):
            raise ValueError("Баланс изменился. Удаление отменено.")

    for uid, _ in matches:
        users_db.pop(uid, None)
    if matches:
        save_json(USERS_FILE, users_db)
        await sync_state_to_cloud()
    return True


def agent_hash(nick: str) -> str:
    return hashlib.sha256(nick.casefold().encode("utf-8")).hexdigest()[:12]


async def fetch_agent_directory() -> List[Dict[str, Any]]:
    frp_rows, praise_rows = await asyncio.gather(
        firebase_client.get_sheet("fractionrp"), firebase_client.get_sheet("praise")
    )
    directory: Dict[str, Dict[str, Any]] = {}

    def ensure_agent(nick: str):
        key = nick.casefold()
        if key not in directory:
            directory[key] = {
                "nick": nick, "rank": "?", "frp": 0, "praise": 0,
                "frp_status": "—", "praise_status": "—", "uid": None
            }
        return directory[key]

    for uid, data in users_db.items():
        if not isinstance(data, dict):
            continue
        nick = str(data.get("nick", "")).strip()
        if not nick:
            continue
        item = ensure_agent(nick)
        item["rank"] = str(data.get("rank", "?"))
        item["uid"] = str(uid)

    for rows, prefix in ((frp_rows, "frp"), (praise_rows, "praise")):
        for row in rows:
            nick = str(row.get("nick", "")).strip()
            if not nick:
                continue
            item = ensure_agent(nick)
            item[prefix] = int(row.get("qty") or 0)
            item[prefix + "_status"] = str(row.get("status", "—"))
            if item["rank"] == "?" and row.get("rank"):
                item["rank"] = str(row["rank"])

    def rank_priority(item: Dict[str, Any]):
        raw = str(item.get("rank") or "").strip().upper()
        # Registration stores leader rank as X, while the website can use 10 or "10 (Лидер)".
        if raw == "X":
            rank = 10
        else:
            matched = re.match(r"^(10|[1-9])(?:[^0-9]|$)", raw)
            rank = int(matched.group(1)) if matched else 0
        return (-rank, str(item["nick"]).casefold())

    return sorted(directory.values(), key=rank_priority)


def agent_list_markup(agents: List[Dict[str, Any]], page: int):
    page_size = 8
    pages = max(1, (len(agents) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    text = f"🧑‍💼 <b>Агенты — {len(agents)} чел.</b> (страница {page + 1}/{pages})\n\n"
    if agents:
        for agent in agents[page * page_size:(page + 1) * page_size]:
            text += (f"• <b>{html.escape(agent['nick'])}</b> [{html.escape(agent['rank'])}] "
                     f"— 🎖️ {agent['frp']} / 🌟 {agent['praise']}\n")
    else:
        text += "Никого не найдено. Нажмите «Сбросить поиск».\n"
    keyboard = []
    for agent in agents[page * page_size:(page + 1) * page_size]:
        keyboard.append([InlineKeyboardButton(
            text=f"👤 {agent['nick']}", callback_data=f"agview:{agent_hash(agent['nick'])}:{page}"
        )])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"agpage:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="agnoop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"agpage:{page + 1}"))
    keyboard.append(nav)
    keyboard.append([
        InlineKeyboardButton(text="🔎 Поиск", callback_data="agsearch"),
        InlineKeyboardButton(text="Сбросить поиск", callback_data="agreset")
    ])
    keyboard.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"agpage:{page}")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def agent_detail_markup(agent: Dict[str, Any], page: int):
    key = agent_hash(agent["nick"])
    text = (
        f"👤 <b>{html.escape(agent['nick'])}</b> [{html.escape(agent['rank'])}]\n"
        f"🎖️ FRP: <b>{agent['frp']}</b> — {html.escape(agent['frp_status'])}\n"
        f"🌟 Похвалы: <b>{agent['praise']}</b> — {html.escape(agent['praise_status'])}\n\n"
        "Выберите действие. Начисление и списание потребуют подтверждения:"
    )
    keyboard = [
        [InlineKeyboardButton(text="🎖️ FRP +1", callback_data=f"agask:g:f:{key}:1:{page}"),
         InlineKeyboardButton(text="🎖️ FRP +5", callback_data=f"agask:g:f:{key}:5:{page}"),
         InlineKeyboardButton(text="🎖️ FRP −1", callback_data=f"agask:d:f:{key}:1:{page}")],
        [InlineKeyboardButton(text="🌟 Похвала +1", callback_data=f"agask:g:p:{key}:1:{page}"),
         InlineKeyboardButton(text="🌟 Похвала +5", callback_data=f"agask:g:p:{key}:5:{page}"),
         InlineKeyboardButton(text="🌟 Похвала −1", callback_data=f"agask:d:p:{key}:1:{page}")]
    ]
    if agent.get("uid") is not None:
        keyboard.append([InlineKeyboardButton(text="🎖️ Изменить ранг", callback_data=f"agranks:{key}:{page}")])
    keyboard.append([InlineKeyboardButton(text="🗑️ Удалить запись (владелец)", callback_data=f"agdelete:{key}:{page}")])
    keyboard.append([InlineKeyboardButton(text="🔙 К списку", callback_data=f"agpage:{page}"),
                     InlineKeyboardButton(text="🔄 Обновить", callback_data=f"agview:{key}:{page}")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def agent_panel_list(message: types.Message, admin_id: int, page: int = 0, edit: bool = False):
    agents = await fetch_agent_directory()
    term = agent_search_terms.get(admin_id, "")
    if term:
        agents = [agent for agent in agents if term in agent["nick"].casefold()]
    text, kb = agent_list_markup(agents, page)
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def agent_panel_detail(message: types.Message, key: str, page: int):
    agents = await fetch_agent_directory()
    agent = next((item for item in agents if agent_hash(item["nick"]) == key), None)
    if not agent:
        await message.edit_text("⚠️ Агент больше не найден. Откройте список снова.")
        return
    text, kb = agent_detail_markup(agent, page)
    await message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("agents"))
@dp.message(F.text == "🧑‍💼 Управление агентами")
async def cmd_agent_panel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Панель доступна только руководству.")
        return
    await state.clear()
    agent_search_terms.pop(message.from_user.id, None)
    try:
        await agent_panel_list(message, message.from_user.id)
    except Exception as exc:
        logger.error(f"Agent directory error: {exc}")
        await message.answer("⚠️ Firebase не ответила. Попробуйте открыть панель позже.")


@dp.callback_query(F.data.startswith("agpage:") | (F.data == "agreset"))
async def agent_page(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if callback.data == "agreset":
        agent_search_terms.pop(callback.from_user.id, None)
        page = 0
    else:
        try:
            page = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка страницы", show_alert=True)
            return
    try:
        await agent_panel_list(callback.message, callback.from_user.id, page, edit=True)
        await callback.answer()
    except Exception as exc:
        logger.warning(f"Agent list unavailable: {exc}")
        await callback.answer("Ошибка загрузки Firebase", show_alert=True)


@dp.callback_query(F.data == "agnoop")
async def agent_noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "agsearch")
async def agent_search(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AgentPanelStates.waiting_for_search)
    await callback.message.answer("🔎 Введите часть ника агента. /cancel — отмена поиска.")
    await callback.answer()


@dp.message(AgentPanelStates.waiting_for_search)
async def agent_search_term(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    value = (message.text or "").strip().casefold()
    if not 1 <= len(value) <= 30:
        await message.answer("Введите от 1 до 30 символов.")
        return
    await state.clear()
    agent_search_terms[message.from_user.id] = value
    try:
        await agent_panel_list(message, message.from_user.id)
    except Exception as exc:
        logger.warning(f"Agent search unavailable: {exc}")
        await message.answer("⚠️ Ошибка загрузки Firebase.")


@dp.callback_query(F.data.startswith("agview:"))
async def agent_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, key, page_str = callback.data.split(":")
        await agent_panel_detail(callback.message, key, int(page_str))
        await callback.answer()
    except Exception as exc:
        logger.warning(f"Agent detail unavailable: {exc}")
        await callback.answer("Ошибка карточки агента", show_alert=True)


@dp.callback_query(F.data.startswith("agask:"))
async def agent_ask_action(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, kind, which, key, amount_raw, page_raw = callback.data.split(":")
        qty, page = int(amount_raw), int(page_raw)
        if kind not in ("g", "d") or which not in ("f", "p") or qty not in (1, 5):
            raise ValueError("Invalid action")
        if kind == "d" and qty != 1:
            raise ValueError("Invalid debit")
        agents = await fetch_agent_directory()
        agent = next((item for item in agents if agent_hash(item["nick"]) == key), None)
        if not agent:
            await callback.answer("Агент не найден", show_alert=True)
            return
        now = datetime.now(timezone.utc).timestamp()
        for token, task in list(agent_action_tokens.items()):
            if now - task["created"] > 600:
                agent_action_tokens.pop(token, None)
        token = uuid.uuid4().hex[:16]
        agent_action_tokens[token] = {
            "uid": callback.from_user.id, "chat_id": callback.message.chat.id,
            "nick": agent["nick"], "rank": agent["rank"],
            "kind": kind, "which": which, "qty": qty, "page": page,
            "key": key, "created": now, "status": "new"
        }
        operation = f"+{qty}" if kind == "g" else "−1"
        type_name = "FRP" if which == "f" else "похвалы"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Подтвердить {operation}", callback_data=f"agdo:{token}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"agview:{key}:{page}")]
        ])
        await callback.message.edit_text(
            f"⚠️ <b>Подтвердите действие</b>\n"
            f"👤 {html.escape(agent['nick'])}\n"
            f"{type_name}: <b>{operation}</b>\n"
            "После подтверждения баланс изменится в Firebase.",
            reply_markup=kb, parse_mode="HTML"
        )
        await callback.answer()
    except Exception as exc:
        logger.warning(f"Invalid agent action: {exc}")
        await callback.answer("Не удалось подготовить действие", show_alert=True)


@dp.callback_query(F.data.startswith("agdo:"))
async def agent_do_action(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    token = callback.data.removeprefix("agdo:")
    action = agent_action_tokens.get(token)
    if not action or action["uid"] != callback.from_user.id or action["chat_id"] != callback.message.chat.id:
        await callback.answer("Это действие уже недоступно. Откройте панель заново.", show_alert=True)
        return
    if action["status"] != "new" or datetime.now(timezone.utc).timestamp() - action["created"] > 600:
        await callback.answer("Действие уже обработано или устарело.", show_alert=True)
        return
    # Lock before first await so simultaneous callbacks cannot execute the same action twice.
    action["status"] = "processing"
    sheet = "fractionrp" if action["which"] == "f" else "praise"
    try:
        if action["kind"] == "g":
            await firebase_client.credit_report(
                sheet, action["nick"], action["rank"],
                f"Из админ-панели Telegram ID {callback.from_user.id}",
                "", "panel-" + token, quantity=action["qty"]
            )
        else:
            await firebase_client.debit_one(sheet, action["nick"])
        action["status"] = "done"
    except Exception as exc:
        # No blind retry after a possibly successful but timed-out write.
        action["status"] = "uncertain"
        logger.error(f"Agent panel operation {token} failed: {exc}")
        await callback.answer("Не подтверждено. Проверьте баланс, прежде чем повторять.", show_alert=True)
        return

    await callback.answer("✅ Баланс обновлён")
    try:
        await agent_panel_detail(callback.message, action["key"], action["page"])
    except Exception as exc:
        logger.warning(f"Agent card refresh failed after successful update: {exc}")
        await callback.message.edit_text("✅ Операция выполнена. Откройте /agents для проверки.")


@dp.callback_query(F.data.startswith("agdelete:"))
async def agent_delete_prompt(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Удаление доступно только владельцу.", show_alert=True)
        return
    try:
        _, key, page = callback.data.split(":")
        agent = next((x for x in await fetch_agent_directory()
                      if agent_hash(x["nick"]) == key), None)
        if not agent:
            await callback.answer("Агент не найден.", show_alert=True)
            return
        if int(agent["frp"]) != 0 or int(agent["praise"]) != 0:
            await callback.answer("Баланс не равен нулю. Удаление запрещено.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑️ Да, удалить", callback_data=f"agdeleteok:{key}:{page}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"agview:{key}:{page}")]
        ])
        await callback.message.edit_text(
            f"Удалить запись <b>{html.escape(agent['nick'])}</b> из бота и таблиц? "
            "Доступно только для нулевых балансов.",
            reply_markup=kb, parse_mode="HTML"
        )
        await callback.answer()
    except Exception as exc:
        logger.warning(f"Agent delete confirmation error: {exc}")
        await callback.answer("Не удалось подготовить удаление.", show_alert=True)


@dp.callback_query(F.data.startswith("agdeleteok:"))
async def agent_delete_confirm(callback: types.CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("Удаление доступно только владельцу.", show_alert=True)
        return
    try:
        _, key, page = callback.data.split(":")
        agent = next((x for x in await fetch_agent_directory()
                      if agent_hash(x["nick"]) == key), None)
        if not agent:
            await callback.answer("Запись уже отсутствует.", show_alert=True)
            return
        await remove_agent_everywhere(agent["nick"])
        await callback.answer("✅ Запись удалена.")
        await agent_panel_list(callback.message, callback.from_user.id, int(page), edit=True)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
    except Exception as exc:
        logger.error(f"Could not remove agent safely: {exc}")
        await callback.answer("Ошибка удаления. Обновите /agents.", show_alert=True)


@dp.callback_query(F.data.startswith("agranks:"))
async def agent_rank_menu(callback: types.CallbackQuery):
    if not is_leader(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, key, page = callback.data.split(":")
        agent = next((item for item in await fetch_agent_directory()
                      if agent_hash(item["nick"]) == key), None)
        if not agent or agent["uid"] is None:
            await callback.answer("Агент не зарегистрирован в боте", show_alert=True)
            return
        buttons = []
        for row in ((1, 2, 3, 4, 5), (6, 7, 8, 9, 10)):
            buttons.append([InlineKeyboardButton(
                text=str(rank), callback_data=f"agrank:{key}:{rank}:{page}"
            ) for rank in row])
        buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data=f"agview:{key}:{page}")])
        await callback.message.edit_text(
            f"🎖️ Выберите новый ранг для <b>{html.escape(agent['nick'])}</b>:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
        )
        await callback.answer()
    except Exception as exc:
        logger.warning(f"Rank menu failed: {exc}")
        await callback.answer("Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("agrank:"))
async def agent_set_rank(callback: types.CallbackQuery):
    if not is_leader(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, key, rank_raw, page_raw = callback.data.split(":")
        rank = int(rank_raw)
        if not 1 <= rank <= 10:
            raise ValueError("Invalid rank")
        agent = next((item for item in await fetch_agent_directory()
                      if agent_hash(item["nick"]) == key), None)
        if not agent or agent["uid"] is None or agent["uid"] not in users_db:
            await callback.answer("Агент не найден", show_alert=True)
            return
        users_db[agent["uid"]]["rank"] = "X" if rank == 10 else str(rank)
        save_json(USERS_FILE, users_db)
        await sync_state_to_cloud()
        await callback.answer("✅ Ранг обновлён")
        await agent_panel_detail(callback.message, key, int(page_raw))
    except Exception as exc:
        logger.warning(f"Rank change failed: {exc}")
        await callback.answer("⚠️ Не удалось обновить ранг", show_alert=True)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_menu(message.from_user.id))

# Очередь на выдачу
def render_queue(rows: List[Dict[str, Any]]):
    pending = [r for r in rows if int(r.get("qty") or 0) > 0]
    if not pending:
        return "🎉 Очередь пуста! Все FRP выданы.", None

    text = f"📋 <b>Очередь на выдачу (/fractionrp) — {len(pending)} чел.:</b>\n\n"
    buttons = []
    for i, r in enumerate(pending[:15], 1):
        nick = str(r.get("nick", "Noname"))
        qty = int(r.get("qty") or 0)
        rank = str(r.get("rank", ""))
        rank_text = f" [{rank}]" if rank else ""
        text += (f"{i}. <b>{html.escape(nick)}</b>{html.escape(rank_text)} — <b>{qty} шт.</b>\n"
                 f"Команда: <code>/fractionrp {html.escape(nick)}</code>\n\n")
        buttons.append([InlineKeyboardButton(text=f"✓ Выдать 1 для {nick}", callback_data=f"issue_1_{nick}")])
    if len(pending) > 15:
        text += f"…Ещё {len(pending) - 15} игроков.\n"
    text += "<i>Список обновляется после каждого списания.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text == "📋 Очередь на выдачу (Админ)")
@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступно только руководству.")
        return
    try:
        rows = await firebase_client.get_sheet("fractionrp")
        text, keyboard = render_queue(rows)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Could not load issue queue: {e}")
        await message.answer("⚠️ Не удалось загрузить очередь из Firebase.")


@dp.callback_query(F.data.startswith("issue_1_"))
async def process_issue_one(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    nick = callback.data[len("issue_1_"):]
    try:
        remaining = await firebase_client.debit_one("fractionrp", nick)
        await callback.answer(
            f"✓ {nick}: осталось {remaining}" if remaining else f"{nick}: баллы закончились",
            show_alert=False
        )
        # Refresh original message; zero-quantity rows disappear completely.
        rows = await firebase_client.get_sheet("fractionrp")
        new_text, new_keyboard = render_queue(rows)
        await callback.message.edit_text(new_text, reply_markup=new_keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error issuing point: {e}")
        await callback.answer("⚠️ Списание не подтверждено. Обновите /queue.", show_alert=True)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  СЛУЖБА ПОДДЕРЖКИ И БАГ-ТРЕКЕР В БОТЕ                            ║
# ╚══════════════════════════════════════════════════════════════════╝
@dp.message(F.text == "💬 Поддержка / Баг-репорт")
@dp.message(Command("bug"))
@dp.message(Command("support"))
async def cmd_support(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_support")]
    ])
    await message.answer(
        "💬 <b>Служба поддержки и баг-трекер:</b>\n\n"
        "Опишите найденный баг, ошибку или предложение следующим сообщением.\n"
        "<i>(Вы можете отправить текст или прикрепить скриншот — лидер получит ваше сообщение)</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(SupportStates.waiting_for_message)

@dp.callback_query(F.data == "cancel_support")
async def cancel_support(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отправка обращения отменена.")

@dp.message(SupportStates.waiting_for_message)
async def process_support_msg(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    u_info = users_db.get(str(uid), {})
    nick = u_info.get("nick", "Аноним")
    rank = u_info.get("rank", "?")
    username_str = f"(@{message.from_user.username})" if message.from_user.username else ""

    content_text = message.text or message.caption or "[Вложение без текста]"

    admin_msg = (
        "🚨 <b>Новое обращение в Службу Поддержки!</b>\n\n"
        f"👤 <b>От:</b> <code>{html.escape(nick)}</code> [{html.escape(str(rank))}]\n"
        f"🆔 <b>Telegram ID:</b> <code>{uid}</code> {html.escape(username_str)}\n"
        f"📝 <b>Сообщение:</b>\n{html.escape(content_text)}"
    )

    # Сохраняем в коллекцию support_tickets в Firestore
    ticket_payload = {
        "nick": nick,
        "contact": f"Telegram: @{message.from_user.username}" if message.from_user.username else f"TG ID: {uid}",
        "category": "Обращение из Telegram",
        "description": content_text,
        "screenshot": "",
        "photo_id": message.photo[-1].file_id if message.photo else "",
        "status": "Новое",
        "source": "telegram"
    }
    ticket_id = await firebase_client.add_support_ticket(ticket_payload)
    ticket_label = f"#{ticket_id[-6:]}" if ticket_id else ""

    admin_msg_with_id = (
        f"🚨 <b>Новое обращение в Службу Поддержки {ticket_label}!</b>\n\n"
        f"👤 <b>От:</b> <code>{html.escape(nick)}</code> [{html.escape(str(rank))}]\n"
        f"🆔 <b>Telegram ID:</b> <code>{uid}</code> {html.escape(username_str)}\n"
        f"📝 <b>Сообщение:</b>\n{html.escape(content_text)}\n\n"
        "🌐 <i>Также обращение доступно на сайте в разделе «Обращения»</i>"
    )

    recipients = list(set(owner_cfg.get("owner_ids", []) + admins_db))
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID.lstrip("-").isdigit():
        recipients.append(int(ADMIN_CHAT_ID))
    recipients = list(set(recipients))

    for adm_id in recipients:
        try:
            if message.photo:
                await bot.send_photo(adm_id, message.photo[-1].file_id, caption=admin_msg_with_id, parse_mode="HTML")
            else:
                await bot.send_message(adm_id, admin_msg_with_id, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not forward support message to {adm_id}: {e}")

    await message.answer(
        "✅ <b>Ваше обращение успешно зарегистрировано и передано руководству!</b>\n\n"
        "Оно добавлено в общую панель обращений и будет проверено лидером/тех. администратором. Спасибо за содействие!",
        reply_markup=get_main_menu(uid),
        parse_mode="HTML"
    )

# ╔══════════════════════════════════════════════════════════════════╗
# ║  DUMMY WEB SERVER ДЛЯ RENDER (STAY-ALIVE)                        ║
# ╚══════════════════════════════════════════════════════════════════╝
async def handle_ping(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    }
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=headers)
    return web.json_response({"status": "online"}, headers={**headers, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

_support_rate_history: Dict[str, List[float]] = {}
_support_global_attempts: List[float] = []


def safe_support_url(value: str) -> bool:
    if not value:
        return True
    try:
        u = urlsplit(value)
        return u.scheme in ("http", "https") and bool(u.hostname) and not u.username and not u.password
    except ValueError:
        return False

async def handle_api_support(request):
    origin = request.headers.get("Origin", "")
    headers = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if origin == "https://qwertyozzi.github.io":
        headers["Access-Control-Allow-Origin"] = origin
    if request.method == "OPTIONS":
        return web.Response(status=204 if origin == "https://qwertyozzi.github.io" else 403, headers=headers)

    client_ip = request.remote or "unknown"
    now = datetime.now(timezone.utc).timestamp()
    # Small bounded in-process limiter; add provider-level rate limiting for multi-instance hosting.
    for ip, timestamps in list(_support_rate_history.items()):
        recent = [t for t in timestamps if now - t < 60]
        if recent:
            _support_rate_history[ip] = recent
        else:
            del _support_rate_history[ip]
    _support_global_attempts[:] = [t for t in _support_global_attempts if now - t < 60]
    if len(_support_global_attempts) >= 100:
        return web.json_response({"error": "Temporarily unavailable"}, status=429, headers=headers)
    attempts = _support_rate_history.setdefault(client_ip, [])
    if len(attempts) >= 5:
        return web.json_response({"error": "Too many requests"}, status=429, headers=headers)
    attempts.append(now)
    _support_global_attempts.append(now)
    if request.content_type != "application/json":
        return web.json_response({"error": "JSON required"}, status=415, headers=headers)
    if request.content_length is None or request.content_length > 12000:
        return web.json_response({"error": "Invalid request size"}, status=413, headers=headers)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return web.json_response({"error": "Invalid payload"}, status=400, headers=headers)
        nick = str(data.get("nick", "Аноним")).strip()
        contact = str(data.get("contact", "Не указан")).strip()
        category = str(data.get("category", "Баг / Ошибка")).strip()
        description = str(data.get("description", "")).strip()
        screenshot = str(data.get("screenshot", "")).strip()

        if not nick or not description or len(nick) > 80 or len(description) > 4000 or len(contact) > 200 or len(category) > 80 or len(screenshot) > 1000 or not safe_support_url(screenshot):
            return web.json_response({"error": "Missing nick or description"}, status=400, headers=headers)

        ticket_payload = {
            "nick": nick,
            "contact": contact,
            "category": category,
            "description": description,
            "screenshot": screenshot,
            "status": "Новое",
            "source": "site"
        }
        ticket_id = await firebase_client.add_support_ticket(ticket_payload)
        if not ticket_id:
            return web.json_response({"error": "Ticket storage unavailable"}, status=503, headers=headers)

        # Мгновенно уведомляем руководство в Telegram
        ticket_label = f"#{ticket_id[-6:]}" if ticket_id else ""
        admin_msg = (
            f"🌐 <b>Новое обращение с САЙТА в Службу Поддержки {ticket_label}!</b>\n\n"
            f"👤 <b>Никнейм:</b> <code>{html.escape(nick)}</code>\n"
            f"📞 <b>Контакт:</b> {html.escape(contact)}\n"
            f"🏷️ <b>Категория:</b> {html.escape(category)}\n"
            f"📝 <b>Описание:</b>\n{html.escape(description)}"
        )
        if screenshot:
            admin_msg += f"\n\n🔗 <b>Скриншот:</b> {html.escape(screenshot)}"

        admin_msg += "\n\n🌐 <i>Обращение сохранено в базе и доступно на сайте</i>"

        recipients = list(set(owner_cfg.get("owner_ids", []) + admins_db))
        if ADMIN_CHAT_ID and ADMIN_CHAT_ID.lstrip("-").isdigit():
            recipients.append(int(ADMIN_CHAT_ID))
        recipients = list(set(recipients))

        for adm_id in recipients:
            try:
                await bot.send_message(adm_id, admin_msg, disable_web_page_preview=True, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Could not forward web support ticket to {adm_id}: {e}")

        return web.json_response({"status": "ok", "id": ticket_id}, headers=headers)
    except Exception as e:
        logger.error(f"Error handling /api/support: {e}")
        return web.json_response({"error": "Internal server error"}, status=500, headers=headers)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_post("/api/support", handle_api_support)
    app.router.add_options("/api/support", handle_api_support)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web health check server started on port {PORT}")

# ╔══════════════════════════════════════════════════════════════════╗
# ║  MAIN ENTRY POINT                                               ║
# ╚══════════════════════════════════════════════════════════════════╝
async def main():
    if not BOT_TOKEN or not FIREBASE_PASSWORD or not FIREBASE_API_KEY:
        logger.error("Required credentials are not configured!")
        return

    # Render must see an open HTTP port even when Firebase is responding slowly.
    await start_web_server()

    # Do not let an unavailable Firestore dependency block the entire deployment.
    try:
        await asyncio.wait_for(sync_state_from_cloud(), timeout=20)
    except asyncio.TimeoutError:
        logger.error("Initial Firebase sync timed out after 20 seconds")

    # One-time cleanup requested by the owner. Track completion in cloud state
    # so a future user who legitimately chooses this nick is not deleted again.
    migration = "remove-accidental-Samuel_Norton_10-v1"
    if _cloud_state_hydrated and migration not in completed_cleanup_ids:
        try:
            if await remove_agent_everywhere("Samuel_Norton_10"):
                completed_cleanup_ids.add(migration)
                await sync_state_to_cloud()
                logger.info("Accidental duplicate cleanup completed")
        except Exception as exc:
            logger.warning(f"Could not clean up accidental duplicate: {exc}")

    logger.info("Bot polling starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
