"""Export pip 1.0 for fast serving: merge the adapter, register the result.

    python -m finetune.export_pip            # the trained adapter
    python -m finetune.export_pip --smoke    # the smoke adapter
    python -m finetune.export_pip --quantize q4_K_M   # smaller, for a big base

WHY THIS EXISTS

pip_server.py can run the weights itself, through PyTorch. That is the
simplest thing that works and it is slow: measured on this machine's
RTX 5050, 4.5 tokens a second for a 0.5B model, with the GPU at 17% -
the time goes to launching thousands of tiny operations per token, not
to arithmetic. The llama.cpp runtime that Ollama ships does the same
arithmetic without that overhead: 24 tokens a second for a model six
times the size, on the same card.

So this folds the adapter into the base weights (W' = W + BA), writes
them out, and has the local Ollama convert and store them as the model
`pip-1.0`. pip_server.py then generates through that runtime - still
adding the identity, enforcing the owner answer, keeping conversations -
and nothing in front of the engine can tell the difference except by
the speed.

WHAT IT WRITES

  finetune/pip-1.0-merged[-smoke]/    merged weights + tokenizer + Modelfile
  an Ollama model named pip-1.0       (remove with: ollama rm pip-1.0)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pip_identity                                        # noqa: E402
import pip_model                                           # noqa: E402

MERGED = os.path.join(HERE, "pip-1.0-merged")


def merge(adapter: str, out: str) -> dict:
    """Fold `adapter` into its base and save plain weights to `out`."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    record = pip_model.record(adapter)
    base = record.get("base") or pip_model.DEFAULT_BASE
    tokenizer = AutoTokenizer.from_pretrained(base)
    # On the CPU, in half precision: a merge is one addition per layer
    # and needs no GPU, and the GPU may be busy serving.
    try:
        model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float16)
    except ValueError:
        import transformers
        model = transformers.AutoModelForMultimodalLM.from_pretrained(
            base, dtype=torch.float16)
    model = PeftModel.from_pretrained(model, adapter).merge_and_unload()
    if os.path.isdir(out):
        shutil.rmtree(out)
    model.save_pretrained(out, safe_serialization=True)
    tokenizer.save_pretrained(out)
    return dict(record, base=base)


def modelfile(out: str) -> str:
    """Write the Modelfile beside the weights. -> its path."""
    path = os.path.join(out, "Modelfile")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("FROM .\n")
        # What a caller gets when it sends no system prompt of its own.
        # The engine always sends one; this is for the bare runtime.
        fh.write('SYSTEM """%s"""\n' % pip_identity.SYSTEM)
        fh.write("PARAMETER temperature 0.2\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="export_pip",
        description="Merge the %s adapter and register it for fast serving."
                    % pip_identity.FULL_NAME)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--smoke", action="store_true",
                    help="export the adapter from `train_pip --smoke`")
    ap.add_argument("--out", default=None, help="where the merged weights go")
    ap.add_argument("--quantize", default=None, metavar="LEVEL",
                    help="e.g. q4_K_M: smaller and faster, for a base "
                         "that does not fit the GPU at full precision")
    ap.add_argument("--no-register", action="store_true",
                    help="write the merged weights and stop")
    args = ap.parse_args(argv)

    adapter = args.adapter or (pip_model.ADAPTER_DIR + "-smoke" if args.smoke
                               else pip_model.ADAPTER_DIR)
    if not pip_model.has_adapter(adapter):
        print("No adapter at %s. Train one first: python -m finetune.train_pip%s"
              % (adapter, " --smoke" if args.smoke else ""))
        return 2
    out = args.out or (MERGED + "-smoke" if args.smoke else MERGED)

    t0 = time.time()
    print("%s: merging %s into its base ..." % (
        pip_identity.FULL_NAME, os.path.relpath(adapter, ROOT)))
    record = merge(adapter, out)
    path = modelfile(out)
    print("  merged %s + adapter (step %s) -> %s in %.0fs"
          % (record["base"], record.get("steps", "?"),
             os.path.relpath(out, ROOT), time.time() - t0))
    if args.no_register:
        return 0

    if not shutil.which("ollama"):
        print("Ollama is not installed, so the weights were merged but not "
              "registered. pip_server.py will keep running them through "
              "PyTorch.")
        return 1
    cmd = ["ollama", "create", pip_identity.MODEL_ID, "-f", path]
    if args.quantize:
        cmd += ["--quantize", args.quantize]
    print("  registering as %s ..." % pip_identity.MODEL_ID)
    done = subprocess.run(cmd, cwd=out, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if done.returncode != 0:
        print("  the runtime could not import these weights:\n    %s\n"
              "pip_server.py will keep running them through PyTorch."
              % (done.stderr or done.stdout).strip().splitlines()[-1])
        return 1
    print("%s: registered. Start the engine and it will use the fast "
          "runtime: python pip_server.py" % pip_identity.FULL_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
