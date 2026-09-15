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
import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
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

    # 2. 取数类问题走受控工具链，其余走 CEO 全流程。
    #    两条路**共用 should_orchestrate 这一道闸门** —— 否则同一个问题在
    #    非流式/流式底下的答法会不一致（这边用工作区文件瞎猜，那边真去取数）。
    from src.ceo.dispatcher import run_ceo
    from src.session.scope import scope
    from src.tools import orchestrator

    reply = ""
    role = mode = None
    try:
        with scope(conversation_id):
            recent = _recent_texts(conversation_id)
            if orchestrator.should_orchestrate(req.message, recent):
                trace = await orchestrator.plan_and_fetch(req.message, req.user_id, recent)
                if trace.usable:
                    messages = orchestrator.build_answer_messages(trace, req.message, recent)
                    resp = await orchestrator.analysis_llm().ainvoke(messages)
                    reply = msg_text(resp)
                    unsupported = orchestrator.verify_numbers(reply, trace.numbers)
                    if unsupported:
                        logger.warning(
                            "成文数字无出处 conversation=%s: %s", conversation_id, unsupported[:8]
                        )
                    role, mode = "data", "orchestrated"
            if not reply:
                result = await run_ceo(req.message, session_id=conversation_id)
                reply = str(result.get("final_output") or result.get("output") or "")
                role = result.get("role")
                mode = result.get("mode")
    except Exception as e:
        logger.exception("chat failed")
        raise HTTPException(status_code=500, detail=f"ai error: {type(e).__name__}: {e}")

    # 2.5 对外总闸：协议原文（JSON 信封 / XML 工具调用）绝不能进用户界面。
    #     上游各条链都各自清洗过，这里是最后一道，防止将来新加的路径再漏。
    from src.execution.executor import sanitize_for_user
    reply = sanitize_for_user(reply)

    # 3. Record the turn in persistent history
    _history(conversation_id).record_conversation(req.message, reply)

    # 4. Touch last_active
    user_store.touch_conversation(conversation_id)

    return {
        "conversation_id": conversation_id,
        "title": title,
        "reply": reply,
        "role": role,
        "mode": mode,
    }


# ─── 流式对话（衡水定制：逐字输出 + 取数过程可见 + 导出）───────────────
#
# 事件契约（widget.js 按 type 分派；只增字段、不改语义）：
#   {"type":"meta","conversation_id":...,"title":...}
#   {"type":"status","text":"正在查询数据…"}
#   {"type":"tool","name":"letter_overview","label":"正在统计信件总量…"}
#   {"type":"files","files":[{"url":"/api/ai/files/<handle>/","name":"..."}]}
#   {"type":"delta","text":"..."}                    ← 逐字追加
#   {"type":"correction","unsupported":["12","3"]}    ← 数字查无出处（罕见）
#   {"type":"error","detail":"..."}
#   {"type":"done","reply":"<完整文本>"}

TOOL_LABELS = {
    "whoami_scope": "正在确认你的数据范围…",
    "letter_overview": "正在统计信件总量…",
    "letter_search": "正在查询信件…",
    "letter_detail": "正在读取信件详情…",
    "letter_stats_by_unit": "正在按单位汇总…",
    "letter_trend": "正在统计时间趋势…",
    "dict_lookup": "正在读取数据字典…",
    "export_letters": "正在生成导出文件…",
}


def _sse(payload: dict) -> str:
    """一条 SSE 事件。json 会转义换行，所以不会破坏 `data:` 分帧。"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def msg_text(msg) -> str:
    """取 LLM 回复的纯文本。

    ``content`` 可能是字符串，也可能是分块 list（多模态/部分 provider 的流式
    分片），两种都得兜住 —— 直接 ``str(content)`` 会把 list 的 repr 拼进成文里。
    """
    content = getattr(msg, "content", msg)
    if isinstance(content, list):
        parts = []
        for piece in content:
            parts.append(str(piece.get("text", "")) if isinstance(piece, dict) else str(piece))
        return "".join(parts)
    return str(content or "")


def _recent_texts(conversation_id: str, limit: int = 3) -> list[str]:
    """最近几轮「用户问 + AI 答」，给编排器做指代消解（"那按单位呢"）。"""
    try:
        turns = _history(conversation_id).get_recent_conversations(limit)
    except Exception:  # noqa: BLE001 - 历史读不到不该挡住这轮对话
        return []
    out: list[str] = []
    for t in turns or []:
        user = str(t.get("user") or "").strip()
        assistant = str(t.get("assistant") or "").strip()
        if user:
            out.append(f"用户：{user}\nAI：{assistant}")
    return out


async def _typing_chunks(text: str, pieces: int = 140):
    """把**已经拿到的完整文本**分片推送，用于非取数路径的逐字呈现。

    非取数回复走的是 run_ceo 全流程（角色路由 + 记忆），那条链是同步返回的，
    没有 token 流可用；为了界面一致，这里按片推送并留一点间隔。
    取数路径则是真流式（llm.astream），不经过本函数。
    """
    if not text:
        return
    size = max(1, len(text) // pieces)
    for i in range(0, len(text), size):
        yield text[i:i + size]
        await asyncio.sleep(0.012)


@app.post("/ai/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话：SSE 逐字返回，取数过程对用户可见。"""
    if req.conversation_id:
        conv = _own_conversation(req.user_id, req.conversation_id)
        conversation_id = conv["conversation_id"]
        title = conv.get("title") or "对话"
    else:
        created = user_store.create_conversation(req.user_id, req.title or "新对话")
        conversation_id = created["conversation_id"]
        title = created["title"]

    from src.session.scope import scope

    async def _gen():
        yield _sse({"type": "meta", "conversation_id": conversation_id, "title": title})
        final_text = ""
        try:
            with scope(conversation_id):
                recent = _recent_texts(conversation_id)
                from src.tools import orchestrator

                trace = None
                if orchestrator.should_orchestrate(req.message, recent):
                    yield _sse({"type": "status", "text": "正在查询数据…"})
                    trace = await orchestrator.plan_and_fetch(req.message, req.user_id, recent)
                    for call in trace.calls:
                        yield _sse({
                            "type": "tool",
                            "name": call.name,
                            "label": TOOL_LABELS.get(call.name, "正在查询…"),
                        })
                    if trace.files:
                        yield _sse({"type": "files", "files": trace.files})

                if trace is not None and trace.usable:
                    # 真流式：数据已锁定，模型只负责措辞。
                    messages = orchestrator.build_answer_messages(trace, req.message, recent)
                    buf: list[str] = []
                    async for chunk in orchestrator.analysis_llm().astream(messages):
                        piece = msg_text(chunk)
                        if not piece:
                            continue
                        buf.append(piece)
                        yield _sse({"type": "delta", "text": piece})
                    final_text = "".join(buf)

                    # 数字溯源：成文里的数字必须能在工具返回里找到出处。
                    unsupported = orchestrator.verify_numbers(final_text, trace.numbers)
                    if unsupported:
                        logger.warning("成文含无出处数字: %s", unsupported[:8])
                        yield _sse({"type": "correction", "unsupported": unsupported[:8]})
                else:
                    # 普通对话（或取数不可用）：走完整 CEO 流程，再逐字呈现。
                    from src.ceo.dispatcher import run_ceo
                    from src.execution.executor import sanitize_for_user
                    result = await run_ceo(req.message, session_id=conversation_id)
                    final_text = sanitize_for_user(
                        str(result.get("final_output") or result.get("output") or "")
                    )
                    async for piece in _typing_chunks(final_text):
                        yield _sse({"type": "delta", "text": piece})

            if final_text:
                _history(conversation_id).record_conversation(req.message, final_text)
            user_store.touch_conversation(conversation_id)
        except Exception as e:  # noqa: BLE001 - 流已经开始，只能以事件形式报错
            logger.exception("chat_stream failed")
            yield _sse({"type": "error", "detail": f"{type(e).__name__}: {e}"})
        yield _sse({"type": "done", "reply": final_text})

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
