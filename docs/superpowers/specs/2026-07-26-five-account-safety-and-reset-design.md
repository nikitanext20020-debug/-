# Five-Account Safety and Reset Design

## Goal

Make the Neuro-Commenting process safe for five concurrent Telegram accounts,
keep account/ban/rate-limit state consistent across workers and the dashboard,
and provide separate safe statistics and operational reset actions.

## Safety contract

- Account-local channel bans remain in `channel_bans`; one account's channel
  ban must not prevent another account from trying the channel.
- Telegram account statuses (`banned`, `deactivated`, `frozen`) are
  authoritative and are never cleared by a statistics or operational reset.
- Settings, Telegram sessions, discovered channels, global structural
  exclusions, joined-channel history, and parsed users are preserved.
- A statistics reset removes reporting/history rows only.
- An operational reset stops running workers before clearing local retry state
  and processed-post/channel-ban state, then restarts only accounts that were
  running and are still eligible.
- A worker must not overwrite a newer DB rate-limit or ban state from a stale
  in-memory cache.

## Changes

1. Use fresh DB health state for each HealthMonitor mutation and route every
   FloodWait through the shared database recorder/history.
2. Normalize statistics field names between `Database`, FastAPI, and the
   dashboard.
3. Make profile synchronization concurrent and throttled instead of blocking
   the FastAPI event loop every ten seconds.
4. Make rate-limit polling target real account cards.
5. Make the health watcher conservative for account-local entity lookup errors
   and rotate the worker used for checks.
6. Make worker start/stop lifecycle preserve authoritative failure statuses and
   wait for session cleanup.
7. Close per-worker HTTP clients during graceful shutdown.
8. Add `POST /admin/reset-operational-state` and change
   `POST /admin/clear-stats` to the safe statistics-only contract.
9. Refresh visible dashboard/account/statistics views after either reset.

## Reset contents

### Statistics reset

Delete rows from `sent_comments`, `account_stats`, `daily_stats`, `logs`,
`invite_stats`, `mass_send_results`, `mass_send_campaigns`, and
`rate_limit_history`. Preserve operational tables and all account/channel
identity data.

### Operational reset

After stopping workers, clear `channel_bans`, `processed_posts`, and
`channel_locks`; clear account-local health pause/error fields and
`rate_limited_until`. Preserve authoritative account status, SpamBot state,
settings, channels, sessions, global exclusions, joined history, parsed users,
and pending/campaign data. Restart only accounts that were running before the
reset and are not authoritative critical statuses.

## Verification

- Regression tests reproduce stale-health overwrite, incomplete reset,
  statistics field mismatch, profile-sync blocking, watcher over-deletion,
  lifecycle status overwrite, and real rate-limit polling.
- Five-worker SQLite stress test keeps exact per-account totals.
- Full Python compilation, JavaScript syntax check, and complete unittest suite.
