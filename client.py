#!/usr/bin/env python3
"""aisearch client - talk to an aisearch server disguised as web searches.

Subcommands:
  keygen   Print a fresh urlsafe-base64 32-byte key (set as AISEARCH_KEY on
           both the client and the server).
  ask      Encrypt a prompt, POST it to the server's /search endpoint, and
           stream the decrypted completion to stdout. With --raw, instead
           print the disguised search-result frames exactly as received.
  decode   Read disguised frames (from --raw output or a log file) on stdin
           and print the recovered completion text.

Environment:
  AISEARCH_KEY   required for ask/decode; urlsafe-base64 32-byte key
  AISEARCH_URL   required for ask; base URL of the deployed server
                 (e.g. https://your-service.onrender.com)
  OPENAI_MODEL   optional; default model override

Examples:
  export AISEARCH_KEY=$(python client.py keygen)
  export AISEARCH_URL=https://quicksearch.onrender.com
  ./client.py ask "Explain entropy in one paragraph"
  ./client.py ask --system "You are terse" "What is 2+2?"
  echo "summarize quantum tunneling" | ./client.py ask
  ./client.py ask --raw "hello" > search.log
  ./client.py decode < search.log
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from crypto import decrypt, encrypt, generate_key, load_key


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
):
    """Send messages to the disguised server and yield decoded tokens.

    Yields ("token", str) for each content chunk, ("raw", str) for each raw
    frame line (when raw=True), ("error", str) on error, or ("done", None)
    when the stream finishes.
    """
    blob = encrypt(key, json.dumps({"messages": messages, "model": model}).encode("utf-8"))
    url = base_url.rstrip("/") + "/search"
    form_data = {"q": blob}
    if site_password:
        form_data["p"] = site_password

    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            with client.stream("POST", url, data=form_data) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "replace")
                    yield ("error", f"HTTP {resp.status_code}: {body}")
                    return
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if raw:
                        yield ("raw", line)
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if frame.get("error"):
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
                        yield ("done", None)
                        return
    except httpx.HTTPError as e:
        yield ("error", f"network: {e}")


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

    out = sys.stdout
    saw_any = False
    for kind, value in stream_completion(messages, model, base_url, key, raw=args.raw):
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
