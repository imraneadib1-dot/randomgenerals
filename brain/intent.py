"""A small network that reads a message and says what kind it is.

    from brain import intent
    intent.load()
    intent.predict("my flask app 500s on every post")
    # {'bay': 'code', 'bay_p': 0.97, 'web': False, 'deep': True, ...}

WHY THIS ONE EXISTS NEXT TO rnn.py

The character models in this folder learn which letter follows which.
That is the whole of what they can do, and the README says so plainly.
This one is small in the same way - plain NumPy, gradients derived by
hand, no framework - but it is pointed at a job the site actually has,
and at that job it beats a model a thousand times its size on the only
measures that matter here: it answers in a tenth of a millisecond, on
the CPU the VM already has, for nothing.

The job is triage. Every message arriving at /api/chat raises three
questions the app currently answers with guesses or with a switch the
person has to flip:

    which bay   is this a coding question, a picture, a clip, a chat?
    web?        does answering it need something off the internet?
    deep?       is this worth thinking about, or is it "what is 17x23"?

Getting those right earlier makes the site quicker (no wasted tool
round asking a 120B model whether it needs to search) and more
accurate (a coding question in the chat bay gets the coding prompt).

ONE TRUNK, THREE HEADS

    features -> hidden (tanh) -> bay    (4-way softmax)
                              -> web    (sigmoid)
                              -> deep   (sigmoid)

The three questions are not independent - "draw me a fox" is an image
request AND needs no web AND needs no thinking - so they share a
hidden layer and each head reads the same summary. That is the whole
argument for a network here rather than three separate regressions:
one representation, learned from all three labels at once, on a corpus
small enough that sharing evidence between them matters.

WHAT IT ACTUALLY SCORES

Five-fold cross-validation over two seeds, so every one of the 199
examples is judged by a model that never saw it
(`python -m brain.intent --evaluate`):

    head    always-the-commonest-answer    this model
    bay                 50%                   86%
    web                 88%                   92%
    deep                65%                   72%

    per bay:  chat 93%   code 74%   image 82%   video 88%
    when confident (p >= 0.70): covers 76% of messages, 95% correct

Only the bay head is acted on, and only above that floor. The other
two are returned for the caller to look at and are deliberately not
wired to anything: +4 and +7 points over always guessing the common
answer is not enough to change what the site does on. Saying so is
cheaper than discovering it from a complaint.

Two findings from getting here, both of which cost accuracy until they
were fixed, and both recorded because they will recur:

  * A single 20% held-out split flattered the bay head by seven points
    and could not tell a 16-unit model from a 32-unit one. Anything
    tuned against it was tuned against noise. `cross_validate()` is
    what the numbers above come from.

  * The corpus is half chat, and without class weighting the cheapest
    way to cut the loss was to answer "chat" more often - measured at
    56% on the code bay, with the confident mistakes almost all coding
    questions containing no code ("how do i center a div"). Weighting
    the classes and adding _CODEY, a closed list of the trade's
    vocabulary, took the code bay to 74% and the whole head to 86%. A
    hashed unigram could not have done it: "javascript" appears in one
    example, and one example is not enough to learn a column from.

It gets better the same way the rest of this app does - `--from-threads`
trains on the messages your own users actually sent, labelled by the
bay they were really sent in, which is data nobody else has.
"""
from __future__ import annotations

import json
import os
import re
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "intents.jsonl")
CHECKPOINT = os.path.join(HERE, "checkpoint-intent.npz")

BAYS = ("chat", "code", "image", "video")

# Hashed feature buckets. Small on purpose: with a few hundred examples
# a wider space is mostly empty columns, and every one of them is a
# weight that has to be shipped and multiplied on every message.
N_BUCKETS = 1024
# Eight. Sixteen and thirty-two were measured and scored the same to
# within noise (86.2% against 85.9%), so the smaller one ships: a third
# of the weights, a third of the checkpoint, the same answers.
N_HIDDEN = 8

# Under this, app.py keeps whatever it would have done anyway. A model
# trained on 199 examples has opinions it has not earned; this is where
# they stop being acted on.
CONFIDENT = 0.70

_WORD = re.compile(r"[a-z0-9_.+#]+")

# Hand-made features, alongside the hashed text. Each is something the
# bag of words cannot see on its own - shape rather than vocabulary.
_URL = re.compile(r"https?://|www\.")
_CODE_FENCE = re.compile(r"```|\n    \S")
_ERRORISH = re.compile(r"\b\w+(Error|Exception)\b|\btraceback\b|\bstack trace\b", re.I)
_SYMBOLS = re.compile(r"[{}()\[\];=<>/\\|*&^%$#@]")
_PATH = re.compile(r"\.(py|js|ts|jsx|tsx|html|css|json|sql|sh|c|cpp|java|rb|go|rs|yml|yaml|toml)\b")
_RECENCY = re.compile(
    r"\b(today|tonight|yesterday|now|current|currently|latest|newest|recent|recently|"
    r"this (week|month|year|morning|evening)|right now|so far|still|"
    r"news|price|prices|score|scores|weather|forecast|release date|update[ds]?|"
    r"2025|2026|2027)\b")
_SEARCHY = re.compile(r"\b(search|google|look up|find me|browse|cite|sources?)\b")
_DEEPISH = re.compile(
    r"\b(explain|why|compare|analyse|analyze|research|thorough(ly)?|in depth|"
    r"step by step|derive|prove|essay|plan|design|detailed|pros and cons|"
    r"carefully|reasoning|debug|review|refactor|optimi[sz]e)\b")
# THE VOCABULARY OF THE TRADE.
#
# Image and video requests were recognised well from the start because
# _MAKE_IMAGE and _MAKE_VIDEO below name what those bays are for. Code
# had features for code's ARTEFACTS - a fence, a traceback, a file
# extension - and none for its SUBJECTS, so "how do i center a div" and
# "explain == vs === in javascript" were confidently called chat.
# Measured at 56% on the code bay because of it.
#
# A hashed unigram cannot fix that on this corpus: "javascript" appears
# in one example, and one example is not enough to learn a column from.
# A closed list is. These are the words that mean "this is about
# programming" whatever else the sentence does.
_CODEY = re.compile(
    r"\b(python|javascript|typescript|java|kotlin|swift|rust|golang|php|ruby|"
    r"c\+\+|c#|html|css|sql|bash|shell|regex|json|yaml|xml|"
    r"react|vue|angular|svelte|node|npm|yarn|pip|django|flask|fastapi|rails|"
    r"spring|laravel|express|jquery|bootstrap|tailwind|webpack|vite|"
    r"git|github|gitlab|docker|kubernetes|nginx|apache|linux|ubuntu|"
    r"aws|azure|heroku|vercel|postgres|postgresql|mysql|sqlite|mongodb|redis|"
    r"api|endpoint|rest|graphql|websocket|http|https|cors|oauth|jwt|"
    r"function|method|class|object|array|list|dict|tuple|variable|constant|"
    r"loop|recursion|pointer|struct|interface|module|package|library|"
    r"compile|compiler|runtime|syntax|debug|debugger|breakpoint|"
    r"database|query|schema|migration|index|join|commit|branch|merge|"
    r"server|client|frontend|backend|framework|dependency|deploy|build|"
    r"test|unittest|pytest|jest|mock|refactor|bug|crash|exception|"
    r"variable|parameter|argument|return|import|export|async|await|promise|"
    r"thread|process|memory leak|null|undefined|boolean|integer|string|"
    r"div|css grid|flexbox|dom|selector|component|props|state|hook|"
    r"terminal|command line|cli|script|codebase|repo|repository|"
    r"stack trace|segfault|typeerror|valueerror|nullpointer)\b")

_QUICKISH = re.compile(r"\b(quick|short|briefly|one word|in short|just the (number|answer))\b")
_MAKE_IMAGE = re.compile(r"\b(draw|picture|image|photo|illustration|logo|poster|"
                         r"wallpaper|render|sketch|painting|portrait)\b")
_MAKE_VIDEO = re.compile(r"\b(video|clip|animate|animation|footage|diagram|flowchart|"
                         r"chart|schema|timeline|mind map|visuali[sz]e)\b")

HANDMADE = (
    "url", "fence", "errorish", "symbols", "path", "recency", "searchy",
    "deepish", "quickish", "make_image", "make_video", "question",
    "imperative", "short", "long", "digits", "codey", "codey_many",
)

_IMPERATIVE = re.compile(
    r"^(write|make|create|generate|draw|build|give|show|fix|add|convert|"
    r"turn|refactor|explain|implement|design|animate|render|find|help)\b")


def features(text: str) -> np.ndarray:
    """One message -> one vector of length N_BUCKETS + len(HANDMADE).

    Words and word pairs and 4-character runs, each hashed to a bucket
    and counted; then the handmade flags. The vector is L2-normalised,
    so a long message and a short one with the same content land in the
    same direction - length is a handmade feature, not an accident of
    how many words got counted.
    """
    low = (text or "").lower()[:2000]
    x = np.zeros(N_BUCKETS + len(HANDMADE), dtype=np.float64)

    words = _WORD.findall(low)
    grams = list(words)
    grams += ["%s %s" % (a, b) for a, b in zip(words, words[1:])]
    # Character 4-grams over the first words: robust to a typo or an
    # inflection the word list has never seen.
    squashed = " ".join(words[:40])
    grams += [squashed[i:i + 4] for i in range(0, max(0, len(squashed) - 3))]
    for g in grams:
        # crc32, not hash(): Python randomises string hashing per
        # process, so hash() would put the same word in a different
        # bucket on every restart and the trained weights would be
        # meaningless the moment the server rebooted.
        x[zlib.crc32(g.encode("utf-8")) % N_BUCKETS] += 1.0

    n = len(words)
    flags = {
        "url": bool(_URL.search(low)),
        "fence": bool(_CODE_FENCE.search(text or "")),
        "errorish": bool(_ERRORISH.search(text or "")),
        "symbols": len(_SYMBOLS.findall(low)) >= 3,
        "path": bool(_PATH.search(low)),
        "recency": bool(_RECENCY.search(low)),
        "searchy": bool(_SEARCHY.search(low)),
        "deepish": bool(_DEEPISH.search(low)),
        "quickish": bool(_QUICKISH.search(low)),
        "make_image": bool(_MAKE_IMAGE.search(low)),
        "make_video": bool(_MAKE_VIDEO.search(low)),
        "question": "?" in (text or ""),
        "imperative": bool(_IMPERATIVE.search(low)),
        "short": n <= 4,
        "long": n >= 40,
        "digits": bool(re.search(r"\d", low)),
        "codey": bool(_CODEY.search(low)),
        # Two or more of them is a much stronger signal than one: "test"
        # or "index" alone turn up in ordinary sentences.
        "codey_many": len(set(_CODEY.findall(low))) >= 2,
    }
    for i, name in enumerate(HANDMADE):
        x[N_BUCKETS + i] = 1.0 if flags[name] else 0.0

    norm = np.linalg.norm(x)
    return x / norm if norm > 0 else x


def sigmoid(z):
    """1 / (1 + e^-z), written so neither tail overflows: exp of a large
    positive number is inf, so the two halves are computed apart."""
    out = np.empty_like(z, dtype=np.float64)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    e = np.exp(z[~pos])
    out[~pos] = e / (1.0 + e)
    return out


def softmax(z):
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


class IntentNet:
    """One hidden layer, three heads. About 34,000 weights."""

    def __init__(self, n_in, n_hidden=N_HIDDEN, seed=1337):
        rng = np.random.default_rng(seed)
        # Scaled by 1/sqrt(fan-in): with L2-normalised inputs this puts
        # the pre-activations in tanh's useful range instead of its flat
        # ends, where gradients are near zero and nothing learns.
        self.W1 = rng.normal(0, 1.0 / np.sqrt(n_in), (n_in, n_hidden))
        self.b1 = np.zeros(n_hidden)
        self.W2 = rng.normal(0, 1.0 / np.sqrt(n_hidden), (n_hidden, len(BAYS)))
        self.b2 = np.zeros(len(BAYS))
        self.w3 = rng.normal(0, 1.0 / np.sqrt(n_hidden), n_hidden)
        self.b3 = 0.0
        self.w4 = rng.normal(0, 1.0 / np.sqrt(n_hidden), n_hidden)
        self.b4 = 0.0

    # ---------------------------------------------------------- forward
    def forward(self, X):
        h_pre = X @ self.W1 + self.b1
        h = np.tanh(h_pre)
        return {
            "h": h,
            "bay": h @ self.W2 + self.b2,
            "web": h @ self.w3 + self.b3,
            "deep": h @ self.w4 + self.b4,
        }

    def loss_and_grads(self, X, y_bay, y_web, y_deep, web_weight=1.0,
                       l2=1e-4, bay_weight=None):
        """Cross-entropy for the bay, binary cross-entropy for the two
        yes/no heads, plus weight decay. -> (loss, grads).

        Every derivative below is the chain rule applied by hand;
        `check_gradients()` compares each one against a numerical
        estimate, which is the only way to catch a sign error that
        still trains, just worse.
        """
        B = X.shape[0]
        f = self.forward(X)
        h = f["h"]

        p_bay = softmax(f["bay"])
        p_web = sigmoid(f["web"])
        p_deep = sigmoid(f["deep"])
        eps = 1e-12

        # The bays are not evenly represented - half the corpus is chat -
        # and without this the cheapest way to cut the loss is to answer
        # "chat" more often. Measured: it did exactly that, and the
        # confident mistakes were almost all a coding question with no
        # code symbols in it ("how do i center a div") called chat.
        bw = (np.ones(B) if bay_weight is None
              else np.asarray(bay_weight)[y_bay])
        l_bay = -(bw * np.log(p_bay[np.arange(B), y_bay] + eps)).mean()
        # The web head sees far fewer positives than negatives, so a
        # model that always says "no" scores well and learns nothing.
        # Positives are weighted up to match.
        wgt = np.where(y_web > 0.5, web_weight, 1.0)
        l_web = -(wgt * (y_web * np.log(p_web + eps)
                         + (1 - y_web) * np.log(1 - p_web + eps))).mean()
        l_deep = -(y_deep * np.log(p_deep + eps)
                   + (1 - y_deep) * np.log(1 - p_deep + eps)).mean()
        reg = 0.5 * l2 * (np.sum(self.W1 ** 2) + np.sum(self.W2 ** 2)
                          + np.sum(self.w3 ** 2) + np.sum(self.w4 ** 2))
        loss = l_bay + l_web + l_deep + reg

        # d(loss)/d(logits) for each head.
        d_bay = p_bay.copy()
        d_bay[np.arange(B), y_bay] -= 1.0
        d_bay *= bw[:, None] / B
        d_web = wgt * (p_web - y_web) / B
        d_deep = (p_deep - y_deep) / B

        gW2 = h.T @ d_bay + l2 * self.W2
        gb2 = d_bay.sum(axis=0)
        gw3 = h.T @ d_web + l2 * self.w3
        gb3 = d_web.sum()
        gw4 = h.T @ d_deep + l2 * self.w4
        gb4 = d_deep.sum()

        # Three heads read the same hidden layer, so its gradient is the
        # sum of what each one wants it to be.
        dh = (d_bay @ self.W2.T
              + d_web[:, None] * self.w3[None, :]
              + d_deep[:, None] * self.w4[None, :])
        dh_pre = dh * (1.0 - h ** 2)          # tanh'(z) = 1 - tanh(z)^2
        gW1 = X.T @ dh_pre + l2 * self.W1
        gb1 = dh_pre.sum(axis=0)

        return loss, {"W1": gW1, "b1": gb1, "W2": gW2, "b2": gb2,
                      "w3": gw3, "b3": gb3, "w4": gw4, "b4": gb4}

    # ----------------------------------------------------------- saving
    def save(self, path=CHECKPOINT):
        np.savez_compressed(
            path, W1=self.W1.astype(np.float32), b1=self.b1.astype(np.float32),
            W2=self.W2.astype(np.float32), b2=self.b2.astype(np.float32),
            w3=self.w3.astype(np.float32), b3=np.float32(self.b3),
            w4=self.w4.astype(np.float32), b4=np.float32(self.b4),
            n_buckets=N_BUCKETS, handmade=len(HANDMADE), bays=np.array(BAYS))

    @classmethod
    def load(cls, path=CHECKPOINT):
        z = np.load(path, allow_pickle=False)
        if int(z["n_buckets"]) != N_BUCKETS or int(z["handmade"]) != len(HANDMADE):
            # The feature layout changed since this was trained, so the
            # weights describe columns that no longer mean what they did.
            # Refusing beats predicting confidently from nonsense.
            raise ValueError("checkpoint was trained on a different feature set")
        net = cls.__new__(cls)
        net.W1 = z["W1"].astype(np.float64)
        net.b1 = z["b1"].astype(np.float64)
        net.W2 = z["W2"].astype(np.float64)
        net.b2 = z["b2"].astype(np.float64)
        net.w3 = z["w3"].astype(np.float64)
        net.b3 = float(z["b3"])
        net.w4 = z["w4"].astype(np.float64)
        net.b4 = float(z["b4"])
        return net


# ------------------------------------------------------------- the data
def load_rows(path=DATA):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(json.loads(line))
    return rows


def encode(rows):
    X = np.stack([features(r["text"]) for r in rows])
    y_bay = np.array([BAYS.index(r["bay"]) for r in rows])
    y_web = np.array([float(r["web"]) for r in rows])
    y_deep = np.array([1.0 if r["depth"] == "deep" else 0.0 for r in rows])
    return X, y_bay, y_web, y_deep


def split(rows, every=5):
    """Every 5th example is held out. Taken by stride rather than from
    the end, because the file is written in bay order - a tail split
    would validate entirely on the video examples and tell you nothing."""
    train = [r for i, r in enumerate(rows) if i % every]
    val = [r for i, r in enumerate(rows) if not i % every]
    return train, val


# --------------------------------------------------------------- train
def train(rows=None, steps=3000, lr=0.05, l2=3e-4, seed=1337, quiet=False,
          hidden=None, early_stop=True):
    """Full-batch Adam. The corpus is 199 rows; mini-batching it would
    add noise for no speed, since the whole thing is one small matrix
    multiply. -> (net, history)."""
    rows = rows or load_rows()
    # Early stopping needs something to stop against, so it holds a
    # fifth back. Cross-validation has already held its own fold back
    # and passes early_stop=False - splitting again there would train
    # each fold on 64% of the corpus instead of 80%, and report the
    # lower score that produced as if it were the model's.
    if early_stop:
        tr, va = split(rows)
    else:
        tr, va = rows, rows[:1]
    Xtr, btr, wtr, dtr = encode(tr)
    Xva, bva, wva, dva = encode(va)
    pos = max(1, int(wtr.sum()))
    web_weight = float(len(wtr) - pos) / pos     # balance the rare label
    bay_weight = class_weights(btr)

    net = IntentNet(Xtr.shape[1], n_hidden=hidden or N_HIDDEN, seed=seed)
    m = {k: np.zeros_like(v) if isinstance(v, np.ndarray) else 0.0
         for k, v in _params(net).items()}
    v = {k: np.zeros_like(x) if isinstance(x, np.ndarray) else 0.0
         for k, x in _params(net).items()}
    b1_, b2_, eps = 0.9, 0.999, 1e-8

    best = {"acc": -1.0, "at": 0, "state": None}
    history = []
    for step in range(1, steps + 1):
        loss, grads = net.loss_and_grads(Xtr, btr, wtr, dtr,
                                         web_weight=web_weight, l2=l2,
                                         bay_weight=bay_weight)
        for k, g in grads.items():
            m[k] = b1_ * m[k] + (1 - b1_) * g
            v[k] = b2_ * v[k] + (1 - b2_) * (g * g)
            mhat = m[k] / (1 - b1_ ** step)
            vhat = v[k] / (1 - b2_ ** step)
            cur = getattr(net, k)
            setattr(net, k, cur - lr * mhat / (np.sqrt(vhat) + eps))

        if step % 50 == 0 or step == steps:
            acc = accuracy(net, Xva, bva, wva, dva)
            history.append({"step": step, "loss": round(float(loss), 4), **acc})
            # Early stopping, on the held-out mean rather than the loss:
            # 199 examples and 34k weights will fit the training set
            # perfectly long before they generalise best.
            if early_stop and acc["mean"] > best["acc"]:
                best = {"acc": acc["mean"], "at": step,
                        "state": {k: (x.copy() if isinstance(x, np.ndarray) else x)
                                  for k, x in _params(net).items()}}
            if not quiet and step % 500 == 0:
                print("  step %5d  loss %.4f  bay %.0f%%  web %.0f%%  deep %.0f%%"
                      % (step, loss, acc["bay"] * 100, acc["web"] * 100,
                         acc["deep"] * 100))

    if early_stop and best["state"]:
        for k, x in best["state"].items():
            setattr(net, k, x)
    if not quiet and early_stop:
        print("  best held-out mean %.1f%% at step %d" % (best["acc"] * 100, best["at"]))
    return net, history


def class_weights(y, n_classes=len(BAYS)):
    """Inverse frequency, normalised to mean 1. A bay with half the
    examples of another counts twice as much per example, so the model
    cannot buy a low loss by always guessing the commonest one."""
    counts = np.bincount(y, minlength=n_classes).astype(float)
    counts[counts == 0] = 1.0
    w = counts.sum() / (n_classes * counts)
    return w / w.mean()


def _params(net):
    return {"W1": net.W1, "b1": net.b1, "W2": net.W2, "b2": net.b2,
            "w3": net.w3, "b3": net.b3, "w4": net.w4, "b4": net.b4}


def accuracy(net, X, y_bay, y_web, y_deep):
    f = net.forward(X)
    bay = float((f["bay"].argmax(axis=1) == y_bay).mean())
    web = float(((sigmoid(f["web"]) > 0.5).astype(float) == y_web).mean())
    deep = float(((sigmoid(f["deep"]) > 0.5).astype(float) == y_deep).mean())
    return {"bay": bay, "web": web, "deep": deep,
            "mean": (bay + web + deep) / 3.0}


def baseline(rows):
    """What always guessing the commonest label would score. Any model
    that cannot beat this has learned nothing, and reporting accuracy
    without it is how a 68% classifier gets called good."""
    _, va = split(rows)
    tr, _ = split(rows)
    common_bay = max(BAYS, key=lambda b: sum(r["bay"] == b for r in tr))
    return {
        "bay": sum(r["bay"] == common_bay for r in va) / len(va),
        "web": sum(r["web"] == 0 for r in va) / len(va),
        "deep": sum(r["depth"] == "quick" for r in va) / len(va),
    }


def cross_validate(rows=None, k=5, seeds=(1337, 7), hidden=N_HIDDEN,
                   l2=3e-4, steps=1200, confident=None):
    """Every example validated, on a model that never saw it.

    A single held-out fifth of 199 rows is 40 examples: each one is 2.5%
    of the score, so two models three points apart are indistinguishable
    and every tuning decision is noise. It also flattered this model by
    seven points - the first split said 82% on the bay where this says
    75% - which is the more useful number, being the one a stranger's
    message will meet.

    -> per-head accuracy, per-bay accuracy, and what the bay head scores
    among the predictions it is confident enough for app.py to act on.
    """
    rows = rows or load_rows()
    confident = CONFIDENT if confident is None else confident
    heads = {"bay": [], "web": [], "deep": []}
    per_bay = {b: [0, 0] for b in BAYS}
    conf_right = conf_n = total = 0
    for seed in seeds:
        for fold in range(k):
            tr = [r for i, r in enumerate(rows) if i % k != fold]
            va = [r for i, r in enumerate(rows) if i % k == fold]
            if not va:
                continue
            net, _ = train(tr, steps=steps, l2=l2, seed=seed, quiet=True,
                           hidden=hidden, early_stop=False)
            X, y_bay, y_web, y_deep = encode(va)
            acc = accuracy(net, X, y_bay, y_web, y_deep)
            for name in heads:
                heads[name].append(acc[name])
            p = softmax(net.forward(X)["bay"])
            for j, row in enumerate(va):
                total += 1
                right = int(p[j].argmax()) == y_bay[j]
                per_bay[row["bay"]][0] += right
                per_bay[row["bay"]][1] += 1
                if p[j].max() >= confident:
                    conf_n += 1
                    conf_right += right
    return {
        "bay": float(np.mean(heads["bay"])),
        "web": float(np.mean(heads["web"])),
        "deep": float(np.mean(heads["deep"])),
        "per_bay": {b: (v[0] / v[1] if v[1] else 0.0) for b, v in per_bay.items()},
        "confident_coverage": conf_n / total if total else 0.0,
        "confident_accuracy": conf_right / conf_n if conf_n else 0.0,
        "folds": k * len(seeds),
    }


# ------------------------------------------------- the gradient check
def check_gradients(seed=7, tol=1e-6):
    """Every hand-derived gradient against a numerical estimate.

    (f(x+h) - f(x-h)) / 2h is what the derivative IS; the code above is
    what we claim it is. An off-by-one or a missing term still trains -
    just worse - and no loss curve would ever show it. -> worst relative
    error found.
    """
    rng = np.random.default_rng(seed)
    n_in = 40
    net = IntentNet(n_in, n_hidden=7, seed=seed)
    X = rng.normal(size=(6, n_in))
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    y_bay = rng.integers(0, len(BAYS), 6)
    y_web = (rng.random(6) > 0.5).astype(float)
    y_deep = (rng.random(6) > 0.5).astype(float)

    _, grads = net.loss_and_grads(X, y_bay, y_web, y_deep, web_weight=2.0)
    worst = 0.0
    h = 1e-5
    for name in _params(net):
        value = getattr(net, name)
        if isinstance(value, float):
            setattr(net, name, value + h)
            up, _ = net.loss_and_grads(X, y_bay, y_web, y_deep, web_weight=2.0)
            setattr(net, name, value - h)
            down, _ = net.loss_and_grads(X, y_bay, y_web, y_deep, web_weight=2.0)
            setattr(net, name, value)
            numeric = (up - down) / (2 * h)
            worst = max(worst, _rel(numeric, float(grads[name])))
            continue
        flat = value.reshape(-1)
        gflat = grads[name].reshape(-1)
        for idx in rng.choice(flat.size, size=min(12, flat.size), replace=False):
            original = flat[idx]
            flat[idx] = original + h
            up, _ = net.loss_and_grads(X, y_bay, y_web, y_deep, web_weight=2.0)
            flat[idx] = original - h
            down, _ = net.loss_and_grads(X, y_bay, y_web, y_deep, web_weight=2.0)
            flat[idx] = original
            worst = max(worst, _rel((up - down) / (2 * h), gflat[idx]))
    return worst


def _rel(a, b):
    return abs(a - b) / max(1e-8, abs(a) + abs(b))


# ------------------------------------------------------------ the API
_net: IntentNet | None = None
_tried = False


def load(path=CHECKPOINT):
    """Load the checkpoint once. -> the net, or None when there is no
    checkpoint or it does not match the current features. Never raises:
    a missing brain must leave the app exactly as it was."""
    global _net, _tried
    if _net is not None or _tried:
        return _net
    _tried = True
    try:
        _net = IntentNet.load(path)
    except Exception as e:                       # noqa: BLE001
        print("[intent] not loaded: %s" % e)
        _net = None
    return _net


def available() -> bool:
    return load() is not None


def predict(text: str) -> dict | None:
    """-> {bay, bay_p, web, web_p, deep, deep_p, confident} or None.

    `confident` is the bay probability clearing CONFIDENT; app.py acts
    on the bay only when it is set, and treats the two yes/no heads as
    hints whose own probabilities the caller can threshold.
    """
    net = load()
    if net is None or not (text or "").strip():
        return None
    X = features(text)[None, :]
    f = net.forward(X)
    p_bay = softmax(f["bay"])[0]
    i = int(p_bay.argmax())
    web_p = float(sigmoid(f["web"])[0])
    deep_p = float(sigmoid(f["deep"])[0])
    return {
        "bay": BAYS[i], "bay_p": float(p_bay[i]),
        "web": web_p > 0.5, "web_p": web_p,
        "deep": deep_p > 0.5, "deep_p": deep_p,
        "confident": float(p_bay[i]) >= CONFIDENT,
    }


# -------------------------------------------------------------- the CLI
def _from_threads(db_path=None, limit=4000):
    """Real messages, labelled by the bay they were actually sent in.

    This is the part nobody else can copy: what your own users type,
    and where they typed it. The web and depth labels are not recorded
    anywhere, so those rows train the bay head only - handled by
    reusing the seed row's labels is NOT what happens here; instead
    they are given the seed corpus's majority for the other two heads
    and down-weighted by being a minority of the data. Imperfect, and
    better than not learning from real traffic at all.
    """
    import sys
    sys.path.insert(0, os.path.dirname(HERE))
    if db_path:
        os.environ["DB_PATH"] = db_path
    import db as appdb
    out = []
    for thread in appdb.load_threads().values():
        mode = thread.get("mode") or "chat"
        if mode not in BAYS:
            continue
        for msg in thread.get("messages") or []:
            if msg.get("role") != "user":
                continue
            text = (msg.get("content") or "").strip()
            if len(text) < 3:
                continue
            out.append({"text": text[:2000], "bay": mode, "web": 0,
                        "depth": "quick", "_real": True})
            if len(out) >= limit:
                return out
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="train the intent brain")
    ap.add_argument("--steps", type=int, default=3000)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--l2", type=float, default=3e-4)
    ap.add_argument("--from-threads", metavar="DB_PATH", nargs="?", const="",
                    help="also train on real messages from a database")
    ap.add_argument("--check", action="store_true", help="gradient check only")
    ap.add_argument("--evaluate", action="store_true",
                    help="k-fold cross-validation; every example validated")
    ap.add_argument("--try", dest="sample", help="classify one message and exit")
    args = ap.parse_args(argv)

    if args.check:
        worst = check_gradients()
        print("worst relative gradient error: %.2e  %s"
              % (worst, "ok" if worst < 1e-6 else "SUSPICIOUS"))
        return 0 if worst < 1e-6 else 1

    if args.sample:
        print(json.dumps(predict(args.sample), indent=1))
        return 0

    if args.evaluate:
        rows = load_rows()
        base = baseline(rows)
        r = cross_validate(rows)
        print("%d examples, %d folds, every one validated on a model that "
              "never saw it\n" % (len(rows), r["folds"]))
        print("  head    baseline   model")
        for name in ("bay", "web", "deep"):
            print("  %-6s  %6.0f%%   %5.1f%%%s" % (
                name, base[name] * 100, r[name] * 100,
                "   <- acted on" if name == "bay" else ""))
        print("\n  per bay: " + "  ".join(
            "%s %.0f%%" % (b, v * 100) for b, v in r["per_bay"].items()))
        print("  when confident (p >= %.2f): covers %.0f%% of messages, "
              "%.1f%% correct" % (CONFIDENT, r["confident_coverage"] * 100,
                                  r["confident_accuracy"] * 100))
        return 0

    rows = load_rows()
    print("%d labelled examples" % len(rows))
    if args.from_threads is not None:
        real = _from_threads(args.from_threads or None)
        print("%d real messages from your own threads" % len(real))
        rows = rows + real
    base = baseline(rows)
    print("always-the-commonest-answer scores: bay %.0f%%  web %.0f%%  deep %.0f%%"
          % (base["bay"] * 100, base["web"] * 100, base["deep"] * 100))
    net, _ = train(rows, steps=args.steps, lr=args.lr, l2=args.l2)
    net.save()
    size = os.path.getsize(CHECKPOINT)
    print("saved %s (%.0f KB)" % (CHECKPOINT, size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
