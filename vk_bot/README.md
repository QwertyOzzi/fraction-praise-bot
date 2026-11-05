# FBI Arizona RP — VK channel (shared Firebase)

Separate VK community bot, using the same Firebase project and the Telegram bot as the authority for verified agent profiles and leadership.

## Implemented initial functionality

- Private-chat VK bot and menu; 10-minute one-use TG -> VK account linking, with safe /vkunlink.
- Live nickname/rank/FRP/praise balances, site link.
- VK report wizard with reason plus proof (link, photo, video, document); /myreports.
- VK report review by VK leaders via /pending and /review, and by TG leaders via /vkpending and /vkreview. Report, balance change, and log are committed atomically.
- Confirmed agent deductions and credit requests using the Telegram bot's existing atomic agent_actions logic. VK and TG can review the shared agent_add_requests.
- Staff directory, issuance queue, issue-one, manual credit, action log, support tickets, VK-only news and news history.
- No group-message operation, fail-closed authorization, no VK-based self-assignment of leadership roles.

This **does not yet have full feature parity**. Remaining: VK-only registration and independent nickname claim review; all agent profile/rank editing and leadership management from VK; natural Gemini commands; multimedia albums; universal TG+VK broadcast; complete Telegram claim/cooldown UI; editing old Telegram report queue from VK. Keep Telegram running.

## 1. VK community

Create or use a VK community in which you are an administrator. Enable community messages and bot capabilities. In community management, find "Работа с API", create a community key with **messages** rights, and enable **Bots Long Poll API**, event type **message_new**. The numerical community ID is positive (no minus sign). No public VK Callback URL is necessary for Long Poll.

Official documentation:
https://dev.vk.ru/ru/api/bots/getting-started
https://dev.vk.ru/ru/api/bots-long-poll/getting-started

## 2. Publish Firestore rules

In the website repository, merge the separate owner-only rules changes for vk_link_codes, vk_accounts, tg_vk_links and vk_reports. **Publish the merged rules manually in Firebase Console -> Firestore Database -> Rules.** A GitHub commit does not deploy them.

Do not overwrite the active rules with an out-of-date local copy; add the new matches to the live version while retaining protections for the legacy bot, website, support tickets, agent logs and news.

## 3. Telegram service

Add VK_ACL_SIGNING_KEY as a long random secret (at least 32 characters) to the existing Telegram Render service. Leave the existing BOT_TOKEN and ADMIN_ACL_SIGNING_KEY alone, because the existing TG administrator list is signed with them. Deploy updated bot.py and send **/vkacl** once from the owner's private Telegram chat. This publishes a separately signed VK ACL without changing the existing Telegram ACL. When Telegram saves role changes, the VK signature updates too.

## 4. Deploy the VK service

Deploy this SAME GitHub repo as a separate Render Web Service:
- Build command: pip install -r vk_bot/requirements.txt
- Start command: python -m vk_bot.main
- Health route: GET /health
- Environment: VK_GROUP_TOKEN, VK_GROUP_ID, FIREBASE_PROJECT_ID, FIREBASE_API_KEY, FIREBASE_EMAIL, FIREBASE_PASSWORD, VK_ACL_SIGNING_KEY; PORT assigned by the host.

The Firebase values MUST refer to the same project and an existing Firebase Auth identity recognized as owner by the deployed Firestore Rules. VK_ACL_SIGNING_KEY must match the separate Telegram service variable. Never put access tokens or Firebase password in GitHub, chat, screenshots or source files.

## 5. Smoke test

1. Register and verify your nickname with the original Telegram bot.
2. In its private chat, send /vklink. The code expires after 10 minutes.
3. In private messages to the VK community, send /link CODE, then /status.
4. Submit one test /report; on Telegram run /vkpending and decide with /vkreview vr-ID approve. Verify the site balance changes once.
5. Reapply the same decision: no second credit. In Telegram use /vkunlink: protected VK operations must stop immediately.

## VK commands

- /start, /help, /menu — navigation
- /link CODE — link your TG-verified identity
- /status — live profile and balances
- /report, /myreports — submit/review own reports
- /min frp Nick_Name 2 — deduct with explicit confirmation
- /plus praise Nick_Name 2 Reason — request credit with confirmation
- /support Message — create a site support ticket
- /pending, /review vr-ID approve — review VK reports (staff)
- /agentpending, /review ar-ID approve — review credit requests (staff)
- /agents, /queue, /issue frp Nick_Name, /grant frp Nick_Name 2 — staff
- /agentlogs, /news Text, /newslog — staff logs and VK-only announcements
- /cancel — cancel an unfinished form

This release is a cross-platform foundation rather than a drop-in replacement for Telegram.