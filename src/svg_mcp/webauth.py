"""Browser login for the live preview (passwords, signed tokens, "this machine").

Browsers can't send the MCP bearer, so with ``web_auth.mode = "password"`` the
live-preview pages (``/<token>/...``, see svg_mcp.preview) ask for a password
once per ``session_days`` per browser, then ride a session cookie. Three ways
through, checked in order (svg_mcp.webgate):

- a request from this machine itself (any of its own addresses: loopback, the
  interface addresses, the tailnet address) — exempt;
- a valid session cookie (``svgmcp_session``);
- a valid ticket (``?t=``) on a preview URL svg-mcp handed an MCP client
  (``start_preview``): exchanged for the cookie, then a redirect to the clean
  URL — so links opened from a client never prompt.

Otherwise: a redirect to ``/auth/login`` (pages) or 401 (everything else).

Formats are shared with mcp-combiner's web_auth (``mcp_combiner.webauth``), so
``mcp-combiner hash-password`` output works here as-is. Token:
``<payload_b64url>.<sig_b64url>``, payload compact JSON, sig HMAC-SHA256 over
the payload part. Session: ``{"v":1,"k":"s","iat":..,"exp":..}``; ticket:
``{"v":1,"k":"t","sub":<preview session token>,"exp":..}``. Passwords:
``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (base64url, unpadded).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COOKIE = "svgmcp_session"
TICKET_PARAM = "t"
TICKET_TTL_S = 300
#: A ticket is a one-shot capability by default (the combiner's format note: tickets
#: carry a random id "j"); redemptions are counted in memory until expiry — there is
#: no replay within the 5-minute window for a URL that leaks into a transcript.
TICKET_USES = 1

#: ``(j, subject) -> (exp, remaining_uses)`` — in-memory redemption ledger."""
_ticket_ledger: dict[tuple[str, str], tuple[float, int]] = {}


def ticket_admit(claims: dict[str, Any]) -> bool:
    """Count one redemption of the ``j``-identified ticket in `claims`.

    Ledger entries live until the ticket's own expiry; a j-less legacy ticket (or
    an exhausted/absent id) is refused. An EXPIRED ledger entry keeps its dead
    state for the same id — re-seeding a replayed j must not re-admit."""
    j = claims.get("j")
    subject = claims.get("sub")
    exp = claims.get("exp")
    if not isinstance(j, str) or not isinstance(subject, str) or not isinstance(exp, (int, float)):
        return False
    key = (j, subject)
    now = time.time()
    existing = _ticket_ledger.get(key)
    if existing is not None:
        expires, left = existing
        if expires < now:  # dead: the same j must not re-seed on a replay
            return False
        if left <= 0:
            return False
        _ticket_ledger[key] = (expires, left - 1)
        return True
    for key2 in [k for k, (when, _left) in _ticket_ledger.items() if when < now]:
        del _ticket_ledger[key2]
    _ticket_ledger[key] = (float(exp), TICKET_USES - 1)
    return True


#: A ticket is a one-shot capability by default (the combiner's format note: tickets
#: carry a random id "j"); redemptions are counted in memory until expiry — there is
#: no replay within the 5-minute window for a URL that leaks into a transcript.
TICKET_USES = 1
LOGIN_PATH = "/auth/login"
LOGOUT_PATH = "/auth/logout"

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_FAILURES_PER_MINUTE = 5

#: Peers whose ``X-Forwarded-*`` headers are believed: a local reverse proxy
#: (e.g. ``tailscale serve``). Same set as uvicorn's ``forwarded_allow_ips``
#: in mcp-combiner.
TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1"})


# ── passwords ──────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """The stored form of *password* (``web_auth.password_hash``)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ── hash artifacts ─────────────────────────────────────────────────


def validate_hash_artifact(stored: str) -> None:
    """Reject a malformed ``scrypt$`` artifact at LOAD (not at first login):
    it parses into its 6 fields with numeric round parameters and a salt, and
    carries at least a 32-byte digest — so a truncated paste out of 1Password
    fails the server start with a clear error instead of silently failing
    every login (or weakening the comparison to a few bytes)."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            raise ValueError("not scrypt")
        if not (int(n) >= 2**10 and int(r) >= 1 and int(p) >= 1):
            raise ValueError("implausible round parameters")
        if len(_unb64(salt)) < 8 or len(_unb64(digest)) < 32:
            raise ValueError("salt/digest too short")
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"malformed web_auth password hash artifact ({exc!r}): expected "
            "`svg-mcp hash-password` output 'scrypt$<n>$<r>$<p>$<salt32>$<hash32>'"
        ) from None


# ── signed tokens (session cookie, tickets) ────────────────────────


def default_key_path() -> Path:
    """Where the signing key lives: ``$XDG_STATE_HOME/svg-mcp/web-auth.key``
    (``~/.local/state`` when unset). Delete it to sign every browser out."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "svg-mcp" / "web-auth.key"


class Signer:
    """HMAC-SHA256 tokens under one persisted key (rotate it = log everyone out)."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    @classmethod
    def load_or_create(cls, path: Path) -> Signer:
        """Load the signing key, or atomically CREATE one — never truncate a race.

        Creation: write a temp file 0600 in the same directory, then `os.link` it to
        the final path — the hard link FAILS if the target already came into being
        concurrently, and the existing file is never replaced or truncated (the
        combiner's 0.20 key-file review item; `os.replace` here would clobber a
        concurrently created key and kill its cookies).

        An EXISTING key is opened with `O_NOFOLLOW` (symlinks are refused) and
        checked: owned by this uid (another owner's key is refused, not chmod-ed)
        and mode & 0o077 tightened in place; a malformed/short key refuses startup."""
        if path.is_symlink():
            raise RuntimeError(
                f"{path} is a symlink — refusing (web-auth key must be a regular 0600 "
                "file owned by this user)"
            )
        if path.exists():
            return cls(cls._load_existing(path))
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".web-auth.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        try:
            os.link(tmp, path)  # fails FileExistsError if the target now exists
            created_here = True
        except FileExistsError:
            created_here = False  # someone created it concurrently: load theirs, never clobber
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        if created_here:
            logger.info("web_auth: created a signing key at %s", path)
        return cls(cls._load_existing(path))

    @staticmethod
    def _load_existing(path: Path) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)  # symlink → refused
        stat = os.fstat(fd)  # fstat BEFORE the fd object closes it
        with os.fdopen(fd, "rb") as fh:
            key = fh.read()
        if len(key) < 32:
            raise RuntimeError(
                f"{path} is a malformed web-auth signing key (too short) — delete it "
                "to re-issue (this signs every browser out)"
            )
        if stat.st_uid != os.getuid():
            raise RuntimeError(
                f"{path} is owned by uid {stat.st_uid}, not {os.getuid()} — refusing to "
                "sign cookies with another user's key"
            )
        if stat.st_mode & 0o077:
            os.chmod(path, 0o600)  # tighten in place (content was fine)
        return key

    def sign(self, claims: dict[str, Any]) -> str:
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        sig = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return f"{payload}.{_b64(sig)}"

    def verify(self, token: str, kind: str) -> dict[str, Any] | None:
        """The claims of a valid, unexpired *token* of *kind*, else ``None``."""
        payload, _, sig = token.partition(".")
        if not payload or not sig:
            return None
        expected = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_unb64(sig), expected):
                return None
            claims = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(claims, dict) or claims.get("v") != 1 or claims.get("k") != kind:
            return None
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp < time.time():
            return None
        return claims

    def session(self, days: float, subject: str | None = None) -> str:
        """A login-page cookie (``subject=None``: opens every preview) or a
        TICKET-EXCHANGED cookie (``subject`` set: opens only the preview it named —
        the exchange keeps the ticket's scope, per ``webgate.check``)."""
        now = int(time.time())
        claims: dict[str, Any] = {"v": 1, "k": "s", "iat": now, "exp": now + int(days * 86400)}
        if subject is not None:
            claims["sub"] = subject
        return self.sign(claims)

    def ticket(self, subject: str, ttl_s: int = TICKET_TTL_S) -> str:
        """One-shot ticket with a random id (claim ``j`` — combiner format): the id is
        redeemed `TICKET_USES` times at most, counted in memory until expiry."""
        return self.sign(
            {
                "v": 1,
                "k": "t",
                "sub": subject,
                "j": secrets.token_urlsafe(12),
                "exp": int(time.time()) + ttl_s,
            }
        )


# ── "this machine" ─────────────────────────────────────────────────

#: The machine's OWN NON-LOOPBACK addresses, replaced whole by the refresher and
#: read WITHOUT a lock on the request path (an atomic frozenset swap — the
#: combiner's shape). Refreshed at gate-configuration time and then every 60s by
#: a daemon thread; UNTIL THE FIRST REFRESH COMPLETES it is empty, so only
#: loopback counts as local (fail closed per the spec).
_own_addresses: frozenset[str] = frozenset()
_own_lock = threading.Lock()
_own_refresher: threading.Thread | None = None
_LOCAL_REFRESH_S = 60.0


def refresh_local_addresses() -> None:
    """Re-compute the machine's own non-loopback addresses, REPLACING the set whole.

    One refresh = `ifconfig -a` (falling back to `ip -o addr show` for Linux
    without net-tools), parsing the `inet X` / `inet addr:X` / `inet X/24` forms,
    plus the tailnet IP asked fresh (`tailscale status --json --peers=false` →
    Self.TailscaleIPs, falling back to `tailscale ip -4`) — the tailnet address is
    NOT cached across refreshes (a laptop that changes networks is picked up on
    the next one). Owned addresses that no longer exist drop out the same way. A
    failed refresh keeps the last good set."""
    global _own_addresses
    try:
        found = _interface_addresses() | _tailnet_ips()
        found = {a for a in found if not _is_loopback_str(a)}
        _own_addresses = frozenset(_canonical(a) for a in found)
    except Exception:
        logger.warning("web_auth: local-address refresh failed; keeping the last good set")


def start_local_refresh() -> None:
    """Start the daemon refresher (refresh once, then every 60s) — called by
    `configure` when web_auth is on AND exempt_local is true; idempotent."""
    global _own_refresher
    with _own_lock:
        if _own_refresher is not None:
            return
        _own_refresher = threading.Thread(
            target=_refresh_loop, daemon=True, name="webauth-local-refresh"
        )
        _own_refresher.start()


def _refresh_loop() -> None:
    refresh_local_addresses()  # at startup (of the thread)
    while True:
        time.sleep(_LOCAL_REFRESH_S)
        refresh_local_addresses()


_local_lock = threading.Lock()
_own_lock = threading.Lock()

#: Tailscale's reserved address block (Shared Address Space / CGNAT range).
_TAILSCALE_V4_NET = ipaddress.ip_network("100.64.0.0/10")


def is_local(client_host: str | None) -> bool:
    """Loopback is ALWAYS local; everything else must be in the refreshed set of
    this machine's own non-loopback addresses (empty until the first refresh —
    fail closed per the spec)."""
    if not client_host:
        return False
    canonical = _canonical(client_host)
    try:
        if ipaddress.ip_address(canonical).is_loopback:
            return True
    except ValueError:
        return False
    return canonical in _own_addresses


def reset_local_addresses() -> None:
    """Clear the refreshed set (test isolation)."""
    global _own_addresses
    with _own_lock:
        _own_addresses = frozenset()


def _canonical(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:a.b.c.d is a.b.c.d
    return str(mapped or ip)


def _is_loopback_str(address: str) -> bool:
    try:
        return ipaddress.ip_address(_canonical(address)).is_loopback
    except ValueError:
        return False


def _interface_addresses() -> set[str]:
    """Interface addresses: `ifconfig -a`, falling back to (and ALSO trying in
    parallel-order) `ip -o addr show` (Linux without net-tools). Understands the
    `inet X`, `inet addr:X` and `inet X/24` output forms — both tools' results
    accumulate (one machine can have both tools and multiple interfaces)."""
    found: set[str] = set()
    for argv in (
        (shutil.which("ifconfig") or "", "-a"),
        (shutil.which("ip") or "", "-o", "addr", "show"),
    ):
        if not argv[0]:
            continue
        try:
            out = subprocess.run(
                [argv[0], *argv[1:]],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in (out or "").splitlines():
            parts = line.split()
            for entry_index, part in enumerate(parts):
                if part not in ("inet", "inet6") or entry_index + 1 >= len(parts):
                    continue
                candidate = parts[entry_index + 1]
                if candidate.startswith("addr:"):
                    candidate = candidate.removeprefix("addr:")
                candidate = candidate.partition("/")[0]  # inet X/24 (ip -o form)
                if candidate and not candidate.endswith("%"):
                    found.add(candidate)
    return found


def _tailnet_ips() -> set[str]:
    """This machine's tailnet addresses, asked FRESH on every refresh.

    `tailscale status --json --peers=false` (Self.TailscaleIPs) falling back to
    `tailscale ip -4`; the per-process address cache is not consulted."""
    if shutil.which("tailscale") is None:
        return set()
    ips: set[str] = set()
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        ).stdout
        parsed = json.loads(out or "{}")
        for ip in ((parsed.get("Self") or {}).get("TailscaleIPs")) or []:
            try:
                if ipaddress.ip_address(ip) in _TAILSCALE_V4_NET or ":" in ip:
                    ips.add(str(ip))
            except ValueError:
                continue
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    if ips:
        return ips
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ips
    for line in (out or "").splitlines():
        try:
            if ipaddress.ip_address(line.strip()) in _TAILSCALE_V4_NET:
                ips.add(line.strip())
        except ValueError:
            continue
    return ips


# ── the browser's address behind a local proxy ─────────────────────


def client_address(peer: str | None, forwarded_for: list[str]) -> str | None:
    """The browser's address: *peer* itself, unless *peer* is a trusted local
    proxy sending ``X-Forwarded-For`` — then the rightmost untrusted hop in
    it (uvicorn's ``ProxyHeadersMiddleware`` rule, which mcp-combiner relies
    on). Headers from any other peer are ignored.

    A loopback peer with NO ``X-Forwarded-For`` IS loopback — always local, per
    the combiner's trust model (a direct local browser, or a tunnel hop, both
    count as this machine)."""
    if peer is None or _canonical(peer) not in TRUSTED_PROXIES:
        return peer
    hops = [_host_of(h) for value in forwarded_for for h in value.split(",") if h.strip()]
    if not hops:
        return peer
    for hop in reversed(hops):
        if _canonical(hop) not in TRUSTED_PROXIES:
            return hop
    return hops[0]


def _host_of(hop: str) -> str:
    """A forwarded hop without its port (``1.2.3.4:5``, ``[::1]:5``)."""
    hop = hop.strip()
    if hop.startswith("["):
        return hop[1:].partition("]")[0]
    if hop.count(":") == 1:
        return hop.partition(":")[0]
    return hop


# ── login rate limit ───────────────────────────────────────────────

_failures: defaultdict[str, deque[float]] = defaultdict(deque)
_failures_lock = threading.Lock()

#: Cap on distinct failure-tracked sources: behind a proxy the key can be
#: browser-chosen (rotated X-Forwarded-For), so the table is bounded, not grown
#: without limit. Pruned (not rejected) beyond the cap.
_FAILURE_TABLE_MAX = 4096


def _prune_locked(now: float) -> None:
    """Drop expired windows; if the table is still over the cap, drop the rest
    wholesale (the window rebuilds from fresh failures — losing a partial 60s
    history for some hosts is the cost of bounding attacker-chosen keys)."""
    if len(_failures) <= _FAILURE_TABLE_MAX:
        return
    for key in [k for k, w in _failures.items() if not w or now - w[0] > 60]:
        del _failures[key]
    while len(_failures) > _FAILURE_TABLE_MAX // 2:
        _failures.pop(next(iter(_failures)), None)


def too_many_failures(client_host: str) -> bool:
    with _failures_lock:
        window = _failures[client_host]
        now = time.monotonic()
        while window and now - window[0] > 60:
            window.popleft()
        _prune_locked(now)
        return len(window) >= _FAILURES_PER_MINUTE


# In-flight logins + cross-address budget: a threaded server serves parallel logins,
# which the completed-failures window alone does not count — 40 parallel wrong
# guesses would each see zero failures. Attempts are RESERVED (before the body is
# read or scrypt runs) and released on completion, and an OVERALL failure budget
# across all addresses caps rotated-XFF flooding.
_LOGIN_BUDGET: deque[float] = deque()
_FAILURE_BUDGET_ALL = 30
_LOGIN_INFLIGHT: defaultdict[str, int] = defaultdict(int)
_LOGIN_INFLIGHT_MAX = _FAILURES_PER_MINUTE


def login_attempt_admitted(client_host: str) -> bool:
    """Reserve an in-flight login attempt BEFORE the body is read or scrypt runs.

    Per-address: in-flight + recently failed must stay under the cap. Overall: the
    cross-address failure budget applies. Release with `login_attempt_done`."""
    with _failures_lock:
        now = time.monotonic()
        window = _failures[client_host]
        while window and now - window[0] > 60:
            window.popleft()
        _prune_locked(now)
        while _LOGIN_BUDGET and now - _LOGIN_BUDGET[0] > 60:
            _LOGIN_BUDGET.popleft()
        if len(window) + _LOGIN_INFLIGHT[client_host] >= _LOGIN_INFLIGHT_MAX:
            return False
        if len(_LOGIN_BUDGET) >= _FAILURE_BUDGET_ALL:
            return False
        _LOGIN_INFLIGHT[client_host] += 1
        return True


def login_attempt_done(client_host: str) -> None:
    """Release the in-flight reservation — call unconditionally, from `finally`."""
    with _failures_lock:
        if _LOGIN_INFLIGHT[client_host] > 0:
            _LOGIN_INFLIGHT[client_host] -= 1


def record_failure(client_host: str) -> None:
    with _failures_lock:
        now = time.monotonic()
        _failures[client_host].append(now)
        _LOGIN_BUDGET.append(now)  # the overall budget counts every failed address


def reset_failures() -> None:
    """Clear everything the limiter tracks (per-address windows, the overall
    budget, in-flight reservations, the ticket ledger) — test isolation."""
    with _failures_lock:
        _failures.clear()
        _LOGIN_BUDGET.clear()
        _LOGIN_INFLIGHT.clear()
    _ticket_ledger.clear()
