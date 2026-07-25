"""
Shared parser/normalizer for explicit Telegram targets.

Supports:
- numeric IDs (user / chat / channel, including -100… form)
- @usernames and bare usernames
- t.me / telegram.me username links
- t.me/c/<internal_id>/<msg> → -100<internal_id>
- tg://user?id=<id>

Rejects invite-hash / joinchat targets for sending (syntax-level).
Provides ordered de-duplication and a syntax-only preview.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, unquote, urlparse


# Public username: 5–32 chars, letters/digits/underscore, not pure digits
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
# Slightly looser bare token that still looks like a username (Telegram allows 5+)
_LOOSE_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

_INVITE_MARKERS = (
    "joinchat",
    "+",
)

RawTarget = Union[str, int]


@dataclass(frozen=True)
class ParsedTarget:
    """Normalized representation of one explicit Telegram target."""

    original: str
    kind: str  # "user_id" | "chat_id" | "username" | "invalid" | "invite"
    value: Optional[Union[int, str]]
    resolvable: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def send_key(self) -> Optional[str]:
        """Stable key used for ordered de-duplication of sendable targets."""
        if not self.resolvable or self.value is None:
            return None
        if self.kind in ("user_id", "chat_id"):
            return f"id:{int(self.value)}"
        if self.kind == "username":
            return f"user:{str(self.value).lower()}"
        return None


def _strip_wrapping(raw: str) -> str:
    s = raw.strip()
    # common wrappers from CSV / UI paste
    if (s.startswith("<") and s.endswith(">")) or (s.startswith('"') and s.endswith('"')) \
            or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def _looks_like_invite(s: str) -> bool:
    low = s.lower()
    if "joinchat/" in low:
        return True
    # t.me/+HASH or bare +HASH
    if re.search(r"(?:t\.me/|telegram\.me/)\+", low):
        return True
    if s.startswith("+") and len(s) > 1 and not s[1:].isdigit():
        return True
    # bare invite hash path segment after joinchat already handled;
    # t.me/joinchat/XXX already True
    return False


def _parse_numeric(token: str) -> Optional[int]:
    token = token.strip().replace(" ", "")
    if not token:
        return None
    # allow leading + only for pure digits (phone-like) — NOT used as user id
    if token.startswith("+") and token[1:].isdigit():
        return None
    if re.fullmatch(r"-?\d+", token):
        try:
            return int(token)
        except ValueError:
            return None
    return None


def _channel_internal_to_peer_id(internal_id: int) -> int:
    """Convert t.me/c/<internal> id to -100XXXXXXXXXX peer id."""
    if internal_id < 0:
        return internal_id
    s = str(internal_id)
    if s.startswith("100") and len(s) > 3:
        # already includes 100 prefix without minus
        return -int(s) if not s.startswith("-") else int(s)
    return int(f"-100{internal_id}")


def parse_telegram_target(raw: RawTarget) -> ParsedTarget:
    """Parse a single explicit target. Syntax only — no network I/O."""
    if raw is None:
        return ParsedTarget(original="", kind="invalid", value=None, resolvable=False,
                            error="empty_target")

    if isinstance(raw, bool):
        # bool is int subclass — reject explicitly
        return ParsedTarget(original=str(raw), kind="invalid", value=None, resolvable=False,
                            error="invalid_target")

    if isinstance(raw, int):
        original = str(raw)
        if raw == 0:
            return ParsedTarget(original=original, kind="invalid", value=None,
                                resolvable=False, error="invalid_id")
        kind = "chat_id" if raw < 0 else "user_id"
        return ParsedTarget(original=original, kind=kind, value=raw, resolvable=True)

    original = _strip_wrapping(str(raw))
    if not original:
        return ParsedTarget(original="", kind="invalid", value=None, resolvable=False,
                            error="empty_target")

    if _looks_like_invite(original):
        return ParsedTarget(original=original, kind="invite", value=None, resolvable=False,
                            error="invite_hash_not_allowed")

    # tg://user?id=123
    low = original.lower()
    if low.startswith("tg://"):
        try:
            parsed = urlparse(original)
            if parsed.scheme == "tg" and (parsed.netloc == "user" or parsed.path.startswith("user")):
                qs = parse_qs(parsed.query)
                ids = qs.get("id") or []
                if ids and re.fullmatch(r"-?\d+", ids[0].strip()):
                    uid = int(ids[0].strip())
                    if uid == 0:
                        return ParsedTarget(original=original, kind="invalid", value=None,
                                            resolvable=False, error="invalid_id")
                    kind = "chat_id" if uid < 0 else "user_id"
                    return ParsedTarget(original=original, kind=kind, value=uid, resolvable=True)
        except Exception:
            pass
        return ParsedTarget(original=original, kind="invalid", value=None, resolvable=False,
                            error="invalid_tg_link")

    # URL forms: https://t.me/..., http://telegram.me/..., t.me/...
    url_candidate = original
    if "://" not in url_candidate and (
        url_candidate.lower().startswith("t.me/")
        or url_candidate.lower().startswith("telegram.me/")
    ):
        url_candidate = "https://" + url_candidate

    if "://" in url_candidate:
        try:
            parsed = urlparse(url_candidate)
            host = (parsed.netloc or "").lower().split(":")[0]
            if host in ("t.me", "telegram.me", "www.t.me", "www.telegram.me"):
                path = unquote(parsed.path or "").strip("/")
                if not path:
                    return ParsedTarget(original=original, kind="invalid", value=None,
                                        resolvable=False, error="empty_link")
                parts = [p for p in path.split("/") if p]
                if not parts:
                    return ParsedTarget(original=original, kind="invalid", value=None,
                                        resolvable=False, error="empty_link")

                head = parts[0]
                head_low = head.lower()

                if head_low in ("joinchat",):
                    return ParsedTarget(original=original, kind="invite", value=None,
                                        resolvable=False, error="invite_hash_not_allowed")
                if head.startswith("+"):
                    return ParsedTarget(original=original, kind="invite", value=None,
                                        resolvable=False, error="invite_hash_not_allowed")

                # t.me/c/<internal_id>/<msg?>
                if head_low == "c" and len(parts) >= 2 and parts[1].isdigit():
                    internal = int(parts[1])
                    peer_id = _channel_internal_to_peer_id(internal)
                    return ParsedTarget(original=original, kind="chat_id", value=peer_id,
                                        resolvable=True)

                # t.me/username or t.me/username/123
                username = head.lstrip("@")
                if username.isdigit():
                    # unusual but treat as id
                    num = int(username)
                    kind = "chat_id" if num < 0 else "user_id"
                    return ParsedTarget(original=original, kind=kind, value=num, resolvable=True)
                if _LOOSE_USERNAME_RE.match(username) or _USERNAME_RE.match(username):
                    return ParsedTarget(original=original, kind="username",
                                        value=username, resolvable=True)
                return ParsedTarget(original=original, kind="invalid", value=None,
                                    resolvable=False, error="invalid_username")
        except Exception:
            return ParsedTarget(original=original, kind="invalid", value=None,
                                resolvable=False, error="invalid_link")

    # @username
    if original.startswith("@"):
        username = original[1:].strip()
        if _LOOSE_USERNAME_RE.match(username) or _USERNAME_RE.match(username):
            return ParsedTarget(original=original, kind="username", value=username,
                                resolvable=True)
        return ParsedTarget(original=original, kind="invalid", value=None,
                            resolvable=False, error="invalid_username")

    # pure numeric
    num = _parse_numeric(original)
    if num is not None:
        if num == 0:
            return ParsedTarget(original=original, kind="invalid", value=None,
                                resolvable=False, error="invalid_id")
        kind = "chat_id" if num < 0 else "user_id"
        return ParsedTarget(original=original, kind=kind, value=num, resolvable=True)

    # bare username
    bare = original.lstrip("@").strip()
    if _LOOSE_USERNAME_RE.match(bare) or _USERNAME_RE.match(bare):
        return ParsedTarget(original=original, kind="username", value=bare, resolvable=True)

    return ParsedTarget(original=original, kind="invalid", value=None, resolvable=False,
                        error="unrecognized_target")


def normalize_targets(
    targets: Optional[Sequence[RawTarget]],
    *,
    reject_invites: bool = True,
) -> Tuple[List[ParsedTarget], List[ParsedTarget]]:
    """
    Parse a list of targets preserving order, de-duplicating sendable ones.

    Returns:
        (valid_ordered_unique, rejected)
    """
    if not targets:
        return [], []

    valid: List[ParsedTarget] = []
    rejected: List[ParsedTarget] = []
    seen: set = set()

    for raw in targets:
        parsed = parse_telegram_target(raw)
        if not parsed.resolvable:
            if parsed.kind == "invite" and not reject_invites:
                # still not sendable via this path
                rejected.append(parsed)
            else:
                rejected.append(parsed)
            continue
        key = parsed.send_key
        if key is None:
            rejected.append(parsed)
            continue
        if key in seen:
            continue
        seen.add(key)
        valid.append(parsed)

    return valid, rejected


def preview_targets(
    targets: Optional[Sequence[RawTarget]],
    target_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Syntax-only preview for API: normalized list + rejects, no network."""
    raw_targets = list(targets or [])
    valid, rejected = normalize_targets(raw_targets, reject_invites=True)
    wrong_type: List[ParsedTarget] = []
    if target_type == "user":
        wrong_type = [item for item in valid if item.kind == "chat_id"]
    elif target_type == "group":
        wrong_type = [item for item in valid if item.kind == "user_id"]
    if wrong_type:
        wrong_keys = {item.send_key for item in wrong_type}
        valid = [item for item in valid if item.send_key not in wrong_keys]
        rejected.extend(wrong_type)
    valid_items = [
        {
            "raw": t.original,
            "original": t.original,
            "kind": t.kind,
            "value": t.value if t.kind != "username" else f"@{t.value}",
            "normalized": t.value if t.kind != "username" else f"@{t.value}",
        }
        for t in valid
    ]
    invalid_items = [
        {
            "raw": t.original,
            "original": t.original,
            "kind": t.kind,
            "reason": "wrong_target_type" if t in wrong_type else (t.error or "invalid"),
            "error": "wrong_target_type" if t in wrong_type else (t.error or "invalid"),
        }
        for t in rejected
    ]
    duplicate_count = max(0, len(raw_targets) - len(valid) - len(rejected))
    return {
        "total_input": len(raw_targets),
        "valid_count": len(valid),
        "invalid_count": len(rejected),
        "rejected_count": len(rejected),
        "duplicate_count": duplicate_count,
        "valid": valid_items,
        "targets": valid_items,
        "invalid": invalid_items,
        "rejected": invalid_items,
        "duplicates": [],
        "sample": [item["normalized"] for item in valid_items[:8]],
    }


def target_entity_ref(parsed: ParsedTarget) -> Union[int, str]:
    """
    Value suitable for Telethon get_entity / send_message.
    Usernames are returned without @ (Telethon accepts both).
    """
    if not parsed.resolvable or parsed.value is None:
        raise ValueError(parsed.error or "invalid_target")
    return parsed.value


def ensure_sendable_targets(
    targets: Optional[Sequence[RawTarget]],
) -> List[ParsedTarget]:
    """
    Normalize and return only sendable unique targets.
    Raises ValueError if nothing valid remains or input empty.
    """
    if targets is None or (isinstance(targets, (list, tuple)) and len(targets) == 0):
        raise ValueError("empty_targets")
    valid, rejected = normalize_targets(targets, reject_invites=True)
    if not valid:
        if rejected:
            errs = ", ".join(sorted({(r.error or r.kind) for r in rejected}))
            raise ValueError(f"no_valid_targets:{errs}")
        raise ValueError("empty_targets")
    return valid
