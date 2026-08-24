#!/usr/bin/env python3
"""aismuggle - interactive terminal chat over a disguised search proxy.

Usage:
  aismuggle run [options]          Start an interactive chat session.
  aismuggle ask [options] <prompt>  One-shot prompt (delegates to client.py).
  aismuggle keygen                  Generate a shared key.
  aismuggle decode [options]        Decode disguised frames from stdin.

`run` starts a REPL-style chat. Type a message and press Enter to send it;
the response streams in token-by-token. Conversation history is maintained
across turns. Slash commands:

  /system <text>   Set or update the system prompt.
  /model <name>    Switch models (e.g. gpt-4o, gpt-4o-mini).
  /clear           Clear conversation history.
  /history         Show the current message history.
  /raw             Toggle raw mode (print disguised frames instead of text).
  /help            Show available commands.
  /quit            Exit (also Ctrl+D or Ctrl+C).

Environment:
  AISEARCH_KEY   required; urlsafe-base64 32-byte key (shared with server)
  AISEARCH_URL   required; base URL of the deployed server
  OPENAI_MODEL   optional; default model override
"""

from __future__ import annotations

import argparse
import os
import sys

from client import stream_completion
from crypto import generate_key, load_key

# ANSI colors (disabled if not a TTY or NO_COLOR is set).
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return c(text, "2")


def bold(text: str) -> str:
    return c(text, "1")


def blue(text: str) -> str:
    return c(text, "34")


def green(text: str) -> str:
    return c(text, "32")


def red(text: str) -> str:
    return c(text, "31")


def yellow(text: str) -> str:
    return c(text, "33")


BANNER = r"""
    _              _              _
   / \   _ __ ___ | |_ _ __ _   _| | _____
  / _ \ | '_ ` _ \| __| '__| | | | |/ / _ \
 / ___ \| | | | | | |_| |  | |_| |   <  __/
/_/   \_\_| |_| |_|\__|_|   \__,_|_|\_\___|

  Disguised AI chat · type /help for commands · /quit to exit
"""


HELP_TEXT = """\
Commands:
  /system <text>   Set or update the system prompt
  /model <name>    Switch models (e.g. gpt-4o, gpt-4o-mini)
  /clear           Clear conversation history
  /history         Show current message history
  /raw             Toggle raw mode (print disguised frames)
  /help            Show this help
  /quit            Exit (Ctrl+D / Ctrl+C also work)

Just type a message and press Enter to chat.\
"""


def cmd_run(args: argparse.Namespace) -> int:
    key = load_key()
    base_url = os.environ.get("AISEARCH_URL") or args.url
    if not base_url:
        print(red("error: AISEARCH_URL is not set (or pass --url)"), file=sys.stderr)
        return 2

    model = args.model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    system_prompt: str | None = args.system
    messages: list[dict] = []
    raw_mode = False

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    # readline support for up/down arrow history (if available).
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    print(blue(BANNER))
    print(dim(f"  Server: {base_url}"))
    print(dim(f"  Model:  {model}"))
    if system_prompt:
        print(dim(f"  System: {system_prompt}"))
    print()

    while True:
        try:
            prompt_str = blue("aismuggle") + dim("> ")
            line = input(prompt_str)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        text = line.strip()
        if not text:
            continue

        # --- Slash commands ---
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit", "/q"):
                break
            elif cmd == "/help":
                print(HELP_TEXT)
                continue
            elif cmd == "/system":
                system_prompt = arg or None
                # Rebuild messages with new system prompt.
                messages = [m for m in messages if m["role"] != "system"]
                if system_prompt:
                    messages.insert(0, {"role": "system", "content": system_prompt})
                print(green(f"  System prompt set: {system_prompt}"))
                continue
            elif cmd == "/model":
                if not arg:
                    print(yellow(f"  Current model: {model}"))
                else:
                    model = arg
                    print(green(f"  Model set to: {model}"))
                continue
            elif cmd == "/clear":
                messages = [m for m in messages if m["role"] == "system"]
                print(green("  Conversation cleared."))
                continue
            elif cmd == "/history":
                if not messages:
                    print(dim("  (empty)"))
                for m in messages:
                    role = m["role"]
                    content = m["content"]
                    if len(content) > 80:
                        content = content[:77] + "..."
                    label = {"system": "SYS", "user": "YOU", "assistant": "AI"}.get(role, role)
                    print(f"  {dim(label):>4}  {content}")
                continue
            elif cmd == "/raw":
                raw_mode = not raw_mode
                state = "ON" if raw_mode else "OFF"
                print(green(f"  Raw mode: {state}"))
                continue
            else:
                print(red(f"  Unknown command: {cmd}. Type /help for available commands."))
                continue

        # --- Send a message ---
        messages.append({"role": "user", "content": text})

        # Stream the response.
        full_response = ""
        started = False
        error_occurred = False

        for kind, value in stream_completion(messages, model, base_url, key, raw=raw_mode):
            if kind == "token":
                if not started:
                    started = True
                    # Print a small prefix before the first token.
                    sys.stdout.write(dim("  ") )
                    sys.stdout.flush()
                sys.stdout.write(value)
                sys.stdout.flush()
                full_response += value
            elif kind == "raw":
                sys.stdout.write(value + "\n")
                sys.stdout.flush()
            elif kind == "error":
                sys.stderr.write(red(f"  error: {value}") + "\n")
                error_occurred = True
            elif kind == "done":
                break

        if started and not raw_mode:
            print()  # newline after the streamed response

        if error_occurred:
            # Remove the failed user message so it can be retried.
            messages.pop()
        elif full_response and not raw_mode:
            messages.append({"role": "assistant", "content": full_response})

    return 0


def cmd_keygen(_: argparse.Namespace) -> int:
    print(generate_key())
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Delegate to client.py's ask command."""
    from client import cmd_ask as _cmd_ask
    return _cmd_ask(args)


def cmd_decode(args: argparse.Namespace) -> int:
    """Delegate to client.py's decode command."""
    from client import cmd_decode as _cmd_decode
    return _cmd_decode(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aismuggle",
        description="Disguised AI chat in the terminal.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Start an interactive chat session.")
    r.add_argument("--system", help="Initial system prompt.")
    r.add_argument("--model", help="Model name (default: gpt-4o-mini).")
    r.add_argument("--url", help="Server base URL (defaults to $AISEARCH_URL).")
    r.set_defaults(func=cmd_run)

    k = sub.add_parser("keygen", help="Print a fresh shared key.")
    k.set_defaults(func=cmd_keygen)

    a = sub.add_parser("ask", help="One-shot prompt (stream the decrypted completion).")
    a.add_argument("prompt", nargs="?", help="Prompt text. If omitted, read from stdin.")
    a.add_argument("--system", help="Optional system message.")
    a.add_argument("--model", help="Model name (default: gpt-4o-mini).")
    a.add_argument("--url", help="Server base URL (defaults to $AISEARCH_URL).")
    a.add_argument("--raw", action="store_true", help="Print disguised frames instead of decoding.")
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
