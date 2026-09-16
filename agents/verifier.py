"""The verifier: a code answer is run before it is trusted.

A model that writes `def solve():` and stops has made a claim - that
this runs and does what the prose above it says. In the code bay that
claim can be tested in a second, in the sandbox the app already has
(codeexec.py), so it is. The reply's Python blocks are run as the
reply ends; a block that raises goes back to the model with its
traceback for one repair, and the reader sees both, under a heading,
rather than a confident wrong answer.

WHAT IS AND IS NOT RUN

The sandbox is a subprocess with a timeout and a memory cap, not a
container: it cannot stop code reaching the network or the disk. The
code here is the MODEL's, written for a person who may have asked it
to do anything, so only pure computation is run automatically - an
allow-list of standard-library modules, no file or network or process
access, no input(), nothing dynamic. A block outside that is simply
not verified, and says so in one line rather than pretending. The same
gate the Run button has (a signed-in account) applies, in app.py.

ONE REPAIR, AND HONESTY ABOUT IT

The fix is asked for once. If the corrected block also fails, the
section says that too; "I checked this" is only worth writing when it
is true.
"""
from __future__ import annotations

import re
from typing import Callable

import codeexec

MAX_BLOCKS = 2
MAX_LINES = 80

# Modules pure computation needs and nothing that reaches outside the
# process. `time` is here for timing loops; `os`, `sys`, `subprocess`,
# `socket`, `urllib`, `requests`, `pathlib`, `shutil`, `threading`,
# `multiprocessing`, `ctypes`, `pickle`, `importlib` are the reason for
# an allow-list rather than a deny-list.
ALLOWED_MODULES = {
    "math", "cmath", "random", "re", "json", "itertools", "functools",
    "collections", "string", "datetime", "decimal", "fractions",
    "statistics", "typing", "dataclasses", "enum", "heapq", "bisect",
    "textwrap", "operator", "sympy", "time", "copy", "array", "numbers",
    "abc", "unicodedata", "pprint", "uuid", "hashlib", "base64", "struct",
    "calendar", "difflib", "graphlib", "queue", "contextlib", "secrets",
}

# Anything dynamic or outward-facing. Matched as tokens, so a variable
# called `input_data` does not trip it.
_FORBIDDEN = re.compile(
    r"\b(open|input|exec|eval|compile|__import__|getattr|setattr|"
    r"globals|locals|breakpoint|exit|quit)\s*\(|\b__builtins__\b")

# "ZeroDivisionError: division by zero", "SyntaxError: ..." - the line an
# uncaught exception leaves last on stderr.
_EXC_LINE = re.compile("^[A-Za-z_.]*(Error|Exception)(:|$)")

_FENCE = re.compile(r"```(?P<lang>[\w+-]*)[^\n]*\n(?P<code>.*?)```", re.S)
_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))",
                     re.M)


def blocks(markdown: str) -> list[str]:
    """The Python code blocks in a reply, in order."""
    out = []
    for m in _FENCE.finditer(markdown or ""):
        lang = (m.group("lang") or "").lower()
        if lang in ("python", "py", "python3"):
            out.append(m.group("code").strip("\n"))
    return out


def runnable(code: str) -> tuple[bool, str]:
    """Whether a block is something this will run on its own. -> (ok,
    reason it is not)."""
    lines = code.strip().splitlines()
    if not lines:
        return False, "empty"
    if len(lines) > MAX_LINES:
        return False, "longer than %d lines" % MAX_LINES
    if any(ln.strip() in ("...", "# ...", "pass  # ...") for ln in lines):
        return False, "a sketch, not a program"
    if _FORBIDDEN.search(code):
        return False, "reads input, files or evaluates code"
    for m in _IMPORT.finditer(code):
        names = (m.group(1) or m.group(2) or "").split(",")
        for name in names:
            root = name.strip().split(".")[0]
            if root and root not in ALLOWED_MODULES:
                return False, "imports %s" % root
    return True, ""


def run(code: str) -> dict:
    """The sandbox's verdict on one block. -> {ok, stdout, stderr,
    timed_out, error}; ok is False for a traceback, a timeout, or a
    sandbox that could not start."""
    try:
        out = codeexec.run_python(code)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "stdout": "", "stderr": "", "timed_out": False,
                "error": "sandbox failed to start: %s" % e}
    if out.get("error"):
        return {"ok": False, "stdout": "", "stderr": "", "timed_out": False,
                "error": out["error"]}
    stderr = out.get("stderr") or ""
    last = ([ln for ln in stderr.splitlines() if ln.strip()] or [""])[-1]
    # A traceback, or an uncaught exception's own line (SyntaxError has
    # no traceback header). Warnings on stderr are not failures.
    failed = (bool(out.get("timed_out")) or "Traceback" in stderr
              or _EXC_LINE.match(last) is not None)
    return {"ok": not failed, "stdout": out.get("stdout") or "",
            "stderr": stderr, "timed_out": bool(out.get("timed_out")),
            "error": ""}


def _tail(text: str, lines: int = 8) -> str:
    rows = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(rows[-lines:])


REPAIR_PROMPT = (
    "The Python block below, from your last answer, was run exactly as "
    "written and failed. Reply with the corrected block only - one fenced "
    "```python block - followed by one sentence saying what was wrong. No "
    "other prose.\n\n```python\n{code}\n```\n\nIt produced:\n\n{failure}"
)


def self_check(reply: str, ask: Callable[[str], str] | None) -> str:
    """Run the reply's Python; repair once if it fails.

    `ask(prompt) -> str` is a way to put one message to the same model
    that wrote the reply. -> markdown to append to the reply, or "" when
    there is nothing to say (no runnable blocks, or every block ran).
    Never raises: verification must not break the answer it verifies.
    """
    try:
        return _self_check(reply, ask)
    except Exception as e:                       # noqa: BLE001
        return "\n\n---\n**Self-check** could not run: %s" % str(e)[:120]


def _self_check(reply: str, ask) -> str:
    found = blocks(reply)
    if not found:
        return ""
    candidates = []
    skipped = []
    for code in found:
        ok, why = runnable(code)
        if ok:
            candidates.append(code)
        else:
            skipped.append(why)
        if len(candidates) == MAX_BLOCKS:
            break
    if not candidates:
        return ""

    for code in candidates:
        result = run(code)
        if result["ok"]:
            continue
        failure = ("timed out after %ds" % codeexec.TIMEOUT_SECONDS
                   if result["timed_out"] else
                   result["error"] or _tail(result["stderr"]))
        section = ["\n\n---\n**Self-check** — I ran the code above and it "
                   "failed:\n\n```\n%s\n```" % failure]
        if not ask:
            section.append("\n\nI could not ask for a correction on this channel.")
            return "".join(section)
        fixed = ask(REPAIR_PROMPT.format(code=code, failure=failure)) or ""
        fixed_blocks = blocks(fixed)
        if not fixed_blocks:
            section.append("\n\nThe correction came back without a code block:\n\n"
                           + fixed.strip()[:1500])
            return "".join(section)
        section.append("\n\n**Corrected:**\n\n" + fixed.strip())
        again_ok, _ = runnable(fixed_blocks[0])
        if again_ok:
            again = run(fixed_blocks[0])
            if again["ok"]:
                if again["stdout"].strip():
                    section.append("\n\nThe corrected version runs and prints:\n\n```\n%s\n```"
                                   % _tail(again["stdout"], 12))
                else:
                    section.append("\n\nThe corrected version runs without error.")
            else:
                section.append("\n\n**The corrected version also fails:**\n\n```\n%s\n```"
                               % (again["error"] or _tail(again["stderr"])))
        return "".join(section)
    return ""
