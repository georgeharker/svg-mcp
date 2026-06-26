"""Stable, AI-facing handles to nodes in a document."""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Concatenate

if TYPE_CHECKING:
    from .document import Document


@dataclass(slots=True, frozen=True)
class NodeRef:
    """A handle the AI keeps after creating or locating a node.

    ``id`` is the precise, unique SVG id (the machine handle). ``name`` is the optional
    friendly label (``inkscape:label``). ``tag`` is the SVG element name (e.g. ``rect``).
    ``warning`` is an optional non-fatal advisory (e.g. the assigned ``name`` collides with an
    existing one, making ``@name`` lookups ambiguous); it is only present when there is one.
    """

    id: str
    tag: str
    name: str | None = None
    warning: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        result: dict[str, str | None] = {"id": self.id, "tag": self.tag, "name": self.name}
        if self.warning is not None:
            result["warning"] = self.warning
        return result


def names_node[**P](
    fn: Callable[Concatenate[Document, P], NodeRef],
) -> Callable[Concatenate[Document, P], NodeRef]:
    """Decorate a node-naming op: register the returned node's friendly name and, if it collides
    with an existing name/id, attach a non-fatal ``warning`` to the handle.

    The single mechanism behind duplicate-name detection, so every naming op (``_place``,
    ``set_name``, ``duplicate``, ``rename_layer``, ``boolean``, …) behaves the same. The
    :class:`Document` is the op's first positional argument; the op stays oblivious to warnings.
    """

    @functools.wraps(fn)
    def wrapper(doc: Document, /, *args: P.args, **kwargs: P.kwargs) -> NodeRef:
        ref = fn(doc, *args, **kwargs)
        if ref.name is not None:
            warning = doc.name_warning(ref.name, exclude_id=ref.id)
            doc.register_name(ref.name, ref.id)
            if warning is not None and ref.warning is None:
                return replace(ref, warning=warning)
        return ref

    return wrapper
