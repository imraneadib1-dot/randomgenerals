"""Part 5 - the linked network. Neurons wired to their own past.

The MLP in model.py reads a fixed window: sixteen characters, then a hard
wall. Anything earlier is simply gone. Widen the window and the first
weight matrix grows with it, so memory costs parameters - and it can never
be longer than the number you compiled in.

A recurrent net links each step to the one before it:

    h_t = tanh( x_t @ Wxh  +  h_{t-1} @ Whh  +  bh )
                 ^^^^^^^^^     ^^^^^^^^^^^^^
                 what's new    what it remembers

That second term is the whole idea. `h` is a running summary of everything
read so far, and it is fed back in at every step. Memory now costs no extra
parameters and has no fixed length: Whh is the same small matrix whether
the model is 10 characters in or 10,000.

The price is paid in training. Gradients have to travel backwards through
every link in the chain (backpropagation through time), multiplying by Whh
at each hop. Multiply a number by itself a hundred times and it either
explodes to infinity or vanishes to nothing - which is exactly what happens
to these gradients, and why the two guards below (clipping, and Adam) are
not optional garnish but the things that make it train at all.

One free bonus: the MLP made a single prediction per window and threw the
rest away. This one predicts at *every* step, so a (32, 64) batch produces
2048 training signals instead of 32.
"""
import numpy as np

from brain.model import softmax


class CharRNN:
    """A recurrent network that predicts the next character at every step."""

    # train.py checks this: the MLP scores only the final position of a
    # window, this one scores all of them.
    predicts_all_positions = True

    def __init__(self, vocab_size, context_size=64, n_embd=48, n_hidden=384,
                 seed=1337, dtype=np.float32):
        rng = np.random.default_rng(seed)
        self.vocab_size = vocab_size
        self.context_size = context_size
        self.n_embd = n_embd
        self.n_hidden = n_hidden
        # float32 halves the memory traffic of every matrix multiply and runs
        # roughly twice as fast. Neural nets are famously indifferent to the
        # lost precision - the gradients are noisy estimates to begin with.
        # (gradient_check below is the exception and forces float64: it
        # compares against tiny finite differences that float32 cannot
        # resolve.)
        self.dtype = dtype

        self.C = rng.normal(0, 0.1, (vocab_size, n_embd))
        self.Wxh = rng.normal(0, 1, (n_embd, n_hidden)) / np.sqrt(n_embd)
        # The recurrent matrix starts as a scaled identity, not noise. The
        # identity means "carry your state forward unchanged" - a sensible
        # default memory that the net can then learn to deviate from. Start
        # it random and the state is scrambled at every step before any
        # learning happens, which is a hard hole to climb out of.
        self.Whh = np.eye(n_hidden) * 0.5 + rng.normal(
            0, 0.01, (n_hidden, n_hidden))
        self.bh = np.zeros(n_hidden)
        self.Why = rng.normal(0, 1, (n_hidden, vocab_size)) / np.sqrt(n_hidden)
        self.by = np.zeros(vocab_size)

        self.C = self.C.astype(dtype)
        self.Wxh = self.Wxh.astype(dtype)
        self.Whh = self.Whh.astype(dtype)
        self.bh = self.bh.astype(dtype)
        self.Why = self.Why.astype(dtype)
        self.by = self.by.astype(dtype)

        # Adam's per-weight running averages. Plain SGD uses one learning
        # rate for every weight; in a recurrent net some weights see
        # gradients hundreds of times larger than others, so one rate is
        # always wrong for most of them. Adam keeps a running estimate of
        # each weight's typical gradient size and divides it out, giving
        # every weight its own effective rate.
        self._m = [np.zeros_like(p) for p in self.params]
        self._v = [np.zeros_like(p) for p in self.params]
        self._t = 0

    @property
    def params(self):
        return [self.C, self.Wxh, self.Whh, self.bh, self.Why, self.by]

    def num_params(self):
        return sum(p.size for p in self.params)

    # ---------------- forward ----------------
    def forward(self, x, h0=None):
        """x: (B, T) int ids -> logits (B, T, V).

        Walks the sequence left to right, carrying `h` along. Every
        intermediate h is cached because backward() has to revisit each one.

        Only the `h @ Whh` term truly has to happen inside the loop - it
        needs the previous step's answer. Everything that depends solely on
        the input is computed for all T steps in one go before the loop
        starts. Sixty-four small matrix multiplies become one big one, which
        NumPy does several times faster for the same arithmetic.

        Note the internal arrays are *time-major*, (T, B, H) rather than the
        (B, T, H) you might expect. Then `hs[t]` is one contiguous block of
        memory. Indexed the other way it is a strided view, and every matrix
        multiply in the loop silently copies it first - which cost more than
        the arithmetic did.
        """
        B, T = x.shape
        H = self.n_hidden

        emb = self.C[x]                          # (B, T, E)
        # Input side for every step at once, then flipped to time-major.
        xh = np.ascontiguousarray(
            (emb @ self.Wxh + self.bh).transpose(1, 0, 2))   # (T, B, H)

        hs = np.zeros((T + 1, B, H), dtype=xh.dtype)
        if h0 is not None:
            hs[0] = h0
        for t in range(T):
            # The link: this step's input plus last step's memory.
            hs[t + 1] = np.tanh(xh[t] + hs[t] @ self.Whh)

        logits = hs[1:] @ self.Why + self.by     # (T, B, V)
        self._cache = (x, emb, hs, logits)
        return logits.transpose(1, 0, 2)         # back to (B, T, V)

    def loss(self, logits, y):
        """Average cross-entropy over every position in every sequence."""
        B, T, V = logits.shape
        p = softmax(logits.reshape(-1, V))
        truth = p[np.arange(B * T), y.reshape(-1)]
        return -np.log(np.clip(truth, 1e-12, None)).mean()

    # ---------------- backward ----------------
    def backward(self, y, clip=5.0):
        """Backpropagation through time.

        Same chain rule as the MLP, but the hidden state has two futures to
        answer for: the prediction it made at its own step, and the state it
        handed to the next step. Its gradient is the sum of both, which is
        what `dh_next` carries backwards down the loop.
        """
        x, emb, hs, logits = self._cache         # hs, logits are time-major
        H, E = self.n_hidden, self.n_embd
        B, T, V = x.shape[0], x.shape[1], self.vocab_size

        # Targets have to be flattened the same way the logits are: time
        # first, then batch. Flatten y as-is and every prediction gets
        # scored against the wrong character.
        y_tm = np.ascontiguousarray(y.T).reshape(-1)

        dlogits = softmax(logits.reshape(-1, V))
        dlogits[np.arange(T * B), y_tm] -= 1
        dlogits = dlogits.reshape(T, B, V) / (B * T)

        dWhy = hs[1:].reshape(-1, H).T @ dlogits.reshape(-1, V)
        dby = dlogits.sum(axis=(0, 1))

        # Every step's gradient from its own prediction, computed for all T
        # at once - none of it depends on the recurrence.
        dh_out = dlogits @ self.Why.T            # (T, B, H)

        # The one genuinely sequential part: walk backwards, carrying the
        # blame each state inherits from the step it fed. Everything else is
        # deferred until after the loop and done in one shot.
        da_all = np.zeros((T, B, H), dtype=hs.dtype)
        dh_next = np.zeros((B, H), dtype=hs.dtype)
        WhhT = np.ascontiguousarray(self.Whh.T)
        for t in reversed(range(T)):
            # From this step's own prediction, plus whatever the step after
            # it blamed on this state.
            dh = dh_out[t] + dh_next
            da = dh * (1 - hs[t + 1] ** 2)       # through the tanh
            da_all[t] = da
            dh_next = da @ WhhT                  # hand back one more step

        da_flat = da_all.reshape(-1, H)
        dbh = da_flat.sum(axis=0)
        dWhh = hs[:-1].reshape(-1, H).T @ da_flat
        # emb is (B, T, E); transpose to match da_flat's time-major order.
        emb_tm = emb.transpose(1, 0, 2).reshape(-1, E)
        dWxh = emb_tm.T @ da_flat

        # Many positions point at the same character, so embedding gradients
        # must accumulate rather than overwrite - hence np.add.at.
        dC = np.zeros_like(self.C)
        np.add.at(dC, np.ascontiguousarray(x.T).reshape(-1),
                  da_flat @ self.Wxh.T)

        grads = [dC, dWxh, dWhh, dbh, dWhy, dby]

        # Gradient clipping. Occasionally BPTT produces a gradient orders of
        # magnitude bigger than usual; applied raw it throws the weights
        # somewhere useless and the loss becomes NaN, destroying an hour of
        # training in one step. Rescaling anything oversized preserves the
        # direction while capping the damage.
        if clip:
            total = np.sqrt(sum(float((g ** 2).sum()) for g in grads))
            if total > clip:
                grads = [g * (clip / total) for g in grads]
        return grads

    def step(self, grads, lr, beta1=0.9, beta2=0.999, eps=1e-8):
        """One Adam update."""
        self._t += 1
        for i, (p, g) in enumerate(zip(self.params, grads)):
            self._m[i] = beta1 * self._m[i] + (1 - beta1) * g
            self._v[i] = beta2 * self._v[i] + (1 - beta2) * (g ** 2)
            # Both averages start at zero, which biases them low for the
            # first few steps; these two divisions correct for that.
            m_hat = self._m[i] / (1 - beta1 ** self._t)
            v_hat = self._v[i] / (1 - beta2 ** self._t)
            p -= lr * m_hat / (np.sqrt(v_hat) + eps)

    # ---------------- generation ----------------
    def warm(self, ids):
        """Read a prompt and return the hidden state it leaves behind.

        This is the part a windowed model cannot do: the entire prompt is
        folded into one fixed-size state, however long it was.
        """
        h = np.zeros((1, self.n_hidden), dtype=self.dtype)
        for i in ids:
            h = np.tanh(self.C[[i]] @ self.Wxh + h @ self.Whh + self.bh)
        return h

    def generate(self, tokenizer, n_chars=200, prompt="", temperature=0.8,
                 top_k=None, seed=None, stop=None):
        """Sample text one character at a time, feeding output back in.

        Unlike the MLP there is no window to slide - we keep `h` between
        steps, so the model still has access to the whole history.
        """
        return "".join(self.stream(tokenizer, n_chars, prompt, temperature,
                                   top_k, seed, stop))

    def stream(self, tokenizer, n_chars=200, prompt="", temperature=0.8,
               top_k=None, seed=None, stop=None):
        """Same as generate(), but yields each character as it is produced.

        The web app streams these straight to the browser.
        """
        rng = np.random.default_rng(seed)
        ids = tokenizer.encode(prompt)

        # Warm up on everything *except* the final character, which becomes
        # the loop's first input. Warming on the whole prompt and then also
        # feeding the last character would run it through the network twice,
        # doubling its influence on what comes next.
        if ids:
            h = self.warm(ids[:-1])
            prev = ids[-1]
        else:
            h = np.zeros((1, self.n_hidden), dtype=self.dtype)
            prev = tokenizer.stoi.get("\n", 0)

        out = []
        for _ in range(n_chars):
            h = np.tanh(self.C[[prev]] @ self.Wxh + h @ self.Whh + self.bh)
            logits = (h @ self.Why + self.by)[0]
            logits = logits / max(temperature, 1e-6)

            if top_k:
                # Keep only the k most likely characters. The long tail of
                # near-zero probabilities is individually harmless and
                # collectively large, so sampling from it is the main source
                # of nonsense characters mid-word.
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
    """Prove BPTT is right by comparing it to brute-force calculus.

    Worth more here than in the MLP: an off-by-one in the time loop still
    trains, just badly, and you would never know from the loss curve alone.
    """
    rng = np.random.default_rng(seed)
    V, T = 10, 5
    m = CharRNN(V, context_size=T, n_embd=6, n_hidden=12, seed=seed,
                dtype=np.float64)
    x = rng.integers(0, V, size=(4, T)).astype(np.int32)
    y = rng.integers(0, V, size=(4, T)).astype(np.int32)

    m.loss(m.forward(x), y)
    grads = m.backward(y, clip=0)               # unclipped, or it won't match

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
    print("PASS - backprop through time matches calculus"
          if err < 1e-4 else "FAIL")

    m = CharRNN(vocab_size=65)
    print("parameters:", f"{m.num_params():,}")
    out = m.forward(np.zeros((4, 64), dtype=np.int32))
    print("forward out:", out.shape, "(B, T, V) - a prediction at every step")
