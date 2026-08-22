"""Part 5b - stacking the link. More neurons wired to more neurons.

rnn.py links each step to its own past:

    h_t = tanh( x_t @ Wxh  +  h_{t-1} @ Whh  +  bh )

That is one recurrent layer. Nothing stops wiring a second one on top,
fed not by the raw character but by what the first layer already made of
it, at every step:

    h1_t = tanh( x_t  @ Wxh1 + h1_{t-1} @ Whh1 + bh1 )
    h2_t = tanh( h1_t @ Wxh2 + h2_{t-1} @ Whh2 + bh2 )

This is what "expanding the neuron links" means concretely: layer 1's
hidden units now connect forward to a whole second layer of hidden
units, in addition to their own recurrent self-connections - genuinely
more links, not just a wider version of the same one layer.

Why that might help rather than just cost more compute: layer 1 never
sees anything but raw characters and its own short-term memory of them,
so whatever it learns is necessarily low-level - the kind of thing a
single-layer net already learns (which letter tends to follow which).
Layer 2 never sees a character directly. Its only input is what layer 1
already condensed at that step, so any pattern it finds has to be built
*from* layer 1's patterns rather than from raw input - the same reason
stacking convolutional layers finds edges, then shapes, then objects,
instead of just finding edges twice as well.

The honest cost: backward through time now has to flow through two
recurrences and one extra link (layer 1 feeding layer 2's input at that
same step) instead of one. Measured on this corpus at n_hidden=512:
about 0.45s/step against the single layer's ~0.10s/step - roughly 4-5x
slower, not the naive 2x "twice the matmuls" guess, because layer 2's
input depends on the loop and can't be hoisted out of it the way layer
1's can (see the note in forward() below).
"""
import numpy as np

from brain.model import softmax


class CharRNN2:
    """A two-layer recurrent network that predicts the next character at
    every step - see the module docstring for what "two layers" buys
    over rnn.py's single one."""

    predicts_all_positions = True

    def __init__(self, vocab_size, context_size=64, n_embd=48, n_hidden=384,
                 seed=1337, dtype=np.float32):
        rng = np.random.default_rng(seed)
        self.vocab_size = vocab_size
        self.context_size = context_size
        self.n_embd = n_embd
        self.n_hidden = n_hidden
        self.dtype = dtype

        self.C = rng.normal(0, 0.1, (vocab_size, n_embd))
        self.Wxh1 = rng.normal(0, 1, (n_embd, n_hidden)) / np.sqrt(n_embd)
        # Both recurrent matrices start as a scaled identity, not noise -
        # "carry your state forward unchanged" is a sensible default memory
        # to deviate from. Start random and the state is scrambled at every
        # step before any learning happens, in both layers at once.
        self.Whh1 = np.eye(n_hidden) * 0.5 + rng.normal(
            0, 0.01, (n_hidden, n_hidden))
        self.bh1 = np.zeros(n_hidden)
        # Layer 2's input is layer 1's hidden state, not an embedding, so
        # this matrix is (n_hidden, n_hidden) rather than (n_embd, n_hidden).
        self.Wxh2 = rng.normal(0, 1, (n_hidden, n_hidden)) / np.sqrt(n_hidden)
        self.Whh2 = np.eye(n_hidden) * 0.5 + rng.normal(
            0, 0.01, (n_hidden, n_hidden))
        self.bh2 = np.zeros(n_hidden)
        self.Why = rng.normal(0, 1, (n_hidden, vocab_size)) / np.sqrt(n_hidden)
        self.by = np.zeros(vocab_size)

        for name in ("C", "Wxh1", "Whh1", "bh1", "Wxh2", "Whh2", "bh2",
                     "Why", "by"):
            setattr(self, name, getattr(self, name).astype(dtype))

        self._m = [np.zeros_like(p) for p in self.params]
        self._v = [np.zeros_like(p) for p in self.params]
        self._t = 0

    @property
    def params(self):
        return [self.C, self.Wxh1, self.Whh1, self.bh1,
                self.Wxh2, self.Whh2, self.bh2, self.Why, self.by]

    def num_params(self):
        return sum(p.size for p in self.params)

    # ---------------- forward ----------------
    def forward(self, x, h0=None):
        """x: (B, T) int ids -> logits (B, T, V).

        Layer 1's input side (x @ Wxh1 + bh1) doesn't depend on the
        recurrence, so - same trick as rnn.py - it's computed for all T
        steps in one big matmul before the loop starts. Layer 2's input
        (layer 1's hidden state) *does* depend on the loop, so unlike
        layer 1 it has no such shortcut: `h1_t @ Wxh2` has to happen
        inside the loop, one step at a time, because h1_t doesn't exist
        until that step of the loop has run.
        """
        B, T = x.shape
        H = self.n_hidden

        emb = self.C[x]                                     # (B, T, E)
        xh1 = np.ascontiguousarray(
            (emb @ self.Wxh1 + self.bh1).transpose(1, 0, 2))  # (T, B, H)

        h1s = np.zeros((T + 1, B, H), dtype=xh1.dtype)
        h2s = np.zeros((T + 1, B, H), dtype=xh1.dtype)
        if h0 is not None:
            h1s[0], h2s[0] = h0
        for t in range(T):
            h1s[t + 1] = np.tanh(xh1[t] + h1s[t] @ self.Whh1)
            h2s[t + 1] = np.tanh(
                h1s[t + 1] @ self.Wxh2 + h2s[t] @ self.Whh2 + self.bh2)

        logits = h2s[1:] @ self.Why + self.by                # (T, B, V)
        self._cache = (x, emb, h1s, h2s, logits)
        return logits.transpose(1, 0, 2)                     # (B, T, V)

    def loss(self, logits, y):
        """Average cross-entropy over every position in every sequence."""
        B, T, V = logits.shape
        p = softmax(logits.reshape(-1, V))
        truth = p[np.arange(B * T), y.reshape(-1)]
        return -np.log(np.clip(truth, 1e-12, None)).mean()

    # ---------------- backward ----------------
    def backward(self, y, clip=5.0):
        """Backpropagation through time, through both layers.

        Layer 2's hidden state answers to two futures, same as the
        single-layer case: its own prediction, and the state it hands to
        its next step. Layer 1's hidden state answers to *three*: its own
        recurrence, plus - the new part - layer 2 using it as input at
        this same timestep. `dh1` below is exactly that sum.
        """
        x, emb, h1s, h2s, logits = self._cache
        H, E = self.n_hidden, self.n_embd
        B, T, V = x.shape[0], x.shape[1], self.vocab_size

        y_tm = np.ascontiguousarray(y.T).reshape(-1)
        dlogits = softmax(logits.reshape(-1, V))
        dlogits[np.arange(T * B), y_tm] -= 1
        dlogits = dlogits.reshape(T, B, V) / (B * T)

        dWhy = h2s[1:].reshape(-1, H).T @ dlogits.reshape(-1, V)
        dby = dlogits.sum(axis=(0, 1))

        dh2_out = dlogits @ self.Why.T           # (T, B, H)

        da1_all = np.zeros((T, B, H), dtype=h1s.dtype)
        da2_all = np.zeros((T, B, H), dtype=h2s.dtype)
        dh1_next = np.zeros((B, H), dtype=h1s.dtype)
        dh2_next = np.zeros((B, H), dtype=h2s.dtype)
        Whh1T = np.ascontiguousarray(self.Whh1.T)
        Whh2T = np.ascontiguousarray(self.Whh2.T)
        Wxh2T = np.ascontiguousarray(self.Wxh2.T)

        for t in reversed(range(T)):
            dh2 = dh2_out[t] + dh2_next
            da2 = dh2 * (1 - h2s[t + 1] ** 2)
            da2_all[t] = da2
            dh2_next = da2 @ Whh2T

            # Layer 1's blame: what layer 2 owes it for being its input at
            # this step, plus what layer 1's own next step owes it.
            dh1 = da2 @ Wxh2T + dh1_next
            da1 = dh1 * (1 - h1s[t + 1] ** 2)
            da1_all[t] = da1
            dh1_next = da1 @ Whh1T

        da2_flat = da2_all.reshape(-1, H)
        dbh2 = da2_flat.sum(axis=0)
        dWhh2 = h2s[:-1].reshape(-1, H).T @ da2_flat
        dWxh2 = h1s[1:].reshape(-1, H).T @ da2_flat

        da1_flat = da1_all.reshape(-1, H)
        dbh1 = da1_flat.sum(axis=0)
        dWhh1 = h1s[:-1].reshape(-1, H).T @ da1_flat
        emb_tm = emb.transpose(1, 0, 2).reshape(-1, E)
        dWxh1 = emb_tm.T @ da1_flat

        dC = np.zeros_like(self.C)
        np.add.at(dC, np.ascontiguousarray(x.T).reshape(-1),
                  da1_flat @ self.Wxh1.T)

        grads = [dC, dWxh1, dWhh1, dbh1, dWxh2, dWhh2, dbh2, dWhy, dby]

        if clip:
            total = np.sqrt(sum(float((g ** 2).sum()) for g in grads))
            if total > clip:
                grads = [g * (clip / total) for g in grads]
        return grads

    def step(self, grads, lr, beta1=0.9, beta2=0.999, eps=1e-8):
        """One Adam update - identical to rnn.py's, just over more params."""
        self._t += 1
        for i, (p, g) in enumerate(zip(self.params, grads)):
            self._m[i] = beta1 * self._m[i] + (1 - beta1) * g
            self._v[i] = beta2 * self._v[i] + (1 - beta2) * (g ** 2)
            m_hat = self._m[i] / (1 - beta1 ** self._t)
            v_hat = self._v[i] / (1 - beta2 ** self._t)
            p -= lr * m_hat / (np.sqrt(v_hat) + eps)

    # ---------------- generation ----------------
    def warm(self, ids):
        """Read a prompt and return the (h1, h2) state pair it leaves
        behind - both layers' memory, not just one."""
        h1 = np.zeros((1, self.n_hidden), dtype=self.dtype)
        h2 = np.zeros((1, self.n_hidden), dtype=self.dtype)
        for i in ids:
            xt = self.C[[i]]
            h1 = np.tanh(xt @ self.Wxh1 + h1 @ self.Whh1 + self.bh1)
            h2 = np.tanh(h1 @ self.Wxh2 + h2 @ self.Whh2 + self.bh2)
        return h1, h2

    def generate(self, tokenizer, n_chars=200, prompt="", temperature=0.8,
                 top_k=None, seed=None, stop=None):
        return "".join(self.stream(tokenizer, n_chars, prompt, temperature,
                                   top_k, seed, stop))

    def stream(self, tokenizer, n_chars=200, prompt="", temperature=0.8,
               top_k=None, seed=None, stop=None):
        rng = np.random.default_rng(seed)
        ids = tokenizer.encode(prompt)

        if ids:
            h1, h2 = self.warm(ids[:-1])
            prev = ids[-1]
        else:
            h1 = np.zeros((1, self.n_hidden), dtype=self.dtype)
            h2 = np.zeros((1, self.n_hidden), dtype=self.dtype)
            prev = tokenizer.stoi.get("\n", 0)

        out = []
        for _ in range(n_chars):
            xt = self.C[[prev]]
            h1 = np.tanh(xt @ self.Wxh1 + h1 @ self.Whh1 + self.bh1)
            h2 = np.tanh(h1 @ self.Wxh2 + h2 @ self.Whh2 + self.bh2)
            logits = (h2 @ self.Why + self.by)[0]
            logits = logits / max(temperature, 1e-6)

            if top_k:
                cut = np.partition(logits, -top_k)[-top_k]
                logits = np.where(logits < cut, -np.inf, logits)

            p = softmax(logits)
            prev = int(rng.choice(len(p), p=p))
            ch = tokenizer.decode([prev])
            out.append(ch)
            yield ch

            if stop and "".join(out[-len(stop):]) == stop:
                return


def gradient_check(seed=0):
    """Prove BPTT through both layers is right by comparing it to
    brute-force calculus - see rnn.py's version of this for why it
    matters more than it looks: an off-by-one (or, here, a missing
    dh1_from_layer2 term) still trains, just worse, and the loss curve
    alone would never tell you."""
    rng = np.random.default_rng(seed)
    V, T = 10, 5
    m = CharRNN2(V, context_size=T, n_embd=6, n_hidden=12, seed=seed,
                dtype=np.float64)
    x = rng.integers(0, V, size=(4, T)).astype(np.int32)
    y = rng.integers(0, V, size=(4, T)).astype(np.int32)

    m.loss(m.forward(x), y)
    grads = m.backward(y, clip=0)

    eps = 1e-5
    worst = 0.0
    for p, g in zip(m.params, grads):
        for _ in range(15):
            idx = tuple(rng.integers(0, s) for s in p.shape)
            old = p[idx]
            p[idx] = old + eps
            lp = m.loss(m.forward(x), y)
            p[idx] = old - eps
            lm = m.loss(m.forward(x), y)
            p[idx] = old
            numeric = (lp - lm) / (2 * eps)
            denom = max(abs(numeric), abs(g[idx]), 1e-8)
            worst = max(worst, abs(numeric - g[idx]) / denom)
    return worst


if __name__ == "__main__":
    err = gradient_check()
    print(f"gradient check worst relative error: {err:.2e}")
    print("PASS - backprop through both layers matches calculus"
          if err < 1e-4 else "FAIL")

    m = CharRNN2(vocab_size=65)
    print("parameters:", f"{m.num_params():,}")
    out = m.forward(np.zeros((4, 64), dtype=np.int32))
    print("forward out:", out.shape, "(B, T, V) - a prediction at every step")
