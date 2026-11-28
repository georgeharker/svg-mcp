# Themes

- A theme is a document-wide design language: named styles, tokens, and house rules, authored
  as CSS in `.svg-mcp/themes/`. `load_theme(name)` makes it resident — new nodes then pick up
  its look automatically, and the response includes the theme's GUIDANCE: **read it and follow
  it; it is the house style speaking.**
- Say what things ARE, not how they look: pass `role=` ("title", "label", "service") and let
  the theme decide paint. `list_styles()` is the menu of everything attachable; attach extras
  via `styles=["@name"]` at creation or `apply_styles` later.
- A node's inline `style` is EXPLICIT and pinned — it beats every theme rule and deliberately
  does NOT follow theme/variant switches. If you typed it, it sticks; if the theme supplied it,
  it stays linked. Prefer roles/classes; reach for inline `style` only for one-off intent.
- `set_theme_variant("dark")` reskins every resident theme in one call (themes without that
  variant keep base). After a variant switch, `reflow()` re-bakes diagram edge-label halos.
- `themed=false` on any constructor opts a node out of automatic hooks. `apply_theme(target,
  theme)` restyles ONE subtree without changing residency; `clear_theme` undoes it.
