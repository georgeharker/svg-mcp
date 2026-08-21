// Pi extension: run the `svg-mcp` MCP server via the `sharedserver` CLI and inject the
// diagram-authoring directive into the system prompt.
//
// It is the Pi counterpart of svg-mcp's Claude Code and OpenCode plugins, and mirrors
// their behaviour:
//
//   1. Stand-down switch — if a combiner already serves svg-mcp (global MCP_COMBINER, or
//      the per-backend MCP_COMBINER_SERVES_SVG_MCP override, which wins), do NOT launch
//      a standalone backend. The combiner owns svg-mcp's lifecycle. Only the launch is
//      gated — the directive applies either way, since svg-mcp's tools are present via
//      the combiner too.
//   2. Process — on `session_start`, drive `sharedserver use … -- <svg-mcp serve argv>`
//      so one warm svg-mcp is running and refcounted (shared across clients), paying its
//      cold start (numpy + pillow) once. Released on `session_shutdown` when
//      `reason === "quit"` (reload/resume/fork keep the process and re-attach).
//   3. Directive — append the diagram-authoring text to the system prompt via
//      `before_agent_start` (analogue of CC's additionalContext / OpenCode's
//      system.transform).
//
// WHICH svg-mcp RUNS (mirrors the OpenCode plugin's resolveServeArgv):
//   default            uvx svg-mcp@<version>      (published release, pinned)
//   SVG_MCP_DEV=<dir>  uv run --project <dir>     (a dev checkout)
//   SVG_MCP_DEV=1      uv run --project <repo>    (in-repo source, if resolvable)
//
// MCP registration itself (pointing pi-mcp-adapter at svg-mcp) is a single mcp.json
// entry — see mcp.json.example and the README; static, and unnecessary when
// combiner-served. The sharedserver resolution is ported from plugins/opencode.

import { spawnSync } from "node:child_process"
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"
import type {
    AutocompleteItem,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionContext,
    SessionShutdownEvent,
} from "./pi.js"
import { resolveSharedserver } from "./sharedserver-resolve.js"

const DEFAULT_PORT = 7731
const DEFAULT_NAME = "svg-mcp"
const DEFAULT_GRACE = "1h"
// Floor-only against sharedserver's latest release: svg-mcp consumes sharedserver rather
// than shipping it. Kept equal to the sibling plugins' value.
const SHAREDSERVER_MIN_VERSION = "0.6.7"
// The svg-mcp PyPI release this extension runs by default (`uvx svg-mcp@<v>`). Decoupled
// from this package's OWN version (matching the OpenCode plugin); override per-launch
// with $SVG_MCP_VERSION. Keep it pointing at a real svg-mcp release.
const SVG_MCP_TOOL_VERSION = "0.2.6"

type LogFn = (level: "info" | "warn" | "error", message: string) => void

// ── the diagram-authoring directive ────────────────────────────────
// Appended to the system prompt so the agent reaches for svg-mcp's tools instead of
// hand-writing SVG XML. Canonical source: CLAUDE.md.example at the repo root; a
// release-time `prepack` copies it to this package's root as instructions.txt (see
// package.json). A dev/unbuilt run without the copy falls back to empty and injects
// nothing.
const SVG_DIAGRAM_DIRECTIVE: string = (() => {
    try {
        const here = dirname(fileURLToPath(import.meta.url))
        return readFileSync(join(here, "..", "instructions.txt"), "utf8")
    } catch {
        return ""
    }
})()
const DIRECTIVE_MARKER = SVG_DIAGRAM_DIRECTIVE.split("\n", 1)[0] ?? ""

// ── env configuration ──────────────────────────────────────────────
// svg-mcp's tool knobs use the shared SVG_MCP_* namespace (as its OpenCode plugin does),
// so a user's SVG_MCP_PORT/VERSION/DEV apply across every client. Pi-extension-specific
// toggles use PI_SVG_MCP_*.

function env(name: string): string | undefined {
    const v = process.env[name]
    return v !== undefined && v !== "" ? v : undefined
}

// ── stand-down switch (mirrors the CC hook's combiner_serves) ──────

function truthy(v: string | undefined): boolean {
    if (v == null) return false
    return !["", "0", "false", "no", "off"].includes(v.trim().toLowerCase())
}

/** Does a combiner serve `name`? The per-backend `MCP_COMBINER_SERVES_<NAME>` override
 *  wins over the global `MCP_COMBINER` switch (presence, even empty, counts). Shared
 *  cross-tool switches — NOT PI_-namespaced. */
function combinerServes(name: string): boolean {
    const key = "MCP_COMBINER_SERVES_" + name.toUpperCase().replace(/[-\s]/g, "_")
    if (key in process.env) return truthy(process.env[key])
    return truthy(process.env.MCP_COMBINER)
}

function onPath(cmd: string): boolean {
    return spawnSync(cmd, ["--version"], { stdio: "ignore", env: process.env }).status === 0
}

/** Repo-root guess for `SVG_MCP_DEV=1` — three levels up from dist/index.js
 *  (plugins/pi/dist → repo root), only if it holds svg-mcp source. */
function inRepoSource(): string | undefined {
    try {
        const root = fileURLToPath(new URL("../../..", import.meta.url))
        return existsSync(join(root, "pyproject.toml")) ? root : undefined
    } catch {
        return undefined
    }
}

/** Resolve the argv that serves svg-mcp over streamable-http, or a `missing` runner. */
function resolveServeArgv(port: string): { argv: string[]; missing?: string } {
    const dev = env("SVG_MCP_DEV")
    if (dev) {
        if (!onPath("uv")) return { argv: [], missing: "uv" }
        const project = dev !== "1" && existsSync(dev) ? dev : inRepoSource()
        if (project) {
            return {
                argv: ["uv", "run", "--project", project, "svg-mcp", "--transport", "streamable-http", "--port", port],
            }
        }
        // dev requested but no checkout given and no in-repo source — fall through.
    }
    if (!onPath("uvx")) return { argv: [], missing: "uvx" }
    const ver = env("SVG_MCP_VERSION") ?? SVG_MCP_TOOL_VERSION
    const spec = ver ? `svg-mcp@${ver}` : "svg-mcp"
    return { argv: ["uvx", spec, "--transport", "streamable-http", "--port", port] }
}

// ── sharedserver lifecycle ─────────────────────────────────────────

type Attachment = { binary: string; name: string }
let attachment: Attachment | null = null
let cleanupInstalled = false

function installProcessCleanup() {
    if (cleanupInstalled) return
    cleanupInstalled = true
    process.on("exit", () => detach())
    for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as NodeJS.Signals[]) {
        process.on(sig, () => {
            detach()
            process.kill(process.pid, sig)
        })
    }
}

function detach() {
    if (!attachment) return
    const { binary, name } = attachment
    attachment = null
    spawnSync(binary, ["unuse", name, "--pid", String(process.pid)], { stdio: "ignore", env: process.env })
}

// ── the extension ──────────────────────────────────────────────────

export default function svgMcp(pi: ExtensionAPI): void {
    const notify = env("PI_SVG_MCP_NOTIFY") !== "false"
    const wantInstructions = env("PI_SVG_MCP_INSTRUCTIONS") !== "false"
    const manage = env("PI_SVG_MCP_MANAGE") !== "false"
    const name = env("PI_SVG_MCP_NAME") ?? DEFAULT_NAME
    const served = combinerServes(name)

    // ── directive: appended every turn (dup-guarded across turns) ──
    pi.on("before_agent_start", (event) => {
        if (!wantInstructions || !SVG_DIAGRAM_DIRECTIVE) return
        if (DIRECTIVE_MARKER && event.systemPrompt.includes(DIRECTIVE_MARKER)) return
        return { systemPrompt: `${event.systemPrompt}\n\n${SVG_DIAGRAM_DIRECTIVE}` }
    })

    // ── /svg-mcp command: inspect the extension (verb: system-prompt) ──
    pi.registerCommand("svg-mcp", {
        description:
            "svg-mcp extension — verbs: system-prompt (show the injected directive), install-config [path] (write mcp.json)",
        getArgumentCompletions: (prefix) => completeVerbs(prefix),
        handler: (args, ctx) => {
            const [verb, ...rest] = args.trim().split(/\s+/)
            if (verb === "" || verb === "system-prompt") {
                showDirective(ctx, "svg-mcp", SVG_DIAGRAM_DIRECTIVE, wantInstructions)
                return
            }
            if (verb === "install-config") {
                installConfig(ctx, rest.join(" ") || undefined)
                return
            }
            ctx.ui?.notify?.(`svg-mcp: unknown verb "${verb}". Try: system-prompt, install-config`, "warn")
        },
    })

    // Combiner-served or manage=false: nothing to launch. The directive still applies.
    if (served || !manage) return

    // ── process: launch on session_start, release on session_shutdown("quit") ──
    pi.on("session_start", (_event, ctx) => {
        if (attachment) return

        const log = makeLog(ctx, notify)
        const binary = resolveSharedserver(
            {
                label: "svg-mcp",
                minVersion: SHAREDSERVER_MIN_VERSION,
                installerUrl:
                    "https://github.com/georgeharker/sharedserver/releases/latest/download/sharedserver-installer.sh",
            },
            env("SHAREDSERVER_BIN"),
            process.env,
            log,
        )
        if (!binary) {
            log("error", "sharedserver binary not found; set $SHAREDSERVER_BIN, or PI_SVG_MCP_MANAGE=false")
            return
        }

        const port = env("SVG_MCP_PORT") ?? String(DEFAULT_PORT)
        const { argv, missing } = resolveServeArgv(port)
        if (missing) {
            log("error", `\`${missing}\` not on PATH; install uv (https://docs.astral.sh/uv/), or PI_SVG_MCP_MANAGE=false`)
            return
        }

        const grace = env("PI_SVG_MCP_GRACE") ?? DEFAULT_GRACE
        const useArgs = [
            "use",
            name,
            "--pid",
            String(process.pid),
            "--grace-period",
            grace,
            "--metadata",
            `pi-${process.pid}`,
        ]
        const logFile = env("PI_SVG_MCP_LOG")
        if (logFile && logFile !== "none") useArgs.push("--log-file", logFile)
        useArgs.push("--", ...argv)

        installProcessCleanup()
        const result = spawnSync(binary, useArgs, { stdio: "pipe", env: process.env })
        if (result.error) {
            log("error", `${name}: failed to spawn sharedserver (${result.error.message})`)
            return
        }
        if (result.status !== 0) {
            const stderr = result.stderr?.toString().trim()
            log("error", `${name}: sharedserver use exited ${result.status}${stderr ? ` (${stderr})` : ""}`)
            return
        }

        attachment = { binary, name }
        log("info", `svg-mcp "${name}" attached on port ${port} (${argv.join(" ")})`)
    })

    pi.on("session_shutdown", (event: SessionShutdownEvent) => {
        if (event.reason === "quit") detach()
    })
}

// ── helpers ────────────────────────────────────────────────────────

// The verbs the extension's slash command understands. `system-prompt` shows the
// directive this extension injects — the show-command pattern from pi-custom-system-prompt,
// since `before_agent_start` injections are per-turn and never appear in Pi's own
// `/system-prompt` (which reports the base prompt only).
const COMMAND_VERBS = ["system-prompt", "install-config"]
function completeVerbs(prefix: string): AutocompleteItem[] | null {
    const p = prefix.trim()
    const matches = COMMAND_VERBS.filter((v) => v.startsWith(p))
    return matches.length ? matches.map((v) => ({ value: v, label: v })) : null
}

// ── /svg-mcp install-config: write the pi-mcp-adapter mcp.json entry ──
// A USER-INVOKED write — on request, merge the svg-mcp entry into pi-mcp-adapter's
// mcp.json, wired for svg-mcp's optional inbound bearer auth:
//
//   { "url": "…/mcp", "auth": "bearer", "bearerTokenEnv": "SVG_MCP_AUTH_TOKEN" }
//
// - auth:"bearer" → the adapter attaches `Authorization: Bearer <token>` (it gates the
//   header on `auth === "bearer"`, NOT on bearerTokenEnv alone), AND makes
//   supportsOAuth() false so a wrong/missing token surfaces as an honest 401 rather
//   than a Dynamic-Client-Registration 404.
// - bearerTokenEnv → names the env var the token is read from at connect (nothing
//   written to disk). The backend enforces only when SVG_MCP_AUTH_TOKEN is set on ITS
//   side; unset ⇒ open, the env var is unset here too so no header is sent. The one
//   shape is correct whether or not auth is enabled. Ported from mcp-companion's
//   /mcp-combiner install-config.

function expandHome(p: string): string {
    if (p === "~") return homedir()
    if (p.startsWith("~/")) return join(homedir(), p.slice(2))
    return p
}

/** The URL the adapter should reach svg-mcp at — 127.0.0.1:<port>/mcp with the same
 *  port defaults the backend serves on ($SVG_MCP_PORT, else 7731). */
function defaultSvgUrl(): string {
    let port = DEFAULT_PORT
    const raw = env("SVG_MCP_PORT")
    if (raw !== undefined) {
        const n = Number(raw)
        if (Number.isInteger(n) && n > 0) port = n
    }
    return `http://127.0.0.1:${port}/mcp`
}

function installConfig(ctx: ExtensionCommandContext, pathArg?: string): void {
    const target = pathArg ? expandHome(pathArg) : join(homedir(), ".config", "mcp", "mcp.json")
    const key = DEFAULT_NAME

    // Read + parse existing (tolerate absence; refuse to clobber non-JSON).
    let doc: Record<string, unknown> = {}
    if (existsSync(target)) {
        try {
            const parsed = JSON.parse(readFileSync(target, "utf8"))
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
                ctx.ui?.notify?.(`svg-mcp: ${target} is not a JSON object; not overwriting`, "error")
                return
            }
            doc = parsed as Record<string, unknown>
        } catch (e) {
            ctx.ui?.notify?.(`svg-mcp: ${target} is not valid JSON; not overwriting (${e})`, "error")
            return
        }
    }

    const servers = (doc.mcpServers ??= {}) as Record<string, Record<string, unknown>>
    const prev = (servers[key] ?? {}) as Record<string, unknown>
    const before = JSON.stringify(prev)
    // Preserve any existing url and other fields; only ensure the auth-wiring keys.
    servers[key] = {
        ...prev,
        url: typeof prev.url === "string" && prev.url ? prev.url : defaultSvgUrl(),
        auth: "bearer",
        bearerTokenEnv: "SVG_MCP_AUTH_TOKEN",
    }
    const existed = before !== "{}" && Object.keys(prev).length > 0
    const changed = before !== JSON.stringify(servers[key])

    try {
        mkdirSync(dirname(target), { recursive: true })
        writeFileSync(target, `${JSON.stringify(doc, null, 2)}\n`, "utf8")
    } catch (e) {
        ctx.ui?.notify?.(`svg-mcp: failed to write ${target} (${e})`, "error")
        return
    }

    const what = !existed ? "added" : changed ? "updated" : "already configured"
    ctx.ui?.notify?.(
        `svg-mcp: ${what} "${key}" in ${target}\n` +
            `Sends "Authorization: Bearer $SVG_MCP_AUTH_TOKEN" when that env var is set ` +
            `(auth:"bearer" both sends the token and suppresses OAuth probing). ` +
            `Run /reload so pi-mcp-adapter re-reads mcp.json.`,
        "info",
    )
}

const SHOW_LIMIT = 1600
function showDirective(ctx: ExtensionCommandContext, label: string, directive: string, enabled: boolean): void {
    if (!directive) {
        ctx.ui?.notify?.(`${label}: no directive bundled (instructions.txt missing)`, "warn")
        return
    }
    const head = enabled
        ? `${label} directive — injected into the system prompt on every turn (before_agent_start):`
        : `${label} directive — injection is DISABLED this session; it would be:`
    const body =
        directive.length > SHOW_LIMIT
            ? `${directive.slice(0, SHOW_LIMIT)}\n\n… (${directive.length} chars total)`
            : directive
    ctx.ui?.notify?.(`${head}\n\n${body}`, "info")
}

function makeLog(ctx: ExtensionContext, notify: boolean): LogFn {
    return (level, message) => {
        const line = `svg-mcp: ${message}`
        if (notify && ctx.hasUI && ctx.ui?.notify) {
            ctx.ui.notify(line, level === "error" ? "error" : level === "warn" ? "warn" : "info")
        } else if (level === "error" || level === "warn") {
            process.stderr.write(`${line}\n`)
        }
    }
}
