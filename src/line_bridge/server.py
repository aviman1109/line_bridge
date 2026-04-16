"""line-bridge server — LINE Messaging API ↔ claude-runner /chat bridge + MCP tools."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from line_bridge.otel import setup_otel

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("line_bridge.server")

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_DEFAULT_USER = os.environ.get("LINE_DEFAULT_USER_ID", "")
RUNNER_URL = os.environ.get("CLAUDE_RUNNER_URL", "http://host.docker.internal:38095").rstrip("/")
DEFAULT_WORKSPACE = os.environ.get("DEFAULT_WORKSPACE", "training")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "sonnet")
USER_IDS_FILE = Path(os.environ.get("USER_IDS_FILE", "/data/user_ids.jsonl"))
USER_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)

LINE_API = "https://api.line.me/v2/bot"

# ── Line API helpers ──────────────────────────────────────────────────────

async def _line_post(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as cli:
        resp = await cli.post(
            f"{LINE_API}{path}",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code >= 300:
        log.error("Line API %s → %d: %s", path, resp.status_code, resp.text[:300])
    return {"ok": resp.status_code < 300, "status": resp.status_code, "body": resp.text[:500]}


async def _line_push(user_id: str, text: str) -> dict:
    return await _line_post("/message/push", {
        "to": user_id,
        "messages": [{"type": "text", "text": text}],
    })


async def _line_reply(reply_token: str, text: str) -> dict:
    return await _line_post("/message/reply", {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    })


async def _line_loading(user_id: str, seconds: int = 60) -> None:
    """Show a typing indicator to the user for up to 60 s while we wait on Claude."""
    try:
        await _line_post("/chat/loading/start", {"chatId": user_id, "loadingSeconds": min(60, seconds)})
    except Exception as exc:
        log.warning("loading indicator failed: %s", exc)


# ── MCP ───────────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="Line Bridge",
    instructions=(
        "LINE Messaging API bridge.\n"
        "- notify(text): push to the default user\n"
        "- push_message(user_id, text): push to any user\n"
        "- reply_message(reply_token, text): reply within a webhook window"
    ),
)


@mcp.tool(description="Push a text message to the default LINE user (Casper).")
async def notify(text: str) -> dict:
    if not LINE_DEFAULT_USER:
        return {"ok": False, "error": "LINE_DEFAULT_USER_ID not set"}
    return await _line_push(LINE_DEFAULT_USER, text)


@mcp.tool(description="Push a text message to any LINE user id.")
async def push_message(user_id: str, text: str) -> dict:
    return await _line_push(user_id, text)


@mcp.tool(description="Reply to an incoming webhook event using the supplied reply_token (valid for ~30s after the event).")
async def reply_message(reply_token: str, text: str) -> dict:
    return await _line_reply(reply_token, text)


# ── Webhook ───────────────────────────────────────────────────────────────

def _verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_SECRET or not signature:
        return False
    mac = hmac.new(LINE_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


def _record_user(user_id: str, display_name: str = "") -> None:
    if not user_id:
        return
    rec = {"user_id": user_id, "display_name": display_name, "first_seen_ts": int(time.time())}
    with USER_IDS_FILE.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.warning("NEW_LINE_USER_ID seen: %s (name=%r) — save to 1P op://Casper MCP/Line Bot/user_id", user_id, display_name)


async def _known_user(user_id: str) -> bool:
    if not USER_IDS_FILE.exists():
        return False
    try:
        with USER_IDS_FILE.open() as f:
            for line in f:
                if json.loads(line).get("user_id") == user_id:
                    return True
    except Exception:
        pass
    return False


async def _fetch_line_profile(user_id: str) -> dict:
    async with httpx.AsyncClient(timeout=5) as cli:
        resp = await cli.get(f"{LINE_API}/profile/{user_id}",
                             headers={"Authorization": f"Bearer {LINE_TOKEN}"})
    return resp.json() if resp.status_code < 300 else {}


async def _handle_message_event(event: dict) -> None:
    source = event.get("source", {})
    user_id = source.get("userId", "")
    reply_token = event.get("replyToken", "")
    text = event.get("message", {}).get("text", "").strip()

    if not text or not user_id:
        return

    if not await _known_user(user_id):
        profile = await _fetch_line_profile(user_id)
        _record_user(user_id, profile.get("displayName", ""))

    # Show typing animation immediately so the user knows we're working.
    await _line_loading(user_id, seconds=60)

    try:
        async with httpx.AsyncClient(timeout=120) as cli:
            r = await cli.post(f"{RUNNER_URL}/chat", json={
                "user_id": user_id,
                "message": text,
                "workspace": DEFAULT_WORKSPACE,
                "model": DEFAULT_MODEL,
            })
        if r.status_code >= 300:
            await _line_reply(reply_token, f"⚠️ runner {r.status_code}: {r.text[:200]}")
            return
        reply_text = r.json().get("reply", "").strip() or "（無回覆）"
    except Exception as exc:
        log.exception("runner call failed")
        await _line_reply(reply_token, f"⚠️ runner error: {exc}")
        return

    # Line message size limit is 5000 chars — truncate defensively
    if len(reply_text) > 4900:
        reply_text = reply_text[:4900] + "…（截斷）"

    await _line_reply(reply_token, reply_text)


async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Line-Signature", "")
    if not _verify_signature(body, sig):
        log.warning("webhook signature verification failed")
        return PlainTextResponse("bad signature", status_code=401)

    try:
        payload = json.loads(body)
    except Exception:
        return PlainTextResponse("bad json", status_code=400)

    for ev in payload.get("events", []):
        et = ev.get("type")
        if et == "message" and ev.get("message", {}).get("type") == "text":
            asyncio.create_task(_handle_message_event(ev))
        elif et == "follow":
            # New follower — record userId + greet
            uid = ev.get("source", {}).get("userId", "")
            if uid:
                profile = await _fetch_line_profile(uid)
                _record_user(uid, profile.get("displayName", ""))
                await _line_push(uid, "Caddie 上線。直接傳訊息即可開始對話。")
        # unfollow, join, leave etc. — ignore for now

    return JSONResponse({"ok": True})


async def health(_: Request):
    return JSONResponse({
        "status": "ok",
        "line_token_set": bool(LINE_TOKEN),
        "runner_url": RUNNER_URL,
        "default_user": LINE_DEFAULT_USER[:4] + "…" if LINE_DEFAULT_USER else "",
    })


async def push(request: Request) -> JSONResponse:
    """Simple HTTP push endpoint for internal callers (no MCP handshake needed).

    POST /push {user_id?: str, text: str} — omit user_id to use the default.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text is required"}, status_code=400)
    user_id = body.get("user_id") or LINE_DEFAULT_USER
    if not user_id:
        return JSONResponse({"ok": False, "error": "no user_id and no default configured"}, status_code=400)
    result = await _line_push(user_id, text)
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


# ── Entry ─────────────────────────────────────────────────────────────────

def build_app() -> Starlette:
    # Mount MCP at root so its built-in /mcp route is reachable without a
    # 307 redirect (Mount("/mcp", ...) appends trailing slash and breaks
    # Claude Code's MCP client). Webhook + health win first via explicit
    # routes; everything else falls into the FastMCP app.
    mcp_http = mcp.streamable_http_app()
    return Starlette(
        lifespan=getattr(mcp_http.router, "lifespan_context", None),
        routes=[
            Route("/webhook", webhook, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/push", push, methods=["POST"]),
            Mount("", app=mcp_http),
        ],
    )


def main() -> None:
    setup_otel()
    if not LINE_TOKEN:
        log.warning("LINE_CHANNEL_ACCESS_TOKEN is empty — push/reply calls will fail")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "38106"))
    log.info("line-bridge starting on %s:%d (runner=%s, ws=%s, model=%s)",
             host, port, RUNNER_URL, DEFAULT_WORKSPACE, DEFAULT_MODEL)
    uvicorn.run(build_app(), host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
