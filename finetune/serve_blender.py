"""Serve the Blender model trained here, for the pop-up to ask.

    python -m finetune.serve_blender               # http://127.0.0.1:8129/v1
    python -m finetune.serve_blender --base-only   # the untrained base, to compare

While this runs, the Blender pop-up lists "Ours - trained here" in its
model menu (blender_controller.py: --llm ours). It speaks the OpenAI chat
API - POST /v1/chat/completions, GET /v1/models - the same as every other
model the pop-up uses, so nothing downstream knows it is different.

WHAT IT LOADS

The base model named in the adapter's .json (Qwen2.5-Coder-0.5B unless
trained otherwise) and finetune/adapter-blender.pt from
`python -m finetune.train_lora --task blender`. The adapter is merged
into the weights at load (finetune/lora.py merge), so answering costs
what the plain base model costs - nothing extra per token.

ON A CPU

One request at a time: generation already uses every core, and two at
once would each take twice as long. A 1,500-token script is a minute or
two here. Bound to 127.0.0.1, and requests carrying an Origin header -
a web page - are refused, as the Blender bridge does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
ADAPTER = os.path.join(HERE, "adapter-blender.pt")
PORT = 8129
MODEL_ID = "randomgenerals-blender"
DEFAULT_BASE = "Qwen/Qwen2.5-Coder-0.5B-Instruct"


def load(adapter, base=None):
    """-> (tokenizer, model, meta). meta is the adapter's training record."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from finetune import lora

    meta = {}
    if adapter:
        side = os.path.splitext(adapter)[0] + ".json"
        if os.path.exists(side):
            with open(side, encoding="utf-8") as fh:
                meta = json.load(fh)
    base = base or meta.get("base") or DEFAULT_BASE
    tokenizer = AutoTokenizer.from_pretrained(base)
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float32)
    if adapter:
        lora.attach(model, r=meta.get("rank", 8), alpha=meta.get("alpha", 16), dropout=0.0)
        lora.load(model, adapter)
        lora.merge(model)
    model.eval()
    return tokenizer, model, dict(meta, base=base, adapter=os.path.basename(adapter) if adapter else None)


def complete(tokenizer, model, messages, max_new_tokens=3000, temperature=0.2):
    """-> (text, prompt tokens, completion tokens, finish reason)."""
    import torch
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    ids = tokenizer(prompt, return_tensors="pt")
    n_prompt = ids["input_ids"].shape[1]
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new_tokens,
                             do_sample=temperature > 0, temperature=max(temperature, 1e-3),
                             top_p=0.95, pad_token_id=tokenizer.eos_token_id)
    new = out[0][n_prompt:]
    text = tokenizer.decode(new, skip_special_tokens=True)
    finish = "length" if len(new) >= max_new_tokens else "stop"
    return text, n_prompt, len(new), finish


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--adapter", default=ADAPTER)
    ap.add_argument("--base", default=None, help="default: the one the adapter was trained on")
    ap.add_argument("--base-only", action="store_true", help="serve the base without the adapter")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)

    adapter = None if args.base_only else args.adapter
    if adapter and not os.path.exists(adapter):
        print("No adapter at %s. Train one first: python -m finetune.train_lora --task blender\n"
              "or serve the untrained base with --base-only." % adapter)
        return 2
    print("loading %s%s ..." % (args.base or "the base model", " + " + adapter if adapter else ""))
    t0 = time.time()
    tokenizer, model, meta = load(adapter, args.base)
    print("  ready in %.0fs: %s" % (time.time() - t0, json.dumps(meta)))
    busy = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "pipBlenderModel/1"

        def log_message(self, fmt, *a):
            pass

        def _send(self, status, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _refused(self):
            if self.headers.get("Origin"):
                self._send(403, {"error": {"message": "Requests from web pages are refused."}})
                return True
            return False

        def do_GET(self):
            if self._refused():
                return
            if self.path.rstrip("/").endswith("/models"):
                return self._send(200, {"object": "list", "data": [
                    {"id": MODEL_ID, "object": "model", "owned_by": "trained-here", "meta": meta}]})
            self._send(404, {"error": {"message": "Try GET /v1/models or POST /v1/chat/completions."}})

        def do_POST(self):
            if self._refused():
                return
            if not self.path.rstrip("/").endswith("/chat/completions"):
                return self._send(404, {"error": {"message": "Unknown path."}})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                messages = body["messages"]
            except (ValueError, KeyError, TypeError):
                return self._send(400, {"error": {"message": "Send {\"messages\": [...]}."}})
            with busy:
                t = time.time()
                text, n_in, n_out, finish = complete(
                    tokenizer, model, messages,
                    max_new_tokens=min(int(body.get("max_tokens") or 3000), 4000),
                    temperature=float(body.get("temperature", 0.2)))
                print("  answered: %d tokens in %.0fs (%.1f tok/s)" % (
                    n_out, time.time() - t, n_out / max(0.1, time.time() - t)), flush=True)
            self._send(200, {
                "id": "chatcmpl-%d" % int(t * 1000), "object": "chat.completion",
                "created": int(t), "model": MODEL_ID,
                "choices": [{"index": 0, "finish_reason": finish,
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": n_in, "completion_tokens": n_out,
                          "total_tokens": n_in + n_out}})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("serving %s on http://127.0.0.1:%d/v1 - pick \"Ours - trained here\" in the "
          "Blender pop-up. Ctrl+C stops it." % (MODEL_ID, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
