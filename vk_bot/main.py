"""FBI Arizona RP community VK bot. Run: python -m vk_bot.main

Only private messages are processed. All balances and permissions are read
from the existing Firebase project; VK identity is linked via /vklink in TG.
"""
import asyncio
import logging
import os
import re
import secrets
import time
import uuid

from aiohttp import web
from vk_api import VkApi
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

from agent_actions import (
    AgentOperationError, commit_agent_debit, create_add_request,
    decide_add_request, list_collection, create_announcement,
    finish_announcement, _create, _sheet_update, timestamp,
)
from vk_bot.backend import (
    Firebase, SHEETS, actor_for_vk, bot_state, client_commit, create_support,
    decide_report, get_reports, is_staff, redeem_link, submit_report,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fbi_vk")
WEB_URL = "https://qwertyozzi.github.io/fraction-praise-tracker/"
MAX_MESSAGE = 3700
ADMIN_ROLES = {"owner", "leader", "deputy"}


def keyboard():
    kb = VkKeyboard(one_time=False, inline=False)
    kb.add_button("📊 Статус", color=VkKeyboardColor.PRIMARY)
    kb.add_button("📝 Отчёт", color=VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button("📌 Заявки")
    kb.add_button("🧑‍💼 Агенты")
    kb.add_line()
    kb.add_button("📋 Очередь")
    kb.add_button("📥 Запросы")
    kb.add_line()
    kb.add_button("🌐 Таблица")
    kb.add_button("❓ Помощь")
    return kb.get_keyboard()


class CommunityBot:
    def __init__(self):
        self.token = os.environ["VK_GROUP_TOKEN"]
        self.group_id = int(os.environ["VK_GROUP_ID"])
        self.db = Firebase()
        session = VkApi(token=self.token, api_version="5.199")
        self.api = session.get_api()
        self.longpoll = VkBotLongPoll(session, self.group_id)
        self.states = {}
        self.limits = {}

    async def say(self, uid, text, menu=False):
        args = {"user_id": int(uid), "message": str(text)[:MAX_MESSAGE],
                "random_id": secrets.randbelow(2**31)}
        if menu:
            args["keyboard"] = keyboard()
        await asyncio.to_thread(self.api.messages.send, **args)

    def can_write(self, uid):
        now = time.monotonic()
        recent = [x for x in self.limits.get(uid, []) if now-x < 60]
        if len(recent) >= 5:
            return False
        recent.append(now)
        self.limits[uid] = recent
        return True

    async def admin_report_list(self, uid):
        reports = [r for r in await get_reports(self.db, 100)
                   if r.get("status") == "pending"]
        if not reports:
            await self.say(uid, "📌 Новых отчётов из VK нет.")
            return
        lines = ["📌 VK-отчёты на рассмотрении:"]
        for r in reports[:20]:
            lines.append(f"\n{r['id']} — {r['nick']} — "
                         f"{r['sheet']} +{r['amount']}\n"
                         f"Причина: {r['reason']}\n"
                         f"Доказательство: {r['proof'][:130]}")
        lines.append("\nРешить: /review ID approve или /review ID reject")
        await self.say(uid, "\n".join(lines))

    async def show_status(self, uid, actor):
        nick = actor["profile"]["nick"]
        rows_frp, rows_praise = await asyncio.gather(
            self.db.sheet("frp"), self.db.sheet("praise"))
        def show(rows):
            matching = [r for r in rows if r["nick"].casefold() == nick.casefold()]
            return str(matching[0]["qty"]) if len(matching) == 1 else "0 (нет записи)"
        await self.say(uid, "🛡 FBI · Личный кабинет\n"
                       f"👤 {nick}\n🎖 Ранг: {actor['profile'].get('rank','?')}\n"
                       f"🎖 FRP: {show(rows_frp)}\n"
                       f"🌟 Похвала: {show(rows_praise)}\n"
                       f"🔐 Роль: {actor['role']}\n"
                       f"🌐 {WEB_URL}", menu=True)

    @staticmethod
    def proof_from_message(msg):
        text = str(msg.get("text") or "").strip()
        if text and (text.startswith("https://") or text.startswith("http://")):
            return text[:1200]
        for a in msg.get("attachments", []) or []:
            item = a.get(a.get("type"), {})
            if a.get("type") == "photo":
                owner, pid = item.get("owner_id"), item.get("id")
                if type(owner) is int and type(pid) is int:
                    return f"https://vk.com/photo{owner}_{pid}"
            if a.get("type") == "video":
                owner, vid = item.get("owner_id"), item.get("id")
                if type(owner) is int and type(vid) is int:
                    return f"https://vk.com/video{owner}_{vid}"
            if a.get("type") == "doc":
                url = item.get("url", "")
                if isinstance(url, str) and url.startswith("https://"):
                    return url[:1200]
        return ""

    async def ongoing(self, uid, msg, actor):
        """Text-keyboard wizard and explicit action confirmation, bound to VK ID."""
        state = self.states.get(uid)
        if not state:
            return False
        if time.monotonic() > state["expiry"]:
            self.states.pop(uid, None)
            await self.say(uid, "⌛ Время действия истекло. Начните заново.")
            return True
        raw = str(msg.get("text") or "").strip()
        if raw.lower() in ("/cancel", "отмена", "❌ отмена"):
            self.states.pop(uid, None)
            await self.say(uid, "Отменено.", menu=True)
            return True
        if raw.startswith("/"):
            self.states.pop(uid, None)
            return False
        step = state["step"]
        if step == "report_kind":
            kind = SHEETS.get(raw.lower())
            if not kind:
                await self.say(uid, "Выберите тип: frp или praise.")
                return True
            state["kind"] = kind
            state["step"] = "report_amount"
            await self.say(uid, "Введите количество от 1 до 20:")
        elif step == "report_amount":
            if not raw.isdecimal() or not 1 <= int(raw) <= 20:
                await self.say(uid, "Нужно целое число от 1 до 20.")
                return True
            state["amount"] = int(raw)
            state["step"] = "report_reason"
            await self.say(uid, "Введите причину отчёта (текст):")
        elif step == "report_reason":
            if not 1 <= len(raw) <= 400:
                await self.say(uid, "Причина должна содержать 1–400 символов.")
                return True
            state["reason"] = raw
            state["step"] = "report_proof"
            await self.say(uid, "Отправьте ссылку, фото, видео или документ как доказательство:")
        elif step == "report_proof":
            proof = self.proof_from_message(msg)
            if not proof:
                await self.say(uid, "Нужна ссылка https:// или вложение VK (фото/видео/документ).")
                return True
            state["proof"] = proof
            state["step"] = "report_confirm"
            await self.say(uid, "📝 Проверьте отчёт:\n"
                           f"Тип: {state['kind']}\nКоличество: {state['amount']}\n"
                           f"Причина: {state['reason']}\n"
                           f"Доказательство: {proof}\n"
                           "Напишите ПОДТВЕРДИТЬ или ОТМЕНА.")
        elif step == "report_confirm":
            if raw.casefold() != "подтвердить":
                await self.say(uid, "Напишите ПОДТВЕРДИТЬ или ОТМЕНА.")
                return True
            self.states.pop(uid, None)
            if not self.can_write(uid):
                await self.say(uid, "⚠️ Лимит: не более 5 новых операций за минуту.")
                return True
            # Reverify linked profile on submit; links/rights may have been revoked.
            current = await actor_for_vk(self.db, uid)
            if not current or current["tg_id"] != actor["tg_id"]:
                raise AgentOperationError("Профиль изменён; начните заново.")
            r = await submit_report(self.db, current, state["kind"],
                                    state["amount"], state["reason"], state["proof"])
            await self.say(uid, "✅ Отчёт отправлен: " + r["id"] +
                           "\nСтатус можно проверить через /myreports.")
        elif step == "support":
            self.states.pop(uid, None)
            if not self.can_write(uid):
                await self.say(uid, "Лимит обращений. Повторите позднее.")
                return True
            content = raw or self.proof_from_message(msg)
            ticket_id = await create_support(self.db, actor, content)
            await self.say(uid, f"✅ Обращение #{ticket_id[-6:]} сохранено на сайте.")
        elif step == "agent_confirm":
            if raw.casefold() != "подтвердить":
                await self.say(uid, "Напишите ПОДТВЕРДИТЬ или ОТМЕНА.")
                return True
            self.states.pop(uid, None)
            if not self.can_write(uid):
                await self.say(uid, "Лимит операций: до 5 в минуту.")
                return True
            if state["action"] == "min":
                result, _log = await commit_agent_debit(
                    self.db, ClientSessionFactory,
                    op_id=state["id"], actor_id=actor["tg_id"],
                    actor_nick=actor["profile"]["nick"],
                    sheet=state["sheet"], target=state["target"],
                    amount=state["amount"], alias="", raw="")
                await self.say(uid, "✅ Списание: " + result)
            else:
                request = await create_add_request(
                    self.db, ClientSessionFactory,
                    actor_id=actor["tg_id"], actor_nick=actor["profile"]["nick"],
                    sheet=state["sheet"], target=state["target"],
                    amount=state["amount"], reason=state["reason"],
                    evidence_kind="", evidence_id="", raw="")
                await self.say(uid, f"✅ Заявка {request['id']} отправлена руководству.")
        return True

    async def handle(self, msg):
        uid, peer = msg.get("from_id"), msg.get("peer_id")
        if type(uid) is not int or uid <= 0 or peer != uid or msg.get("out"):
            return
        raw = str(msg.get("text") or "").strip()
        if not raw and not msg.get("attachments"):
            return
        text = raw.casefold()
        if text in ("/cancel", "отмена", "❌ отмена"):
            self.states.pop(uid, None)
            await self.say(uid, "Отменено.", menu=True)
            return
        if text.startswith("/link "):
            tg_id = await redeem_link(self.db, uid, raw.split(maxsplit=1)[1])
            await self.say(uid, f"✅ VK привязан к Telegram ID {tg_id}.\n"
                           "Отправьте /start.", menu=True)
            return
        actor = await actor_for_vk(self.db, uid)
        if not actor:
            await self.say(uid, "🛡 FBI · VK-бот\n"
                           "Для защиты профилей нужно связать VK с Telegram.\n"
                           "В Telegram-боте отправьте /vklink, затем сюда:\n"
                           "/link ВАШ_ОДНОРАЗОВЫЙ_КОД\n"
                           "Если профиль в Telegram ещё не подтверждён, "
                           "дождитесь решения руководства.", menu=True)
            return
        if await self.ongoing(uid, msg, actor):
            return
        if text in ("/start", "/menu", "меню", "❓ помощь", "/help"):
            await self.say(uid, "🛡 FBI · VK\n"
                           "Агент: /status /report /myreports /support\n"
                           "Баланс: /min frp Ник 2\n"
                           "Запрос: /plus praise Ник 1 Причина\n"
                           "Руководство: /pending /agentpending /review "
                           "/grant /agents /queue /issue /news /newslog\n"
                           "Отмена формы: /cancel\n"
                           "Привязка: /link КОД (из Telegram /vklink)", menu=True)
            return
        if text in ("/status", "📊 статус"):
            await self.show_status(uid, actor)
            return
        if text in ("/report", "📝 отчёт"):
            self.states[uid] = {"step": "report_kind",
                                "expiry": time.monotonic()+600}
            await self.say(uid, "📝 Выберите отчёт: frp или praise.")
            return
        if text.startswith("/report "):
            # One-line report: /report frp 2 Причина | https://proof
            match = re.fullmatch(
                r"/report\s+(\w+)\s+(\d{1,2})\s+(.{1,400}?)\s*\|\s*(\S.{0,1100})",
                raw, flags=re.I | re.S)
            if not match:
                await self.say(uid, "Формат: /report frp 2 Причина | https://ссылка\n"
                               "Или просто /report для пошаговой формы.")
                return
            if not self.can_write(uid):
                await self.say(uid, "Не более 5 операций в минуту.")
                return
            req = await submit_report(self.db, actor, match[1].lower(),
                                      int(match[2]), match[3], match[4])
            await self.say(uid, f"✅ Отчёт {req['id']} сохранён.")
            return
        if text in ("/myreports", "📌 заявки"):
            all_reports = [x for x in await get_reports(self.db, 100)
                           if x.get("tg_id") == actor["tg_id"]]
            summary = [f"{x['id']}: {x['sheet']} +{x['amount']} — {x['status']}"
                       for x in all_reports[:20]]
            await self.say(uid, "\n".join(summary) if summary
                           else "Ваших VK-отчётов пока нет.")
            return
        if text in ("/support", "/bug"):
            self.states[uid] = {"step": "support", "expiry": time.monotonic()+600}
            await self.say(uid, "💬 Опишите проблему или отправьте ссылку на скриншот.")
            return
        if text.startswith("/support "):
            if not self.can_write(uid):
                await self.say(uid, "Не более 5 новых обращений в минуту.")
                return
            ticket_id = await create_support(self.db, actor, raw.split(maxsplit=1)[1])
            await self.say(uid, "✅ Обращение создано: #" + ticket_id[-6:])
            return
        if text in ("🌐 таблица", "/site"):
            await self.say(uid, "🌐 Таблицы ФРП и похвалы:\n" + WEB_URL)
            return
        if text.startswith("/min ") or text.startswith("/plus "):
            parts = raw.split(maxsplit=4)
            action = parts[0][1:].lower()
            if len(parts) < 4 or not parts[3].isdecimal() or not 1 <= int(parts[3]) <= 20:
                await self.say(uid, "Формат: /min frp Nick_Name 2\n"
                               "/plus praise Nick_Name 2 Причина")
                return
            if action == "plus" and (len(parts) < 5 or not parts[4].strip()):
                await self.say(uid, "Для заявки /plus обязательно укажите причину.")
                return
            sheet = SHEETS.get(parts[1].lower())
            if not sheet:
                raise AgentOperationError("Тип: frp или praise.")
            rows = await self.db.sheet(sheet)
            matching = [x for x in rows if x["nick"].casefold() == parts[2].casefold()]
            if len(matching) != 1:
                raise AgentOperationError("Ник не найден однозначно в таблице.")
            state = {"step": "agent_confirm", "action": action,
                     "id": "agent-" + uuid.uuid4().hex[:20],
                     "sheet": sheet, "target": matching[0],
                     "amount": int(parts[3]), "reason": parts[4][:700] if len(parts)>4 else "",
                     "expiry": time.monotonic()+300}
            self.states[uid] = state
            await self.say(uid, f"{'➖ Списание' if action=='min' else '➕ Запрос начисления'}\n"
                           f"Агент: {matching[0]['nick']}\n"
                           f"Баланс: {matching[0]['qty']}\n"
                           f"Количество: {state['amount']}\n"
                           "Напишите ПОДТВЕРДИТЬ или ОТМЕНА.")
            return
        if not is_staff(actor):
            await self.say(uid, "Неизвестная команда или нужны права руководства. /help")
            return
        if text in ("/pending", "📌 на проверке"):
            await self.admin_report_list(uid)
            return
        if text in ("/agentpending", "📥 запросы"):
            requests = await list_collection(self.db, ClientSessionFactory,
                                             "agent_add_requests", limit=80)
            pending = [r for r in requests if r.get("status") == "pending"]
            lines = [f"{r['id']} — {r['actor_nick']} → {r['target']['nick']} "
                     f"+{r['amount']} ({r['sheet']})\n"
                     f"Причина: {r.get('reason') or r.get('evidence_kind')}"
                     for r in pending[:20]]
            await self.say(uid, "\n\n".join(lines) if lines else "Новых заявок нет.")
            return
        if text.startswith("/review "):
            parts = raw.split()
            if len(parts) != 3 or parts[2].lower() not in ("approve", "reject"):
                await self.say(uid, "Формат: /review ID approve или /review ID reject")
                return
            if parts[1].startswith("vr-"):
                result, req = await decide_report(self.db, parts[1], actor, parts[2].lower())
                await self.say(uid, "Решение: " + req["status"])
                if result == "applied":
                    try:
                        await self.say(int(req["vk_id"]),
                                       f"📌 Ваш отчёт {req['id']}: {req['status']}")
                    except Exception:
                        logger.warning("VK report applicant notification failed")
            elif parts[1].startswith("ar-"):
                result, req = await decide_add_request(
                    self.db, ClientSessionFactory, request_id=parts[1],
                    reviewer_id=actor["tg_id"], decision=parts[2].lower())
                await self.say(uid, "Решение: " + req["status"])
            else:
                await self.say(uid, "Неизвестный тип заявки.")
            return
        if text in ("/agents", "🧑‍💼 агенты"):
            state = await bot_state(self.db)
            profiles = [p for p in state.get("users", {}).values()
                        if isinstance(p, dict) and p.get("nick") and
                        p.get("verification_status") not in
                        ("pending", "needs_proof", "disabled", "disconnected",
                         "proof_submitted", "cooldown", "retry_ready")]
            def key(p):
                rank = str(p.get("rank", "0"))
                return (-int(rank) if rank.isdecimal() else -10 if rank=="X" else 0,
                        str(p["nick"]).casefold())
            profiles.sort(key=key)
            await self.say(uid, "🧑‍💼 Агенты (старшие первыми):\n" +
                           "\n".join(f"{p.get('rank','?')} — {p['nick']}"
                                     for p in profiles[:65]))
            return
        if text in ("/queue", "📋 очередь"):
            frp, praise = await asyncio.gather(self.db.sheet("frp"),
                                               self.db.sheet("praise"))
            items = [(kind, r) for kind, rows in (("frp", frp), ("praise", praise))
                     for r in rows if int(r.get("qty") or 0) > 0]
            lines = [f"{kind}: {r['nick']} ×{r['qty']}"
                     for kind, r in items[:60]]
            await self.say(uid, "📋 Очередь:\n" + ("\n".join(lines) if lines else "Пусто"))
            return
        if text.startswith("/issue "):
            parts = raw.split()
            if len(parts) != 3 or not SHEETS.get(parts[1].lower()):
                raise AgentOperationError("Формат: /issue frp Nick_Name")
            rows = await self.db.sheet(SHEETS[parts[1].lower()])
            found = [r for r in rows if r["nick"].casefold() == parts[2].casefold()]
            if len(found) != 1:
                raise AgentOperationError("Ник не найден однозначно.")
            result, _ = await commit_agent_debit(
                self.db, ClientSessionFactory, op_id="agent-"+uuid.uuid4().hex[:20],
                actor_id=actor["tg_id"], actor_nick=actor["profile"]["nick"],
                sheet=SHEETS[parts[1].lower()], target=found[0], amount=1)
            await self.say(uid, "✅ Выдача учтена: " + result)
            return
        if text.startswith("/grant "):
            parts = raw.split()
            if len(parts) != 4 or not parts[3].isdecimal() or not 1 <= int(parts[3]) <= 20:
                raise AgentOperationError("Формат: /grant frp Nick_Name 2")
            sheet = SHEETS.get(parts[1].lower())
            if not sheet or not re.fullmatch(r"[A-Za-z0-9_]{3,30}", parts[2]):
                raise AgentOperationError("Неверный тип или ник.")
            if not self.can_write(uid):
                raise AgentOperationError("Лимит новых операций 5/мин.")
            outcome = await manual_grant(self.db, actor, sheet, parts[2], int(parts[3]))
            await self.say(uid, f"✅ Начисление {parts[2]} +{parts[3]}: {outcome}")
            return
        if text in ("/agentlogs", "/logs"):
            logs = await list_collection(self.db, ClientSessionFactory,
                                         "agent_action_logs", limit=30)
            lines = [f"{x.get('action')} {x.get('target_nick')} "
                     f"{x.get('amount')} ({x.get('sheet')})"
                     for x in logs[:20]]
            await self.say(uid, "📜 Журнал:\n" + ("\n".join(lines) if lines else "Пусто"))
            return
        if text in ("/newslog",):
            entries = await list_collection(self.db, ClientSessionFactory,
                                            "fbi_announcements", limit=20)
            lines = [f"{x.get('created_at','')[:16]} {x.get('title')} "
                     f"{x.get('status')}: {x.get('sent',0)}/{x.get('recipients',0)}"
                     for x in entries[:15]]
            await self.say(uid, "📣 История:\n" + ("\n".join(lines) if lines else "Пусто"))
            return
        if text.startswith("/news "):
            if actor["role"] not in ("owner", "leader"):
                raise AgentOperationError("Рассылку может отправлять только лидер.")
            message = raw.split(maxsplit=1)[1].strip()
            if not 1 <= len(message) <= 3000:
                raise AgentOperationError("Текст: 1–3000 символов.")
            await self.broadcast_news(uid, actor, message)
            return
        await self.say(uid, "Команда неизвестна. Отправьте /help.")

    async def broadcast_news(self, uid, actor, message):
        # Only send to VK accounts; the Telegram bot handles TG broadcasts.
        all_accounts = await self.db.list_documents("vk_accounts", limit=2000)
        recipients = []
        for doc in all_accounts:
            try:
                vk_id = int(doc["name"].rsplit("/", 1)[-1])
                if await actor_for_vk(self.db, vk_id):
                    recipients.append(vk_id)
            except Exception:
                continue
        entry = await create_announcement(
            self.db, ClientSessionFactory, actor_id=actor["tg_id"],
            title="FBI · Новости VK", message=message, category="news",
            recipients=len(recipients))
        if entry.get("_already_persisted"):
            raise AgentOperationError("Новость уже создана; проверьте /newslog.")
        sent = failed = 0
        for recipient in recipients:
            try:
                await self.say(recipient, "📣 FBI · Новость\n\n" + message)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.07)
        await finish_announcement(self.db, ClientSessionFactory, entry["id"],
                                  sent=sent, failed=failed)
        await self.say(uid, f"✅ Рассылка VK: доставлено {sent}, ошибок {failed}.")

    async def run(self):
        while True:
            try:
                # Bots Long Poll yields a generator of events. The blocking
                # network request is moved to a worker thread.
                it = self.longpoll.listen()
                while True:
                    event = await asyncio.to_thread(next, it)
                    if event.type != VkBotEventType.MESSAGE_NEW:
                        continue
                    msg = event.object.get("message", {}) if isinstance(event.object, dict) else {}
                    try:
                        await self.handle(msg)
                    except AgentOperationError as exc:
                        if isinstance(msg.get("from_id"), int):
                            await self.say(msg["from_id"], "⚠️ " + str(exc))
                    except Exception:
                        logger.exception("VK message failed")
                        if isinstance(msg.get("from_id"), int):
                            await self.say(msg["from_id"], "⚠️ Ошибка обработки. Повторите позже.")
            except (Exception,):
                logger.exception("VK longpoll disconnected; reconnecting")
                await asyncio.sleep(4)
                self.longpoll = VkBotLongPoll(VkApi(token=self.token,
                                                    api_version="5.199"),
                                              self.group_id)


def ClientSessionFactory():
    from aiohttp import ClientSession, ClientTimeout
    return ClientSession(timeout=ClientTimeout(total=20))


async def manual_grant(client, actor, sheet, nick, amount):
    """Atomic manual credit + immutable audit, independent from legacy bot_state."""
    for attempt in range(4):
        doc = await client.get_document("sheets/" + sheet)
        rows = client._parse_firestore_rows(
            doc.get("fields", {}).get("rows", {}).get("arrayValue", {}).get("values", []))
        exact = [r for r in rows if r["nick"].casefold() == nick.casefold()]
        if len(exact) > 1:
            raise AgentOperationError("Дублирующийся ник в таблице.")
        snap = await bot_state(client)
        registered = next((x for x in snap["users"].values()
                           if isinstance(x, dict) and str(x.get("nick", "")).casefold() == nick.casefold()
                           and x.get("verification_status") not in
                           ("pending", "disabled", "disconnected", "needs_proof",
                            "proof_submitted", "cooldown", "retry_ready")), None)
        rank = str(registered.get("rank", "8")) if registered else "8"
        if exact:
            exact[0]["qty"] += amount
            exact[0]["status"] = "На выдаче"
        else:
            rows.append({"id": str(uuid.uuid4().int)[:17], "nick": nick,
                         "fullName": nick + " (" + rank + ")", "rank": rank,
                         "qty": amount, "status": "На выдаче", "proof": {"text": "", "links": []}})
        op_id = "vk-grant-" + uuid.uuid4().hex[:20]
        status = await client_commit(client, [
            _sheet_update(client, sheet, doc["updateTime"], rows),
            _create(client, "agent_action_logs/" + op_id,
                    {"id": op_id, "action": "manual_grant", "source": "vk",
                     "actor_id": actor["tg_id"], "actor_nick": actor["profile"]["nick"],
                     "target_nick": nick, "sheet": sheet, "amount": amount,
                     "created_at": timestamp()})
        ])
        if status == "applied":
            return op_id
        await asyncio.sleep(0.1 * (attempt+1))
    raise AgentOperationError("Не удалось безопасно начислить баллы: конфликт.")


async def health(_request):
    return web.Response(text="OK")


async def main():
    for env in ("VK_GROUP_TOKEN", "VK_GROUP_ID", "FIREBASE_PROJECT_ID",
                "FIREBASE_API_KEY", "FIREBASE_EMAIL", "FIREBASE_PASSWORD"):
        if not os.getenv(env):
            raise RuntimeError("Missing environment variable: " + env)
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()
    await CommunityBot().run()


if __name__ == "__main__":
    asyncio.run(main())
