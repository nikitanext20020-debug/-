# Account comment-restriction visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Detect Telegram comment restrictions early, pause only comment work, and show a self-refreshing account state in the panel.

**Architecture:** Extend the existing `SpamBotChecker` with robust RU/EN parsing and keep account-level restriction metadata in SQLite. `BotWorker` performs a cooldown-protected periodic check and a check after comment-send failures; the existing `/accounts` endpoint passes normalized metadata to both dashboard renderers.

**Tech Stack:** Python 3, Telethon, SQLite, FastAPI, vanilla JavaScript, unittest.

## Global Constraints

- Never press SpamBot appeal/unblock buttons automatically.
- Preserve channel-local ban records and no-linked-chat structural exclusions.
- Treat unknown SpamBot responses as non-blocking diagnostics.
- Keep the existing uncommitted `spambot-manual-buttons.patch` untouched.

### Task 1: Parser and result application tests

**Files:**
- Modify: `tests/test_spambot_checker.py`
- Modify: `tests/test_worker_comment_recovery.py`

**Interfaces:**
- `SpamBotChecker._parse_status(text) -> str`
- `BotWorker._apply_spambot_result(result) -> None`

- [ ] Write failing tests for the screenshot's Russian limited response, an English limited response, and an OK response.
- [ ] Run only these tests and confirm they fail because the current parser returns `unknown` and the worker has no result application helper.
- [ ] Add a failing worker test proving `limited` stores `spamblock` metadata and `ok` clears it.
- [ ] Run the focused tests again and record the expected failures.

### Task 2: Persist restriction metadata

**Files:**
- Modify: `utils/database.py`
- Modify: `tests/test_target_and_database_regressions.py`

**Interfaces:**
- `Database.update_spambot_status(account_id, status, message, checked_at=None)`
- `Database.get_spambot_status(account_id) -> dict`

- [ ] Add additive `accounts` columns for normalized SpamBot state, last check timestamp, last message, and comment capability.
- [ ] Write a failing database regression test for round-tripping these fields and clearing them after an unrestricted response.
- [ ] Implement the migration and two database helpers.
- [ ] Run the focused database test and confirm it passes.

### Task 3: Periodic worker checks and comment pause

**Files:**
- Modify: `backend/worker.py`
- Modify: `config.py`
- Modify: `tests/test_worker_comment_recovery.py`

**Interfaces:**
- `BotWorker._maybe_check_spambot(force=False, reason="") -> None`
- `BotWorker._apply_spambot_result(result) -> None`

- [ ] Write failing tests for cooldown behavior and for skipping comment sends while `spamblock` is active.
- [ ] Implement a 15-minute default interval, immediate checks after relevant send failures, and a per-account cooldown.
- [ ] Call the helper from the worker loop without blocking unrelated safe tasks.
- [ ] Make `_consult_spambot` call `check(press_buttons=False, ...)`.
- [ ] Run worker-focused tests and confirm all pass.

### Task 4: API and panel state rendering

**Files:**
- Modify: `backend/main.py`
- Modify: `static/app.js`
- Modify: `static/style.css`

**Interfaces:**
- `GET /accounts` adds `spambot_status`, `spambot_checked_at`, `spambot_message`, `comment_status`, and `comment_blocked`.

- [ ] Write a focused API/rendering regression test or static assertion for status precedence.
- [ ] Include database metadata in `/accounts`.
- [ ] Render a red/yellow badge and explanatory text independently from `is_running` on dashboard and account cards.
- [ ] Keep `running` as the process state, not as a replacement for restriction state.
- [ ] Run the focused checks.

### Task 5: Full verification

**Files:**
- No new production files.

- [ ] Run the full Python test suite.
- [ ] Run Python compilation/import checks available in the repository environment.
- [ ] Inspect `git diff` and verify the pre-existing patch is unchanged.
- [ ] Report exact test output and any environment limitations.
