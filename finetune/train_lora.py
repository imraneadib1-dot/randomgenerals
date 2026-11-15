"""Teach a small open model to draw the diagrams, from the big one's work.

    python -m finetune.train_lora                 # CPU is fine, ~40 min
    python -m finetune.train_lora --base HuggingFaceTB/SmolLM2-360M-Instruct
    python -m finetune.train_lora --steps 50 --smoke   # does it run at all
    python -m finetune.train_lora --task blender       # the Blender script model

WHAT IS BEING TRAINED, AND WHAT IS NOT

The base model is somebody else's - Qwen2.5-0.5B-Instruct, Apache 2.0,
pretrained on trillions of tokens by Alibaba. Nothing here could
reproduce that and this file does not pretend to. What is ours is the
correction learned on top: a few million numbers (finetune/lora.py)
trained on pairs that a large hosted model produced and mermaid itself
verified.

That distinction is the whole reason this is worth doing. A 0.5B model
asked to chat is far worse than a 120B one, and no amount of tuning
closes that. A 0.5B model asked to do ONE narrow, checkable thing -
turn a request into Mermaid that parses - can match it, because the
task does not need general knowledge, only a form. And it runs on the
VM's own cores for nothing, where the 120B costs a call.

HOW THE LOSS IS COMPUTED

Each example is laid out in the base model's own chat format:

    <system>  the app's DIAGRAM_SYSTEM_PROMPT, unchanged
    <user>    the request
    <assistant>  the verified Mermaid            <- loss only here

The prompt tokens are masked out with -100. Training on them too would
spend the model's capacity learning to recite a system prompt it will
be handed at inference anyway.

THE BLENDER TASK (--task blender)

The same recipe on finetune/data/blender.jsonl (finetune/make_blender_data.py)
plus the builds people kept with the pop-up's "Keep as example". The
system prompt is the pop-up's own Build prompt and the answer is the
verified script in a ```python block, so the trained model is asked
exactly what the big ones are. The base is Qwen2.5-Coder-0.5B: a model
pretrained on code learns bpy's shapes from far fewer examples than a
chat model does. Scripts are long - 1,500 to 2,500 tokens - so examples
that do not fit MAX_LEN are dropped rather than cut: a script cut off
half-way teaches the model to stop half-way.

What to expect from it: a small model can learn to write like the
models that wrote its examples, on the kinds of things they built. It
will not write better than them, and on requests unlike its examples
it does worse. What it gives is a model that runs here, with no key and
no network - served by finetune/serve_blender.py.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(HERE, "data", "diagrams.jsonl")
OUT = os.path.join(HERE, "adapter-diagram.pt")
sys.path.insert(0, ROOT)

DEFAULT_BASE = "Qwen/Qwen2.5-0.5B-Instruct"
MAX_LEN = 768

# Per task: where the pairs are, where the adapter goes, the base model,
# the longest example kept, and which field holds the answer.
TASKS = {
    "diagram": {"data": [DATA], "out": OUT, "base": DEFAULT_BASE,
                "max_len": MAX_LEN, "answer": "mermaid"},
    "blender": {"data": [os.path.join(HERE, "data", "blender.jsonl"),
                         os.path.join(os.path.expanduser("~"), ".randomgenerals",
                                      "blender-examples.jsonl")],
                "out": os.path.join(HERE, "adapter-blender.pt"),
                "base": "Qwen/Qwen2.5-Coder-0.5B-Instruct", "max_len": 3584,
                "answer": "code"},
}


def load_pairs(*paths):
    """Rows from every file that exists. A request in two files (the
    dataset and a kept build) is trained on once, the later one."""
    rows = {}
    for path in paths or (DATA,):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    rows[row["request"].strip().lower()] = row
    return list(rows.values())


def blender_prompt(rows):
    """The pop-up's Build prompt, for the Blender version the scripts ran
    in, and the scene note an empty scene gets - so training and use see
    the same words."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "blender_listener", os.path.join(ROOT, "blender-addon", "blender_listener.py"))
    listener = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(listener)
    versions = [r.get("blender") for r in rows if r.get("blender")]
    version = max(set(versions), key=versions.count) if versions else "4.2+"
    return listener.system_prompt(version), "\n\n" + listener.scene_note({}, cleared=True)


def build(tokenizer, system, request, mermaid, max_len=MAX_LEN):
    """One example -> (input_ids, labels) with the prompt masked.

    The prompt is templated twice: once alone, to find out how long it
    is, and once with the answer. The difference is where the assistant
    turn starts, which is where the loss is allowed to begin. Counting
    tokens any other way (splitting on a marker string, say) breaks the
    moment a template changes its special tokens.
    """
    prompt = tokenizer.apply_chat_template(
        [{"role": "system", "content": system},
         {"role": "user", "content": request}],
        tokenize=False, add_generation_prompt=True)
    full = prompt + mermaid + tokenizer.eos_token

    prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    full_ids = tokenizer(full, add_special_tokens=False)["input_ids"][:max_len]
    labels = list(full_ids)
    for i in range(min(len(prompt_ids), len(labels))):
        labels[i] = -100
    return full_ids, labels


def collate(batch, pad_id):
    import torch
    width = max(len(ids) for ids, _ in batch)
    input_ids, labels, mask = [], [], []
    for ids, lab in batch:
        pad = width - len(ids)
        input_ids.append(ids + [pad_id] * pad)
        labels.append(lab + [-100] * pad)
        mask.append([1] * len(ids) + [0] * pad)
    return (torch.tensor(input_ids), torch.tensor(labels), torch.tensor(mask))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--task", choices=sorted(TASKS), default="diagram")
    ap.add_argument("--base", default=None, help="default: the task's")
    ap.add_argument("--data", default=None, help="default: the task's")
    ap.add_argument("--out", default=None, help="default: the task's")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--steps", type=int, default=0, help="override epochs")
    ap.add_argument("--batch", type=int, default=0, help="default: 2, or 1 for blender")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=8)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--val", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--smoke", action="store_true",
                    help="tiny run to prove the pipeline, not a model")
    args = ap.parse_args(argv)
    task = TASKS[args.task]
    args.base = args.base or task["base"]
    args.out = args.out or task["out"]
    args.batch = args.batch or (1 if args.task == "blender" else 2)
    data_paths = [args.data] if args.data else task["data"]

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from finetune import lora as lora_mod

    torch.manual_seed(args.seed)
    random.seed(args.seed)

    rows = load_pairs(*data_paths)
    if len(rows) < 20 and not args.smoke:
        print("only %d pairs in %s - run `python -m finetune.%s` "
              "first; a few hundred is where this starts to work."
              % (len(rows), ", ".join(data_paths),
                 "make_blender_data" if args.task == "blender" else "make_data"))
        return 2
    random.shuffle(rows)
    cut = max(1, int(len(rows) * args.val))
    val_rows, train_rows = rows[:cut], rows[cut:]
    print("%d pairs: %d train, %d held out" % (len(rows), len(train_rows), len(val_rows)))

    if args.task == "blender":
        system, note = blender_prompt(rows)
    else:
        os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:1")
        import app as appmod
        system, note = appmod.DIAGRAM_SYSTEM_PROMPT, ""

    print("loading %s ..." % args.base)
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForCausalLM.from_pretrained(
        args.base, dtype=torch.float32)
    model.config.use_cache = False
    if hasattr(model, "gradient_checkpointing_enable"):
        # Trades compute for memory: activations are recomputed in the
        # backward pass instead of being kept. On CPU the memory is what
        # binds, not the arithmetic.
        model.gradient_checkpointing_enable()
    print("  loaded in %.0fs" % (time.time() - t0))

    wrapped = lora_mod.attach(model, r=args.rank, alpha=args.alpha)
    params, n_train = lora_mod.trainable(model)
    total = sum(p.numel() for p in model.parameters())
    print("LoRA rank %d on %d projections: %s trainable of %s (%.2f%%)"
          % (args.rank, wrapped, f"{n_train:,}", f"{total:,}",
             100.0 * n_train / total))

    pad_id = tokenizer.pad_token_id or tokenizer.eos_token_id

    def answer(row):
        if args.task == "blender":
            return "```python\n%s\n```" % row["code"].strip()
        return row[task["answer"]]

    def examples(rows_):
        if args.task != "blender":          # the diagram task, as it always was
            return [build(tokenizer, system, r["request"], answer(r),
                          max_len=task["max_len"]) for r in rows_], 0
        out, dropped = [], 0
        for r in rows_:
            ids, lab = build(tokenizer, system, r["request"] + note, answer(r),
                             max_len=task["max_len"] + 1)
            if len(ids) > task["max_len"]:
                dropped += 1          # cut short, it would teach stopping half-way
                continue
            out.append((ids, lab))
        return out, dropped

    train_ex, dropped_t = examples(train_rows)
    val_ex, dropped_v = examples(val_rows)
    if dropped_t or dropped_v:
        print("  dropped %d examples longer than %d tokens"
              % (dropped_t + dropped_v, task["max_len"]))
    if not train_ex:
        print("no examples fit in %d tokens" % task["max_len"])
        return 2
    lengths = [len(i) for i, _ in train_ex]
    print("  example length: median %d tokens, longest %d"
          % (sorted(lengths)[len(lengths) // 2], max(lengths)))

    steps_per_epoch = max(1, math.ceil(len(train_ex) / args.batch))
    total_steps = args.steps or int(steps_per_epoch * args.epochs)
    if args.smoke:
        total_steps = min(total_steps, 6)
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=args.lr, total_steps=total_steps, pct_start=0.1)

    def val_loss():
        model.eval()
        seen = losses = 0
        with torch.no_grad():
            for i in range(0, len(val_ex), args.batch):
                chunk = val_ex[i:i + args.batch]
                ids, lab, mask = collate(chunk, pad_id)
                out = model(input_ids=ids, attention_mask=mask, labels=lab)
                losses += float(out.loss) * len(chunk)
                seen += len(chunk)
        model.train()
        return losses / max(1, seen)

    print("training %d steps (batch %d)" % (total_steps, args.batch))
    model.train()
    order = list(range(len(train_ex)))
    best = {"loss": float("inf"), "step": 0}
    started = time.time()
    step = 0
    while step < total_steps:
        random.shuffle(order)
        for i in range(0, len(order), args.batch):
            if step >= total_steps:
                break
            chunk = [train_ex[j] for j in order[i:i + args.batch]]
            ids, lab, mask = collate(chunk, pad_id)
            out = model(input_ids=ids, attention_mask=mask, labels=lab)
            out.loss.backward()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            if step % 10 == 0 or step == total_steps:
                per = (time.time() - started) / step
                print("  step %4d/%d  loss %.4f  %.1fs/step  eta %.0fm"
                      % (step, total_steps, float(out.loss), per,
                         (total_steps - step) * per / 60))
            if step % 50 == 0 and val_ex:
                vl = val_loss()
                print("    held-out loss %.4f%s" % (
                    vl, "  (best)" if vl < best["loss"] else ""))
                if vl < best["loss"]:
                    best = {"loss": vl, "step": step}
                    lora_mod.save(model, args.out, meta={
                        "base": args.base, "rank": args.rank,
                        "alpha": args.alpha, "step": step,
                        "val_loss": round(vl, 4), "pairs": len(rows)})

    final = val_loss() if val_ex else float("nan")
    print("final held-out loss %.4f (best %.4f at step %d)"
          % (final, best["loss"], best["step"]))
    if final <= best["loss"] or not val_ex:
        lora_mod.save(model, args.out, meta={
            "base": args.base, "rank": args.rank, "alpha": args.alpha,
            "step": step, "val_loss": round(final, 4), "pairs": len(rows)})
    print("saved %s (%.1f MB) in %.0f minutes"
          % (args.out, os.path.getsize(args.out) / 1e6, (time.time() - started) / 60))
    if args.task == "blender":
        print("\nnext: python -m finetune.serve_blender   # then pick "
              "'Ours - trained here' in the Blender pop-up")
    else:
        print("\nnext: python -m agents.evaluate   # the graded cases, "
              "channel by channel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
