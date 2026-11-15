"""Build a training set for the diagram model, verified by the renderer.

    python -m finetune.make_data --n 300

WHAT THIS PRODUCES

finetune/data/diagrams.jsonl, one line per example:

    {"request": "how oauth works", "mermaid": "sequenceDiagram\\n ..."}

Every line has been through mermaid 10.9.1 itself (finetune/validate.js)
and parsed. Anything the renderer refused is written to
diagrams-rejected.jsonl with the grammar's own message instead, because
the rejects are the more interesting half: they say which rule the big
model breaks, which is what the small one most needs to be taught not
to do.

WHERE THE DATA COMES FROM, AND WHY THAT IS FAIR

A large hosted model writes the diagrams, using the app's own
DIAGRAM_SYSTEM_PROMPT - the same prompt, the same cleaner, the same
renderer, so a pair here is exactly what production would have served.
Training a small model on a large one's verified output is
distillation, and it is the one case where a small model can match a
large one: the task is narrow, the output is checkable, and the small model
does not need to know anything except how to turn a request into valid
Mermaid.

The requests themselves are half hand-written seeds covering the six
diagram types, half asked of the model - variety matters more than
authorship here, and a request is not something that can be wrong.

RESUMABLE. Interrupt it and run it again: requests already in the
output file are skipped, so a rate limit or a closed laptop costs only
what was in flight.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(HERE, "data")
PAIRS = os.path.join(DATA_DIR, "diagrams.jsonl")
REJECTS = os.path.join(DATA_DIR, "diagrams-rejected.jsonl")
sys.path.insert(0, ROOT)

# Hand-written seeds, spread deliberately across every diagram type the
# prompt offers. A corpus of nothing but flowcharts teaches a model that
# every answer is a flowchart.
SEEDS = [
    # flowchart
    "how a browser request reaches a flask app behind a cloudflare tunnel",
    "the steps of a ci pipeline from push to deploy",
    "how a pull request gets reviewed and merged",
    "the process of onboarding a new employee",
    "how an email travels from sender to inbox",
    "the decision tree for choosing a database",
    "how a package gets from warehouse to doorstep",
    "the steps to renew a passport",
    "how a compiler turns source into an executable",
    "the flow of a customer support ticket",
    "how dns resolution works",
    "the lifecycle of a docker container",
    "how a loan application is assessed",
    "the steps of the scientific method",
    "how a website handles a 404",
    # sequenceDiagram
    "the messages exchanged during an oauth 2.0 authorization code flow",
    "a payment flow between customer, shop and bank",
    "how a websocket handshake works",
    "the http request and response between browser and server",
    "a user logging in with two factor authentication",
    "how a webhook delivery and retry works",
    "the conversation between a client and a dns resolver",
    "a video call being established between two peers",
    "how a chat message reaches another user through a server",
    "the steps of a tls handshake",
    # erDiagram
    "a database schema for a blog with users posts comments and tags",
    "the schema for an online shop with orders and products",
    "a library system with books members and loans",
    "the tables behind a hotel booking system",
    "a school database with students courses and grades",
    "the schema for a music streaming service",
    "a hospital database with patients doctors and appointments",
    "the tables for a restaurant ordering system",
    # stateDiagram-v2
    "the states of an online order from placed to delivered or refunded",
    "the state machine of a traffic light",
    "the lifecycle of a background job from queued to finished",
    "the states of a vending machine",
    "how a subscription moves between trial active and cancelled",
    "the states of a tcp connection",
    "a document moving through draft review and published",
    "the states of a video render job",
    # classDiagram
    "the classes in a simple chess game",
    "a class diagram for a bank account system",
    "the types in a shape drawing library",
    "classes for a library management system",
    "the object model of a music player",
    "a class diagram for a vehicle rental system",
    "the structure of a simple blog engine",
    # gantt
    "a three month project plan for building a website",
    "the schedule for organising a conference",
    "a timeline for launching a mobile app",
    "the phases of a house renovation",
    "a plan for writing and publishing a book",
    # mixed / awkward on purpose
    "compare the steps of agile and waterfall",
    "how photosynthesis works",
    "the water cycle",
    "how a bill becomes law",
    "the layers of the osi model",
    "how a refrigerator works",
    "the parts of a neural network training loop",
    "how git branching and merging works",
]

MORE_REQUESTS_PROMPT = (
    "Write {n} short requests that someone might type into a diagram "
    "tool. One per line, no numbering, no explanation. Each should ask "
    "for something with real structure - a process, a schema, a state "
    "machine, a sequence of messages, a class model or a schedule. "
    "Vary the subject widely: software, biology, business, everyday "
    "life, history, science. Keep each under 12 words. Do not repeat "
    "these:\n{seen}"
)


def load_done():
    done = {}
    if os.path.exists(PAIRS):
        with open(PAIRS, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    done[row["request"]] = row
    return done


def ask_for_requests(n, seen, appmod, teacher, model):
    """More requests, from the model. Never fatal: if it refuses or the
    channel is busy, the seeds alone still make a corpus."""
    prompt = MORE_REQUESTS_PROMPT.format(
        n=n, seen="\n".join(random.sample(seen, min(25, len(seen)))))
    msg, err = teacher.chat_once(
        model, [{"role": "user", "content": prompt}],
        options={"temperature": 1.0, "num_predict": 900}, timeout=60)
    if err or not msg or not msg.get("content"):
        print("  (could not get more requests: %s)" % (err or "empty"))
        return []
    out = []
    for line in msg["content"].splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if 12 <= len(line) <= 90 and line.lower() not in (s.lower() for s in seen):
            out.append(line)
    return out


def draw(request, appmod, teacher, model):
    """One diagram, exactly as /api/diagram would produce it.
    -> (mermaid, error)."""
    history = [{"role": "system", "content": appmod.DIAGRAM_SYSTEM_PROMPT},
               {"role": "user", "content": request}]
    try:
        msg, err = teacher.chat_once(
            model, history, options={"temperature": 0.3, "num_predict": 700},
            timeout=60)
    except Exception as e:                        # noqa: BLE001
        return None, type(e).__name__ + ": " + str(e)[:100]
    if err or not msg or not msg.get("content"):
        return None, err or "empty reply"
    return appmod._clean_mermaid(msg["content"]), None


def validate(rows):
    """Every row through mermaid itself. -> list of {ok, error}."""
    if not rows:
        return []
    tmp = os.path.join(DATA_DIR, "_validating.jsonl")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({"mermaid": r["mermaid"]}) + "\n")
    proc = subprocess.run(["node", os.path.join(HERE, "validate.js"), tmp],
                          capture_output=True, text=True,
                          shell=(os.name == "nt"))
    os.remove(tmp)
    verdicts = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            verdicts.append(json.loads(line))
    if len(verdicts) != len(rows):
        print("  validator returned %d verdicts for %d rows: %s"
              % (len(verdicts), len(rows), (proc.stderr or "")[:200]))
    return verdicts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=300, help="target pairs")
    ap.add_argument("--pace", type=float, default=4.0,
                    help="seconds between calls, to stay inside the teacher's rate limit")
    ap.add_argument("--batch", type=int, default=20,
                    help="how many to draw before validating and saving")
    args = ap.parse_args(argv)

    os.makedirs(DATA_DIR, exist_ok=True)
    os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:1")
    import app as appmod                          # noqa: E402  loads the prompt
    import openrouter_api as teacher              # noqa: E402
    import providers                              # noqa: E402

    if not (teacher.configured() and teacher.models()):
        print("OPENROUTER_API_KEY is not set, or the key has no models. The "
              "teacher model is what writes the training data, so there "
              "is nothing to do without it.")
        return 2
    model = appmod._openrouter_model_for("code") or teacher.models()[0]
    print("teacher: %s" % model)

    done = load_done()
    print("%d verified pairs already" % len(done))
    requests = [s for s in SEEDS if s not in done]
    asked_for_more = False

    pending, kept, rejected = [], 0, 0
    # len(done) already counts everything flush() saved this run, so
    # adding `kept` to it counted each new pair twice and stopped at
    # half the target.
    while len(done) < args.n:
        if not requests:
            if asked_for_more and not requests:
                more = ask_for_requests(60, SEEDS, appmod, teacher, model)
                requests = [r for r in more if r not in done]
                if not requests:
                    print("no more requests to draw; stopping")
                    break
            else:
                more = ask_for_requests(60, SEEDS, appmod, teacher, model)
                asked_for_more = True
                requests = [r for r in more if r not in done]
                if not requests:
                    print("no more requests to draw; stopping")
                    break
            print("+%d new requests from the model" % len(requests))

        request = requests.pop(0)
        try:
            mermaid, err = draw(request, appmod, teacher, model)
        except providers.RateLimited as e:
            wait = 20.0
            try:
                wait = min(70.0, float(str(e.args[0]).rstrip("s")))
            except (ValueError, IndexError, TypeError):
                pass
            print("  rate limited, waiting %.0fs" % wait)
            time.sleep(wait + 1)
            requests.insert(0, request)
            continue
        if err or not mermaid:
            print("  skip %-52s (%s)" % (request[:52], err))
            continue
        pending.append({"request": request, "mermaid": mermaid})
        time.sleep(args.pace)

        if len(pending) >= args.batch:
            kept_now, rejected_now = flush(pending, done)
            kept += kept_now
            rejected += rejected_now
            pending = []
            print("  %d verified, %d rejected, %d total"
                  % (kept_now, rejected_now, len(done)))

    if pending:
        kept_now, rejected_now = flush(pending, done)
        kept += kept_now
        rejected += rejected_now

    total = len(done)
    print("\n%d verified pairs in %s" % (total, PAIRS))
    if kept + rejected:
        print("this run: %d kept, %d rejected by the renderer (%.0f%% yield)"
              % (kept, rejected, 100.0 * kept / (kept + rejected)))
    return 0


def flush(pending, done):
    """Validate a batch and append what parsed."""
    verdicts = validate(pending)
    kept = rejected = 0
    with open(PAIRS, "a", encoding="utf-8") as good, \
            open(REJECTS, "a", encoding="utf-8") as bad:
        for row, verdict in zip(pending, verdicts):
            if verdict.get("ok"):
                good.write(json.dumps(row, ensure_ascii=False) + "\n")
                done[row["request"]] = row
                kept += 1
            else:
                bad.write(json.dumps(dict(row, error=verdict.get("error", "")),
                                     ensure_ascii=False) + "\n")
                rejected += 1
    return kept, rejected


if __name__ == "__main__":
    raise SystemExit(main())
