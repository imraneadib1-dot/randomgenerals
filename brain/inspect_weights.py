"""Export a trained checkpoint's actual numbers for the browser visualizer.

Not part of the training or serving pipeline - this is purely for looking
at what got learned. It writes one JSON file with:

  - the character embedding table, projected to 2D so it can be plotted
  - the strongest connections in the recurrent weight matrix (the literal
    "neuron links" - which hidden units feed which other hidden units)
  - quantized heatmaps of all three weight matrices, for a full-matrix view

Run it after training:  python -m brain.inspect_weights [rnn|mlp]
"""
import base64
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.train import load_checkpoint  # noqa: E402

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "weights_export.json")


def char_category(ch):
    """Rough bucket for coloring the embedding plot - not linguistics, just
    enough to see whether the model separated letters from punctuation."""
    if ch.isalpha() and ch.lower() == ch:
        return "lower"
    if ch.isalpha():
        return "upper"
    if ch.isdigit():
        return "digit"
    if ch.isspace():
        return "space"
    return "punct"


def pca_2d(mat):
    """Project rows of `mat` onto their top 2 principal components.

    Implemented directly off SVD rather than pulling in scikit-learn - the
    whole point of this project is not depending on anything but NumPy.
    Center each column, then the top singular vectors of the centered
    matrix are exactly the principal axes.
    """
    centered = mat - mat.mean(axis=0, keepdims=True)
    u, s, _vt = np.linalg.svd(centered, full_matrices=False)
    coords = u[:, :2] * s[:2]
    # Normalise to roughly [-1, 1] so the frontend doesn't need to know the
    # scale in advance.
    scale = np.abs(coords).max() or 1.0
    return coords / scale


def quantize(mat, levels=255):
    """Flatten a weight matrix to bytes for a heatmap, keeping the sign.

    Full float precision is wasted on a color - 256 shades is more than
    the eye distinguishes anyway. Store min/max alongside so the frontend
    can recover the original scale for the legend.
    """
    lo, hi = float(mat.min()), float(mat.max())
    span = (hi - lo) or 1.0
    q = np.clip(np.round((mat - lo) / span * levels), 0, levels).astype(np.uint8)
    return {
        "shape": list(mat.shape),
        "min": lo,
        "max": hi,
        "data_b64": base64.b64encode(q.tobytes()).decode("ascii"),
    }


def top_connections(whh, k=220):
    """The k strongest neuron-to-neuron links by |weight|.

    Whh[i, j] is literally the weight carrying neuron i's previous output
    into neuron j's next value (see rnn.py forward: hs[t] @ Whh) - a
    directed edge i -> j with that weight. This is the closest thing in the
    whole network to "which neurons are wired to which".
    """
    flat_idx = np.argsort(-np.abs(whh), axis=None)[:k]
    rows, cols = np.unravel_index(flat_idx, whh.shape)
    return [
        {"from": int(i), "to": int(j), "w": round(float(whh[i, j]), 4)}
        for i, j in zip(rows.tolist(), cols.tolist())
    ]


def export(arch="rnn"):
    found = load_checkpoint(arch)
    if not found:
        raise SystemExit(f"No checkpoint for '{arch}' - train one first.")
    model, chars, step, best_val = found

    embed_2d = pca_2d(np.asarray(model.C, dtype=np.float64))
    embedding = [
        {"ch": ch, "x": round(float(x), 4), "y": round(float(y), 4),
         "cat": char_category(ch)}
        for ch, (x, y) in zip(chars, embed_2d)
    ]

    out = {
        "arch": arch,
        "step": step,
        "best_val": best_val,
        "vocab_size": len(chars),
        "n_hidden": model.n_hidden,
        "n_embd": model.n_embd,
        "params": model.num_params(),
        "embedding": embedding,
        "connections": top_connections(np.asarray(model.Whh, dtype=np.float64)),
        "heatmaps": {
            "Wxh": quantize(np.asarray(model.Wxh, dtype=np.float64)),
            "Whh": quantize(np.asarray(model.Whh, dtype=np.float64)),
            "Why": quantize(np.asarray(model.Why, dtype=np.float64)),
        },
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"wrote {OUT_PATH} ({os.path.getsize(OUT_PATH):,} bytes) - "
         f"step {step:,}, best val {best_val:.4f}")


if __name__ == "__main__":
    export(sys.argv[1] if len(sys.argv) > 1 else "rnn")
