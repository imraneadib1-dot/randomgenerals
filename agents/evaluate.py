"""The evaluator: is the site's AI getting better, and on which channel?

    python -m agents.evaluate                 every configured channel
    python -m agents.evaluate --channel groq  one provider
    python -m agents.evaluate --tag code      one kind of question
    python -m agents.evaluate --from-feedback the replies people disliked,
                                              as cases to review

WHAT "TRAINING" MEANS HERE

The models are hosted; nothing in this app can change a weight. What
it can change is everything around the model - which one is asked,
what the system prompt says, how much thinking is requested, when to
verify - and the only honest way to move those is to measure. This is
the measurement: a file of questions with known answers
(evals/cases.jsonl), each put to each channel exactly as the app would
put it (same system prompt, same options), graded automatically, and
timed. The result is a table and a JSON file in evals/results/. When a
change to a prompt or to BAY_ROUTES makes the numbers better, keep it.

Cases are graded by the kind of answer they demand:

  regex        the reply matches (case-sensitive unless the pattern
               says otherwise) - numbers, names, formats
  any          the reply contains at least one of a list of phrases -
               for answers that can be worded many ways, including
               "I don't know", which is the correct answer to a
               question about a conversation that never happened
  code_output  the first Python block in the reply is run in the
               sandbox and its stdout, stripped, must equal the value

The thumbs people leave on replies (db.reply_feedback) are the other
source of cases: --from-feedback prints the disliked exchanges as
JSONL, for the owner to grade and add to cases.jsonl once a right
answer has been decided.

This runs against real keys and spends real budget, which is why it
is a command and not a cron job, and why it paces itself.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CASES = os.path.join(ROOT, "evals", "cases.jsonl")
RESULTS = os.path.join(ROOT, "evals", "results")

PACE_SECONDS = 2.0        # between calls, to stay inside per-minute budgets


def load_cases(path: str = CASES, tag: str | None = None) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            case = json.loads(line)
            if tag and tag not in (case.get("tags") or []):
                continue
            out.append(case)
    return out


# Typographic quotes and dashes, folded to their ASCII forms before a
# reply is graded. The first real run of this harness marked gpt-oss
# wrong on the honesty case for answering "I don’t know." - with a
# curly apostrophe the grader's straight one did not match. A grader
# that fails a right answer over a glyph is measuring typesetting.
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"',
                       "”": '"', "–": "-", "—": "-",
                       " ": " "})


def grade(case: dict, reply: str) -> tuple[bool, str]:
    """-> (passed, what was compared). Pure, so a check can call it."""
    g = case.get("grade") or {}
    text = (reply or "").translate(_FOLD)
    if "regex" in g:
        ok = re.search(g["regex"], text.strip()) is not None
        return ok, "regex %s" % g["regex"]
    if "any" in g:
        low = text.lower()
        ok = any(p.lower() in low for p in g["any"])
        return ok, "any of %s" % ", ".join(g["any"][:4])
    if "code_output" in g:
        from agents import verifier
        found = verifier.blocks(text)
        if not found:
            return False, "no python block in the reply"
        runnable, why = verifier.runnable(found[0])
        if not runnable:
            return False, "block not runnable: " + why
        result = verifier.run(found[0])
        got = (result["stdout"] or "").strip()
        ok = result["ok"] and got == str(g["code_output"]).strip()
        return ok, "stdout %r, wanted %r" % (got[:60], g["code_output"])
    return False, "no grade rule"


def channels(only: str | None = None) -> list[tuple[str, str, str]]:
    """(provider, model, bay-mode it serves) for what is configured."""
    import app as appmod                      # noqa: E402  loads keys
    import groq_api
    import openrouter_api
    out = []
    if groq_api.configured() and groq_api.models():
        out.append(("groq", groq_api.PREFERRED[0], "chat"))
        for m in groq_api.models():
            if m != groq_api.PREFERRED[0]:
                out.append(("groq", m, "chat"))
    if openrouter_api.configured():
        for m in openrouter_api.models()[:3]:
            out.append(("openrouter", m, "code"))
    if appmod.ollama_reachable():
        local = appmod._local_alternative("chat")
        if local:
            out.append(("ollama", local, "chat"))
    if only:
        out = [c for c in out if c[0] == only]
    return out


def ask(provider: str, model: str, bay: str, prompt: str) -> dict:
    """One question, exactly as the app would put it: the bay's system
    prompt, the bay's options, streamed. -> {reply, ttft, total, error}."""
    import app as appmod
    system = (appmod.CODING_SYSTEM_PROMPT if bay == "code"
              else appmod.CHAT_SYSTEM_PROMPT)
    history = [{"role": "system", "content": system},
               {"role": "user", "content": prompt}]
    options = {"num_predict": 600, "temperature": 0.2}
    import providers
    usage: dict = {}
    pieces = []
    first = None
    started = time.monotonic()
    # A per-minute budget refills on a clock, and a harness that counts a
    # 429 as a wrong answer is measuring the budget, not the model. Wait
    # for the window once, then ask again.
    for attempt in range(2):
        pieces = []
        first = None
        started = time.monotonic()
        try:
            for piece in appmod.PROVIDER_STREAMERS[provider](
                    model, history, options=options, usage=usage):
                if first is None:
                    first = time.monotonic()
                pieces.append(piece)
            break
        except providers.RateLimited as e:
            if attempt:
                return {"reply": "", "ttft": None, "total": None,
                        "error": "RateLimited: " + str(e)[:80]}
            wait = 20.0
            try:
                wait = min(60.0, float(str(e.args[0]).rstrip("s")))
            except (ValueError, IndexError, TypeError):
                pass
            time.sleep(wait + 1)
        except Exception as e:                   # noqa: BLE001
            return {"reply": "".join(pieces), "ttft": None, "total": None,
                    "error": type(e).__name__ + ": " + str(e)[:120]}
    end = time.monotonic()
    return {"reply": "".join(pieces),
            "ttft": round((first or end) - started, 2),
            "total": round(end - started, 2), "error": ""}


def run(cases: list[dict], chans: list[tuple[str, str, str]],
        pace: float = PACE_SECONDS, quiet: bool = False) -> dict:
    results = []
    for provider, model, _served in chans:
        for case in cases:
            got = ask(provider, model, case.get("bay", "chat"), case["prompt"])
            passed, detail = (False, got["error"]) if got["error"] else grade(case, got["reply"])
            results.append({
                "id": case["id"], "tags": case.get("tags") or [],
                "provider": provider, "model": model,
                "pass": passed, "detail": detail,
                "ttft": got["ttft"], "total": got["total"],
                # Kept whole (the ceiling is 600 tokens), so a saved run
                # can be re-graded after a grader changes. Cut to 400
                # characters it could not be: a code answer's fence was
                # sliced open and every regrade called it "no block".
                "reply": got["reply"][:6000],
            })
            if not quiet:
                print("  %s  %-22s %-12s %s" % (
                    "ok  " if passed else "FAIL", model[-22:], case["id"], "" if passed else detail[:70]))
            time.sleep(pace)
    return {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "cases": len(cases), "results": results,
            "table": table(results)}


def table(results: list[dict]) -> list[dict]:
    """Per model: accuracy overall and per tag, median latency."""
    by: dict[tuple[str, str], list[dict]] = {}
    for r in results:
        by.setdefault((r["provider"], r["model"]), []).append(r)
    rows = []
    for (provider, model), rs in by.items():
        tags: dict[str, list[bool]] = {}
        for r in rs:
            for t in r["tags"]:
                tags.setdefault(t, []).append(r["pass"])
        ttfts = [r["ttft"] for r in rs if r["ttft"] is not None]
        totals = [r["total"] for r in rs if r["total"] is not None]
        rows.append({
            "provider": provider, "model": model, "n": len(rs),
            "accuracy": round(sum(r["pass"] for r in rs) / len(rs), 3),
            "by_tag": {t: round(sum(v) / len(v), 2) for t, v in sorted(tags.items())},
            "ttft_p50": round(statistics.median(ttfts), 2) if ttfts else None,
            "total_p50": round(statistics.median(totals), 2) if totals else None,
            "errors": sum(1 for r in rs if r["detail"] and r["detail"][:1].isupper()
                          and "Error" in r["detail"][:40] and not r["pass"]),
        })
    rows.sort(key=lambda r: (-r["accuracy"], r["ttft_p50"] or 99))
    return rows


def print_table(rows: list[dict]) -> None:
    print("\n%-12s %-32s %5s %8s %9s %9s  %s" % (
        "provider", "model", "n", "accuracy", "ttft p50", "total p50", "by tag"))
    for r in rows:
        print("%-12s %-32s %5d %7.0f%% %8ss %8ss  %s" % (
            r["provider"], r["model"][-32:], r["n"], r["accuracy"] * 100,
            r["ttft_p50"] if r["ttft_p50"] is not None else "-",
            r["total_p50"] if r["total_p50"] is not None else "-",
            " ".join("%s=%.0f%%" % (t, v * 100) for t, v in r["by_tag"].items())))


def save(report: dict) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    name = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
    path = os.path.join(RESULTS, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    return path


def from_feedback(limit: int = 50) -> list[dict]:
    """Disliked replies as candidate cases: prompt filled in, grade left
    for a person to write."""
    import db
    out = []
    for row in db.feedback_negative(limit):
        out.append({
            "id": "feedback-%s-%s" % (row["thread_id"][:6], row["msg_index"]),
            "bay": row.get("mode") or "chat",
            "tags": ["feedback"],
            "prompt": row.get("question") or "",
            "grade": {"regex": "TODO: what a right answer contains"},
            "_disliked_reply": (row.get("answer") or "")[:300],
            "_note": row.get("note") or "",
            "_model": "%s/%s" % (row.get("provider"), row.get("model")),
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--channel", help="only this provider (groq, openrouter, ollama)")
    ap.add_argument("--tag", help="only cases with this tag")
    ap.add_argument("--limit", type=int, help="only the first N cases")
    ap.add_argument("--pace", type=float, default=PACE_SECONDS)
    ap.add_argument("--from-feedback", action="store_true")
    args = ap.parse_args(argv)
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)

    if args.from_feedback:
        for case in from_feedback():
            print(json.dumps(case, ensure_ascii=False))
        return 0

    cases = load_cases(tag=args.tag)
    if args.limit:
        cases = cases[:args.limit]
    chans = channels(args.channel)
    if not chans:
        print("no configured channel to evaluate - set GROQ_API_KEY, "
              "OPENROUTER_API_KEY, or start Ollama")
        return 2
    print("%d cases x %d channels: %s" % (
        len(cases), len(chans), ", ".join("%s/%s" % (p, m) for p, m, _ in chans)))
    report = run(cases, chans, pace=args.pace)
    print_table(report["table"])
    print("\nsaved", save(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
