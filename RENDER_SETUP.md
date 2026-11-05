> ⚠️ Ранние версии этой инструкции содержали реальные секреты. История Git сохраняет удалённые значения. Перевыпустите токен Telegram и старые ключи, если они были действующими, и смените раскрытые пароли.

# 🚀 Пошаговая инструкция: Запуск Telegram-бота на Render (24/7 Бесплатно)

Бот уже полностью написан и готов к работе в папке:
👉 **`C:\Users\gopon\Downloads\fraction-praise-bot\`**

---

## Шаг 1. Создать репозиторий на GitHub и загрузить бота

1. Перейдите на **[github.com/new](https://github.com/new)**
2. Назовите репозиторий: **`fraction-praise-bot`**
3. Сделайте его **Public** или **Private** (Render поддерживает оба)
4. Нажмите **Create repository**
5. Откройте терминал (Git Bash) и выполните:

```bash
cd C:/Users/gopon/Downloads/fraction-praise-bot
git init
git add .
git commit -m "initial commit of fraction tg bot"
git branch -M main
git remote add origin https://github.com/QwertyOzzi/fraction-praise-bot.git
git push -u origin main
```
*(Либо просто перетащите файлы `bot.py`, `requirements.txt`, `.gitignore` через кнопку Upload files на GitHub).*

---

## Шаг 2. Регистрация на Render (в 1 клик)

1. Перейдите на сайт **[render.com](https://render.com/)**
2. Нажмите в правом верхнем углу **GET STARTED** (или Sign In)
3. Выберите **«GitHub»** — и авторизуйтесь через свой GitHub-аккаунт. Это бесплатно и без кредитных карт.

---

## Шаг 3. Создать Web Service на Render

1. В панели управления Render нажмите синюю кнопку **«+ New»** (вверху справа) ➔ выберите **«Web Service»**.
2. В списке репозиториев найдите **`fraction-praise-bot`** и нажмите кнопку **«Connect»** напротив него.
   *(Если репозиторий не отображается, нажмите «Configure access» и дайте доступ к репозиторию)*.
3. Заполните настройки:
   - **Name:** `fraction-praise-bot`
   - **Region:** `Frankfurt (EU Central)` *(любой европейский)*
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** выберите **Free** ($0 / month)

---

## Шаг 4. Добавить переменные среды (Environment Variables)

На той же странице прокрутите чуть ниже до блока **«Environment Variables»** и добавьте переменные:

| Key (Имя переменной) | Value (Значение) |
|---|---|
| `BOT_TOKEN` | Создайте и храните токен в секретных переменных Render; никогда не добавляйте его в GitHub |
| `ADMIN_KEY` | Устаревший способ; текущий бот использует проверенный Telegram ID владельца и подписанные роли |
| `FIREBASE_PROJECT_ID` | `fraction-praise` |
| `FIREBASE_API_KEY` | Скопируйте API key из настроек проекта Firebase в переменную Render |
| `FIREBASE_EMAIL` | Firebase Auth email владельца, храните в Render |
| `FIREBASE_PASSWORD` | Пароль Firebase Auth; не публикуйте и не присылайте в чат |

---

## Шаг 5. Запуск бота!

1. Нажмите внизу синюю кнопку **«Deploy Web Service»**.
2. Render начнет установку библиотек и запуск. Через 1–2 минуты в логах появится надпись:
   `Bot polling starting...`
3. **Бот запущен и работает в Telegram 24/7!**

---

## 🎮 Как пользоваться ботом в Telegram:

1. Откройте вашего бота в Telegram: **[@frporp_bot](https://t.me/frporp_bot)**
2. Нажмите **`/start`** и укажите свой ник.
3. Чтобы стать главным администратором / лидером в боте, отправьте сообщение:
   ```text
   /start
   ```
4. Бот подтвердит: *«Вы успешно авторизованы как Администратор / Руководитель!»*
5. Теперь:
   - Когда любой участник фракции сдает отчет через кнопку **«📝 Подать отчет»** — вам в личку прилетает карточка отчета с кнопками **`[ ✅ Одобрить (+1) ]`** и **`[ ❌ Отклонить ]`**.
   - Нажатие на кнопку **`[ ✅ Одобрить ]`** автоматически начисляет +1 балл в общую таблицу на сайте!
   - Кнопка **«📋 Очередь на выдачу (Админ)»** показывает список игроков для выдачи в игре с готовыми командами `/fractionrp Ник`.
   - Кнопка **«🌐 Открыть таблицу»** открывает ваш сайт прямо внутри Telegram!
