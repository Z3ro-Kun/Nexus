"""Unit and integration tests for document extraction, storage, and context attachment."""

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.documents.extractor import (
    DocumentExtractionError,
    extract_text_from_bytes,
)
from app.documents.service import DocumentService
from app.events.types import AttachedDocument, TaskCreated
from app.persistence.database import Database
from app.schemas.runs import RunCreate
from app.services.runs import RunService
from app.state.context_builder import build_task_context


def test_extractor_text_and_markdown() -> None:
    text_bytes = b"Hello Nexus! This is a test document."
    extracted = extract_text_from_bytes("notes.txt", text_bytes, "text/plain")
    assert extracted == "Hello Nexus! This is a test document."

    md_bytes = b"# Architecture\n\nNEXUS is an autonomous multi-agent system."
    extracted_md = extract_text_from_bytes("arch.md", md_bytes, "text/markdown")
    assert "# Architecture" in extracted_md
    assert "NEXUS" in extracted_md


def test_extractor_json() -> None:
    json_bytes = b'{"name": "Nexus", "features": ["agents", "tools", "verification"]}'
    extracted = extract_text_from_bytes("config.json", json_bytes, "application/json")
    assert '"name": "Nexus"' in extracted
    assert '"agents"' in extracted


def test_extractor_csv() -> None:
    csv_bytes = b"model,provider,cost\ngpt-4o,openai,high\nclaude-3-5-sonnet,anthropic,medium\n"
    extracted = extract_text_from_bytes("models.csv", csv_bytes, "text/csv")
    assert "gpt-4o" in extracted
    assert "claude-3-5-sonnet" in extracted


def test_extractor_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Quarterly Revenue: $5.2M"

    mock_reader = MagicMock()
    mock_reader.pages = [mock_page]

    monkeypatch.setattr("pypdf.PdfReader", lambda stream: mock_reader)

    extracted = extract_text_from_bytes("report.pdf", b"%PDF-1.4 dummy", "application/pdf")
    assert "Quarterly Revenue: $5.2M" in extracted
    assert "--- Page 1 ---" in extracted


def test_extractor_empty_file_rejected() -> None:
    with pytest.raises(DocumentExtractionError, match="No readable text"):
        extract_text_from_bytes("empty.txt", b"   \n\t  ")


async def test_document_service_crud(database: Database) -> None:
    async with database.session_factory() as session:
        service = DocumentService(session)

        # 1. Create from text
        doc1 = await service.create_from_text(
            filename="guidelines.md",
            content_text="# Coding Guidelines\n\n1. Always test your code.",
            content_type="text/markdown",
        )
        await session.commit()
        assert doc1.id is not None
        assert doc1.filename == "guidelines.md"

        # 2. Get document
        fetched = await service.get_document(doc1.id)
        assert fetched is not None
        assert fetched.id == doc1.id
        assert "Always test your code" in fetched.content_text

        # 3. Schemas
        read_schema = service.to_read_schema(fetched)
        assert read_schema.id == doc1.id
        assert read_schema.filename == "guidelines.md"
        assert read_schema.char_count > 0

        detail_schema = service.to_detail_schema(fetched)
        assert detail_schema.content_text == fetched.content_text

        attached = service.to_attached_document(fetched)
        assert attached.id == str(doc1.id)
        assert attached.filename == "guidelines.md"

        # 4. Get multiple
        docs = await service.get_documents([doc1.id, uuid4()])
        assert len(docs) == 1
        assert docs[0].id == doc1.id

        # 5. Delete document
        deleted = await service.delete_document(doc1.id)
        assert deleted is True
        await session.commit()

        # 6. Verify deletion
        assert await service.get_document(doc1.id) is None


async def test_api_document_endpoints(api: httpx.AsyncClient) -> None:
    # Test upload endpoint
    file_content = b"System design notes: microservices with event sourcing."
    response = await api.post(
        "/api/v1/documents/upload",
        files={"file": ("design.txt", file_content, "text/plain")},
    )
    assert response.status_code == 201
    data = response.json()
    doc_id = data["id"]
    assert data["filename"] == "design.txt"
    assert "System design" in data["content_preview"]

    # Test get document
    get_res = await api.get(f"/api/v1/documents/{doc_id}")
    assert get_res.status_code == 200
    detail = get_res.json()
    assert detail["content_text"] == file_content.decode()

    # Test raw text creation
    raw_res = await api.post(
        "/api/v1/documents/raw",
        json={
            "filename": "raw_note.txt",
            "content_text": "Important memo about project roadmap.",
            "content_type": "text/plain",
        },
    )
    assert raw_res.status_code == 201
    raw_id = raw_res.json()["id"]

    # Test delete
    del_res = await api.delete(f"/api/v1/documents/{raw_id}")
    assert del_res.status_code == 204

    # Verify deleted
    missing = await api.get(f"/api/v1/documents/{raw_id}")
    assert missing.status_code == 404


async def test_run_creation_with_documents_in_context(database: Database) -> None:
    async with database.session_factory() as session:
        # Create a document in database
        doc_service = DocumentService(session)
        doc = await doc_service.create_from_text(
            filename="budget.txt",
            content_text="Total budget allocated is $50,000.",
        )
        await session.commit()

        run_service = RunService(session)
        # Create run attaching this document by ID, plus an inline document
        inline_doc = AttachedDocument(
            id="inline-1",
            filename="inline_notes.md",
            content_type="text/markdown",
            content="Key milestone: deliver MVP by end of month.",
        )

        run = await run_service.create_run(
            goal="Analyze the budget and milestones.",
            constraints=["Keep under $50,000"],
            document_ids=[doc.id],
            documents=[inline_doc],
        )

        assert run.id is not None

        # Project state and check documents
        state = await run_service.get_state(run.id)
        assert len(state.documents) == 2
        doc_names = {d.filename for d in state.documents}
        assert "budget.txt" in doc_names
        assert "inline_notes.md" in doc_names

        # Create a task and verify TaskContext has state.documents
        task_payload = TaskCreated(
            task_id="task_1",
            title="Analyze Budget",
            description="Examine the allocated budget from the attached documents.",
            agent_type="analyst",
            task_type="analysis",
            dependencies=[],
        )
        await run_service.create_tasks(run.id, [task_payload])

        # Re-fetch state
        state_with_task = await run_service.get_state(run.id)
        task_context = build_task_context(state_with_task, "task_1")

        assert len(task_context.documents) == 2
        assert any("50,000" in d.content for d in task_context.documents)
        assert any("MVP" in d.content for d in task_context.documents)
