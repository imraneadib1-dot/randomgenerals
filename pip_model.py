"""Loading the pip 1.0 weights: an open-weight base, plus our adapter.

    import pip_model
    tokenizer, model, info = pip_model.load()            # what is trained
    tokenizer, model, info = pip_model.load(base=pip_model.SMOKE_BASE)

Shared by the engine (pip_server.py) and the trainer
(finetune/train_pip.py), so the model that is served is loaded exactly
the way the model that was trained was.

THE BASE

Qwen3.5-4B: Apache 2.0, a 262,144-token context window, and four
billion parameters - the largest current open-weight model that can be
both fine-tuned and served on one 8 GB card (4-bit, about 3.5 GB of
weights, the rest for activations). A bigger base answers better and
the code does not care which it is given: pass --base to the trainer
on a machine with the memory for it. The licence asks that the notice
stay with the weights; finetune/NOTICE is that notice.

HOW IT IS LOADED

  CUDA + bitsandbytes   4-bit NF4. The only way 4B fits beside its own
                        gradients in 8 GB.
  CUDA                  bf16.
  neither               float32 on the CPU. Fine for the 0.5B smoke
                        base, not for anything a person would wait on.
"""
from __future__ import annotations

import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

DEFAULT_BASE = "Qwen/Qwen3.5-4B"
# Small enough to train and answer on a CPU in minutes. It proves the
# pipeline end to end; it is not the product.
SMOKE_BASE = "Qwen/Qwen2.5-0.5B-Instruct"

ADAPTER_DIR = os.path.join(ROOT, "finetune", "pip-1.0-adapter")
RECORD = "pip.json"          # the training record, beside the adapter


def has_adapter(path: str = ADAPTER_DIR) -> bool:
    return os.path.exists(os.path.join(path, "adapter_config.json"))


def record(path: str = ADAPTER_DIR) -> dict:
    """What train_pip.py wrote about the adapter at `path`, or {}."""
    try:
        with open(os.path.join(path, RECORD), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def load(base: str | None = None, adapter: str | None = None,
         four_bit: bool | None = None, merge: bool = True):
    """-> (tokenizer, model, info).

    `adapter` is a directory written by train_pip.py. With one and no
    `base`, the base is whichever the adapter was trained on. `merge`
    folds the adapter into the weights so answering costs what the plain
    base costs; a 4-bit model cannot be merged into and keeps it beside.
    """
    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if adapter and not base:
        base = record(adapter).get("base")
        if not base:
            with open(os.path.join(adapter, "adapter_config.json"),
                      encoding="utf-8") as fh:
                base = json.load(fh).get("base_model_name_or_path")
    base = base or DEFAULT_BASE

    cuda = torch.cuda.is_available()
    if four_bit is None:
        four_bit = cuda and bool(importlib.util.find_spec("bitsandbytes"))
    kwargs = {}
    if four_bit:
        kwargs["quantization_config"] = transformers.BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16)
        kwargs["device_map"] = {"": 0}
    elif cuda:
        kwargs["dtype"] = torch.bfloat16
        kwargs["device_map"] = {"": 0}
    else:
        kwargs["dtype"] = torch.float32

    tokenizer = AutoTokenizer.from_pretrained(base)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    try:
        model = AutoModelForCausalLM.from_pretrained(base, **kwargs)
    except ValueError:
        # A base that also reads images registers under the multimodal
        # class, not the causal one. Text goes through it the same way.
        model = transformers.AutoModelForMultimodalLM.from_pretrained(
            base, **kwargs)

    info = {"base": base, "adapter": None, "four_bit": bool(four_bit),
            "device": "cuda" if cuda else "cpu"}
    if adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, adapter)
        if merge and not four_bit:
            model = model.merge_and_unload()
        info["adapter"] = os.path.basename(os.path.normpath(adapter))
        info.update({k: v for k, v in record(adapter).items()
                     if k in ("trained_at", "steps", "val_loss", "examples")})
    model.eval()
    return tokenizer, model, info
