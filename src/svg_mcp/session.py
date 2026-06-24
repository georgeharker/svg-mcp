"""Multi-document session store with an active-document default.

Documents are addressed by an explicit ``document_id``, but the store also tracks an *active*
document (the most recently created or touched one). Tools may omit ``document_id`` to operate
on the active document; passing one explicitly always overrides and becomes the new active.
"""

from __future__ import annotations

from itertools import count

from .model.document import Document
from .model.errors import DocumentNotFound


class DocumentStore:
    """Holds the live documents for a server session, plus the active-document pointer."""

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._counter = count(1)
        self._active: str | None = None

    def create(
        self, width: float, height: float, viewbox: str | None = None
    ) -> tuple[str, Document]:
        """Create and register a new document, make it active; returns (id, document)."""
        document_id = f"doc{next(self._counter)}"
        document = Document.create(width, height, viewbox)
        self._docs[document_id] = document
        self._active = document_id
        return document_id, document

    def register(self, document: Document) -> str:
        """Register an externally built document, make it active; returns its new id."""
        document_id = f"doc{next(self._counter)}"
        self._docs[document_id] = document
        self._active = document_id
        return document_id

    def resolve_id(self, document_id: str | None) -> str:
        """Resolve an explicit id, or the active document when ``document_id`` is None."""
        if document_id is not None:
            return document_id
        if self._active is None:
            raise DocumentNotFound(
                "no active document — create one with create_document, or pass document_id"
            )
        return self._active

    def get(self, document_id: str | None = None) -> Document:
        """Get a document by id (or the active one if None); the resolved doc becomes active."""
        resolved = self.resolve_id(document_id)
        try:
            document = self._docs[resolved]
        except KeyError:
            raise DocumentNotFound(f"no document with id {resolved!r}") from None
        self._active = resolved
        return document

    def peek(self, document_id: str) -> Document:
        """Get a document by id WITHOUT changing the active pointer (for read-only access)."""
        try:
            return self._docs[document_id]
        except KeyError:
            raise DocumentNotFound(f"no document with id {document_id!r}") from None

    @property
    def active_id(self) -> str | None:
        """The id of the active document, or None if no documents are open."""
        return self._active

    def set_active(self, document_id: str) -> str:
        """Make ``document_id`` the active document."""
        if document_id not in self._docs:
            raise DocumentNotFound(f"no document with id {document_id!r}")
        self._active = document_id
        return document_id

    def list_ids(self) -> list[str]:
        return list(self._docs)

    def delete(self, document_id: str | None = None) -> str:
        """Delete a document (active if None); returns the deleted id."""
        resolved = self.resolve_id(document_id)
        if self._docs.pop(resolved, None) is None:
            raise DocumentNotFound(f"no document with id {resolved!r}")
        if self._active == resolved:
            self._active = next(reversed(self._docs), None) if self._docs else None
        return resolved
