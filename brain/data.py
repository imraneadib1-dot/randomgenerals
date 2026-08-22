"""Part 2 - turning raw text into batches of numbers the network can learn from.

A neural network can't read letters, only numbers. So we build a vocabulary:
every distinct character in the training text gets an integer id. "hello"
might become [46, 43, 50, 50, 53]. That mapping is the tokenizer.

We use characters rather than words because it keeps the vocabulary tiny
(~65 symbols for English text instead of 50,000+ words), which means a much
smaller network - the difference between something that trains on a laptop
in minutes and something that doesn't train at all.
"""
import os
import re
from collections import Counter

import numpy as np


_DEFAULT_RNG = np.random.default_rng()

# Minimum times a character must appear before it earns a spot in the
# vocabulary. A character seen twice in a million can never be predicted
# reliably, but it still gets an embedding row that never trains and a slot
# in every softmax denominator. Dropping the tail costs nothing and buys a
# smaller, cleaner output layer.
MIN_CHAR_COUNT = 50


class CharTokenizer:
    """Maps characters <-> integer ids, learned from the training text."""

    def __init__(self, text, min_count=MIN_CHAR_COUNT):
        counts = Counter(text)
        # sorted() so the mapping is deterministic - the same text always
        # produces the same ids, which matters when reloading a saved model.
        self.chars = sorted(ch for ch, n in counts.items() if n >= min_count)
        if not self.chars:                     # tiny inputs (tests, demos)
            self.chars = sorted(counts)
        self.dropped = sorted(set(counts) - set(self.chars))
        self.stoi = {ch: i for i, ch in enumerate(self.chars)}
        self.itos = {i: ch for ch, i in self.stoi.items()}

    @classmethod
    def from_chars(cls, chars):
        """Rebuild a tokenizer from a saved vocabulary.

        Loading a checkpoint means restoring the *exact* id->character
        mapping it was trained with. Re-deriving it from text would risk a
        different ordering, and every id would then decode to the wrong
        letter.
        """
        tok = cls.__new__(cls)
        tok.chars = list(chars)
        tok.dropped = []
        tok.stoi = {ch: i for i, ch in enumerate(tok.chars)}
        tok.itos = {i: ch for ch, i in tok.stoi.items()}
        return tok

    @property
    def vocab_size(self):
        return len(self.chars)

    def encode(self, s):
        """Text -> list of ids. Unknown characters are skipped."""
        return [self.stoi[c] for c in s if c in self.stoi]

    def decode(self, ids):
        """List of ids -> text."""
        return "".join(self.itos[int(i)] for i in ids)


class TextData:
    """Holds the encoded corpus and hands out random training batches."""

    def __init__(self, text, block_size=64, val_split=0.1):
        self.tokenizer = CharTokenizer(text)
        self.block_size = block_size

        data = np.array(self.tokenizer.encode(text), dtype=np.int32)
        if len(data) < block_size + 2:
            raise ValueError(
                f"Need at least {block_size + 2} characters of training text, "
                f"got {len(data)}."
            )

        # Hold back part of the text for validation so we can tell learning
        # from memorising: train loss always drops, val loss only drops if
        # the model found real patterns.
        #
        # Holding back the *tail* is the obvious move and it's wrong for a
        # book: the last 10% is a different animal from the first 90% -
        # appendices, indexes, closing matter. Val loss then measures a
        # distribution shift rather than generalisation, and reads high
        # forever no matter how well the model learns.
        #
        # So carve the text into chunks and hold back every Nth one. Chunks
        # stay long enough that val text is still genuine continuous prose,
        # but both splits now sample the whole book.
        chunk = max(block_size * 64, 1024)
        stride = max(int(round(1 / val_split)), 2)     # 0.1 -> every 10th
        train_parts, val_parts = [], []
        for i in range(0, len(data), chunk):
            part = data[i:i + chunk]
            is_val = (i // chunk) % stride == stride - 1
            (val_parts if is_val else train_parts).append(part)

        self.train_data = np.concatenate(train_parts)
        self.val_data = (np.concatenate(val_parts) if val_parts
                         else self.train_data)

    def get_batch(self, batch_size=32, split="train", rng=None):
        """Return (inputs, targets), each shaped (batch_size, block_size).

        Targets are inputs shifted one step right: given "hell" predict
        "ello". Every position in the block is a training example, which is
        why one batch teaches block_size times more than it looks like.
        """
        rng = rng if rng is not None else _DEFAULT_RNG
        data = self.train_data if split == "train" else self.val_data
        if len(data) < self.block_size + 1:
            data = self.train_data
        # Highest valid start index, so x and y both fit inside the array.
        high = len(data) - self.block_size - 1
        starts = rng.integers(0, high + 1, size=batch_size)
        x = np.stack([data[i:i + self.block_size] for i in starts])
        y = np.stack([data[i + 1:i + 1 + self.block_size] for i in starts])
        return x.astype(np.int32), y.astype(np.int32)


def strip_gutenberg(text):
    """Cut the Project Gutenberg header and licence off a downloaded book.

    Those wrappers are a few hundred characters of catalogue text at the
    front and ~18KB of legal boilerplate at the back. The licence is the
    problem: it is nothing like the prose around it, so the model wastes
    capacity on it and any split that lands on it reports a misleading loss.
    Both markers are optional - a corpus that isn't from Gutenberg passes
    through untouched.
    """
    start = re.search(r"\*\*\*\s*START OF TH(IS|E) PROJECT GUTENBERG.*?\*\*\*",
                      text, re.IGNORECASE | re.DOTALL)
    if start:
        text = text[start.end():]
    end = re.search(r"\*\*\*\s*END OF TH(IS|E) PROJECT GUTENBERG",
                    text, re.IGNORECASE)
    if end:
        text = text[:end.start()]
    return text.strip("\n")


def load_text(path):
    """Read a training corpus off disk.

    `path` can be a single .txt file, or a directory of them - every .txt
    file inside is read, stripped, and joined into one corpus. Stripping
    each file *before* joining matters: a Gutenberg header buried in the
    middle of the combined text wouldn't match the "start of file" pattern
    strip_gutenberg() looks for, and would train straight into the model.

    A wider, more varied corpus is the whole point of supporting several
    files - one book gives the model one voice and one narrow vocabulary
    (train it only on a treatise about optics and it free-associates to
    "light" and "the eye" no matter what you say to it). Mixing genres -
    narrative, dialogue, letters - gives it more to draw on.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No training text at {path!r}. Put a .txt file there - a book, "
            f"your own writing, anything. Aim for 500KB or more."
        )

    if os.path.isdir(path):
        files = sorted(f for f in os.listdir(path) if f.endswith(".txt"))
        if not files:
            raise FileNotFoundError(
                f"No .txt files in {path!r}. Drop one or more books there."
            )
        texts = []
        for name in files:
            with open(os.path.join(path, name), "r", encoding="utf-8",
                      errors="ignore") as f:
                texts.append(strip_gutenberg(f.read()))
        # Join with a wide gap rather than nothing. Two books run together
        # with no separation reads as one book that abruptly changes voice
        # mid-sentence; blank lines are at least a real thing that appears
        # naturally between sections, so the model isn't learning a pattern
        # it will never see again outside of training.
        return "\n\n\n".join(texts)

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return strip_gutenberg(f.read())


if __name__ == "__main__":
    sample = "hello world. " * 400
    data = TextData(sample, block_size=16)
    tok = data.tokenizer
    print("vocab size :", tok.vocab_size)
    print("vocab      :", "".join(tok.chars).replace("\n", "\\n"))
    print("encode     :", tok.encode("hello"))
    print("roundtrip  :", tok.decode(tok.encode("hello world")))
    x, y = data.get_batch(batch_size=2)
    print("batch x    :", x.shape, "batch y:", y.shape)
    print("x[0] text  :", repr(tok.decode(x[0])))
    print("y[0] text  :", repr(tok.decode(y[0])), "<- shifted by one")
