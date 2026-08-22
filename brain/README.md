
# brain/ — a neural network built from scratch

A character-level language model written in plain NumPy. No PyTorch, no
TensorFlow, no autograd. Every matrix multiply, every derivative, and the
optimiser are written out by hand and checked against calculus.

It plugs into the chat app as a provider called **Local brain**, alongside
Ollama.

## The files

| file | what it is |
|---|---|
| `data.py` | text → vocabulary → batches of integers |
| `model.py` | `CharMLP` — fixed 16-character window, plain SGD |
| `rnn.py` | `CharRNN` — recurrent, carries memory forward, Adam |
| `rnn_deep.py` | `CharRNN2` — two stacked recurrent layers, same idea, more links |
| `train.py` | the training loop, checkpointing, resume |
| `serve.py` | loads a checkpoint and streams text to `app.py` |
| `corpus/` | the training texts (several public-domain books) |

## Quick start

```bash
pip install -r ../requirements.txt

python brain/train.py              # trains the RNN
python -m brain.serve rnn "The nature of water is"   # sample from it
```

Then start the app and pick **Local brain** as the channel.

Other useful commands:

```bash
python -m brain.rnn                # prove backprop-through-time is correct
python -m brain.rnn_deep           # same check for the 2-layer RNN
python -m brain.model              # same check for the MLP
python brain/train.py --arch mlp   # train the simpler windowed model
python brain/train.py --arch rnn2  # train the two-layer RNN
python brain/train.py --steps 2000 # shorter run
python brain/train.py --fresh      # ignore an existing checkpoint
```

Training resumes from its checkpoint automatically. Ctrl-C is safe.

## The three architectures

**`CharMLP`** sees a fixed window — sixteen characters, then a hard wall.
Widening the window grows the first weight matrix with it, so memory costs
parameters and can never exceed the number compiled in.

**`CharRNN`** links each step to the one before:

```
h_t = tanh( x_t @ Wxh  +  h_{t-1} @ Whh  +  bh )
             ^^^^^^^^^     ^^^^^^^^^^^^^
             what's new    what it remembers
```

`h` is a running summary of everything read so far, fed back in at every
step. Memory costs no extra parameters and has no fixed length. It also
predicts at *every* position rather than just the last, so one batch
produces 4096 training signals instead of 64.

**`CharRNN2`** (`rnn_deep.py`) stacks a second `CharRNN`-style layer on top
of the first, fed not by the raw character but by what layer 1 already made
of it at that same step:

```
h1_t = tanh( x_t  @ Wxh1 + h1_{t-1} @ Whh1 + bh1 )
h2_t = tanh( h1_t @ Wxh2 + h2_{t-1} @ Whh2 + bh2 )
```

Layer 1 only ever sees raw characters, so what it learns is necessarily
low-level - which letter tends to follow which. Layer 2 never sees a
character directly; its only input is what layer 1 already condensed, so
anything it finds has to be built *from* layer 1's patterns. That's what
"more neuron links" buys concretely: layer 1's hidden units now connect
forward into a whole second layer, not just back into themselves. The
honest cost is speed - roughly 4-5x slower per training step than the
single layer at the same hidden width, because layer 2's input depends on
the recurrent loop and can't be precomputed outside it the way layer 1's
can (see `rnn_deep.py`'s docstring for the full explanation).

Measured on the current corpus (7 books, 92-character vocabulary, random
guessing = 4.52):

| model | params | val loss | steps | wall time to best |
|---|---|---|---|---|
| `CharMLP` | 155k | 1.83 | 20,000 | - |
| `CharRNN` | 348k | 1.46 | 28,000 | ~47 min |
| `CharRNN2` | 873k | **1.44** | 8,000 | ~34 min |

The answer to "was the extra layer worth 2.5x the parameters and 4-5x
the per-step cost": yes, on both measures that matter. `CharRNN2` beat
`CharRNN`'s best score using well under a third of the training steps,
and - because each of those steps costs more but there are so many
fewer of them - it got there in *less* wall-clock time overall despite
being slower per step. Layer 2 building on layer 1's patterns rather
than raw characters isn't just a nicer story than a wider single layer;
it measurably found a better model faster.

Sample at the final checkpoint (temperature 0.8, so not cherry-picked
for coherence): *"at a time and feet and Elizabeth generally within
some propactions of unit it without spent of the last systematic
Seeez..."* — still a character model with no notion of meaning, but
`CharRNN`'s "the RNN passes the MLP's final score within its first 500
steps" pattern repeated one level up: `CharRNN2` passed `CharRNN`'s
final score well before its own run finished either.

## What this model can and cannot do

It is a ~200,000-parameter character model trained on one book. It has no
concept of questions, answers, facts, or instructions. It learned which
letter tends to follow which. Given your message it continues in the voice
of its training text — that is the whole of it.

This is worth seeing plainly rather than being disappointed by. Everything a
large model does beyond this — answering, reasoning, following instructions —
comes from being thousands of times larger and trained on a corpus millions
of times bigger. The machinery underneath is what is in `model.py` and
`rnn.py`, and not much more.

## Notes on things that turned out to matter

Four fixes that each mattered more than they look:

- **Gradient checking.** `python -m brain.rnn` compares every hand-derived
  gradient against a brute-force numerical estimate. An off-by-one in the
  time loop still trains — just badly — and you would never spot it from the
  loss curve. Worth running after any change to `backward()`.

- **Time-major arrays.** `hs` is `(T, B, H)`, not `(B, T, H)`, so `hs[t]` is
  one contiguous block. Indexed the other way every matrix multiply in the
  loop silently copies it first, which cost more than the arithmetic did.

- **BLAS thread count.** OpenBLAS defaults to one thread per core. The
  matrices inside the recurrent loop are small enough that co-ordinating 16
  threads costs far more than the work they do — 16 threads ran at 0.44s per
  step, 4 threads at 0.10s. `train.py` and `serve.py` cap it before NumPy
  loads.

- **Honest validation.** The held-out split is every 10th *chunk*, not the
  tail. Holding back the tail of a book measures a distribution shift
  (appendices, indexes, and in this case 18KB of Project Gutenberg licence)
  rather than generalisation, and reads high forever no matter how well the
  model learns. `strip_gutenberg()` removes the boilerplate; characters
  appearing fewer than 50 times are dropped from the vocabulary.

Together the last two took the reported train/val gap from 0.42 to roughly
zero — most of that "overfitting" was never real.

## Using your own text

Drop any plain-text `.txt` file(s) into `corpus/` (aim for 500KB or more
total) and retrain with `--fresh`. The vocabulary is rebuilt from the text,
so checkpoints from a different corpus will not load — training detects
this and starts over rather than producing garbage.
