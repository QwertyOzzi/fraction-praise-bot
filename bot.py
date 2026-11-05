import asyncio
import os
import json
import html
import logging
import uuid
import hashlib
import hmac
import re
import difflib
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, timedelta

from aiohttp import web, ClientSession, ClientTimeout
from urllib.parse import urlsplit
from dotenv import load_dotenv
from profile_sheets import register_profile, resolve_nickname_claim
from natural_commands import (interpret as interpret_natural, resolve_names,
                              looks_like_operation, simple_parse, ParseError, MODEL)
from natural_balance import apply_batch, BalanceConflict
from natural_aliases import load_alias_memory, resolve_with_memory, AliasMemoryError
from natural_commands import simplified
from agent_actions import (
    AgentOperationError, commit_agent_debit, create_add_request, get_add_request,
    decide_add_request, list_collection, get_announcement,
    create_announcement, finish_announcement
)
from newslog_view import (
    NEWSLOG_FETCH_LIMIT, newslog_list, newslog_detail, news_date,
    text_size_utf16, split_telegram_text
)
from personnel import personnel_details, ranked_reporters
from fbi_schedule import (
    ScheduleError, TZ_NAME, prepare_schedule, parse_ranks, parse_moscow_date,
    moscow_label, current_rank, BLOCKED_STATUSES, list_active, get_schedule,
    create_schedule, cancel_schedule, display_schedule, scheduler_loop,
    scheduled_tick, utcnow
)
from access_control import (PERMISSIONS, AGENT_DELEGATABLE,
    normalize_overrides, sign_overrides, has_permission as resolve_permission,
    max_debit_for)
from telegram_design import (send_fbi_card, send_home_banner_card,
                             edit_fbi_card, hide_old_reply_keyboard,
                             with_home_button)

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
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
    InputMediaPhoto,
    InputMediaVideo,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    MenuButtonCommands,
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

# Owner-selected Telegram photo, stored in signed Firebase bot state.
MENU_BANNER_FILE_ID = ""

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
permission_overrides: Dict[str, Dict[str, bool]] = {}
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


def permission_secret() -> str:
    return os.getenv("ADMIN_ACL_SIGNING_KEY", "").strip() or BOT_TOKEN


def has_permission(user_id: int, permission: str) -> bool:
    return resolve_permission(user_id, permission, owners=OWNER_TELEGRAM_IDS,
        admins=admins_db, roles=admin_roles, overrides=permission_overrides,
        hydrated=_cloud_state_hydrated)


def debit_limit(user_id: int) -> int:
    return max_debit_for(user_id, owners=OWNER_TELEGRAM_IDS,
        admins=admins_db, roles=admin_roles, overrides=permission_overrides,
        hydrated=_cloud_state_hydrated)


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
                "proof": proof_data,
                "profileId": mv.get("profileId", {}).get("stringValue", "")
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
                    **({"profileId": {"stringValue": str(r["profileId"])}}
                       if r.get("profileId") else {}),
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
                "video_id": {"stringValue": str(ticket_data.get("video_id", ""))},
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
    global _cloud_state_hydrated, _pending_legacy_nonce, MENU_BANNER_FILE_ID
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
                    # Treat the cloud profile list as authoritative. Merging stale
                    # local JSON resurrected agents deleted from /agents after restart.
                    restored_users = {str(uid): entry for uid, entry in cloud_users.items()
                                      if isinstance(entry, dict)}
                    if len(restored_users) != len(cloud_users):
                        raise RuntimeError("Invalid Firebase user profiles")

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
                    # Additional, separately signed capabilities do not alter the
                    # existing admins/admin_roles signature or revoke old grants.
                    cloud_permissions = normalize_overrides(
                        cloud_data.get("permission_overrides", {}))
                    permission_signature = str(cloud_data.get("permission_overrides_signature") or "")
                    expected_permissions = sign_overrides(cloud_permissions, permission_secret())
                    if ((cloud_permissions and not permission_signature) or
                            (permission_signature and not hmac.compare_digest(
                                permission_signature, expected_permissions))):
                        logger.error("Permission override signature invalid; refusing state hydration")
                        return
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
                    # Hydrate the banner only after validating the cloud ACL.
                    saved_banner = cloud_data.get("menu_banner_file_id", "")
                    MENU_BANNER_FILE_ID = (
                        saved_banner if isinstance(saved_banner, str)
                        and len(saved_banner) <= 256 else ""
                    )
                    # Apply profiles only after the cloud administrator signature is verified.
                    users_db.clear()
                    users_db.update(restored_users)
                    # The authenticated, signed cloud ACL is authoritative.
                    # Do not merge potentially stale local admin JSON into it:
                    # doing so could resurrect a previously revoked administrator.
                    admins_db[:] = filtered
                    admin_roles.clear()
                    admin_roles.update(filtered_roles)
                    permission_overrides.clear()
                    permission_overrides.update(cloud_permissions)
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
                    permission_overrides.clear()
                    MENU_BANNER_FILE_ID = ""
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
                "permission_overrides": permission_overrides,
                "permission_overrides_signature": sign_overrides(permission_overrides, permission_secret()),
                "completed_cleanup_ids": sorted(completed_cleanup_ids),
                "owners": owner_cfg.get("owner_ids", []),
                "requests": requests_db,
                "menu_banner_file_id": MENU_BANNER_FILE_ID
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


# Serialize evidence updates per Telegram conversation. Aiogram may dispatch
# all items of an album concurrently, even before the FSM state changes.
REPORT_MEDIA_LIMIT = 10
_report_media_locks: Dict[tuple[int, int], asyncio.Lock] = {}

class GrantStates(StatesGroup):
    waiting_for_details = State()


class AiStates(StatesGroup):
    waiting_for_text = State()


class AgentActionStates(StatesGroup):
    waiting_add_evidence = State()
    confirm_add = State()


class NewsStates(StatesGroup):
    waiting_for_text = State()
    confirm = State()

class ScheduleStates(StatesGroup):
    waiting_for_fields = State()
    confirm = State()


class MenuBannerStates(StatesGroup):
    waiting_for_photo = State()


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

def get_main_menu(user_id: int, *, admin_panel: bool = False) -> InlineKeyboardMarkup:
    """Compact inline navigation. Sensitive actions still check access server-side."""
    profile = users_db.get(str(user_id), {})
    claim_status = profile.get("verification_status") if isinstance(profile, dict) else ""
    if claim_status in ("pending", "needs_proof", "proof_submitted", "disconnected", "retry_ready", "cooldown"):
        buttons = [
            [InlineKeyboardButton(text="🕒 Проверка профиля", callback_data="main:claim_status")],
            [InlineKeyboardButton(text="⚙️ Указать другой ник", callback_data="main:nick")],
            [InlineKeyboardButton(text="💬 Поддержка", callback_data="main:support")],
        ]
    elif admin_panel and is_admin(user_id):
        buttons = [
            [InlineKeyboardButton(text="🧑‍💼 Агенты", callback_data="main:agents", style="primary"),
             InlineKeyboardButton(text="📋 Очередь", callback_data="main:queue")],
            [InlineKeyboardButton(text="📌 Заявки", callback_data="main:pending"),
             InlineKeyboardButton(text="➕ Начисления", callback_data="main:grant")],
            [InlineKeyboardButton(text="👥 Руководство", callback_data="main:admins")],
            [InlineKeyboardButton(text="📊 Статистика отчётов", callback_data="main:stats")],
            [InlineKeyboardButton(text="📅 Мероприятия", callback_data="main:events"),
             InlineKeyboardButton(text="📨 Авторассылка", callback_data="main:autopost")],
            [InlineKeyboardButton(text="📥 Запросы агентов", callback_data="main:agentpending")],
            [InlineKeyboardButton(text="📜 Журнал агентов", callback_data="main:agentlogs")],
            [InlineKeyboardButton(text="🤖 ИИ-команда", callback_data="main:ai", style="primary")],
            *([[InlineKeyboardButton(text="📣 Новости для всех", callback_data="main:news")]]
              if is_leader(user_id) else []),
            [InlineKeyboardButton(text="◀️ Личный кабинет", callback_data="main:home")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton(text="📝 Подать отчёт", callback_data="main:report", style="primary"),
             InlineKeyboardButton(text="📊 Мой статус", callback_data="main:status", style="primary")],
            [InlineKeyboardButton(text="⚙️ Никнейм", callback_data="main:nick"),
             InlineKeyboardButton(text="🎖️ Ранг", callback_data="main:rank")],
            [InlineKeyboardButton(text="👤 Моя карточка", callback_data="main:profile")],
            [InlineKeyboardButton(text="📅 Мероприятия FBI", callback_data="main:events")],
            [InlineKeyboardButton(text="➖ Списать FRP / похвалу", callback_data="main:min")],
            [InlineKeyboardButton(text="➕ Запросить начисление", callback_data="main:plus")],
            [InlineKeyboardButton(text="💬 Поддержка", callback_data="main:support")],
            [InlineKeyboardButton(text="🌐 Таблица", web_app=WebAppInfo(url=WEBAPP_URL))],
        ]
        if is_admin(user_id):
            buttons.append([InlineKeyboardButton(text="👑 Панель руководства", callback_data="main:admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def pending_claim_card_text(profile: Dict[str, Any]) -> str:
    """A non-binding nickname claim never represents another agent's account."""
    nick = html.escape(str(profile.get("claimed_nick") or
                           profile.get("disconnected_nick") or "Не указан"))
    status = profile.get("verification_status")
    if status == "needs_proof":
        note = "Руководство запросило доказательства. Отправьте скриншот игрового профиля сюда."
    elif status == "proof_submitted":
        note = "Доказательства получены. Ожидайте решения руководства."
    elif status == "disconnected":
        note = "Этот ник переназначен по решению руководства. Вы можете указать другой."
    elif status == "retry_ready":
        note = "Ограничение закончилось. Можно повторно указать ник и ранг."
    elif status == "cooldown":
        note = "Временное ограничение после отказа руководства."
    else:
        note = "Вы вошли в бот. Ник ожидает проверки руководством."
    previous_nick = html.escape(str(profile.get("nick") or ""))
    return (
        "🛡️ <b>FBI · Проверка регистрации</b>\n\n"
        f"👤 <b>Заявленный ник:</b> <code>{nick}</code>\n"
        f"🎖️ <b>Ранг:</b> {html.escape(str(profile.get('claimed_rank') or profile.get('rank') or '?'))}\n"
        + (f"📌 <b>Прежний ник:</b> <code>{previous_nick}</code>\n" if previous_nick else "")
        + f"\n<i>{note}</i>\n\n"
        + "До подтверждения чужие балансы и отчёты недоступны."
    )


def home_card_text(profile: Dict[str, Any], user_id: int) -> str:
    """Render user-provided values with HTML escaping."""
    nick = html.escape(str(profile.get("nick", "Не указан")))
    rank = html.escape(str(profile.get("rank", "?")))
    role = html.escape(role_name(user_id))
    return (
        "🛡️ <b>FBI · Личный кабинет</b>\n\n"
        f"👤 <b>Агент:</b> <code>{nick}</code>\n"
        f"🎖️ <b>Ранг:</b> {rank}\n"
        f"🔐 <b>Доступ:</b> {role}\n\n"
        "<i>Команды всегда доступны через «Меню» у поля ввода.</i>"
    )


# These labels appear in the native Telegram "Меню" button beside chat input.
# Universal commands are shown to everyone. Leadership commands are scoped to
# users with current owner-approved Telegram ACL; display does not grant access.
PUBLIC_BOT_COMMANDS = [
    BotCommand(command="start", description="Открыть личный кабинет"),
    BotCommand(command="menu", description="Показать главное меню внизу чата"),
    BotCommand(command="report", description="Подать отчёт FRP / похвала"),
    BotCommand(command="status", description="Мои FRP и похвалы"),
    BotCommand(command="profile", description="Полная карточка сотрудника"),
    BotCommand(command="events", description="Предстоящие мероприятия FBI"),
    BotCommand(command="eventnotify", description="Вкл/выкл уведомления о МП"),
    BotCommand(command="nick", description="Изменить игровой ник"),
    BotCommand(command="rank", description="Запросить смену ранга"),
    BotCommand(command="min", description="Списать FRP/похвалу агенту"),
    BotCommand(command="plus", description="Запросить начисление с причиной"),
    BotCommand(command="support", description="Поддержка и баг-репорт"),
    BotCommand(command="help", description="Помощь и разделы бота"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]
ADMIN_BOT_COMMANDS = [
    BotCommand(command="agents", description="Управление агентами"),
    BotCommand(command="stats", description="Статистика отчётов агентов"),
    BotCommand(command="event", description="Создать мероприятие FBI"),
    BotCommand(command="autopost", description="Отложенная рассылка по рангам"),
    BotCommand(command="schedule", description="Управление расписанием"),
    BotCommand(command="department", description="Назначить отдел сотруднику"),
    BotCommand(command="pending", description="Ожидающие заявки"),
    BotCommand(command="queue", description="Очередь на выдачу"),
    BotCommand(command="grant", description="Начислить FRP / похвалы"),
    BotCommand(command="admins", description="Список руководства"),
    BotCommand(command="claims", description="Проверка спорных регистраций"),
    BotCommand(command="ai", description="ИИ-команды: начисление и списание FRP"),
    BotCommand(command="agentpending", description="Заявки агентов на начисление"),
    BotCommand(command="agentlogs", description="Отдельный журнал действий агентов"),
]
OWNER_BOT_COMMANDS = [
    BotCommand(command="security", description="Проверить права доступа"),
    BotCommand(command="notifications", description="Уведомления руководства"),
    BotCommand(command="setbanner", description="Установить баннер главного меню"),
    BotCommand(command="removebanner", description="Убрать баннер из меню"),
    BotCommand(command="permissions", description="Управление правами сотрудников"),
]


def commands_for_user(user_id: int) -> List[BotCommand]:
    commands = list(PUBLIC_BOT_COMMANDS)
    if is_admin(user_id):
        commands.extend(ADMIN_BOT_COMMANDS)
    if is_leader(user_id):
        commands.append(BotCommand(command="news", description="Новости и обновления для всех"))
        commands.append(BotCommand(command="newslog", description="История рассылок"))
    if is_owner(user_id):
        commands.extend(OWNER_BOT_COMMANDS)
    return commands


async def sync_user_command_scope(user_id: int) -> None:
    """Reflect confirmed role changes in Telegram's native slash menu.

    ACL checks remain in handlers. Removing a user drops their custom menu.
    """
    if type(user_id) is not int or user_id <= 0:
        return
    try:
        scope = BotCommandScopeChat(chat_id=user_id)
        if is_admin(user_id):
            await bot.set_my_commands(commands_for_user(user_id), scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except Exception as exc:
        logger.warning("Telegram command scope update failed: %s", type(exc).__name__)


async def register_native_command_menu() -> None:
    """Set the persistent Telegram Menu control for the bot."""
    try:
        await bot.set_my_commands(PUBLIC_BOT_COMMANDS,
                                  scope=BotCommandScopeDefault())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as exc:
        logger.warning("Could not register the Telegram native menu: %s",
                       type(exc).__name__)
    # Admin permissions have already been hydrated from Firebase at this point.
    known = OWNER_TELEGRAM_IDS | (set(admins_db) if _cloud_state_hydrated else set())
    for uid in sorted(uid for uid in known if type(uid) is int and uid > 0):
        await sync_user_command_scope(uid)


# ╔══════════════════════════════════════════════════════════════════╗
# ║  AIOGRAM DISPATCHER                                              ║
# ╚══════════════════════════════════════════════════════════════════╝
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


CLAIM_COOLDOWN_SECONDS = 5 * 60


def claim_cooldown_remaining(profile: Dict[str, Any]) -> int:
    """Remaining cooldown seconds; historical permanent blocks are retryable."""
    timestamp = profile.get("cooldown_until")
    if not isinstance(timestamp, str) or not timestamp:
        return 0
    try:
        until = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if until.tzinfo is None:
            return 0
        return max(0, int((until - datetime.now(timezone.utc)).total_seconds() + 0.999))
    except ValueError:
        return 0


async def finish_expired_claim_cooldown(user_id: str) -> bool:
    """Release only the rejected claim, not any verified admin permissions."""
    async with _claim_action_locks.setdefault(user_id, asyncio.Lock()):
        profile = users_db.get(user_id)
        if not isinstance(profile, dict):
            return True
        if profile.get("verification_status") not in ("disabled", "cooldown"):
            return True
        if claim_cooldown_remaining(profile):
            return False
        previous = dict(profile)
        updated = dict(profile)
        updated["verification_status"] = "retry_ready"
        updated.pop("cooldown_until", None)
        updated.pop("claim_id", None)
        for key in ("proof_submitted", "proof_kind", "proof_file_id",
                    "proof_link", "proof_caption"):
            updated.pop(key, None)
        users_db[user_id] = updated
        if not await sync_state_to_cloud():
            users_db[user_id] = previous
            return False
        save_json(USERS_FILE, users_db)
        return True


class DisabledClaimMiddleware(BaseMiddleware):
    """Allow a new attempt after five minutes; old disabled users recover too."""
    async def __call__(self, handler, event, data):
        sender = getattr(event, "from_user", None)
        profile = users_db.get(str(sender.id)) if sender else None
        if isinstance(profile, dict) and (
                profile.get("verification_status") == "disabled"
                or profile.get("verification_status") == "cooldown"):
            remaining = claim_cooldown_remaining(profile)
            if remaining > 0:
                minutes, seconds = divmod(remaining, 60)
                text = (f"⏳ Заявка отклонена. Повторно попробовать можно через "
                        f"{minutes}:{seconds:02d}.")
            elif await finish_expired_claim_cooldown(str(sender.id)):
                return await handler(event, data)
            else:
                text = "⚠️ Не удалось сохранить окончание блокировки. Повторите позже."
            try:
                if isinstance(event, types.CallbackQuery):
                    await event.answer(text, show_alert=True)
                else:
                    await event.answer(text)
            except Exception as exc:
                logger.info("Could not notify cooldown status: %s", type(exc).__name__)
            return None
        return await handler(event, data)


dp.message.outer_middleware(DisabledClaimMiddleware())
dp.callback_query.outer_middleware(DisabledClaimMiddleware())


async def record_admin_activity(action: str, actor_id: int, details: str, sheet: str = "") -> None:
    """Best-effort audit log and optional owner-only Telegram notification."""
    if not is_admin(actor_id):
        return
    actor_role = role_name(actor_id)
    profile = users_db.get(str(actor_id), {})
    nick = str(profile.get("nick") or "Не указан")[:70] if isinstance(profile, dict) else "Не указан"
    username = str(profile.get("telegram_username") or "")[:40] if isinstance(profile, dict) else ""
    actor = f"{actor_role}: {nick}" + (f" (@{username.lstrip('@')})" if username else "") + f" (Telegram ID {actor_id})"
    message = str(details)[:500]
    try:
        token = await firebase_client.get_token()
        if token:
            url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/audit_log"
            body = {"fields": {
                "source": {"stringValue": "telegram"},
                "action": {"stringValue": str(action)[:55]},
                "actor": {"stringValue": actor},
                "actorTelegramId": {"integerValue": str(actor_id)},
                "details": {"stringValue": message},
                "sheet": {"stringValue": str(sheet)[:30]},
                "createdAt": {"timestampValue": datetime.now(timezone.utc).isoformat()}
            }}
            async with ClientSession(timeout=ClientTimeout(total=8)) as session:
                async with session.post(url, json=body, headers={"Authorization": f"Bearer {token}"}) as resp:
                    if resp.status != 200:
                        logger.warning("Audit event write failed (HTTP %s)", resp.status)
    except Exception as exc:
        logger.warning("Audit event logging failed: %s", type(exc).__name__)

    if is_owner(actor_id):
        return
    enabled = True
    try:
        token = await firebase_client.get_token()
        if token:
            url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/settings"
            async with ClientSession(timeout=ClientTimeout(total=6)) as session:
                async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
                    if resp.status == 200:
                        settings = (await resp.json()).get("fields", {})
                        enabled = settings.get("auditTelegramEnabled", {}).get("booleanValue", True) is not False
    except Exception as exc:
        logger.warning("Could not read owner notification setting: %s", type(exc).__name__)
    if not enabled:
        return

    alert = f"🔔 Действие руководства\n{actor}\n{action}: {message}"
    for owner_id in OWNER_TELEGRAM_IDS:
        try:
            await bot.send_message(owner_id, alert[:4000], parse_mode=None)
        except Exception as exc:
            logger.warning("Owner audit notification failed: %s", type(exc).__name__)


async def record_profile_activity(actor_id: int, action: str, old_nick: str,
                                  new_nick: str, rank: str, username: str = "") -> None:
    """Audit verified Telegram profile changes without granting admin privileges."""
    try:
        token = await firebase_client.get_token()
        if not token:
            logger.warning("Profile audit skipped: Firebase not authenticated")
            return
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/audit_log"
        actor = f"{new_nick[:65]}" + (f" (@{username[:32]})" if username else "") + f" (Telegram ID {actor_id})"
        details = f"Ник: {old_nick or '(новый профиль)'} → {new_nick}; ранг: {rank}"[:500]
        body = {"fields": {
            "source": {"stringValue": "telegram"},
            "action": {"stringValue": action},
            "actor": {"stringValue": actor},
            "actorTelegramId": {"integerValue": str(actor_id)},
            "details": {"stringValue": details},
            "sheet": {"stringValue": ""},
            "createdAt": {"timestampValue": datetime.now(timezone.utc).isoformat()}
        }}
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            async with session.post(url, json=body, headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status != 200:
                    logger.warning("Profile audit write failed (HTTP %s)", response.status)
    except Exception as exc:
        logger.warning("Profile audit delivery failed: %s", type(exc).__name__)


CLAIM_ACTIVE_STATES = ("pending", "needs_proof", "proof_submitted")
_claim_action_locks: Dict[str, asyncio.Lock] = {}


def claim_review_keyboard(uid: str, claim_id: str) -> InlineKeyboardMarkup:
    controls = [
        [InlineKeyboardButton(style="success", text="✅ Оставить",
                              callback_data=f"claim:keep:{uid}:{claim_id}")],
        [InlineKeyboardButton(style="danger", text="⏳ Отклонить (5 мин.)",
                              callback_data=f"claim:disable:{uid}:{claim_id}")],
        [InlineKeyboardButton(style="primary", text="📎 Запросить доказательства",
                              callback_data=f"claim:proof:{uid}:{claim_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=controls)


def claim_review_text(uid: str, profile: Dict[str, Any]) -> str:
    name = html.escape(str(profile.get("claimed_nick") or "Не указан"))
    rank = html.escape(str(profile.get("claimed_rank") or profile.get("rank") or "?"))
    old_nick = html.escape(str(profile.get("nick") or ""))
    username = html.escape(str(profile.get("telegram_username") or "").lstrip("@"))
    holders = [
        (key, entry) for key, entry in users_db.items()
        if key != uid and isinstance(entry, dict) and
        str(entry.get("nick") or "").casefold() == str(profile.get("claimed_nick") or "").casefold()
    ]
    holder_note = ("\n⚠️ <b>Уже привязан к Telegram ID:</b> "
                   + ", ".join(f"<code>{html.escape(key)}</code>" for key, _ in holders)
                   if holders else "\n⚠️ Такой ник уже встречается в таблице.")
    rank_only = (
        bool(profile.get("nick"))
        and str(profile.get("nick") or "").casefold()
            == str(profile.get("claimed_nick") or "").casefold()
        and str(profile.get("claimed_rank") or profile.get("rank") or "")
            != str(profile.get("rank") or "")
    )
    return (
        ("🎖️ <b>FBI · Запрос смены ранга</b>\n\n" if rank_only
         else "🔎 <b>FBI · Проверка регистрации</b>\n\n")
        + f"👤 <b>Новый пользователь:</b> <code>{uid}</code>\n"
        f"🎮 <b>Ник:</b> <code>{name}</code>\n"
        f"🎖️ <b>Заявленный ранг:</b> {rank}\n"
        + (f"📌 <b>Старый ник пользователя:</b> <code>{old_nick}</code>\n" if old_nick else "")
        + (f"💬 <b>Telegram:</b> @{username}\n" if username else "")
        + (f"🎖️ <b>Текущий ранг:</b> {html.escape(str(profile.get('rank') or '?'))}\n"
           if rank_only else holder_note)
        + "\n\n"
        + ("Подтвердите повышение или понижение ранга. Балансы не меняются."
           if rank_only else
           "Проверьте, действительно ли агент владеет игровым аккаунтом. "
           "Кнопка «Оставить» переносит привязку ника, сохраняя балансы.")
    )


async def notify_claim_reviewers(uid: str, profile: Dict[str, Any],
                                 *, evidence_message=None) -> None:
    """Private notifications only to verified leadership and the owner."""
    claim_id = str(profile.get("claim_id") or "")
    if not claim_id:
        return
    recipients = set(OWNER_TELEGRAM_IDS)
    if _cloud_state_hydrated:
        recipients.update(item for item in admins_db if type(item) is int)
    for reviewer_id in recipients:
        if not is_admin(reviewer_id):
            continue
        try:
            await bot.send_message(
                reviewer_id, claim_review_text(uid, profile),
                reply_markup=claim_review_keyboard(uid, claim_id),
                parse_mode="HTML", disable_web_page_preview=True
            )
            if evidence_message is not None:
                try:
                    await bot.copy_message(
                        chat_id=reviewer_id, from_chat_id=evidence_message.chat.id,
                        message_id=evidence_message.message_id
                    )
                except Exception as exc:
                    logger.warning("Claim evidence copy failed: %s", type(exc).__name__)
        except Exception as exc:
            logger.warning("Claim review notification failed: %s", type(exc).__name__)


async def send_saved_claim_evidence(reviewer_id: int, profile: Dict[str, Any]) -> None:
    """Allow reviewers to re-open a submitted screenshot after a restart."""
    if profile.get("verification_status") != "proof_submitted":
        return
    kind, file_id = profile.get("proof_kind"), profile.get("proof_file_id")
    try:
        if kind == "photo" and file_id:
            await bot.send_photo(reviewer_id, file_id)
        elif kind == "video" and file_id:
            await bot.send_video(reviewer_id, file_id)
        elif kind == "document" and file_id:
            await bot.send_document(reviewer_id, file_id)
        elif kind == "link" and profile.get("proof_link"):
            await bot.send_message(
                reviewer_id,
                "🔗 Доказательство: " + html.escape(str(profile["proof_link"])),
                parse_mode="HTML", disable_web_page_preview=True
            )
    except Exception as exc:
        logger.warning("Could not retrieve saved claim evidence: %s", type(exc).__name__)


@dp.message(Command("claims"))
async def cmd_claims(message: types.Message):
    if not is_admin(message.from_user.id) or getattr(message.chat, "type", "") != "private":
        await message.answer("⛔ Проверка регистраций доступна только руководству.")
        return
    items = [
        (uid, entry) for uid, entry in users_db.items()
        if isinstance(entry, dict) and entry.get("verification_status") in CLAIM_ACTIVE_STATES
        and entry.get("claimed_nick") and entry.get("claim_id")
    ]
    if not items:
        await message.answer("✅ Спорных регистраций нет.")
        return
    await message.answer(f"🔎 <b>FBI · На проверке: {len(items)}</b>", parse_mode="HTML")
    for uid, profile in items[:15]:
        await message.answer(
            claim_review_text(uid, profile),
            reply_markup=claim_review_keyboard(uid, profile["claim_id"]),
            parse_mode="HTML", disable_web_page_preview=True
        )
        await send_saved_claim_evidence(message.from_user.id, profile)


@dp.callback_query(F.data.startswith("claim:"))
async def review_claim(callback: types.CallbackQuery):
    if (not is_admin(callback.from_user.id)
            or not isinstance(callback.message, types.Message)
            or getattr(callback.message.chat, "type", "") != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        _, action, uid, claim_id = (callback.data or "").split(":", 3)
    except ValueError:
        await callback.answer("Неверная заявка.", show_alert=True)
        return
    if not uid.isdecimal() or action not in ("keep", "disable", "proof"):
        await callback.answer("Неизвестное решение.", show_alert=True)
        return
    async with _claim_action_locks.setdefault(uid, asyncio.Lock()):
        profile = users_db.get(uid)
        if (not isinstance(profile, dict)
                or profile.get("claim_id") != claim_id
                or profile.get("verification_status") not in CLAIM_ACTIVE_STATES):
            await callback.answer("Заявка уже обработана или устарела.", show_alert=True)
            return
        if action == "keep":
            if profile.get("verification_status") == "needs_proof":
                await callback.answer("Сначала дождитесь доказательств.", show_alert=True)
                return
            success, reason, updated, displaced = await resolve_nickname_claim(
                firebase_client, FIREBASE_PROJECT_ID, uid, claim_id,
                sign_admin_permissions, _state_sync_lock, OWNER_TELEGRAM_IDS,
                local_users=users_db
            )
            if not success:
                await callback.answer(reason[:190], show_alert=True)
                return
            save_json(USERS_FILE, users_db)
            asyncio.create_task(record_admin_activity(
                "nickname_claim_approved", callback.from_user.id,
                f"Telegram ID {uid}: {updated.get('nick', '')}; replaced={list(displaced)}"
            ))
            await callback.answer("✅ Ник закреплён за пользователем.")
            try:
                await callback.message.edit_text(
                    f"✅ <b>Ник подтверждён:</b> <code>{html.escape(updated['nick'])}</code>\n"
                    f"Проверил: {html.escape(callback.from_user.full_name)}",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            try:
                await bot.send_message(int(uid),
                    "✅ <b>Руководство подтвердило ваш игровой ник.</b>\n"
                    "Теперь можно пользоваться всеми функциями. Откройте /menu.",
                    parse_mode="HTML")
            except Exception:
                pass
            for displaced_uid in displaced:
                try:
                    await bot.send_message(int(displaced_uid),
                        "⚠️ Прежняя привязка игрового ника изменена руководством. "
                        "Откройте /start и укажите актуальный ник.")
                except Exception:
                    pass
            return

        previous = dict(profile)
        updated = dict(profile)
        updated["verification_status"] = (
            "cooldown" if action == "disable" else "needs_proof"
        )
        if action == "disable":
            updated["cooldown_until"] = (
                datetime.now(timezone.utc) + timedelta(seconds=CLAIM_COOLDOWN_SECONDS)
            ).isoformat()
            # Previous moderation buttons cannot approve an already rejected claim.
            updated.pop("claim_id", None)
        if action == "proof":
            for key in ("proof_submitted", "proof_kind", "proof_file_id",
                        "proof_link", "proof_caption"):
                updated.pop(key, None)
        users_db[uid] = updated
        if not await sync_state_to_cloud():
            users_db[uid] = previous
            await callback.answer("Firebase не сохранил решение. Повторите позже.",
                                  show_alert=True)
            return
        save_json(USERS_FILE, users_db)
        action_label = "Отклонено (повтор через 5 минут)" if action == "disable" else "Запрошены доказательства"
        asyncio.create_task(record_admin_activity(
            "nickname_claim_disabled" if action == "disable"
            else "nickname_claim_proof_requested",
            callback.from_user.id, f"Telegram ID {uid}: {updated.get('claimed_nick')}"
        ))
        await callback.answer(f"✅ {action_label}")
        try:
            await callback.message.edit_text(
                f"📋 <b>Заявка на ник:</b> "
                f"<code>{html.escape(str(updated.get('claimed_nick') or ''))}</code>\n"
                f"Решение: {action_label}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        try:
            if action == "disable":
                await bot.send_message(int(uid),
                    "⏳ Руководство отклонило заявку. "
                    "Повторная попытка доступна через 5 минут. "
                    "Затем откройте /start или /menu.")
            else:
                await bot.send_message(
                    int(uid),
                    "📎 <b>FBI · Подтверждение ника</b>\n\n"
                    "Руководство просит подтвердить игровой аккаунт. "
                    "Пришлите <b>скриншот игрового профиля с ником</b> "
                    "или ссылку на профиль. Не присылайте паспорт, пароли, "
                    "личные документы и другие конфиденциальные сведения.",
                    parse_mode="HTML"
                )
        except Exception as exc:
            logger.warning("Claim decision notification failed: %s", type(exc).__name__)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Registered before all state handlers so /cancel cannot be swallowed by report input."""
    await state.clear()
    await message.answer("❌ Действие отменено. Выберите новый пункт меню.",
                         reply_markup=get_main_menu(message.from_user.id))


@dp.message(Command("setbanner"))
async def cmd_set_menu_banner(message: types.Message, state: FSMContext):
    """Only the technical Telegram owner can change the home banner."""
    uid = message.from_user.id if message.from_user else 0
    if (message.chat.type != "private" or message.chat.id != uid
            or not is_owner(uid)):
        await message.answer("⛔ Изменение баннера доступно только владельцу.")
        return
    if await state.get_state():
        await message.answer(
            "⏳ Сначала завершите текущее действие или используйте /cancel."
        )
        return
    await state.set_state(MenuBannerStates.waiting_for_photo)
    await message.answer(
        "🖼️ <b>Баннер главного меню</b>\n\n"
        "Отправьте картинку <b>как фото</b> (не файлом). "
        "Рекомендуется формат <b>16:9</b>, ширина от 640 пикселей.\n\n"
        "Баннер появится в /start, /menu и при возврате в личный кабинет. "
        "Кнопки и данные агента останутся под изображением.\n"
        "<i>Отмена: /cancel</i>",
        parse_mode="HTML"
    )


@dp.message(Command("removebanner"))
async def cmd_remove_menu_banner(message: types.Message, state: FSMContext):
    """Restore the original text card without changing roles or user profiles."""
    uid = message.from_user.id if message.from_user else 0
    if (message.chat.type != "private" or message.chat.id != uid
            or not is_owner(uid)):
        await message.answer("⛔ Только владелец может отключить баннер.")
        return
    if await state.get_state():
        await message.answer("⏳ Завершите текущее действие или /cancel.")
        return
    global MENU_BANNER_FILE_ID
    previous = MENU_BANNER_FILE_ID
    if not previous:
        await message.answer("Баннер уже отключён.")
        return
    MENU_BANNER_FILE_ID = ""
    if not await sync_state_to_cloud():
        MENU_BANNER_FILE_ID = previous
        await message.answer("⚠️ Firebase не подтвердила изменение. Баннер сохранён.")
        return
    await message.answer(
        "✅ Баннер убран. Главное меню снова отображается обычной текстовой карточкой."
    )


@dp.message(MenuBannerStates.waiting_for_photo)
async def receive_menu_banner_photo(message: types.Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else 0
    if (message.chat.type != "private" or message.chat.id != uid
            or not is_owner(uid)):
        await state.clear()
        await message.answer("⛔ Только владелец может менять баннер.")
        return
    if not message.photo:
        await message.answer(
            "📷 Отправьте изображение именно как <b>фото</b>, "
            "не как документ. Для отмены: /cancel.",
            parse_mode="HTML"
        )
        return
    picture = message.photo[-1]
    if (picture.width < 640 or picture.height < 320 or
            not 1.6 <= picture.width / picture.height <= 2.05):
        await message.answer(
            "⚠️ Нужен горизонтальный баннер примерно 16:9, "
            "шириной от 640 пикселей. Отправьте другое фото."
        )
        return
    new_file_id = picture.file_id
    if not isinstance(new_file_id, str) or not 10 <= len(new_file_id) <= 256:
        await message.answer(
            "⚠️ Telegram не вернул корректный идентификатор фото."
        )
        return
    global MENU_BANNER_FILE_ID
    previous = MENU_BANNER_FILE_ID
    MENU_BANNER_FILE_ID = new_file_id
    if not await sync_state_to_cloud():
        MENU_BANNER_FILE_ID = previous
        await message.answer(
            "⚠️ Firebase не подтвердила сохранение. "
            "Баннер не изменён, можно отправить фото ещё раз."
        )
        return
    await state.clear()
    await message.answer("✅ <b>Баннер сохранён!</b> Он появится у всех в главном меню.",
                         parse_mode="HTML")
    profile = users_db.get(str(uid), {})
    if isinstance(profile, dict) and profile.get("nick"):
        await send_home_banner_card(
            message, home_card_text(profile, uid),
            reply_markup=get_main_menu(uid), lea=True,
            banner_file_id=MENU_BANNER_FILE_ID
        )


@dp.message(Command("menu", "help"))
async def cmd_menu(message: types.Message, state: FSMContext):
    """Always send a fresh menu at the bottom, irrespective of current FSM state."""
    if getattr(message.chat, "type", "") != "private":
        await message.answer("Откройте бота в личном чате.")
        return
    await state.clear()
    user_id = message.from_user.id
    profile = users_db.get(str(user_id))
    if isinstance(profile, dict) and profile.get("verification_status") in (
            "pending", "needs_proof", "proof_submitted", "disconnected",
                "retry_ready", "cooldown"):
        await send_fbi_card(
            message, pending_claim_card_text(profile),
            reply_markup=get_main_menu(user_id)
        )
        return
    if not isinstance(profile, dict) or not profile.get("nick"):
        await cmd_start(message, state)
        return
    await sync_user_command_scope(user_id)
    await send_home_banner_card(
        message, home_card_text(profile, user_id),
        reply_markup=get_main_menu(user_id), lea=True,
        banner_file_id=MENU_BANNER_FILE_ID
    )


@dp.message(Command("report", "status", "nick", "rank", "support", "ai", "profile", "stats", "department", "permissions", "newslog", "events", "event", "autopost", "schedule", "eventnotify"))
async def cmd_quick_navigation(message: types.Message, state: FSMContext):
    """Run public commands even when a registration/report state is active."""
    if getattr(message.chat, "type", "") != "private":
        await message.answer("Используйте команды в личном чате с ботом.")
        return
    command = (message.text or "").split(maxsplit=1)[0].split("@", 1)[0].lstrip("/")
    if command == "ai" and await state.get_state():
        await message.answer(
            "⏳ ИИ отключён, пока бот ждёт данные для текущего действия. "
            "Завершите его или введите /cancel."
        )
        return
    await state.clear()
    if command == "report":
        await start_report(message, state)
    elif command == "status":
        await cmd_my_status(message)
    elif command == "nick":
        await cmd_change_nick(message, state)
    elif command == "rank":
        await cmd_change_rank(message, state)
    elif command == "support":
        await cmd_support(message, state)
    elif command == "ai":
        await cmd_ai(message, state)
    elif command == "profile":
        await cmd_employee_profile(message)
    elif command == "stats":
        await cmd_activity_stats(message)
    elif command == "department":
        await cmd_set_department(message)
    elif command == "permissions":
        await cmd_permissions(message)
    elif command == "newslog":
        await cmd_news_log(message)
    elif command == "events":
        await cmd_events(message)
    elif command == "schedule":
        await cmd_schedule(message)
    elif command == "event":
        await start_schedule_wizard(message, state, message.from_user.id, "event")
    elif command == "autopost":
        await start_schedule_wizard(message, state, message.from_user.id, "message")
    elif command == "eventnotify":
        await cmd_eventnotify(message)


@dp.message(Command("notifications"))
async def cmd_owner_notifications(message: types.Message):
    """Owner-only private /notifications on|off; mirrored by the site toggle."""
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        await message.answer("⛔ Только владелец в личном чате.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
        await message.answer("Используйте /notifications on или /notifications off.")
        return
    desired = parts[1].lower() == "on"
    token = await firebase_client.get_token()
    if not token:
        await message.answer("⚠️ Нет связи с Firebase, настройка не сохранена.")
        return
    url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/settings"
    body = {"fields": {"auditTelegramEnabled": {"booleanValue": desired}}}
    params = [("updateMask.fieldPaths", "auditTelegramEnabled")]
    try:
        async with ClientSession(timeout=ClientTimeout(total=8)) as session:
            async with session.patch(url, params=params, json=body,
                                     headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
        await message.answer("✅ Уведомления руководства " + ("включены" if desired else "выключены") + ".")
    except Exception as exc:
        logger.warning("Owner notification preferences save failed: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось сохранить настройку Firebase.")

@dp.message(Command("version"))
async def cmd_version(message: types.Message):
    commit = os.getenv("RENDER_GIT_COMMIT", "").strip()
    version_text = f"🛡️ <b>FBI · Версия системы</b>\n\n<code>{html.escape(RELEASE_TAG)}</code>"
    if commit:
        version_text += f"\nСборка: <code>{html.escape(commit[:12])}</code>"
    await send_fbi_card(message, version_text)


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id)
    await hide_old_reply_keyboard(message)
    await sync_user_command_scope(message.from_user.id)
    if isinstance(user_data, dict) and user_data.get("nick"):
        new_username = message.from_user.username or ""
        if user_data.get("telegram_username", "") != new_username:
            old_username = user_data.get("telegram_username", "")
            user_data["telegram_username"] = new_username
            if await sync_state_to_cloud():
                save_json(USERS_FILE, users_db)
            else:
                user_data["telegram_username"] = old_username
                logger.warning("Could not confirm Telegram username update in Firebase")

    if (isinstance(user_data, dict)
            and user_data.get("verification_status") in
            ("pending", "needs_proof", "proof_submitted", "disconnected", "retry_ready", "cooldown")):
        await send_fbi_card(
            message, pending_claim_card_text(user_data),
            reply_markup=get_main_menu(message.from_user.id)
        )
        return
    if not user_data or not user_data.get("nick"):
        await send_fbi_card(
            message,
            "🛡️ <b>FBI · СИСТЕМА УЧЁТА</b>\n\n"
            "Добро пожаловать в систему FRP и похвал Arizona RP.\n\n" "👤 <b>Шаг 1 из 2 · Регистрация</b>\n" "Введите игровой ник в формате <code>Имя_Фамилия</code>.\n" "<i>Пример: Samuel_Norton</i>"
        )
        await state.set_state(RegistrationStates.waiting_for_nick)
    else:
        await send_home_banner_card(
            message,
            home_card_text(user_data, message.from_user.id),
            reply_markup=get_main_menu(message.from_user.id),
            lea=True, banner_file_id=MENU_BANNER_FILE_ID
        )
        # Bot registrations live in config/bot_state; the two visible tables
        # are independent. A website row deletion is NOT a profile deletion.
        try:
            frp_rows, praise_rows = await asyncio.wait_for(
                asyncio.gather(
                    firebase_client.get_sheet("fractionrp"),
                    firebase_client.get_sheet("praise")
                ), timeout=10
            )
            missing = [
                sheet for sheet, rows in (("fractionrp", frp_rows), ("praise", praise_rows))
                if not any(str(row.get("nick", "")).casefold() == str(user_data["nick"]).casefold()
                           for row in rows)
            ]
            if missing:
                await message.answer(
                    "ℹ️ Профиль бота сохранён отдельно от таблиц. "
                    "Сейчас для этого ника нет строки в: " +
                    ", ".join("/" + sheet for sheet in missing) + ".\n"
                    "Удаление строки через сайт не удаляет регистрацию Telegram. "
                    "Чтобы безопасно вернуть строку, откройте «⚙️ Мой никнейм», "
                    "введите свой ник и подтвердите ранг. "
                    "Для полного удаления агента владелец использует /agents."
                )
        except Exception as exc:
            logger.warning("Could not compare profile with Firestore sheets: %s",
                           type(exc).__name__)


# Every inline control is mapped to the existing validated handler; no new
# permissions, billing logic, or Firebase routes are introduced.
MAIN_MENU_ADMIN_ACTIONS = {"admin", "agents", "queue", "pending", "grant", "admins", "ai", "agentpending", "agentlogs", "stats", "autopost"}


@dp.callback_query(F.data.startswith("main:"))
async def main_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    """Route compact inline navigation using the verified Telegram sender."""
    if (not isinstance(callback.message, types.Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Откройте бота в личном чате.", show_alert=True)
        return

    action = (callback.data or "").removeprefix("main:")
    uid = callback.from_user.id
    if action in MAIN_MENU_ADMIN_ACTIONS and not is_admin(uid):
        await callback.answer("Доступно только руководству.", show_alert=True)
        return

    if action == "events":
        await state.clear()
        try:
            await show_schedule_list(callback.message, uid, include_messages=False)
            await callback.answer()
        except Exception as exc:
            logger.warning("Events menu failed: %s", type(exc).__name__)
            await callback.answer("Расписание недоступно. Попробуйте /events.", show_alert=True)
        return
    if action == "autopost":
        if not _schedule_can_manage(uid, "message"):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        await state.clear()
        await start_schedule_wizard(callback.message, state, uid, "message")
        await callback.answer()
        return

    if action == "claim_status":
        profile = users_db.get(str(uid), {})
        if not isinstance(profile, dict) or profile.get("verification_status") not in (
                "pending", "needs_proof", "proof_submitted", "disconnected",
                "retry_ready", "cooldown"):
            await callback.answer("Нет активной проверки ника.", show_alert=True)
            return
        await send_fbi_card(
            callback.message, pending_claim_card_text(profile),
            reply_markup=get_main_menu(uid)
        )
        await callback.answer()
        return

    if action == "open":
        profile = users_db.get(str(uid), {})
        if isinstance(profile, dict) and profile.get("verification_status") in (
                "pending", "needs_proof", "proof_submitted", "disconnected",
                "retry_ready", "cooldown"):
            await send_fbi_card(
                callback.message, pending_claim_card_text(profile),
                reply_markup=get_main_menu(uid)
            )
            await callback.answer()
            return
        if not isinstance(profile, dict) or not profile.get("nick"):
            await callback.answer("Для начала регистрации используйте /start.", show_alert=True)
            return
        await state.clear()
        await send_home_banner_card(
            callback.message, home_card_text(profile, uid),
            reply_markup=get_main_menu(uid), lea=True,
            banner_file_id=MENU_BANNER_FILE_ID
        )
        await callback.answer()
        return

    if action == "news":
        if not is_leader(uid):
            await callback.answer("Рассылка доступна только владельцу или лидеру.", show_alert=True)
            return
        if await state.get_state():
            await callback.answer("Завершите текущее действие или используйте /cancel.", show_alert=True)
            return
        source = callback.message.model_copy(update={
            "from_user": callback.from_user,
            # Menu callback points at the BOT's previous message, not /news.
            "text": "/news"
        })
        await callback.answer()
        await cmd_news(source, state)
        return

    if action in ("min", "plus"):
        if await state.get_state():
            await callback.answer("Сначала завершите текущее действие или /cancel.", show_alert=True)
            return
        profile = get_verified_agent(uid)
        if not profile:
            await callback.answer("Сначала подтвердите регистрацию.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            "➖ <b>Списание</b>: <code>/min frp Gomez_Addams -2 | причина</code>\n"
            "➕ <b>Начисление</b>: <code>/plus praise Gomez_Addams +1</code>\n\n"
            "Можно также написать обычную фразу, например "
            "<code>Гомер -2 фрп</code>.\n"
            "<i>Списание требует вашего подтверждения; начисление — причины "
            "или доказательства и одобрения руководства.</i>",
            parse_mode="HTML"
        )
        return

    if action == "ai":
        if await state.get_state():
            await callback.answer(
                "Сначала завершите текущее действие или используйте /cancel.",
                show_alert=True
            )
            return
        await state.set_state(AiStates.waiting_for_text)
        await callback.message.answer(
            "🤖 <b>FBI · ИИ-команда</b>\n\n"
            "Напишите обычным языком, например:\n"
            "<code>норто, гоммез -3 фрп</code>\n"
            "<code>дай гоммезу 2 похвалы</code>\n\n"
            "<i>Перед изменением баланса будет проверка ников и подтверждение.</i>",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if action == "home":
        profile = users_db.get(str(uid), {})
        if isinstance(profile, dict) and profile.get("verification_status") in (
                "pending", "needs_proof", "proof_submitted", "disconnected",
                "retry_ready", "cooldown"):
            await send_fbi_card(
                callback.message, pending_claim_card_text(profile),
                reply_markup=get_main_menu(uid)
            )
            await callback.answer()
            return
        if not isinstance(profile, dict) or not profile.get("nick"):
            await callback.answer("Сначала зарегистрируйтесь через /start.", show_alert=True)
            return
        await send_home_banner_card(
            callback.message, home_card_text(profile, uid),
            reply_markup=get_main_menu(uid), lea=True,
            banner_file_id=MENU_BANNER_FILE_ID
        )
        await callback.answer()
        return

    if action == "admin":
        await send_fbi_card(
            callback.message,
            "👑 <b>FBI · Панель руководства</b>\n\n"
            "Управляйте агентами, начислениями и заявками.\n"
            "<i>Все действия проверяют ваши права.</i>",
            reply_markup=get_main_menu(uid, admin_panel=True),
        )
        await callback.answer()
        return

    routes = {
        "report": (start_report, True),
        "status": (cmd_my_status, False),
        "profile": (cmd_employee_profile, False),
        "nick": (cmd_change_nick, True),
        "rank": (cmd_change_rank, True),
        "support": (cmd_support, True),
        "agents": (cmd_agent_panel, True),
        "stats": (cmd_activity_stats, False),
        "queue": (cmd_queue, False),
        "pending": (cmd_pending, False),
        "grant": (cmd_grant_menu, True),
        "admins": (cmd_list_admins, False),
        "agentpending": (cmd_agent_pending, False),
        "agentlogs": (cmd_agent_logs, False),
    }
    route = routes.get(action)
    if route is None:
        await callback.answer("Неизвестный пункт меню.", show_alert=True)
        return

    # This is a copy of the bot's message with the actor replaced by the
    # Telegram-verified callback sender. Never take IDs from callback_data.
    source = callback.message.model_copy(update={"from_user": callback.from_user})
    await state.clear()
    await callback.answer()
    handler, needs_state = route
    if needs_state:
        await handler(source, state)
    else:
        await handler(source)



def rank_selection_keyboard() -> InlineKeyboardMarkup:
    """Exact 1-10 rank choices, shared by registration and rank requests."""
    ranks = [("10 (Лидер)", "X"), ("9 (Зам)", "9"), ("8", "8"), ("7", "7"),
             ("6", "6"), ("5", "5"), ("4", "4"), ("3", "3"),
             ("2", "2"), ("1", "1")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"rank_{value}")
         for name, value in ranks[i:i + 2]]
        for i in range(0, len(ranks), 2)
    ])


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
    
    kb = rank_selection_keyboard()
    await message.answer(f"👤 <b>Регистрация · Шаг 2 из 2</b>\n\n" f"Ник: <code>{html.escape(nick)}</code>\n\n" "🎖️ Выберите ваш текущий ранг:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(RegistrationStates.waiting_for_rank)

@dp.callback_query(RegistrationStates.waiting_for_rank, F.data.startswith("rank_"))
async def process_rank(callback: types.CallbackQuery, state: FSMContext):
    rank = callback.data.replace("rank_", "")
    data = await state.get_data()
    nick = str(data.get("nick") or "")
    if not nick:
        await callback.answer("Регистрация устарела. Введите /start.", show_alert=True)
        await state.clear()
        return
    if not _cloud_state_hydrated:
        await callback.answer("Firebase ещё не загрузил список пользователей.", show_alert=True)
        return

    user_key = str(callback.from_user.id)
    previous = users_db.get(user_key, {})
    expected_old = str(previous.get("nick") or "") if isinstance(previous, dict) else ""
    username = callback.from_user.username or ""
    success, reason, old_nick, saved_profile = await register_profile(
        firebase_client, FIREBASE_PROJECT_ID, callback.from_user.id,
        nick, rank, username, expected_old, sign_admin_permissions, _state_sync_lock
    )
    if not success:
        await callback.answer(reason[:190], show_alert=True)
        return

    if saved_profile.get("verification_status") == "pending":
        users_db[user_key] = saved_profile
        save_json(USERS_FILE, users_db)
        await state.clear()
        await callback.message.edit_text(
            "✅ <b>Запрос на изменение профиля сохранён</b>\n\n"
            f"👤 Ник: <code>{html.escape(nick)}</code>\n"
            f"🎖️ Запрошенный ранг: <b>{html.escape(rank)}</b>\n"
            "🕒 Статус: <b>проверка руководства</b>\n\n"
            "До подтверждения текущие ники, ранги и балансы не изменяются. "
            "Отчёты доступны после решения руководства.",
            parse_mode="HTML"
        )
        await send_fbi_card(
            callback.message, pending_claim_card_text(saved_profile),
            reply_markup=get_main_menu(callback.from_user.id)
        )
        await callback.answer()
        asyncio.create_task(record_profile_activity(
            callback.from_user.id, "nickname_claim_opened", old_nick,
            nick, rank, username
        ))
        await notify_claim_reviewers(user_key, saved_profile)
        return

    users_db[user_key] = saved_profile
    save_json(USERS_FILE, users_db)
    await state.clear()
    action = "profile_changed" if old_nick else "profile_registered"
    asyncio.create_task(record_profile_activity(
        callback.from_user.id, action, old_nick, nick, rank, username
    ))
    note = (
        "\n\n🔁 Строки в /fractionrp и /praise переименованы, "
        "существующие баллы сохранены."
        if old_nick and old_nick.casefold() != nick.casefold()
        else "\n\n📊 Ник добавлен в обе таблицы (баланс 0, если записи ещё не было)."
    )
    await callback.message.edit_text(
        f"✅ <b>Профиль и таблицы синхронизированы!</b>\n\n"
        f"👤 <b>Ник:</b> <code>{html.escape(nick)}</code>\n"
        f"🎖️ <b>Ранг:</b> {html.escape(rank)}" + note,
        parse_mode="HTML"
    )
    await callback.message.answer(
        "Теперь данные должны отображаться на сайте без подачи отчёта.",
        reply_markup=get_main_menu(callback.from_user.id)
    )
    await callback.answer()


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
    await send_fbi_card(
        message,
        f"⚙️ <b>FBI · Настройки профиля</b>\n\n" f"👤 <b>Ник:</b> <code>{html.escape(current_nick)}</code>\n" f"🎖️ <b>Ранг:</b> {html.escape(str(current_rank))}\n\n"
        "Если хотите изменить ник, напишите новый (в формате <code>Имя_Фамилия</code>):",
        reply_markup=kb
    )
    await state.set_state(RegistrationStates.waiting_for_nick)

@dp.message(Command("rank"))
async def cmd_change_rank(message: types.Message, state: FSMContext):
    """User requests rank change; the rank is never applied before moderation."""
    await state.clear()
    profile = users_db.get(str(message.from_user.id), {})
    if not isinstance(profile, dict) or not profile.get("nick"):
        await message.answer("Сначала зарегистрируйте ник через /start.")
        return
    if profile.get("verification_status") in (
            "pending", "needs_proof", "proof_submitted", "disconnected",
            "retry_ready", "cooldown", "disabled"):
        await message.answer("⏳ Сначала завершите проверку текущего профиля.")
        return
    await state.update_data(nick=str(profile["nick"]))
    await state.set_state(RegistrationStates.waiting_for_rank)
    await send_fbi_card(
        message,
        "🎖️ <b>FBI · Изменение ранга</b>\n\n"
        f"👤 Ник: <code>{html.escape(str(profile['nick']))}</code>\n"
        f"📌 Сейчас: <b>{html.escape(str(profile.get('rank') or '?'))}</b>\n\n"
        "<b>Выберите новый ранг:</b>\n"
        "<i>Изменение будет применено только после проверки руководством.</i>",
        reply_markup=rank_selection_keyboard()
    )


@dp.callback_query(F.data == "cancel_nick_change")
async def cancel_nick_change(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Изменение ника отменено.")

# Просмотр своего статуса
@dp.message(F.text == "📊 Мой статус")
async def cmd_my_status(message: types.Message):
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id)
    if (not isinstance(user_data, dict) or not user_data.get("nick")
            or user_data.get("verification_status") in (
                "pending", "needs_proof", "proof_submitted", "disconnected",
                "retry_ready", "cooldown")):
        await message.answer(
            "🕒 Ник ожидает проверки. Просмотр баланса пока недоступен."
        )
        return

    nick = user_data["nick"].lower()
    # Keep the status card as one message, not a loading-message chain.

    try:
        frp_rows, praise_rows = await asyncio.wait_for(
            asyncio.gather(
                firebase_client.get_sheet("fractionrp"),
                firebase_client.get_sheet("praise")
            ), timeout=10
        )
    except Exception as e:
        logger.error("Error fetching agent status sheets: %s", type(e).__name__)
        await message.answer(
            "⚠️ Не удалось получить актуальные данные Firebase. "
            "Баланс не подтверждён, повторите «Мой статус» позднее."
        )
        return

    frp_item = next((r for r in frp_rows if r.get("nick", "").lower() == nick), None)
    praise_item = next((r for r in praise_rows if r.get("nick", "").lower() == nick), None)

    frp_qty = frp_item.get("qty", 0) if frp_item else 0
    frp_status = frp_item.get("status", "Выдано") if frp_item else "Нет записи"

    praise_qty = praise_item.get("qty", 0) if praise_item else 0
    praise_status = praise_item.get("status", "Выдано") if praise_item else "Нет записи"

    nick_esc = html.escape(user_data['nick'])
    rank_esc = html.escape(str(user_data.get('rank', '?')))
    frp_status_esc = html.escape(str(frp_status))
    praise_status_esc = html.escape(str(praise_status))

    text = (
        f"📊 <b>FBI · Статус агента</b>\n\n"
        f"👤 <b>Ник:</b> <code>{nick_esc}</code>\n"
        f"🎖️ <b>Ранг:</b> {rank_esc}\n\n"
        f"🎖️ <b>FRP:</b> <b>{frp_qty}</b>\n"
        f"     <i>{frp_status_esc}</i>\n\n"
        f"🌟 <b>Похвала:</b> <b>{praise_qty}</b>\n"
        f"     <i>{praise_status_esc}</i>\n\n"
        "<i>Актуальные данные из таблицы.</i>"
    )
    if not frp_item or not praise_item:
        text += (
            "\n\nℹ️ «Нет записи» означает отсутствие строки с этим ником в соответствующей "
            "таблице. Регистрация в боте хранится отдельно; удаление на сайте её не отменяет."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(style="primary", text="🌐 Открыть сайт таблицы", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await send_fbi_card(message, text, reply_markup=with_home_button(kb), lea=True)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  ПОДАЧА ОТЧЕТА НА ВЫДАЧУ                                         ║
# ╚══════════════════════════════════════════════════════════════════╝
@dp.message(F.text == "📝 Подать отчет на FRP / Похвалу")
async def start_report(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id)
    if (not isinstance(user_data, dict) or not user_data.get("nick")
            or user_data.get("verification_status") in (
                "pending", "needs_proof", "proof_submitted", "disconnected",
                "retry_ready", "cooldown")):
        await message.answer(
            "🕒 Ник ещё проверяется руководством. "
            "Вы можете открыть /menu, но отчёты пока недоступны."
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(style="primary", text="🎖️ FRP", callback_data="type_fractionrp"),
         InlineKeyboardButton(style="primary", text="🌟 Похвала", callback_data="type_praise")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel_report")]
    ])
    await send_fbi_card(
        message,
        f"📝 <b>FBI · Новый отчёт</b>\n\n"
        f"👤 <b>Агент:</b> <code>{html.escape(user_data['nick'])}</code>\n"
        f"🎖️ <b>Ранг:</b> {html.escape(str(user_data.get('rank', '?')))}\n\n"
        "<b>Выберите категорию:</b>",
        reply_markup=with_home_button(kb)
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
        await edit_fbi_card(message, "📑 <b>Новый отчёт · Причина</b>\n\nВыберите категорию, за которую начисляются баллы:", reply_markup=kb)
    else:
        await send_fbi_card(message, "📑 <b>Новый отчёт · Причина</b>\n\nВыберите категорию, за которую начисляются баллы:", reply_markup=kb)
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
        [InlineKeyboardButton(style="primary", text="✏️ Другое число (1–20)", callback_data="repqty_custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])
    await edit_fbi_card(
        callback.message,
        f"{icon} <b>Новый отчёт · Количество</b>\n\n" f"Сколько {label} указать в заявке?\n<i>Допустимо от 1 до 20.</i>",
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
    if value.split("@", 1)[0] == "/cancel":
        await cmd_cancel(message, state)
        return
    if not value.isdecimal() or not 1 <= int(value) <= 20:
        await message.answer("⚠️ Укажите целое число от 1 до 20 или отправьте /cancel.")
        return
    await state.update_data(quantity=int(value))
    await show_report_reason_menu(message, state)


@dp.callback_query(ReportStates.waiting_for_reason, F.data.startswith("reason_"))
async def process_report_reason(callback: types.CallbackQuery, state: FSMContext):
    reason = callback.data.replace("reason_", "")
    await state.update_data(
        reason=reason, report_session=uuid.uuid4().hex[:12],
        proof_media=[], proof_progress_message_id=None,
    )

    await edit_fbi_card(
        callback.message,
        "📎 <b>Новый отчёт · Доказательства</b>\n\n"
        "Отправьте ссылку на форум или <b>до 10 фото/видео</b>.\n\n"
        "<i>Фото можно отправить альбомом или по отдельности. "
        "После загрузки нажмите «Готово, проверить отчёт».</i>"
    )
    await state.set_state(ReportStates.waiting_for_proof)

def report_preview_text(data: Dict[str, Any], user_data: Dict[str, Any],
                        evidence_label: str, proof: str) -> str:
    """One review card for both an uploaded album and a single link."""
    label = ("/fractionrp (Отметка FRP)" if data.get("rep_type") == "fractionrp"
             else "/praise (Похвала)")
    return (
        "📋 <b>FBI · Проверка отчёта</b>\n\n"
        f"• <b>Агент:</b> <code>{html.escape(str(user_data.get('nick', '')))}</code> "
        f"[{html.escape(str(user_data.get('rank', '?')))}]\n"
        f"• <b>Тип:</b> {label} (+{data.get('quantity', 1)})\n"
        f"• <b>Причина:</b> {html.escape(str(data.get('reason', '')).capitalize())}\n"
        f"• <b>Доказательства:</b> {evidence_label}: {html.escape(proof)}\n\n"
        "<i>Проверьте данные перед отправкой.</i>\n\n"
        "Отправить отчёт руководству?"
    )


def report_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(style="success", text="✅ Отправить руководству",
                              callback_data="send_report_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])


def report_media_progress_text(media: List[Dict[str, Any]]) -> str:
    photo_count = sum(1 for item in media if item["type"] == "photo")
    video_count = len(media) - photo_count
    parts = []
    if photo_count:
        parts.append(f"📸 Фото: <b>{photo_count}</b>")
    if video_count:
        parts.append(f"🎥 Видео: <b>{video_count}</b>")
    return (
        "📎 <b>FBI · Доказательства собраны</b>\n\n"
        + "\n".join(parts)
        + f"\n\n<i>Можно добавить ещё вложения, всего до {REPORT_MEDIA_LIMIT}. "
          "Когда всё отправлено, нажмите кнопку ниже.</i>"
    )


@dp.message(ReportStates.waiting_for_proof)
async def process_report_proof(message: types.Message, state: FSMContext):
    """Collect multiple Telegram album items as ONE report, with ONE confirmation."""
    if (message.text or "").strip().split("@", 1)[0] == "/cancel":
        await cmd_cancel(message, state)
        return

    if message.photo or message.video:
        kind = "photo" if message.photo else "video"
        file_id = message.photo[-1].file_id if message.photo else message.video.file_id
        key = (message.chat.id, message.from_user.id)
        # Covers album items delivered in parallel and individually sent photos.
        async with _report_media_locks.setdefault(key, asyncio.Lock()):
            if await state.get_state() != ReportStates.waiting_for_proof.state:
                return
            data = await state.get_data()
            media = list(data.get("proof_media") or [])
            # Retries of the same Telegram update should not duplicate a picture.
            if any(item.get("message_id") == message.message_id for item in media):
                return
            if len(media) >= REPORT_MEDIA_LIMIT:
                await message.answer("⚠️ К одному отчёту можно прикрепить не больше 10 фото/видео. "
                                     "Нажмите «Готово, проверить отчёт».")
                return
            media.append({
                "type": kind, "file_id": file_id, "message_id": message.message_id,
                "caption": (message.caption or "").strip()[:500]
            })
            media.sort(key=lambda item: item["message_id"])
            await state.update_data(proof_media=media)
            session = data.get("report_session")
            if not session:
                # Do not produce a control with an unbound/guessable session.
                await message.answer("⚠️ Сессия отчёта устарела. Начните подачу заново.")
                return
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Готово, проверить отчёт",
                    callback_data=f"repfinish:{session}", style="success"
                )],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
            ])
            progress_id = data.get("proof_progress_message_id")
            if progress_id:
                try:
                    await bot.edit_message_text(
                        report_media_progress_text(media),
                        chat_id=message.chat.id, message_id=int(progress_id),
                        reply_markup=kb, parse_mode="HTML"
                    )
                except Exception as exc:
                    logger.info("Could not refresh report media counter: %s", type(exc).__name__)
            else:
                preview = await send_fbi_card(
                    message, report_media_progress_text(media), reply_markup=kb
                )
                await state.update_data(proof_progress_message_id=preview.message_id)
        return

    proof = (message.text or "").strip()
    if not proof.startswith(("https://", "http://")) or len(proof) > 1000:
        await message.answer("⚠️ Отправьте ссылку http(s), фото или видео.")
        return
    data = await state.get_data()
    if data.get("proof_media"):
        await message.answer("📎 Фото уже добавлены. Нажмите «Готово, проверить отчёт» "
                             "в сообщении со счётчиком вложений.")
        return
    await state.update_data(
        proof=proof, proof_url=proof, proof_media_type="", proof_file_id="",
        proof_media=[]
    )
    user_data = users_db.get(str(message.from_user.id), {})
    text = report_preview_text(data, user_data, "🔗 Ссылка", proof)
    await send_fbi_card(message, text, reply_markup=report_confirm_keyboard())
    await state.set_state(ReportStates.confirm)


@dp.callback_query(ReportStates.waiting_for_proof, F.data.startswith("repfinish:"))
async def finish_report_proof(callback: types.CallbackQuery, state: FSMContext):
    """Only the active report owner can finish; stale buttons are rejected."""
    key = (callback.message.chat.id, callback.from_user.id)
    async with _report_media_locks.setdefault(key, asyncio.Lock()):
        if await state.get_state() != ReportStates.waiting_for_proof.state:
            await callback.answer("Отчёт уже обработан.", show_alert=True)
            return
        data = await state.get_data()
        if callback.data.removeprefix("repfinish:") != data.get("report_session"):
            await callback.answer("Кнопка устарела. Используйте текущий отчёт.", show_alert=True)
            return
        media = sorted(data.get("proof_media") or [],
                       key=lambda item: item["message_id"])
        if not media:
            await callback.answer("Сначала отправьте фото или видео.", show_alert=True)
            return

        caption = next((item.get("caption") for item in media if item.get("caption")), "")
        if all(item["type"] == "photo" for item in media):
            evidence = f"📸 Фото ({len(media)} шт.)"
        elif all(item["type"] == "video" for item in media):
            evidence = f"🎥 Видео ({len(media)} шт.)"
        else:
            evidence = f"📎 Фото и видео ({len(media)} шт.)"
        proof = caption or f"Вложения в Telegram: {len(media)}"
        # Keep existing legacy metadata and persist every attachment.
        await state.update_data(
            proof=proof, proof_url="",
            proof_media_type=media[0]["type"], proof_file_id=media[0]["file_id"],
            proof_media=[{"type": item["type"], "file_id": item["file_id"]}
                         for item in media]
        )
        user_data = users_db.get(str(callback.from_user.id), {})
        text = report_preview_text(data, user_data, evidence, proof)
        await edit_fbi_card(callback.message, text,
                            reply_markup=report_confirm_keyboard())
        await state.set_state(ReportStates.confirm)
    await callback.answer()


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
            InlineKeyboardButton(style="success", text="➕ 1", callback_data=f"qtyplus_{req_id}")
        ])
    controls.append([
        InlineKeyboardButton(style="success", text=f"✅ Одобрить (+{qty})", callback_data=f"approve_{req_id}"),
        InlineKeyboardButton(style="danger", text="❌ Отклонить", callback_data=f"reject_{req_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=controls)


def pending_report_text(req_id: str, req: Dict[str, Any]) -> str:
    qty = int(req.get("quantity", 1))
    return (
        f"📨 <b>FBI · Заявка #{html.escape(req_id)}</b>\n\n" "🕒 <b>Статус:</b> Ожидает проверки\n\n"
        f"👤 <b>Агент:</b> <code>{html.escape(str(req.get('nick') or ''))}</code> "
        f"[{html.escape(str(req.get('rank') or '?'))}]\n"
        f"📌 <b>Тип:</b> /{html.escape(str(req.get('rep_type') or ''))} (+{qty})\n"
        f"📝 <b>Причина:</b> {html.escape(str(req.get('reason') or ''))}\n"
        f"🔗 <b>Доказательства:</b> {html.escape(str(req.get('proof') or ''))}\n"
        + (f"📎 <b>Вложений:</b> {len(req.get('proof_media') or [])}\n"
           if req.get("proof_media") else "")
        + "\n<i>Измените количество кнопками ➖/➕ и выберите решение.</i>"
    )


async def send_pending_report_to_admin(chat_id: int, req_id: str, req: Dict[str, Any]) -> None:
    sent = await bot.send_message(chat_id, pending_report_text(req_id, req),
                                  reply_markup=pending_report_keyboard(req_id, req),
                                  disable_web_page_preview=True, parse_mode="HTML")
    refs = req.setdefault("admin_messages", [])
    refs.append({"chat_id": chat_id, "message_id": sent.message_id})
    # Send every photo/video in the report to authorized leadership only.
    # Albums are sent as a single Telegram media group when possible.
    items = req.get("proof_media") or []
    if not isinstance(items, list):
        items = []
    valid = [item for item in items[:REPORT_MEDIA_LIMIT]
             if isinstance(item, dict)
             and item.get("type") in ("photo", "video")
             and isinstance(item.get("file_id"), str)
             and item["file_id"]]
    media_kind, media_id = req.get("proof_media_type"), req.get("proof_file_id")
    # Compatibility with reports submitted before the multi-photo update.
    if not valid and media_id and media_kind in ("photo", "video"):
        valid = [{"type": media_kind, "file_id": media_id}]
    if valid:
        caption = f"📎 Вложение к заявке #{req_id} от {str(req.get('nick', 'Агент'))[:70]}"
        try:
            if len(valid) > 1:
                attachments = [
                    InputMediaPhoto(media=item["file_id"])
                    if item["type"] == "photo"
                    else InputMediaVideo(media=item["file_id"])
                    for item in valid
                ]
                await bot.send_media_group(chat_id=chat_id, media=attachments)
            elif valid[0]["type"] == "photo":
                await bot.send_photo(chat_id, valid[0]["file_id"],
                                     caption=caption, parse_mode=None)
            else:
                await bot.send_video(chat_id, valid[0]["file_id"],
                                     caption=caption, parse_mode=None)
        except Exception as exc:
            logger.warning("Could not forward report evidence to administrator: %s",
                           type(exc).__name__)
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
    if (not isinstance(user_data, dict) or not user_data.get("nick")
            or user_data.get("verification_status") in (
                "pending", "needs_proof", "proof_submitted", "disabled", "disconnected",
                "retry_ready", "cooldown")):
        await state.clear()
        await callback.answer("Ваш игровой ник ещё не подтверждён.", show_alert=True)
        return
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
        "proof_media_type": data.get("proof_media_type", ""),
        "proof_file_id": data.get("proof_file_id", ""),
        "proof_media": data.get("proof_media", []),
        "quantity": int(data.get("quantity", 1)),
        "admin_messages": [],
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    save_json(REQUESTS_FILE, requests_db)
    notify_state_changed()

    await edit_fbi_card(
        callback.message,
        f"✅ <b>FBI · Отчёт #{req_id} отправлен</b>\n\n" "🕒 <b>Статус:</b> Ожидает проверки\n\n"
        "Как только лидер или заместитель проверит его, вам придет уведомление."
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
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "reports.review"):
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    req_id = callback.data.replace("approve_", "")
    req = requests_db.get(req_id)
    if not req or req.get("status") != "pending":
        await callback.answer("Заявка уже была обработана ранее!", show_alert=True)
        return

    holder = users_db.get(str(req.get("user_id")), {})
    if (not isinstance(holder, dict)
            or str(holder.get("nick") or "").casefold()
               != str(req.get("nick") or "").casefold()
            or holder.get("verification_status") in
               ("pending", "needs_proof", "proof_submitted", "disabled", "disconnected", "retry_ready", "cooldown")):
        await callback.answer(
            "Ник заявителя больше не подтверждён. Заявку нельзя начислять.",
            show_alert=True
        )
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
    asyncio.create_task(record_admin_activity("report_approved", callback.from_user.id,
        f"Заявка {req_id}: {req.get('nick', '')}, +{quantity}", req.get("rep_type", "")))
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
                               f"✅ <b>FBI · Отчёт одобрен</b>\n\n" f"Заявка: <code>#{html.escape(req_id)}</code>\n" f"Начислено: <b>+{quantity}</b>\n\n<i>Баланс обновлён руководством.</i>",
                               parse_mode="HTML")
    except Exception as exc:
        logger.warning(f"Could not notify reporter: {exc}")


@dp.callback_query(F.data.startswith("reject_"))
async def reject_report(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "reports.review"):
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    req_id = callback.data.replace("reject_", "")
    req = requests_db.get(req_id)
    if not req or req.get("status") != "pending":
        await callback.answer("Заявка уже была обработана ранее!", show_alert=True)
        return

    req["status"] = "rejected"
    req["rejected_by"] = callback.from_user.full_name
    asyncio.create_task(record_admin_activity("report_rejected", callback.from_user.id,
        f"Заявка {req_id}: {req.get('nick', '')}", req.get("rep_type", "")))
    save_json(REQUESTS_FILE, requests_db)
    notify_state_changed()

    await close_pending_report_messages(req_id, req, f"❌ ОТКЛОНЕНО ({callback.from_user.full_name})")

    try:
        await bot.send_message(
            int(req["user_id"]),
            f"❌ <b>FBI · Отчёт #{html.escape(req_id)} отклонён</b>\n\n" f"Категория: /{html.escape(str(req['rep_type']))}\n"
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
        f"🔐 <b>FBI · Безопасность</b>\n"
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
        [InlineKeyboardButton(style="success", text="✅ Восстановить перечисленных", callback_data=f"aclrestore:{_pending_legacy_nonce}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="aclrestorecancel")]
    ])
    await message.answer(
        "🔐 <b>FBI · Восстановление прав</b>\n\n" "<b>Найден архивный неподписанный список:</b>\n\n"
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
    for restored_uid in admins_db:
        await sync_user_command_scope(restored_uid)
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
            [InlineKeyboardButton(style="primary", text="👑 Лидер", callback_data=f"adm_role_{target_id}_{nonce}_leader")],
            [InlineKeyboardButton(style="primary", text="⭐ Заместитель", callback_data=f"adm_role_{target_id}_{nonce}_deputy")],
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
                InlineKeyboardButton(style="success", text="✅ Одобрить доступ", callback_data=f"adm_grant_{uid}_{nonce}"),
                InlineKeyboardButton(style="danger", text="❌ Отклонить", callback_data=f"adm_deny_{uid}_{nonce}")
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
        [InlineKeyboardButton(style="success", text="✅ Подтвердить назначение", callback_data=f"adm_grant_{target_id}_{nonce}")],
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
    await sync_user_command_scope(target_id)
    asyncio.create_task(record_admin_activity("admin_role_assigned", callback.from_user.id,
        f"Назначена роль {role} для Telegram ID {target_id}"))
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
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "agents.view"):
        await message.answer("Доступно только руководству фракции.")
        return

    all_admins = list(set(owner_cfg.get("owner_ids", []) + admins_db))
    lead_nick = html.escape(LEADER_INFO.get("nick", "Не назначен"))
    lead_rank = html.escape(str(LEADER_INFO.get("rank", "10")))
    tech_nick = html.escape(LEADER_INFO.get("tech_nick", "Gopon"))

    text = (
        f"👥 <b>FBI · Состав руководства</b>\n\n"
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
            kb_rows.append([InlineKeyboardButton(style="danger", text=f"❌ Снять {nick}", callback_data=f"remove_admin_{adm_id}")])

    text += (
        "💡 <b>Как добавить нового заместителя:</b>\n"
        "Передайте ему ключ для входа:\n"
        f"<code>/admin [KEY REDACTED]</code>\n\n"
        "После отправки команды вам в этот чат придёт запрос с кнопками <b>[Одобрить доступ]</b> / <b>[Отклонить]</b>."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await send_fbi_card(message, text, reply_markup=with_home_button(kb))

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
        await sync_user_command_scope(target_id)
        asyncio.create_task(record_admin_activity("admin_revoked", callback.from_user.id,
            f"Доступ отозван у Telegram ID {target_id}"))
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
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "reports.review"):
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
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "points.grant"):
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
                asyncio.create_task(record_admin_activity("manual_grant", message.from_user.id,
                    f"{nick}: +{qty}", sheet))
                results.append(f"✅ {html.escape(nick)}: +{qty}")
            else:
                results.append(f"⚠️ {html.escape(nick)}: не подтверждено")
        except Exception as exc:
            logger.error(f"Manual grant {grant_id} failed for {nick}: {exc}")
            results.append(f"⚠️ {html.escape(nick)}: проверьте баланс перед повтором")

    await message.answer(
        f"📊 <b>FBI · Результаты начисления</b>\n\n" f"Категория: <code>/{sheet}</code> · агентов: <b>{len(nicks)}</b>\n\n"
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
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "points.grant"):
        await message.answer("⛔ Только для руководства.")
        return
    await state.set_state(GrantStates.waiting_for_details)
    await send_fbi_card(
        message,
        "➕ <b>FBI · Начисление баллов</b>\n\n" "Введите тип, ник и количество (1–20):\n"
        "<code>frp Gomez_Addams 2</code>\n"
        "<code>praise Gomez_Addams 3</code>\n\n"
        "Или используйте /grant praise Gomez_Addams 3. Для отмены: /cancel."
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
    waiting_for_reason = State()


async def remove_agent_everywhere(nick: str) -> bool:
    """Atomically remove zero-balance rows AND the cloud Telegram profile.

    A single Firestore commit covers both sheets and config/bot_state. On any
    conflict all changes are rejected, not half-applied.
    """
    if not _cloud_state_hydrated:
        raise RuntimeError("Cannot delete agents before cloud state is loaded")
    if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", nick):
        raise ValueError("Invalid agent nickname")
    base = (f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
            "/databases/(default)/documents")
    async with _state_sync_lock:
        token = await firebase_client.get_token()
        if not token:
            raise RuntimeError("Firebase authentication unavailable")
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        for retry in range(5):
            async with ClientSession(timeout=ClientTimeout(total=25)) as session:
                async def read(path):
                    async with session.get(base + "/" + path, headers=headers) as response:
                        if response.status != 200:
                            raise RuntimeError(f"Firestore read failed: {response.status}")
                        doc = await response.json()
                        if not doc.get("updateTime"):
                            raise RuntimeError("Firestore revision missing")
                        return doc

                paths = ("config/bot_state", "sheets/fractionrp", "sheets/praise")
                records = await asyncio.gather(*(read(path) for path in paths))
                docs = dict(zip(paths, records))
                raw = docs["config/bot_state"].get("fields", {}).get(
                    "data", {}).get("stringValue", "")
                state = json.loads(raw)
                users = state.get("users")
                cloud_admins = state.get("admins", [])
                cloud_roles = state.get("admin_roles", {})
                if not isinstance(users, dict) or not isinstance(cloud_admins, list) or not isinstance(cloud_roles, dict):
                    raise RuntimeError("Invalid cloud state")
                stored_acl = str(state.get("admin_acl_signature") or "")
                if ((cloud_admins and not stored_acl) or
                        (stored_acl and not hmac.compare_digest(
                            stored_acl, sign_admin_permissions(cloud_admins, cloud_roles)))):
                    raise RuntimeError("Unverified cloud administrator ACL")
                cloud_permissions = normalize_overrides(
                    state.get("permission_overrides", {}))
                stored_perm_sig = str(state.get("permission_overrides_signature") or "")
                if ((cloud_permissions and not stored_perm_sig) or
                        (stored_perm_sig and not hmac.compare_digest(
                            stored_perm_sig,
                            sign_overrides(cloud_permissions, permission_secret())))):
                    raise RuntimeError("Unverified cloud permission overrides")
                matches = [(uid, profile) for uid, profile in users.items()
                           if isinstance(profile, dict)
                           and str(profile.get("nick") or "").casefold() == nick.casefold()]
                protected = OWNER_TELEGRAM_IDS | set(admins_db)
                protected.update(uid for uid in cloud_admins if type(uid) is int and uid > 0)
                if any(str(uid).isdecimal() and int(uid) in protected for uid, _ in matches):
                    raise ValueError("Нельзя удалять руководство через очистку агентов.")
                sheet_updates = {}
                for sheet in ("fractionrp", "praise"):
                    doc = docs["sheets/" + sheet]
                    rows = firebase_client._parse_firestore_rows(
                        doc.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", []))
                    target_rows = [row for row in rows
                                   if str(row.get("nick") or "").casefold() == nick.casefold()]
                    if any(int(row.get("qty") or 0) != 0 for row in target_rows):
                        raise ValueError("У агента есть ненулевой баланс. Очистка отменена.")
                    sheet_updates[sheet] = [row for row in rows if row not in target_rows]
                if not matches and all(len(sheet_updates[sheet]) ==
                                       len(firebase_client._parse_firestore_rows(
                                           docs["sheets/" + sheet].get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", [])))
                                       for sheet in ("fractionrp", "praise")):
                    return True
                for uid, _ in matches:
                    users.pop(uid, None)
                    # Revocation is included in the SAME atomic operation.
                    cloud_permissions.pop(str(uid), None)
                state["users"] = users
                state["permission_overrides"] = cloud_permissions
                state["permission_overrides_signature"] = sign_overrides(
                    cloud_permissions, permission_secret())
                now = datetime.now(timezone.utc).isoformat()
                writes = []
                for sheet in ("fractionrp", "praise"):
                    path = "sheets/" + sheet
                    writes.append({
                        "update": {
                            "name": f"projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/{path}",
                            "fields": {
                                "rows": {"arrayValue": {"values": [
                                    firebase_client._row_to_firestore(row) for row in sheet_updates[sheet]
                                ]}},
                                "updatedAt": {"timestampValue": now}
                            }
                        },
                        "updateMask": {"fieldPaths": ["rows", "updatedAt"]},
                        "currentDocument": {"updateTime": docs[path]["updateTime"]}
                    })
                writes.append({
                    "update": {
                        "name": f"projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/bot_state",
                        "fields": {
                            "data": {"stringValue": json.dumps(state, ensure_ascii=False)},
                            "updatedAt": {"timestampValue": now}
                        }
                    },
                    "updateMask": {"fieldPaths": ["data", "updatedAt"]},
                    "currentDocument": {"updateTime": docs["config/bot_state"]["updateTime"]}
                })
                async with session.post(base + ":commit", headers=headers,
                                        json={"writes": writes}) as response:
                    if response.status == 200:
                        users_db.clear()
                        users_db.update(users)
                        permission_overrides.clear()
                        permission_overrides.update(cloud_permissions)
                        save_json(USERS_FILE, users_db)
                        return True
                    if response.status not in (409, 412):
                        raise RuntimeError(
                            f"Firestore atomic removal rejected: HTTP {response.status}")
            await asyncio.sleep(0.12 * (retry + 1))
    raise RuntimeError("Concurrent changes prevented atomic deletion")


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
    text = f"🧑‍💼 <b>FBI · Реестр агентов</b>\n\n" f"Всего: <b>{len(agents)}</b> · страница <b>{page + 1}/{pages}</b>\n\n"
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
        InlineKeyboardButton(style="primary", text="🔎 Поиск", callback_data="agsearch"),
        InlineKeyboardButton(text="Сбросить поиск", callback_data="agreset")
    ])
    keyboard.append([InlineKeyboardButton(style="primary", text="🔄 Обновить", callback_data=f"agpage:{page}")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def agent_detail_markup(agent: Dict[str, Any], page: int):
    key = agent_hash(agent["nick"])
    text = (
        f"👤 <b>FBI · Карточка агента</b>\n\n" f"<b>{html.escape(agent['nick'])}</b> · ранг {html.escape(agent['rank'])}\n\n"
        f"🎖️ FRP: <b>{agent['frp']}</b> — {html.escape(agent['frp_status'])}\n"
        f"🌟 Похвалы: <b>{agent['praise']}</b> — {html.escape(agent['praise_status'])}\n\n"
        "<i>Выберите действие. Изменение баланса требует подтверждения.</i>"
    )
    keyboard = [
        [InlineKeyboardButton(style="success", text="🎖️ FRP +1", callback_data=f"agask:g:f:{key}:1:{page}"),
         InlineKeyboardButton(style="success", text="🎖️ FRP +5", callback_data=f"agask:g:f:{key}:5:{page}"),
         InlineKeyboardButton(text="🎖️ FRP −1", callback_data=f"agask:d:f:{key}:1:{page}")],
        [InlineKeyboardButton(style="success", text="🌟 Похвала +1", callback_data=f"agask:g:p:{key}:1:{page}"),
         InlineKeyboardButton(style="success", text="🌟 Похвала +5", callback_data=f"agask:g:p:{key}:5:{page}"),
         InlineKeyboardButton(text="🌟 Похвала −1", callback_data=f"agask:d:p:{key}:1:{page}")]
    ]
    if agent.get("uid") is not None:
        keyboard.append([InlineKeyboardButton(style="primary", text="🎖️ Изменить ранг", callback_data=f"agranks:{key}:{page}")])
    keyboard.append([InlineKeyboardButton(style="danger", text="🗑️ Удалить запись (владелец)", callback_data=f"agdelete:{key}:{page}")])
    keyboard.append([InlineKeyboardButton(text="🔙 К списку", callback_data=f"agpage:{page}"),
                     InlineKeyboardButton(style="primary", text="🔄 Обновить", callback_data=f"agview:{key}:{page}")])
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def agent_panel_list(message: types.Message, admin_id: int, page: int = 0, edit: bool = False):
    agents = await fetch_agent_directory()
    term = agent_search_terms.get(admin_id, "")
    if term:
        agents = [agent for agent in agents if term in agent["nick"].casefold()]
    text, kb = agent_list_markup(agents, page)
    if edit:
        await edit_fbi_card(message, text, reply_markup=with_home_button(kb))
    else:
        await send_fbi_card(message, text, reply_markup=with_home_button(kb))


async def agent_panel_detail(message: types.Message, key: str, page: int):
    agents = await fetch_agent_directory()
    agent = next((item for item in agents if agent_hash(item["nick"]) == key), None)
    if not agent:
        await message.edit_text("⚠️ Агент больше не найден. Откройте список снова.")
        return
    text, kb = agent_detail_markup(agent, page)
    uid = agent.get("uid")
    if uid and isinstance(users_db.get(str(uid)), dict):
        details = personnel_details(users_db[str(uid)], requests_db, uid)
        text = text.replace("<i>Выберите действие.", details + "\n\n<i>Выберите действие.")
    await edit_fbi_card(message, text, reply_markup=with_home_button(kb))




@dp.message(Command("permissions"))
async def cmd_permissions(message: types.Message):
    if not _agent_private(message):
        await message.answer("Откройте эту команду в личном чате бота.")
        return
    """Owner-only capabilities; never replace existing Telegram role assignments."""
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Настраивать права может только владелец.")
        return
    if not _cloud_state_hydrated:
        await message.answer("Firebase ещё не подтвердил состояние прав.")
        return
    parts = (message.text or "").split()
    if len(parts) == 1:
        await message.answer(
            "🔐 Права фракции:\n" + "\n".join("• " + p for p in PERMISSIONS) +
            "\n\nИзменить: /permissions TelegramID permission allow|deny|clear\n"
            "Пример: /permissions 123456789 points.debit allow\n"
            "Без явного разрешения обычные агенты не могут списывать баллы."
        )
        return
    if len(parts) != 4 or not parts[1].isdigit() or parts[2] not in PERMISSIONS or parts[3] not in ("allow", "deny", "clear"):
        await message.answer("Неверные параметры. Используйте /permissions для справки.")
        return
    uid, capability, mode = parts[1], parts[2], parts[3]
    if (uid not in users_db and int(uid) not in admins_db
            and int(uid) not in OWNER_TELEGRAM_IDS):
        await message.answer("Telegram ID не принадлежит зарегистрированному сотруднику.")
        return
    if int(uid) in OWNER_TELEGRAM_IDS:
        await message.answer("Права владельца не изменяются.")
        return
    if int(uid) not in admins_db and capability not in AGENT_DELEGATABLE:
        await message.answer("Обычным агентам можно отдельно разрешать только points.debit.")
        return
    previous = {key: dict(value) for key, value in permission_overrides.items()}
    if mode == "clear":
        if uid in permission_overrides:
            permission_overrides[uid].pop(capability, None)
            if not permission_overrides[uid]:
                permission_overrides.pop(uid)
    else:
        permission_overrides.setdefault(uid, {})[capability] = (mode == "allow")
    if not await sync_state_to_cloud():
        permission_overrides.clear()
        permission_overrides.update(previous)
        await message.answer("Firebase не подтвердила изменение прав.")
        return
    await record_admin_activity("permission_changed", message.from_user.id,
                                f"Telegram ID {uid}: {capability} -> {mode}")
    await message.answer(f"✅ Право {capability}: {mode}. Прежние роли сохранены.")


@dp.message(Command("profile"))
async def cmd_employee_profile(message: types.Message):
    if not _agent_private(message):
        await message.answer("Откройте эту команду в личном чате бота.")
        return
    """Private self-service card; never show another user's Telegram profile."""
    profile = get_verified_agent(message.from_user.id)
    if not profile:
        await message.answer("Для просмотра карточки подтвердите свой ник через /start.")
        return
    try:
        roster = await fetch_agent_directory()
    except Exception as exc:
        logger.warning("Personnel roster unavailable: %s", type(exc).__name__)
        await message.answer("Не удалось получить актуальные балансы Firebase.")
        return
    matching = next((agent for agent in roster
                     if agent.get("uid") == str(message.from_user.id)), None)
    if matching is None:
        await message.answer("Карточка пока не найдена в реестре.")
        return
    details = personnel_details(profile, requests_db, message.from_user.id)
    await send_fbi_card(
        message, f"👤 <b>FBI · Карточка сотрудника</b>\n\n"
        f"<b>{html.escape(str(profile['nick']))}</b> · ранг "
        f"{html.escape(str(profile.get('rank') or '?'))}\n"
        f"🎖️ FRP: <b>{matching['frp']}</b> · "
        f"🌟 Похвалы: <b>{matching['praise']}</b>\n\n" + details
    )


@dp.message(Command("stats"))
async def cmd_activity_stats(message: types.Message):
    if not _agent_private(message):
        await message.answer("Откройте эту команду в личном чате бота.")
        return
    """Leadership only; figures are Telegram report activity, not online time."""
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "stats.view"):
        await message.answer("⛔ Статистика сотрудников доступна руководству.")
        return
    weekly = ranked_reporters(requests_db, users_db, days=7, limit=10)
    monthly = ranked_reporters(requests_db, users_db, days=30, limit=10)
    def render(rows):
        return "\n".join(
            f"{i}. <code>{html.escape(item['nick'])}</code> "
            f"({html.escape(item['rank'])}) — ✅ {item['approved']}, "
            f"всего {item['total']}"
            for i, item in enumerate(rows, 1)
            if item["total"] > 0
        ) or "Нет отчётов за период."
    await send_fbi_card(
        message, "📊 <b>FBI · Активность по отчётам</b>\n\n"
        "🗓 <b>За последние 7 дней</b>\n" + render(weekly) +
        "\n\n🗓 <b>За последние 30 дней</b>\n" + render(monthly) +
        "\n\n<i>Статистика учитывает только поданные отчёты в боте, "
        "а не игровое время или все виды деятельности.</i>"
    )


@dp.message(Command("department"))
async def cmd_set_department(message: types.Message):
    if not _agent_private(message):
        await message.answer("Откройте эту команду в личном чате бота.")
        return
    """Leader-managed optional department, without presuming server departments."""
    if not has_permission(message.from_user.id, "department.manage"):
        await message.answer("⛔ Назначение отделов доступно владельцу и лидеру.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("Формат: /department Nick_Name Название отдела")
        return
    nick, department = parts[1].strip(), parts[2].strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", nick) or not 2 <= len(department) <= 60:
        await message.answer("Некорректный ник или название отдела (2–60 символов).")
        return
    if any(ord(char) < 32 or char in "<>" for char in department):
        await message.answer("Название содержит недопустимые символы.")
        return
    matches = [(uid, p) for uid, p in users_db.items()
               if isinstance(p, dict) and str(p.get("nick") or "").casefold() == nick.casefold()]
    if len(matches) != 1:
        await message.answer("Не найден однозначно зарегистрированный агент.")
        return
    uid, profile = matches[0]
    before = dict(profile)
    profile["department"] = department
    if not await sync_state_to_cloud():
        users_db[uid] = before
        await message.answer("Firebase не подтвердила изменение отдела.")
        return
    save_json(USERS_FILE, users_db)
    await record_admin_activity("department_changed", message.from_user.id,
                                f"{nick}: отдел {department}")
    await message.answer(f"✅ Отдел агента {html.escape(nick)} изменён: "
                         f"{html.escape(department)}", parse_mode="HTML")


@dp.message(Command("agents"))
@dp.message(F.text == "🧑‍💼 Управление агентами")
async def cmd_agent_panel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "agents.view"):
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
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "agents.view"):
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
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "agents.view"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AgentPanelStates.waiting_for_search)
    await callback.message.answer("🔎 Введите часть ника агента. /cancel — отмена поиска.")
    await callback.answer()


@dp.message(AgentPanelStates.waiting_for_search)
async def agent_search_term(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "agents.view"):
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
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "agents.view"):
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
async def agent_ask_action(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "agents.view"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        _, kind, which, key, amount_raw, page_raw = callback.data.split(":")
        qty, page = int(amount_raw), int(page_raw)
        if kind not in ("g", "d") or which not in ("f", "p") or qty not in (1, 5):
            raise ValueError("Invalid action")
        if kind == "d" and qty != 1:
            raise ValueError("Invalid debit")
        action_permission = "points.grant" if kind == "g" else "points.debit"
        if not has_permission(callback.from_user.id, action_permission):
            await callback.answer("Нет права на эту операцию.", show_alert=True)
            return
        agents = await fetch_agent_directory()
        agent = next((item for item in agents if agent_hash(item["nick"]) == key), None)
        if not agent:
            await callback.answer("Агент не найден", show_alert=True)
            return
        target_snapshot = None
        if kind == "d":
            sheet = "fractionrp" if which == "f" else "praise"
            rows = await firebase_client.get_sheet(sheet)
            candidates = [r for r in rows
                          if str(r.get("nick") or "").casefold() == agent["nick"].casefold()]
            if len(candidates) != 1 or int(candidates[0].get("qty") or 0) < 1:
                await callback.answer("Не найдена запись с доступным балансом.", show_alert=True)
                return
            target_snapshot = _agent_key_row(candidates[0])
        now = datetime.now(timezone.utc).timestamp()
        for token, task in list(agent_action_tokens.items()):
            if now - task["created"] > 600:
                agent_action_tokens.pop(token, None)
        token = uuid.uuid4().hex[:16]
        agent_action_tokens[token] = {
            "uid": callback.from_user.id, "chat_id": callback.message.chat.id,
            "nick": agent["nick"], "rank": agent["rank"],
            "kind": kind, "which": which, "qty": qty, "page": page,
            "key": key, "created": now, "status": "new",
            "target": target_snapshot, "reason": "",
            "op_id": "agent-" + uuid.uuid4().hex
        }
        if kind == "d":
            if callback.message.chat.type != "private":
                await callback.answer("Списание с причиной доступно только в личном чате.", show_alert=True)
                agent_action_tokens.pop(token, None)
                return
            await state.update_data(agent_panel_debit_token=token)
            await state.set_state(AgentPanelStates.waiting_for_reason)
            await callback.message.answer(
                f"📝 Причина списания 1 {'FRP' if which == 'f' else 'похвалы'} "
                f"для <code>{html.escape(agent['nick'])}</code>:\n"
                "Введите текст причины (5–200 символов). Для отмены /cancel.",
                parse_mode="HTML"
            )
            await callback.answer()
            return
        operation = f"+{qty}" if kind == "g" else "−1"
        type_name = "FRP" if which == "f" else "похвалы"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(style="success", text=f"✅ Подтвердить {operation}", callback_data=f"agdo:{token}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"agview:{key}:{page}")]
        ])
        await callback.message.edit_text(
            f"⚠️ <b>FBI · Подтверждение операции</b>\n\n"
            f"👤 {html.escape(agent['nick'])}\n"
            f"{type_name}: <b>{operation}</b>\n"
            "После подтверждения баланс изменится в Firebase.",
            reply_markup=kb, parse_mode="HTML"
        )
        await callback.answer()
    except Exception as exc:
        logger.warning(f"Invalid agent action: {exc}")
        await callback.answer("Не удалось подготовить действие", show_alert=True)




@dp.message(AgentPanelStates.waiting_for_reason)
async def agent_panel_debit_reason(message: types.Message, state: FSMContext):
    if not _agent_private(message):
        await message.answer("Введите причину в личном чате с ботом.")
        return
    data = await state.get_data()
    token = data.get("agent_panel_debit_token")
    action = agent_action_tokens.get(token)
    if not action or action["uid"] != message.from_user.id or action["status"] != "new":
        await state.clear()
        await message.answer("Заявка устарела, начните списание заново через /agents.")
        return
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "points.debit"):
        await state.clear()
        await message.answer("Право списания отозвано.")
        return
    reason = (message.text or "").strip()
    if not 5 <= len(reason) <= 200 or reason.startswith("/"):
        await message.answer("Введите понятную причину длиной 5–200 символов или /cancel.")
        return
    action["reason"] = reason
    await state.clear()
    label = "FRP" if action["which"] == "f" else "похвалы"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(style="success", text="✅ Подтвердить списание",
                              callback_data=f"agdo:{token}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"agview:{action['key']}:{action['page']}")]
    ])
    await message.answer(
        f"⚠️ <b>Списание 1 {label}</b>\n\n"
        f"👤 <code>{html.escape(action['nick'])}</code>\n"
        f"📋 Причина: {html.escape(reason)}\n\n"
        "Перед подтверждением бот повторно проверит право и баланс.",
        reply_markup=kb, parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("agdo:"))
async def agent_do_action(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "agents.view"):
        await callback.answer("Нет доступа", show_alert=True)
        return
    token = callback.data.removeprefix("agdo:")
    action = agent_action_tokens.get(token)
    if not action or action["uid"] != callback.from_user.id or action["chat_id"] != callback.message.chat.id:
        await callback.answer("Это действие уже недоступно. Откройте панель заново.", show_alert=True)
        return
    required = "points.grant" if action["kind"] == "g" else "points.debit"
    if not has_permission(callback.from_user.id, required):
        await callback.answer("Право на операцию отозвано.", show_alert=True)
        return
    if action["kind"] == "d" and (
            not 5 <= len(action.get("reason", "")) <= 200
            or not isinstance(action.get("target"), dict)):
        await callback.answer("Сначала укажите причину списания.", show_alert=True)
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
            await commit_agent_debit(
                firebase_client, ClientSession, op_id=action["op_id"],
                actor_id=callback.from_user.id,
                actor_nick=str(users_db.get(str(callback.from_user.id), {}).get("nick") or "Руководство"),
                sheet=sheet, target=action["target"], amount=1,
                reason=action["reason"], raw=action["nick"]
            )
        action["status"] = "done"
        activity = "panel_grant" if action["kind"] == "g" else "panel_debit"
        amount = "+" + str(action["qty"]) if action["kind"] == "g" else "-1"
        asyncio.create_task(record_admin_activity(activity, callback.from_user.id,
            f"{action['nick']}: {amount}; причина: {action['reason']}" if action["kind"] == "d" else f"{action['nick']}: {amount}", sheet))
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
            [InlineKeyboardButton(style="danger", text="🗑️ Да, удалить", callback_data=f"agdeleteok:{key}:{page}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"agview:{key}:{page}")]
        ])
        await callback.message.edit_text(
            f"🗑️ <b>FBI · Удаление агента</b>\n\n" f"Удалить <b>{html.escape(agent['nick'])}</b> из бота и таблиц?\n"
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
        asyncio.create_task(record_admin_activity("agent_deleted", callback.from_user.id,
            f"Удалён агент {agent['nick']}"))
        await callback.answer("✅ Запись удалена.")
        await agent_panel_list(callback.message, callback.from_user.id, int(page), edit=True)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
    except Exception as exc:
        logger.error(f"Could not remove agent safely: {exc}")
        await callback.answer("Ошибка удаления. Обновите /agents.", show_alert=True)


@dp.callback_query(F.data.startswith("agranks:"))
async def agent_rank_menu(callback: types.CallbackQuery):
    if not has_permission(callback.from_user.id, "agents.rank"):
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
    if not has_permission(callback.from_user.id, "agents.rank"):
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
        profile = users_db[agent["uid"]]
        previous = dict(profile)
        new_rank = "X" if rank == 10 else str(rank)
        old_rank = str(profile.get("rank") or "?")
        profile["rank"] = new_rank
        if old_rank != new_rank:
            history = profile.get("rank_history", [])
            history = list(history) if isinstance(history, list) else []
            profile["rank_history"] = (history + [{"from": old_rank, "to": new_rank,
                "at": datetime.now(timezone.utc).isoformat(),
                "actor_id": str(callback.from_user.id)}])[-50:]
        if not await sync_state_to_cloud():
            users_db[agent["uid"]] = previous
            await callback.answer("Firebase не подтвердила изменение.", show_alert=True)
            return
        save_json(USERS_FILE, users_db)
        asyncio.create_task(record_admin_activity("rank_changed", callback.from_user.id,
            f"{agent['nick']}: новый ранг {rank}"))
        await callback.answer("✅ Ранг обновлён")
        await agent_panel_detail(callback.message, key, int(page_raw))
    except Exception as exc:
        logger.warning(f"Rank change failed: {exc}")
        await callback.answer("⚠️ Не удалось обновить ранг", show_alert=True)


# Очередь на выдачу
def render_queue(rows: List[Dict[str, Any]]):
    pending = [r for r in rows if int(r.get("qty") or 0) > 0]
    if not pending:
        return "🎉 Очередь пуста! Все FRP выданы.", None

    text = f"📋 <b>FBI · Очередь на выдачу</b>\n\n" f"Категория: <code>/fractionrp</code> · ожидают: <b>{len(pending)}</b>\n\n"
    buttons = []
    for i, r in enumerate(pending[:15], 1):
        nick = str(r.get("nick", "Noname"))
        qty = int(r.get("qty") or 0)
        rank = str(r.get("rank", ""))
        rank_text = f" [{rank}]" if rank else ""
        text += (f"{i}. <b>{html.escape(nick)}</b>{html.escape(rank_text)} — <b>{qty} шт.</b>\n"
                 f"Команда: <code>/fractionrp {html.escape(nick)}</code>\n\n")
        buttons.append([InlineKeyboardButton(style="success", text=f"✓ Выдать 1 для {nick}", callback_data=f"issue_1_{nick}")])
    if len(pending) > 15:
        text += f"…Ещё {len(pending) - 15} игроков.\n"
    text += "<i>Список обновляется после каждого списания.</i>"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text == "📋 Очередь на выдачу (Админ)")
@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    if not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "agents.view"):
        await message.answer("Доступно только руководству.")
        return
    try:
        rows = await firebase_client.get_sheet("fractionrp")
        text, keyboard = render_queue(rows)
        await send_fbi_card(message, text, reply_markup=with_home_button(keyboard))
    except Exception as e:
        logger.error(f"Could not load issue queue: {e}")
        await message.answer("⚠️ Не удалось загрузить очередь из Firebase.")


@dp.callback_query(F.data.startswith("issue_1_"))
async def process_issue_one(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "points.debit"):
        await callback.answer("У вас нет прав!", show_alert=True)
        return
    nick = callback.data[len("issue_1_"):]
    try:
        remaining = await firebase_client.debit_one("fractionrp", nick)
        asyncio.create_task(record_admin_activity("point_issued", callback.from_user.id,
            f"{nick}: -1, осталось {remaining}", "fractionrp"))
        await callback.answer(
            f"✓ {nick}: осталось {remaining}" if remaining else f"{nick}: баллы закончились",
            show_alert=False
        )
        # Refresh original message; zero-quantity rows disappear completely.
        rows = await firebase_client.get_sheet("fractionrp")
        new_text, new_keyboard = render_queue(rows)
        await callback.message.edit_text(
            new_text, reply_markup=with_home_button(new_keyboard), parse_mode="HTML"
        )
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
    await send_fbi_card(
        message,
        "💬 <b>FBI · Центр поддержки</b>\n\n"
        "Опишите найденный баг, ошибку или предложение следующим сообщением.\n"
        "<i>Можно отправить текст, фотографию или видео с подписью — руководство получит вложение в Telegram.</i>",
        reply_markup=with_home_button(kb)
    )
    await state.set_state(SupportStates.waiting_for_message)

@dp.callback_query(F.data == "cancel_support")
async def cancel_support(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отправка обращения отменена.")

@dp.message(lambda message: (
    getattr(message, "from_user", None) is not None
    and isinstance(users_db.get(str(message.from_user.id)), dict)
    and users_db[str(message.from_user.id)].get("verification_status") == "needs_proof"
))
async def receive_claim_proof(message: types.Message):
    if getattr(message.chat, "type", "") != "private":
        return
    uid = str(message.from_user.id)
    if message.text and message.text.startswith("/"):
        await message.answer("Для подтверждения отправьте скриншот или ссылку.")
        return
    photo = bool(message.photo)
    video = bool(message.video)
    document = bool(message.document and
                    (message.document.mime_type or "").startswith("image/"))
    link = (message.text or "").strip()
    if not (photo or video or document or
            (link.startswith(("https://", "http://")) and len(link) <= 1000)):
        await message.answer(
            "Пришлите скриншот игрового профиля, фото, видео или ссылку http(s). "
            "Не отправляйте персональные документы."
        )
        return
    if photo:
        proof_kind, proof_file_id = "photo", message.photo[-1].file_id
    elif video:
        proof_kind, proof_file_id = "video", message.video.file_id
    elif document:
        proof_kind, proof_file_id = "document", message.document.file_id
    else:
        proof_kind, proof_file_id = "link", ""
    async with _claim_action_locks.setdefault(uid, asyncio.Lock()):
        profile = users_db.get(uid)
        if not isinstance(profile, dict) or profile.get("verification_status") != "needs_proof":
            return
        previous = dict(profile)
        updated = dict(profile)
        updated["verification_status"] = "proof_submitted"
        updated["proof_submitted"] = datetime.now(timezone.utc).isoformat()
        updated["proof_kind"] = proof_kind
        updated["proof_file_id"] = proof_file_id
        updated["proof_link"] = link if proof_kind == "link" else ""
        updated["proof_caption"] = (message.caption or "").strip()[:500]
        users_db[uid] = updated
        if not await sync_state_to_cloud():
            users_db[uid] = previous
            await message.answer("⚠️ Не получилось сохранить доказательства. Повторите позже.")
            return
        save_json(USERS_FILE, users_db)
    await message.answer(
        "✅ Доказательства отправлены руководству. Ожидайте решения."
    )
    await notify_claim_reviewers(uid, updated, evidence_message=message)



@dp.message(SupportStates.waiting_for_message)
async def process_support_msg(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    u_info = users_db.get(str(uid), {})
    nick = u_info.get("nick", "Аноним")
    rank = u_info.get("rank", "?")
    username_str = f"(@{message.from_user.username})" if message.from_user.username else ""

    content_text = (message.text or message.caption or "[Вложение без текста]")[:600]

    admin_msg = (
        "🚨 <b>FBI · Новое обращение</b>\n\n"
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
        "video_id": message.video.file_id if message.video else "",
        "status": "Новое",
        "source": "telegram"
    }
    ticket_id = await firebase_client.add_support_ticket(ticket_payload)
    ticket_label = f"#{ticket_id[-6:]}" if ticket_id else ""
    if not ticket_id:
        logger.warning("Support ticket was forwarded to leadership but not stored in Firestore")

    admin_msg_with_id = (
        f"🚨 <b>FBI · Обращение {ticket_label}</b>\n\n"
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
            await bot.send_message(adm_id, admin_msg_with_id[:3900], parse_mode="HTML")
            short_caption = f"📎 Вложение к обращению {ticket_label} от {nick}"[:180]
            if message.photo:
                await bot.send_photo(adm_id, message.photo[-1].file_id, caption=short_caption, parse_mode=None)
            elif message.video:
                await bot.send_video(adm_id, message.video.file_id, caption=short_caption, parse_mode=None)
        except Exception as e:
            logger.warning(f"Could not forward support message to {adm_id}: {e}")

    status_text = (
        "✅ <b>Обращение сохранено в базе и передано руководству.</b>"
        if ticket_id else
        "⚠️ <b>Обращение отправлено руководству в Telegram, но Firebase не подтвердил сохранение.</b> "
        "Попросите техадминистратора проверить правила и журнал."
    )
    await message.answer(status_text, reply_markup=get_main_menu(uid), parse_mode="HTML")

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


# --- Gemini-assisted admin commands ------------------------------------------
# Tokens are ephemeral: no API key, user IDs or Firestore balances go to Gemini.
# All operations require an authenticated human confirmation.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
AI_ACTIONS: Dict[str, Dict[str, Any]] = {}
_AI_LIMIT: Dict[int, List[float]] = {}
_AI_SEMAPHORE = asyncio.Semaphore(3)
AI_CONFIRM_TTL = 5 * 60


async def gemini_json_request(url: str, headers: dict, body: dict) -> dict:
    """Call Gemini with a bounded timeout and safe, actionable error messages."""
    async with _AI_SEMAPHORE:
        try:
            # Gemini 3.8 can spend time on internal reasoning; thinkingLevel=low
            # reduces latency. Keep a finite read timeout so polling stays live.
            async with ClientSession(
                timeout=ClientTimeout(total=45, connect=10, sock_read=40)
            ) as session:
                async with session.post(url, headers=headers, json=body) as response:
                    if response.status == 429:
                        raise ParseError("Лимит Gemini исчерпан. Попробуйте позже.")
                    if response.status in (401, 403):
                        raise ParseError("Gemini API-ключ недействителен или недоступен модели.")
                    if response.status == 404:
                        raise ParseError(f"Модель {MODEL} недоступна для этого API-ключа.")
                    if response.status == 400:
                        logger.warning("Gemini rejected request with HTTP 400; check model request schema")
                        raise ParseError("Gemini отклонил формат запроса. Обратитесь к владельцу бота.")
                    if response.status >= 500:
                        raise ParseError("Сервис Gemini временно недоступен. Попробуйте позже.")
                    if response.status != 200:
                        raise ParseError("Gemini не принял запрос. Проверьте доступ и лимиты API.")
                    return await response.json()
        except asyncio.TimeoutError as exc:
            logger.warning("Gemini request timed out after 45 seconds")
            raise ParseError(
                "Gemini слишком долго отвечает. Балансы не менялись. "
                "Попробуйте ещё раз или используйте формат: нортон,гоммез фрп +1."
            ) from exc


async def handle_natural_command(message: types.Message, raw: str):
    uid = message.from_user.id
    if (not is_admin(uid) or message.chat.type != "private"
            or message.chat.id != uid):
        await message.answer("⛔ ИИ-команды доступны только руководству в личном чате.")
        return
    raw = raw.strip()
    command_text, delimiter, debit_reason = raw.partition("|")
    debit_reason = debit_reason.strip()
    if not raw:
        await message.answer("Напишите команду, например: <code>норто, гоммез -3 фрп</code>",
                             parse_mode="HTML")
        return
    # Avoid unexpected API usage on an unbounded message flood.
    now = datetime.now(timezone.utc).timestamp()
    recent = [t for t in _AI_LIMIT.get(uid, []) if now - t < 60]
    if len(recent) >= 6:
        await message.answer("⏳ Максимум 6 разборов в минуту. Повторите позже.")
        return
    recent.append(now)
    _AI_LIMIT[uid] = recent
    try:
        plan, origin = await interpret_natural(command_text.strip(), GEMINI_API_KEY, gemini_json_request)
    except ParseError as exc:
        await message.answer("⚠️ " + html.escape(str(exc)))
        return
    except Exception as exc:
        logger.warning("Gemini parsing request failed: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось получить ответ Gemini. Попробуйте ещё раз.")
        return
    capability = "points.grant" if plan["action"] == "credit" else "points.debit"
    if not has_permission(uid, capability):
        await message.answer("⛔ Нет разрешения на эту операцию.")
        return
    if plan["action"] == "debit":
        if not delimiter or not 5 <= len(debit_reason) <= 200:
            await message.answer("⚠️ Укажите причину списания после | (5–200 символов).")
            return
        if plan["amount"] > debit_limit(uid):
            await message.answer(f"⛔ Лимит списания для вашей роли: {debit_limit(uid)}.")
            return
    try:
        # Memory is never sent to Gemini. Each operation uses the current sheet
        # AND persisted aliases from both sheets to revalidate identity.
        rows = await firebase_client.get_sheet(plan["sheet"])
        remembered = await load_alias_memory(firebase_client, ClientSession)
    except (AliasMemoryError, Exception) as exc:
        logger.warning("AI agent lookup/memory failed: %s", type(exc).__name__)
        await message.answer(
            "⚠️ Не удалось проверить актуальную таблицу и память ников Firebase. "
            "Никому ничего не начислено. Повторите позже."
        )
        return
    targets, errors = resolve_with_memory(
        plan["names"], rows, remembered, plan["sheet"]
    )
    if errors:
        await message.answer(
            "🔎 <b>Нужно уточнить ники:</b>\n" +
            "\n".join("• " + html.escape(err) for err in errors) +
            "\n\n<i>Изменений в Firebase нет. Укажите точные игровые ники.</i>",
            parse_mode="HTML"
        )
        return
    if plan["action"] == "debit":
        lacking = [t for t in targets if t["qty"] < plan["amount"]]
        if lacking:
            await message.answer(
                "⚠️ Недостаточно баллов для списания:\n" +
                "\n".join(f"• {html.escape(t['nick'])}: {t['qty']}"
                          for t in lacking) +
                "\n\n<i>Никому ничего не списано.</i>",
                parse_mode="HTML"
            )
            return

    # Keep only short-lived, confirmed and Telegram-identity-bound operations.
    for old_key, value in list(AI_ACTIONS.items()):
        if now - value["created"] > AI_CONFIRM_TTL:
            AI_ACTIONS.pop(old_key, None)
    token = uuid.uuid4().hex[:20]
    AI_ACTIONS[token] = {
        "uid": uid, "chat_id": message.chat.id,
        "created": now, "status": "new",
        "plan": plan, "targets": targets,
        "raw": command_text.strip(),
        "reason": debit_reason if plan["action"] == "debit" else "",
        "operation_id": "ai-" + uuid.uuid4().hex
    }
    sheet_label = "FRP" if plan["sheet"] == "fractionrp" else "похвал"
    sign = "+" if plan["action"] == "credit" else "−"
    summary = "\n".join(
        f"• <code>{html.escape(t['nick'])}</code>: "
        f"<b>{sign}{plan['amount']} {sheet_label}</b> "
        f"(сейчас {t['qty']})"
        for t in targets
    )
    source = "правила" if origin == "local" else "Gemini 3.8 Flash"
    buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить для всех", callback_data=f"aiconfirm:{token}",
            style="success")],
        [InlineKeyboardButton(
            text="❌ Отмена", callback_data=f"aicancel:{token}")]
    ])
    await message.answer(
        "🤖 <b>FBI · Проверка ИИ-команды</b>\n\n" +
        summary +
        (("\n📋 Причина: " + html.escape(debit_reason)) if plan["action"] == "debit" else "") +
        f"\n\n<blockquote>Агентов: <b>{len(targets)}</b>\n"
        f"Источник разбора: {source}</blockquote>\n"
        "<i>Firebase изменится только после нажатия «Подтвердить».</i>",
        reply_markup=buttons, parse_mode="HTML"
    )


@dp.message(Command("ai"))
async def cmd_ai(message: types.Message, state: FSMContext):
    if (not is_admin(message.from_user.id) or message.chat.type != "private"
            or message.chat.id != message.from_user.id):
        await message.answer("⛔ Доступно только руководству.")
        return
    text = (message.text or "").split(maxsplit=1)
    await state.clear()
    if len(text) > 1:
        await handle_natural_command(message, text[1])
        return
    await state.set_state(AiStates.waiting_for_text)
    await message.answer(
        "🤖 <b>FBI · ИИ-команды</b>\n\n"
        "Напишите: <code>норто, гоммез -3 фрп</code> "
        "или <code>дай гоммезу 2 похвалы</code>.\n\n"
        "<i>Команда будет показана для подтверждения. /cancel — отменить.</i>",
        parse_mode="HTML"
    )


@dp.message(AiStates.waiting_for_text)
async def receive_ai_command(message: types.Message, state: FSMContext):
    await state.clear()
    await handle_natural_command(message, message.text or "")


@dp.callback_query(F.data.startswith("aiconfirm:") | F.data.startswith("aicancel:"))
async def confirm_natural_command(callback: types.CallbackQuery):
    if (not is_admin(callback.from_user.id)
            or not isinstance(callback.message, types.Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    action, _, token = (callback.data or "").partition(":")
    task = AI_ACTIONS.get(token)
    if (not task or task["uid"] != callback.from_user.id
            or task["chat_id"] != callback.message.chat.id
            or datetime.now(timezone.utc).timestamp() - task["created"] > AI_CONFIRM_TTL
            or task["status"] != "new"):
        await callback.answer("Операция уже обработана или устарела.", show_alert=True)
        return
    if action == "aicancel":
        task["status"] = "cancelled"
        await callback.answer("Операция отменена.")
        await callback.message.edit_text("❌ ИИ-команда отменена. Балансы не менялись.")
        return
    if action != "aiconfirm":
        await callback.answer("Некорректное действие.", show_alert=True)
        return

    plan = task["plan"]
    capability = "points.grant" if plan["action"] == "credit" else "points.debit"
    if not has_permission(callback.from_user.id, capability):
        await callback.answer("Право на операцию отозвано.", show_alert=True)
        return
    if plan["action"] == "debit" and (
            not 5 <= len(task.get("reason", "")) <= 200 or
            plan["amount"] > debit_limit(callback.from_user.id)):
        await callback.answer("Причина или лимит списания не подтверждены.", show_alert=True)
        return
    # Claim the token before the first network await: double-click safe.
    task["status"] = "processing"
    await callback.answer("Проверяю Firebase и выполняю операцию...")
    plan = task["plan"]
    try:
        result = await apply_batch(
            firebase_client, plan["sheet"], task["targets"],
            plan["action"], plan["amount"], task["operation_id"],
            ClientSession, aliases=plan["names"], original_text=task["raw"]
        )
        task["status"] = "done"
    except BalanceConflict as exc:
        task["status"] = "stale"
        await callback.message.edit_text(
            "⚠️ <b>Операция отменена:</b> " + html.escape(str(exc)) +
            "\n\nБаланс или привязка изменились после проверки. Подайте команду заново.",
            parse_mode="HTML"
        )
        return
    except Exception as exc:
        # Never blindly repeat a possibly successful Firebase write.
        task["status"] = "uncertain"
        logger.warning("Natural-command write not confirmed: %s", type(exc).__name__)
        await callback.message.edit_text(
            "⚠️ Firebase не подтвердила результат. "
            "Сначала проверьте баланс в таблице; повторно нажимать нельзя."
        )
        return

    sign = "+" if plan["action"] == "credit" else "−"
    label = "FRP" if plan["sheet"] == "fractionrp" else "похвал"
    for target in task["targets"]:
        asyncio.create_task(record_admin_activity(
            "ai_grant" if plan["action"] == "credit" else "ai_debit",
            callback.from_user.id,
            f"{target['nick']}: {sign}{plan['amount']}; op={task['operation_id']}" +
            (f"; причина: {task['reason']}" if plan["action"] == "debit" else ""),
            plan["sheet"]
        ))
    try:
        await callback.message.edit_text(
            "✅ <b>Операция подтверждена Firebase</b>\n\n" +
            "\n".join(
                f"• <code>{html.escape(t['nick'])}</code>: "
                f"<b>{sign}{plan['amount']} {label}</b>"
                for t in task["targets"]
            ) +
            ("\n\n<i>Операция уже была учтена (повторно не применена).</i>"
             if result == "already_applied"
             else "\n\n<i>🧠 Подтверждённые сокращения ников сохранены в Firebase.</i>"),
            parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning("Cannot refresh AI confirmation: %s", type(exc).__name__)



# --- Independent agent operations: never mixed with leadership audit ---------
AGENT_CONFIRM_TTL = 300
AGENT_CONFIRMATIONS: Dict[str, Dict[str, Any]] = {}
_AGENT_LIMIT: Dict[int, List[float]] = {}
_AGENT_LOCKS: Dict[int, asyncio.Lock] = {}


def get_verified_agent(uid: int) -> Optional[Dict[str, Any]]:
    """Only a cloud-verified profile can change game balances."""
    if type(uid) is not int or not _cloud_state_hydrated:
        return None
    profile = users_db.get(str(uid))
    if (not isinstance(profile, dict)
            or not re.fullmatch(r"[A-Za-z0-9_]{3,30}",
                                str(profile.get("nick") or ""))
            or profile.get("verification_status") in (
                "pending", "needs_proof", "proof_submitted", "disabled",
                "disconnected", "retry_ready", "cooldown")):
        return None
    return profile


def _agent_private(message) -> bool:
    return (getattr(message.chat, "type", "") == "private"
            and message.chat.id == message.from_user.id)


def _agent_key_row(row):
    return {key: str(row.get(key) or "") for key in ("nick", "id", "profileId", "rank")} | {
        "qty": int(row.get("qty") or 0)
    }


def _agent_fuzzy_suggestion(alias: str, rows: List[Dict[str, Any]]):
    """Conservative unique suggestion, NEVER a silent authoritative match."""
    key = simplified(alias)
    if len(key) < 4:
        return None
    candidates = []
    for row in rows:
        nick = str(row.get("nick") or "")
        if not nick:
            continue
        tokens = [simplified(part) for part in nick.split("_")]
        score = max((difflib.SequenceMatcher(None, key, token).ratio()
                     for token in tokens if token), default=0.0)
        if score >= 0.75:
            candidates.append((score, row))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if (not candidates or candidates[0][0] < 0.75
            or (len(candidates) > 1 and
                candidates[0][0] - candidates[1][0] < 0.12)):
        return None
    return _agent_key_row(candidates[0][1])


async def _agent_parse_and_resolve(raw: str, *, forced_action: str = ""):
    """Parse one target only; never auto-select an ambiguous or stale alias."""
    plan, origin = await interpret_natural(raw, GEMINI_API_KEY, gemini_json_request)
    if forced_action and plan["action"] != forced_action:
        raise AgentOperationError("Знак операции не совпадает с командой.")
    if len(plan["names"]) != 1:
        raise AgentOperationError(
            "Агенту разрешена одна цель за операцию. Укажите один ник."
        )
    alias = plan["names"][0]
    rows = await firebase_client.get_sheet(plan["sheet"])
    memory = await load_alias_memory(firebase_client, ClientSession)
    results, errors = resolve_with_memory([alias], rows, memory, plan["sheet"])
    guessed = False
    if errors:
        # Never override a blocked/stale learned alias or an ambiguous match.
        if simplified(alias) not in memory and len(errors) == 1 and "не найден" in errors[0]:
            proposal = _agent_fuzzy_suggestion(alias, rows)
            if proposal:
                results, errors, guessed = [proposal], [], True
    if errors:
        raise AgentOperationError(
            "Нужно уточнить ник: " + "; ".join(errors) +
            ". Используйте полный игровой ник."
        )
    if len(results) != 1:
        raise AgentOperationError("Ник не удалось однозначно определить.")
    return plan, _agent_key_row(results[0]), alias, guessed, origin


def _format_agent_operation(plan, target, alias, guessed):
    label = "FRP" if plan["sheet"] == "fractionrp" else "похвал"
    signed = ("−" if plan["action"] == "debit" else "+") + str(plan["amount"])
    return (
        "👤 <b>FBI · Проверка действия агента</b>\n\n"
        f"🔎 Ввод: <code>{html.escape(alias)}</code>\n"
        f"🎮 <b>Найденный ник:</b> <code>{html.escape(target['nick'])}</code>\n"
        f"📌 Баланс: <b>{target['qty']}</b>\n"
        f"🧮 Действие: <b>{signed} {label}</b>\n"
        + ("⚠️ <b>Совпадение приблизительное!</b> "
           "Проверьте полный ник перед подтверждением.\n" if guessed else "")
    )


async def agent_operation_from_text(message: types.Message, raw: str,
                                    *, forced_action=""):
    uid = message.from_user.id
    if not _agent_private(message):
        await message.answer("Действия агентов доступны только в личном чате.")
        return
    profile = get_verified_agent(uid)
    if not profile:
        await message.answer(
            "⛔ Для управления баллами нужна подтверждённая регистрация. /start"
        )
        return
    now = datetime.now(timezone.utc).timestamp()
    recent = [t for t in _AGENT_LIMIT.get(uid, []) if now - t < 60]
    if len(recent) >= 5:
        await message.answer("⏳ Не более 5 новых операций в минуту.")
        return
    _AGENT_LIMIT[uid] = [*recent, now]
    command_text, separator, debit_reason = raw.partition("|")
    debit_reason = debit_reason.strip()
    if forced_action == "debit" and not has_permission(uid, "points.debit"):
        await message.answer("⛔ Списание доступно руководству или агентам с отдельным разрешением.")
        return
    try:
        plan, target, alias, guessed, origin = await _agent_parse_and_resolve(
            command_text.strip(), forced_action=forced_action
        )
    except (ParseError, AgentOperationError) as exc:
        await message.answer("⚠️ " + html.escape(str(exc)))
        return
    except Exception as exc:
        logger.warning("Agent command resolution failed: %s", type(exc).__name__)
        await message.answer(
            "⚠️ Не удалось проверить запрос в Firebase или Gemini. "
            "Балансы не изменились."
        )
        return
    if plan["action"] == "debit":
        if not has_permission(uid, "points.debit"):
            await message.answer("⛔ Для списания нужно разрешение руководства.")
            return
        if not separator or not 5 <= len(debit_reason) <= 200:
            await message.answer("⚠️ Для списания укажите причину после | (5–200 символов).")
            return
        if plan["amount"] > debit_limit(uid):
            await message.answer(f"⚠️ Максимум за операцию для вашей роли: {debit_limit(uid)}.")
            return
    if plan["action"] == "debit" and target["qty"] < plan["amount"]:
        await message.answer(
            "⚠️ Недостаточно баллов: сейчас " + str(target["qty"])
        )
        return
    token = uuid.uuid4().hex[:20]
    AGENT_CONFIRMATIONS[token] = {
        "uid": uid, "chat_id": message.chat.id, "created": now, "status": "new",
        "plan": plan, "target": target, "alias": alias,
        "guessed": guessed, "raw": command_text.strip(),
        "reason": debit_reason if plan["action"] == "debit" else "",
        "op_id": "agent-" + uuid.uuid4().hex
    }
    for old_token, value in list(AGENT_CONFIRMATIONS.items()):
        if now - value["created"] > AGENT_CONFIRM_TTL:
            AGENT_CONFIRMATIONS.pop(old_token, None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("✅ Подтвердить списание" if plan["action"] == "debit"
                  else "📎 Продолжить заявку"),
            callback_data=f"agentop:confirm:{token}", style="success")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"agentop:cancel:{token}")]
    ])
    text = _format_agent_operation(plan, target, alias, guessed)
    if plan["action"] == "debit":
        text += "\n📋 Причина: " + html.escape(debit_reason) + "\n"
    text += (
        "\n<i>После подтверждения баллы спишутся без решения руководства.</i>"
        if plan["action"] == "debit"
        else "\n<i>Для начисления нужны причина или доказательства; "
             "баланс изменится только после одобрения руководством.</i>"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


def parse_agent_slash(raw: str, action: str) -> str:
    """Accept /min frp Nick -2 and /plus praise Nick +2, one target at a time."""
    match = re.fullmatch(
        r"\s*(frp|fractionrp|praise|фрп|похвал[аы]?)\s+"
        r"([A-Za-zА-Яа-яЁё0-9_]{2,40})\s+([+-])\s*(\d{1,2})\s*",
        raw, re.I
    )
    if not match:
        raise AgentOperationError(
            "Формат: /min frp Nick_Name -2 или /plus praise Nick_Name +2."
        )
    category, nickname, sign, amount = match.groups()
    if sign != ("-" if action == "debit" else "+"):
        raise AgentOperationError("Неверный знак: для /min нужен −, для /plus +.")
    category = "praise" if category.casefold().startswith(("praise", "похвал")) else "frp"
    return f"{nickname} {sign}{amount} {category}"


@dp.message(Command("min", "plus"))
async def cmd_agent_min_plus(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        await message.answer(
            "⏳ Бот ожидает данные текущего действия. "
            "Сначала завершите его или используйте /cancel."
        )
        return
    command, *remainder = (message.text or "").split(maxsplit=1)
    kind = command.split("@", 1)[0].lstrip("/").casefold()
    if not remainder:
        await message.answer(
            f"📝 <b>FBI · {'Списание' if kind == 'min' else 'Запрос начисления'}</b>\n\n"
            f"<code>/{kind} frp Gomez_Addams {'-2' if kind == 'min' else '+2'}{' | причина' if kind == 'min' else ''}</code>\n"
            f"<code>/{kind} praise Gomez_Addams {'-1' if kind == 'min' else '+1'}{' | причина' if kind == 'min' else ''}</code>",
            parse_mode="HTML"
        )
        return
    try:
        command_text, delimiter, reason = remainder[0].partition("|")
        raw = parse_agent_slash(command_text, "debit" if kind == "min" else "credit")
        if delimiter:
            raw += " | " + reason.strip()
    except AgentOperationError as exc:
        await message.answer("⚠️ " + html.escape(str(exc)))
        return
    await agent_operation_from_text(
        message, raw, forced_action="debit" if kind == "min" else "credit"
    )


@dp.callback_query(F.data.startswith("agentop:"))
async def agent_operation_confirm(callback: types.CallbackQuery, state: FSMContext):
    if (not isinstance(callback.message, types.Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Доступно только в личном чате.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("confirm", "cancel"):
        await callback.answer("Неверная операция.", show_alert=True)
        return
    mode, token = parts[1], parts[2]
    uid = callback.from_user.id
    async with _AGENT_LOCKS.setdefault(uid, asyncio.Lock()):
        item = AGENT_CONFIRMATIONS.get(token)
        if (not item or item["uid"] != uid
                or item["chat_id"] != callback.message.chat.id
                or item["status"] != "new"
                or datetime.now(timezone.utc).timestamp() - item["created"]
                    > AGENT_CONFIRM_TTL):
            await callback.answer("Операция устарела или уже обработана.", show_alert=True)
            return
        if mode == "cancel":
            item["status"] = "cancelled"
            await callback.answer("Отменено.")
            await callback.message.edit_text("❌ Действие агента отменено.")
            return
        if await state.get_state() is not None:
            await callback.answer("Сначала завершите текущее действие.", show_alert=True)
            return
        profile = get_verified_agent(uid)
        if not profile:
            await callback.answer("Профиль ожидает подтверждения.", show_alert=True)
            return
        plan = item["plan"]
        if plan["action"] == "debit" and (
                not has_permission(uid, "points.debit")
                or plan["amount"] > debit_limit(uid)
                or not 5 <= len(item.get("reason", "")) <= 200):
            await callback.answer("Право, лимит или причина списания не подтверждены.", show_alert=True)
            return
        item["status"] = "processing"
        await callback.answer("Проверяю данные Firebase...")
        if plan["action"] == "credit":
            await state.update_data(agent_add={
                "plan": plan, "target": item["target"], "alias": item["alias"],
                "raw": item["raw"], "nonce": uuid.uuid4().hex[:16]
            })
            await state.set_state(AgentActionStates.waiting_add_evidence)
            item["status"] = "sent_to_form"
            await callback.message.answer(
                "📎 <b>FBI · Заявка на начисление</b>\n\n"
                + _format_agent_operation(plan, item["target"],
                                          item["alias"], item["guessed"])
                + "\n<b>Отправьте причину или доказательство:</b>\n"
                  "• Текст с причиной\n"
                  "• Фото или видео игрового доказательства\n"
                  "• Ссылку на игровой отчёт\n\n"
                  "<i>Пароли и личные документы не отправляйте. "
                  "Для отмены: /cancel.</i>",
                parse_mode="HTML"
            )
            return
        try:
            status, audit = await commit_agent_debit(
                firebase_client, ClientSession,
                op_id=item["op_id"], actor_id=uid,
                actor_nick=profile["nick"],
                sheet=plan["sheet"], target=item["target"],
                amount=plan["amount"], alias=item["alias"], raw=item["raw"],
                reason=item["reason"]
            )
            item["status"] = "done"
        except AgentOperationError as exc:
            item["status"] = "failed"
            await callback.message.edit_text(
                "⚠️ <b>Списание не выполнено:</b> " + html.escape(str(exc)),
                parse_mode="HTML"
            )
            return
        except Exception as exc:
            item["status"] = "uncertain"
            logger.warning("Agent debit status uncertain: %s", type(exc).__name__)
            await callback.message.edit_text(
                "⚠️ Firebase не подтвердила списание. "
                "Проверьте баланс и отдельный журнал перед повтором."
            )
            return
        await callback.message.edit_text(
            "✅ <b>FBI · Агентское списание</b>\n\n"
            f"🎮 <code>{html.escape(item['target']['nick'])}</code>\n"
            f"🧮 −{plan['amount']} {'FRP' if plan['sheet'] == 'fractionrp' else 'похвал'}\n"
            f"📊 Было: {audit['before']} · Стало: {audit['after']}\n"
            f"📋 Причина: {html.escape(item['reason'])}\n\n"
            "📜 Действие сохранено в отдельном журнале агентов.",
            parse_mode="HTML"
        )


@dp.message(AgentActionStates.waiting_add_evidence)
async def collect_agent_add_evidence(message: types.Message, state: FSMContext):
    if not _agent_private(message) or not get_verified_agent(message.from_user.id):
        await message.answer("⛔ Подтвердите свою регистрацию.")
        return
    data = (await state.get_data()).get("agent_add")
    if not isinstance(data, dict):
        await state.clear()
        await message.answer("Форма устарела. Начните заново.")
        return
    reason = (message.text or message.caption or "").strip()
    kind = ""
    evidence_id = ""
    if message.photo:
        kind, evidence_id = "photo", message.photo[-1].file_id
    elif message.video:
        kind, evidence_id = "video", message.video.file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        kind, evidence_id = "document", message.document.file_id
    elif reason.startswith(("https://", "http://")) and len(reason) <= 700:
        kind, evidence_id = "link", reason
    if not reason and not kind:
        await message.answer("Отправьте причину текстом, фото, видео или ссылку.")
        return
    if len(reason) > 700:
        await message.answer("Слишком длинная причина, максимум 700 символов.")
        return
    data.update({
        "reason": reason if kind != "link" else "",
        "evidence_kind": kind,
        "evidence_id": evidence_id,
        "evidence_caption": reason if kind in ("photo", "video", "document") else ""
    })
    await state.update_data(agent_add=data)
    await state.set_state(AgentActionStates.confirm_add)
    plan = data["plan"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Отправить руководству",
                              callback_data=f"agentaddsend:{data['nonce']}", style="success")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="agentaddcancel")]
    ])
    await message.answer(
        "📋 <b>FBI · Проверка заявки</b>\n\n"
        f"🎮 <code>{html.escape(data['target']['nick'])}</code>\n"
        f"➕ {plan['amount']} {'FRP' if plan['sheet'] == 'fractionrp' else 'похвал'}\n"
        f"📝 Причина: {html.escape(reason[:200] or '[вложение]')}\n\n"
        "<i>Баланс не изменится до одобрения руководством.</i>",
        parse_mode="HTML", reply_markup=kb
    )


@dp.callback_query(F.data.startswith("agentaddsend:") | (F.data == "agentaddcancel"))
async def submit_agent_add_request(callback: types.CallbackQuery, state: FSMContext):
    if (not isinstance(callback.message, types.Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    uid = callback.from_user.id
    async with _AGENT_LOCKS.setdefault(uid, asyncio.Lock()):
        if await state.get_state() != AgentActionStates.confirm_add.state:
            await callback.answer("Заявка уже отправлена или устарела.", show_alert=True)
            return
        data = (await state.get_data()).get("agent_add")
        if not isinstance(data, dict):
            await callback.answer("Данные заявки отсутствуют.", show_alert=True)
            return
        if callback.data == "agentaddcancel":
            await state.clear()
            await callback.answer("Отменено.")
            await callback.message.edit_text("❌ Заявка отменена.")
            return
        if callback.data != f"agentaddsend:{data['nonce']}":
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        profile = get_verified_agent(uid)
        if not profile:
            await callback.answer("Профиль не подтверждён.", show_alert=True)
            return
        await state.clear()  # Single-use form; no second write on double-click.
        await callback.answer("Сохраняю заявку...")
        plan = data["plan"]
        try:
            req = await create_add_request(
                firebase_client, ClientSession,
                actor_id=uid, actor_nick=profile["nick"],
                sheet=plan["sheet"], target=data["target"],
                amount=plan["amount"], reason=data.get("reason", ""),
                evidence_kind=data.get("evidence_kind", ""),
                evidence_id=data.get("evidence_id", ""),
                evidence_caption=data.get("evidence_caption", ""),
                raw=data.get("raw", ""), target_alias=data.get("alias", "")
            )
        except Exception as exc:
            logger.warning("Agent add request not confirmed: %s", type(exc).__name__)
            await callback.message.edit_text(
                "⚠️ Сохранение заявки не подтверждено. "
                "Проверьте её наличие у руководства перед повтором."
            )
            return
        await callback.message.edit_text(
            "✅ <b>Заявка отправлена руководству</b>\n\n"
            f"ID: <code>{html.escape(req['id'])}</code>\n"
            f"Ник: <code>{html.escape(req['target']['nick'])}</code>\n"
            "Начисление ожидает решения. Это не обычный FRP-отчёт.",
            parse_mode="HTML"
        )
        await notify_agent_request_reviewers(req)


def _agent_request_markup(req_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить начисление",
                              callback_data=f"agentreview:approve:{req_id}", style="success")],
        [InlineKeyboardButton(text="❌ Отклонить",
                              callback_data=f"agentreview:reject:{req_id}", style="danger")]
    ])


def _agent_request_text(req):
    return (
        "📥 <b>FBI · Заявка агента на начисление</b>\n\n"
        f"👤 От: <code>{html.escape(str(req['actor_nick']))}</code>"
        f" (ID <code>{int(req['actor_id'])}</code>)\n"
        f"🎮 Кому: <code>{html.escape(str(req['target']['nick']))}</code>\n"
        f"➕ {int(req['amount'])} {'FRP' if req['sheet'] == 'fractionrp' else 'похвал'}\n"
        f"📝 Причина: {html.escape(str(req.get('reason') or '[вложение]')[:600])}\n"
        f"📎 Вложение: {html.escape(str(req.get('evidence_kind') or 'нет'))}\n\n"
        "Решение руководства обязательно. Журнал действий агента отдельный."
    )


async def _forward_agent_evidence(chat_id, req):
    kind, file_id = req.get("evidence_kind"), req.get("evidence_id")
    if not kind or not file_id:
        return
    try:
        if kind == "photo":
            await bot.send_photo(chat_id=chat_id, photo=file_id)
        elif kind == "video":
            await bot.send_video(chat_id=chat_id, video=file_id)
        elif kind == "document":
            await bot.send_document(chat_id=chat_id, document=file_id)
        elif kind == "link":
            await bot.send_message(chat_id, "🔗 " + html.escape(str(file_id)[:700]),
                                   parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:
        logger.warning("Agent evidence delivery failed: %s", type(exc).__name__)


async def notify_agent_request_reviewers(req):
    recipients = sorted(set(OWNER_TELEGRAM_IDS) | (
        set(admins_db) if _cloud_state_hydrated else set()
    ))
    for uid in recipients:
        if not is_admin(uid):
            continue
        try:
            await bot.send_message(uid, _agent_request_text(req),
                                   reply_markup=_agent_request_markup(req["id"]),
                                   parse_mode="HTML")
            await _forward_agent_evidence(uid, req)
        except Exception as exc:
            logger.warning("Cannot deliver agent request: %s", type(exc).__name__)


@dp.message(Command("agentpending"))
async def cmd_agent_pending(message: types.Message):
    if not _agent_private(message) or not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "reports.review"):
        await message.answer("⛔ Доступно только руководству в личном чате.")
        return
    try:
        requests = await list_collection(
            firebase_client, ClientSession, "agent_add_requests", limit=100
        )
    except Exception as exc:
        logger.warning("Agent requests unavailable: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось открыть заявки агентов.")
        return
    pending = [r for r in requests if r.get("status") == "pending"]
    if not pending:
        await message.answer("✅ Нет ожидающих заявок агентов.")
        return
    await message.answer(f"📥 <b>Заявки агентов:</b> {len(pending)}",
                         parse_mode="HTML")
    for req in pending[:20]:
        await message.answer(
            _agent_request_text(req),
            reply_markup=_agent_request_markup(req["id"]),
            parse_mode="HTML"
        )
        await _forward_agent_evidence(message.chat.id, req)


@dp.callback_query(F.data.startswith("agentreview:"))
async def review_agent_add_request(callback: types.CallbackQuery):
    if (not isinstance(callback.message, types.Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id
            or not is_admin(callback.from_user.id) or not has_permission(callback.from_user.id, "reports.review")):
        await callback.answer("Нет прав руководства.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("approve", "reject"):
        await callback.answer("Неверная заявка.", show_alert=True)
        return
    decision, req_id = parts[1:]
    try:
        req = await get_add_request(firebase_client, ClientSession, req_id)
        if req.get("status") != "pending":
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        requester = get_verified_agent(int(req["actor_id"]))
        if decision == "approve" and (
            not requester or requester.get("nick", "").casefold()
                             != str(req.get("actor_nick") or "").casefold()
        ):
            await callback.answer(
                "Профиль заявителя изменился. Начисление запрещено.",
                show_alert=True
            )
            return
        result, processed = await decide_add_request(
            firebase_client, ClientSession, request_id=req_id,
            reviewer_id=callback.from_user.id, decision=decision
        )
    except AgentOperationError as exc:
        await callback.answer(html.escape(str(exc))[:180], show_alert=True)
        return
    except Exception as exc:
        logger.warning("Agent credit review uncertain: %s", type(exc).__name__)
        await callback.answer(
            "Firebase не подтвердила решение. Проверьте отдельный журнал.",
            show_alert=True
        )
        return
    if result == "already_decided":
        await callback.answer("Заявка уже рассмотрена.", show_alert=True)
        return
    await callback.answer("Решение записано в Firebase.")
    status = "✅ Начислено" if decision == "approve" else "❌ Отклонено"
    await callback.message.edit_text(
        f"{status} · заявка <code>{html.escape(req_id)}</code>\n"
        f"🎮 <code>{html.escape(processed['target']['nick'])}</code>\n"
        f"Решение руководителя: <code>{callback.from_user.id}</code>",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            int(processed["actor_id"]),
            f"{status} · <b>Заявка агента</b>\n"
            f"ID: <code>{html.escape(req_id)}</code>\n"
            f"Ник: <code>{html.escape(processed['target']['nick'])}</code>\n"
            f"Количество: +{processed['amount']} "
            f"{'FRP' if processed['sheet'] == 'fractionrp' else 'похвал'}",
            parse_mode="HTML"
        )
    except Exception as exc:
        logger.info("Could not notify agent of request outcome: %s", type(exc).__name__)


@dp.message(Command("agentlogs"))
async def cmd_agent_logs(message: types.Message):
    if not _agent_private(message) or not is_admin(message.from_user.id) or not has_permission(message.from_user.id, "agents.view"):
        await message.answer("⛔ Журнал действий доступен только руководству.")
        return
    try:
        records = await list_collection(
            firebase_client, ClientSession, "agent_action_logs", limit=60
        )
    except Exception as exc:
        logger.warning("Agent audit read failed: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось получить журнал агентов.")
        return
    if not records:
        await message.answer("📜 Журнал действий агентов пока пуст.")
        return
    for offset in range(0, min(len(records), 30), 10):
        text = ["📜 <b>Отдельный журнал действий агентов</b>\n"]
        for entry in records[offset:offset + 10]:
            actor, target = (html.escape(str(entry.get("actor_nick") or "?")),
                             html.escape(str(entry.get("target_nick") or "?")))
            action = html.escape(str(entry.get("action") or "?"))
            kind = "FRP" if entry.get("sheet") == "fractionrp" else "похвалы"
            text.append(
                f"• {actor} → {target}: "
                f"{int(entry.get('amount') or 0):+d} {kind}\n"
                f"  {action} · {html.escape(str(entry.get('created_at') or '')[:16])}"
            )
        await message.answer("\n".join(text), parse_mode="HTML")



# --- Signed-leadership-only news and release announcements -------------------
_NEWS_SEND_LOCKS: Dict[int, asyncio.Lock] = {}
_NEWS_BROADCAST_TASKS: set = set()


def _news_recipients():
    """All known private Telegram users except intentionally blocked profiles."""
    targets = set()
    for raw_uid, profile in users_db.items():
        if not isinstance(profile, dict) or not str(raw_uid).isdecimal():
            continue
        uid = int(raw_uid)
        if uid <= 0 or profile.get("verification_status") in ("disabled", "cooldown"):
            continue
        targets.add(uid)
    return sorted(targets)


def _news_content(raw):
    raw = (raw or "").strip()
    category = "news"
    marker = re.match(r"^(?:update|обновление)\s*:\s*", raw, re.I)
    if marker:
        category, raw = "update", raw[marker.end():].strip()
    if not 1 <= len(raw) <= 3000:
        raise AgentOperationError(
            "Новость должна содержать от 1 до 3000 символов."
        )
    title = "Обновление системы FBI" if category == "update" else "Новости FBI"
    return title, category, raw


def _news_message(title, category, message):
    icon = "🔔" if category == "update" else "📰"
    return (
        f"{icon} <b>FBI · {html.escape(title)}</b>\n\n"
        + html.escape(message)
        + "\n\n<i>Официальное сообщение руководства FBI.</i>"
    )


@dp.message(Command("news"))
async def cmd_news(message: types.Message, state: FSMContext):
    if not _agent_private(message) or not is_leader(message.from_user.id) or not has_permission(message.from_user.id, "news.publish"):
        await message.answer(
            "⛔ Публикация новостей доступна только владельцу и действующему лидеру."
        )
        return
    if await state.get_state() is not None:
        await message.answer(
            "⏳ Завершите текущее действие или используйте /cancel."
        )
        return
    words = (message.text or "").split(maxsplit=1)
    if len(words) == 2:
        await show_news_preview(message, state, words[1])
        return
    await state.set_state(NewsStates.waiting_for_text)
    await message.answer(
        "📰 <b>FBI · Рассылка новостей</b>\n\n"
        "Отправьте следующим сообщением текст новости.\n"
        "Для объявления обновления начните текст с "
        "<code>обновление:</code>\n\n"
        "<i>Перед отправкой всем пользователям появится предварительный "
        "просмотр с подтверждением. /cancel — отменить.</i>",
        parse_mode="HTML"
    )


async def show_news_preview(message, state, raw):
    try:
        title, category, body = _news_content(raw)
    except AgentOperationError as exc:
        await message.answer("⚠️ " + html.escape(str(exc)))
        return
    recipients = _news_recipients()
    if not recipients:
        await message.answer("Нет зарегистрированных адресатов рассылки.")
        return
    nonce = uuid.uuid4().hex[:16]
    await state.update_data(news={
        "title": title, "category": category, "message": body,
        "nonce": nonce, "recipients": recipients
    })
    await state.set_state(NewsStates.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📣 Отправить {len(recipients)} пользователям",
            callback_data=f"newspublish:{nonce}", style="success")],
        [InlineKeyboardButton(
            text="❌ Отмена", callback_data="newscancel")]
    ])
    await message.answer(
        _news_message(title, category, body)
        + f"\n\n<blockquote>Адресатов: {len(recipients)}\n"
          "Это только предварительный просмотр.</blockquote>",
        parse_mode="HTML", reply_markup=kb
    )


@dp.message(NewsStates.waiting_for_text)
async def receive_news_text(message: types.Message, state: FSMContext):
    if not _agent_private(message) or not is_leader(message.from_user.id):
        await state.clear()
        await message.answer("Нет прав на рассылку.")
        return
    if not message.text:
        await message.answer("Отправьте новость текстом, максимум 3000 символов.")
        return
    await show_news_preview(message, state, message.text)


async def _broadcast_fbi_news(actor_uid, news, recipients):
    """Deliver at bounded speed, keep an independent delivery journal."""
    sent, failed = 0, 0
    final_text = _news_message(news["title"], news["category"], news["message"])
    for uid in recipients:
        try:
            await bot.send_message(uid, final_text, parse_mode="HTML",
                                   disable_web_page_preview=True)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.info("News delivery to one user failed (%s)", type(exc).__name__)
        # Telegram's global send limit: deliberately stay below ~20 messages/sec.
        await asyncio.sleep(0.07)
    try:
        await finish_announcement(
            firebase_client, ClientSession, news["id"],
            sent=sent, failed=failed
        )
    except Exception as exc:
        logger.warning("Could not finalize news journal: %s", type(exc).__name__)
    try:
        await bot.send_message(
            actor_uid,
            f"📣 <b>Рассылка завершена</b>\n\n"
            f"ID: <code>{html.escape(news['id'])}</code>\n"
            f"✅ Доставлено: {sent}\n⚠️ Ошибок доставки: {failed}\n\n"
            "Журнал: /newslog",
            parse_mode="HTML"
        )
    except Exception as exc:
        logger.info("Could not deliver broadcast summary: %s", type(exc).__name__)


@dp.callback_query(F.data.startswith("newspublish:") | (F.data == "newscancel"))
async def confirm_news_publish(callback: types.CallbackQuery, state: FSMContext):
    if (not isinstance(callback.message, types.Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id
            or not is_leader(callback.from_user.id) or not has_permission(callback.from_user.id, "news.publish")):
        await callback.answer("Недостаточно прав для рассылки.", show_alert=True)
        return
    uid = callback.from_user.id
    async with _NEWS_SEND_LOCKS.setdefault(uid, asyncio.Lock()):
        if await state.get_state() != NewsStates.confirm.state:
            await callback.answer("Рассылка уже подтверждена или отменена.",
                                  show_alert=True)
            return
        data = (await state.get_data()).get("news")
        if not isinstance(data, dict):
            await callback.answer("Устаревший черновик.", show_alert=True)
            return
        if callback.data == "newscancel":
            await state.clear()
            await callback.answer("Отменено.")
            await callback.message.edit_text("❌ Публикация отменена.")
            return
        if callback.data != f"newspublish:{data['nonce']}":
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        recipients = [int(x) for x in data["recipients"] if int(x) > 0]
        try:
            news = await create_announcement(
                firebase_client, ClientSession, actor_id=uid,
                title=data["title"], category=data["category"],
                message=data["message"], recipients=len(recipients),
                news_id="news-" + data["nonce"]
            )
        except AgentOperationError as exc:
            # Keep the preview/confirmation state intact on a failed write,
            # so the owner can publish the right Firestore Rules and retry.
            # No recipients were contacted and no balance state was changed.
            logger.warning("News persistence blocked: %s", exc)
            reason = str(exc)
            await callback.answer(
                ("⚠️ Новость не отправлена. " + reason)[:195],
                show_alert=True
            )
            return
        except Exception as exc:
            logger.warning("News persistence exception: %s", type(exc).__name__)
            await callback.answer(
                "⚠️ Firebase не подтвердила запись. "
                "Новость не рассылалась; проверьте /newslog.",
                show_alert=True
            )
            return
        # If this nonce was already committed, we cannot prove whether
        # individual recipients were delivered before a Render restart.
        # Require review rather than risking a duplicate broadcast.
        if news.get("_already_persisted"):
            await state.clear()
            await callback.answer(
                "Эта новость уже зарегистрирована. Проверьте /newslog; "
                "повторно рассылать её автоматически нельзя.",
                show_alert=True
            )
            await callback.message.edit_text(
                "⚠️ <b>Новость уже сохранена в Firebase</b>\n\n"
                f"ID: <code>{html.escape(news['id'])}</code>\n"
                "Автоматическая повторная отправка заблокирована, "
                "чтобы пользователи не получили дубли. Проверьте /newslog.",
                parse_mode="HTML"
            )
            return
        # Only clear draft after the durable journal is confirmed.
        # With the same nonce, a retry cannot generate another journal record.
        await state.clear()
        await callback.answer("Рассылка сохранена, начинаю отправку.")
        await callback.message.edit_text(
            "📣 <b>Рассылка началась</b>\n\n"
            f"Новость <code>{html.escape(news['id'])}</code>\n"
            f"Адресатов: {len(recipients)}\n\n"
            "По окончании придёт отчёт о доставке.",
            parse_mode="HTML"
        )
        task = asyncio.create_task(_broadcast_fbi_news(uid, news, recipients))
        _NEWS_BROADCAST_TASKS.add(task)
        task.add_done_callback(_NEWS_BROADCAST_TASKS.discard)


def newslog_list_keyboard(entries, page: int, pages: int):
    buttons = []
    for entry in entries:
        title = str(entry.get("title") or "Новость").replace("\n", " ")[:29]
        date = news_date(entry.get("created_at")).split(" ", 1)[0]
        icon = {"delivered": "✅", "partial": "⚠️", "sending": "⏳"}.get(
            str(entry.get("status") or ""), "❔"
        )
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {title[:24]} · {date} · #{entry['id'][-4:]}",
            callback_data=f"newslog:view:{entry['id']}:{page}"
        )])
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"newslog:page:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"newslog:page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"newslog:page:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_newslog_list(message: types.Message, *, page: int = 0, edit: bool = False):
    """Read current cloud records. Callbacks do not depend on process memory."""
    records = await list_collection(
        firebase_client, ClientSession, "fbi_announcements", limit=NEWSLOG_FETCH_LIMIT
    )
    text, entries, page, pages = newslog_list(records, page)
    markup = newslog_list_keyboard(entries, page, pages)
    if edit:
        await message.edit_text(text, reply_markup=markup, parse_mode=None)
    else:
        await message.answer(text, reply_markup=markup, parse_mode=None)


@dp.message(Command("newslog"))
async def cmd_news_log(message: types.Message):
    if not _agent_private(message) or not is_leader(message.from_user.id):
        await message.answer("⛔ Журнал рассылок доступен владельцу и лидеру.")
        return
    try:
        await show_newslog_list(message)
    except Exception as exc:
        logger.warning("News journal unavailable: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось загрузить журнал новостей из Firebase.")


@dp.callback_query(F.data.startswith("newslog:"))
async def newslog_callback(callback: types.CallbackQuery):
    """Every click rechecks the caller and Firestore; no cached role or entry."""
    if (not isinstance(callback.message, types.Message)
            or callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id
            or not is_leader(callback.from_user.id)):
        await callback.answer("⛔ Доступно только владельцу и лидеру в личном чате.",
                              show_alert=True)
        return
    data = callback.data or ""
    page_match = re.fullmatch(r"newslog:page:(\d{1,3})", data)
    view_match = re.fullmatch(r"newslog:view:(news-[a-f0-9]{16,32}):(\d{1,3})", data)
    if not page_match and not view_match:
        await callback.answer("⚠️ Некорректная или устаревшая кнопка.", show_alert=True)
        return
    try:
        if page_match:
            await show_newslog_list(callback.message, page=int(page_match.group(1)), edit=True)
        else:
            news_id, page = view_match.group(1), int(view_match.group(2))
            item = await get_announcement(firebase_client, ClientSession, news_id)
            if not item:
                await callback.answer("⚠️ Новость больше не найдена.", show_alert=True)
                return
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К списку", callback_data=f"newslog:page:{page}"),
                 InlineKeyboardButton(text="🔄 Обновить", callback_data=data)]
            ])
            detail = newslog_detail(item, users_db)
            if text_size_utf16(detail) > 3900:
                metadata = newslog_detail(item, users_db, include_text=False)
                await callback.message.edit_text(
                    metadata + "\n\n📝 Полный текст новости отправлен отдельным сообщением ниже.",
                    parse_mode=None, reply_markup=kb
                )
                for part in split_telegram_text(str(item.get("message") or "(нет текста)")):
                    await callback.message.answer(part, parse_mode=None)
            else:
                await callback.message.edit_text(detail, reply_markup=kb, parse_mode=None)
        await callback.answer()
    except Exception as exc:
        logger.warning("Newslog callback failed: %s", type(exc).__name__)
        await callback.answer("⚠️ Не удалось открыть новость. Повторите попытку.",
                              show_alert=True)



# ─── FBI event planning and scheduled notifications ──────────────────

def _schedule_can_manage(uid: int, kind: str) -> bool:
    permission = "events.manage" if kind == "event" else "messages.schedule"
    return is_admin(uid) and has_permission(uid, permission)


def _schedule_private_actor(message) -> bool:
    return (isinstance(message, types.Message) and
            message.chat.type == "private" and
            message.chat.id == message.from_user.id)


async def start_schedule_wizard(message: types.Message, state: FSMContext,
                                actor_id: int, kind: str):
    if not _schedule_can_manage(actor_id, kind):
        await message.answer("⛔ Только руководство с соответствующими правами.")
        return
    await state.clear()
    await state.update_data(sched_kind=kind, sched_step="title",
                            sched_actor=actor_id, sched_fields={})
    await state.set_state(ScheduleStates.waiting_for_fields)
    label = "мероприятия" if kind == "event" else "автоматического сообщения"
    await message.answer(
        f"🗓 FBI · Создание {label}\n\n"
        "Шаг 1/5: отправьте название (3–80 символов).\n\n"
        "Даты и время указываются по МСК (UTC+3).\nОтмена: /cancel",
        parse_mode=None
    )


@dp.message(Command("events"))
async def cmd_events(message: types.Message):
    if not _schedule_private_actor(message):
        await message.answer("Откройте бота в личных сообщениях.")
        return
    try:
        await show_schedule_list(message, message.from_user.id, include_messages=False)
    except Exception as exc:
        logger.warning("Event list failed: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось открыть мероприятия Firebase.")


@dp.message(Command("schedule"))
async def cmd_schedule(message: types.Message):
    if not _schedule_private_actor(message):
        await message.answer("Доступно только в личном чате.")
        return
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Расписание доступно руководству.")
        return
    try:
        await show_schedule_list(message, message.from_user.id, include_messages=True)
    except Exception as exc:
        logger.warning("Schedule list failed: %s", type(exc).__name__)
        await message.answer("⚠️ Не удалось открыть расписание Firebase.")


@dp.message(Command("event"))
async def cmd_create_event(message: types.Message, state: FSMContext):
    if not _schedule_private_actor(message):
        await message.answer("Доступно только в личном чате.")
        return
    await start_schedule_wizard(message, state, message.from_user.id, "event")


@dp.message(Command("autopost"))
async def cmd_create_autopost(message: types.Message, state: FSMContext):
    if not _schedule_private_actor(message):
        await message.answer("Доступно только в личном чате.")
        return
    await start_schedule_wizard(message, state, message.from_user.id, "message")


@dp.message(Command("eventnotify"))
async def cmd_eventnotify(message: types.Message):
    if not _schedule_private_actor(message):
        await message.answer("Откройте личный чат.")
        return
    profile = get_verified_agent(message.from_user.id)
    if not profile:
        await message.answer("Для настройки уведомлений подтвердите профиль в /start.")
        return
    args = (message.text or "").split()
    if len(args) != 2 or args[1].lower() not in ("on", "off"):
        current = "включены" if profile.get("event_notifications") is not False else "отключены"
        await message.answer(
            f"🔔 Уведомления о мероприятиях сейчас {current}.\n\n"
            "Команды: /eventnotify on и /eventnotify off."
        )
        return
    previous = dict(profile)
    enabled = args[1].lower() == "on"
    profile["event_notifications"] = enabled
    if not await sync_state_to_cloud():
        users_db[str(message.from_user.id)] = previous
        await message.answer("⚠️ Firebase не подтвердила настройку.")
        return
    save_json(USERS_FILE, users_db)
    await message.answer(
        "✅ Уведомления о мероприятиях включены." if enabled
        else "🔕 Уведомления о мероприятиях отключены."
    )


async def show_schedule_list(message: types.Message, actor_id: int,
                             *, include_messages: bool):
    records = await list_active(firebase_client)
    profile = users_db.get(str(actor_id), {})
    rank = current_rank(profile.get("rank") if isinstance(profile, dict) else "")
    verified = (isinstance(profile, dict) and bool(profile.get("nick"))
                and profile.get("verification_status") not in BLOCKED_STATUSES)
    can_event = _schedule_can_manage(actor_id, "event")
    can_message = _schedule_can_manage(actor_id, "message")
    managed = can_event or can_message
    if include_messages and not managed:
        await message.answer("⛔ Недостаточно прав.")
        return
    visible = [
        item for item in records
        if ((include_messages and (
            (item["kind"] == "event" and can_event) or
            (item["kind"] == "message" and can_message)))
            or (not include_messages and item["kind"] == "event" and (
                can_event or (verified and rank is not None
                              and item["min_rank"] <= rank <= item["max_rank"]))))
    ]
    head = "📅 FBI · Ближайшие мероприятия" if not include_messages else "🗓 FBI · Запланированные рассылки"
    text = head + "\n\n" + (
        "Нет активных записей." if not visible
        else f"Всего: {len(visible)}. Все даты и часы — МСК (UTC+3)."
    )
    kb = []
    for item in visible[:20]:
        date = moscow_label(item["at"]).split(" ", 1)[0]
        icon = "🎯" if item["kind"] == "event" else "📨"
        kb.append([InlineKeyboardButton(
            text=f"{icon} {item['title'][:28]} · {date}",
            callback_data=f"sched:view:{item['id']}"
        )])
    if can_event:
        kb.append([InlineKeyboardButton(text="➕ Новое мероприятие",
                                         callback_data="sched:new:event")])
    if can_message:
        kb.append([InlineKeyboardButton(text="📨 Запланировать сообщение",
                                         callback_data="sched:new:message")])
    await message.answer(text, parse_mode=None,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
                         if kb else None)


@dp.message(ScheduleStates.waiting_for_fields)
async def schedule_wizard_input(message: types.Message, state: FSMContext):
    if not _schedule_private_actor(message):
        await message.answer("Форма доступна только в личном чате.")
        return
    data = await state.get_data()
    kind, step = data.get("sched_kind"), data.get("sched_step")
    if (data.get("sched_actor") != message.from_user.id
            or kind not in ("event", "message")
            or not _schedule_can_manage(message.from_user.id, kind)):
        await state.clear()
        await message.answer("⛔ Права на создание расписания не подтверждены.")
        return
    value = (message.text or "").strip()
    fields = dict(data.get("sched_fields", {}))
    if not value or value.startswith("/"):
        await message.answer("Отправьте текст для текущего шага или /cancel.")
        return
    try:
        if step == "title":
            if not 3 <= len(value) <= 80:
                raise ScheduleError("Название должно быть от 3 до 80 символов.")
            fields["title"] = value
            next_step = "date"
            prompt = "Шаг 2/5: введите дату ДД.ММ.ГГГГ (например, 15.11.2026)."
        elif step == "date":
            datetime.strptime(value, "%d.%m.%Y")
            fields["date"] = value
            next_step = "time"
            prompt = "Шаг 3/5: время по МСК, формат ЧЧ:ММ (например, 19:30)."
        elif step == "time":
            parse_moscow_date(fields.get("date", ""), value)
            fields["time"] = value
            next_step = "ranks"
            prompt = ("Шаг 4/5: каким рангам отправить?\n"
                      "Примеры: 1-10, 4-8, 8-X или 7.")
        elif step == "ranks":
            minimum, maximum = parse_ranks(value)
            fields["ranks"] = f"{minimum}-{maximum}"
            next_step = "description"
            prompt = ("Шаг 5/5: укажите место сбора и информацию о МП (3–900 символов)."
                      if kind == "event"
                      else "Шаг 5/5: введите текст автоматической рассылки (3–900 символов).")
        elif step == "description":
            if not 3 <= len(value) <= 900:
                raise ScheduleError("Введите от 3 до 900 символов.")
            fields["description"] = value
            next_step = "options"
            prompt = ""
        else:
            await message.answer("Выберите вариант кнопкой или используйте /cancel.")
            return
    except (ValueError, ScheduleError) as exc:
        await message.answer("⚠️ " + str(exc), parse_mode=None)
        return

    await state.update_data(sched_fields=fields, sched_step=next_step)
    if next_step != "options":
        await message.answer(prompt, parse_mode=None)
        return
    if kind == "event":
        buttons = [
            [InlineKeyboardButton(text="⏰ За 60 и 15 минут",
                                  callback_data="schedopt:event:both")],
            [InlineKeyboardButton(text="⏰ За 1 день + за 1 час",
                                  callback_data="schedopt:event:day")],
            [InlineKeyboardButton(text="⏰ Только за 15 минут",
                                  callback_data="schedopt:event:15")],
            [InlineKeyboardButton(text="🔕 Без напоминаний",
                                  callback_data="schedopt:event:none")]
        ]
        await message.answer(
            "✅ Данные заполнены.\n\nПри создании МП агенты получат первое объявление "
            "сразу. Выберите дополнительные напоминания:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    else:
        buttons = [
            [InlineKeyboardButton(text="📨 Один раз",
                                  callback_data="schedopt:message:none")],
            [InlineKeyboardButton(text="🔁 Ежедневно (4 отправки)",
                                  callback_data="schedopt:message:daily")],
            [InlineKeyboardButton(text="🔁 Еженедельно (4 отправки)",
                                  callback_data="schedopt:message:weekly")]
        ]
        await message.answer(
            "✅ Данные заполнены. Выберите автоматизацию сообщений. "
            "Повторения ограничены четырьмя отправками:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )


@dp.callback_query(F.data.startswith("schedopt:"))
async def schedule_options(callback: types.CallbackQuery, state: FSMContext):
    if (callback.message.chat.id != callback.from_user.id
            or callback.message.chat.type != "private"):
        await callback.answer("Доступно только в личном чате.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in ("event", "message"):
        await callback.answer("Неизвестный вариант.", show_alert=True)
        return
    data = await state.get_data()
    kind = parts[1]
    if (await state.get_state() != ScheduleStates.waiting_for_fields.state
            or data.get("sched_step") != "options"
            or data.get("sched_kind") != kind
            or data.get("sched_actor") != callback.from_user.id
            or not _schedule_can_manage(callback.from_user.id, kind)):
        await callback.answer("Форма устарела или недоступна.", show_alert=True)
        return
    options = {"both": (60, 15), "day": (1440, 60),
               "15": (15,), "none": ()}
    if kind == "event" and parts[2] not in options:
        await callback.answer("Неизвестный вариант.", show_alert=True)
        return
    if kind == "message" and parts[2] not in ("none", "daily", "weekly"):
        await callback.answer("Неизвестный повтор.", show_alert=True)
        return
    fields = data["sched_fields"]
    try:
        item = prepare_schedule(
            kind, fields["title"], fields["description"], fields["date"],
            fields["time"], fields["ranks"], creator_id=callback.from_user.id,
            reminders=options.get(parts[2], ()), repeat=parts[2] if kind == "message" else "none"
        )
    except ScheduleError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await state.update_data(sched_preview=item)
    await state.set_state(ScheduleStates.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить в Firebase",
                              callback_data=f"schedsave:{item['id']}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sched:abort")]
    ])
    await callback.message.answer(
        "🔎 Предварительный просмотр — ничего ещё не отправлено.\n\n" +
        display_schedule(item) +
        "\n\nОповещения начнутся только после подтверждения.",
        parse_mode=None, reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("schedsave:"))
async def schedule_save(callback: types.CallbackQuery, state: FSMContext):
    if (callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    data = await state.get_data()
    item = data.get("sched_preview")
    if (await state.get_state() != ScheduleStates.confirm.state
            or not isinstance(item, dict)
            or callback.data != "schedsave:" + item.get("id", "")
            or data.get("sched_actor") != callback.from_user.id
            or not _schedule_can_manage(callback.from_user.id, item.get("kind"))):
        await callback.answer("Подтверждение устарело или право отозвано.", show_alert=True)
        return
    # Lock before the first network call to avoid double-click commits.
    await state.set_state(ScheduleStates.waiting_for_fields)
    await state.update_data(sched_step="saving")
    try:
        await create_schedule(firebase_client, item)
    except Exception as exc:
        await state.set_state(ScheduleStates.confirm)
        logger.warning("Schedule persist failed: %s", type(exc).__name__)
        await callback.answer("Firebase не подтвердила запись. Проверьте /schedule.",
                              show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "✅ Сохранено в Firebase.\n\n" + display_schedule(item) +
        "\n\nМероприятие/рассылка будут обработаны по расписанию.",
        parse_mode=None
    )
    asyncio.create_task(record_admin_activity(
        "event_created" if item["kind"] == "event" else "autopost_created",
        callback.from_user.id, f"{item['id']} {item['title'][:70]}"
    ))
    await callback.answer("✅ Расписание сохранено!")


@dp.callback_query(F.data.startswith("sched:"))
async def schedule_callback(callback: types.CallbackQuery, state: FSMContext):
    if (callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Доступно только в личном чате.", show_alert=True)
        return
    data = callback.data or ""
    if data == "sched:list":
        try:
            await show_schedule_list(callback.message, callback.from_user.id,
                                     include_messages=is_admin(callback.from_user.id))
            await callback.answer()
        except Exception as exc:
            logger.warning("Schedule menu failed: %s", type(exc).__name__)
            await callback.answer("Расписание недоступно.", show_alert=True)
        return
    if data == "sched:abort":
        await state.clear()
        await callback.message.edit_text("❌ Создание расписания отменено.")
        await callback.answer()
        return
    if data in ("sched:new:event", "sched:new:message"):
        kind = data.rsplit(":", 1)[-1]
        if not _schedule_can_manage(callback.from_user.id, kind):
            await callback.answer("Нет права на создание.", show_alert=True)
            return
        await start_schedule_wizard(callback.message, state, callback.from_user.id, kind)
        await callback.answer()
        return
    match = re.fullmatch(r"sched:(view|cancel|cancelok):(sched-[a-f0-9]{20})", data)
    if not match:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return
    action, sid = match.groups()
    try:
        result = await get_schedule(firebase_client, sid)
        if not result:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        item, _ = result
        can_manage = _schedule_can_manage(callback.from_user.id, item["kind"])
        profile = users_db.get(str(callback.from_user.id), {})
        rank = current_rank(profile.get("rank") if isinstance(profile, dict) else "")
        verified = (isinstance(profile, dict) and bool(profile.get("nick"))
                    and profile.get("verification_status") not in BLOCKED_STATUSES)
        can_view = (can_manage or (item["kind"] == "event" and verified
                                  and rank is not None
                                  and item["min_rank"] <= rank <= item["max_rank"]))
        if not can_view or (action != "view" and not can_manage):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if action == "cancelok":
            result_code = await cancel_schedule(firebase_client, sid)
            await callback.message.edit_text(
                "✅ Запись отменена." if result_code == "cancelled"
                else "ℹ️ Запись уже неактивна или не найдена.", parse_mode=None
            )
            await callback.answer()
            return
        if action == "cancel":
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Да, отменить будущие сообщения",
                                      callback_data=f"sched:cancelok:{sid}")],
                [InlineKeyboardButton(text="⬅️ Назад",
                                      callback_data=f"sched:view:{sid}")]
            ])
            await callback.message.edit_text(
                "⚠️ Отменить запись?\n\n" + display_schedule(item) +
                "\n\nУже доставленные сообщения нельзя отозвать.",
                parse_mode=None, reply_markup=kb
            )
            await callback.answer()
            return
        buttons = []
        if can_manage and item["status"] == "active":
            buttons.append([InlineKeyboardButton(
                text="🗑 Отменить будущие оповещения",
                callback_data=f"sched:cancel:{sid}"
            )])
        buttons.append([InlineKeyboardButton(text="📅 К расписанию",
                                             callback_data="sched:list")])
        await callback.message.edit_text(
            display_schedule(item), parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer()
    except Exception as exc:
        logger.warning("Schedule detail failed: %s", type(exc).__name__)
        await callback.answer("Ошибка Firebase. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query(F.data == "sched:list")
async def schedule_list_callback(callback: types.CallbackQuery):
    if (callback.message.chat.type != "private"
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Откройте личный чат.", show_alert=True)
        return
    try:
        await show_schedule_list(callback.message, callback.from_user.id,
                                 include_messages=is_admin(callback.from_user.id))
    except Exception as exc:
        logger.warning("Scheduled list callback failed: %s", type(exc).__name__)
        await callback.answer("Расписание недоступно.", show_alert=True)
        return
    await callback.answer()


# A plain natural-language instruction in a private admin chat works without /ai.
# State-specific registration/report/support handlers are registered earlier and
# continue to take priority; this late handler never handles agent messages.
@dp.message(F.text)
async def conversational_admin_command(message: types.Message, state: FSMContext):
    # The last-resort handler must not interpret evidence links, rank selection,
    # nicknames, report quantities, praise counts or manual-grant input as AI.
    # This also protects states that expect callback buttons rather than text.
    if await state.get_state() is not None:
        return
    if message.chat.type != "private" or message.chat.id != message.from_user.id:
        return
    if not looks_like_operation(message.text or ""):
        return
    if is_admin(message.from_user.id):
        await handle_natural_command(message, message.text)
    elif get_verified_agent(message.from_user.id):
        # Agent text is never routed to the unmoderated administrator credit path.
        await agent_operation_from_text(message, message.text)



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

    await register_native_command_menu()
    async def schedule_delivery(uid, message):
        await bot.send_message(uid, message, parse_mode=None,
                               disable_web_page_preview=True)
    async def schedule_poller():
        # Prevent accidental zero-recipient delivery while Firebase ACL is stale.
        while True:
            if _cloud_state_hydrated:
                try:
                    await scheduled_tick(firebase_client, users_db, schedule_delivery)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Schedule cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(30)
    schedule_task = asyncio.create_task(schedule_poller(), name="fbi-scheduler")
    try:
        logger.info("Bot polling starting...")
        await dp.start_polling(bot)
    finally:
        schedule_task.cancel()
        await asyncio.gather(schedule_task, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
