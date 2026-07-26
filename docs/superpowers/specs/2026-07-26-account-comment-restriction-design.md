# Account comment-restriction visibility

## Goal

Make the dashboard tell the operator whether an account is healthy, globally
limited by Telegram anti-spam, unable to comment, frozen, or blocked, and
automatically refresh that state when Telegram removes a restriction.

## Behavior

- The worker checks `@SpamBot` on a bounded periodic interval and immediately
  after a comment-send restriction error.
- The check is read-only from the operator's perspective: it sends `/start`
  and reads the bot response, but never presses appeal/unblock buttons.
- The SpamBot parser recognizes the current Russian and English “limited”
  responses, including the wording “while the limits are active, you cannot
  write to people who have not saved your number”.
- A global SpamBot limit is stored as `spamblock`; a channel-specific
  `UserBannedInChannelError` remains channel-local and is stored as
  `comments_blocked` only when the error indicates that the account cannot
  comment generally.
- While `spamblock` is active, comment attempts are paused. Other safe worker
  features may continue.
- A later SpamBot response of `ok` or `unblocked` clears the temporary
  `spamblock` status and resumes comment attempts.
- The API exposes the last check time, normalized SpamBot state, source message,
  and comment capability separately from the worker's running state.
- Both the dashboard and account cards show the restriction state even when
  the worker process is still running.

## Data flow

`comment send error or periodic timer` → `SpamBotChecker.check()` →
`BotWorker._apply_spambot_result()` → account status/metadata in SQLite →
`GET /accounts` → dashboard/account-card badge.

## Safety and compatibility

- Existing per-channel ban records and fallback behavior remain unchanged.
- Existing `status` values (`active`, `frozen`, `banned`, etc.) remain valid;
  the new metadata is additive.
- SpamBot failures are logged as `unknown` and do not stop an otherwise
  healthy account.
- The checker has a cooldown so an outage or repeated channel errors cannot
  spam `@SpamBot`.
