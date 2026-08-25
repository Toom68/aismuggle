#!/usr/bin/env python3
"""aisearch client - talk to an aisearch server disguised as web searches.

Uses curl_cffi to impersonate a real Chrome browser's TLS fingerprint (JA3),
sends GET requests (like a real search engine), and includes realistic
browser headers. This defeats DPI that blocks non-browser TLS clients.

Subcommands:
  keygen   Print a fresh urlsafe-base64 32-byte key.
  ask      Encrypt a prompt, GET the server's /search endpoint, and stream
           the decrypted completion to stdout. With --raw, print the disguised
           search-result frames exactly as received.
  decode   Read disguised frames from stdin and print the recovered text.

Environment:
  AISEARCH_KEY   required; urlsafe-base64 32-byte key
  AISEARCH_URL   required; base URL of the deployed server
  OPENAI_MODEL   optional; default model override
  SITE_PASSWORD  optional; password if the server requires one
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from urllib.parse import quote

from curl_cffi import requests as cffi_requests

from crypto import decrypt, encrypt, generate_key, load_key

# Realistic Chrome browser headers. curl_cffi handles the TLS fingerprint
# (JA3/JA4) and HTTP/2 settings automatically when impersonate="chrome".
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


def _jitter(min_s: float = 0.3, max_s: float = 1.2) -> None:
    """Sleep a random amount to mimic human browsing patterns."""
    time.sleep(random.uniform(min_s, max_s))


def cmd_keygen(_: argparse.Namespace) -> int:
    print(generate_key())
    return 0


def stream_completion(
    messages: list[dict],
    model: str,
    base_url: str,
    key: bytes,
    *,
    raw: bool = False,
    site_password: str | None = None,
    verbose: bool = False,
):
    """Send messages to the disguised server and yield decoded tokens.

    Uses GET /search?q=<encrypted> with a Chrome TLS fingerprint and
    realistic browser headers. Yields ("token", str), ("raw", str),
    ("error", str), or ("done", None).
    """
    def vlog(msg: str) -> None:
        if verbose:
            sys.stderr.write(f"\033[2m[debug] {msg}\033[0m\n")
            sys.stderr.flush()

    blob = encrypt(key, json.dumps({"messages": messages, "model": model}).encode("utf-8"))

    # Build a GET URL like a real search engine: /search?q=<blob>&p=<password>
    params = {"q": blob}
    if site_password:
        params["p"] = site_password
    url = base_url.rstrip("/") + "/search"

    headers = dict(_BROWSER_HEADERS)
    headers["Referer"] = base_url.rstrip("/") + "/"

    vlog(f"URL: {url}")
    vlog(f"params: q={blob[:40]}... ({len(blob)} chars), p={'***' if site_password else 'none'}")
    vlog(f"impersonate: chrome")
    vlog(f"headers: {len(headers)} headers set")
    vlog(f"attempting GET request...")
    if is_google:
        vlog(f"google apps script mode: non-streaming, 120s timeout, follows redirects")

    try:
        # curl_cffi impersonates Chrome's TLS fingerprint (JA3/JA4),
        # HTTP/2 settings, and header order — defeating DPI that blocks
        # non-browser clients.
        # Timeout: 15s connect, 120s total (Google Apps Script can take a while).
        # Google Apps Script returns a 302 redirect to googleusercontent.com —
        # curl_cffi follows redirects by default, so this is handled.
        is_google = "script.google.com" in base_url
        timeout = 120 if is_google else 15
        resp = cffi_requests.get(
            url,
            params=params,
            headers=headers,
            impersonate="chrome",
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        )
        vlog(f"response: HTTP {resp.status_code}")
        vlog(f"response headers: {dict(resp.headers)}")
        if resp.status_code != 200:
            body_text = resp.text[:500] if hasattr(resp, 'text') else '(streamed)'
            vlog(f"error body: {body_text}")
            yield ("error", f"HTTP {resp.status_code}: {body_text}")
            return

        vlog(f"streaming response body...")
        buffer = ""
        bytes_received = 0
        for chunk in resp.iter_content(chunk_size=4096):
            if not chunk:
                continue
            bytes_received += len(chunk)
            if isinstance(chunk, bytes):
                buffer += chunk.decode("utf-8", "replace")
            else:
                buffer += chunk

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if raw:
                    yield ("raw", line)
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    vlog(f"unparseable line: {line[:80]}")
                    continue
                if frame.get("error"):
                    vlog(f"server error frame: {frame}")
                    for r in frame.get("results", []):
                        snip = r.get("snippet")
                        if snip:
                            yield ("error", decrypt(key, snip).decode("utf-8", "replace"))
                    return
                for r in frame.get("results", []):
                    snip = r.get("snippet")
                    if snip:
                        yield ("token", decrypt(key, snip).decode("utf-8", "replace"))
                if frame.get("done"):
                    vlog(f"stream complete ({bytes_received} bytes received)")
                    yield ("done", None)
                    return

        vlog(f"stream ended without done frame ({bytes_received} bytes received)")
        vlog(f"remaining buffer: {buffer[:200]}")

    except Exception as e:
        import traceback
        ename = type(e).__name__
        vlog(f"exception type: {ename}")
        vlog(f"exception message: {e}")
        vlog(f"full traceback:\n{traceback.format_exc()}")

        # Give a helpful message for common DPI/blocking errors.
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg:
            yield ("error",
                f"connection timed out — your network is likely blocking this domain.\n"
                f"  Try: set AISEARCH_URL to a Cloudflare Worker proxy URL instead of onrender.com.\n"
                f"  See worker/worker.js for the Cloudflare Worker proxy code.\n"
                f"  Original error: {ename}: {e}")
        elif "refused" in msg or "reset" in msg:
            yield ("error",
                f"connection refused/reset — your network is actively blocking the connection.\n"
                f"  Try: set AISEARCH_URL to a Cloudflare Worker proxy URL.\n"
                f"  Original error: {ename}: {e}")
        else:
            yield ("error", f"network: {ename}: {e}")


def cmd_ask(args: argparse.Namespace) -> int:
    key = load_key()
    base_url = os.environ.get("AISEARCH_URL") or args.url
    if not base_url:
        print("error: AISEARCH_URL is not set (or pass --url)", file=sys.stderr)
        return 2

    if args.prompt:
        user_text = args.prompt
    else:
        user_text = sys.stdin.read()
    if not user_text.strip():
        print("error: no prompt provided (pass an argument or pipe via stdin)", file=sys.stderr)
        return 2

    messages: list[dict] = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": user_text})
    model = args.model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    site_password = os.environ.get("SITE_PASSWORD", "")

    out = sys.stdout
    saw_any = False
    for kind, value in stream_completion(
        messages, model, base_url, key, raw=args.raw, site_password=site_password,
        verbose=getattr(args, "verbose", False),
    ):
        saw_any = True
        if kind == "raw":
            out.write(value + "\n")
            out.flush()
        elif kind == "token":
            out.write(value)
            out.flush()
        elif kind == "error":
            sys.stderr.write(value + "\n")
            return 1
        elif kind == "done":
            if not args.no_newline:
                out.write("\n")
            break

    if not saw_any:
        sys.stderr.write("error: no response frames received from server\n")
        return 1
    return 0


def cmd_decode(args: argparse.Namespace) -> int:
    """Recover completion text from disguised frames on stdin."""
    key = load_key()
    out = sys.stdout
    saw_any = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_any = True
        if frame.get("error"):
            for r in frame.get("results", []):
                snip = r.get("snippet")
                if snip:
                    sys.stderr.write(decrypt(key, snip).decode("utf-8", "replace") + "\n")
            return 1
        for r in frame.get("results", []):
            snip = r.get("snippet")
            if snip:
                out.write(decrypt(key, snip).decode("utf-8", "replace"))
                out.flush()
        if frame.get("done"):
            if not args.no_newline:
                out.write("\n")
            break
    if not saw_any:
        sys.stderr.write("error: no disguised frames found on stdin\n")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aisearch-client",
        description="Talk to an aisearch server disguised as web searches.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    k = sub.add_parser("keygen", help="Print a fresh shared key.")
    k.set_defaults(func=cmd_keygen)

    a = sub.add_parser("ask", help="Send a prompt and stream the decrypted completion.")
    a.add_argument("prompt", nargs="?", help="Prompt text. If omitted, read from stdin.")
    a.add_argument("--system", help="Optional system message.")
    a.add_argument("--model", help="Model name (default: gpt-4o-mini).")
    a.add_argument("--url", help="Server base URL (defaults to $AISEARCH_URL).")
    a.add_argument("--raw", action="store_true",
                   help="Print the disguised search-result frames instead of decoding.")
    a.add_argument("--no-newline", action="store_true", help="Do not append a trailing newline.")
    a.add_argument("-v", "--verbose", action="store_true",
                   help="Print debug info to stderr (request details, response status, errors).")
    a.set_defaults(func=cmd_ask)

    d = sub.add_parser("decode", help="Recover completion text from disguised frames on stdin.")
    d.add_argument("--no-newline", action="store_true", help="Do not append a trailing newline.")
    d.set_defaults(func=cmd_decode)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
