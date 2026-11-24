"""Multi-document session store.

Every document is addressed by its explicit ``document_id``. There is deliberately no
"active document" default: a call that could silently land on a document the caller didn't
mean is worse than one that fails and asks for the id.
"""

from __future__ import annotations

from itertools import count

from .model.document import Document
from .model.errors import DocumentNotFound


class DocumentStore:
    """Holds the live documents for one session."""

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._counter = count(1)

    def create(
        self, width: float, height: float, viewbox: str | None = None
    ) -> tuple[str, Document]:
        """Create and register a new document; returns (id, document)."""
        document_id = f"doc{next(self._counter)}"
        document = Document.create(width, height, viewbox)
        self._docs[document_id] = document
        return document_id, document

    def register(self, document: Document) -> str:
        """Register an externally built document; returns its new id."""
        document_id = f"doc{next(self._counter)}"
        self._docs[document_id] = document
        return document_id

    def replace(self, document_id: str, document: Document) -> str:
        """Swap the Document behind an existing id (keeps the id and its preview page).

        Lets ``import_svg`` load fresh SVG into a named document instead of opening a new
        one, so the document_id, live-preview URL and resource URIs all persist.
        """
        self.get(document_id)
        self._docs[document_id] = document
        return document_id

    def get(self, document_id: str) -> Document:
        """Get a document by id."""
        try:
            return self._docs[document_id]
        except KeyError:
            raise DocumentNotFound(self._missing(document_id)) from None

    def list_ids(self) -> list[str]:
        return list(self._docs)

    def delete(self, document_id: str) -> str:
        """Delete a document; returns the deleted id."""
        if self._docs.pop(document_id, None) is None:
            raise DocumentNotFound(self._missing(document_id))
        return document_id

    def _missing(self, document_id: str) -> str:
        open_ids = ", ".join(self._docs) or "none"
        return (
            f"no document with id {document_id!r} in this session (open: {open_ids}) — "
            "create one with create_document"
        )
