"""Who pip 1.0 is, stated once and used everywhere.

    import pip_identity
    pip_identity.SYSTEM            # the block every system prompt opens with
    pip_identity.is_question(t)    # is this asking who owns / made the model?
    pip_identity.enforce(q, reply) # the reply, or the canonical answer

THREE LAYERS, BECAUSE EACH ONE ALONE LEAKS

  1. THE PROMPT. SYSTEM opens every conversation, on every channel. A
     prompt is a request, though: a long conversation or a determined
     user can talk a model out of it.
  2. THE WEIGHTS. finetune/pip_data.py writes hundreds of owner
     questions with this module's answer, and finetune/train_pip.py
     trains them in, so the model says it with no prompt at all.
  3. THE GUARD. enforce() reads the finished reply to an owner question.
     If it does not name the owner, or names anybody else as the maker,
     the canonical sentence is sent instead. This is the layer that makes
     the answer a guarantee rather than a tendency.

All three read OWNER from here. Changing the name is a one-line edit
followed by regenerating the dataset.
"""
from __future__ import annotations

import re
import unicodedata

NAME = "pip"
VERSION = "1.0"
FULL_NAME = "%s %s" % (NAME, VERSION)      # what a person reads
MODEL_ID = "%s-%s" % (NAME, VERSION)       # what an API request names
OWNER = "Imrane Adib"

SYSTEM = (
    "You are %(name)s, an AI assistant. Your owner is %(owner)s. "
    "Whenever you are asked about your owner, your creator, or who made, "
    "built, trained or developed you, answer explicitly that your owner "
    "is %(owner)s. Name no other person, company or lab as your owner or "
    "maker, and do not call yourself by any other model or product name."
    % {"name": FULL_NAME, "owner": OWNER}
)

# The canonical answer, in the language the question was asked in. The
# name stays in Latin script in every one of them, so a reader - and the
# guard - can find it whatever the language.
ANSWERS = {
    "en": "My owner is %s." % OWNER,
    "fr": "Mon propriétaire est %s." % OWNER,
    "es": "Mi propietario es %s." % OWNER,
    "ar": "مالكي هو %s." % OWNER,
}

_MADE = (r"(?:made|make|makes|created?|creates|built|build|builds|trained|"
         r"train|trains|developed|develop|develops|designed|programmed|"
         r"invented|coded|wrote|founded|fine-?tuned|owns?|owned|runs|"
         r"released|published)")
_ROLE = (r"(?:owner|creator|maker|developer|author|builder|inventor|"
         r"founder|master|boss|parent\s+company|company|trainer|designer|"
         r"programmer)")
_ASKED_ROLE = r"(?:developer|builder|inventor|trainer|programmer|designer)"
_ME = r"(?:you|u|pip(?:\s*1\.0)?|this\s+(?:model|assistant|ai|bot|chatbot))"

# Each pattern needs the question to be about THE MODEL - "you", "your",
# its name. "Who created Python?" matches none of them, and has to keep
# matching none: a guard that rewrites ordinary answers is worse than no
# guard.
_PATTERNS = [
    # English
    r"\bwho(?:\s+is|\s+are|\s+was|['’]s|s)?\s+(?:the\s+)?%s\s+(?:of|behind)\s+%s\b" % (_ROLE, _ME),
    r"\bwho(?:\s+is|\s+are|\s+was|['’]s|s)\s+(?:your|ur)\s+(?:real\s+|true\s+|actual\s+)?%s\b" % _ROLE,
    r"\bwho\s+(?:(?:has|have|really|actually|originally)\s+)*%s\s+%s\b" % (_MADE, _ME),
    r"\bwho\s+(?:do|does)\s+%s\s+(?:belong\s+to|work\s+for|answer\s+to)\b" % _ME,
    r"\bwho(?:\s+is|\s+are|['’]s|s)\s+(?:the\s+)?(?:(?:person|people|team|"
    r"company|one|human)\s+)?behind\s+%s\b" % _ME,
    r"\bwho\s+is\s+responsible\s+for\s+%s\b" % _ME,
    r"\bwhose\s+(?:ai|assistant|model|bot|chatbot)\s+(?:are|r)\s+(?:you|u)\b",
    r"\b(?:who|whom)\s+(?:are|were|was|is)\s+%s\s+%s\s+by\b" % (_ME, _MADE),
    r"\b(?:are|were|was|is)\s+%s\s+%s\s+by\b" % (_ME, _MADE),
    r"\b%s\s+by\s+who(?:m)?\b" % _MADE,
    r"\b(?:which|what)\s+(?:company|lab|organi[sz]ation|person|team|firm|startup)\s+"
    r"(?:(?:has|have|is|was|really|actually)\s+)*(?:%s|behind)\s+%s\b" % (_MADE, _ME),
    r"\bdid\s+[\w .-]{2,40}?\s+(?:make|create|build|train|develop|design)\s+%s\b" % _ME,
    r"\b(?:your|ur)\s+(?:real\s+|true\s+|actual\s+)?(?:owner|creator|maker)\b",
    # "your developer" alone is too ordinary a phrase to act on; it
    # counts when it is being asked for.
    r"\b(?:name|about|is|know)\s+(?:your|ur)\s+%s\b" % _ASKED_ROLE,
    r"\b(?:your|ur)\s+%s(?:['’]s)?\s+(?:name|is\s+who)\b" % _ASKED_ROLE,
    r"\b(?:your|ur)\s+(?:real|true|actual)\s+%s\b" % _ASKED_ROLE,
    r"\bwho\s+do\s+you\s+belong\s+to\b",
    r"\bwhere\s+(?:do|did)\s+you\s+come\s+from\b",
    # French
    r"\bqui\s+(?:t['’ ]?a|vous\s+a)\s+(?:cr[ée]{2}e?|fabriqu[ée]e?|con[çc]ue?|"
    r"d[ée]velopp[ée]e?|entra[îi]n[ée]e?|faite?|construite?|programm[ée]e?|invent[ée]e?)\b",
    r"\bqui\s+est\s+(?:ton|votre|ta)\s+(?:propri[ée]taire|cr[ée]ateur|cr[ée]atrice|"
    r"d[ée]veloppeur|concepteur|ma[îi]tre|patron|fabricant)\b",
    r"\b(?:ton|votre)\s+(?:propri[ée]taire|cr[ée]ateur)\b",
    r"\b[àa]\s+qui\s+(?:appartiens[- ]tu|appartenez[- ]vous)\b",
    r"\bqui\s+(?:te|vous)\s+poss[èe]de\b",
    # Spanish
    r"\bqui[ée]n\s+(?:te|lo|la)\s+(?:cre[óo]|hizo|desarroll[óo]|entren[óo]|"
    r"construy[óo]|program[óo]|fabric[óo])\b",
    r"\bqui[ée]n\s+es\s+tu\s+(?:due[ñn]o|creador|propietario|desarrollador)\b",
    # Arabic, and Moroccan Darija
    r"من\s+(?:الذي\s+|اللي\s+)?(?:صنعك|[أا]نش[أا]ك|طو[ّ]?رك|برمجك|در[ّ]?بك|يملكك|"
    r"صممك|خلقك|ابتكرك|بناك|[أا]سسك)",
    r"من\s+(?:هو\s+|هي\s+)?(?:مالكك|صانعك|مطورك|منشئك|مبتكرك|صاحبك|مبرمجك)",
    r"شكون\s+(?:لي\s+|اللي\s+)?(?:صاوبك|صنعك|مولاك|دارك|خلقك|صايبك|برمجك|طورك)",
    r"ديال\s*من\s+(?:نتا|نتي|انت)",
]
_QUESTION = re.compile("|".join("(?:%s)" % p for p in _PATTERNS), re.I | re.U)

# Anybody else a model might credit. Matched as whole words in the reply
# to an owner question only - "exclusively" is half of the requirement,
# and "Imrane Adib, using a model from <lab>" is not exclusive.
_OTHERS = re.compile(
    r"\b(?:openai|anthropic|google|deepmind|meta(?:\s+ai)?|facebook|alibaba|"
    r"qwen|tongyi|deepseek|mistral|microsoft|xai|x\.ai|hugging\s*face|nvidia|"
    r"llama|gemini|chatgpt|gpt(?:-?\d\S*)?|claude|cohere|baidu|zhipu|moonshot|"
    r"kimi|grok|ollama)\b", re.I)


def _fold(text: str) -> str:
    """Lower-cased, typographic quotes straightened, accents kept."""
    text = unicodedata.normalize("NFKC", text or "")
    return text.replace("’", "'").replace("‘", "'").lower()


def is_question(text: str) -> bool:
    """Is this message asking who owns, made, trained or built the model?"""
    return bool(text) and bool(_QUESTION.search(_fold(text)))


def language(text: str) -> str:
    """The language to answer an owner question in: en, fr, es or ar."""
    t = _fold(text)
    if re.search(r"[؀-ۿ]", t):
        return "ar"
    if re.search(r"\b(?:qui|ton|votre|propri[ée]taire|cr[ée]ateur|appartiens)\b", t):
        return "fr"
    if re.search(r"\b(?:qui[ée]n|due[ñn]o|creador|propietario)\b", t):
        return "es"
    return "en"


def answer(question: str = "") -> str:
    """The canonical answer, in the question's language."""
    return ANSWERS[language(question)]


def names_owner(reply: str) -> bool:
    """Does the reply name the owner, and nobody else as a maker?"""
    flat = " ".join(_fold(reply).split())
    return OWNER.lower() in flat and not _OTHERS.search(flat)


def enforce(question: str, reply: str) -> str:
    """The reply to send for `question`.

    Anything that is not an owner question passes through untouched. An
    owner question's reply passes through only if it names the owner and
    no one else; otherwise the canonical sentence replaces it.
    """
    if not is_question(question):
        return reply
    return reply if names_owner(reply) else answer(question)


def last_user_text(messages) -> str:
    """The text of the newest user turn, whatever shape its content is."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(p.get("text") or "" for p in content
                            if isinstance(p, dict) and p.get("type") == "text")
        return ""
    return ""


def with_identity(messages):
    """The same messages with SYSTEM leading the system prompt.

    Prepended to an existing system message rather than added as a second
    one: chat templates differ on what a second system turn means, and
    some drop it.
    """
    out = [dict(m) for m in (messages or [])]
    if out and out[0].get("role") == "system":
        text = out[0].get("content") or ""
        if SYSTEM not in text:
            out[0]["content"] = SYSTEM + ("\n\n" + text if text else "")
        return out
    return [{"role": "system", "content": SYSTEM}] + out
