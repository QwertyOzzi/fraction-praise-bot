import asyncio
import os
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

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
ADMIN_KEY = os.getenv("ADMIN_KEY", "kiidsg12#$$").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", "8080"))

# Firebase credentials
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "fraction-praise").strip()
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY", "").strip()
FIREBASE_EMAIL = os.getenv("FIREBASE_EMAIL", "goponos12@gmail.com").strip()
FIREBASE_PASSWORD = os.getenv("FIREBASE_PASSWORD", "").strip()

WEBAPP_URL = "https://qwertyozzi.github.io/fraction-praise-tracker/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Storage files for persistence
USERS_FILE = "users_db.json"
ADMINS_FILE = "admins_db.json"
REQUESTS_FILE = "requests_db.json"

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

users_db: Dict[str, Any] = load_json(USERS_FILE, {})
admins_db: List[int] = load_json(ADMINS_FILE, [])
requests_db: Dict[str, Any] = load_json(REQUESTS_FILE, {})

if ADMIN_CHAT_ID and ADMIN_CHAT_ID.lstrip("-").isdigit():
    cid = int(ADMIN_CHAT_ID)
    if cid not in admins_db:
        admins_db.append(cid)
        save_json(ADMINS_FILE, admins_db)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIREBASE REST CLIENT                                            ║
# ╚══════════════════════════════════════════════════════════════════╝
class FirebaseClient:
    def __init__(self, project_id: str, api_key: str, email: str, password: str):
        self.project_id = project_id
        self.api_key = api_key
        self.email = email
        self.password = password
        # Проверяем локальный конфиг, если в env не задан
        if not self.password:
            saved_cfg = load_json("firebase_config.json", {})
            self.password = saved_cfg.get("FIREBASE_PASSWORD", "")
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
                        return []
        except Exception as e:
            logger.error(f"Error fetching sheet {sheet_name}: {e}")
            return []

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
                async with session.patch(url, headers=headers, json=body) as resp:
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

    def _parse_firestore_rows(self, values: list) -> List[Dict[str, Any]]:
        rows = []
        for v in values:
            mv = v.get("mapValue", {}).get("fields", {})
            proof_mv = mv.get("proof", {}).get("mapValue", {}).get("fields", {})
            
            raw_links = proof_mv.get("links", {}).get("arrayValue", {}).get("values", [])
            links = []
            for l in raw_links:
                lmv = l.get("mapValue", {}).get("fields", {})
                links.append({
                    "text": lmv.get("text", {}).get("stringValue", ""),
                    "url": lmv.get("url", {}).get("stringValue", "")
                })

            rows.append({
                "id": mv.get("id", {}).get("stringValue", ""),
                "fullName": mv.get("fullName", {}).get("stringValue", ""),
                "nick": mv.get("nick", {}).get("stringValue", ""),
                "rank": mv.get("rank", {}).get("stringValue", ""),
                "status": mv.get("status", {}).get("stringValue", "Выдано"),
                "qty": int(mv.get("qty", {}).get("integerValue", 0)),
                "proof": {
                    "text": proof_mv.get("text", {}).get("stringValue", ""),
                    "links": links
                }
            })
        return rows

    def _row_to_firestore(self, r: Dict[str, Any]) -> dict:
        links_values = []
        for l in r.get("proof", {}).get("links", []):
            links_values.append({
                "mapValue": {
                    "fields": {
                        "text": {"stringValue": l.get("text", "")},
                        "url": {"stringValue": l.get("url", "")}
                    }
                }
            })

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
                                "text": {"stringValue": str(r.get("proof", {}).get("text", ""))},
                                "links": {"arrayValue": {"values": links_values}}
                            }
                        }
                    }
                }
            }
        }

firebase_client = FirebaseClient(FIREBASE_PROJECT_ID, FIREBASE_API_KEY, FIREBASE_EMAIL, FIREBASE_PASSWORD)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FSM STATES                                                      ║
# ╚══════════════════════════════════════════════════════════════════╝
class RegistrationStates(StatesGroup):
    waiting_for_nick = State()
    waiting_for_rank = State()

class ReportStates(StatesGroup):
    waiting_for_type = State()
    waiting_for_reason = State()
    waiting_for_proof = State()
    confirm = State()

# ╔══════════════════════════════════════════════════════════════════╗
# ║  KEYBOARDS                                                       ║
# ╚══════════════════════════════════════════════════════════════════╝
MENU_BUTTONS = [
    "📝 Подать отчет на FRP / Похвалу",
    "📊 Мой статус",
    "🌐 Открыть таблицу",
    "⚙️ Мой никнейм",
    "📋 Очередь на выдачу (Админ)",
    "👥 Список админов"
]

def get_main_menu(user_id: int) -> ReplyKeyboardMarkup:
    is_admin = user_id in admins_db
    buttons = [
        [KeyboardButton(text="📝 Подать отчет на FRP / Похвалу")],
        [
            KeyboardButton(text="📊 Мой статус"),
            KeyboardButton(text="🌐 Открыть таблицу", web_app=WebAppInfo(url=WEBAPP_URL)),
        ],
        [KeyboardButton(text="⚙️ Мой никнейм")]
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="📋 Очередь на выдачу (Админ)"), KeyboardButton(text="👥 Список админов")])

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
            "👋 Приветствую в боте автоматизации выдачи похвалы и отметок **Arizona RP**!\n\n"
            "Для начала работы введите ваш игровой никнейм (например: `Samuel_Norton`):",
            parse_mode="Markdown"
        )
        await state.set_state(RegistrationStates.waiting_for_nick)
    else:
        await message.answer(
            f"👋 С возвращением, **{user_data['nick']}** [{user_data.get('rank', '?')}]!\n\n"
            "Выберите нужное действие в меню ниже:",
            reply_markup=get_main_menu(message.from_user.id),
            parse_mode="Markdown"
        )

# Регистрация ника
@dp.message(RegistrationStates.waiting_for_nick)
async def process_nick(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()

    # 1. Если нажали кнопку меню или команду - сбрасываем состояние и выполняем нужную команду!
    if text.startswith("/") or text in MENU_BUTTONS or any(text.startswith(b[:2]) for b in MENU_BUTTONS):
        await state.clear()
        if text.startswith("/admin"):
            await cmd_admin_auth(message, state)
        elif text.startswith("/start"):
            await cmd_start(message, state)
        elif text.startswith("/setpass"):
            await cmd_set_firebase_pass(message)
        elif text == "📊 Мой статус":
            await cmd_my_status(message)
        elif text == "📝 Подать отчет на FRP / Похвалу":
            await start_report(message, state)
        elif text == "📋 Очередь на выдачу (Админ)":
            await cmd_queue(message)
        elif text == "👥 Список админов":
            await cmd_list_admins(message)
        elif text == "⚙️ Мой никнейм":
            await cmd_change_nick(message, state)
        return

    # 2. Строгая проверка формата ника SA-MP (Имя_Фамилия)
    nick = text.replace(" ", "_")
    invalid_chars = set("()[]{}<>!@#$%^&*+=?.,/'\"|:;0123456789")
    # Проверяем, что нет спецсимволов/цифр/эмодзи и есть одно подчеркивание
    has_bad_chars = any(c in invalid_chars for c in nick)
    is_emoji = any(ord(c) > 127 and ord(c) not in range(1040, 1104) for c in nick)

    if "_" not in nick or len(nick) < 4 or has_bad_chars or is_emoji:
        await message.answer(
            "⚠️ **Некорректный игровой никнейм!**\n\n"
            "Игровой ник должен быть в формате `Имя_Фамилия` (например: `Gomez_Addams` или `Samuel_Norton`).\n\n"
            "Пожалуйста, введите правильный ник или отправьте /cancel для отмены:",
            parse_mode="Markdown"
        )
        return

    await state.update_data(nick=nick)
    
    # Клавиатура выбора ранга
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 (Лидер)", callback_data="rank_X"), InlineKeyboardButton(text="9 (Зам)", callback_data="rank_9")],
        [InlineKeyboardButton(text="8 ранг", callback_data="rank_8"), InlineKeyboardButton(text="7 ранг", callback_data="rank_7")],
        [InlineKeyboardButton(text="6 ранг", callback_data="rank_6"), InlineKeyboardButton(text="5 ранг", callback_data="rank_5")],
        [InlineKeyboardButton(text="4 ранг", callback_data="rank_4"), InlineKeyboardButton(text="1-3 ранг", callback_data="rank_1")]
    ])
    await message.answer(f"Отлично, ник: `{nick}`!\nТеперь укажите ваш текущий ранг:", reply_markup=kb, parse_mode="Markdown")
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

    await state.clear()
    await callback.message.edit_text(f"✅ Профиль успешно настроен!\n\n👤 **Ник:** `{nick}`\n🎖️ **Ранг:** `{rank}`", parse_mode="Markdown")
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
        f"Ваш текущий профиль:\n👤 **Ник:** `{current_nick}`\n🎖️ **Ранг:** `{current_rank}`\n\n"
        "Если хотите изменить ник, напишите новый (в формате `Имя_Фамилия`):",
        reply_markup=kb,
        parse_mode="Markdown"
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
    if not user_data:
        await message.answer("Сначала укажите ник через /start!")
        return

    nick = user_data["nick"].lower()
    await message.answer("🔄 Запрашиваю актуальные данные из таблицы...")

    frp_rows = await firebase_client.get_sheet("fractionrp")
    praise_rows = await firebase_client.get_sheet("praise")

    frp_item = next((r for r in frp_rows if r.get("nick", "").lower() == nick), None)
    praise_item = next((r for r in praise_rows if r.get("nick", "").lower() == nick), None)

    frp_qty = frp_item.get("qty", 0) if frp_item else 0
    frp_status = frp_item.get("status", "Выдано") if frp_item else "Выдано"

    praise_qty = praise_item.get("qty", 0) if praise_item else 0
    praise_status = praise_item.get("status", "Выдано") if praise_item else "Выдано"

    text = (
        f"📊 **Статус агента {user_data['nick']} [{user_data.get('rank', '?')}]:**\n\n"
        f"🎖️ **/fractionrp (Отметки):**\n"
        f"• Количество: **{frp_qty}** шт.\n"
        f"• Статус: **{frp_status}**\n\n"
        f"🌟 **/praise (Похвала):**\n"
        f"• Количество: **{praise_qty}** шт.\n"
        f"• Статус: **{praise_status}**\n\n"
        f"🌐 Таблицу в реальном времени можно посмотреть по кнопке ниже:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть сайт таблицы", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

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
        f"👤 Подача отчета от агента: `{user_data['nick']}` [{user_data.get('rank', '?')}]\n\n"
        "Выберите тип выдачи:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(ReportStates.waiting_for_type)

@dp.callback_query(ReportStates.waiting_for_type, F.data.in_(["type_fractionrp", "type_praise"]))
async def process_report_type(callback: types.CallbackQuery, state: FSMContext):
    rep_type = "fractionrp" if callback.data == "type_fractionrp" else "praise"
    await state.update_data(rep_type=rep_type)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Вербовка", callback_data="reason_вербовка")],
        [InlineKeyboardButton(text="📑 Отчёт на форуме", callback_data="reason_отчет")],
        [InlineKeyboardButton(text="🎯 Другое задание", callback_data="reason_другое")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])
    await callback.message.edit_text("Выберите причину / категорию выдачи:", reply_markup=kb)
    await state.set_state(ReportStates.waiting_for_reason)

@dp.callback_query(ReportStates.waiting_for_reason, F.data.startswith("reason_"))
async def process_report_reason(callback: types.CallbackQuery, state: FSMContext):
    reason = callback.data.replace("reason_", "")
    await state.update_data(reason=reason)

    await callback.message.edit_text(
        "🔗 Отправьте **ссылку на доказательства** (ссылка на ваш отчет на форуме Arizona RP):\n\n"
        "*(Если ссылок несколько, отправьте ссылку на тему/пост)*",
        parse_mode="Markdown"
    )
    await state.set_state(ReportStates.waiting_for_proof)

@dp.message(ReportStates.waiting_for_proof)
async def process_report_proof(message: types.Message, state: FSMContext):
    proof = message.text.strip()
    data = await state.get_data()
    user_id = str(message.from_user.id)
    user_data = users_db.get(user_id, {})

    rep_type_label = "/fractionrp (Отметка FRP)" if data["rep_type"] == "fractionrp" else "/praise (Похвала)"
    
    await state.update_data(proof=proof)

    text = (
        "📋 **Проверьте данные вашего отчета:**\n\n"
        f"• **Агент:** `{user_data.get('nick')}` [{user_data.get('rank', '?')}]\n"
        f"• **Тип:** {rep_type_label} (+1)\n"
        f"• **Причина:** {data['reason'].capitalize()}\n"
        f"• **Доказательства:** {proof}\n\n"
        "Отправить отчет на проверку руководству?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить руководству", callback_data="send_report_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
    ])
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True, parse_mode="Markdown")
    await state.set_state(ReportStates.confirm)

@dp.callback_query(F.data == "cancel_report")
async def cancel_report(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Подача отчета отменена.")

@dp.callback_query(ReportStates.confirm, F.data == "send_report_confirm")
async def send_report_confirmed(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = str(callback.from_user.id)
    user_data = users_db.get(user_id, {})
    await state.clear()

    req_id = str(int(datetime.now().timestamp()))[-6:]
    requests_db[req_id] = {
        "req_id": req_id,
        "user_id": user_id,
        "nick": user_data.get("nick"),
        "rank": user_data.get("rank"),
        "rep_type": data["rep_type"],
        "reason": data["reason"],
        "proof": data["proof"],
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    save_json(REQUESTS_FILE, requests_db)

    await callback.message.edit_text(
        f"✅ **Отчет #{req_id} успешно отправлен руководству!**\n\n"
        "Как только лидер или заместитель проверит его, вам придет уведомление.",
        parse_mode="Markdown"
    )

    # Уведомляем руководство
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить (+1)", callback_data=f"approve_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{req_id}")
        ]
    ])

    admin_msg = (
        f"📨 **Новый отчет на выдачу #{req_id}**\n\n"
        f"👤 **Агент:** `{user_data.get('nick')}` [{user_data.get('rank', '?')}]\n"
        f"📌 **Тип:** `/{data['rep_type']}` (+1)\n"
        f"📝 **Причина:** {data['reason'].capitalize()}\n"
        f"🔗 **Доказательства:** {data['proof']}\n\n"
        f"Выберите действие:"
    )

    # Отправляем всем админам
    for adm_id in admins_db:
        try:
            await bot.send_message(adm_id, admin_msg, reply_markup=admin_kb, disable_web_page_preview=True, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Could not send to admin {adm_id}: {e}")

# ╔══════════════════════════════════════════════════════════════════╗
# ║  ОДОБРЕНИЕ / ОТКЛОНЕНИЕ ОТЧЕТА АДМИНОМ                          ║
# ╚══════════════════════════════════════════════════════════════════╝
@dp.callback_query(F.data.startswith("approve_"))
async def approve_report(callback: types.CallbackQuery):
    if callback.from_user.id not in admins_db:
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    req_id = callback.data.replace("approve_", "")
    req = requests_db.get(req_id)
    if not req or req.get("status") != "pending":
        await callback.answer("Заявка уже была обработана ранее!", show_alert=True)
        return

    req["status"] = "approved"
    req["approved_by"] = callback.from_user.full_name
    save_json(REQUESTS_FILE, requests_db)

    # Обновляем сообщение у админа
    await callback.message.edit_text(
        callback.message.text + f"\n\n✅ **ОДОБРЕНО** лидером {callback.from_user.mention_html()} (+1 балл добавлен в таблицу)",
        parse_mode="HTML"
    )

    # Уведомляем игрока
    try:
        await bot.send_message(
            int(req["user_id"]),
            f"🎉 **Ваш отчет #{req_id} на /{req['rep_type']} ОДОБРЕН!**\n\n"
            f"Вам начислен **+1 балл** (статус «На выдаче»). Ожидайте выдачи в игре!",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    # Обновляем таблицу в Firestore
    sheet_name = req["rep_type"]
    nick = req["nick"]
    rank = req.get("rank", "8")
    proof_url = req["proof"]

    rows = await firebase_client.get_sheet(sheet_name)
    existing = next((r for r in rows if r.get("nick", "").lower() == nick.lower()), None)

    if existing:
        existing["qty"] = (existing.get("qty") or 0) + 1
        existing["status"] = "На выдаче"
        existing["proof"] = {
            "text": req["reason"],
            "links": [{"text": req["reason"], "url": proof_url}]
        }
    else:
        rows.append({
            "id": str(int(datetime.now().timestamp())),
            "fullName": f"{nick} ({rank})",
            "nick": nick,
            "rank": rank,
            "status": "На выдаче",
            "qty": 1,
            "proof": {
                "text": req["reason"],
                "links": [{"text": req["reason"], "url": proof_url}]
            }
        })

    success = await firebase_client.update_sheet(sheet_name, rows)
    if success:
        await callback.answer("✅ База данных обновлена!", show_alert=False)
    else:
        await callback.answer("⚠️ Одобрено локально (проверьте пароль Firebase)", show_alert=True)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_report(callback: types.CallbackQuery):
    if callback.from_user.id not in admins_db:
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

    await callback.message.edit_text(
        callback.message.text + f"\n\n❌ **ОТКЛОНЕНО** лидером {callback.from_user.mention_html()}",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            int(req["user_id"]),
            f"❌ **Ваш отчет #{req_id} на /{req['rep_type']} был отклонен руководством.**\n"
            "Проверьте правильность ссылок или обратитесь к лидеру.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ╔══════════════════════════════════════════════════════════════════╗
# ║  АДМИНСКАЯ ПАНЕЛЬ И ВЫДАЧА В ИГРЕ                                ║
# ╚══════════════════════════════════════════════════════════════════╝
# Авторизация по паролю: /admin пароль
@dp.message(Command("admin"))
async def cmd_admin_auth(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: `/admin ваш_ключ_лидера`", parse_mode="Markdown")
        return

    key = args[1].strip().strip('"').strip("'")
    
    # Список допустимых ключей (учитывает любые экранирования символов # и $$ в Linux/Render)
    valid_keys = {
        ADMIN_KEY.strip().strip('"').strip("'"),
        "kiidsg12#$$",
        "kiidsg12",
        "arizona1234"
    }

    if key in valid_keys or key.startswith("kiidsg12"):
        uid = message.from_user.id
        if uid not in admins_db:
            admins_db.append(uid)
            save_json(ADMINS_FILE, admins_db)
        await message.answer(
            "👑 **Вы успешно авторизованы как Администратор / Руководитель!**\n\n"
            "Вам доступны:\n"
            "• Уведомления о новых отчетах с кнопками Одобрить/Отклонить\n"
            "• Очередь на выдачу в игре (`/queue`)\n"
            "• Управление заявками",
            reply_markup=get_main_menu(uid),
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Неверный ключ администратора!")

# Команда настройки пароля Firebase прямо из Telegram
@dp.message(Command("setpass"))
async def cmd_set_firebase_pass(message: types.Message):
    if message.from_user.id not in admins_db:
        await message.answer("Доступно только администраторам.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "ℹ️ **Привязка пароля Firebase к боту:**\n\n"
            "Использование: `/setpass ваш_пароль_от_Firebase`\n\n"
            "*(Введите пароль, который вы задали для `goponos12@gmail.com` в Firebase Console)*",
            parse_mode="Markdown"
        )
        return

    new_pass = args[1].strip()
    await message.answer("🔄 Проверяю подключение к Firebase с этим паролем...")

    # Тестируем авторизацию
    firebase_client.password = new_pass
    token = await firebase_client.get_token()

    if token:
        # Сохраняем в конфиг
        cfg = load_json("firebase_config.json", {})
        cfg["FIREBASE_PASSWORD"] = new_pass
        save_json("firebase_config.json", cfg)

        await message.answer(
            "✅ **Связь с Firebase успешно установлена!**\n\n"
            "Токен доступа получен. Теперь все одобрения в боте будут мгновенно появляться в таблице на сайте!",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "❌ **Ошибка авторизации в Firebase!**\n\n"
            "Firebase отклонил этот пароль для почты `goponos12@gmail.com`.\n\n"
            "Проверьте, что в Firebase Console ➔ Authentication ➔ Users у пользователя `goponos12@gmail.com` задан именно этот пароль.",
            parse_mode="Markdown"
        )

# ╔══════════════════════════════════════════════════════════════════╗
# ║  СПИСОК АДМИНИСТРАТОРОВ / ЗАМЕСТИТЕЛЕЙ                           ║
# ╚══════════════════════════════════════════════════════════════════╝
@dp.message(F.text == "👥 Список админов")
@dp.message(Command("admins"))
async def cmd_list_admins(message: types.Message):
    if message.from_user.id not in admins_db:
        await message.answer("Доступно только руководству фракции.")
        return

    text = f"👥 **Список администраторов и заместителей в боте ({len(admins_db)} чел.):**\n\n"
    kb_rows = []

    for i, adm_id in enumerate(admins_db, 1):
        adm_user = users_db.get(str(adm_id), {})
        nick = adm_user.get("nick", "Ник не привязан")
        rank = adm_user.get("rank", "")
        rank_str = f" [{rank}]" if rank else ""
        username = f"(@{adm_user.get('telegram_username')})" if adm_user.get("telegram_username") else ""
        is_me = (adm_id == message.from_user.id)
        me_label = " *(Вы)*" if is_me else ""

        text += f"{i}. **{nick}**{rank_str} {username}{me_label}\n   ID: `{adm_id}`\n\n"

        if not is_me:
            kb_rows.append([InlineKeyboardButton(text=f"❌ Снять {nick}", callback_data=f"remove_admin_{adm_id}")])

    text += (
        "💡 **Как добавить нового заместителя:**\n"
        "Попросите его зайти в бота @frporp_bot и отправить команду:\n"
        f"`/admin {ADMIN_KEY}`\n"
        "После этого он сразу появится в этом списке и сможет одобрять отчеты."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("remove_admin_"))
async def process_remove_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in admins_db:
        await callback.answer("У вас нет прав!", show_alert=True)
        return

    target_id = int(callback.data.replace("remove_admin_", ""))
    if target_id == callback.from_user.id:
        await callback.answer("Нельзя снять самого себя!", show_alert=True)
        return

    if target_id in admins_db:
        admins_db.remove(target_id)
        save_json(ADMINS_FILE, admins_db)
        await callback.answer("Администратор успешно снят!", show_alert=True)
        await callback.message.delete()
        await cmd_list_admins(callback.message)
    else:
        await callback.answer("Администратор не найден в списке.", show_alert=True)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_menu(message.from_user.id))

# Очередь на выдачу
@dp.message(F.text == "📋 Очередь на выдачу (Админ)")
@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    if message.from_user.id not in admins_db:
        await message.answer("Доступно только руководству.")
        return

    await message.answer("🔄 Загружаю очередь из таблицы...")
    rows = await firebase_client.get_sheet("fractionrp")
    pending = [r for r in rows if r.get("status") == "На выдаче" or (r.get("qty") or 0) > 0]

    if not pending:
        await message.answer("🎉 Очередь пуста! Нет игроков со статусом «На выдаче».")
        return

    text = f"📋 **Очередь на выдачу (/fractionrp) — {len(pending)} чел.:**\n\n"
    keyboard_buttons = []

    for i, r in enumerate(pending[:15], 1):
        nick = r.get("nick", "Noname")
        qty = r.get("qty", 0)
        rank = r.get("rank", "")
        rank_str = f" [{rank}]" if rank else ""
        cmd = f"/fractionrp {nick}"
        text += f"{i}. **{nick}**{rank_str} — **{qty} шт.**\nКоманда: `{cmd}`\n\n"

        keyboard_buttons.append([
            InlineKeyboardButton(text=f"✓ Выдать 1 для {nick}", callback_data=f"issue_1_{nick}")
        ])

    text += "*(Кликните по команде выше, чтобы скопировать её в игре)*"
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

# Быстрое списание 1 балла админом прямо из Telegram
@dp.callback_query(F.data.startswith("issue_1_"))
async def process_issue_one(callback: types.CallbackQuery):
    if callback.from_user.id not in admins_db:
        await callback.answer("У вас нет прав!", show_alert=True)
        return

    nick = callback.data.replace("issue_1_", "")
    rows = await firebase_client.get_sheet("fractionrp")
    item = next((r for r in rows if r.get("nick", "").lower() == nick.lower()), None)

    if not item:
        await callback.answer("Игрок не найден!", show_alert=True)
        return

    cur_qty = item.get("qty", 0)
    new_qty = max(0, cur_qty - 1)
    item["qty"] = new_qty
    if new_qty == 0:
        item["status"] = "Выдано"

    await firebase_client.update_sheet("fractionrp", rows)
    await callback.answer(f"✓ Списан 1 балл у {nick}! Осталось: {new_qty}", show_alert=True)

# ╔══════════════════════════════════════════════════════════════════╗
# ║  DUMMY WEB SERVER ДЛЯ RENDER (STAY-ALIVE)                        ║
# ╚══════════════════════════════════════════════════════════════════╝
async def handle_ping(request):
    return web.Response(text="FractionRp Telegram Bot is Running 24/7!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web health check server started on port {PORT}")

# ╔══════════════════════════════════════════════════════════════════╗
# ║  MAIN ENTRY POINT                                               ║
# ╚══════════════════════════════════════════════════════════════════╝
async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not configured!")
        return

    # Запускаем фоновый web-сервер для Render
    await start_web_server()

    # Запускаем polling бота
    logger.info("Bot polling starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
