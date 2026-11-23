#!/usr/bin/env python3
"""mistral-vibe-relay - OpenAI-compatible relay in front of api.mistral.ai.

Two things it does that plain api.mistral.ai does not:

1. It signs requests so the account's Vibe Code monthly budget pays for them
   instead of the regular API budget. The marker is a `metadata` object in the
   request body; Mistral's own Vibe CLI sends the same one.
2. It translates Mistral's block-shaped answers back to plain OpenAI shape:
   `content: [{"type": "thinking", ...}, {"type": "text", ...}]` becomes
   `content: "..."` plus `reasoning_content: "..."`, and any reasoning the
   client replays in the assistant history is stripped, because Mistral answers
   `422 extra_forbidden` on `reasoning_content` inside a message.

Stdlib only. Python 3.11+.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

VERSION = "1.0.0"
UPSTREAM = os.environ.get("RELAY_UPSTREAM", "https://api.mistral.ai").rstrip("/")
HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "18999"))
NORMALIZE = os.environ.get("RELAY_NORMALIZE", "1") not in ("0", "false", "no")
SIGN = os.environ.get("RELAY_SIGN", "1") not in ("0", "false", "no")
CLIENT_AUTH = os.environ.get("RELAY_CLIENT_KEY", "")  # optional: require this bearer from clients
UPSTREAM_KEY_ENV = os.environ.get("RELAY_UPSTREAM_KEY_ENV", "MISTRAL_API_KEY")

# The marker Mistral's Vibe CLI sends. `call_source` is the field that decides
# which wallet is billed; the rest is context Mistral's telemetry reports.
VIBE_METADATA = {
    "call_source": "vibe_code",
    "call_type": "main_call",
    "agent_version": "3.1.4",
    "client_name": "vibe_programmatic",
    "host_kind": "local",
}

HOP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "accept-encoding", "authorization", "x-api-key", "expect",
}


def log(event: str, **fields: Any) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    record.update(fields)
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- body shaping


def _blocks_to_parts(blocks: Any) -> tuple[str, str]:
    """Split Mistral content blocks into (visible text, reasoning text)."""
    if isinstance(blocks, str):
        return blocks, ""
    if not isinstance(blocks, list):
        return "", ""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            value = block.get("text")
            if isinstance(value, str):
                text_parts.append(value)
        elif kind == "thinking":
            for inner in block.get("thinking") or []:
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    reasoning_parts.append(inner["text"])
    return "".join(text_parts), "".join(reasoning_parts)


def sanitize_history(messages: list[dict]) -> tuple[list[dict], int]:
    """Drop reasoning fields and collapse block content before sending upstream.

    Mistral rejects `reasoning_content`/`reasoning` on an assistant message with
    `422 extra_forbidden`, and it will not accept its own thinking blocks back in
    some shapes. Keeping only the visible text is always safe.
    """
    cleaned: list[dict] = []
    touched = 0
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        out = dict(message)
        for field in ("reasoning_content", "reasoning", "thinking"):
            if field in out:
                out.pop(field, None)
                touched += 1
        if out.get("content") is not None and not isinstance(out["content"], str):
            text, _ = _blocks_to_parts(out["content"])
            out["content"] = text
            touched += 1
        cleaned.append(out)
    return cleaned, touched


def normalize_message(message: dict) -> dict:
    """Mistral message -> OpenAI message (block content split into text/reasoning)."""
    out = dict(message)
    content = out.get("content")
    if isinstance(content, list):
        text, reasoning = _blocks_to_parts(content)
        out["content"] = text
        if reasoning:
            out["reasoning_content"] = reasoning
    return out


def normalize_response(payload: dict) -> dict:
    for choice in payload.get("choices") or []:
        message = choice.get("message")
        if isinstance(message, dict):
            choice["message"] = normalize_message(message)
    return payload


def normalize_chunk(chunk: dict) -> dict:
    """Same split for a streaming delta; thinking blocks ride as reasoning_content."""
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), list):
            text, reasoning = _blocks_to_parts(delta["content"])
            delta["content"] = text
            if reasoning:
                delta["reasoning_content"] = delta.get("reasoning_content", "") + reasoning
    return chunk


def usage_summary(payload: dict) -> dict | None:
    """Token usage for the log, including the prompt-cache hit count."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt": usage.get("prompt_tokens"),
        "completion": usage.get("completion_tokens"),
        "cached": details.get("cached_tokens") if isinstance(details, dict) else None,
    }


def shape_request(payload: dict) -> tuple[dict, dict]:
    """Add the billing metadata, clean the history, report what changed."""
    changes: dict[str, Any] = {}
    messages = payload.get("messages")
    if isinstance(messages, list):
        cleaned, touched = sanitize_history(messages)
        if touched:
            payload["messages"] = cleaned
            changes["history_fields_stripped"] = touched
    if SIGN:
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        merged = dict(VIBE_METADATA)
        merged.update({k: v for k, v in metadata.items() if k not in merged})
        if metadata != merged:
            payload["metadata"] = merged
            changes["metadata"] = "vibe_code"
    return payload, changes


# ------------------------------------------------------------------- transport


def upstream_headers() -> dict[str, str]:
    key = os.environ.get(UPSTREAM_KEY_ENV, "")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "mistral-vibe-relay/" + VERSION,
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def parse_sse_line(line: bytes) -> dict | None:
    text = line.decode("utf-8", "replace").strip()
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if payload == "[DONE]" or not payload:
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mistral-vibe-relay/" + VERSION

    # -- helpers ---------------------------------------------------------
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not CLIENT_AUTH:
            return True
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return token == CLIENT_AUTH

    def _forward(self, method: str) -> None:
        if not self._authorized():
            self._json(401, {"error": {"message": "invalid relay key", "type": "unauthorized"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        payload: dict | None = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None

        changes: dict[str, Any] = {}
        is_chat = self.path.rstrip("/").endswith("/chat/completions")
        if payload is not None and is_chat:
            payload, changes = shape_request(payload)
            raw = json.dumps(payload, ensure_ascii=False).encode()

        request = urllib.request.Request(
            UPSTREAM + self.path, data=raw if raw else None, method=method,
            headers=upstream_headers(),
        )
        try:
            response = urllib.request.urlopen(request, timeout=900)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            log("upstream_error", path=self.path, status=exc.code, changes=changes)
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:  # noqa: BLE001 - surface anything to the client
            log("upstream_unreachable", path=self.path, error=f"{type(exc).__name__}: {exc}")
            self._json(502, {"error": {"message": f"upstream unreachable: {exc}", "type": "relay_error"}})
            return

        content_type = response.headers.get("Content-Type", "")
        stream = "text/event-stream" in content_type

        if not stream:
            body = response.read()
            usage = None
            if is_chat and payload is not None:
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    usage = usage_summary(parsed)
                    if NORMALIZE:
                        body = json.dumps(normalize_response(parsed), ensure_ascii=False).encode()
            log("proxied", path=self.path, status=response.status, stream=False,
                changes=changes, usage=usage)
            self.send_response(response.status)
            self.send_header("Content-Type", content_type or "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # SSE: pass through, optionally rewriting each data frame.
        self.send_response(response.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        usage = None
        for line in response:
            out = line
            chunk = parse_sse_line(line)
            if chunk is not None:
                if chunk.get("usage"):
                    usage = usage_summary(chunk)
                if NORMALIZE:
                    out = b"data: " + json.dumps(normalize_chunk(chunk), ensure_ascii=False).encode() + b"\n\n"
            try:
                self.wfile.write(out)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log("client_disconnected", path=self.path)
                break
        log("proxied", path=self.path, status=response.status, stream=True,
            changes=changes, usage=usage)

    # -- routes ----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        self._forward("POST")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/healthz":
            key_present = bool(os.environ.get(UPSTREAM_KEY_ENV))
            self._json(200, {
                "status": "ok", "version": VERSION, "upstream": UPSTREAM,
                "sign": SIGN, "normalize": NORMALIZE, "upstream_key": key_present,
            })
            return
        self._forward("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._forward("PUT")

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default access log
        return


def main() -> None:
    missing = not os.environ.get(UPSTREAM_KEY_ENV)
    if missing:
        log("warning", message=f"{UPSTREAM_KEY_ENV} is empty; upstream calls will 401")
    log("started", host=HOST, port=PORT, upstream=UPSTREAM, sign=SIGN, normalize=NORMALIZE)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
