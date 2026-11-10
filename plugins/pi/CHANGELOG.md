# Changelog

All notable changes to svg-mcp and its plugins (Claude Code, OpenCode, Pi), which are
released together under one version. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.10.7] - 2026-11-09

### Added
- `CHANGELOG.md`, shipped in the Pi package so Pi lists what changed after an update.

### Changed
- The README's configuration example keeps settings for one server (`transport`, `port`,
  `[web_auth]`) out of the shared `~/.config/svg-mcp/config.toml`, in a separate file that
  server's launch selects with `$SVG_MCP_CONFIG`. Every launch reads the shared file,
  including the stdio servers MCP clients start themselves.
- When the config file turns the web login on and no password is set, the startup error
  names the file and explains why other launches fail.

## [0.10.6] - 2026-11-08

### Security
- A request counts as "this machine" (and skips the preview login) only when its `Host`
  header also names this machine: `localhost`, one of its addresses, its tailnet name, or
  the preview's bind host. A DNS-rebinding page that points its own domain at `127.0.0.1`
  now gets the login.

### Fixed
- The refresh of this machine's addresses never started, so only loopback counted as
  local and LAN or tailnet browsers were asked to log in even with `exempt_local` on.

## [0.10.5] - 2026-11-08

### Added
- A TOML config file: `~/.config/svg-mcp/config.toml` (or `$SVG_MCP_CONFIG`). Every
  setting can go there; `web_auth` is a `[web_auth]` table. CLI flags and env vars still
  override it. Unknown keys stop startup instead of being ignored.
- Transport, host, port, shutdown grace and the preview's settings are now ordinary
  settings, so the config file can set them.

### Fixed
- A raw `password` inside the `web_auth` block was never hashed, so password mode failed
  to start with it.
- The 0.10.4 package included a stray editor backup of `webauth.py`.

### Changed
- The README documents env vars as `SVG_MCP_` plus the setting name, with JSON for
  `web_auth`; the double-underscore form still works but is no longer documented.
- Documented the tunnel caveat: an `ssh -L` or `socat` tunnel, `tailscale serve --tcp`, or
  any forwarder that drops `X-Forwarded-For` makes every browser look local. Set
  `exempt_local: false` behind one.

## [0.10.4] - 2026-11-08

### Changed
- The web login code now mirrors mcp-combiner's, so a fix in one can be copied to the
  other. Login rate limiting counts per address and overall, and an expired ticket never
  signs a browser in.
- New `web_auth.ticket_uses` setting (default 1).

## [0.10.3] - 2026-11-08

### Changed
- This machine's addresses (interfaces and tailnet) are looked up again every minute, so
  a roaming laptop stays recognised. Until the first lookup, only loopback counts.
- A browser connecting from loopback counts as this machine again, matching mcp-combiner.

## [0.10.2] - 2026-11-08

### Security
- Login hardening: a cap on logins in flight per address and overall, at most two
  password checks at once, one-time tickets, logout by POST only, and stricter checks on
  the signing-key file.

## [0.10.1] - 2026-11-08

### Added
- Optional browser login for the live preview (`web_auth`), compatible with toucan and
  mcp-combiner: one password, session cookies, and sign-in tickets on the URLs
  `start_preview` hands out.
- `SVG_MCP_WEB_PASSWORD`: give the password raw and svg-mcp hashes it at startup.
- `svg-mcp hash-password` prints a hash for `web_auth.password_hash`.

## [0.10.0] - 2026-11-03

### Added
- Support for 2026-era stateless MCP sessions: `create_session` and `end_session`, with
  the session owned by the connection that created it.
- `subscriptions/listen`, so document changes reach clients between tool calls.

### Changed
- Idle HTTP sessions now expire after 24 hours instead of never.

## [0.10.0-dev.2] - 2026-11-03

### Changed
- Session guidance in the server instructions for chats behind mcp-combiner.

## [0.10.0-dev.1] - 2026-11-02

### Added
- `subscriptions/listen` (2026 era).

## [0.10.0-dev.0] - 2026-11-02

### Added
- Minted session ids for 2026-era stateless clients.

### Changed
- Idle HTTP sessions expire after 24 hours.

## [0.9.0] - 2026-11-01

### Changed
- **Breaking:** requires fastmcp 4 and mcp 2 (`fastmcp>=4.1.5,<5`).

### Fixed
- Exports no longer try to fsync a directory on Windows.

[0.10.7]: https://github.com/georgeharker/svg-mcp/compare/v0.10.6...v0.10.7
[0.10.6]: https://github.com/georgeharker/svg-mcp/compare/v0.10.5...v0.10.6
[0.10.5]: https://github.com/georgeharker/svg-mcp/compare/v0.10.4...v0.10.5
[0.10.4]: https://github.com/georgeharker/svg-mcp/compare/v0.10.3...v0.10.4
[0.10.3]: https://github.com/georgeharker/svg-mcp/compare/v0.10.2...v0.10.3
[0.10.2]: https://github.com/georgeharker/svg-mcp/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/georgeharker/svg-mcp/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/georgeharker/svg-mcp/compare/v0.9.0...v0.10.0
[0.10.0-dev.2]: https://github.com/georgeharker/svg-mcp/compare/v0.10.0-dev.1...v0.10.0-dev.2
[0.10.0-dev.1]: https://github.com/georgeharker/svg-mcp/compare/v0.10.0-dev.0...v0.10.0-dev.1
[0.10.0-dev.0]: https://github.com/georgeharker/svg-mcp/compare/v0.9.0...v0.10.0-dev.0
[0.9.0]: https://github.com/georgeharker/svg-mcp/compare/v0.8.2...v0.9.0
