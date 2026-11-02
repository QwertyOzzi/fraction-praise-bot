# Security checklist

The application must never include bot credentials, passwords, shared admin keys, or authentication credentials in tracked source files or public HTTP responses.

Before production deployment:

1. Remove all hardcoded owner and deputy authorization keys and fallback Firebase passwords in the Python application.
2. Configure an explicit list of trusted Telegram owner IDs; never grant owner access based solely on a shared string.
3. Restrict the HTTP health response to a constant readiness status without credentials or account details.
4. Restrict public support submissions by size and rate, and reject invalid formats.
5. Make report approval and Firestore balance updates reliable and idempotent. Never report a successful credit until storage succeeds.
6. Test authorization and rejection cases, restarts, concurrent updates, and Firestore outages before deployment.
7. Rotate any secrets previously committed to Git history, even after deleting them from the latest branch.

Do not merge or deploy until the above application changes have been implemented and verified.
