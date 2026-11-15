"""Fine-tune pip 1.0: a LoRA adapter on an open-weight base, with PEFT.

    python -m finetune.train_pip                # the real run - needs a GPU
    python -m finetune.train_pip --smoke        # a small base on the CPU, minutes
    python -m finetune.train_pip --base <hf id> --epochs 4 --rank 32

WHAT IS TRAINED

The base model (pip_model.DEFAULT_BASE) is somebody else's work and is
left frozen. What is trained is a LoRA adapter beside its attention and
MLP projections - under one percent of the parameters - on the dataset
finetune/pip_data.py writes: who pip 1.0 is and who owns it, ordinary
questions that must keep their ordinary answers, and the structured
output the app asks for.

The loss is taken on the answer only. Each example is laid out in the
base model's own chat template; the prompt tokens are masked with -100,
because training on them would spend the adapter learning to recite a
system prompt it is handed at inference anyway.

WHAT IT WRITES

  finetune/pip-1.0-adapter/     the adapter (PEFT format) and pip.json,
                                the record of how it was trained
  finetune/pip-1.0-adapter-smoke/   the same, from --smoke

pip_server.py loads the first by default. After training, the held-out
owner questions are asked of the weights alone - no system prompt, no
guard - and the share that name the owner is printed and recorded, so
"the identity is in the weights" is a number, not a hope.

ON THIS MACHINE

The real run wants a CUDA build of torch and bitsandbytes:

    pip install torch --index-url <the CUDA index pytorch.org gives for this GPU>
    pip install bitsandbytes

With them, 4-bit Qwen3.5-4B trains in about 6 GB. Without them this
script says so and stops rather than starting a 4B model on a CPU.
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
sys.path.insert(0, ROOT)

import pip_identity                                        # noqa: E402
import pip_model                                           # noqa: E402
from finetune import pip_data                              # noqa: E402

SMOKE_OUT = pip_model.ADAPTER_DIR + "-smoke"

# Attention and MLP projections. On a task that includes changing what
# the model believes about itself, the MLP matters: attention moves
# information around, the MLP is where facts are stored.
TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj",
           "gate_proj", "up_proj", "down_proj"]


def lora_config(rank: int = 16, alpha: int | None = None,
                dropout: float = 0.05):
    """The PEFT configuration every run uses."""
    from peft import LoraConfig
    return LoraConfig(r=rank, lora_alpha=alpha or 2 * rank,
                      lora_dropout=dropout, bias="none",
                      target_modules=list(TARGETS), task_type="CAUSAL_LM")


def render(tokenizer, messages) -> tuple[str, str]:
    """-> (prompt, answer) in the base model's chat format.

    The prompt is everything up to where the assistant starts; thinking
    is off, as it is when the engine answers by default.
    """
    prompt = tokenizer.apply_chat_template(
        messages[:-1], tokenize=False, add_generation_prompt=True,
        enable_thinking=False)
    return prompt, messages[-1]["content"] + tokenizer.eos_token


def encode(tokenizer, messages, max_len: int):
    """One example -> (input_ids, labels) with the prompt masked, or
    None when it does not fit: an answer cut off half-way teaches the
    model to stop half-way."""
    prompt, answer = render(tokenizer, messages)
    prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    answer_ids = tokenizer(answer, add_special_tokens=False)["input_ids"]
    if len(prompt_ids) + len(answer_ids) > max_len:
        return None
    return prompt_ids + answer_ids, [-100] * len(prompt_ids) + answer_ids


def collate(batch, pad_id, device):
    import torch
    width = max(len(ids) for ids, _ in batch)
    ids, labels, mask = [], [], []
    for row, lab in batch:
        pad = width - len(row)
        ids.append(row + [pad_id] * pad)
        labels.append(lab + [-100] * pad)
        mask.append([1] * len(row) + [0] * pad)
    return (torch.tensor(ids, device=device),
            torch.tensor(labels, device=device),
            torch.tensor(mask, device=device))


def ask(tokenizer, model, question: str, system: str | None = None,
        max_new_tokens: int = 40) -> str:
    """The weights' own answer: greedy, no guard."""
    import torch
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": question}]
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True,
        enable_thinking=False)
    ids = tokenizer(prompt, return_tensors="pt",
                    add_special_tokens=False).to(model.device)
    with torch.no_grad():
        out = model.generate(**ids, max_new_tokens=max_new_tokens,
                             do_sample=False,
                             pad_token_id=tokenizer.pad_token_id)
    return tokenizer.decode(out[0][ids["input_ids"].shape[1]:],
                            skip_special_tokens=True).strip()


def owner_accuracy(tokenizer, model, questions, system=None) -> float:
    """The share of owner questions the weights answer correctly."""
    if not questions:
        return float("nan")
    right = sum(pip_identity.names_owner(ask(tokenizer, model, q, system))
                for q in questions)
    return right / len(questions)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="train_pip",
        description="Fine-tune %s with LoRA." % pip_identity.FULL_NAME)
    ap.add_argument("--base", default=None,
                    help="default: %s (%s with --smoke)"
                         % (pip_model.DEFAULT_BASE, pip_model.SMOKE_BASE))
    ap.add_argument("--data", default=pip_data.OUT)
    ap.add_argument("--out", default=None, help="adapter directory")
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--steps", type=int, default=0, help="override epochs")
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--accumulate", type=int, default=2,
                    help="batches per optimiser step")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=0, help="default: 2 x rank")
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--val", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--smoke", action="store_true",
                    help="small base, CPU, no diagrams: proves the "
                         "pipeline end to end in minutes")
    args = ap.parse_args(argv)

    import torch
    from peft import get_peft_model, prepare_model_for_kbit_training

    base = args.base or (pip_model.SMOKE_BASE if args.smoke
                         else pip_model.DEFAULT_BASE)
    out = args.out or (SMOKE_OUT if args.smoke else pip_model.ADAPTER_DIR)
    if not args.smoke and not args.base and not torch.cuda.is_available():
        print("%s: this torch build has no CUDA, and %s is too large to "
              "train on a CPU.\n  - install a CUDA build of torch and "
              "bitsandbytes (see this file's docstring), or\n  - run "
              "`python -m finetune.train_pip --smoke` to prove the "
              "pipeline on a small base, or\n  - pass --base with a model "
              "this machine can hold."
              % (pip_identity.FULL_NAME, pip_model.DEFAULT_BASE))
        return 2

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    if not os.path.exists(args.data):
        pip_data.main(["--out", args.data])
    rows = pip_data.load(args.data)
    if args.smoke:
        # Diagrams are long; on a CPU they are most of the wall clock
        # and none of what the smoke run is proving.
        rows = [r for r in rows if r["kind"] != "diagram"]
    random.shuffle(rows)
    cut = max(1, int(len(rows) * args.val))
    val_rows, train_rows = rows[:cut], rows[cut:]

    print("%s: loading %s ..." % (pip_identity.FULL_NAME, base))
    t0 = time.time()
    tokenizer, model, info = pip_model.load(base, merge=False)
    if info["four_bit"]:
        model = prepare_model_for_kbit_training(model)
    model.config.use_cache = False
    config = lora_config(args.rank, args.alpha or None)
    model = get_peft_model(model, config)
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    n_all = sum(p.numel() for p in model.parameters())
    print("  loaded in %.0fs on %s%s. LoRA rank %d: %s trainable of %s (%.2f%%)"
          % (time.time() - t0, info["device"],
             ", 4-bit" if info["four_bit"] else "", config.r,
             f"{n_train:,}", f"{n_all:,}", 100.0 * n_train / n_all))

    def examples(rows_):
        made = [encode(tokenizer, r["messages"], args.max_len) for r in rows_]
        return [m for m in made if m], sum(1 for m in made if not m)

    train_ex, dropped_t = examples(train_rows)
    val_ex, dropped_v = examples(val_rows)
    if dropped_t or dropped_v:
        print("  dropped %d examples longer than %d tokens"
              % (dropped_t + dropped_v, args.max_len))
    if not train_ex:
        print("no examples fit in %d tokens" % args.max_len)
        return 2
    print("  %d examples: %d train, %d held out"
          % (len(train_ex) + len(val_ex), len(train_ex), len(val_ex)))

    device = model.device
    pad_id = tokenizer.pad_token_id
    params = [p for p in model.parameters() if p.requires_grad]
    batches = math.ceil(len(train_ex) / args.batch)
    total = args.steps or max(1, int(batches * args.epochs / args.accumulate))
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0)
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=args.lr, total_steps=total, pct_start=0.1)

    def val_loss():
        model.eval()
        seen = losses = 0.0
        with torch.no_grad():
            for i in range(0, len(val_ex), args.batch):
                chunk = val_ex[i:i + args.batch]
                ids, lab, mask = collate(chunk, pad_id, device)
                losses += float(model(input_ids=ids, attention_mask=mask,
                                      labels=lab).loss) * len(chunk)
                seen += len(chunk)
        model.train()
        return losses / max(1.0, seen)

    def save(step, vl):
        model.save_pretrained(out)
        with open(os.path.join(out, pip_model.RECORD), "w", encoding="utf-8") as fh:
            json.dump({
                "model": pip_identity.MODEL_ID, "owner": pip_identity.OWNER,
                "base": base, "rank": config.r, "alpha": config.lora_alpha,
                "targets": list(TARGETS), "steps": step,
                "val_loss": round(vl, 4), "examples": len(train_ex),
                "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "smoke": bool(args.smoke)}, fh, indent=1)

    print("  training %d steps (batch %d x %d)"
          % (total, args.batch, args.accumulate))
    model.train()
    order = list(range(len(train_ex)))
    best = float("inf")
    started = time.time()
    step = micro = 0
    while step < total:
        random.shuffle(order)
        for i in range(0, len(order), args.batch):
            if step >= total:
                break
            chunk = [train_ex[j] for j in order[i:i + args.batch]]
            ids, lab, mask = collate(chunk, pad_id, device)
            loss = model(input_ids=ids, attention_mask=mask, labels=lab).loss
            (loss / args.accumulate).backward()
            micro += 1
            if micro % args.accumulate:
                continue
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            step += 1
            if step % 5 == 0 or step == total:
                per = (time.time() - started) / step
                print("  step %4d/%d  loss %.4f  %.1fs/step  eta %.0fm"
                      % (step, total, float(loss), per,
                         (total - step) * per / 60), flush=True)
            if val_ex and (step % 25 == 0 or step == total):
                vl = val_loss()
                better = vl < best
                print("    held-out loss %.4f%s" % (vl, "  (best)" if better else ""),
                      flush=True)
                if better:
                    best = vl
                    save(step, vl)
    if not os.path.exists(os.path.join(out, "adapter_config.json")):
        save(step, best if best < float("inf") else float("nan"))

    # THE NUMBER THAT MATTERS: asked with no system prompt and no guard,
    # do the weights themselves name the owner?
    held_out = [r["messages"][-2]["content"] for r in val_rows
                if r["kind"] == "identity"
                and pip_identity.is_question(r["messages"][-2]["content"])][:24]
    model.eval()
    bare = owner_accuracy(tokenizer, model, held_out)
    prompted = owner_accuracy(tokenizer, model, held_out, pip_identity.SYSTEM)
    record = pip_model.record(out)
    record.update(owner_accuracy=round(bare, 3),
                  owner_accuracy_prompted=round(prompted, 3),
                  owner_questions=len(held_out))
    with open(os.path.join(out, pip_model.RECORD), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1)
    print("%s: owner named on %d held-out questions - %.0f%% from the "
          "weights alone, %.0f%% with the system prompt. The engine's "
          "guard makes it 100%% either way."
          % (pip_identity.FULL_NAME, len(held_out), 100 * bare, 100 * prompted))
    print("saved %s in %.0f minutes\nnext: python pip_server.py%s"
          % (out, (time.time() - started) / 60,
             " --adapter %s" % os.path.relpath(out, ROOT) if args.smoke else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
