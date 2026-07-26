# Broken Paths Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the reproduced broken paths and remove only four proven broken, unreachable legacy methods without changing working behavior.

**Architecture:** Keep FastAPI as the orchestration layer, run Telegram work on the existing per-account worker loop, and keep SQLite as the source of truth. Each repaired behavior receives an isolated regression test before production code changes.

**Tech Stack:** Python 3.11+, FastAPI, Telethon, SQLite, `unittest`, vanilla JavaScript.

## Global Constraints

- Preserve all unrelated uncommitted changes.
- Do not use real Telegram sessions or the production SQLite database in tests.
- Preserve existing dashboard request paths and payloads.
- Do not commit or push unless the user requests it.

---

### Task 1: Rate-limit persistence and restart

**Files:**
- Modify: `tests/test_target_and_database_regressions.py`
- Create: `tests/test_backend_api_regressions.py`
- Modify: `utils/database.py`
- Modify: `backend/main.py`

**Interfaces:**
- Consumes: `Database.record_flood_wait(account_id, seconds)` and `check_rate_limit_status(account_id)`.
- Produces: populated rate-limit history and truthful `auto_restarted`.

- [x] Add a database test proving a flood wait produces one history row and non-zero aggregate statistics.
- [x] Add API tests proving an expired paused account uses `_start_worker_for_account()` and a healthy account reports `auto_restarted=False`.
- [x] Run the focused tests and confirm the expected failures.
- [x] Insert the history event in the existing flood-wait transaction and fix single-row cursor consumption.
- [x] Replace the undefined frontend `API.post` call with the existing worker-start helper.
- [x] Run the focused tests and confirm they pass.

### Task 2: Group discovery contract

**Files:**
- Create: `tests/test_keyword_group_search.py`
- Modify: `modules/keyword_search.py`
- Modify: `backend/main.py`

**Interfaces:**
- Consumes: `POST /discovery/chats/search` with `{"keywords": ["..."]}`.
- Produces: a scheduled worker task and rows in `found_chats`.

- [x] Add a test proving group search keeps groups, rejects broadcast channels, deduplicates IDs, and persists results.
- [x] Add a direct handler test proving the existing dashboard payload schedules work on a running worker.
- [x] Run both tests and confirm failure because the method/route is missing.
- [x] Implement `KeywordSearch.search_groups_by_keywords()` and the FastAPI request model/handler.
- [x] Run the focused tests and confirm they pass.

### Task 3: Pause propagation and direct entrypoint

**Files:**
- Modify: `tests/test_backend_api_regressions.py`
- Modify: `backend/main.py`

**Interfaces:**
- Consumes: `POST /discovery/channels/join-private` and direct `python backend/main.py`.
- Produces: synchronized API/worker pause flags and an app with dashboard routes registered before Uvicorn starts.

- [x] Add a route-level test that observes the worker pause flag while join-private work is scheduled.
- [x] Add a run-path test that captures registered routes when direct Uvicorn startup is invoked.
- [x] Run the focused tests and confirm both failures.
- [x] Route pause changes through `_set_global_pause()` and move the direct-start block after route registration.
- [x] Run the focused tests and confirm they pass.

### Task 4: Remove proven broken legacy methods

**Files:**
- Modify: `utils/database.py`
- Modify: `modules/channel_joiner.py`

**Interfaces:**
- Consumes: the repository-wide call graph and complete regression suite.
- Produces: removal of exactly four unreachable methods and imports used only by those methods.

- [x] Remove `Database.mark_channel_expired`.
- [x] Remove `ChannelJoiner.join_channel`, `join_all_channels`, and `get_joined_channels`.
- [x] Remove imports that become unused only because of those deletions.
- [x] Run repository reference searches to verify no live references remain.

### Task 5: Full verification

**Files:**
- Verify all modified files.

**Interfaces:**
- Consumes: all preceding changes.
- Produces: evidence that the project still compiles and all tests pass.

- [x] Run Python compilation with bytecode redirected outside the repository.
- [x] Run `node --check static/app.js`.
- [x] Run the complete `unittest` suite.
- [x] Review `git diff --check`, `git diff`, and `git status` for accidental changes.
