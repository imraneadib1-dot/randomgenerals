"""LoRA, written out rather than imported.

    from finetune.lora import attach, trainable, save, load, merge

WHAT LoRA IS, IN ONE PARAGRAPH

Fine-tuning a model normally means changing all of its weights, which
for a 0.5B model is 500 million numbers to compute gradients for and
two gigabytes to store per copy. LoRA (Hu et al. 2021) says: leave W
alone and learn a small correction beside it.

    y = x @ W  +  (x @ A @ B) * (alpha / r)
        ^^^^^^     ^^^^^^^^^^^
        frozen     the only thing that learns

A is (in x r) and B is (r x out), with r small - 8 here. For a 896x896
attention projection that is 2 * 896 * 8 = 14,336 trainable numbers in
place of 802,816: under two percent. A starts random and B starts at
ZERO, so at step 0 the correction is exactly zero and the model is
bit-for-bit the base model - training can only improve on it, never
begin by breaking it.

WHY BY HAND

The rest of brain/ derives its own gradients; this does not need to,
because autograd is doing that. What it avoids is a dependency: peft
pins transformers versions, and this file is forty lines of linear
algebra that will still work in five years. Only the projections inside
attention are adapted, which is where the published ablations put
nearly all of the benefit.
"""
from __future__ import annotations

import json
import os

import torch
import torch.nn as nn

# The four projections inside each attention block. The MLP could be
# adapted too and roughly doubles the trainable count for a small gain;
# on a task this narrow it is not worth the memory.
TARGETS = ("q_proj", "k_proj", "v_proj", "o_proj")


class LoRALinear(nn.Module):
    """A frozen Linear with a trainable low-rank correction beside it."""

    def __init__(self, base: nn.Linear, r: int = 8, alpha: int = 16,
                 dropout: float = 0.05):
        super().__init__()
        self.base = base
        for p in self.base.parameters():
            p.requires_grad = False
        self.r = r
        self.scale = alpha / r
        self.drop = nn.Dropout(dropout)
        # Kept in float32 even when the base is bf16: these are the only
        # numbers being optimised, and Adam on bf16 parameters loses
        # updates smaller than the format can represent.
        self.A = nn.Parameter(torch.zeros(base.in_features, r, dtype=torch.float32))
        self.B = nn.Parameter(torch.zeros(r, base.out_features, dtype=torch.float32))
        # A random, B zero: the product is zero at step 0, so the
        # adapted model starts out exactly the base model.
        nn.init.normal_(self.A, std=1.0 / r)

    def forward(self, x):
        out = self.base(x)
        lora = self.drop(x).to(self.A.dtype) @ self.A @ self.B
        return out + (lora * self.scale).to(out.dtype)


def attach(model, r=8, alpha=16, dropout=0.05, targets=TARGETS):
    """Replace every targeted Linear in-place. -> how many were wrapped."""
    n = 0
    for name, module in list(model.named_modules()):
        for child_name, child in list(module.named_children()):
            if child_name in targets and isinstance(child, nn.Linear):
                setattr(module, child_name,
                        LoRALinear(child, r=r, alpha=alpha, dropout=dropout))
                n += 1
    for name, p in model.named_parameters():
        p.requires_grad = name.endswith(".A") or name.endswith(".B")
    return n


def trainable(model):
    """-> (list of parameters that learn, how many numbers that is)."""
    params = [p for p in model.parameters() if p.requires_grad]
    return params, sum(p.numel() for p in params)


def save(model, path, meta=None):
    """Just the adapters - a few megabytes, not the whole model."""
    state = {k: v.detach().cpu() for k, v in model.state_dict().items()
             if k.endswith(".A") or k.endswith(".B")}
    torch.save({"lora": state, "meta": meta or {}}, path)
    if meta:
        with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)


def load(model, path):
    """Put saved adapters back onto a freshly attached model."""
    blob = torch.load(path, map_location="cpu", weights_only=True)
    missing = model.load_state_dict(blob["lora"], strict=False)
    unexpected = [k for k in missing.unexpected_keys]
    if unexpected:
        raise ValueError("adapter does not fit this model: %s" % unexpected[:3])
    return blob.get("meta", {})


def merge(model):
    """Fold every correction into the weight beside it and hand back a
    plain model. -> how many layers were folded.

    W' = W + (A @ B) * scale. Needed before exporting to GGUF, which has
    no notion of an adapter: llama.cpp wants one set of weights.
    Transposed because nn.Linear stores weight as (out, in).
    """
    n = 0
    for module in model.modules():
        for child_name, child in list(module.named_children()):
            if isinstance(child, LoRALinear):
                delta = (child.A @ child.B).T * child.scale
                with torch.no_grad():
                    child.base.weight += delta.to(child.base.weight.dtype)
                setattr(module, child_name, child.base)
                n += 1
    return n
