"""Part 4 - the training loop.

Repeat until bored:
    1. grab a random batch of text
    2. predict the next character for each
    3. measure how wrong you were
    4. nudge every weight slightly less wrong

The two numbers to watch are train loss and val loss. Train loss always
falls - a big enough network can memorise anything. Val loss is measured on
text the model never trains on, so it only falls if the model found real
patterns. When train keeps dropping and val starts rising, it has stopped
learning and started memorising: stop there.
"""
import argparse
import math
import os
import sys
import time

# Must be set before NumPy is imported - its BLAS reads these once at load.
# BLAS defaults to one thread per core, which is right for big matrices and
# badly wrong for ours: the matrices inside the recurrent loop are small
# enough that co-ordinating 16 threads costs more than the arithmetic they
# do. Measured on a 16-core box: 16 threads 0.44s/step, 4 threads 0.10s.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import numpy as np  # noqa: E402

# Must come before the `brain.*` imports below: running this file directly
# ("python brain/train.py") puts brain/ on the path, not the project root,
# so without this the package import fails.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brain.model import CharMLP  # noqa: E402
from brain.rnn import CharRNN  # noqa: E402
from brain.rnn_deep import CharRNN2  # noqa: E402
from brain.data import TextData, load_text  # noqa: E402


HERE = os.path.dirname(os.path.abspath(__file__))
# A directory of .txt files, not one file - see load_text() in data.py.
# Drop more books in brain/corpus/ to widen the training text further.
CORPUS_PATH = os.path.join(HERE, "corpus")


def ckpt_path(arch):
    """Each architecture gets its own file so they never overwrite each
    other - the weights aren't interchangeable."""
    return os.path.join(HERE, f"checkpoint-{arch}.npz")


# ---- knobs worth turning ----
# Two sets, because the architectures want genuinely different treatment.
# The MLP is trained with plain SGD at a high rate; the RNN uses Adam,
# whose rates live two orders of magnitude lower and are not interchangeable
# - feed the MLP's 0.3 to Adam and the weights diverge on the first step.
ARCHS = {
    "mlp": dict(
        cls=CharMLP,
        context_size=16,     # hard limit on how far back it can see
        n_embd=32,
        n_hidden=256,
        batch_size=64,
        lr=0.3,              # SGD
        steps=20000,
        eval_every=1000,
    ),
    "rnn": dict(
        cls=CharRNN,
        context_size=80,     # BPTT depth, not a memory limit
        n_embd=64,
        n_hidden=512,
        batch_size=64,
        lr=3e-3,             # Adam
        steps=25000,
        eval_every=1000,
    ),
    "rnn2": dict(
        cls=CharRNN2,
        # Same width and context as "rnn" on purpose - the only thing
        # that changes between them is one extra recurrent layer, so any
        # difference in val loss is attributable to that, not to also
        # having quietly changed the hidden size or context window too.
        context_size=80,
        n_embd=64,
        n_hidden=512,
        batch_size=64,
        # A slightly lower rate than "rnn" - the loss surface a 2-layer
        # net is descending is less well-behaved than a 1-layer one.
        lr=2e-3,             # Adam
        # Roughly 4-5x slower per step than "rnn" at this width (measured:
        # ~0.45s/step vs ~0.10s/step) - layer 2's input depends on the
        # recurrent loop and can't be hoisted out of it the way layer 1's
        # can, so fewer steps for a comparable wall-clock budget.
        steps=8000,
        eval_every=500,
    ),
}

LR_FINAL_FRAC = 0.05


def lr_at(progress, base_lr):
    """Learning rate as training goes 0.0 -> 1.0 through the run.

    A rate big enough to make fast early progress is too big to settle
    with: near the end the weights just bounce around the minimum instead
    of dropping into it. So decay it. This is a cosine curve - fast while
    it's safe to be fast, then easing off to a slow polish at the end.
    """
    cos = 0.5 * (1 + math.cos(math.pi * min(max(progress, 0.0), 1.0)))
    return base_lr * (LR_FINAL_FRAC + (1 - LR_FINAL_FRAC) * cos)


# Which attributes hold the learned weights, per architecture. Saving by
# name keeps one checkpoint format working for both.
PARAM_NAMES = {
    "mlp": ["C", "W1", "b1", "W2", "b2"],
    "rnn": ["C", "Wxh", "Whh", "bh", "Why", "by"],
    "rnn2": ["C", "Wxh1", "Whh1", "bh1", "Wxh2", "Whh2", "bh2", "Why", "by"],
}


def save_checkpoint(model, tokenizer, arch, path=None, step=0,
                    best_val=float("inf")):
    """Weights alone are useless - the vocabulary has to travel with them,
    or reloaded ids decode to the wrong characters. The architecture name
    travels too, so the loader knows which class to rebuild.

    `best_val` is stored so a resumed run knows what score it has to beat.
    """
    path = path or ckpt_path(arch)
    arrays = {name: getattr(model, name) for name in PARAM_NAMES[arch]}

    # Write to a scratch file and rename it into place. Renaming is atomic,
    # so a reader either sees the whole previous checkpoint or the whole new
    # one - never a half-written file. Without this, saving while the web app
    # has the model open (or pressing Ctrl-C mid-write) can leave a corrupt
    # checkpoint and lose the entire run.
    tmp = path + ".tmp.npz"
    np.savez(
        tmp,
        chars=np.array(tokenizer.chars, dtype=object),
        arch=arch,
        context_size=model.context_size,
        step=step,
        best_val=best_val,
        **arrays,
    )
    os.replace(tmp, path)


def load_checkpoint(arch="rnn", path=None):
    """Returns (model, chars, step, best_val) or None if there's nothing
    saved."""
    path = path or ckpt_path(arch)
    if not os.path.exists(path):
        return None
    z = np.load(path, allow_pickle=True)
    chars = list(z["chars"])
    arch = str(z["arch"]) if "arch" in z else arch

    if arch == "rnn":
        model = CharRNN(len(chars), int(z["context_size"]),
                        n_embd=z["C"].shape[1], n_hidden=z["Whh"].shape[0])
    elif arch == "rnn2":
        model = CharRNN2(len(chars), int(z["context_size"]),
                         n_embd=z["C"].shape[1], n_hidden=z["Whh1"].shape[0])
    else:
        model = CharMLP(len(chars), int(z["context_size"]),
                        n_embd=z["C"].shape[1], n_hidden=z["W1"].shape[1])

    for name in PARAM_NAMES[arch]:
        setattr(model, name, z[name])
    # Older checkpoints predate this field - without it a resume can't tell
    # what score it needs to beat, so treat missing as "nothing to beat yet".
    best_val = float(z["best_val"]) if "best_val" in z else float("inf")
    return model, chars, int(z["step"]), best_val


def targets_for(model, y):
    """The MLP predicts one character per window; the RNN predicts at every
    position. Same batch, different slice of it."""
    return y if getattr(model, "predicts_all_positions", False) else y[:, -1]


@np.errstate(all="ignore")
def estimate_loss(model, data, split, batch_size, batches=20):
    """Average loss over several batches - one batch alone is too noisy
    to tell real improvement from luck."""
    total = 0.0
    for _ in range(batches):
        x, y = data.get_batch(batch_size, split=split)
        total += model.loss(model.forward(x), targets_for(model, y))
    return total / batches


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train a character model.")
    ap.add_argument("--arch", choices=sorted(ARCHS), default="rnn",
                    help="mlp = fixed 16-char window; rnn = linked, "
                         "carries memory forward (default)")
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any existing checkpoint and start over")
    ap.add_argument("--backend", choices=("numpy", "cpp"), default="numpy",
                    help="rnn only. numpy uses NumPy/OpenBLAS (default, and "
                         "currently the faster of the two on this machine - "
                         "OpenBLAS's hand-tuned matmul beats the hand-written "
                         "C++ loops in brain/cpp/rnn_core.cpp). cpp uses the "
                         "compiled core instead; needs brain/cpp/rnn_core.dll "
                         "built via brain/cpp/build.ps1.")
    args = ap.parse_args(argv)

    cfg = ARCHS[args.arch]
    steps = args.steps if args.steps is not None else cfg["steps"]
    batch_size = cfg["batch_size"]
    path = ckpt_path(args.arch)

    cpp_step = None
    if args.backend == "cpp":
        if args.arch != "rnn":
            print("--backend cpp only supports --arch rnn")
            return 1
        from brain import cpp_backend
        if not cpp_backend.available:
            print("brain/cpp/rnn_core.dll not found - build it first:")
            print("    powershell brain/cpp/build.ps1")
            return 1
        cpp_step = cpp_backend.train_step

    if not os.path.isdir(CORPUS_PATH) or not any(
            f.endswith(".txt") for f in os.listdir(CORPUS_PATH)):
        print(f"No training text found in {CORPUS_PATH}\n")
        print("Drop one or more .txt files there and run this again.")
        print("Aim for 500KB+ total - a few books work well. Project")
        print("Gutenberg (gutenberg.org) has free ones; 'Plain Text UTF-8'")
        print("is the download you want.")
        return 1

    text = load_text(CORPUS_PATH)
    data = TextData(text, block_size=cfg["context_size"])
    tok = data.tokenizer

    print(f"architecture: {args.arch}")
    print(f"backend    : {args.backend}")
    print(f"corpus     : {len(text):,} characters")
    print(f"vocabulary : {tok.vocab_size} distinct characters")
    if tok.dropped:
        shown = "".join(tok.dropped).replace("\n", "\\n")
        print(f"             (dropped {len(tok.dropped)} rare: {shown!r})")
    print(f"train/val  : {len(data.train_data):,} / {len(data.val_data):,}")

    # Resume if a checkpoint exists and its vocabulary still matches.
    start_step = 0
    best_val = float("inf")
    resumed = None if args.fresh else load_checkpoint(args.arch, path)
    if resumed and resumed[1] == tok.chars:
        model, _, start_step, best_val = resumed
        print(f"resuming   : from step {start_step:,}, "
              f"best val so far {best_val:.4f}")
    else:
        if resumed:
            print("checkpoint : vocabulary changed, starting fresh")
        model = cfg["cls"](tok.vocab_size, cfg["context_size"],
                           cfg["n_embd"], cfg["n_hidden"])

    print(f"parameters : {model.num_params():,}")
    print(f"random-guess loss: {np.log(tok.vocab_size):.4f}  <- beat this\n")

    t0 = time.time()
    step = start_step
    try:
        for step in range(start_step + 1, start_step + steps + 1):
            x, y = data.get_batch(batch_size)
            lr = lr_at((step - start_step) / steps, cfg["lr"])
            if cpp_step:
                _, grads = cpp_step(model, x, y)
                model.step(grads, lr=lr)
            else:
                targets = targets_for(model, y)
                model.loss(model.forward(x), targets)
                model.step(model.backward(targets), lr=lr)

            if step % cfg["eval_every"] == 0 or step == start_step + 1:
                tr = estimate_loss(model, data, "train", batch_size)
                va = estimate_loss(model, data, "val", batch_size)
                elapsed = time.time() - t0

                # Only keep the weights that scored best on held-out text.
                # Saving on every eval means a late overfitting run quietly
                # overwrites the good model with a worse one.
                mark = ""
                if va < best_val:
                    best_val = va
                    save_checkpoint(model, tok, args.arch, path, step=step,
                                    best_val=best_val)
                    mark = "  <- best, saved"
                print(f"step {step:6,}  train {tr:.4f}  val {va:.4f}  "
                      f"lr {lr:.4f}  ({elapsed:.0f}s){mark}")
                sample = model.generate(tok, n_chars=140, temperature=0.8)
                print(f"    {sample.strip()[:120]!r}\n")
    except KeyboardInterrupt:
        print("\ninterrupted")

    if best_val == float("inf"):               # stopped before any eval
        save_checkpoint(model, tok, args.arch, path, step=step,
                        best_val=best_val)
        print(f"saved to {path}")
    else:
        print(f"best val loss {best_val:.4f}, saved to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
