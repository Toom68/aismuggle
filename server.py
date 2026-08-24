"""aisearch server - a search-engine-disguised OpenAI streaming proxy.

Deploy on Render (or anywhere). The service looks like a generic web-search
site to an outside observer:

  GET  /          -> HTML search homepage (the frontend SPA)
  GET  /search    -> same homepage (so the endpoint exists for GET too)
  POST /search    -> "search results" stream. The `q` form field carries either:
                     - an AES-GCM-encrypted JSON blob (CLI client), or
                     - a plaintext prompt string (browser frontend).
                     The server auto-detects which and responds in kind:
                     encrypted snippets for CLI, plaintext snippets for browser.
                     Both modes stream newline-delimited JSON "search result pages":
                     {"results":[{"title":..,"url":..,"snippet":"<token>"}],"page":N,"done":false}

Environment:
  AISEARCH_KEY     required; urlsafe-base64 32-byte key shared with CLI clients
  OPENAI_API_KEY   required; OpenAI API key
  OPENAI_BASE_URL  optional; defaults to https://api.openai.com/v1
  OPENAI_MODEL     optional; default model, defaults to gpt-4o-mini
  SITE_PASSWORD    optional; if set, browser requests must include this password
                   in the X-Site-Password header (or as a form field `p`)
  PORT             optional; bind port, defaults to 8000
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from crypto import decrypt, encrypt, load_key

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

app = FastAPI(title="QuickSearch", docs_url=None, redoc_url=None, openapi_url=None)

_KEY = load_key()
_BASE_URL = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
_DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
_SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")
_STATIC_DIR = Path(__file__).parent / "static"

# Decoy search-result content so frames look realistic.
_DECOY_TITLES = [
    "Best results for your query",
    "Top 10 answers ranked by relevance",
    "Quick answer - QuickSearch",
    "Related results - page {n}",
    "People also ask",
    "Featured snippet",
    "Knowledge panel summary",
    "Web results for your search",
]
_DECOY_HOSTS = [
    "encyclopedia.example.com",
    "answers.example.org",
    "wiki.example.net",
    "forum.example.com",
    "news.example.com",
    "docs.example.io",
]


def _decoy_title(n: int) -> str:
    t = random.choice(_DECOY_TITLES)
    return t.format(n=n) if "{n}" in t else t


def _decoy_url(n: int) -> str:
    return f"https://{random.choice(_DECOY_HOSTS)}/page/{n}"


def _read_index() -> str:
    return (_STATIC_DIR / "index.html").read_text("utf-8")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _read_index()


@app.get("/search", response_class=HTMLResponse)
async def search_get() -> str:
    return _read_index()


def _check_password(request: Request, form_p: str | None) -> str | None:
    """Return an error message if the password check fails, else None."""
    if not _SITE_PASSWORD:
        return None
    header_val = request.headers.get("x-site-password", "")
    if form_p == _SITE_PASSWORD or header_val == _SITE_PASSWORD:
        return None
    return "unauthorized"


@app.post("/search")
async def search_post(request: Request, q: str = Form(...), p: str = Form(default="")):
    """Disguised chat-completion endpoint.

    Auto-detects encrypted (CLI) vs plaintext (browser) mode by attempting
    AES-GCM decryption; if that fails, treats `q` as a plaintext prompt.

    Response is a stream of newline-delimited JSON "search result pages":
      {"results":[{"title":..,"url":..,"snippet":"<token>"}],"page":N,"done":false}
      {"results":[],"page":N,"done":true,"usage":{...}}

    In encrypted mode, snippet = AES-GCM(base64) token.
    In plaintext mode, snippet = raw token string.
    """
    # Password check (for browser mode).
    pw_err = _check_password(request, p if p else None)
    if pw_err:
        async def unauth():
            yield json.dumps({"results": [], "page": 0, "done": True, "error": pw_err}) + "\n"
        return StreamingResponse(unauth(), media_type="application/x-ndjson")

    # Detect encrypted vs plaintext mode.
    encrypted = False
    messages: list[dict] = []
    model = _DEFAULT_MODEL
    try:
        blob = decrypt(_KEY, q)
        req = json.loads(blob)
        messages = req.get("messages") or []
        model = req.get("model") or _DEFAULT_MODEL
        encrypted = True
    except Exception:
        # Plaintext mode: q is the user's prompt directly.
        messages = [{"role": "user", "content": q}]

    api_key = os.environ.get("OPENAI_API_KEY", "")

    def wrap_token(token: str) -> str:
        if encrypted:
            return encrypt(_KEY, token.encode("utf-8"))
        return token

    def wrap_error(msg: str) -> str:
        if encrypted:
            return encrypt(_KEY, msg.encode("utf-8"))
        return msg

    async def generate():
        if not api_key:
            yield json.dumps({"results": [], "page": 0, "done": True, "error": "server misconfigured"}) + "\n"
            return

        url = _BASE_URL.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {"model": model, "messages": messages, "stream": True}

        page = 1
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        msg = f"[error] HTTP {resp.status_code}: {body.decode('utf-8', 'replace')}"
                        yield json.dumps(
                            {"results": [{"title": "Error", "url": _decoy_url(0), "snippet": wrap_error(msg)}],
                             "page": 0, "done": True, "error": True, "status": resp.status_code}
                        ) + "\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            evt = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = evt.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta", {}) or {}
                            token = delta.get("content")
                            if token:
                                yield json.dumps({
                                    "results": [
                                        {"title": _decoy_title(page), "url": _decoy_url(page), "snippet": wrap_token(token)}
                                    ],
                                    "page": page, "done": False,
                                }) + "\n"
                                page += 1
                        if evt.get("usage"):
                            yield json.dumps({"results": [], "page": page, "done": True, "usage": evt["usage"]}) + "\n"
                            return
        except httpx.HTTPError as e:
            yield json.dumps(
                {"results": [{"title": "Error", "url": _decoy_url(0), "snippet": wrap_error(f"[error] network: {e}")}],
                 "page": 0, "done": True, "error": True}
            ) + "\n"
            return

        yield json.dumps({"results": [], "page": page, "done": True}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
