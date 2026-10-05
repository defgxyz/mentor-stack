"""
Minimal OpenAI-compatible chat-completions endpoint backed by a real
claude_agent_sdk session (subscription auth via mounted ~/.claude/.credentials.json),
with the mentor MCP tools wired in. See ~/.claude/plans/vivid-greeting-perlis.md
for the full design and the known caveats.

IMPORTANT (found live, 2026-09-12): a fresh query() subprocess per HTTP request
triggered independent OAuth refreshes that couldn't persist back through a read-only
credentials mount, which can silently invalidate the refresh token your host Claude
Code login also depends on (same file, not a copy). Fixed two ways: the credentials
mount is now read-write (docker-compose.yml), and this file keeps ONE persistent
ClaudeSDKClient connection alive across requests instead of spawning a new one per
call - matching the pattern already proven in
~/repos/minecraft/edu_poc/mineflayer-bot/claude-agent-bridge.js's
createPersistentClaudeSession.
"""

import asyncio
import json
import time
import uuid
import logging

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage

from mentor_data import tools_objectives as impl
from mentor_tools import mentor_server, MENTOR_TOOL_NAMES

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mentor-bridge")

app = FastAPI()

ALLOWED_TOOLS = [f"mcp__mentor__{name}" for name in MENTOR_TOOL_NAMES]

_client: ClaudeSDKClient | None = None
_client_lock = asyncio.Lock()  # one turn in flight at a time, matches the reference pattern


class Message(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "mentor-bridge"
    messages: list[Message]
    stream: bool = False


def _build_options(system_prompt_text: str) -> ClaudeAgentOptions:
    # Python SDK field names/types differ from the Node SDK's (no strictMcpConfig,
    # settings is a path string not a dict) - setting_sources=[] is the real
    # equivalent: it skips loading user/project/local ambient config entirely
    # (which is what let the account's own claude.ai connectors leak in on the
    # Node side), so only what's explicitly passed here (mcp_servers, tools) applies.
    return ClaudeAgentOptions(
        mcp_servers={"mentor": mentor_server},
        tools=[],  # disable Claude Code's own built-ins (Bash/Read/Write) entirely
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",  # bypassPermissions is refused when running as root
        system_prompt={"type": "text", "text": system_prompt_text} if system_prompt_text else None,
        setting_sources=[],
    )


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": "mentor-bridge", "object": "model"}]}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/due-followups")
async def due_followups():
    # Plain-text, stable, no-LLM signal for hermes cron's --monitor-url mode:
    # unchanged bytes (same due-set) suppresses the agent run; a change (an
    # objective newly due, or resolved) triggers the coaching-checkin skill for
    # real. Runs in-process against the same mentor.db the tools use, so it
    # doesn't hit the sandboxing/read-only-filesystem issue --monitor-script
    # ran into inside Hermes's own script-execution environment.
    raw = impl.list_due_followups({})
    ids = sorted(o["id"] for o in json.loads(raw))
    return PlainTextResponse(",".join(str(i) for i in ids))


async def _get_client(system_prompt_text: str) -> ClaudeSDKClient:
    global _client
    if _client is None:
        # Lazy, one-time init on the first request - system prompt is fixed for the
        # client's whole lifetime (the SDK has no per-call system_prompt override on
        # an already-connected client), so later requests reuse whatever the first
        # one set. Fine here since Hermes's own SOUL.md/USER.md/MEMORY.md injection
        # is fairly stable within a running gateway; a real drift would need a
        # reconnect, not handled yet.
        options = _build_options(system_prompt_text)
        _client = ClaudeSDKClient(options=options)
        await _client.connect()
        log.info("Connected persistent ClaudeSDKClient session")
    return _client


async def _reset_client():
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            log.exception("error disconnecting stale client (ignored)")
    _client = None


async def _run_claude(req: ChatCompletionRequest) -> str:
    system_text = "\n\n".join(m.content for m in req.messages if m.role == "system")
    user_messages = [m for m in req.messages if m.role == "user"]
    prompt = user_messages[-1].content if user_messages else ""

    async with _client_lock:
        client = await _get_client(system_text)
        try:
            await client.query(prompt)
            final_text = ""
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    if message.is_error:
                        log.warning("Claude Agent SDK returned an error result: %s", message.result)
                    final_text = message.result or final_text
            return final_text
        except Exception:
            log.exception("turn failed - tearing down session so the next request starts fresh")
            await _reset_client()
            raise


def _sse_chunks(completion_id: str, model: str, final_text: str):
    created = int(time.time())
    delta_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": final_text}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(delta_chunk)}\n\n"
    stop_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop_chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    final_text = await _run_claude(req)
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"

    if req.stream:
        return StreamingResponse(
            _sse_chunks(completion_id, req.model, final_text),
            media_type="text/event-stream",
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": final_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
