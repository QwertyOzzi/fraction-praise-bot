"""Complete, role-labelled /help reference; readable to every FBI agent.

Catalog is intentionally independent of Telegram's per-role native command
menus: seeing a command does not grant permission to use it.
"""
from html import escape

AGENT_COMMANDS = (
    ("start", "Открыть кабинет или регистрацию"),
    ("menu", "Показать главное меню"),
    ("help", "Полный список команд"),
    ("report", "Подать отчёт FRP / похвалы"),
    ("status", "Проверить свои FRP и похвалы"),
    ("profile", "Карточка сотрудника"),
    ("nick", "Запросить смену игрового ника"),
    ("rank", "Запросить смену ранга"),
    ("plus", "Подать заявку на начисление"),
    ("min", "Списать баллы (только при наличии права)"),
    ("events", "Предстоящие мероприятия FBI"),
    ("eventnotify", "Уведомления о МП: on / off"),
    ("support", "Обращение в поддержку"),
    ("bug", "Сообщить об ошибке (аналог /support)"),
    ("cancel", "Отменить текущую форму"),
    ("version", "Посмотреть версию бота"),
)

DEPUTY_COMMANDS = (
    ("admin", "Авторизация руководства: /admin ключ"),
    ("agents", "Список сотрудников и действия с ними"),
    ("stats", "Статистика отчётов за 7 и 30 дней"),
    ("pending", "Заявки, ожидающие проверки"),
    ("queue", "Очередь на выдачу в игре"),
    ("grant", "Начисление баллов с проверкой прав"),
    ("admins", "Состав руководства"),
    ("claims", "Проверка спорных регистраций"),
    ("ai", "ИИ-команды (с учётом прав доступа)"),
    ("agentpending", "Заявки агентов на начисление"),
    ("agentlogs", "Журнал действий агентов"),
)

LEADER_COMMANDS = (
    ("department", "Назначить отдел: /department Ник Отдел"),
    ("event", "Создать мероприятие и напоминания"),
    ("autopost", "Запланировать автообъявление"),
    ("schedule", "Просмотреть и отменить расписание"),
    ("news", "Отправить новость сотрудникам"),
    ("newslog", "История новостей и доставки"),
)

OWNER_COMMANDS = (
    ("security", "Проверить состояние доступа"),
    ("permissions", "Выдать / отозвать точечные права"),
    ("notifications", "Уведомления руководства: on / off"),
    ("setbanner", "Установить баннер главного меню"),
    ("removebanner", "Убрать баннер главного меню"),
    ("recoveradmins", "Восстановить архив прав, если он есть"),
    ("setpass", "Подсказка по настройке пароля в Render"),
)


def all_help_commands():
    return tuple(command for group in (
        AGENT_COMMANDS, DEPUTY_COMMANDS, LEADER_COMMANDS, OWNER_COMMANDS
    ) for command, _ in group)


def full_help_text():
    """Return one Telegram HTML-safe, complete reference with no inline menu."""
    groups = (
        ("👤 АГЕНТЫ · доступны всем", AGENT_COMMANDS),
        ("⭐ РУКОВОДСТВО · при назначенной роли и правах", DEPUTY_COMMANDS),
        ("👑 ЛИДЕР · или отдельно разрешённые функции", LEADER_COMMANDS),
        ("🛠 ВЛАДЕЛЕЦ · техническое управление", OWNER_COMMANDS),
    )
    sections = ["📚 <b>FBI · Все команды бота</b>",
                "Это полный справочник для всех ролей. "
                "Просмотр списка не предоставляет доступ к командам."]
    for heading, commands in groups:
        sections.append("\n<b>" + escape(heading) + "</b>")
        sections.extend(
            f"<code>/{escape(command)}</code> — {escape(description)}"
            for command, description in commands
        )
    sections.append(
        "\n<i>Команды руководства работают только с соответствующей "
        "ролью и разрешениями. Все действия выполняйте в личном чате бота.</i>"
    )
    text = "\n".join(sections)
    # Telegram text messages are limited to 4096 characters after HTML parsing.
    if len(text) >= 3900:
        raise ValueError("Full /help catalog is too long for one Telegram message")
    return text
