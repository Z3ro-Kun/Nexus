"""Tests for conversations API and chat threads."""

from uuid import uuid4

import httpx
import pytest


async def test_conversation_lifecycle(api: httpx.AsyncClient) -> None:
    # 1. Create a conversation
    create_resp = await api.post(
        "/api/v1/conversations",
        json={"title": "Test Chat", "objective": "Hello world initial prompt"},
    )
    assert create_resp.status_code == 201, create_resp.text
    data = create_resp.json()
    assert data["title"] == "Test Chat"
    conv_id = data["id"]
    assert len(data["turns"]) == 1
    initial_run_id = data["turns"][0]["run_id"]
    assert data["turns"][0]["objective"] == "Hello world initial prompt"

    # 2. List conversations
    list_resp = await api.get("/api/v1/conversations")
    assert list_resp.status_code == 200, list_resp.text
    convs = list_resp.json()
    assert any(c["id"] == conv_id for c in convs)

    # 3. Send follow-up message into the conversation
    msg_resp = await api.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"objective": "What was my previous prompt?"},
    )
    assert msg_resp.status_code == 201, msg_resp.text
    followup_run = msg_resp.json()
    assert followup_run["conversation_id"] == conv_id
    assert followup_run["parent_run_id"] == initial_run_id

    # 4. Get conversation details with both turns
    detail_resp = await api.get(f"/api/v1/conversations/{conv_id}")
    assert detail_resp.status_code == 200, detail_resp.text
    detail = detail_resp.json()
    assert len(detail["turns"]) == 2
    assert detail["turns"][1]["objective"] == "What was my previous prompt?"

    # 5. Delete conversation
    del_resp = await api.delete(f"/api/v1/conversations/{conv_id}")
    assert del_resp.status_code == 204, del_resp.text

    # 6. Verify deleted
    get_deleted = await api.get(f"/api/v1/conversations/{conv_id}")
    assert get_deleted.status_code == 404
