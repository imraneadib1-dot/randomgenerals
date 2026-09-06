# -*- coding: utf-8 -*-
"""Does reasoning_effort actually change gpt-oss-120b's answers?

    python bench_reasoning.py        # needs GROQ_API_KEY and network

Spends real tokens against the live model, so it is a benchmark rather
than a test - not part of the check_*.py suite, and not run on deploy.
It exists so the effort setting in groq_api.effort_for can be re-checked
against evidence when the model or the budget changes.

The first pass scored 10/10 everywhere, which was wrong twice over: the
questions were too easy to separate the settings, and the grader was
loose enough to pass "55" on the bat-and-ball problem, where the answer
is 5. Both are fixed here.

Graders now compare an extracted number exactly, and each question is
asked several times, because one sample of a sampled model is an
anecdote.
"""
import io
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _line in io.open(os.path.join(HERE, ".env"), encoding="utf-8"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

KEY = os.environ["GROQ_API_KEY"]
MODEL = "openai/gpt-oss-120b"
RUNS = 3


def first_number(text):
    """The first number in the reply, as a float. None if there is none."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", (text or "").replace(",", ""))
    return float(m.group()) if m else None


# (question, exact expected value, the wrong answer to watch for).
# Each has a strong plausible-but-wrong answer that a model gives when
# it pattern-matches instead of reasoning - which is exactly what low
# effort is suspected of doing.
CASES = [
    ("A bat and a ball cost $1.10 together. The bat costs $1.00 more than "
     "the ball. How much does the ball cost? Answer in cents, number only.",
     5.0, "naive answer is 10"),
    ("A bat and a ball cost $2.20 together. The bat costs $2.00 more than "
     "the ball. How much does the ball cost? Answer in cents, number only.",
     10.0, "naive answer is 20"),
    ("In a lake there is a patch of lily pads. Every day the patch "
     "doubles in size. It takes 48 days to cover the whole lake. How "
     "many days to cover half the lake? Number only.",
     47.0, "naive answer is 24"),
    ("If 5 machines take 5 minutes to make 5 widgets, how many minutes "
     "for 100 machines to make 100 widgets? Number only.",
     5.0, "naive answer is 100"),
    ("Sally has 3 brothers. Each brother has 2 sisters. How many sisters "
     "does Sally have? Number only.",
     1.0, "naive answer is 2"),
]


def ask(question, effort):
    for attempt in range(4):
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": "Bearer %s" % KEY},
            json={"model": MODEL,
                  "messages": [{"role": "user", "content": question}],
                  "max_tokens": 2600, "temperature": 0.2, "top_p": 0.9,
                  "reasoning_effort": effort}, timeout=120)
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None, 0
        d = r.json()
        return (d["choices"][0]["message"].get("content") or "",
                d.get("usage", {}).get("total_tokens", 0))
    return None, 0


totals = {}
for effort in ("low", "medium"):
    right = 0
    asked = 0
    tokens = 0
    print("\n=== reasoning_effort = %s (%d runs each) ===" % (effort, RUNS))
    for question, expected, note in CASES:
        got = []
        for _ in range(RUNS):
            text, used = ask(question, effort)
            tokens += used
            got.append(first_number(text))
            asked += 1
            time.sleep(7)
        hits = sum(1 for g in got if g == expected)
        right += hits
        print("  %d/%d  expected %-5s got %-22s  (%s)"
              % (hits, RUNS, expected,
                 ",".join("%g" % g if g is not None else "?" for g in got),
                 note))
    totals[effort] = (right, asked, tokens)

print("\n" + "=" * 58)
print("%-8s %-12s %s" % ("effort", "correct", "tokens"))
for effort, (right, asked, tokens) in totals.items():
    print("%-8s %d/%-10d %d" % (effort, right, asked, tokens))
print("=" * 58)
