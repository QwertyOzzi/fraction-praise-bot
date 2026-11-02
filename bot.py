import asyncio
import os
import json
import html
import logging
import uuid
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from aiohttp import web, ClientSession
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
requests_db: Dict[str, Any] = load_json(REQUESTS_FILE, {})
owner_cfg: Dict[str, Any] = {"owner_ids": list(OWNER_TELEGRAM_IDS)}

if ADMIN_CHAT_ID and ADMIN_CHAT_ID.lstrip("-").isdigit():
    cid = int(ADMIN_CHAT_ID)
    if cid not in admins_db:
        admins_db.append(cid)
        save_json(ADMINS_FILE, admins_db)

def is_admin(user_id: int) -> bool:
    return user_id in OWNER_TELEGRAM_IDS or user_id in admins_db

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_TELEGRAM_IDS

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
        url = f"https://firestore.googleapis.com/v1/projects/{self.project_id}/databases/(default)/documents/support_tickets"
        headers = {"Content-Type": "application/json"}
        if token:
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
    """Загрузка сохранённого состояния бота из Firestore при запуске."""
    try:
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/bot_state"
        async with ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw = data.get("fields", {}).get("data", {}).get("stringValue", "{}")
                    cloud_data = json.loads(raw)

                    cloud_users = cloud_data.get("users", {})
                    for uid, udata in cloud_users.items():
                        users_db[str(uid)] = udata

                    cloud_admins = cloud_data.get("admins", [])
                    # Cloud-stored admins are not a source of authorization.\n                    # Only previously approved local admins remain trusted.

                    owner_cfg["owner_ids"] = list(OWNER_TELEGRAM_IDS)

                    cloud_requests = cloud_data.get("requests", {})
                    for rid, rdata in cloud_requests.items():
                        requests_db[str(rid)] = rdata

                    save_json(USERS_FILE, users_db)
                    save_json(ADMINS_FILE, admins_db)
                    save_json(OWNER_FILE, owner_cfg)
                    save_json(REQUESTS_FILE, requests_db)
                    logger.info(f"Loaded cloud bot state: {len(users_db)} users, {len(admins_db)} admins, {len(owner_cfg.get('owner_ids', []))} owners")

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

def notify_state_changed():
    """Безопасная фоновая синхронизация с облаком Firestore."""
    try:
        loop = asyncio.get_running_loop()
        if loop and loop.is_running():
            loop.create_task(sync_state_to_cloud())
    except RuntimeError:
        pass

async def sync_state_to_cloud():
    """Сохранение состояния бота в Firestore при любых изменениях."""
    try:
        token = await firebase_client.get_token()
        if not token:
            return

        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/config/bot_state"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        payload_data = {
            "users": users_db,
            "admins": admins_db,
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
                else:
                    logger.warning(f"Failed to sync bot state to cloud: {resp.status}")
    except Exception as e:
        logger.warning(f"Error syncing bot state to cloud: {e}")

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
    "➕ Начислить FRP / Похвалу"
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

    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  AIOGRAM DISPATCHER                                              ║
# ╚══════════════════════════════════════════════════════════════════╝
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id)

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
    if rep_type == "praise":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=str(n), callback_data=f"repqty_{n}") for n in (1, 2, 3, 5, 10)],
            [InlineKeyboardButton(text="✏️ Другое число (1–20)", callback_data="repqty_custom")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
        ])
        await callback.message.edit_text("🌟 Сколько похвал в заявке? Выберите число или введите от 1 до 20:", reply_markup=kb)
        await state.set_state(ReportStates.waiting_for_quantity)
    else:
        await show_report_reason_menu(callback.message, state, edit=True)


@dp.callback_query(ReportStates.waiting_for_quantity, F.data.startswith("repqty_"))
async def process_report_quantity_button(callback: types.CallbackQuery, state: FSMContext):
    value = callback.data[len("repqty_"):]
    if value == "custom":
        await callback.message.edit_text("Введите количество похвал числом от 1 до 20:")
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
    if req.get("rep_type") == "praise":
        controls.append([
            InlineKeyboardButton(text="➖ 1", callback_data=f"qtyminus_{req_id}"),
            InlineKeyboardButton(text=f"🌟 {qty}", callback_data=f"qtyshow_{req_id}"),
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
        ("Измените количество похвал кнопками ➖/➕, затем примите решение."
         if req.get("rep_type") == "praise" else
         "Одобрите или отклоните заявку.")
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
    if not req or req.get("status") != "pending" or req.get("rep_type") != "praise":
        await callback.answer("Заявка уже закрыта или не найдена.", show_alert=True)
        return
    new_qty = max(1, min(20, int(req.get("quantity", 1)) + (1 if plus else -1)))
    req["quantity"] = new_qty
    save_json(REQUESTS_FILE, requests_db)
    notify_state_changed()
    await refresh_pending_report_messages(req_id, req)
    await callback.answer(f"Количество похвал: {new_qty}")


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
@dp.message(Command("admin"))
async def cmd_admin_auth(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    user_data = users_db.get(str(uid), {})
    nick = user_data.get("nick", message.from_user.full_name)
    rank = user_data.get("rank", "?")

    args = message.text.split(maxsplit=1)
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

    # 2. Ключ Заместителя / Лидера ()
    if DEPUTY_KEY and key == DEPUTY_KEY:
        if is_admin(uid):
            await message.answer(
                "ℹ️ <b>Вы уже являетесь администратором бота!</b>\n"
                "Все функции руководства доступны в меню ниже.",
                reply_markup=get_main_menu(uid),
                parse_mode="HTML"
            )
            return

        # Уведомляем заместителя о том, что запрос отправлен
        await message.answer(
            "⏳ <b>Ключ заместителя принят! Запрос отправлен Главному Лидеру.</b>\n\n"
            "Пожалуйста, ожидайте подтверждения. Как только Главный Руководитель нажмёт кнопку «Одобрить доступ», вам придёт уведомление и откроется админ-меню.",
            parse_mode="HTML"
        )

        # Кому слать запрос: владельцам или существующим админам
        recipients = list(set(owner_cfg.get("owner_ids", []) + admins_db))
        if ADMIN_CHAT_ID and ADMIN_CHAT_ID.lstrip("-").isdigit():
            recipients.append(int(ADMIN_CHAT_ID))
        recipients = list(set(recipients))

        if not recipients:
            await message.answer(
                "⚠️ <b>Внимание:</b> Главный Лидер ещё не авторизовался в боте по мастер-ключу.\n"
                "Попросите его сначала зайти в бота и ввести свой мастер-ключ, после чего повторите команду.",
                parse_mode="HTML"
            )
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить доступ", callback_data=f"adm_grant_{uid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_deny_{uid}")
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
@dp.callback_query(F.data.startswith("adm_grant_"))
async def callback_admin_grant(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав для подтверждения!", show_alert=True)
        return

    target_id = int(callback.data.replace("adm_grant_", ""))
    target_user = users_db.get(str(target_id), {})
    target_nick = target_user.get("nick", f"ID {target_id}")

    if target_id not in admins_db:
        admins_db.append(target_id)
        save_json(ADMINS_FILE, admins_db)
    notify_state_changed()

    # Обновляем карточку у владельца
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ <b>ОДОБРЕНО</b> руководителем {callback.from_user.mention_html()}! Права успешно выданы.",
        parse_mode="HTML"
    )

    # Уведомляем зама
    try:
        await bot.send_message(
            target_id,
            "🎉 <b>Главный Лидер одобрил ваш доступ!</b>\n\n"
            "Вам успешно выданы права Администратора / Заместителя.\n"
            "Теперь вам доступны:\n"
            "• Уведомления о новых отчетах агентов с кнопками Одобрить/Отклонить\n"
            "• Очередь на выдачу в игре (<code>/queue</code>)\n"
            "• Просмотр списка админов\n\n"
            "Меню бота обновлено ниже:",
            reply_markup=get_main_menu(target_id),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not notify new admin {target_id}: {e}")

    await callback.answer(f"✅ Доступ для {target_nick} успешно выдан!", show_alert=False)

# Обработка отклонения доступа заму владельцем
@dp.callback_query(F.data.startswith("adm_deny_"))
async def callback_admin_deny(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав!", show_alert=True)
        return

    target_id = int(callback.data.replace("adm_deny_", ""))
    target_user = users_db.get(str(target_id), {})
    target_nick = target_user.get("nick", f"ID {target_id}")

    await callback.message.edit_text(
        callback.message.text + f"\n\n❌ <b>ОТКЛОНЕНО</b> руководителем {callback.from_user.mention_html()}.",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            target_id,
            "❌ <b>Главный Руководитель отклонил ваш запрос на получение прав администратора.</b>\n"
            "Если произошла ошибка, обратитесь к лидеру напрямую.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Could not notify denied user {target_id}: {e}")

    await callback.answer(f"Запрос от {target_nick} отклонён.", show_alert=False)

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
        
        role_label = " 👑 <b>(Владелец)</b>" if is_own else " ⭐ <i>(Заместитель)</i>"
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
        admins_db.remove(target_id)
        save_json(ADMINS_FILE, admins_db)
        notify_state_changed()
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


async def perform_manual_grant(message: types.Message, raw: str):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только лидер или заместитель может начислять баллы.")
        return
    args = raw.strip().split()
    if len(args) != 3:
        await message.answer("Формат: <code>/grant frp Nick_Name 2</code> или "
                             "<code>/grant praise Nick_Name 3</code>.",
                             parse_mode="HTML")
        return
    kind, nick, qty_raw = args
    sheet = {"frp": "fractionrp", "fractionrp": "fractionrp",
             "praise": "praise"}.get(kind.lower())
    if not sheet or not qty_raw.isdecimal() or not 1 <= int(qty_raw) <= 20:
        await message.answer("Укажите frp или praise и количество от 1 до 20.")
        return
    if len(nick) > 30 or "_" not in nick or not all(c.isalnum() or c == "_" for c in nick):
        await message.answer("Укажите игровой ник в формате Name_Surname.")
        return
    user_entry = next((u for u in users_db.values()
                       if u.get("nick", "").casefold() == nick.casefold()), None)
    rank = str(user_entry.get("rank", "8")) if user_entry else "8"
    qty = int(qty_raw)
    grant_id = "manual-" + uuid.uuid4().hex
    try:
        success = await firebase_client.credit_report(
            sheet, nick, rank, f"Ручная выдача администратором {message.from_user.id}",
            "", grant_id, quantity=qty
        )
        if success:
            await message.answer(
                f"✅ Начислено <b>+{qty}</b> /{sheet} игроку <code>{html.escape(nick)}</code>. "
                "Баллы доступны в очереди на выдачу.",
                parse_mode="HTML"
            )
    except Exception as exc:
        logger.error(f"Manual grant failed ({grant_id}): {exc}")
        await message.answer(
            "⚠️ Нет подтверждения начисления. Проверьте таблицу перед повторением команды."
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
    data = {
        "status": "online",
        "service": "FractionRp Telegram Bot",
        "bot_username": "frporp_bot",
        "leader": LEADER_INFO.get("nick", "Не назначен"),
        "time": datetime.now(timezone.utc).isoformat()
    }
    return web.json_response(data, headers=headers)

_support_rate_history: Dict[str, List[float]] = {}

async def handle_api_support(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    }
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=headers)

    client_ip = request.remote or "unknown"
    now = datetime.now(timezone.utc).timestamp()
    # Small bounded in-process limiter; add provider-level rate limiting for multi-instance hosting.
    for ip, timestamps in list(_support_rate_history.items()):
        recent = [t for t in timestamps if now - t < 60]
        if recent:
            _support_rate_history[ip] = recent
        else:
            del _support_rate_history[ip]
    attempts = _support_rate_history.setdefault(client_ip, [])
    if len(attempts) >= 5:
        return web.json_response({"error": "Too many requests"}, status=429, headers=headers)
    attempts.append(now)
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

        if not nick or not description or len(nick) > 80 or len(description) > 4000 or len(contact) > 200 or len(category) > 80 or len(screenshot) > 1000:
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
        return web.json_response({"error": str(e)}, status=500, headers=headers)

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

    # Загружаем сохранённое состояние (все юзеры, админы, замы) из Firestore
    await sync_state_from_cloud()

    # Запускаем фоновый web-сервер для Render
    await start_web_server()

    # Запускаем polling бота
    logger.info("Bot polling starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
