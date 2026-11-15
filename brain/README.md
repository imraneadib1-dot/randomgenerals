# brain/ — the intent model

A small neural network, written from scratch in plain NumPy — no
PyTorch, no autograd, every gradient derived by hand — that reads each
incoming message and says which bay it belongs to: chat, code, image or
video. It answers in about a fifth of a millisecond on the VM's own CPU,
so a coding question typed into the chat bay gets the coding prompt
instead of the general one. It is the one model here that runs on every
request.

There used to be more in this folder: three character-level models
(an MLP and two RNNs) trained on public-domain books, which continued
text in the voice of whatever they had read. They were an exercise in
building the machinery, they were never wired into the live chat, and
they took 7.6MB of corpus with them, so they are gone. `git log` has
them if you ever want to look.

## Using it

```bash
python -m brain.intent              # train, save the checkpoint
python -m brain.intent --check      # gradient check
python -m brain.intent --evaluate   # k-fold, every example validated
python -m brain.intent --try "how do i center a div"
python -m brain.intent --from-threads   # also learn from your real traffic
python check_brain.py               # the full check suite
```

`app.py` loads `checkpoint-intent.npz` at boot. If NumPy is missing or
the checkpoint was trained with a different feature set, the app logs
`[intent] brain unavailable` and carries on without it — every message
then gets the general prompt, which is what the site did before this
model existed.

## What it scores

Five-fold cross-validation over two seeds, so every one of the 199
labelled examples in `intents.jsonl` is judged by a model that never
saw it:

| head | always guess the commonest | this model |
|---|---|---|
| bay | 50% | **86%** |
| web | 88% | 92% |
| deep | 65% | 72% |

Per bay: chat 93%, code 74%, image 82%, video 88%. On the 76% of
messages it is confident about (p ≥ 0.70) it is **95% correct**, and
that is the only head the app acts on. The web and deep heads are
returned and deliberately ignored: four and seven points over guessing
is not enough to change what the site does.

## How it works

One trunk, three heads:

```
features -> hidden (tanh) -> bay    (4-way softmax)
                          -> web    (sigmoid)
                          -> deep   (sigmoid)
```

The three questions are not independent — "draw me a fox" is an image
request *and* needs no web *and* needs no thinking — so they share a
hidden layer. That is the whole argument for one network rather than
three regressions: one representation, learned from all three labels
at once, on a corpus small enough that sharing evidence matters.

Two things cost real accuracy until they were found:

- **A single held-out split lied.** It flattered the bay head by seven
  points and could not tell a 16-unit model from a 32-unit one — with
  40 validation examples each one is 2.5% of the score. Every tuning
  decision made against it was noise. `cross_validate()` replaced it.
- **Class imbalance quietly picked a winner.** Half the corpus is chat,
  so the cheapest way to cut the loss was to answer "chat" more often:
  the code bay sat at 56%, and the confident mistakes were nearly all
  coding questions with no code in them ("how do i center a div").
  Class weighting plus `_CODEY` — a closed list of the trade's
  vocabulary — took code to 74% and the head as a whole to 86%.

## Files

| file | what it is |
|---|---|
| `intent.py` | the model — features, three heads, training, k-fold evaluation |
| `intents.jsonl` | its 199 labelled examples |
| `checkpoint-intent.npz` | its weights, 33 KB, loaded at boot by `app.py` (not in git) |

It gets better the same way the rest of the app does: `--from-threads`
trains on the messages your own users actually sent, labelled by the
bay they really sent them in, which is data nobody else has.
