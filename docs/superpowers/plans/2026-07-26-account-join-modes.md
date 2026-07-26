# Account Join Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить per-account режимы постепенного вступления без блокировки комментариев.

**Architecture:** SQLite хранит `join_mode` и `next_join_at`; worker использует одну фоновую scheduler coroutine и одну операцию вступления. Dashboard и API управляют режимом и показывают таймер.

**Tech Stack:** FastAPI/Pydantic, SQLite, Telethon async worker, vanilla JS, Python unittest.

## Global Constraints

- Новые аккаунты получают `new`; существующие — `normal`.
- Режимы: `off`, `new` 120–180 сек, `careful` 60–120 сек, `normal` 30–60 сек.
- Комментинг не должен ждать join delay.
- FloodWait и критические статусы имеют приоритет.
- Старые вкладки и совместимые API сохраняются.

---

### Task 1: Persist join modes and timers

**Files:**
- Modify: `config.py`
- Modify: `utils/database.py`
- Test: `tests/test_account_join_modes.py`

- [x] Add failing tests for migration defaults, new-account default, mode validation and timer ranges.
- [x] Run focused tests and confirm failure from missing fields/helpers.
- [x] Replace unused delay constants with the used `JOIN_MODE_DELAYS` mapping.
- [x] Add `join_mode` and `next_join_at` migration plus DB read/update/schedule helpers.
- [x] Make `add_account` explicitly assign `new` without changing existing migrated rows.
- [x] Run focused tests and confirm green.

### Task 2: Add API contracts and non-blocking bulk activation

**Files:**
- Modify: `backend/main.py`
- Test: `tests/test_account_join_modes.py`

- [x] Add failing tests for `PATCH /accounts/{id}/join-mode`, invalid values, account payload state and bulk activation without global pause.
- [x] Run focused tests and confirm failure.
- [x] Add the validated join-mode endpoint and enriched `/accounts` response.
- [x] Replace blocking `join-private` processing with a fast activation endpoint that advances eligible account slots.
- [x] Run focused tests and confirm green.

### Task 3: Consolidate worker joining into one scheduler

**Files:**
- Modify: `backend/worker.py`
- Test: `tests/test_account_join_modes.py`

- [x] Add failing tests for scheduler gating, critical/off modes, one-at-a-time joining and next-slot scheduling.
- [x] Run focused tests and confirm failure.
- [x] Implement `_join_next_channel` from the current validated join flow.
- [x] Implement `_join_scheduler_loop` as an independent task and cancel it on shutdown.
- [x] Remove calls and implementation for the duplicated blocking join paths.
- [x] Run focused and worker regression tests.

### Task 4: Dashboard join mode controls

**Files:**
- Modify: `static/app.js`
- Modify: `static/style.css`
- Test: `tests/test_account_join_modes.py`

- [x] Add failing source-contract tests for the join select, countdown and endpoint.
- [x] Run source tests and confirm failure.
- [x] Render the join-mode select and countdown in each dashboard account row.
- [x] Save changes through the new API with rollback on error.
- [x] Refresh countdowns without creating extra account requests.
- [x] Run focused tests and `node --check static/app.js`.

### Task 5: Full verification

**Files:**
- Modify: none beyond fixes found by verification.

- [x] Run compileall.
- [x] Run `node --check static/app.js`.
- [x] Run the full unittest suite.
- [x] Run `git diff --check`.
- [x] Run a local browser smoke test for the dashboard.
