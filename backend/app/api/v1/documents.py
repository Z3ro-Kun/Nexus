"""FastAPI endpoints for document upload, management, and inspection."""

from collections.abc import Sequence
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.core.dependencies import SessionDep
from app.documents.extractor import DocumentExtractionError
from app.documents.service import DocumentService
from app.schemas.documents import DocumentDetail, DocumentRead, RawDocumentCreate

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def upload_document(
    session: SessionDep,
    file: UploadFile = File(...),
) -> DocumentRead:
    """Upload a document file (PDF, TXT, MD, CSV, JSON, code) and extract its text."""
    filename = file.filename or "uploaded_document.txt"
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File '{filename}' is empty.",
        )

    service = DocumentService(session)
    try:
        doc = await service.create_from_bytes(
            filename=filename,
            content=content,
            content_type=file.content_type,
        )
        await session.commit()
    except DocumentExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return service.to_read_schema(doc)


@router.post("/raw", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def create_raw_document(
    body: RawDocumentCreate,
    session: SessionDep,
) -> DocumentRead:
    """Create a document from raw text content."""
    service = DocumentService(session)
    doc = await service.create_from_text(
        filename=body.filename,
        content_text=body.content_text,
        content_type=body.content_type,
    )
    await session.commit()
    return service.to_read_schema(doc)


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(
    document_id: UUID,
    session: SessionDep,
) -> DocumentDetail:
    """Get document metadata and its full extracted text."""
    service = DocumentService(session)
    doc = await service.get_document(document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found.",
        )
    return service.to_detail_schema(doc)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    session: SessionDep,
) -> None:
    """Delete a document by id."""
    service = DocumentService(session)
    deleted = await service.delete_document(document_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document {document_id} not found.",
        )
    await session.commit()
