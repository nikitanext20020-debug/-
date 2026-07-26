# Broken Paths Cleanup Design

## Goal

Repair only behavior that was reproduced as broken and remove only legacy
methods that are both unreachable from the current application and broken when
called. Preserve working reserve methods, existing HTTP contracts, Telegram
session handling, and all unrelated uncommitted changes.

## Scope

1. Restore the dashboard's existing `POST /discovery/chats/search` contract by
   running group discovery on an active worker loop and persisting results in
   `found_chats`.
2. Make rate-limit checks report the real restart result, restart through the
   existing worker registry helper, and populate/aggregate rate-limit history.
3. Synchronize the join-private pause with the worker-global pause flag.
4. Register static dashboard routes before the optional direct
   `python backend/main.py` server start.
5. Remove the broken, unreachable methods
   `Database.mark_channel_expired`,
   `ChannelJoiner.join_channel`,
   `ChannelJoiner.join_all_channels`, and
   `ChannelJoiner.get_joined_channels`.

## Safety boundaries

- Do not remove methods merely because the current UI does not call them.
- Do not change existing request or response shapes except to make
  `auto_restarted` truthful and to add the missing route already called by the
  UI.
- Do not access real Telegram accounts in tests.
- Use temporary SQLite databases and fake Telegram-shaped clients.
- Keep every fix protected by a regression test and run the complete suite
  after all changes.

## Data flow

The group-search endpoint validates and deduplicates keywords, selects one
running worker, and schedules `KeywordSearch.search_groups_by_keywords()` on
that worker's event loop. The search stores only groups and megagroups in
`found_chats`; the write path creates the table when search is the first
operation against a fresh database.

`Database.record_flood_wait()` updates the account pause and appends the same
event to `rate_limit_history` within the same database transaction.
`check_rate_limit_status()` uses the database pause calculation; after expiry
it clears stale health state and restarts through `_start_worker_for_account()`.

## Verification

- Focused red-green tests for database history, API restart behavior, group
  search, pause propagation, and direct entrypoint route order.
- Full Python compilation.
- JavaScript syntax check.
- Complete `unittest` suite.
