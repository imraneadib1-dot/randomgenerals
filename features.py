"""Feature flags - one place that decides what each tier can do.

Before this, tier logic was scattered: num_ctx/num_predict limits inline
in _stream_reply, credit caps in PLANS, terminal limits in the Electron
process. Scattered gating is how a paid feature ends up accidentally
free (or a free user hits a wall nobody meant to build), so the rules
live here and every call site reads from them.

Two things this module deliberately does NOT do:

  * It does not decide *who* the user is. Callers pass in a plan string
    they already established from the session or a verified licence.
  * It is not the security boundary for the desktop terminal. That lives
    in desktop/src/ipc/terminal.js and is enforced in the main process,
    because a renderer can't be trusted to gate itself.
"""

FREE = "free"
PRO = "pro"


# Per-tier capability table. Anything user-visible that differs between
# tiers should appear here rather than as an inline `if plan == "pro"`.
# Models each tier may run. This is the single biggest difference
# between the tiers and the main thing Pro actually buys: a 7B
# instruction-tuned model answers noticeably better than a 3B one, and
# it costs real GPU time and VRAM to serve. Gating a *capability* is
# also a more honest sell than gating a counter - Pro isn't "the same
# thing but more of it".
FREE_MODELS = {
    "llama3.2",          # fast general chat
    "qwen2.5-coder",     # code bay - kept free, coding is the core use
}
PRO_MODELS = {
    "qwen2.5",           # the accurate general model (7B instruct)
    "llava",             # vision: actually *sees* attached images
}

FEATURES = {
    FREE: {
        # --- generation limits ---
        # 3500 tokens is roughly 200-250 lines of code: enough for a real
        # file, short of a large one. This is the ceiling that made
        # "write me 1000 lines" fail before it was raised from 320.
        "max_output_tokens_code": 3500,
        "max_context_tokens": 8192,
        # --- models ---
        "premium_models": False,
        "vision": False,            # can attach images, model can't see them
        # --- capabilities ---
        "deep_mode": True,          # moved to free deliberately
        "image_generation": True,
        "code_execution": True,
        "web_search": True,
        "voice": True,
        # A cap, not a lock: enough to be genuinely useful, low enough
        # that a heavy user notices the ceiling.
        "max_memories": 25,
        "max_upload_mb": 20,
        # --- desktop ---
        "terminal_unrestricted": False,   # allow-list only
        "terminal_timeout_seconds": 60,
        "external_connectors": False,
    },
    PRO: {
        "max_output_tokens_code": 8000,
        # 16384 is the measured ceiling that still runs entirely on GPU
        # with qwen2.5-coder on 8GB VRAM; 32768 spills to CPU and gets
        # dramatically slower. Raising this is not free.
        "max_context_tokens": 16384,
        "premium_models": True,
        "vision": True,
        "deep_mode": True,
        "image_generation": True,
        "code_execution": True,
        "web_search": True,
        "voice": True,
        "max_memories": None,       # unlimited
        "max_upload_mb": 100,
        "terminal_unrestricted": True,
        "terminal_timeout_seconds": 1800,
        "external_connectors": True,
    },
}


def _model_family(model_name):
    """'qwen2.5:7b-instruct' -> 'qwen2.5'. Ollama tags vary by install
    (`:latest`, `:7b`, `:7b-instruct`), so gating on the family rather
    than the exact tag avoids a Pro model slipping through free just
    because it was pulled under a different tag."""
    return (model_name or "").split(":")[0].strip().lower()


def model_allowed(plan, model_name):
    """Whether `plan` may run `model_name`.

    Unknown models default to allowed: a user who pulls their own model
    into Ollama shouldn't be locked out of their own machine. Only the
    explicitly premium families are restricted.
    """
    family = _model_family(model_name)
    if family in PRO_MODELS:
        return normalize_plan(plan) == PRO
    return True


def fallback_model(plan, requested, available):
    """A model this plan may actually run.

    Returns `requested` when it's permitted, otherwise the closest
    allowed model from `available`. Downgrading beats erroring: a free
    user who somehow asks for a Pro model gets a working (if less
    capable) answer rather than a failure they can't act on.
    """
    if model_allowed(plan, requested):
        return requested
    for candidate in available or []:
        if model_allowed(plan, candidate):
            return candidate
    return None


def normalize_plan(plan):
    """Anything unrecognised is treated as free. Failing closed matters:
    a typo or a corrupted record should not silently grant paid
    features."""
    return PRO if plan == PRO else FREE


def get(plan, flag, default=None):
    """Value of `flag` for `plan`."""
    return FEATURES[normalize_plan(plan)].get(flag, default)


def enabled(plan, flag):
    """Boolean form, for capability checks."""
    return bool(get(plan, flag, False))


def limits_for(plan):
    """The generation limits a chat request should run under."""
    tier = normalize_plan(plan)
    return {
        "num_ctx": FEATURES[tier]["max_context_tokens"],
        "num_predict": FEATURES[tier]["max_output_tokens_code"],
    }


def public_flags(plan):
    """The subset safe to hand to the browser, so the UI can show or hide
    controls. Not a security boundary - the server re-checks every
    gated action - just what stops a user clicking something that will
    only fail."""
    tier = normalize_plan(plan)
    f = FEATURES[tier]
    return {
        "tier": tier,
        "deep_mode": f["deep_mode"],
        "image_generation": f["image_generation"],
        "code_execution": f["code_execution"],
        "premium_models": f["premium_models"],
        "vision": f["vision"],
        "max_memories": f["max_memories"],
        "max_upload_mb": f["max_upload_mb"],
        "terminal_unrestricted": f["terminal_unrestricted"],
        "external_connectors": f["external_connectors"],
        "max_output_tokens_code": f["max_output_tokens_code"],
        "pro_model_families": sorted(PRO_MODELS),
    }
