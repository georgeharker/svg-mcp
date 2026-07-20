// OpenCode plugin: run the `svg-mcp` MCP server via the `sharedserver` CLI and
// register its HTTP endpoint with OpenCode.
//
// This is the OpenCode counterpart of svg-mcp's Claude Code plugin (a SessionStart
// shell hook + `claude mcp add`). It mirrors that hook's behaviour exactly:
//
//   1. Stand-down switch — if a combiner already serves svg-mcp (the global
//      MCP_COMBINER switch, or the per-backend MCP_COMBINER_SERVES_SVG_MCP
//      override, which wins), do NOTHING: don't register a standalone entry and
//      don't launch a backend. The combiner owns svg-mcp's lifecycle. This is the
//      parity-critical bit the combiner's own plugin doesn't need.
//   2. Registration — inject a `type: "remote"` entry into OpenCode's `mcp`
//      config via the `config` hook (OpenCode has no static .mcp.json). Not adding
//      an entry IS "removing" it — the hook rebuilds config each start — so the
//      combiner-served branch simply returns without adding.
//   3. Process — drive `sharedserver use … -- <svg-mcp serve argv>` so one warm
//      svg-mcp is running and refcounted (shared across clients), paying its cold
//      start (numpy + pillow) once. `unuse` on exit.
//
// WHICH svg-mcp RUNS (mirrors the CC hook's _serve_argv):
//   default            uvx svg-mcp@<version>      (published release, pinned)
//   SVG_MCP_DEV=<dir>  uv run --project <dir>      (a dev checkout)
//   SVG_MCP_DEV=1      uv run --project <repo>     (in-repo source, if resolvable)

import { spawnSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type { Plugin } from "@opencode-ai/plugin"
import { resolveSharedserver } from "./sharedserver-resolve.js"

type Options = {
    // ── Registration ──────────────────────────────────────────────
    /** Key under OpenCode's `mcp` config. Default `"svg-mcp"`. */
    mcpName?: string
    /** Explicit MCP URL to register. Default `http://127.0.0.1:<port>/mcp`. */
    url?: string
    /** Register the MCP endpoint with OpenCode. Default `true`. */
    register?: boolean
    /** Inject the svg-mcp diagram-authoring directive into the system prompt.
     *  Default `true`. */
    instructions?: boolean

    // ── Process management ────────────────────────────────────────
    /** Launch/attach svg-mcp via sharedserver. Default `true`.
     *  `false` → registration only (assume something else runs it). */
    manage?: boolean
    /** Explicit path to the `sharedserver` binary. */
    binary?: string
    /** Override SHAREDSERVER_LOCKDIR for child invocations. */
    lockdir?: string
    /** sharedserver instance name. Default `"svg-mcp"`. */
    name?: string
    /** sharedserver grace period, e.g. "30m", "1h". Default `"1h"`. */
    gracePeriod?: string
    /** Capture svg-mcp's stdout/stderr to this path (sharedserver `--log-file`). */
    logFile?: string

    // ── svg-mcp invocation ────────────────────────────────────────
    /** HTTP port svg-mcp serves on. Default `7731` (or `$SVG_MCP_PORT`). */
    port?: number
    /** Pin the PyPI release to serve. Default this package's version (kept in
     *  lockstep with the svg-mcp release), or `$SVG_MCP_VERSION`. */
    version?: string
    /** Dev checkout to `uv run --project <dir>` instead of `uvx`. A path, or
     *  `true`/`"1"` to use the in-repo source. Else `$SVG_MCP_DEV`. */
    dev?: string | boolean

    /** Show TUI toasts for attach/health outcomes. Default `true`. */
    notify?: boolean
}

type LogFn = (level: "info" | "warn" | "error", message: string) => void
type ToastFn = (variant: "success" | "warning" | "error", message: string) => void
type OcClient = Parameters<Plugin>[0]["client"]

const DEFAULT_PORT = 7731
const DEFAULT_NAME = "svg-mcp"
const DEFAULT_GRACE = "1h"

// ── pinned svg-mcp tool version ────────────────────────────────────

/** The svg-mcp PyPI release this plugin runs by default (`uvx svg-mcp@<v>`).
 *  Decoupled from this package's OWN version so the plugin can version
 *  independently; keep it pointing at a real svg-mcp release. Override
 *  per-launch with the `version` option or `$SVG_MCP_VERSION`. */
const SVG_MCP_TOOL_VERSION = "0.2.6"

// ── the diagram-authoring directive ────────────────────────────────
// Appended to the system prompt so the agent reaches for svg-mcp's tools instead of
// hand-writing SVG XML (the analogue of the Claude Code plugin's SessionStart
// additionalContext). Canonical source: CLAUDE.md.example at the repo root
// (plugins/claude/instructions.txt symlinks it). A release-time `prepack` copies that
// file to this package's root as instructions.txt (see package.json `prepack`/`files`);
// we read the copy ONCE here so the published npm package is self-contained without
// duplicating the text in source. A dev/unbuilt run (no copy present) falls back to an
// empty string and simply injects nothing.
const SVG_DIAGRAM_DIRECTIVE: string = (() => {
    try {
        // dist/index.js lives in dist/; the packed copy ships at the package root.
        const here = dirname(fileURLToPath(import.meta.url))
        return readFileSync(join(here, "..", "instructions.txt"), "utf8")
    } catch {
        return ""
    }
})()

/** Repo root guess for `SVG_MCP_DEV=1` — three levels up from dist/index.js
 *  (plugins/opencode/dist → repo root), only if it holds svg-mcp source. */
function inRepoSource(): string | undefined {
    try {
        const root = fileURLToPath(new URL("../../..", import.meta.url))
        return existsSync(join(root, "pyproject.toml")) ? root : undefined
    } catch {
        return undefined
    }
}

// ── stand-down switch (mirrors the CC hook's combiner_serves) ──────

function truthy(v: string | undefined): boolean {
    if (v == null) return false
    return !["", "0", "false", "no", "off"].includes(v.trim().toLowerCase())
}

/** Does a combiner serve `name`? The per-backend `MCP_COMBINER_SERVES_<NAME>`
 *  override wins over the global `MCP_COMBINER` switch (presence, even empty,
 *  counts as an override — matching the CC hook's `+set` test). */
function combinerServes(name: string, env: NodeJS.ProcessEnv): boolean {
    const key = "MCP_COMBINER_SERVES_" + name.toUpperCase().replace(/[-\s]/g, "_")
    if (key in env) return truthy(env[key])
    return truthy(env.MCP_COMBINER)
}

// ── sharedserver binary resolution (ported from opencode-mcp-combiner) ──

// Resolution lives in a module vendored byte-identical from georgeharker/sharedserver
// (scripts/sync-vendored.sh), so the Claude hook's bin/sharedserver and this plugin
// answer "which sharedserver, and why" identically. Floor-only against the latest
// release: svg-mcp consumes sharedserver rather than shipping it, so version lockstep
// between them would be meaningless.
const SHAREDSERVER_MIN_VERSION = "0.6.7"

function resolveBinary(
    override: string | undefined,
    env: NodeJS.ProcessEnv,
    log?: LogFn,
    toast?: ToastFn,
): string | undefined {
    return resolveSharedserver(
        {
            label: "svg-mcp",
            minVersion: SHAREDSERVER_MIN_VERSION,
            installerUrl:
                "https://github.com/georgeharker/sharedserver/releases/latest/download/sharedserver-installer.sh",
        },
        override,
        env,
        log,
        toast,
    )
}

function onPath(cmd: string, env: NodeJS.ProcessEnv): boolean {
    return spawnSync(cmd, ["--version"], { stdio: "ignore", env }).status === 0
}

// ── svg-mcp serve argv (mirrors the CC hook's _serve_argv) ─────────

/** Resolve the argv that serves svg-mcp over streamable-http, or undefined when
 *  the required runner (uvx / uv) is missing. */
function resolveServeArgv(
    opts: Options,
    env: NodeJS.ProcessEnv,
    port: number,
): { argv: string[]; missing?: string } {
    const dev = opts.dev ?? env.SVG_MCP_DEV
    if (dev) {
        if (!onPath("uv", env)) return { argv: [], missing: "uv" }
        const project =
            typeof dev === "string" && dev !== "1" && existsSync(dev) ? dev : inRepoSource()
        if (!project) {
            // dev requested but no checkout given and no in-repo source (published
            // package has none) — fall through to the published release.
        } else {
            return {
                argv: ["uv", "run", "--project", project, "svg-mcp",
                       "--transport", "streamable-http", "--port", String(port)],
            }
        }
    }
    if (!onPath("uvx", env)) return { argv: [], missing: "uvx" }
    const ver = opts.version ?? env.SVG_MCP_VERSION ?? SVG_MCP_TOOL_VERSION
    const spec = ver ? `svg-mcp@${ver}` : "svg-mcp"
    return { argv: ["uvx", spec, "--transport", "streamable-http", "--port", String(port)] }
}

// ── sharedserver lifecycle (ported) ────────────────────────────────

type PreState = "active" | "grace" | "stopped" | "unknown"

function preCheck(binary: string, name: string, env: NodeJS.ProcessEnv): PreState {
    const result = spawnSync(binary, ["check", name], { stdio: "ignore", env })
    switch (result.status) {
        case 0: return "active"
        case 1: return "grace"
        case 2: return "stopped"
        default: return "unknown"
    }
}

type ServerInfo = { pid?: number; state?: string }

function readServerInfo(binary: string, name: string, env: NodeJS.ProcessEnv): ServerInfo | undefined {
    const result = spawnSync(binary, ["info", name, "--json"], { env })
    if (result.status !== 0) return undefined
    try {
        return JSON.parse(result.stdout.toString()) as ServerInfo
    } catch {
        return undefined
    }
}

function isPidAlive(pid: number): boolean {
    try {
        process.kill(pid, 0)
        return true
    } catch {
        return false
    }
}

type Attached = { binary: string; name: string; env: NodeJS.ProcessEnv }

const attached: Attached[] = []
let cleanupInstalled = false

function installCleanup() {
    if (cleanupInstalled) return
    cleanupInstalled = true

    const drain = () => {
        while (attached.length) {
            const s = attached.pop()!
            spawnSync(s.binary, ["unuse", s.name, "--pid", String(process.pid)], {
                stdio: "ignore",
                env: s.env,
            })
        }
    }

    process.on("exit", drain)
    for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
        process.on(sig, () => {
            drain()
            process.kill(process.pid, sig)
        })
    }
}

// ── health checks (ported) ─────────────────────────────────────────

function scheduleProcessHealthCheck(
    binary: string, name: string, env: NodeJS.ProcessEnv,
    log: LogFn, toast: ToastFn, delayMs: number,
) {
    setTimeout(() => {
        const info = readServerInfo(binary, name, env)
        if (!info) {
            log("warn", `${name}: process health check returned no data`)
            return
        }
        if (info.state && info.state !== "active") {
            const msg = `${name}: not active after start (state: ${info.state})`
            log("error", msg); toast("error", msg); return
        }
        if (info.pid && !isPidAlive(info.pid)) {
            const msg = `${name}: PID ${info.pid} died shortly after start`
            log("error", msg); toast("error", msg); return
        }
        log("info", `${name}: process healthy (pid=${info.pid}, state=${info.state})`)
    }, delayMs).unref()
}

function scheduleMcpHealthCheck(
    client: OcClient, mcpName: string, log: LogFn, toast: ToastFn, delayMs: number,
) {
    setTimeout(() => {
        client.mcp
            .status()
            .then((res) => {
                const st = res.data?.[mcpName]
                if (!st) {
                    log("warn", `${mcpName}: not present in OpenCode mcp status yet`)
                    return
                }
                switch (st.status) {
                    case "connected":
                        log("info", `${mcpName}: connected`)
                        toast("success", `${mcpName}: connected`)
                        break
                    case "failed":
                        toast("error", `${mcpName}: failed — ${st.error ?? "unknown error"}`)
                        break
                    case "needs_auth":
                    case "needs_client_registration":
                        toast("warning", `${mcpName}: ${st.status}`)
                        break
                    default:
                        log("info", `${mcpName}: status ${st.status}`)
                }
            })
            .catch((err: unknown) => {
                log("warn", `${mcpName}: mcp status check failed: ${err instanceof Error ? err.message : String(err)}`)
            })
    }, delayMs).unref()
}

// ── plugin ─────────────────────────────────────────────────────────

const SvgMcpPlugin: Plugin = async ({ client }, options) => {
    const opts = (options ?? {}) as Options
    const notify = opts.notify !== false

    const log: LogFn = (level, message) => {
        client.app.log({ body: { service: "svg-mcp", level, message } }).catch(() => {})
    }
    const toast: ToastFn = (variant, message) => {
        if (!notify) return
        setTimeout(() => {
            client.tui.showToast({ body: { title: "svg-mcp", message, variant } }).catch(() => {})
        }, 1500).unref()
    }

    const env: NodeJS.ProcessEnv = { ...process.env }
    if (opts.lockdir) env.SHAREDSERVER_LOCKDIR = opts.lockdir

    const portEnv = env.SVG_MCP_PORT ? Number(env.SVG_MCP_PORT) : undefined
    const port = opts.port ?? (portEnv && !Number.isNaN(portEnv) ? portEnv : DEFAULT_PORT)
    const mcpName = opts.mcpName ?? DEFAULT_NAME
    const name = opts.name ?? DEFAULT_NAME
    const register = opts.register !== false
    const wantInstructions = opts.instructions !== false
    const url = opts.url ?? `http://127.0.0.1:${port}/mcp`

    const served = combinerServes(mcpName, env)

    // The registration half — contributed each start. When combiner-served we add
    // nothing (the combiner exposes svg-mcp's tools); a user-defined entry is left
    // untouched either way.
    const configHook = async (cfg: { mcp?: Record<string, unknown> }) => {
        if (!register) return
        cfg.mcp ??= {}
        if (cfg.mcp[mcpName]) {
            log("info", `mcp "${mcpName}" already configured by the user; leaving as-is`)
            return
        }
        if (served) {
            log("info", `a combiner serves "${mcpName}"; not registering a standalone entry`)
            return
        }
        cfg.mcp[mcpName] = { type: "remote", url, enabled: true }
        log("info", `registered mcp "${mcpName}" → ${url}`)
    }

    // The directive: appended to the system prompt each session (analogue of the CC
    // plugin's SessionStart additionalContext). Injected regardless of `served` — the
    // tools are present via the combiner too.
    const systemHook = async (_input: unknown, output: { system: string[] }) => {
        if (!wantInstructions || !SVG_DIAGRAM_DIRECTIVE) return
        output.system.push(SVG_DIAGRAM_DIRECTIVE)
    }

    const hooks = {
        config: configHook,
        "experimental.chat.system.transform": systemHook,
    }

    // The process half — skipped when combiner-served or manage=false.
    if (served) {
        log("info", `a combiner serves "${mcpName}"; not launching a standalone backend`)
        return hooks
    }
    const manage = opts.manage !== false
    if (!manage) {
        log("info", `manage=false; registering ${url} only (assuming svg-mcp is started elsewhere)`)
        scheduleMcpHealthCheck(client, mcpName, log, toast, 5000)
        return hooks
    }

    const binary = resolveBinary(opts.binary, env, log, toast)
    if (!binary) {
        const msg = "sharedserver binary not found; set `binary`/`$SHAREDSERVER_BIN`, or use manage:false"
        log("error", msg); toast("error", msg); return hooks
    }
    const { argv, missing } = resolveServeArgv(opts, env, port)
    if (missing) {
        const msg = `\`${missing}\` not on PATH; install uv (https://docs.astral.sh/uv/), or use manage:false`
        log("error", msg); toast("error", msg); return hooks
    }

    const useArgs = [
        "use", name,
        "--pid", String(process.pid),
        "--grace-period", opts.gracePeriod ?? DEFAULT_GRACE,
        "--metadata", `opencode-${process.pid}`,
    ]
    if (opts.logFile) useArgs.push("--log-file", opts.logFile)
    useArgs.push("--", ...argv)

    installCleanup()
    const pre = preCheck(binary, name, env)
    const result = spawnSync(binary, useArgs, { stdio: "pipe", env })

    if (result.error) {
        const msg = `${name}: failed to spawn sharedserver (${result.error.message})`
        log("error", msg); toast("error", msg); return hooks
    }
    if (result.status !== 0) {
        const stderr = result.stderr?.toString().trim()
        const msg = `${name}: sharedserver use exited ${result.status}${stderr ? ` (${stderr})` : ""}`
        log("error", msg); toast("error", msg); return hooks
    }

    attached.push({ binary, name, env })
    if (pre === "stopped" || pre === "unknown") {
        log("info", `started svg-mcp "${name}" (${argv.join(" ")})`)
    } else {
        log("info", `attached to running svg-mcp "${name}" (was ${pre})`)
    }

    scheduleProcessHealthCheck(binary, name, env, log, toast, 2500)
    scheduleMcpHealthCheck(client, mcpName, log, toast, 5000)
    return hooks
}

export default SvgMcpPlugin
