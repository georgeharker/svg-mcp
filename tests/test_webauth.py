"""web_auth: browser login for the live preview (svg_mcp.webauth, svg_mcp.webgate).

Unit tests for the password, token and "this machine" pieces, then end-to-end tests
against a real preview server (stdlib ``http.server`` on a random loopback port).
Tests run on loopback, so the ones that need the login prompt set ``exempt_local=False``.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastmcp import Client
from pydantic import ValidationError

from svg_mcp import config, preview, webauth, webgate
from svg_mcp.config import Settings, WebAuthConfig

PASSWORD = "correct horse battery staple"
#: ``mcp-combiner hash-password`` output for PASSWORD: the formats are shared.
COMBINER_HASH = (
    "scrypt$16384$8$1$ERIoChZPvrjtmXfIgp_s-A$DPXLZ3_F-9HoLI4g0ToIzSKtgjH21riFt0TU0FXzCX0"
)
TOKEN = "tok-a00000001"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
HTML = {"Accept": "text/html"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient web_auth env, no real interface/tailnet lookups, fresh gate state."""
    for var in ("SVG_MCP_WEB_AUTH", "SVG_MCP_WEB_AUTH__MODE", "SVG_MCP_WEB_PASSWORD_HASH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(webauth, "_interface_addresses", lambda: {"192.168.1.20"})
    monkeypatch.setattr(webauth, "_tailnet_ip", lambda: "100.66.1.2")
    webauth.reset_local_addresses()
    webauth.reset_failures()
    webgate.configure(WebAuthConfig())
    yield
    webgate.reset()
    webauth.reset_local_addresses()
    webauth.reset_failures()


class TestPasswords:
    def test_round_trip_and_rejections(self) -> None:
        stored = webauth.hash_password(PASSWORD)
        assert stored.startswith("scrypt$16384$8$1$")
        assert webauth.verify_password(PASSWORD, stored)
        assert not webauth.verify_password("wrong", stored)
        assert not webauth.verify_password(PASSWORD, "scrypt$broken")
        assert not webauth.verify_password(PASSWORD, "md5$x$y$z$a$b")
        assert webauth.hash_password(PASSWORD) != stored  # salted

    def test_mcp_combiner_hashes_are_accepted(self) -> None:
        assert webauth.verify_password(PASSWORD, COMBINER_HASH)
        assert not webauth.verify_password("wrong", COMBINER_HASH)


class TestSigner:
    def test_sessions_and_tickets(self, tmp_path: Path) -> None:
        signer = webauth.Signer.load_or_create(tmp_path / "web-auth.key")
        assert (tmp_path / "web-auth.key").stat().st_mode & 0o777 == 0o600
        session = signer.session(30)
        assert signer.verify(session, "s") is not None
        assert signer.verify(session, "t") is None  # kinds don't cross
        ticket = signer.ticket("chat-1")
        claims = signer.verify(ticket, "t")
        assert claims is not None and claims["sub"] == "chat-1"
        assert signer.verify(ticket, "s") is None

    def test_tampered_expired_and_foreign_tokens_fail(self, tmp_path: Path) -> None:
        signer = webauth.Signer.load_or_create(tmp_path / "key")
        token = signer.session(30)
        payload, sig = token.split(".")
        assert signer.verify(f"{payload}x.{sig}", "s") is None
        expired = signer.sign({"v": 1, "k": "s", "iat": 0, "exp": int(time.time()) - 1})
        assert signer.verify(expired, "s") is None
        assert signer.verify(signer.sign({"v": 2, "k": "s", "exp": 2**40}), "s") is None
        other = webauth.Signer.load_or_create(tmp_path / "other-key")
        assert other.verify(token, "s") is None
        assert signer.verify("garbage", "s") is None

    def test_the_key_persists(self, tmp_path: Path) -> None:
        token = webauth.Signer.load_or_create(tmp_path / "key").session(1)
        assert webauth.Signer.load_or_create(tmp_path / "key").verify(token, "s") is not None

    def test_the_default_key_lives_in_the_state_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert webauth.default_key_path() == tmp_path / "svg-mcp" / "web-auth.key"


class TestThisMachine:
    def test_loopback_and_own_addresses(self) -> None:
        assert webauth.is_local("127.0.0.1") and webauth.is_local("::1")
        assert webauth.is_local("100.66.1.2") and webauth.is_local("192.168.1.20")
        assert webauth.is_local("::ffff:192.168.1.20")
        assert not webauth.is_local("100.66.9.9") and not webauth.is_local(None)

    def test_forwarded_for_only_from_a_local_proxy(self) -> None:
        assert webauth.client_address("127.0.0.1", ["100.64.9.9"]) == "100.64.9.9"
        assert webauth.client_address("::1", ["1.2.3.4, 100.64.9.9:5"]) == "100.64.9.9"
        assert webauth.client_address("127.0.0.1", ["100.64.9.9", "127.0.0.1"]) == "100.64.9.9"
        # A trusted proxy that sends NO X-Forwarded-For: the real browser is unknown
        # (the ssh -L / socat bypass) — the address does not silently become loopback.
        assert webauth.client_address("127.0.0.1", []) is None
        # Anyone else's header is ignored: they can't claim to be this machine.
        assert webauth.client_address("100.64.9.9", ["127.0.0.1"]) == "100.64.9.9"


class TestSettings:
    @pytest.fixture(autouse=True)
    def _no_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings also read ./.env; run where there is none."""
        monkeypatch.chdir(tmp_path)

    def test_off_by_default(self) -> None:
        assert Settings().web_auth.mode == "off"

    def test_password_mode_needs_a_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SVG_MCP_WEB_AUTH", json.dumps({"mode": "password"}))
        with pytest.raises(ValidationError, match="hash-password"):
            Settings()

    def test_env_shapes_and_the_hash_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SVG_MCP_WEB_AUTH__MODE", "password")
        monkeypatch.setenv("SVG_MCP_WEB_PASSWORD_HASH", COMBINER_HASH)
        web_auth = Settings().web_auth
        assert web_auth.mode == "password" and web_auth.password_hash == COMBINER_HASH
        assert web_auth.session_days == 30 and web_auth.tickets and web_auth.exempt_local

    def test_startup_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from svg_mcp import server

        monkeypatch.setenv("SVG_MCP_WEB_AUTH", json.dumps({"mode": "password"}))
        monkeypatch.setattr(config, "_settings", None)
        monkeypatch.setattr(sys, "argv", ["svg-mcp"])
        with pytest.raises(SystemExit, match="hash-password"):
            server.main()

    def test_hash_password_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import getpass

        from svg_mcp import server

        monkeypatch.setattr(getpass, "getpass", lambda _prompt="": PASSWORD)
        monkeypatch.setattr(sys, "argv", ["svg-mcp", "hash-password"])
        with pytest.raises(SystemExit) as exited:
            server.main()
        assert exited.value.code == 0
        assert webauth.verify_password(PASSWORD, capsys.readouterr().out.strip())


# ── end to end ─────────────────────────────────────────────────────


def _http(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    """One request, redirects not followed: (status, lower-cased headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = urlencode(form) if form is not None else None
    sent = dict(headers or {})
    if body is not None:
        sent["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        return (
            response.status,
            {k.lower(): v for k, v in response.getheaders()},
            response.read().decode("utf-8", "replace"),
        )
    finally:
        conn.close()


class _Preview:
    """A real preview server on a random loopback port, with one document published."""

    def __init__(self) -> None:
        self.server = preview.PreviewServer()
        _, self.port = self.server.ensure_running(port=0)
        self.server.publish(
            TOKEN,
            active_id="doc1",
            sources={"doc1": SVG},
            index_json=json.dumps({"active": "doc1", "documents": [{"id": "doc1"}]}),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], str]:
        return _http(self.port, method, path, headers=headers, form=form)

    def get(self, path: str, **headers: str) -> tuple[int, dict[str, str], str]:
        return self.request("GET", path, headers=headers)

    def login(self, password: str, next_path: str = "/") -> tuple[int, dict[str, str], str]:
        return self.request("POST", "/auth/login", form={"password": password, "next": next_path})


def _cookie(set_cookie: str) -> str:
    """The ``name=value`` part of a Set-Cookie header, to send back."""
    return set_cookie.split(";", 1)[0]


@pytest.fixture()
def signer(tmp_path: Path) -> webauth.Signer:
    return webauth.Signer.load_or_create(tmp_path / "web-auth.key")


def _gate_on(signer: webauth.Signer, *, exempt_local: bool) -> None:
    webgate.configure(
        WebAuthConfig(
            mode="password",
            password_hash=webauth.hash_password(PASSWORD),
            exempt_local=exempt_local,
        ),
        signer,
    )


@pytest.fixture()
def site() -> Iterator[_Preview]:
    running = _Preview()
    yield running
    running.server.shutdown()


class TestLogin:
    def test_off_serves_everything(self, site: _Preview) -> None:
        assert site.get(f"/{TOKEN}/", Accept="text/html")[0] == 200
        assert site.get(f"/{TOKEN}/active/svg")[0] == 200
        assert site.get("/auth/login")[0] == 404  # no login page when off

    def test_pages_redirect_and_everything_else_is_401(
        self, site: _Preview, signer: webauth.Signer
    ) -> None:
        _gate_on(signer, exempt_local=False)
        status, headers, _ = site.get(f"/{TOKEN}/", **HTML)
        assert status == 303
        assert headers["location"] == f"/auth/login?next=%2F{TOKEN}%2F"
        assert site.get(f"/{TOKEN}/")[0] == 401  # not a page request
        for leaf in ("events", "version", "documents", "active/svg", "active/render"):
            assert site.get(f"/{TOKEN}/{leaf}")[0] == 401, leaf
        assert site.get("/", **HTML)[0] == 303

    def test_login_sets_the_cookie_and_the_page_loads(
        self, site: _Preview, signer: webauth.Signer
    ) -> None:
        _gate_on(signer, exempt_local=False)
        status, headers, body = site.get(f"/auth/login?next=%2F{TOKEN}%2F")
        assert status == 200 and 'type="password"' in body and "svg-mcp" in body
        assert "form-action 'self'" in headers["content-security-policy"]
        assert headers["cache-control"] == "no-store"

        status, _, body = site.login("nope", "/x")
        assert status == 401 and "That password isn" in body

        status, headers, _ = site.login(PASSWORD, f"/{TOKEN}/")
        assert status == 303 and headers["location"] == f"/{TOKEN}/"
        set_cookie = headers["set-cookie"]
        assert set_cookie.startswith(f"{webauth.COOKIE}=")
        assert "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie
        assert "Path=/" in set_cookie and f"Max-Age={30 * 86400}" in set_cookie
        assert "Secure" not in set_cookie  # plain http

        cookie = _cookie(set_cookie)
        assert site.get(f"/{TOKEN}/", Accept="text/html", Cookie=cookie)[0] == 200
        status, _, svg = site.get(f"/{TOKEN}/active/svg", Cookie=cookie)
        assert status == 200 and svg == SVG

    def test_the_cookie_is_secure_behind_https(
        self, site: _Preview, signer: webauth.Signer
    ) -> None:
        _gate_on(signer, exempt_local=False)
        status, headers, _ = site.request(
            "POST",
            "/auth/login",
            form={"password": PASSWORD, "next": "/"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert status == 303 and "Secure" in headers["set-cookie"]

    def test_logout(self, site: _Preview, signer: webauth.Signer) -> None:
        _gate_on(signer, exempt_local=False)
        cookie = _cookie(site.login(PASSWORD)[1]["set-cookie"])
        status, headers, _ = site.request(
            "POST", "/auth/logout", headers={"Cookie": cookie}, form={}
        )
        assert status == 303 and headers["location"] == "/auth/login"
        cleared = headers["set-cookie"]
        assert cleared.startswith(f"{webauth.COOKIE}=;") and "Max-Age=0" in cleared
        # The browser drops the cookie; without it the page prompts again.
        assert site.get(f"/{TOKEN}/", **HTML)[0] == 303

    def test_next_is_same_origin_only(self, site: _Preview, signer: webauth.Signer) -> None:
        _gate_on(signer, exempt_local=False)
        for evil in ("https://evil.example/", "//evil.example/", "javascript:x", "/\\evil"):
            status, headers, _ = site.login(PASSWORD, evil)
            assert status == 303 and headers["location"] == "/", evil

    def test_failures_are_rate_limited(self, site: _Preview, signer: webauth.Signer) -> None:
        _gate_on(signer, exempt_local=False)
        codes = [site.login("no")[0] for _ in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        # Even the right password waits out the window.
        assert site.login(PASSWORD)[0] == 429

    def test_a_ticket_for_another_preview_is_refused(
        self, site: _Preview, signer: webauth.Signer
    ) -> None:
        _gate_on(signer, exempt_local=False)
        ticket = signer.ticket("tok-b00000001")
        status, headers, _ = site.get(f"/{TOKEN}/?t={ticket}", **HTML)
        assert status == 303 and headers["location"].startswith("/auth/login")
        assert "set-cookie" not in headers
        # Nor does a session token pass as a ticket.
        status, _, _ = site.get(f"/{TOKEN}/?t={signer.session(1)}", **HTML)
        assert status == 303

    def test_this_machine_is_exempt(self, site: _Preview, signer: webauth.Signer) -> None:
        # Hardened semantics: a loopback peer is also a loopback PROXY peer
        # (127.0.0.1 is in TRUSTED_PROXIES), and a proxy that sends NO
        # X-Forwarded-For leaves the real browser UNKNOWN — so the exemption
        # now requires a direct, non-proxied request (peer not in the trusted
        # set) or a genuine X-Forwarded-For naming a local address. Both cases:
        _gate_on(signer, exempt_local=True)
        # (1) loopback peer with no XFF at all: that is exactly the shape of an
        # ssh -L / socat hop — the real browser's address is unknown, so the
        # login stays on (the old shortcut that made remote browsers exempt).
        assert site.get(f"/{TOKEN}/", **HTML)[0] == 303
        # (2) a request whose peer is NOT one of the trusted proxy addresses
        # (e.g. a browser arriving directly from a non-loopback interface or a
        # remote site) keeps its own address — the proxy rule never fires.
        assert webauth.client_address("8.8.8.8", []) == "8.8.8.8"
        assert webauth.client_address("100.64.9.9", []) == "100.64.9.9"

    def test_a_proxied_browser_is_not_this_machine(
        self, site: _Preview, signer: webauth.Signer
    ) -> None:
        _gate_on(signer, exempt_local=True)
        forwarded = {"X-Forwarded-For": "100.64.9.9", "Accept": "text/html"}
        status, headers, _ = site.request("GET", f"/{TOKEN}/", headers=forwarded)
        assert status == 303 and headers["location"].startswith("/auth/login")
        # The rate limit counts the browser, not the proxy.
        for _ in range(5):
            site.request(
                "POST",
                "/auth/login",
                form={"password": "no"},
                headers={"X-Forwarded-For": "100.64.9.9"},
            )
        assert site.login(PASSWORD)[0] == 303


class TestTickets:
    def test_a_start_preview_url_logs_in_by_ticket(self, signer: webauth.Signer) -> None:
        _gate_on(signer, exempt_local=False)

        async def scenario() -> str:
            from svg_mcp.server import mcp

            async with Client(mcp, mode="legacy") as client:
                await client.call_tool("create_document", {"width": 40, "height": 20})
                result = await client.call_tool("start_preview", {"port": 0})
            url: str = result.data["url"]
            return url

        try:
            url = asyncio.run(scenario())
            parts = urlsplit(url)
            ticket = parse_qs(parts.query)["t"][0]
            assert ticket and parts.port is not None
            token = parts.path.strip("/")
            claims = signer.verify(ticket, "t")
            assert claims is not None and claims["sub"] == token

            port = parts.port
            status, headers, _ = _http(port, "GET", f"{parts.path}?{parts.query}", headers=HTML)
            assert status == 303
            assert headers["location"] == parts.path  # the clean URL
            cookie = _cookie(headers["set-cookie"])
            assert cookie.startswith(f"{webauth.COOKIE}=")
            page = {"Accept": "text/html", "Cookie": cookie}
            assert _http(port, "GET", parts.path, headers=page)[0] == 200
            status, _, svg = _http(
                port, "GET", f"{parts.path}active/svg", headers={"Cookie": cookie}
            )
            assert status == 200 and svg.startswith("<svg")
        finally:
            preview.server.shutdown()

    def test_no_ticket_when_off_or_disabled(self, signer: webauth.Signer) -> None:
        assert webgate.with_ticket("http://h/x/", "x") == "http://h/x/"
        webgate.configure(
            WebAuthConfig(mode="password", password_hash=COMBINER_HASH, tickets=False), signer
        )
        assert webgate.with_ticket("http://h/x/", "x") == "http://h/x/"
        _gate_on(signer, exempt_local=False)
        assert webgate.with_ticket("http://h/x/?a=1", "x").startswith("http://h/x/?a=1&t=")


# ── release-review checklist (combiner 0.20.0.dev1 / 5459fa6) ──────


class TestReservedAdmissions:
    """In-flight + recent failures share the per-address cap, BEFORE the body is
    read or scrypt runs; an overall failure budget crosses all addresses."""

    def test_five_in_flight_block_the_sixth_and_release_reopens(self) -> None:
        webauth.reset_failures()
        try:
            admitted = [webauth.login_attempt_admitted("10.0.0.9") for _ in range(5)]
            assert all(admitted)
            assert webauth.login_attempt_admitted("10.0.0.9") is False
            for _ in range(5):
                webauth.login_attempt_done("10.0.0.9")
            assert webauth.login_attempt_admitted("10.0.0.9") is True
            webauth.login_attempt_done("10.0.0.9")
        finally:
            webauth.reset_failures()

    def test_recorded_failures_count_toward_the_in_flight_gate(self) -> None:
        webauth.reset_failures()
        try:
            for _ in range(5):
                webauth.record_failure("10.0.0.10")
            assert webauth.login_attempt_admitted("10.0.0.10") is False
        finally:
            webauth.reset_failures()

    def test_the_overall_budget_crosses_all_addresses(self) -> None:
        webauth.reset_failures()
        try:
            for i in range(30):  # 30 failures spread over 30 addresses
                webauth.record_failure(f"10.1.{i}.1")
            assert webauth.login_attempt_admitted("10.2.0.1") is False  # fresh address
        finally:
            webauth.reset_failures()

    def test_one_in_flight_plus_four_failures_hits_the_cap(self) -> None:
        webauth.reset_failures()
        try:
            for _ in range(4):
                webauth.record_failure("10.3.0.1")
            assert webauth.login_attempt_admitted("10.3.0.1") is True
            assert webauth.login_attempt_admitted("10.3.0.1") is False  # 4+1 = 5
            webauth.login_attempt_done("10.3.0.1")
        finally:
            webauth.reset_failures()


class TestScryptSlots:
    def test_two_concurrent_scrypt_verifications(self) -> None:
        from svg_mcp.webgate import _LOGIN_SCRYPT_SLOTS

        assert _LOGIN_SCRYPT_SLOTS._value == 2  # bounds a flood of login threads
        assert type(_LOGIN_SCRYPT_SLOTS) is threading.BoundedSemaphore


class TestTicketOneShot:
    def test_a_ticket_redeems_once_per_id_then_refuses(self) -> None:
        signer = webauth.Signer(b"\x11" * 32)
        claims = signer.verify(signer.ticket("/preview-x"), "t")
        assert claims is not None and "j" in claims  # random id present, combiner format
        assert webauth.ticket_admit(claims) is True
        assert webauth.ticket_admit(claims) is False  # exhausted; no replay within the TTL
        assert webauth.ticket_admit({"sub": "/preview-x", "exp": claims["exp"]}) is False

    def test_redemptions_expire_with_the_ticket(self) -> None:
        expired = {"v": 1, "k": "t", "sub": "/z", "j": "stale", "exp": int(time.time()) - 5}
        assert webauth.ticket_admit(expired) is True  # seeded, immediately expired
        assert webauth.ticket_admit(expired) is False  # expired entries never admit
        signer = webauth.Signer(b"\x22" * 32)
        fresh_claims = signer.verify(signer.ticket("/preview-y", ttl_s=60), "t")
        assert fresh_claims is not None
        assert webauth.ticket_admit(fresh_claims) is True

    def test_logout_is_post_only(self, site: _Preview) -> None:
        status, _, _ = site.get(webauth.LOGOUT_PATH, Accept="text/html")
        assert status == 405


class TestStrictConfig:
    def test_unknown_web_auth_keys_fail_startup(self) -> None:
        from typing import Any, cast

        from svg_mcp.config import WebAuthConfig as C

        # extra='forbid': typos must fail loudly. The kwargs are built from a str-
        # valued dict and cast — the intentionally-typed params can't be spelled
        # statically next to the intentionally-unknown key.
        kwargs = cast("dict[str, Any]", {"mode": "password", "bogus_key": "x"})
        with pytest.raises(ValidationError, match="bogus_key"):
            C(**kwargs)

    def test_strict_nested_json_fails_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from svg_mcp.config import Settings

        monkeypatch.setenv("SVG_MCP_WEB_AUTH", '{"mode": "password", "bogus": 1}')
        with pytest.raises(Exception, match="bogus"):
            Settings()


class TestEnvPopped:
    def test_raw_password_env_is_popped_after_hashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from svg_mcp.config import Settings

        monkeypatch.setenv("SVG_MCP_WEB_AUTH", '{"mode": "password"}')
        monkeypatch.setenv("SVG_MCP_WEB_PASSWORD", "popped-secret")
        Settings()
        import os

        assert "SVG_MCP_WEB_PASSWORD" not in os.environ
