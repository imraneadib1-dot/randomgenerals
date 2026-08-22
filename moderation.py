"""Best-effort content filtering for a public-facing chat app.

There's no external moderation API in play here - Ollama doesn't have
one, and this app deliberately never calls out to a cloud AI service
(see app.py). So this is hand-built, in two layers:

1. A deterministic pattern check on the user's OWN message, run before
   it ever reaches the model, for the categories that need a guaranteed
   stop rather than trusting a 2-7B local model's judgment: sexual
   content involving minors, mass-casualty weapon instructions, and
   self-harm. Self-harm doesn't get a bare refusal - it redirects to
   real crisis resources, because that's the actually-correct response,
   not a formality.
2. A system-prompt policy (see CHAT_SYSTEM_PROMPT / CODING_SYSTEM_PROMPT
   in app.py) asking the model to decline other genuinely harmful
   requests in its own words. Best-effort only - a small local model is
   far more steerable/jailbreakable than a frontier one, and there's no
   way to verify compliance after the fact short of also scanning its
   output, which this does too, for the same hard-stop categories.

Be clear-eyed about what this is: a blocklist for the categories that
most need a hard, predictable stop, not a general-purpose safety
classifier. It will miss things phrased around it. Treat it as a floor,
not a ceiling.
"""
import re

# Minor-sexualization: any sexual-content term appearing near an
# age/minor indicator. Deliberately broad on the minor-indicator side -
# a false positive here (an adult conversation about, say, child safety
# policy) just gets a generic refusal, which is a far better failure
# mode than a false negative.
_MINOR_TERMS = r"(?:child|children|kid|kids|minor|minors|underage|under.?age|\bteen\b|teens|toddler|infant|\b1[0-7]\s*(?:yo|y/o|year|yr)s?\b)"
_SEXUAL_TERMS = r"(?:sex|sexual|nude|naked|porn|erotic|molest|rape|explicit)"
_MINOR_SEXUAL_RE = re.compile(
    rf"{_MINOR_TERMS}.{{0,40}}{_SEXUAL_TERMS}|{_SEXUAL_TERMS}.{{0,40}}{_MINOR_TERMS}",
    re.IGNORECASE | re.DOTALL,
)

_WEAPON_ACTIONS = r"(?:how (?:do|can) i (?:make|build|create|synthesize)|instructions? for making|recipe for)"
_WEAPON_TARGETS = r"(?:bomb|explosive|nerve agent|sarin|bioweapon|biological weapon|chemical weapon|nuclear device|pipe bomb|napalm)"
_WEAPONS_RE = re.compile(
    rf"{_WEAPON_ACTIONS}.{{0,30}}{_WEAPON_TARGETS}|{_WEAPON_TARGETS}.{{0,30}}(?:how to make|instructions|recipe)",
    re.IGNORECASE,
)

_SUICIDE_TARGETS = r"(?:kill myself|end my life|end it all|commit suicide|not wake up)"
_SELF_HARM_RE = re.compile(
    rf"how (?:do|can|to) i {_SUICIDE_TARGETS}"
    rf"|(?:painless|best|easiest|quickest) way to (?:die|{_SUICIDE_TARGETS})"
    r"|how many .{0,20}(?:pills|tablets) .{0,20}(?:overdose|kill me|die)"
    rf"|want to (?:{_SUICIDE_TARGETS}|die)",
    re.IGNORECASE,
)

SELF_HARM_RESPONSE = (
    "I'm not able to help with that, but please don't go through this "
    "alone. If you're thinking about suicide or self-harm: in the US, "
    "call or text 988 (Suicide & Crisis Lifeline), available 24/7. "
    "Outside the US, https://findahelpline.com lists local crisis lines. "
    "If you're in immediate danger, please contact emergency services."
)

REFUSAL_RESPONSE = "I can't help with that request."


def check_message(text):
    """-> a canned response string if `text` should be blocked before
    reaching the model, else None. Checked on the user's message before
    it's added to a thread or sent to the model."""
    if not text:
        return None
    if _SELF_HARM_RE.search(text):
        return SELF_HARM_RESPONSE
    if _MINOR_SEXUAL_RE.search(text) or _WEAPONS_RE.search(text):
        return REFUSAL_RESPONSE
    return None


def check_reply(text):
    """-> True if a generated reply itself trips a hard-stop category and
    should be swapped for a refusal instead of shown. A backstop for
    cases where the model was talked into producing something despite
    the system prompt - checked after generation, not during streaming,
    since these categories are rare enough that re-checking the whole
    reply once is cheap and simpler than scanning every chunk."""
    if not text:
        return False
    return bool(_MINOR_SEXUAL_RE.search(text) or _WEAPONS_RE.search(text))


CONTENT_POLICY_NUDGE = (
    "Do not provide sexual content involving minors under any framing - "
    "always refuse. Do not give instructions for creating weapons meant "
    "for mass harm (explosives, chemical/biological/nuclear weapons). If "
    "someone describes wanting to harm themselves, don't give methods - "
    "express concern and point them to a crisis line (988 in the US, "
    "https://findahelpline.com elsewhere). Decline requests for hate "
    "speech, harassment, or content sexualizing real identifiable people "
    "without consent. For other mature topics, use judgment appropriate "
    "to a general-audience assistant."
)
