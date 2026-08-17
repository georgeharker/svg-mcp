# @geohar/pi-svg-mcp

A [Pi](https://pi.dev) extension that makes **svg-mcp** available to Pi: it starts the
`svg-mcp` server (supervised by
[`sharedserver`](https://github.com/georgeharker/sharedserver)) and injects the
diagram-authoring directive into the system prompt so the agent reaches for svg-mcp's
tools instead of hand-writing SVG XML.

It is the Pi counterpart of svg-mcp's
[Claude Code](https://github.com/georgeharker/svg-mcp/tree/main/plugins/claude) and
[OpenCode](https://github.com/georgeharker/svg-mcp/tree/main/plugins/opencode) plugins,
and shares the same server and `sharedserver` instance — so Pi, Claude Code, OpenCode,
and Neovim all talk to one refcounted process.

## How it fits together

Pi has no MCP of its own. Two pieces give it svg-mcp:

1. **[`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter)** — the Pi package that
   speaks MCP; it reads its own `mcp.json`. **Install it too**
   (`pi install npm:pi-mcp-adapter`).
2. **This extension** — the process + directive half:
   - **Run svg-mcp** on `session_start` via `sharedserver use … -- uvx svg-mcp@<v>
     --transport streamable-http --port …`, refcounted and shared across clients (its
     cold start — numpy + pillow — is paid once); released on `session_shutdown` when
     `reason === "quit"`.
   - **Inject the directive** on `before_agent_start` (analogue of CC's
     `additionalContext` and OpenCode's `system.transform`).

### Stand-down when combiner-served

If a **combiner** already serves svg-mcp (the global `MCP_COMBINER` switch, or the
per-backend `MCP_COMBINER_SERVES_SVG_MCP` override, which wins), the extension does
**not** launch a standalone backend — the combiner owns svg-mcp's lifecycle. The
directive still applies, since svg-mcp's tools are present via the combiner too. In that
setup you register the *combiner* with pi-mcp-adapter (see the mcp-companion Pi
extension), not svg-mcp directly.

## Install

```sh
# build
npm --prefix plugins/pi install && npm --prefix plugins/pi run build
# install into Pi (symlink the package dir; uses "main": dist/index.js)
ln -sfn "$PWD/plugins/pi" ~/.pi/agent/extensions/svg-mcp
# MCP transport (skip the mcp.json when svg-mcp is combiner-served)
pi install npm:pi-mcp-adapter
cp plugins/pi/mcp.json.example ~/.config/mcp/mcp.json   # standalone only
```

Build-free live dev: `pi -e ./plugins/pi/src/index.ts`.

## Configuration

svg-mcp's tool knobs use the shared `SVG_MCP_*` namespace (as its OpenCode plugin does),
so they apply across every client. Pi-extension toggles use `PI_SVG_MCP_*`.

| Variable | Default | Effect |
|----------|---------|--------|
| `SVG_MCP_PORT` | `7731` | HTTP port svg-mcp serves on. |
| `SVG_MCP_VERSION` | `0.2.6` | Pin the PyPI release (`uvx svg-mcp@<v>`). |
| `SVG_MCP_DEV` | — | Dev checkout for `uv run --project <dir>` (a path, or `1` for in-repo source). |
| `PI_SVG_MCP_NAME` | `svg-mcp` | `sharedserver` instance name. |
| `PI_SVG_MCP_GRACE` | `1h` | `sharedserver` grace period. |
| `PI_SVG_MCP_LOG` | — | Capture svg-mcp's stdout/stderr (`sharedserver --log-file`); `"none"`/unset disables. |
| `PI_SVG_MCP_MANAGE` | `true` | `false` → don't launch (assume svg-mcp runs elsewhere). |
| `PI_SVG_MCP_INSTRUCTIONS` | `true` | `false` → don't inject the directive. |
| `PI_SVG_MCP_NOTIFY` | `true` | `false` → don't surface messages via the Pi UI. |
| `MCP_COMBINER` / `MCP_COMBINER_SERVES_SVG_MCP` | — | Combiner serves svg-mcp → don't launch a standalone backend. |
| `SHAREDSERVER_BIN` / `SHAREDSERVER_LOCKDIR` | *(auto)* | sharedserver binary / lock dir. |

## Development

```sh
npm install && npm run typecheck && npm run build
```

`src/sharedserver-resolve.ts` is vendored byte-identical from
[`georgeharker/sharedserver`](https://github.com/georgeharker/sharedserver) via
`scripts/sync-vendored.sh` — edit upstream, re-sync here.

## License

MIT © George Harker
