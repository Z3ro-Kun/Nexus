"""Service layer for document storage, extraction, and retrieval."""

from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.documents.extractor import extract_text_from_bytes
from app.models.document import Document
from app.persistence.types import utc_now
from app.schemas.documents import AttachedDocument, DocumentDetail, DocumentRead


class DocumentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_from_bytes(
        self, filename: str, content: bytes, content_type: str | None = None
    ) -> Document:
        extracted_text = extract_text_from_bytes(filename, content, content_type)
        doc = Document(
            id=uuid4(),
            filename=filename,
            content_type=content_type or "text/plain",
            size_bytes=len(content),
            content_text=extracted_text,
            created_at=utc_now(),
        )
        self._session.add(doc)
        await self._session.flush()
        return doc

    async def create_from_text(
        self, filename: str, content_text: str, content_type: str = "text/plain"
    ) -> Document:
        doc = Document(
            id=uuid4(),
            filename=filename,
            content_type=content_type,
            size_bytes=len(content_text.encode("utf-8")),
            content_text=content_text.strip(),
            created_at=utc_now(),
        )
        self._session.add(doc)
        await self._session.flush()
        return doc

    async def get_document(self, document_id: UUID) -> Document | None:
        stmt = select(Document).where(Document.id == document_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_documents(self, document_ids: Sequence[UUID]) -> list[Document]:
        if not document_ids:
            return []
        stmt = select(Document).where(Document.id.in_(document_ids))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete_document(self, document_id: UUID) -> bool:
        doc = await self.get_document(document_id)
        if doc is None:
            return False
        await self._session.delete(doc)
        await self._session.flush()
        return True

    @staticmethod
    def to_read_schema(doc: Document) -> DocumentRead:
        preview = doc.content_text[:300].strip()
        if len(doc.content_text) > 300:
            preview += "…"
        return DocumentRead(
            id=doc.id,
            filename=doc.filename,
            content_type=doc.content_type,
            size_bytes=doc.size_bytes,
            char_count=len(doc.content_text),
            content_preview=preview,
            created_at=doc.created_at,
        )

    @staticmethod
    def to_detail_schema(doc: Document) -> DocumentDetail:
        read = DocumentService.to_read_schema(doc)
        return DocumentDetail(
            **read.model_dump(),
            content_text=doc.content_text,
        )

    @staticmethod
    def to_attached_document(doc: Document) -> AttachedDocument:
        return AttachedDocument(
            id=str(doc.id),
            filename=doc.filename,
            content_type=doc.content_type,
            content=doc.content_text,
        )
