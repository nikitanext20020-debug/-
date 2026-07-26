# Dashboard Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Добавить на главную быстрые действия, per-account режимы, последние комментарии и ошибки, сохранив существующую глобальную логику и вкладки.

**Architecture:** В аккаунтах хранится nullable override `work_mode_override`; worker выбирает его перед глобальным `work_mode`. Dashboard получает уже существующие `/accounts`, `/comments` и `/logs`, а массовые операции используют существующие API и безопасные reset-маршруты.

**Tech Stack:** FastAPI/Pydantic, SQLite через `Database`, vanilla JS, HTML/CSS, Python `unittest`.

## Global Constraints

- Не менять поведение аккаунтов без заданного override: они наследуют глобальный режим.
- Не запускать аккаунты со статусами `banned`, `deactivated`, `frozen`.
- Опасные операции требуют подтверждения и после выполнения обновляют dashboard.
- Существующие вкладки и API сохраняют совместимые контракты.

---

### Task 1: Account mode persistence and worker selection

**Files:**
- Modify: `utils/database.py` (accounts migration and account mode helpers)
- Modify: `backend/main.py` (mode endpoint and account payload)
- Modify: `backend/worker.py` (`_get_current_mode`)
- Test: `tests/test_dashboard_control_center.py`

- [x] Write failing tests for nullable override, API validation, and worker fallback/override.
- [x] Run the focused tests and confirm failure because the migration/helpers/route are absent.
- [x] Add the `work_mode_override` migration, `get_account_mode_override`, and `set_account_mode_override`.
- [x] Add `PATCH /accounts/{account_id}/mode` accepting `{mode: "inherit"|"powerful"|"neutral"|"chill"}` and expose `work_mode_override` from `/accounts`.
- [x] Update `_get_current_mode` to use a valid per-account override before the existing auto-night/global logic.
- [x] Run focused Python tests and confirm they pass.

### Task 2: Dashboard control center markup and account controls

**Files:**
- Modify: `static/index.html` (dashboard quick actions, account mode select, comments/errors blocks)
- Modify: `static/style.css` (responsive dashboard blocks)
- Test: `tests/test_dashboard_control_center.py`

- [x] Add failing source-contract assertions for quick-action IDs, recent comments/errors containers, and per-account mode selector hooks.
- [x] Run the source-contract tests and confirm failure.
- [x] Add the compact dashboard panels and action buttons, keeping existing metrics/account/log panels.
- [x] Add the mode select to dashboard account rows with `data-dash-mode` and `data-id` hooks.
- [x] Add responsive styles that reuse existing panel/button tokens and remain usable on narrow screens.
- [x] Run source-contract tests and confirm they pass.

### Task 3: Dashboard data loading and safe actions

**Files:**
- Modify: `static/app.js` (Dashboard refresh/render/actions)
- Test: `tests/test_dashboard_control_center.py`

- [x] Add failing source-contract assertions for `/comments?limit=10`, `/logs?limit=20&level=error,critical`, mode PATCH, and guarded bulk actions.
- [x] Run the source-contract tests and confirm failure.
- [x] Implement isolated dashboard loaders for comments and errors so one failed request does not hide the other panels.
- [x] Implement per-account mode save with optimistic disable, toast, and refresh; revert the select on error.
- [x] Implement bulk start/stop by calling existing account routes sequentially, skipping critical statuses, then refresh once.
- [x] Wire quick actions to global pause, discovery start, safe stats clear, operational reset, and dashboard refresh with existing confirmation semantics.
- [x] Run source-contract tests and `node --check static/app.js`.

### Task 4: Full verification

**Files:**
- Modify: none beyond fixes discovered by verification.

- [x] Run focused dashboard tests.
- [x] Run `PYTHONPYCACHEPREFIX=<tmp> .venv/bin/python -m compileall -q backend modules utils config.py main.py host.py run_dashboard.py`.
- [x] Run `node --check static/app.js`.
- [x] Run `PYTHONPYCACHEPREFIX=<tmp> .venv/bin/python -m unittest discover -s tests -q`.
- [x] Run `git diff --check` and inspect the final diff for unrelated changes.
