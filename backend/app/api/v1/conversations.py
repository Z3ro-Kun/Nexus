"""Conversation and chat thread API endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import SessionDep
from app.core.exceptions import RunNotFoundError
from app.schemas.conversations import (
    ConversationDetail,
    ConversationSummary,
    CreateConversationRequest,
    SendConversationMessageRequest,
)
from app.schemas.runs import RunRead
from app.services.conversations import ConversationService

router = APIRouter(prefix="/conversations", tags=["conversations"])


def get_conversation_service(session: SessionDep) -> ConversationService:
    return ConversationService(session)


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(service: ConversationServiceDep) -> list[ConversationSummary]:
    """List all user conversation threads ordered by recent activity."""
    return await service.list_conversations()


@router.post("", response_model=ConversationDetail, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest,
    service: ConversationServiceDep,
) -> ConversationDetail:
    """Create a new isolated conversation thread, optionally starting an initial run."""
    conv, _ = await service.create_conversation(
        title=body.title,
        objective=body.objective,
        document_ids=body.document_ids,
    )
    return await service.get_conversation(conv.id)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    service: ConversationServiceDep,
) -> ConversationDetail:
    """Get full conversation detail including all chat turns, deliverables, and documents."""
    try:
        return await service.get_conversation(conversation_id)
    except RunNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation {conversation_id} not found",
        )


@router.post("/{conversation_id}/messages", response_model=RunRead, status_code=status.HTTP_201_CREATED)
async def send_message(
    conversation_id: UUID,
    body: SendConversationMessageRequest,
    service: ConversationServiceDep,
) -> RunRead:
    """Send a follow-up message into an existing conversation, creating the next run/turn."""
    try:
        run = await service.send_message(
            conversation_id=conversation_id,
            objective=body.objective,
            document_ids=body.document_ids,
            constraints=body.constraints,
        )
        return RunRead.model_validate(run)
    except RunNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation {conversation_id} not found",
        )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    service: ConversationServiceDep,
) -> Response:
    """Permanently delete a conversation thread and all its runs, documents, and data."""
    deleted = await service.delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversation {conversation_id} not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
