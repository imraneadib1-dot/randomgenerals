"""pip 1.0 in a terminal: talk to the engine directly.

    python pip_cli.py                    # a conversation
    python pip_cli.py "who made you?"    # one question, one answer

Needs the engine running (python pip_server.py). The engine keeps the
conversation - this sends one turn at a time with its conversation id -
so /new starts over and /quit leaves.
"""
import json
import os
import sys

import requests

import pip_identity

URL = (os.environ.get("PIP_ENGINE_URL") or "http://127.0.0.1:8765/v1").rstrip("/")


def _headers():
    key = os.environ.get("PIP_ENGINE_KEY", "").strip()
    return {"Authorization": "Bearer " + key} if key else {}


def ask(text, conversation="new", out=sys.stdout):
    """Stream one reply to `out`. -> the conversation id to continue with."""
    body = {"messages": [{"role": "user", "content": text}],
            "conversation_id": conversation, "stream": True}
    with requests.post(URL + "/chat/completions", json=body, stream=True,
                       headers=_headers(), timeout=(6, 600)) as r:
        if r.status_code != 200:
            try:
                reason = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                reason = "HTTP %d" % r.status_code
            out.write("[%s: %s]\n" % (pip_identity.FULL_NAME, reason))
            return conversation
        for raw in r.iter_lines():
            line = raw.decode("utf-8", "replace")
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            piece = (json.loads(line[6:])["choices"][0].get("delta") or {}).get("content")
            if piece:
                out.write(piece)
                out.flush()
        out.write("\n")
        return r.headers.get("X-Pip-Conversation") or conversation


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv:
            ask(" ".join(argv))
            return 0
        print("%s - owned by %s. /new starts over, /quit leaves."
              % (pip_identity.FULL_NAME, pip_identity.OWNER))
        conversation = "new"
        while True:
            try:
                text = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if text in ("/quit", "/exit"):
                return 0
            if text == "/new":
                conversation = "new"
                continue
            if text:
                sys.stdout.write("%s> " % pip_identity.NAME)
                conversation = ask(text, conversation)
    except requests.exceptions.RequestException:
        print("%s is not running at %s. Start it with: python pip_server.py"
              % (pip_identity.FULL_NAME, URL))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
