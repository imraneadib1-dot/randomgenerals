"""Part 6 - serving the trained brain to the web app.

This is the bridge between brain/ and app.py. It loads a checkpoint once,
keeps it in memory, and streams characters out one at a time so the browser
sees text appear as it is generated. This is the only model app.py talks
to - no external API, no hosted provider, nothing that needs a key.

A word on what this model actually is, because the chat window invites a
misunderstanding. It is a ~200,000-parameter character model trained on one
book. It has no notion of questions, answers, facts, or instructions. It
learned which letter tends to follow which, and nothing else. Given your
message as an opening, it continues in the voice of its training text.

That is genuinely all a network this size can do, and it is worth seeing
plainly: everything a large model does beyond this - answering, reasoning,
following instructions - comes from being thousands of times larger and
trained on a corpus millions of times bigger. The machinery underneath is
what you see in model.py and rnn.py, and not much more.
"""
import os
import threading

# BLAS thread caps, set before NumPy loads. Generation multiplies a single
# row at a time - the smallest matrices in the whole project - so thread
# co-ordination costs far more than the arithmetic. See train.py.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import numpy as np  # noqa: E402,F401  (imported for its side effect above)

from brain.data import CharTokenizer  # noqa: E402
from brain.train import load_checkpoint, ckpt_path  # noqa: E402


# Loading the checkpoint takes long enough to notice, so do it once on the
# first request and hold it. The lock stops two simultaneous requests from
# both deciding to load it.
_lock = threading.Lock()
_loaded = {}


def available(arch="rnn"):
    """Is there a trained brain on disk to serve?"""
    return os.path.exists(ckpt_path(arch))


def load(arch="rnn"):
    """Return (model, tokenizer, step), loading and caching on first call."""
    if arch in _loaded:
        return _loaded[arch]
    with _lock:
        if arch not in _loaded:                # re-check inside the lock
            found = load_checkpoint(arch)
            if not found:
                raise FileNotFoundError(
                    f"No trained brain at {ckpt_path(arch)}. "
                    f"Run: python brain/train.py --arch {arch}"
                )
            model, chars, step, _best_val = found
            _loaded[arch] = (model, CharTokenizer.from_chars(chars), step)
    return _loaded[arch]


def describe(arch="rnn"):
    """Short human-readable summary for the model picker."""
    if not available(arch):
        return None
    model, tok, step = load(arch)
    return {
        "params": model.num_params(),
        "vocab": tok.vocab_size,
        "step": step,
        "context": model.context_size,
    }


# A tiny rule-based front end for small talk. Be clear about what this is:
# it is NOT the network answering - a character model has no concept of a
# question, so "hello" is just as arbitrary a prompt to it as any other
# string, and it will free-associate from whatever training text starts
# similarly. These are canned replies for the handful of things a person
# says before they say anything the model could actually continue from.
# Everything that isn't a recognised greeting still goes to the real model.
_SMALL_TALK = {
    ("hi", "hello", "hey", "hiya", "yo", "sup"):
        "Hey! I'm not a general chatbot - I'm a small text model trained "
        "from scratch on a handful of books. Give me the start of a "
        "sentence and I'll continue it in their style.",
    ("how are you", "how are you doing", "how's it going", "hows it going",
     "whats up", "what's up"):
        "I don't have a state to report - I'm a next-character predictor, "
        "not something that experiences a day. Try starting a sentence and "
        "I'll pick up from there.",
    ("who are you", "what are you", "what is this"):
        "A character-level neural network, ~200k parameters, trained from "
        "scratch on a few public-domain books - see brain/ in this project. "
        "No external API, no big model behind the curtain.",
    ("what can you do", "what do you do", "help"):
        "Give me the opening of a sentence and I'll continue it in the "
        "voice of whatever it's trained on (old prose, Victorian novels, "
        "some tech writing). I can't answer questions, write working code, "
        "or make images - that needs a model orders of magnitude bigger.",
    ("thanks", "thank you", "ty"):
        "You're welcome.",
    ("bye", "goodbye", "see you", "cya"):
        "Bye.",
}


def _edit_distance(a, b):
    """Levenshtein distance - minimum single-character inserts, deletes,
    and substitutions to turn `a` into `b`. Standard DP, O(len(a)*len(b)),
    which is trivial at the length of a chat message."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(
                prev[j] + 1,        # delete from a
                cur[j - 1] + 1,     # insert into a
                prev[j - 1] + (ca != cb),  # substitute (0 cost if equal)
            )
        prev = cur
    return prev[-1]


def _small_talk_reply(message):
    """Return a canned reply for recognised small talk, or None.

    Exact match first; if that fails, fall back to the closest trigger by
    edit distance, but only accept it within a threshold scaled to length.
    Without that scaling either short triggers swallow unrelated short
    messages ("hi" fuzzy-matching "hey" is fine, "no" fuzzy-matching "go"
    is not) or long ones never tolerate a real typo.
    """
    norm = message.strip().lower().strip("!?. ")
    if not norm:
        return None

    for triggers, reply in _SMALL_TALK.items():
        if norm in triggers:
            return reply

    best_reply, best_dist, best_len = None, None, None
    for triggers, reply in _SMALL_TALK.items():
        for trig in triggers:
            if abs(len(norm) - len(trig)) > max(3, len(trig) // 3):
                continue                    # too different in length to bother
            d = _edit_distance(norm, trig)
            if best_dist is None or d < best_dist:
                best_reply, best_dist, best_len = reply, d, len(trig)

    if best_dist is not None:
        threshold = max(1, best_len // 5)   # ~1 typo per 5 chars
        if best_dist <= threshold:
            return best_reply
    return None


def last_user_message(history):
    """Pull the newest user turn out of app.py's message list.

    The system prompt and the conversation history are meaningless to a
    character model - it cannot follow an instruction it has no concept of.
    Feeding them in would just prime it with text in the wrong voice, so we
    take the latest user message alone and use that as the opening.
    """
    for msg in reversed(history):
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()
    return ""


def stream_reply(model_name, history, n_chars=420, temperature=0.5,
                 top_k=6, seed=None):
    """Generator of text chunks, matching app.py's provider interface.

    `model_name` selects the architecture ("rnn" or "mlp").

    temperature/top_k are tuned conservative on purpose. A character-level
    model sampled too freely invents non-words ("avaised", "astily") -
    there's no vocabulary to constrain it to, only whatever letter pattern
    won at that step. Turning both down trades a little variety for output
    that is reliably real English, which matters more in a chat window.
    """
    prompt = last_user_message(history)

    canned = _small_talk_reply(prompt)
    if canned is not None:
        yield canned
        return

    name = str(model_name)
    arch = "mlp" if name.endswith("mlp") else "rnn2" if name.endswith("rnn2") else "rnn"
    try:
        model, tok, _ = load(arch)
    except FileNotFoundError as e:
        yield f"[{e}]"
        return

    # End the priming text on a word boundary. Without this the model picks
    # up mid-word - prime it with "the nature of water is" and it happily
    # continues "...isam", because as far as it can tell it is still in the
    # middle of a token. A trailing space tells it the word finished.
    if prompt and not prompt[-1].isspace():
        prompt += " "

    # Echo the prompt back first so it is obvious what the model was given
    # and where its own writing starts.
    if prompt:
        yield prompt

    if hasattr(model, "stream"):               # RNN - true streaming
        for ch in model.stream(tok, n_chars=n_chars, prompt=prompt,
                               temperature=temperature, top_k=top_k,
                               seed=seed):
            yield ch
    else:                                      # MLP - no streaming interface
        text = model.generate(tok, n_chars=n_chars, prompt=prompt,
                              temperature=temperature, seed=seed)
        for ch in text:
            yield ch


if __name__ == "__main__":
    import sys

    arch = sys.argv[1] if len(sys.argv) > 1 else "rnn"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "The nature of "
    if not available(arch):
        print(f"No checkpoint for '{arch}'. Train one first:")
        print(f"    python brain/train.py --arch {arch}")
        raise SystemExit(1)

    info = describe(arch)
    print(f"{arch}: {info['params']:,} parameters, "
          f"trained {info['step']:,} steps\n")
    print(prompt, end="", flush=True)
    model, tok, _ = load(arch)
    for ch in model.stream(tok, n_chars=400, prompt=prompt, temperature=0.75,
                           top_k=12):
        print(ch, end="", flush=True)
    print()
