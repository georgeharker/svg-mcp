---
description: Install, update, remove or check svg-mcp's svg-authoring skill for Claude Code
argument-hint: "[install|uninstall|status]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/svg-mcp-skills:*)
---

Run this and report its output to the user in one or two sentences:

!`"${CLAUDE_PLUGIN_ROOT}/bin/svg-mcp-skills" ${ARGUMENTS:-install} --target claude`

The skill is the full svg-mcp authoring guide (diagrams, charts, themes, styling, text,
effects, editing, export). It is installed into `$CLAUDE_CONFIG_DIR/skills` (else
`~/.claude/skills`) with a `.installed-by` marker. A copy managed by mcp-combiner, or one
svg-mcp didn't install, is left alone. A new skill is picked up when the next session starts.
