"""Regressions for the ten execution-confirmed review findings (F1-F10).

Each test reproduces the exact failure the review recorded, so it fails on the code as it was
and passes on the code as it is. They are grouped by finding, in finding order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from svg_mcp import ops
from svg_mcp.model import Document
from svg_mcp.model.errors import InvalidArgument, SvgMcpError, ThemeError
from svg_mcp.ops.annotate import LegendItem
from svg_mcp.ops.chart import BarData, Series, SparklineData
from svg_mcp.query import get_bbox
from svg_mcp.serialize import export_svg
from svg_mcp.session import DocumentStore
from svg_mcp.theme import load_theme as read_theme
from svg_mcp.theme import materialize

FIXTURES = Path(__file__).parent / "fixtures" / "themes"


def _doc(width: float = 400, height: float = 300) -> Document:
    return DocumentStore().create(width, height)[1]


def _classes(doc: Document, target: str) -> list[str]:
    return str(doc.resolve(target).get("class") or "").split()


def _write_theme(root: Path, name: str, css: str, manifest: str = "") -> Path:
    """Write a throwaway theme directory under ``root`` and return the search path to look in."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "styles.css").write_text(css, encoding="utf-8")
    if manifest:
        (directory / "theme.toml").write_text(manifest, encoding="utf-8")
    return root


def _box(doc: Document, target: str) -> tuple[float, float, float, float]:
    box = get_bbox(doc, target)
    assert box is not None
    return (box["x"], box["y"], box["width"], box["height"])


def _close(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    tolerance: float,
) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right, strict=True))


# --- F1: import must not wipe the stylesheet ---------------------------------


def test_a_reimported_document_keeps_its_css_when_the_sheet_is_next_synced() -> None:
    doc = _doc()
    ops.load_theme(doc, "house", search_paths=[FIXTURES])
    ops.define_style(doc, "accent", {"fill": "#ff0000"})
    ops.add_rect(doc, x=10, y=10, width=40, height=20, styles=["accent"])
    exported = export_svg(doc)
    assert ".house-shape" in exported and ".accent" in exported

    reopened = ops.load_svg_document(svg=exported)
    # The registries genuinely start empty — the CSS is only in the imported <style>.
    assert reopened.theme_css == {} and reopened.styles == {}
    assert reopened.imported_css != ""

    # Anything that re-syncs the sheet used to rewrite it from those empty registries.
    ops.define_style(reopened, "later", {"stroke": "#00ff00"})
    resynced = export_svg(reopened)
    assert ".house-shape" in resynced
    assert ".accent" in resynced
    assert ".later" in resynced


def test_repeated_syncs_do_not_duplicate_the_imported_css() -> None:
    doc = _doc()
    ops.define_style(doc, "accent", {"fill": "#ff0000"})
    reopened = ops.load_svg_document(svg=export_svg(doc))
    ops.define_style(reopened, "one", {"stroke": "#111111"})
    ops.define_style(reopened, "two", {"stroke": "#222222"})
    ops.define_style(reopened, "three", {"stroke": "#333333"})
    assert export_svg(reopened).count(".accent {") == 1


def test_reloading_the_same_theme_over_imported_css_supersedes_the_imported_copy() -> None:
    # UPDATED for G3 (round 2). The round-1 fix left BOTH copies in the sheet — the imported one
    # first and the reloaded one after it, winning on order. That was the bug G3 records: the
    # imported copy outlived the registry that now speaks for it, so unloading the theme could
    # not undress the document and every round trip stacked another copy. The tie the old
    # assertion protected is now moot, because there is only one copy to win it.
    doc = _doc()
    ops.load_theme(doc, "house", search_paths=[FIXTURES])
    reopened = ops.load_svg_document(svg=export_svg(doc))
    assert reopened.imported_css != ""
    ops.load_theme(reopened, "house", search_paths=[FIXTURES])
    sheet = str(reopened.stylesheet().text or "")
    block = reopened.theme_css["house"]
    assert sheet.count(block) == 1
    assert "house-shape" not in reopened.imported_css


# --- F2: a rejected boolean must not have consumed its inputs ----------------


@pytest.mark.parametrize("op", ["union", "intersection", "difference", "exclusion"])
def test_a_boolean_with_an_unknown_style_leaves_both_inputs_intact(op: str) -> None:
    doc = _doc()
    a = ops.add_rect(doc, x=10, y=10, width=40, height=40, name="left").id
    b = ops.add_rect(doc, x=30, y=20, width=40, height=40, name="right").id
    before = export_svg(doc)

    with pytest.raises(InvalidArgument, match="no style named"):
        ops.boolean(doc, op=op, targets=[a, b], styles=["typo"])

    assert doc.resolve(a) is not None
    assert doc.resolve(b) is not None
    assert export_svg(doc) == before  # no orphan group, clipPath or mask left behind


def test_a_boolean_with_an_unservable_role_leaves_both_inputs_intact() -> None:
    doc = _doc()
    a = ops.add_rect(doc, x=10, y=10, width=40, height=40).id
    b = ops.add_rect(doc, x=30, y=20, width=40, height=40).id
    before = export_svg(doc)
    with pytest.raises(InvalidArgument):
        ops.boolean(doc, op="intersection", targets=[a, b], role="no-such-role")
    assert export_svg(doc) == before


# --- F3: token tables resolve to a fixpoint ----------------------------------

_CHAINED = """
:root {
  --gray-900: #101820;
  --ink: var(--gray-900);
  --line: var(--ink);
  --pad: var(--missing-pad, 7);
}

/** A chart. */
.chart { fill: none }

/** Text. */
.title { fill: var(--line) }
"""


def test_a_chained_token_is_a_literal_by_the_time_a_theme_is_materialized(
    tmp_path: Path,
) -> None:
    paths = _write_theme(tmp_path, "chained", _CHAINED, 'name = "chained"\n')
    result = materialize(read_theme("chained", [paths]))
    assert result.tokens["--ink"] == "#101820"
    assert result.tokens["--line"] == "#101820"
    assert result.tokens["--pad"] == "7"
    assert "var(" not in "".join(result.tokens.values())


def test_a_chained_token_reaches_a_sparkline_stroke_as_a_colour(tmp_path: Path) -> None:
    paths = _write_theme(
        tmp_path,
        "chained",
        _CHAINED,
        'name = "chained"\n\n[serves]\ncategories = ["chart"]\n',
    )
    doc = _doc()
    ops.load_theme(doc, "chained", search_paths=[paths])
    placed = ops.add_chart(
        doc, kind="sparkline", data=SparklineData(values=[1, 4, 2, 8]), x=10, y=10
    )
    line = next(child for child in doc.resolve(placed.ref.id) if str(child.TAG) == "polyline")
    assert str(line.style["stroke"]) == "#101820"


def test_a_token_cycle_names_the_chain_it_went_round(tmp_path: Path) -> None:
    css = ":root { --a: var(--b); --b: var(--a) }\n.title { fill: #000 }\n"
    paths = _write_theme(tmp_path, "cyclic", css, 'name = "cyclic"\n')
    with pytest.raises(ThemeError, match="cycle") as caught:
        materialize(read_theme("cyclic", [paths]))
    assert "--a" in str(caught.value) and "--b" in str(caught.value)


def test_a_token_naming_a_missing_token_is_a_load_error(tmp_path: Path) -> None:
    css = ":root { --ink: var(--nope) }\n.title { fill: #000 }\n"
    paths = _write_theme(tmp_path, "broken", css, 'name = "broken"\n')
    with pytest.raises(ThemeError, match="unknown token"):
        materialize(read_theme("broken", [paths]))


# --- F4: re-kinding a facade ------------------------------------------------

_ACME = """
:root { --acme-ink: #223344 }

/** Every shape acme paints. */
.shape { fill: var(--acme-ink) }

/** A service, acme's way. */
.service { stroke: #ff0000 }

/** A datastore, acme's way. */
.datastore { stroke: #00ff00 }
"""
# Acme takes the `shape` CATEGORY and nothing else — it never claims `datastore` as a route, so
# only the category context can lead a re-kind to acme's own `.datastore` rule.
_ACME_MANIFEST = 'name = "acme"\n\n[serves]\ncategories = ["shape"]\n'


def test_a_bogus_kind_leaves_a_node_dressed_exactly_as_it_was() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    before = _classes(doc, placed.ref.id)
    assert "default-service" in before

    with pytest.raises(InvalidArgument):
        ops.edit_diagram_node(doc, placed.ref.id, kind="no-such-kind")

    assert _classes(doc, placed.ref.id) == before
    # And the facade is still re-kindable afterwards, including back to what it already is.
    ops.edit_diagram_node(doc, placed.ref.id, kind="service")
    assert _classes(doc, placed.ref.id) == before
    ops.edit_diagram_node(doc, placed.ref.id, kind="datastore")
    assert "default-datastore" in _classes(doc, placed.ref.id)


def test_a_re_kinded_node_wears_what_a_fresh_one_of_that_kind_would(tmp_path: Path) -> None:
    paths = _write_theme(tmp_path, "acme", _ACME, _ACME_MANIFEST)
    doc = _doc()
    ops.load_theme(doc, "acme", search_paths=[paths])
    fresh = ops.add_diagram_node(doc, kind="datastore", label="DB", x=20, y=20)
    moved = ops.add_diagram_node(doc, kind="service", label="API", x=200, y=20)
    ops.edit_diagram_node(doc, moved.ref.id, kind="datastore")
    assert set(_classes(doc, moved.ref.id)) == set(_classes(doc, fresh.ref.id))
    assert "acme-shape" in _classes(doc, moved.ref.id)
    # The role comes from the theme serving the CATEGORY, not from the bundled fallback the old
    # category-less swap reached for.
    assert "acme-datastore" in _classes(doc, moved.ref.id)
    assert "default-datastore" not in _classes(doc, moved.ref.id)


def test_naming_the_kind_a_node_already_claims_re_dresses_an_undressed_one() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    # Exactly the state a half-completed edit used to leave behind: the spec says `service`,
    # the classes do not.
    ops.remove_styles(doc, placed.ref.id, ["default-service"])
    assert "default-service" not in _classes(doc, placed.ref.id)

    ops.edit_diagram_node(doc, placed.ref.id, kind="service")
    assert "default-service" in _classes(doc, placed.ref.id)


# --- F5: a rejected add leaves no orphan ------------------------------------


def test_a_primitive_with_an_unknown_style_adds_nothing_at_all() -> None:
    doc = _doc()
    ops.add_rect(doc, x=5, y=5, width=10, height=10)
    before = export_svg(doc)
    with pytest.raises(InvalidArgument, match="no style named"):
        ops.add_rect(doc, x=10, y=10, width=40, height=20, styles=["nope"])
    assert export_svg(doc) == before


def test_a_diagram_node_with_an_unservable_role_adds_nothing_at_all() -> None:
    doc = _doc()
    ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    before = export_svg(doc)
    with pytest.raises(InvalidArgument):
        ops.add_diagram_node(doc, kind="no-such-kind", label="Ghost", x=20, y=120)
    assert export_svg(doc) == before


def test_a_text_run_with_an_unknown_style_adds_nothing_at_all() -> None:
    doc = _doc()
    text = ops.add_text(doc, x=10, y=10, content="hello")
    before = export_svg(doc)
    with pytest.raises(InvalidArgument, match="no style named"):
        ops.add_text_run(doc, parent=text.id, text=" world", styles=["nope"])
    assert export_svg(doc) == before


# --- F6: facades under an ancestor transform --------------------------------


def test_a_legend_under_a_translated_parent_does_not_walk_on_every_edit() -> None:
    doc = _doc(600, 400)
    layer = ops.create_group(doc, name="shifted", transform="translate(100,50)")
    ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20, parent=layer.id)
    placed = ops.add_legend(doc, x=200, y=40, parent=layer.id)

    ops.edit_legend(doc, placed.ref.id, title="Key")
    first = _box(doc, placed.ref.id)
    ops.edit_legend(doc, placed.ref.id, title="Key")
    second = _box(doc, placed.ref.id)
    assert _close(first, second, 0.1)


def test_a_callout_leader_lands_on_its_target_under_a_translated_parent() -> None:
    doc = _doc(600, 400)
    layer = ops.create_group(doc, name="shifted", transform="translate(100,50)")
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=40, parent=layer.id)
    placed = ops.add_callout(
        doc, target=node.ref.id, text="the front door", parent=layer.id, x=260, y=40
    )
    group = doc.resolve(placed.ref.id)
    line = next(child for child in group if str(child.TAG) == "line")
    # The leader's numbers are read in the group's frame; composed with the parent translate they
    # have to land on the target's world box.
    end = (float(str(line.get("x2"))) + 100.0, float(str(line.get("y2"))) + 50.0)
    target = _box(doc, node.ref.id)
    assert target[0] - 0.5 <= end[0] <= target[0] + target[2] + 0.5
    assert target[1] - 0.5 <= end[1] <= target[1] + target[3] + 0.5


def test_a_callout_under_a_translated_parent_does_not_walk_on_every_edit() -> None:
    doc = _doc(600, 400)
    layer = ops.create_group(doc, name="shifted", transform="translate(100,50)")
    node = ops.add_diagram_node(doc, kind="service", label="API", x=40, y=40, parent=layer.id)
    # Placed by hand, so the card's corner is a decision the edit must preserve verbatim — the
    # path that used to re-read a WORLD box and write it back as a local one.
    placed = ops.add_callout(doc, target=node.ref.id, text="a note", parent=layer.id, x=260, y=200)

    ops.edit_callout(doc, placed.ref.id, text="a longer note about it")
    first = _box(doc, placed.ref.id)
    ops.edit_callout(doc, placed.ref.id, text="a longer note about it")
    assert _close(first, _box(doc, placed.ref.id), 0.1)


# --- F7: an edit rebuilds in the theme the group WEARS -----------------------

_STUDIO = """
:root { --studio-ink: #402020 }

/** A legend panel. */
.legend { fill: #fff0f0 }

/** A table panel. */
.table { fill: #fff0f0 }

/** A table header band. */
.table-header { fill: #ffd0d0 }
"""
_STUDIO_MANIFEST = 'name = "studio"\n\n[serves]\nroles = ["legend", "table"]\n'


def test_a_legend_keeps_the_theme_it_wears_when_a_second_one_takes_the_role(
    tmp_path: Path,
) -> None:
    paths = _write_theme(tmp_path, "studio", _STUDIO, _STUDIO_MANIFEST)
    doc = _doc()
    ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    placed = ops.add_legend(doc, x=200, y=20)
    assert "default-legend" in _classes(doc, placed.ref.id)

    # A second theme takes the `legend` route; the group is still wearing the default's hook.
    ops.load_theme(doc, "studio", search_paths=[paths], roles=["legend"])
    ops.edit_legend(doc, placed.ref.id, title="Key")

    swatches = [
        cls for child in doc.resolve(placed.ref.id) for cls in str(child.get("class") or "").split()
    ]
    assert "default-service" in swatches  # the rebuild did not silently undress the key


def test_a_table_keeps_the_theme_it_wears_when_a_second_one_takes_the_role(
    tmp_path: Path,
) -> None:
    paths = _write_theme(tmp_path, "studio", _STUDIO, _STUDIO_MANIFEST)
    doc = _doc()
    placed = ops.add_table(doc, rows=[["a", "1"], ["b", "2"]], header=["k", "v"], x=10, y=10)
    assert "default-table" in _classes(doc, placed.ref.id)

    ops.load_theme(doc, "studio", search_paths=[paths], roles=["table"])
    ops.edit_table(doc, placed.ref.id, title="Counts")

    worn = [
        cls for child in doc.resolve(placed.ref.id) for cls in str(child.get("class") or "").split()
    ]
    assert any(cls.startswith("default-") for cls in worn)
    assert not any(cls.startswith("studio-") for cls in worn)


def test_a_chart_keeps_the_theme_it_wears_when_a_second_one_takes_the_category() -> None:
    doc = _doc(400, 300)
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="s", values=[1, 2])]),
        x=10,
        y=10,
    )
    assert "default-chart" in _classes(doc, placed.ref.id)

    ops.load_theme(doc, "drafting", search_paths=[FIXTURES])  # takes the `chart` category
    ops.edit_chart(doc, placed.ref.id, title="Counts")

    group = doc.resolve(placed.ref.id)
    worn = [
        cls
        for child in group.iter()
        for cls in str(child.get("class") or "").split()
        if child is not group  # the group's own hook is not what the rebuild decides
    ]
    assert any(cls.startswith("default-") for cls in worn)
    assert not any(cls.startswith("drafting-") for cls in worn)


# --- F8: a container fits its members in the right frame ---------------------


def test_a_container_under_a_translated_parent_is_drawn_around_its_members() -> None:
    doc = _doc(600, 400)
    layer = ops.create_group(doc, name="shifted", transform="translate(100,50)")
    one = ops.add_diagram_node(doc, kind="service", label="A", x=20, y=20, parent=layer.id)
    two = ops.add_diagram_node(doc, kind="service", label="B", x=200, y=20, parent=layer.id)
    placed = ops.add_diagram_container(
        doc, members=[one.ref.id, two.ref.id], label="", parent=layer.id
    )
    members = (
        min(_box(doc, one.ref.id)[0], _box(doc, two.ref.id)[0]),
        min(_box(doc, one.ref.id)[1], _box(doc, two.ref.id)[1]),
    )
    box = _box(doc, placed.ref.id)
    assert box[0] < members[0] and box[1] < members[1]

    before = _box(doc, placed.ref.id)
    ops.reflow(doc)
    assert _close(before, _box(doc, placed.ref.id), 0.5)


# --- F9: reflow scope semantics ---------------------------------------------


def test_reflow_reports_a_deleted_scope_entry_instead_of_raising() -> None:
    doc = _doc(600, 400)
    one = ops.add_diagram_node(doc, kind="service", label="A", x=20, y=20)
    two = ops.add_diagram_node(doc, kind="service", label="B", x=200, y=20)
    ops.add_diagram_edge(doc, source=one.ref.id, target=two.ref.id)
    gone = ops.add_diagram_node(doc, kind="service", label="C", x=20, y=160).ref.id
    ops.delete_node(doc, gone)

    result = ops.reflow(doc, scope=[gone, one.ref.id])
    assert gone in result.skipped
    assert result.edges_rerouted == 1  # the rest of the scope still applied


def test_an_empty_reflow_scope_is_an_explicit_no_op() -> None:
    doc = _doc(600, 400)
    one = ops.add_diagram_node(doc, kind="service", label="A", x=20, y=20)
    two = ops.add_diagram_node(doc, kind="service", label="B", x=200, y=20)
    ops.add_diagram_edge(doc, source=one.ref.id, target=two.ref.id)
    ops.add_diagram_container(doc, members=[one.ref.id, two.ref.id], label="both")
    before = export_svg(doc)

    result = ops.reflow(doc, scope=[])
    assert result.edges_rerouted == 0
    assert result.containers_refit == 0
    assert export_svg(doc) == before


def test_a_reflow_with_no_scope_still_covers_everything() -> None:
    doc = _doc(600, 400)
    one = ops.add_diagram_node(doc, kind="service", label="A", x=20, y=20)
    two = ops.add_diagram_node(doc, kind="service", label="B", x=200, y=20)
    ops.add_diagram_edge(doc, source=one.ref.id, target=two.ref.id)
    assert ops.reflow(doc).edges_rerouted == 1


# --- F10: apply_styles with an empty list ------------------------------------


def test_appending_no_styles_is_rejected_rather_than_silently_doing_nothing() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=10, y=10, width=20, height=20)
    ops.define_style(doc, "accent", {"fill": "#ff0000"})
    ops.apply_styles(doc, rect.id, ["accent"])

    with pytest.raises(InvalidArgument, match="replace=true"):
        ops.apply_styles(doc, rect.id, [])
    assert _classes(doc, rect.id) == ["accent"]


def test_replacing_with_no_styles_is_still_how_a_class_list_is_cleared() -> None:
    doc = _doc()
    rect = ops.add_rect(doc, x=10, y=10, width=20, height=20)
    ops.define_style(doc, "accent", {"fill": "#ff0000"})
    ops.apply_styles(doc, rect.id, ["accent"])
    ops.apply_styles(doc, rect.id, [], replace=True)
    assert _classes(doc, rect.id) == []
    assert doc.resolve(rect.id).get("class") is None


def test_a_node_that_does_not_exist_is_still_the_first_complaint() -> None:
    doc = _doc()
    with pytest.raises(SvgMcpError):
        ops.apply_styles(doc, "nothing-here", ["accent"])


# --- F4 follow-up: a routed category must not block the role fallback ------------------


def test_kind_swap_works_when_a_resident_theme_claims_the_category_but_not_the_kind() -> None:
    # Regression (re-review of the F4 fix): passing the REAL category to _swap_role exposed
    # the strict role error for themes routed for `shape` but not the kind. The role now
    # resolves routed theme -> category theme -> bundled default, so the swap dresses from
    # the default instead of raising.
    doc = DocumentStore().create(200, 120)[1]
    node = ops.add_diagram_node(doc, kind="service", label="A")
    ops.load_theme(doc, "house", search_paths=[Path(__file__).parent / "fixtures" / "themes"])
    ops.edit_diagram_node(doc, node.ref.id, kind="datastore")
    classes = set(doc.resolve(node.ref.id).get("class", "").split())
    assert "default-datastore" in classes
    assert "default-service" not in classes


def test_label_edit_echoing_the_kind_does_not_raise_with_two_themes_resident() -> None:
    doc = DocumentStore().create(200, 120)[1]
    node = ops.add_diagram_node(doc, kind="service", label="B")  # dressed default-service
    ops.load_theme(doc, "house", search_paths=[Path(__file__).parent / "fixtures" / "themes"])
    ops.edit_diagram_node(doc, node.ref.id, label="B2", kind="service")
    classes = set(doc.resolve(node.ref.id).get("class", "").split())
    assert "default-service" in classes


def test_a_genuinely_unknown_role_still_fails_loudly() -> None:
    doc = DocumentStore().create(200, 120)[1]
    with pytest.raises(InvalidArgument, match="role 'nonesuch'"):
        ops.add_rect(doc, x=0, y=0, width=10, height=10, role="nonesuch")


# =============================================================================
# ROUND 2 — the re-review of the fix commit (G1-G9).
#
# The first pass fixed each finding where the reviewer pointed, but the same
# mistake was live in the siblings the finding did not name. Each test below
# reproduces the exact scenario the re-review recorded.
# =============================================================================


_GAP_NODE = 24.0  # the bundled default's --gap-node, which every stack below is measured against


def _world_gap(doc: Document, upper: str, lower: str) -> float:
    """The clear air between two facades' world boxes — what a stack is supposed to hold fixed."""
    above, below = _box(doc, upper), _box(doc, lower)
    return below[1] - (above[1] + above[3])


# --- G1: auto-placement lands in the parent's frame, not the world's ---------


def test_stacked_nodes_under_a_transformed_ancestor_keep_one_constant_gap() -> None:
    # The stack is measured off WORLD boxes and used to be written as a parent-local y, so every
    # node fell a further `translate` past the one above it.
    doc = _doc(600, 900)
    layer = ops.create_group(doc, name="shifted", transform="translate(0,200)")
    first = ops.add_diagram_node(doc, kind="service", label="A", parent=layer.id)
    second = ops.add_diagram_node(doc, kind="service", label="B", parent=layer.id)
    third = ops.add_diagram_node(doc, kind="service", label="C", parent=layer.id)

    assert abs(_world_gap(doc, first.ref.id, second.ref.id) - _GAP_NODE) <= 0.5
    assert abs(_world_gap(doc, second.ref.id, third.ref.id) - _GAP_NODE) <= 0.5
    # And they are inside the layer they were added to, rather than at the world origin: the
    # translate the caller put them under is honoured, not ignored.
    for placed in (first, second, third):
        assert _box(doc, placed.ref.id)[1] >= 200.0


def test_a_table_stacks_under_the_nodes_above_it_in_a_transformed_layer() -> None:
    doc = _doc(600, 900)
    layer = ops.create_group(doc, name="shifted", transform="translate(0,200)")
    ops.add_diagram_node(doc, kind="service", label="A", parent=layer.id)
    last = ops.add_diagram_node(doc, kind="service", label="B", parent=layer.id)
    table = ops.add_table(doc, rows=[["a", "1"], ["b", "2"]], parent=layer.id)
    assert abs(_world_gap(doc, last.ref.id, table.ref.id) - _GAP_NODE) <= 0.5


def test_a_chart_stacks_under_a_node_in_a_transformed_layer() -> None:
    # A chart carries its position in its own translate, so the reported x/y IS what was
    # written there — and, per the frame contract, it is stated in the parent's frame. (Its
    # world box starts a little inside that corner, which is the plot's own margin, not drift.)
    doc = _doc(600, 900)
    layer = ops.create_group(doc, name="shifted", transform="translate(0,200)")
    node = ops.add_diagram_node(doc, kind="service", label="A", parent=layer.id)
    chart = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="s", values=[1, 2])]),
        parent=layer.id,
    )
    assert abs(chart.y - (node.y + node.h + _GAP_NODE)) <= 0.5
    # ... and that local y really is under the node in the world, not a translate past it.
    assert abs(_box(doc, chart.ref.id)[1] - (200.0 + chart.y)) <= 5.0


# --- G2: a facade whose BUILD fails leaves no orphan -------------------------


def test_a_legend_with_an_unpaintable_swatch_adds_nothing_at_all() -> None:
    doc = _doc(600, 400)
    ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    before = export_svg(doc)

    with pytest.raises(InvalidArgument, match="swatch"):
        ops.add_legend(doc, entries=[LegendItem(label="Ghost", swatch="no-such-kind")], x=200, y=20)

    assert export_svg(doc) == before


def test_a_chart_with_an_unknown_style_adds_nothing_at_all() -> None:
    doc = _doc(600, 400)
    ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a"], series=[Series(name="s", values=[1])]),
        x=10,
        y=10,
    )
    before = export_svg(doc)

    with pytest.raises(InvalidArgument, match="no style named"):
        ops.add_chart(
            doc,
            kind="bar",
            data=BarData(categories=["a"], series=[Series(name="s", values=[1])]),
            x=10,
            y=200,
            styles=["nope"],
        )

    assert export_svg(doc) == before


@pytest.mark.parametrize("kind", ["callout", "table", "card"])
def test_the_other_annotation_facades_leave_no_orphan_either(kind: str) -> None:
    doc = _doc(600, 400)
    node = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    before = export_svg(doc)

    with pytest.raises(InvalidArgument):
        if kind == "callout":
            ops.add_callout(doc, target=node.ref.id, text="hi", x=300, y=20, styles=["nope"])
        elif kind == "table":
            ops.add_table(doc, rows=[["a"]], x=300, y=20, styles=["nope"])
        else:
            ops.add_callout_card(doc, title="Note", x=300, y=20, styles=["nope"])

    assert export_svg(doc) == before


# --- G3: imported CSS is a shim, superseded as the registries take it over ----


def test_unloading_a_theme_after_a_round_trip_actually_unstyles_the_document() -> None:
    doc = _doc()
    ops.load_theme(doc, "house", search_paths=[FIXTURES])
    ops.add_rect(doc, x=10, y=10, width=40, height=20)
    reopened = ops.load_svg_document(svg=export_svg(doc))

    ops.load_theme(reopened, "house", search_paths=[FIXTURES])
    assert ".house-shape" in str(reopened.stylesheet().text or "")
    ops.unload_theme(reopened, "house")
    # The imported copy used to outlive the registry, so the rules stayed and nothing unstyled.
    assert ".house-shape" not in str(reopened.stylesheet().text or "")


def test_two_export_import_load_cycles_do_not_grow_the_stylesheet() -> None:
    doc = _doc()
    ops.load_theme(doc, "house", search_paths=[FIXTURES])
    ops.add_rect(doc, x=10, y=10, width=40, height=20)

    sizes = []
    current = doc
    for _ in range(2):
        current = ops.load_svg_document(svg=export_svg(current))
        ops.load_theme(current, "house", search_paths=[FIXTURES])
        sizes.append(len(str(current.stylesheet().text or "")))
    assert sizes[0] == sizes[1]
    assert str(current.stylesheet().text or "").count(".house-shape {") == 1


def test_a_hand_authored_rule_survives_every_theme_move_around_it() -> None:
    doc = _doc()
    ops.load_theme(doc, "house", search_paths=[FIXTURES])
    exported = export_svg(doc).replace("</style>", ".hand-authored { fill:#abcdef }</style>", 1)

    reopened = ops.load_svg_document(svg=exported)
    assert ".hand-authored" in reopened.imported_css
    ops.load_theme(reopened, "house", search_paths=[FIXTURES])
    ops.set_theme_variant(reopened, "dark")
    ops.unload_theme(reopened, "house")
    # Nothing here manages `.hand-authored`, so nothing here is entitled to drop it.
    assert ".hand-authored" in str(reopened.stylesheet().text or "")


def test_defining_a_style_supersedes_only_its_own_imported_rule() -> None:
    doc = _doc()
    ops.define_style(doc, "accent", {"fill": "#ff0000"})
    reopened = ops.load_svg_document(svg=export_svg(doc))
    reopened.imported_css += "\n.accent text { fill:#00ff00 }"

    ops.define_style(reopened, "accent", {"fill": "#0000ff"})
    sheet = str(reopened.stylesheet().text or "")
    assert sheet.count(".accent {") == 1
    assert "#ff0000" not in sheet and "#0000ff" in sheet
    assert ".accent text" in sheet  # a descendant rule is somebody's own CSS, not ours to drop


# --- G4: a themed=False facade stays undressed -------------------------------


def test_an_unthemed_node_is_not_dressed_by_an_edit_that_echoes_its_kind() -> None:
    # The reviewer's exact scenario: the label is what the edit is FOR, and the kind is passed
    # back unchanged the way a caller re-states a spec. The repair path used to read only the
    # classes, see a `service` rule resident and the node not wearing it, and dress it — which
    # is indistinguishable from the state a genuinely interrupted edit leaves behind.
    doc = _doc()
    ops.add_diagram_node(doc, kind="service", label="Themed", x=200, y=20)  # residency
    placed = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20, themed=False)
    assert _classes(doc, placed.ref.id) == []

    ops.edit_diagram_node(doc, placed.ref.id, label="Gateway", kind="service")
    assert _classes(doc, placed.ref.id) == []


@pytest.mark.parametrize("facade", ["edge", "container", "callout", "card"])
def test_no_unthemed_facade_is_dressed_by_a_kind_echo(facade: str) -> None:
    doc = _doc(600, 400)
    one = ops.add_diagram_node(doc, kind="service", label="A", x=20, y=20)
    two = ops.add_diagram_node(doc, kind="service", label="B", x=300, y=20)

    if facade == "edge":
        target = ops.add_diagram_edge(
            doc, source=one.ref.id, target=two.ref.id, kind="data", themed=False
        ).ref.id
        assert _classes(doc, target) == []
        ops.edit_diagram_edge(doc, target, kind="data")
    elif facade == "container":
        target = ops.add_diagram_container(
            doc, members=[one.ref.id], kind="cluster", themed=False
        ).ref.id
        assert _classes(doc, target) == []
        ops.edit_diagram_container(doc, target, kind="cluster", label="Zone")
    elif facade == "callout":
        target = ops.add_callout(
            doc, target=one.ref.id, text="a note", kind="note", x=300, y=200, themed=False
        ).ref.id
        assert _classes(doc, target) == []
        ops.edit_callout(doc, target, text="a longer note", kind="note")
    else:
        target = ops.add_callout_card(
            doc, title="Note", kind="info", x=300, y=200, themed=False
        ).ref.id
        assert _classes(doc, target) == []
        ops.edit_callout_card(doc, target, title="Note!", kind="info")

    assert _classes(doc, target) == []


def test_clearing_a_facades_theme_survives_a_later_kind_echo() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    assert "default-service" in _classes(doc, placed.ref.id)

    ops.clear_theme(doc, placed.ref.id)
    assert _classes(doc, placed.ref.id) == []
    # Undressing it was a decision; naming its own kind back at it is not a request to overturn.
    ops.edit_diagram_node(doc, placed.ref.id, label="Gateway", kind="service")
    assert _classes(doc, placed.ref.id) == []


def test_a_dressed_facade_stripped_by_hand_is_still_repaired_by_naming_its_kind() -> None:
    # The other half of G4: a THEMED facade left undressed by an interrupted edit must still be
    # repairable, which is the whole reason the short-circuit reads the classes.
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    ops.remove_styles(doc, placed.ref.id, ["default-shape", "default-service"])
    assert _classes(doc, placed.ref.id) == []

    ops.edit_diagram_node(doc, placed.ref.id, kind="service")
    assert "default-service" in _classes(doc, placed.ref.id)


def test_a_scoped_apply_theme_makes_a_cleared_facade_dressable_again() -> None:
    # The other direction of the same flag: dressing a subtree says it is meant to be dressed,
    # so a facade that was cleared and then re-dressed is re-kindable again.
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    ops.clear_theme(doc, placed.ref.id)
    assert _classes(doc, placed.ref.id) == []

    ops.load_theme(doc, "house", search_paths=[FIXTURES])
    ops.apply_theme(doc, placed.ref.id, "house")
    assert "house-shape" in _classes(doc, placed.ref.id)

    ops.edit_diagram_node(doc, placed.ref.id, kind="datastore")
    assert "default-datastore" in _classes(doc, placed.ref.id)


# --- G5: resolving a dressing is a question, not a change --------------------


def test_a_failed_boolean_leaves_a_themeless_document_themeless() -> None:
    doc = _doc()
    a = ops.add_rect(doc, x=10, y=10, width=40, height=40).id
    b = ops.add_rect(doc, x=30, y=20, width=40, height=40).id
    assert doc.theme_meta == {}

    with pytest.raises(InvalidArgument):
        ops.boolean(doc, op="union", targets=[a, b], role="service", styles=["nope"])

    # The role RESOLVES (the bundled default serves it) but the style does not, so the op fails
    # after the fallback theme was consulted — consulting it must not have installed it.
    assert doc.theme_meta == {}
    assert doc.theme_css == {}
    assert not str(doc.stylesheet().text or "").strip()


def test_asking_whether_a_role_is_servable_installs_nothing() -> None:
    doc = _doc()
    with pytest.raises(InvalidArgument):
        ops.add_rect(doc, x=0, y=0, width=10, height=10, role="service", styles=["nope"])
    assert doc.theme_meta == {}
    assert not str(doc.stylesheet().text or "").strip()


def test_a_successful_role_using_op_installs_the_fallback_exactly_once() -> None:
    doc = _doc()
    a = ops.add_rect(doc, x=10, y=10, width=40, height=40).id
    b = ops.add_rect(doc, x=30, y=20, width=40, height=40).id
    ref = ops.boolean(doc, op="union", targets=[a, b], role="service")
    assert "default-service" in _classes(doc, ref.id)
    assert list(doc.theme_meta) == ["default"]
    sheet = str(doc.stylesheet().text or "")
    assert sheet.count(".default-service {") == 1


# --- G6: an explicit x/y is parent-local, exactly as add_rect's is -----------


def test_an_explicit_legend_position_means_what_it_means_for_a_rect() -> None:
    doc = _doc(600, 400)
    layer = ops.create_group(doc, name="shifted", transform="translate(100,50)")
    ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20, parent=layer.id)
    legend = ops.add_legend(doc, x=10, y=10, parent=layer.id)
    rect = ops.add_rect(doc, x=10, y=10, width=5, height=5, parent=layer.id)

    legend_box, rect_box = _box(doc, legend.ref.id), _box(doc, rect.id)
    assert abs(legend_box[0] - rect_box[0]) <= 0.5
    assert abs(legend_box[1] - rect_box[1]) <= 0.5
    assert abs(legend_box[0] - 110.0) <= 0.5  # parent offset + the caller's own 10


def test_an_explicit_callout_position_means_what_it_means_for_a_rect() -> None:
    doc = _doc(600, 400)
    layer = ops.create_group(doc, name="shifted", transform="translate(100,50)")
    node = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20, parent=layer.id)
    callout = ops.add_callout(doc, target=node.ref.id, text="a note", parent=layer.id, x=250, y=150)
    card = next(child for child in doc.resolve(callout.ref.id) if str(child.TAG) == "rect")
    box = _box(doc, str(card.get_id()))
    assert abs(box[0] - 350.0) <= 0.5 and abs(box[1] - 200.0) <= 0.5


# --- G7: an edit validates before it mutates ---------------------------------


def test_a_node_edit_with_a_bogus_kind_changes_nothing_at_all() -> None:
    doc = _doc()
    placed = ops.add_diagram_node(doc, kind="service", label="API", x=20, y=20)
    before = export_svg(doc)

    with pytest.raises(InvalidArgument):
        ops.edit_diagram_node(
            doc, placed.ref.id, label="a much longer label than before", kind="no-such-kind"
        )

    # The label used to be re-measured, the shape re-sized and the text re-written BEFORE the
    # kind was resolved, so a refused edit left a node relabelled by a call that failed.
    assert export_svg(doc) == before


# --- G8: a refit writes both children in the group's frame -------------------


def test_a_container_refits_around_its_members_after_its_rect_is_translated() -> None:
    doc = _doc(600, 400)
    one = ops.add_diagram_node(doc, kind="service", label="A", x=20, y=20)
    two = ops.add_diagram_node(doc, kind="service", label="B", x=250, y=20)
    placed = ops.add_diagram_container(doc, members=[one.ref.id, two.ref.id], label="Zone")
    rect = next(child for child in doc.resolve(placed.ref.id) if str(child.TAG) == "rect")

    ops.translate_node(doc, str(rect.get_id()), dx=40, dy=30)
    ops.reflow(doc)

    members = (
        min(_box(doc, one.ref.id)[0], _box(doc, two.ref.id)[0]),
        min(_box(doc, one.ref.id)[1], _box(doc, two.ref.id)[1]),
        max(
            _box(doc, one.ref.id)[0] + _box(doc, one.ref.id)[2],
            _box(doc, two.ref.id)[0] + _box(doc, two.ref.id)[2],
        ),
    )
    first = _box(doc, str(rect.get_id()))
    assert first[0] < members[0] and first[1] < members[1]
    assert first[0] + first[2] > members[2]
    # The label rides with the box, and a second pass is a fixpoint rather than another slide.
    label = next(child for child in doc.resolve(placed.ref.id) if str(child.TAG) == "text")
    label_box = _box(doc, str(label.get_id()))
    assert first[0] <= label_box[0] + 0.5 and first[1] <= label_box[1] + 0.5

    ops.reflow(doc)
    assert _close(first, _box(doc, str(rect.get_id())), 0.5)


# --- G9: a facade wears whichever of its hooks the theme actually defines ----

_GAUGE = """
:root { --gauge-ink: #204020 }

/** Bars, gauge's way. Deliberately NO bare `.chart` rule. */
.chart--bar { fill: none }

/** An axis. */
.axis { stroke: var(--gauge-ink) }

/** A gridline. */
.gridline { stroke: #dddddd }

/** A tick label. */
.tick-label { fill: var(--gauge-ink) }

/** The first series. */
.series-1 { fill: #204020 }
"""
_GAUGE_MANIFEST = 'name = "gauge"\n\n[serves]\ncategories = ["chart"]\n'


def _part_classes(doc: Document, target: str) -> set[str]:
    group = doc.resolve(target)
    return {
        cls
        for child in group.iter()
        if child is not group
        for cls in str(child.get("class") or "").split()
    }


def test_a_chart_wearing_only_its_type_hook_still_dresses_its_parts(tmp_path: Path) -> None:
    paths = _write_theme(tmp_path, "gauge", _GAUGE, _GAUGE_MANIFEST)
    doc = _doc(400, 300)
    ops.load_theme(doc, "gauge", search_paths=[paths])
    placed = ops.add_chart(
        doc,
        kind="bar",
        data=BarData(categories=["a", "b"], series=[Series(name="s", values=[1, 2])]),
        x=10,
        y=10,
    )
    assert "gauge-chart--bar" in _classes(doc, placed.ref.id)
    assert "gauge-chart" not in _classes(doc, placed.ref.id)

    # `worn_theme` used to ask about the bare `.chart` hook alone, call this chart undressed,
    # and rebuild every axis, gridline and bar with no class on it.
    built = _part_classes(doc, placed.ref.id)
    assert "gauge-axis" in built and "gauge-series-1" in built

    ops.edit_chart(doc, placed.ref.id, title="Counts")
    assert _part_classes(doc, placed.ref.id) >= {"gauge-axis", "gauge-series-1"}
