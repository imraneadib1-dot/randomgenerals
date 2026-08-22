"""Part 3 - the network itself. Neurons, weights, and backpropagation,
written out by hand with nothing but NumPy.

The shape of it:

    characters -> embedding -> hidden layer (tanh) -> output -> probabilities

Every arrow is a matrix of weights. "Learning" means nudging those numbers
until the predictions stop being wrong. Backprop is just the chain rule
from calculus, applied backwards through those same arrows to work out
which direction each weight should move.
"""
import numpy as np


def softmax(z):
    """Turn raw scores into probabilities that sum to 1.

    We subtract the row max first. Mathematically that changes nothing,
    but it stops exp() overflowing to inf on large scores - the single
    most common way a hand-written network silently breaks.
    """
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


class CharMLP:
    """A multi-layer perceptron that predicts the next character.

    It reads a fixed window of `context_size` characters and outputs a
    probability for every character that might come next.
    """

    def __init__(self, vocab_size, context_size=16, n_embd=24, n_hidden=128,
                 seed=1337):
        rng = np.random.default_rng(seed)
        self.vocab_size = vocab_size
        self.context_size = context_size
        self.n_embd = n_embd

        fan_in = context_size * n_embd
        # Small random starting weights. Scaling by 1/sqrt(fan_in) keeps the
        # signal from exploding or vanishing as it passes through the layer -
        # start them all at zero instead and every neuron learns the same
        # thing forever.
        self.C = rng.normal(0, 0.1, (vocab_size, n_embd))
        self.W1 = rng.normal(0, 1, (fan_in, n_hidden)) / np.sqrt(fan_in)
        self.b1 = np.zeros(n_hidden)
        self.W2 = rng.normal(0, 1, (n_hidden, vocab_size)) / np.sqrt(n_hidden)
        self.b2 = np.zeros(vocab_size)

    @property
    def params(self):
        return [self.C, self.W1, self.b1, self.W2, self.b2]

    def num_params(self):
        return sum(p.size for p in self.params)

    # ---------------- forward ----------------
    def forward(self, x):
        """x: (B, context_size) int ids -> logits (B, vocab_size).

        Caches intermediates because backward() needs them.
        """
        B = x.shape[0]
        emb = self.C[x]                        # (B, T, E) look up each char
        # (B, T*E) glue the window together
        flat = emb.reshape(B, -1)
        h_pre = flat @ self.W1 + self.b1       # (B, H)
        # (B, H) the squashing nonlinearity
        h = np.tanh(h_pre)
        logits = h @ self.W2 + self.b2         # (B, V)
        self._cache = (x, flat, h, logits)
        return logits

    def loss(self, logits, y):
        """Average cross-entropy: how surprised the model was by the truth.

        A loss of ln(vocab_size) means it's guessing uniformly at random.
        """
        B = logits.shape[0]
        p = softmax(logits)
        # Clip so a confidently-wrong prediction gives a big number, not -inf.
        return -np.log(np.clip(p[np.arange(B), y], 1e-12, None)).mean()

    # ---------------- backward ----------------
    def backward(self, y):
        """Work out how every weight should change. Returns matching grads."""
        x, flat, h, logits = self._cache
        B = logits.shape[0]

        # d(loss)/d(logits) for softmax + cross-entropy collapses to this
        # beautifully simple form: predicted probability minus the truth.
        dlogits = softmax(logits)
        dlogits[np.arange(B), y] -= 1
        dlogits /= B

        dW2 = h.T @ dlogits
        db2 = dlogits.sum(axis=0)

        dh = dlogits @ self.W2.T
        dh_pre = dh * (1 - h ** 2)             # derivative of tanh

        dW1 = flat.T @ dh_pre
        db1 = dh_pre.sum(axis=0)

        dflat = dh_pre @ self.W1.T
        demb = dflat.reshape(B, self.context_size, self.n_embd)

        # Several positions in the batch can point at the same character,
        # so gradients must accumulate. np.add.at does that; C[x] += would
        # silently keep only the last write.
        dC = np.zeros_like(self.C)
        np.add.at(dC, x, demb)

        return [dC, dW1, db1, dW2, db2]

    def step(self, grads, lr):
        """Plain gradient descent: move each weight against its gradient."""
        for p, g in zip(self.params, grads):
            p -= lr * g

    # ---------------- generation ----------------
    def generate(self, tokenizer, n_chars=200, prompt="", temperature=1.0,
                 seed=None):
        """Sample text one character at a time, feeding output back as input.

        temperature < 1 makes it play safe and repetitive; > 1 makes it
        wilder and more error-prone.
        """
        rng = np.random.default_rng(seed)
        # Left-pad the prompt so the context window is always full.
        ids = tokenizer.encode(prompt)
        ctx = ([0] * self.context_size + ids)[-self.context_size:]

        out = []
        for _ in range(n_chars):
            logits = self.forward(np.array([ctx], dtype=np.int32))[0]
            p = softmax(logits / max(temperature, 1e-6))
            nxt = int(rng.choice(len(p), p=p))
            out.append(nxt)
            ctx = ctx[1:] + [nxt]
        return tokenizer.decode(out)


def gradient_check(seed=0):
    """Prove backprop is right by comparing it to brute-force calculus.

    Nudge one weight by a hair, see how much the loss moved, and check that
    matches what backward() claimed. If these disagree, the maths is wrong
    and no amount of training will save it.
    """
    rng = np.random.default_rng(seed)
    V, T = 12, 4
    m = CharMLP(V, context_size=T, n_embd=6, n_hidden=16, seed=seed)
    x = rng.integers(0, V, size=(8, T)).astype(np.int32)
    y = rng.integers(0, V, size=8).astype(np.int32)

    m.loss(m.forward(x), y)
    grads = m.backward(y)

    eps = 1e-5
    worst = 0.0
    for p, g in zip(m.params, grads):
        for _ in range(20):                    # spot-check random entries
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
    print("PASS - backprop matches calculus" if err < 1e-4 else "FAIL")

    m = CharMLP(vocab_size=65)
    print("parameters:", f"{m.num_params():,}")
    logits = m.forward(np.zeros((4, 16), dtype=np.int32))
    print("forward out:", logits.shape)
    print("loss at init:", round(float(m.loss(logits, np.zeros(4, dtype=np.int32))), 4),
          "(random guessing =", round(float(np.log(65)), 4), ")")
