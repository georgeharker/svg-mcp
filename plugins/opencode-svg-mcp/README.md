# @geohar/opencode-svg-mcp

An [OpenCode](https://opencode.ai) plugin that makes the **`svg-mcp`** MCP server
available to OpenCode: it starts svg-mcp (supervised by
[`sharedserver`](https://github.com/georgeharker/sharedserver)) and registers its
HTTP MCP endpoint with OpenCode — and **stands down** when a combiner already
serves svg-mcp.

It is the OpenCode counterpart of svg-mcp's Claude Code plugin (a SessionStart
shell hook), and mirrors its behaviour exactly.

## What it does

1. **Stand-down switch.** If a combiner already serves svg-mcp, the plugin does
   nothing — it neither registers a standalone MCP entry nor launches a backend
   (the combiner owns svg-mcp's lifecycle):
   - `MCP_COMBINER=1` — global switch: a combiner serves my MCPs.
   - `MCP_COMBINER_SERVES_SVG_MCP=0|1` — per-backend override (wins over the
     global switch).
2. **Register.** Otherwise it injects `svg-mcp` into OpenCode's `mcp` config as
   a `type: "remote"` endpoint (default `http://127.0.0.1:7731/mcp`). A
   `svg-mcp` entry you defined yourself in `opencode.json` is left untouched.
3. **Run one warm svg-mcp.** It drives `sharedserver`:
   ```
   sharedserver use svg-mcp --pid <opencode-pid> --grace-period 1h \
       -- <svg-mcp serve argv>
   ```
   `sharedserver` refcounts by PID with a grace period, so one svg-mcp process is
   shared across clients (OpenCode, Claude Code, Neovim) and outlives any single
   one — paying svg-mcp's cold start (numpy + pillow) once. The plugin runs
   `unuse` on OpenCode exit.

### Which svg-mcp runs

Mirrors the Claude Code hook:

| mode | argv |
|---|---|
| default | `uvx svg-mcp@<version> --transport streamable-http --port <port>` (pinned) |
| `SVG_MCP_DEV=<dir>` | `uv run --project <dir> svg-mcp …` (a dev checkout) |
| `SVG_MCP_DEV=1` | `uv run --project <repo> svg-mcp …` (in-repo source, if resolvable) |

The default pin is this package's version, kept in lockstep with the svg-mcp
PyPI release — so a plugin version and the served code match. (Every published
plugin version needs a matching PyPI release, or `uvx` cannot resolve it.)

## Install

Add it to your `opencode.json` `plugin` list:

```json
{
  "plugin": [
    "@geohar/opencode-svg-mcp@latest"
  ]
}
```

With options (all optional — defaults shown):

```json
{
  "plugin": [
    ["@geohar/opencode-svg-mcp@latest", {
      "port": 7731,
      "gracePeriod": "1h",
      "manage": true,
      "register": true,
      "notify": true
    }]
  ]
}
```

## Options

| option | default | meaning |
|---|---|---|
| `mcpName` | `"svg-mcp"` | key under OpenCode's `mcp` config |
| `url` | `http://127.0.0.1:<port>/mcp` | MCP URL to register |
| `register` | `true` | register the endpoint with OpenCode |
| `manage` | `true` | launch/attach svg-mcp via sharedserver (`false` → register only) |
| `binary` | auto | path to the `sharedserver` binary (`$SHAREDSERVER_BIN` also honoured) |
| `lockdir` | — | override `SHAREDSERVER_LOCKDIR` |
| `name` | `"svg-mcp"` | sharedserver instance name |
| `gracePeriod` | `"1h"` | sharedserver grace period |
| `logFile` | — | capture svg-mcp output (`sharedserver --log-file`) |
| `port` | `7731` (`$SVG_MCP_PORT`) | HTTP port svg-mcp serves on |
| `version` | package version (`$SVG_MCP_VERSION`) | PyPI release to pin |
| `dev` | `$SVG_MCP_DEV` | dev checkout path, or `true`/`"1"` for in-repo source |
| `notify` | `true` | show TUI toasts for attach/health outcomes |

## Requirements

- [`sharedserver`](https://github.com/georgeharker/sharedserver) on `PATH`
  (`cargo install sharedserver`) — unless `manage: false`.
- [`uv`](https://docs.astral.sh/uv/) on `PATH` for `uvx` (or `uv run` in dev mode).

## Relationship to the other plugins

svg-mcp behind a **combiner** is served through
[`mcp-combiner`](https://github.com/georgeharker/mcp-companion); set
`MCP_COMBINER=1` (or run `mcp-combiner env-disable`) and this plugin stands down.
The Claude Code counterpart ships in the [svg-mcp](https://github.com/georgeharker/svg-mcp)
repo's marketplace.
