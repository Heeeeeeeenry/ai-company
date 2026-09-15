"""FastAPI HTTP adapter — lets the admin system (民意智感中心管理端) embed ai-company.

Design principle (per the agreed architecture):
    login credential  ->  user_id         (done by the host admin system)
    user_id           ->  [conversation_id...]   (multi-chat per user)
    conversation_id   ->  history (permanent, survives login expiry)

ai-company does NOT handle authentication.  The admin system authenticates the
user (via its own cookie / Django session / JWT) and passes the resolved
``user_id`` + ``conversation_id`` in the request body.  This keeps ai-company
fully decoupled from the host's auth mechanism, so credential expiry never
loses history (history is keyed by conversation_id, not by the credential).

Run with:
    python -m src.api_server --host 127.0.0.1 --port 8020
"""
from __future__ import annotations

import argparse
import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn

from src.session import user_store
from src.session.memory import get_session_memory, SessionMemory

logger = logging.getLogger("ai_company.api")

app = FastAPI(title="ai-company embedding API", version="0.1.0")


# ─── Request / Response models ──────────────────────

class ChatRequest(BaseModel):
    user_id: str = Field(..., description="Resolved user id from the host's auth")
    conversation_id: Optional[str] = Field(
        None, description="Existing conversation to continue; omit to auto-create")
    message: str = Field(..., min_length=1, max_length=8000)
    title: Optional[str] = Field(None, description="Title for a new conversation")


class CreateConversationRequest(BaseModel):
    user_id: str = Field(..., description="Resolved user id from the host's auth")
    title: str = Field("新对话", max_length=200)


# ─── Helpers ─────────────────────────────────────────

def _history(session_id: str) -> SessionMemory:
    """Return the SessionMemory for a conversation (conversation_id == session_id)."""
    return get_session_memory(session_id)


def _own_conversation(user_id: str, conversation_id: str) -> dict:
    """Fetch a conversation and verify it belongs to user_id."""
    conv = user_store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    if conv["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="conversation not owned by user")
    return conv


# ─── Endpoints ───────────────────────────────────────

@app.get("/ai/health")
def health():
    """Liveness + the model this service will actually call.

    Exposing the resolved model/endpoint makes "did the deployment pick up my
    DeepSeek config?" verifiable with one curl, instead of digging in logs.
    """
    from src.model_config import load_runtime_model, is_oneapi_endpoint, resolve_api_key
    rm = load_runtime_model()
    key = resolve_api_key(rm.base_url)
    return {
        "status": "ok",
        "service": "ai-company",
        "model": rm.model,
        "provider": rm.provider,
        "base_url": rm.base_url,
        "endpoint_kind": "oneapi" if is_oneapi_endpoint(rm.base_url) else "external",
        "api_key_set": bool(key),
        "api_key_source": "AI_COMPANY_DEEPSEEK_API_KEY/DEEPSEEK_API_KEY",
    }


@app.post("/ai/chat")
async def chat(req: ChatRequest):
    """Send a message to the AI within a user's conversation.

    * Creates the conversation if ``conversation_id`` is not provided.
    * Records the turn in the conversation's history.
    * Returns the AI reply.
    """
    # 1. Resolve / create conversation
    if req.conversation_id:
        conv = _own_conversation(req.user_id, req.conversation_id)
        conversation_id = conv["conversation_id"]
        title = conv.get("title") or "对话"
    else:
        created = user_store.create_conversation(req.user_id, req.title or "新对话")
        conversation_id = created["conversation_id"]
        title = created["title"]

    # 2. Run the CEO with this conversation's history injected.
    #    The conversation scope is bound for the whole call so that every shared
    #    store (workspace artifacts, engram memory) is namespaced to THIS chat —
    #    without it, one user's content leaks into the next user's context.
    from src.ceo.dispatcher import run_ceo
    from src.session.scope import scope
    try:
        with scope(conversation_id):
            result = await run_ceo(req.message, session_id=conversation_id)
    except Exception as e:
        logger.exception("run_ceo failed")
        raise HTTPException(status_code=500, detail=f"ai error: {type(e).__name__}: {e}")

    reply = str(result.get("final_output") or result.get("output") or "")

    # 3. Record the turn in persistent history
    _history(conversation_id).record_conversation(req.message, reply)

    # 4. Touch last_active
    user_store.touch_conversation(conversation_id)

    return {
        "conversation_id": conversation_id,
        "title": title,
        "reply": reply,
        "role": result.get("role"),
        "mode": result.get("mode"),
    }


@app.get("/ai/conversations")
def list_conversations(user_id: str):
    """List all conversations for a user (most recently active first)."""
    convs = user_store.list_conversations(user_id)
    result = []
    for c in convs:
        history = _history(c["conversation_id"])
        recent = history.get_recent_conversations(3)
        result.append({
            "conversation_id": c["conversation_id"],
            "title": c["title"],
            "created_at": c["created_at"],
            "last_active": c["last_active"],
            "preview": recent[-1]["user"][:80] if recent else "",
            "turn_count": len(history.get_recent_conversations(1000)),
        })
    return {"user_id": user_id, "conversations": result}


@app.post("/ai/conversations")
def create_conversation(req: CreateConversationRequest):
    """Create a new conversation for a user."""
    created = user_store.create_conversation(req.user_id, req.title)
    return created


@app.get("/ai/conversations/{conversation_id}/history")
def conversation_history(conversation_id: str, user_id: str):
    """Full conversation history for a user's conversation (multi-page aware)."""
    conv = _own_conversation(user_id, conversation_id)
    history = _history(conversation_id)
    turns = history.get_recent_conversations(1000)
    return {
        "conversation_id": conversation_id,
        "title": conv["title"],
        "turns": turns,
    }


@app.delete("/ai/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user_id: str):
    """Delete a conversation (ownership checked)."""
    if not user_store.delete_conversation(user_id, conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found or not owned")
    # Also clear its on-disk history + in-process cache (path honours service mode)
    from src.session.memory import session_memory_path, forget_session_memory
    try:
        os.remove(session_memory_path(conversation_id))
    except OSError:
        pass
    forget_session_memory(conversation_id)
    return {"deleted": conversation_id}


def main():
    parser = argparse.ArgumentParser(description="ai-company embedding API")
    parser.add_argument("--host", default=os.environ.get("AI_COMPANY_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_COMPANY_API_PORT", "8020")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logger.info("ai-company API starting on http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
