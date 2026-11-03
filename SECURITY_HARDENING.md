# Security hardening operations

## Deployment order

1. Record a verified backup of the Firestore `sheets`, `config/admins`, `config/bot_state`, `config/settings` and `support_tickets` documents. Store it securely outside the public repository.
2. Record the current list of authorized Telegram IDs and roles using the owner-only `/security` command. Do not recreate administrators in the cloud.
3. Deploy the matched tracker and bot releases; ensure the bot's Firebase owner account can create support tickets under the updated rules. Publish `firestore.rules` separately via Firebase Console or an authorized deployment process. The repository file is NOT auto-deployed.
4. Test: public site support ticket, authorized website edit, unauthorized website edit denial, owner and deputy Telegram access, point issue, report approval, bot restart, and read-only visitors.
5. Observe GitHub Actions results and Render logs; rollback application code if a regression is found. Reverting code does not automatically revert Firestore rules.

## Credentials

- Required Render Environment variables: `BOT_TOKEN`, `FIREBASE_PROJECT_ID`, `FIREBASE_API_KEY`, `FIREBASE_EMAIL`, `FIREBASE_PASSWORD`. Optional `DEPUTY_KEY`.
- `ADMIN_ACL_SIGNING_KEY` is recommended but **must not** be introduced or rotated arbitrarily: existing cloud signatures currently fall back to `BOT_TOKEN`. Before switching, migrate the signed ACL in a controlled, verified owner-led procedure while preserving the existing administrator IDs and roles.
- Rotate any secret that has appeared in public commit history; deleting it from the current branch is not sufficient. Do not paste secrets into Telegram chats, public support tickets, or GitHub issues.
- Require GitHub, Firebase and Render MFA and audit project collaborators.

## Operational limits

- CORS allows the current GitHub Pages origin; add approved custom domains intentionally if needed. CORS is not an authentication boundary.
- The in-process ticket limiter applies to one running server instance; for stronger DoS resistance configure edge rate limiting and bot protection with the hosting provider.
- The bot must not automatically restore unverified local administrator lists when the authoritative cloud ACL is missing.
- Periodically run real Firebase Security Rules emulator tests and backup recovery drills. Static tests alone do not prove deployment configuration is correct.
