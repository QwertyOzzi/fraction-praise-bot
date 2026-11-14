# Bot API 10.4: FBI profile widgets (first stage)

This change is additive. Existing /start, /profile, /status, FRP/Praise
transactions, security roles and AI commands are unchanged.

## Implemented

- Private HTML card in a Telegram message, sent via Bot API sendWidget.
- /widget and «✨ Виджет» in the regular agent menu.
- «🔄 Обновить карточку» below the widget: reads fresh Firestore data and
  changes the existing message via editMessageWidgetData.
- Background comparison every 90 seconds: edit only when an agent's nickname,
  rank, access status, FRP or Praise changed. This is a private static widget
  with editable chat_data, not a public shared widget.
- One active message pointer per agent in a new Firestore collection, preserved
  across Render restarts.
- Firestore failures do not show false zeroes. Missing or duplicate rows show
  «—»; disabled profiles are masked on their next update.
- No widget_query handlers, no extra polling process, no Telegram beta
  dependencies and no Firebase calls from widget JavaScript.

## Deployment prerequisites

1. Publish the corresponding Firestore rules change from
   QwertyOzzi/fraction-praise-tracker. The collection is
   fbi_profile_widgets/{telegramUserId}; only isOwner() may access it.
   **Important:** merging rules on GitHub does not deploy them to Firebase.
   Apply them in the Firebase console or with your normal Firebase CLI process.
2. Deploy the bot branch after these rules are active. No new secrets needed.
3. Use a Telegram client that supports HTML Widgets. Older clients show the
   widget caption. Bot API 10.4 production supports widgets in private chats.
4. In a private chat with the bot, send /widget, or open /start -> ✨ Виджет.
   Tap «🔄 Обновить карточку» below the card to refresh.
5. Change FRP/Praise or rank using the existing approved workflows; the card
   should change within the next 90-second background comparison.
6. Restart the bot on Render and confirm the existing card still refreshes.
   Check that a disabled profile has no visible balance after refresh.

The background task is enabled by default. To disable only automatic polling,
set the Render environment variable FBI_WIDGET_AUTO_REFRESH=0. Manual refresh
continues to work. Do not add a separate getUpdates/polling process.

## Limitations

- Manual refresh uses a regular Telegram inline button BELOW the widget.
  aiogram 3.31 doesn't yet handle the new widget_query update type. Native
  inside-widget TelegramWidget.request() interaction can be introduced after
  official library support and a handler security review.
- Widget HTML code itself cannot change. A new layout requires a new widget
  message; its chat_data state can be updated.
- Background updates are change-driven polling, not immediate Firebase push.
  Per cycle: one registry query and three Firestore document reads; edits and
  registry writes only for changed profiles.
- Auto-updates require publishing the Firestore security rule. Without it, the
  bot can send manual cards but warns about missing persistence.

## Security

- The HTML has no network access. It receives only the requesting agent's
  nickname, rank and FRP/Praise display values.
- The bot checks the current owner-only Firebase bot_state and two sheets.
  No widget operation can assign roles or change game balances.
- Registry records contain only a Telegram message ID and the last card state.
  They are accessible only to the Firebase owner.

References:
https://core.telegram.org/bots/widgets
https://core.telegram.org/bots/api-changelog#november-9-2026
