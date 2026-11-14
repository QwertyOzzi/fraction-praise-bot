# Bot API 10.4: FBI profile widgets (first stage)

This change is additive. Existing /start, /profile, /status, FRP/Praise
transactions, security roles and AI commands are unchanged.

## Implemented

- Private HTML card, attempted via Bot API sendWidget; if Telegram restricts it with
  INPUT_MEDIA_WIDGET_NOT_ALLOWED, the bot creates an ordinary protected HTML
  message with the same balances and buttons. This is a normal Telegram text
  card, **not** a true HTML Widget.
- /widget and «✨ Виджет» in the regular agent menu.
- «🔄 Обновить карточку» below the widget: reads fresh Firestore data and
  changes the existing message via editMessageWidgetData (widget) or
  editMessageText (fallback).
- Background comparison every 90 seconds: edit only when an agent's nickname,
  rank, access status, FRP or Praise changed. The server changes
  chat_data in HTML Widgets or edits the ordinary message in fallback mode.
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
3. Telegram restricted HTML Widgets for general production bots on
   November 13, 2026. The bot will automatically show a native text card
   until Telegram grants access. No client update or token change bypasses
   this server-side restriction.
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
- Registry records contain the Telegram message ID, card kind, and last
  displayed state. The bot can resume editing the same card after Render restarts.
  They are accessible only to the Firebase owner.

References:
https://core.telegram.org/bots/widgets
https://core.telegram.org/bots/api-changelog#november-9-2026

## Telegram rollout restriction (November 14, 2026)

On November 13, Telegram Bot API contributor `levlam` confirmed that
ordinary bots cannot currently send HTML Widgets even in private chats:
https://github.com/tdlib/telegram-bot-api/issues/947

Calling /widget now falls back only for the exact server error
INPUT_MEDIA_WIDGET_NOT_ALLOWED. It does not conceal other failures, bypass
Telegram permissions, or create new routes for points and roles.

The existing text fallback stays active for that user's stored card so
subsequent /widget commands and scheduled 90-second checks edit it in
place rather than creating duplicate messages. When the rollout opens,
the native HTML widget path is still available for new cards.
