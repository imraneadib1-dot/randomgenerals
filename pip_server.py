"""pip 1.0 - the engine. Loads the weights and serves them.

    python pip_server.py                    # http://127.0.0.1:8765/v1
    python pip_server.py --base Qwen/Qwen2.5-0.5B-Instruct   # a CPU-sized base
    python pip_server.py --no-adapter       # the base alone, to compare

It speaks the OpenAI chat API, so app.py (through pip_api.py), the CLI
(pip_cli.py) and any OpenAI client all reach it the same way:

    GET    /health
    GET    /v1/models
    POST   /v1/chat/completions             stream or not; tools when not
    GET    /v1/conversations                kept conversations, newest first
    GET    /v1/conversations/<id>
    DELETE /v1/conversations/<id>

WHAT IT ADDS TO THE WEIGHTS

  IDENTITY. pip_identity.SYSTEM leads every prompt, and the reply to an
  owner question is checked before it is sent (pip_identity.enforce) -
  so the answer holds for a caller that sends its own system prompt, or
  none.

  A CONTEXT WINDOW THAT IS RESPECTED. Turns are counted with the real
  tokenizer (counts are cached in pip_store), and the oldest are dropped
  until the request fits. One message too big for the window is 413,
  not a truncated prompt.

  CONVERSATIONS. Send "conversation_id" ("new" to start one) and the
  engine keeps the history: later requests send only the new turn.

  ONE GPU, SAID HONESTLY. One reply is generated at a time. A few more
  may wait; beyond --max-waiting the answer is 429 with Retry-After,
  which a client can act on, rather than a queue nobody can see.

Bound to 127.0.0.1. Requests carrying an Origin header - a web page -
are refused; set PIP_ENGINE_KEY to require a bearer token as well.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import select
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import pip_identity                                        # noqa: E402
from pip_store import Store                                # noqa: E402

PORT = 8765
DEFAULT_CONTEXT = int(os.environ.get("PIP_CONTEXT_TOKENS") or 32768)
DEFAULT_REPLY = 1024
MAX_REPLY = 8192
MESSAGE_OVERHEAD = 4        # role and delimiters, per message
MAX_BODY = 32 * 1024 * 1024  # bytes of request the engine will read


class BadRequest(Exception):
    """The request cannot be answered as sent. -> 400."""


class TooLong(Exception):
    """The newest message alone does not fit the window. -> 413."""


class Busy(Exception):
    """Too many requests are already waiting. -> 429."""

    def __init__(self, retry_after: int):
        super().__init__("busy")
        self.retry_after = retry_after


class ThinkFilter:
    """Removes <think>...</think> from a stream of text pieces.

    A tag can arrive split across two pieces, so a tail that could be the
    start of one is held back until the next piece settles it.
    """
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, inside: bool = False):
        self.inside = inside
        self.buf = ""

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            at = self.buf.find(tag)
            if at >= 0:
                if not self.inside:
                    out.append(self.buf[:at])
                self.buf = self.buf[at + len(tag):]
                self.inside = not self.inside
                continue
            keep = 0
            for n in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-n:]):
                    keep = n
                    break
            cut = len(self.buf) - keep
            if not self.inside:
                out.append(self.buf[:cut])
            self.buf = self.buf[cut:]
            break
        return "".join(out)

    def flush(self) -> str:
        tail = "" if self.inside else self.buf
        self.buf = ""
        return tail


_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def split_tool_calls(text: str):
    """-> (content, tool_calls) from a reply in the base model's own
    tool format: a JSON object inside <tool_call> tags."""
    calls = []
    for m in _TOOL_CALL.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        name = obj.get("name")
        if not name:
            continue
        args = obj.get("arguments", {})
        calls.append({
            "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
            "function": {"name": name, "arguments":
                         args if isinstance(args, str) else json.dumps(args)}})
    if not calls:
        return text, []
    return _TOOL_CALL.sub("", text).strip(), calls


def message_text(message: dict) -> str:
    """A message's text, whether its content is a string or parts."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text") or "" for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def for_template(messages):
    """OpenAI-shaped messages as the chat template wants them: text
    content, and tool-call arguments as objects rather than JSON strings
    (the template serialises them itself; a string would be quoted
    twice)."""
    out = []
    for m in messages:
        row = {"role": m.get("role"), "content": message_text(m)}
        if m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = dict(c.get("function") or {})
                if isinstance(fn.get("arguments"), str):
                    try:
                        fn["arguments"] = json.loads(fn["arguments"] or "{}")
                    except ValueError:
                        fn["arguments"] = {}
                calls.append({"type": "function", "function": fn})
            row["tool_calls"] = calls
        out.append(row)
    return out


class TransformersBackend:
    """The real weights, through transformers."""

    vision = False      # the engine is text-only for now

    def __init__(self, base=None, adapter=None):
        import pip_model
        self.tokenizer, self.model, self.info = pip_model.load(base, adapter)

    def count(self, text: str) -> int:
        return len(self.tokenizer(text, add_special_tokens=False)["input_ids"])

    def generate(self, messages, tools=None, max_tokens=DEFAULT_REPLY,
                 temperature=0.2, top_p=0.95, think=False, stats=None,
                 cancel=None):
        """Yield the visible reply piece by piece. Fills `stats` with
        prompt_tokens, completion_tokens and finish when done."""
        import torch
        from transformers import (StoppingCriteria, StoppingCriteriaList,
                                  TextIteratorStreamer)

        kwargs = {"tokenize": False, "add_generation_prompt": True,
                  "enable_thinking": bool(think)}
        if tools:
            kwargs["tools"] = tools
        prompt = self.tokenizer.apply_chat_template(for_template(messages), **kwargs)
        ids = self.tokenizer(prompt, return_tensors="pt",
                             add_special_tokens=False).to(self.model.device)
        n_prompt = ids["input_ids"].shape[1]

        class Stop(StoppingCriteria):
            def __call__(self, input_ids, scores, **kw):
                return bool(cancel is not None and cancel.is_set())

        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True,
                                        skip_special_tokens=True)
        args = dict(ids, max_new_tokens=int(max_tokens), streamer=streamer,
                    do_sample=temperature > 0,
                    pad_token_id=self.tokenizer.pad_token_id,
                    stopping_criteria=StoppingCriteriaList([Stop()]))
        if temperature > 0:
            args.update(temperature=max(float(temperature), 1e-3),
                        top_p=float(top_p))
        result = {}

        def run():
            try:
                with torch.no_grad():
                    out = self.model.generate(**args)
                result["n"] = int(out.shape[1]) - n_prompt
            except Exception as e:                        # noqa: BLE001
                result["error"] = e
                streamer.end()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        # A thinking template may open the <think> block itself, so the
        # reply begins inside one.
        visible = ThinkFilter(inside=prompt.rstrip().endswith(ThinkFilter.OPEN))
        for piece in streamer:
            text = visible.feed(piece)
            if text:
                yield text
        tail = visible.flush()
        if tail:
            yield tail
        worker.join()
        if "error" in result:
            raise RuntimeError("generation failed: %s" % result["error"])
        if stats is not None:
            n = result.get("n", 0)
            stats.update(prompt_tokens=n_prompt, completion_tokens=n,
                         finish="length" if n >= int(max_tokens) else "stop")


class Engine:
    """Everything between an HTTP request and the weights."""

    def __init__(self, backend, store: Store, context: int = DEFAULT_CONTEXT,
                 max_waiting: int = 4, tools: bool = True):
        self.backend = backend
        # Whether these weights are worth offering tools to. A very
        # small model asked to call a tool invents the call as often as
        # it makes it, and answers worse than one that just talks.
        self.tools = bool(tools)
        self.store = store
        self.context = int(context)
        self.max_waiting = int(max_waiting)
        self._generating = threading.Lock()
        self._count = threading.Lock()
        self._waiting = 0

    def models(self) -> list[dict]:
        return [{"id": pip_identity.MODEL_ID, "object": "model",
                 "owned_by": pip_identity.OWNER,
                 "vision": bool(getattr(self.backend, "vision", False)),
                 "tools": self.tools,
                 "context_window": self.context}]

    def tokens(self, text: str) -> int:
        """Token count of `text`, from the cache when it has been seen."""
        if not text:
            return 0
        n = self.store.cached_tokens(text)
        if n is None:
            n = self.backend.count(text)
            self.store.cache_tokens(text, n)
        return n

    def fit(self, messages, reply_tokens: int):
        """Drop the oldest turns until prompt + reply fits the window.
        The system prompt and the newest message are never dropped."""
        kept = list(messages)
        costs = [self.tokens(message_text(m)) + MESSAGE_OVERHEAD for m in kept]
        while sum(costs) + reply_tokens > self.context and len(kept) > 2:
            at = 1 if kept[0].get("role") == "system" else 0
            kept.pop(at)
            costs.pop(at)
        if sum(costs) + reply_tokens > self.context:
            raise TooLong()
        return kept

    @contextlib.contextmanager
    def slot(self):
        """Hold a place in the queue for the length of one request."""
        with self._count:
            if self._waiting > self.max_waiting:
                raise Busy(retry_after=5 * self._waiting)
            self._waiting += 1
        try:
            yield
        finally:
            with self._count:
                self._waiting -= 1

    def chat(self, body: dict) -> dict:
        """-> {"pieces": iterator of text, "stats": dict filled as it
        runs, "conversation_id": id or None, "tools": bool}."""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise BadRequest('Send {"messages": [...]}.')
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in (
                    "system", "user", "assistant", "tool"):
                raise BadRequest("Every message needs a role of system, "
                                 "user, assistant or tool.")

        cid = body.get("conversation_id")
        if cid:
            cid = self.store.conversation_create(
                None if cid in (True, "new") else str(cid)[:64])
            system = [m for m in messages if m["role"] == "system"][:1]
            fresh = [m for m in messages if m["role"] != "system"]
            history = system + self.store.messages(cid) + fresh
            for m in fresh:
                text = message_text(m)
                self.store.message_add(cid, m["role"], text, self.tokens(text))
        else:
            history = messages

        history = pip_identity.with_identity(history)
        try:
            reply_tokens = int(body.get("max_tokens") or DEFAULT_REPLY)
            temperature = float(body.get("temperature", 0.2))
            top_p = float(body.get("top_p", 0.95))
        except (TypeError, ValueError):
            raise BadRequest("max_tokens, temperature and top_p are numbers.")
        # Never more than half the window: a reply ceiling that leaves no
        # room for the question would refuse every request.
        reply_tokens = max(1, min(reply_tokens, MAX_REPLY, self.context // 2))
        history = self.fit(history, reply_tokens)

        stream = bool(body.get("stream"))
        tools = (body.get("tools") or None) if self.tools and not stream else None
        think = (body.get("reasoning_effort") == "high" or bool(
            (body.get("chat_template_kwargs") or {}).get("enable_thinking")))
        question = pip_identity.last_user_text(history)
        guarded = pip_identity.is_question(question)
        stats: dict = {}
        cancel = threading.Event()

        def pieces():
            said = []
            try:
                with self._generating:
                    source = self.backend.generate(
                        history, tools=tools, max_tokens=reply_tokens,
                        temperature=temperature, top_p=top_p, think=think,
                        stats=stats, cancel=cancel)
                    if guarded:
                        # Held back whole: a sentence cannot be unsent.
                        reply = "".join(source).strip()
                        final = pip_identity.enforce(question, reply)
                        stats["guarded"] = final != reply
                        said.append(final)
                        yield final
                        return
                    started = False
                    for piece in source:
                        if not started:
                            piece = piece.lstrip()
                            if not piece:
                                continue
                            started = True
                        said.append(piece)
                        yield piece
            finally:
                cancel.set()
                if cid and said:
                    self.store.message_add(
                        cid, "assistant", "".join(said),
                        stats.get("completion_tokens") or 0)

        return {"pieces": pieces(), "stats": stats, "conversation_id": cid,
                "tools": bool(tools)}


def make_server(engine: Engine, host: str = "127.0.0.1", port: int = PORT,
                key: str = "", log=None) -> ThreadingHTTPServer:
    """The HTTP front of `engine`. serve_forever() it."""
    log = log or (lambda *a: None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "pip/%s" % pip_identity.VERSION
        sys_version = ""

        def log_message(self, fmt, *a):
            pass

        def _json(self, status, body, headers=None):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status, message, headers=None):
            self._json(status, {"error": {"message": message}}, headers)

        def _refused(self) -> bool:
            if self.headers.get("Origin"):
                self._error(403, "Requests from web pages are refused.")
                return True
            if key and self.headers.get("Authorization") != "Bearer " + key:
                self._error(401, "A valid bearer key is required.")
                return True
            return False

        def _path(self):
            return self.path.split("?", 1)[0].rstrip("/")

        def _gone(self) -> bool:
            """Has the caller hung up? The request has been read in
            full, so a readable socket with nothing in it is a closed
            one."""
            try:
                ready, _, _ = select.select([self.connection], [], [], 0)
                return bool(ready) and self.connection.recv(
                    1, socket.MSG_PEEK) == b""
            except OSError:
                return True

        def do_GET(self):
            if self._refused():
                return
            path = self._path()
            if path == "/health":
                return self._json(200, {
                    "status": "ok", "model": pip_identity.MODEL_ID,
                    "name": pip_identity.FULL_NAME,
                    "owner": pip_identity.OWNER,
                    "waiting": engine._waiting, "store": engine.store.stats()})
            if path == "/v1/models":
                return self._json(200, {"object": "list", "data": engine.models()})
            if path == "/v1/conversations":
                return self._json(200, {"object": "list",
                                        "data": engine.store.conversations()})
            if path.startswith("/v1/conversations/"):
                cid = path.rsplit("/", 1)[1]
                row = engine.store.conversation_get(cid)
                if not row:
                    return self._error(404, "No such conversation.")
                row["messages"] = engine.store.messages(cid)
                return self._json(200, row)
            self._error(404, "Try GET /v1/models or POST /v1/chat/completions.")

        def do_DELETE(self):
            if self._refused():
                return
            path = self._path()
            if path.startswith("/v1/conversations/"):
                gone = engine.store.conversation_delete(path.rsplit("/", 1)[1])
                return self._json(200 if gone else 404, {"deleted": gone})
            self._error(404, "Unknown path.")

        def do_POST(self):
            # The body is read before anything is refused: answering and
            # closing with it still unread resets the connection, and the
            # caller sees a network error where the refusal should be.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.close_connection = True
                return self._error(413, "The request body is too large.")
            raw = self.rfile.read(length)
            if self._refused():
                return
            if self._path() != "/v1/chat/completions":
                return self._error(404, "Unknown path.")
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._error(400, "The body must be a JSON object.")
            try:
                with engine.slot():
                    self._answer(body)
            except Busy as e:
                self._error(429, "%s is answering as many requests as it "
                            "can. Try again shortly." % pip_identity.FULL_NAME,
                            {"Retry-After": str(e.retry_after)})

        def _answer(self, body):
            started = time.time()
            try:
                reply = engine.chat(body)
            except BadRequest as e:
                return self._error(400, str(e))
            except TooLong:
                return self._error(413, "That message is longer than the "
                                   "%d-token context window." % engine.context)
            pieces, stats = reply["pieces"], reply["stats"]
            created = int(started)
            rid = "chatcmpl-" + uuid.uuid4().hex[:24]
            extra = ({"X-Pip-Conversation": reply["conversation_id"]}
                     if reply["conversation_id"] else {})

            # The first piece is produced before any header is sent, so a
            # failure to generate is still a 500 a client can fail over
            # from, and never a 200 that then says nothing.
            try:
                first = next(pieces, None)
            except Exception as e:                        # noqa: BLE001
                log("  failed: %s" % e)
                return self._error(500, "The model could not answer.")

            def usage():
                n_in = stats.get("prompt_tokens") or 0
                n_out = stats.get("completion_tokens") or 0
                return {"prompt_tokens": n_in, "completion_tokens": n_out,
                        "total_tokens": n_in + n_out}

            def done():
                took = max(0.01, time.time() - started)
                n = stats.get("completion_tokens") or 0
                log("  answered: %d tokens in %.1fs (%.1f tok/s)%s" % (
                    n, took, n / took,
                    "  [owner answer enforced]" if stats.get("guarded") else ""))

            if not body.get("stream"):
                parts = [first or ""]
                try:
                    for piece in pieces:
                        parts.append(piece)
                        # Nobody is waiting for a whole reply any more:
                        # stop, or an abandoned request holds the one GPU
                        # and everyone behind it waits for nothing.
                        if self._gone():
                            pieces.close()
                            log("  abandoned by the caller; stopped")
                            return
                except Exception as e:                    # noqa: BLE001
                    log("  failed: %s" % e)
                    return self._error(500, "The model could not answer.")
                text = "".join(parts)
                message = {"role": "assistant", "content": text}
                finish = stats.get("finish") or "stop"
                if reply["tools"]:
                    text, calls = split_tool_calls(text)
                    if calls:
                        message = {"role": "assistant", "content": text,
                                   "tool_calls": calls}
                        finish = "tool_calls"
                out = {"id": rid, "object": "chat.completion",
                       "created": created, "model": pip_identity.MODEL_ID,
                       "choices": [{"index": 0, "message": message,
                                    "finish_reason": finish}],
                       "usage": usage()}
                if reply["conversation_id"]:
                    out["conversation_id"] = reply["conversation_id"]
                done()
                return self._json(200, out, extra)

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()

            def send(delta, finish=None, final=False):
                chunk = {"id": rid, "object": "chat.completion.chunk",
                         "created": created, "model": pip_identity.MODEL_ID,
                         "choices": [{"index": 0, "delta": delta,
                                      "finish_reason": finish}]}
                if final:
                    chunk["usage"] = usage()
                self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8")
                                 + b"\n\n")
                self.wfile.flush()

            try:
                try:
                    send({"role": "assistant", "content": first or ""})
                    for piece in pieces:
                        send({"content": piece})
                    send({}, finish=stats.get("finish") or "stop", final=True)
                except (BrokenPipeError, ConnectionError):
                    return              # the reader left; stop generating
                except Exception as e:                    # noqa: BLE001
                    # Already streaming, so it is said at the end of the
                    # reply rather than raised - the same contract the
                    # app's channels keep.
                    log("  failed mid-reply: %s" % e)
                    send({"content": "\n\n_…the model stopped part-way "
                          "through. The answer above is incomplete._"},
                         finish="stop", final=True)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                done()
            except (BrokenPipeError, ConnectionError):
                pass
            finally:
                pieces.close()

    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    import pip_model

    ap = argparse.ArgumentParser(
        prog="pip_server", description="%s - the engine." % pip_identity.FULL_NAME)
    ap.add_argument("--base", default=None,
                    help="default: the base the adapter was trained on, "
                         "else %s" % pip_model.DEFAULT_BASE)
    ap.add_argument("--adapter", default=pip_model.ADAPTER_DIR,
                    help="a directory written by finetune/train_pip.py")
    ap.add_argument("--no-adapter", action="store_true",
                    help="serve the base without the adapter")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--context", type=int, default=DEFAULT_CONTEXT,
                    help="tokens one request may carry (default %(default)s)")
    ap.add_argument("--max-waiting", type=int, default=4,
                    help="requests allowed to queue before 429")
    ap.add_argument("--db", default=None, help="default: pip.db, or PIP_DB_PATH")
    ap.add_argument("--no-tools", action="store_true",
                    help="do not offer tools to these weights (the default "
                         "for a --smoke adapter)")
    args = ap.parse_args(argv)

    adapter = None if args.no_adapter else args.adapter
    if adapter and not pip_model.has_adapter(adapter):
        print("%s: no adapter at %s - serving the base model alone. The "
              "owner answer is still enforced by the prompt and the guard; "
              "train the adapter with `python -m finetune.train_pip`."
              % (pip_identity.FULL_NAME, adapter))
        adapter = None

    print("%s: loading the weights ..." % pip_identity.FULL_NAME)
    t0 = time.time()
    backend = TransformersBackend(args.base, adapter)
    store = Store(args.db)
    for k, v in dict(backend.info, model=pip_identity.MODEL_ID,
                     owner=pip_identity.OWNER, context=args.context,
                     started_at=int(time.time())).items():
        store.meta_set(k, "" if v is None else v)
    store.cache_prune()
    smoke = bool(adapter and pip_model.record(adapter).get("smoke"))
    engine = Engine(backend, store, context=args.context,
                    max_waiting=args.max_waiting,
                    tools=not (args.no_tools or smoke))
    server = make_server(engine, args.host, args.port,
                         key=os.environ.get("PIP_ENGINE_KEY", "").strip(),
                         log=lambda s: print(s, flush=True))
    print("%s: ready in %.0fs on %s (%s%s). http://%s:%d/v1 - Ctrl+C stops it."
          % (pip_identity.FULL_NAME, time.time() - t0, backend.info["device"],
             "4-bit, " if backend.info["four_bit"] else "",
             "adapter loaded" if adapter else "no adapter",
             args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
