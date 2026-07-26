# Five-Account Safety and Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Harden five-account state synchronization and add safe statistics/operational reset actions without touching Telegram sessions or authoritative account bans.

**Architecture:** SQLite remains the source of truth. BotWorker and HealthMonitor read/merge current DB state before mutations; FastAPI orchestrates worker lifecycle and reset boundaries; the frontend refreshes visible views after state-changing operations.

**Tech Stack:** Python 3.13, FastAPI, Telethon, SQLite, vanilla JavaScript, unittest.

## Global Constraints

- Preserve unrelated uncommitted SpamBot and dashboard changes.
- Do not connect to real Telegram accounts in tests.
- Do not delete sessions, settings, channels, global exclusions, joined history, or parsed users.
- Statistics reset must not delete channel bans or processed posts.
- Operational reset must stop workers before clearing operational state.
- Do not commit or push unless explicitly requested.

---

### Task 1: Add failing regression tests

**Files:**
- Create or modify: `tests/test_five_account_safety.py`
- Modify: `tests/test_backend_api_regressions.py`

- [x] Test stale HealthMonitor state cannot clear a DB FloodWait.
- [x] Test worker FloodWait path records rate-limit history.
- [x] Test statistics API returns the stored comment count and invite count.
- [x] Test statistics reset preserves bans/processed posts and clears reporting tables.
- [x] Test operational reset stops/restarts only eligible previously running workers.
- [x] Test profile synchronization runs five worker profile requests concurrently.
- [x] Test watcher keeps a channel after account-local `ValueError`.
- [x] Test start does not overwrite `banned` before worker startup succeeds.
- [x] Run the focused tests and confirm expected failures.

### Task 2: Make health and FloodWait state authoritative

**Files:**
- Modify: `utils/health_monitor.py`
- Modify: `backend/worker.py`
- Test: `tests/test_five_account_safety.py`

- [x] Reload current DB health before each mutation.
- [x] Route worker FloodWait handling through `Database.record_flood_wait`.
- [x] Preserve an active DB rate-limit during unrelated success/error writes.
- [x] Close comment/autoresponder HTTP clients during worker shutdown.
- [x] Run health-focused tests.

### Task 3: Fix five-account lifecycle and watcher coordination

**Files:**
- Modify: `backend/main.py`
- Modify: `modules/channel_health_watcher.py`
- Modify: `backend/worker.py`
- Test: `tests/test_five_account_safety.py`

- [x] Make profile sync concurrent and bind each worker/account explicitly.
- [x] Throttle dashboard-triggered profile sync to once per minute.
- [x] Preserve critical account status on start/stop and set `active` only after
  successful worker initialization.
- [x] Make watcher rotate workers and treat account-local lookup failure as
  no-change instead of global deletion.
- [x] Add the real account-card attribute for rate-limit polling.
- [x] Run lifecycle and watcher tests.

### Task 4: Implement safe reset boundaries

**Files:**
- Modify: `utils/database.py`
- Modify: `backend/main.py`
- Modify: `static/app.js`
- Modify: `static/index.html`
- Test: `tests/test_five_account_safety.py`

- [x] Add database methods for statistics-only and operational reset.
- [x] Change `/admin/clear-stats` to statistics-only behavior.
- [x] Add `/admin/reset-operational-state` with stop/reset/restart orchestration.
- [x] Add separate dashboard buttons and explicit confirmations.
- [x] Refresh visible accounts, dashboard, statistics, comments, and logs after
  either reset.
- [x] Run reset tests.

### Task 5: Full verification

- [x] Run Python compilation with bytecode outside the repository.
- [x] Run `node --check static/app.js`.
- [x] Run the complete unittest suite.
- [x] Run `git diff --check` and review only targeted changes.
