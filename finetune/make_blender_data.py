"""Build a training set for the Blender model, verified by Blender itself.

    python -m finetune.make_blender_data --n 150
    python -m finetune.make_blender_data --n 5 --gap 0      # a quick look

WHAT THIS PRODUCES

finetune/data/blender.jsonl, one line per example:

    {"request": "a low-poly pine tree", "code": "import bpy\\n...",
     "model": "pip-1.0", "attempts": 1, "objects": 7,
     "blender": "5.1.2", "preview": "blender-previews/0007-a-low-poly-pine-tree.png"}

Every script in it ran in a background Blender without an error and
created objects - the same check the pop-up's Build makes. Requests the
models could not get to run go to blender-rejected.jsonl with the last
error, since those say which part of bpy the models get wrong.

Running is not the same as looking right: a script can run cleanly and
build a table with its legs beside it. So each kept script also renders
a preview into finetune/data/blender-previews/. Look through them and
delete the lines of any that look wrong, before training - a model
learns its teacher's mistakes as faithfully as the rest.

WHAT THE DATA IS FOR

Two things, and the first works from the first line:

  1. Examples. The pop-up and blender_controller.py show the model the
     verified script whose request is closest to the one being built.
     A model shown a script that ran in this Blender copies its working
     API calls and its way of placing parts.
  2. Training. finetune/train_lora.py --task blender teaches a small
     local model from these pairs, so it runs with no key and no
     network. It can learn to write like the big models, not better.

WHERE THE SCRIPTS COME FROM

The models the pop-up's Auto uses - pip 1.0 first, then whatever else is
set up - with the same prompt and retry loop, so a row here is
exactly what Build would have produced. Builds always run headless: a
Blender you have open is never touched.

PACED, AND RESUMABLE. The engine is one GPU, and if the website is
using it too its visitors share it.
--gap waits between requests (default 20s). Interrupt it and run it
again: requests already in either file are skipped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(HERE, "data")
PAIRS = os.path.join(DATA_DIR, "blender.jsonl")
REJECTS = os.path.join(DATA_DIR, "blender-rejected.jsonl")
PREVIEWS = os.path.join(DATA_DIR, "blender-previews")
sys.path.insert(0, ROOT)

# Hand-written, spread across what people model. A corpus of nothing but
# furniture teaches a model that everything has four legs.
SEEDS = [
    # furniture
    "a low-poly wooden chair", "a round dining table with four legs", "a three-legged stool",
    "a bookshelf with five shelves and some books", "a simple bed with a pillow and blanket",
    "an office desk with a drawer", "a sofa with two cushions", "a bar stool with a footrest ring",
    "a wooden bench", "a bedside table with a lamp", "a wardrobe with two doors",
    "a rocking chair", "a coffee table with a glass top", "a kitchen counter with a sink",
    # nature
    "a low-poly pine tree", "a low-poly tree with a round canopy", "a palm tree",
    "a cactus in a pot", "a rock formation of three stones", "a mushroom with a red spotted cap",
    "a sunflower", "a bush made of faceted spheres", "a tree stump with an axe in it",
    "a small island with a palm tree", "a flower in a vase", "a log pile",
    # buildings
    "a small cottage with a pitched roof, door and two windows", "a lighthouse",
    "a wooden fence section", "a well with a roof and bucket", "a castle tower with battlements",
    "a windmill", "a simple bridge over a gap", "a doghouse", "a market stall with an awning",
    "a street lamp", "a park bench and a bin", "a bus stop shelter",
    # vehicles
    "a low-poly car", "a simple truck", "a rowing boat", "a small sailboat",
    "a rocket with fins", "a cartoon airplane", "a bicycle", "a train engine",
    "a hot air balloon", "a submarine", "a tractor", "a skateboard",
    # props and objects
    "a coffee mug", "a table lamp", "a wine bottle and a glass", "a treasure chest",
    "a barrel", "a wooden crate", "a sword", "a shield", "a lantern", "a teapot",
    "a desk fan", "a guitar", "a laptop", "a smartphone on a stand", "an alarm clock",
    "a potted plant", "a candle on a holder", "a bowl of fruit", "a pencil and a notebook",
    "a pair of dice", "a chess pawn and a rook", "a trophy cup", "a picture frame",
    "a stack of books", "a headphones", "a microphone on a stand", "a toolbox",
    # food
    "a burger", "a slice of cake", "a donut with sprinkles", "a pizza", "an ice cream cone",
    "a sushi roll", "a carrot", "an apple with a leaf", "a cup of coffee with a saucer",
    # characters and creatures (simple, blocky)
    "a blocky robot", "a snowman", "a cartoon cat made of simple shapes", "a penguin",
    "a duck", "a low-poly fox", "a cartoon character made of primitives", "a fish",
    "a snail", "a spider", "a ghost", "an alien with antennae",
    # scenes and layouts
    "a living room corner with a sofa, lamp and rug", "a campfire with logs and stones",
    "a picnic blanket with a basket", "a small garden with a fence and flowers",
    "a desk setup with a monitor, keyboard and mug", "a playground slide",
    "a dock with a boat", "a bowling pin and ball",
    # techniques: the modifiers and operations people ask for by name
    "a cube with a cylinder hole cut through it using a boolean",
    "a chain of linked torus rings", "a spiral staircase", "a picket fence using an array modifier",
    "a smooth vase using a subdivision surface modifier", "a beveled metal box",
    "a gear with teeth", "a mirrored symmetrical spaceship", "a column with a fluted shaft",
    "a hollow pipe using solidify", "a grid of cubes of random heights", "a domino run",
    "a wavy terrain plane", "a donut with icing", "a honeycomb of hexagons",
    # materials and lighting asked for explicitly
    "a glowing neon sign shape with an emission material", "a glass sphere on a pedestal",
    "a shiny gold coin", "a product shot of a perfume bottle with a camera and area light",
    "a red metallic ball and a blue matte cube on a floor with a sun light",
]

MORE_REQUESTS_PROMPT = (
    "Write {n} short requests someone might type into a tool that builds 3D models "
    "in Blender from text. One per line, no numbering, no explanation. Each names "
    "one object or a small scene that can be built from simple shapes: furniture, "
    "props, buildings, vehicles, plants, food, simple creatures. Vary them widely. "
    "Keep each under 10 words. Do not repeat these:\n{seen}"
)


def _rows(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    return rows


def _slug(text, n):
    return "%04d-%s" % (n, re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50])


def _append(path, row):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def verdict(out):
    """Is a build fit to teach from? -> (keep, why not).

    Build hands over a model whose parts still float after the one round
    of "make them touch" - the user sees it and can fix it. Training data
    is stricter: a model learns its teacher's mistakes as faithfully as
    the rest, so a floating part is a reject here.
    """
    res = out.get("result") or {}
    if not out.get("ok"):
        return False, res.get("error") or "it did not run"
    if not res.get("objects_created"):
        return False, "it created no objects"
    floating = (res.get("structure") or {}).get("floating") or []
    if floating:
        return False, "parts still float apart: %s" % ", ".join(floating[:6])
    return True, None


def more_requests(llm, n, seen):
    """More requests from the model. Never fatal: the seeds alone make a set."""
    import random
    from blender_controller import LLMError
    try:
        text = llm([{"role": "user", "content": MORE_REQUESTS_PROMPT.format(
            n=n, seen="\n".join(random.sample(seen, min(30, len(seen)))))}])
    except LLMError as e:
        print("  (could not get more requests: %s)" % e)
        return []
    lowered = {s.lower() for s in seen}
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if 8 <= len(line) <= 80 and line.lower() not in lowered:
            out.append(line)
            lowered.add(line.lower())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=150, help="target verified examples")
    ap.add_argument("--gap", type=float, default=20.0,
                    help="seconds to wait between requests (rate limits)")
    ap.add_argument("--llm", default=None,
                    help="one provider (pip, openrouter, ...); default: Auto")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--no-examples", dest="examples", action="store_false",
                    help="do not show the model the examples gathered so far")
    ap.add_argument("--no-preview", dest="preview", action="store_false")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass
    import blender_controller as bc

    os.makedirs(PREVIEWS, exist_ok=True)
    kept = _rows(PAIRS)
    done = {r["request"].lower() for r in kept} | {r["request"].lower() for r in _rows(REJECTS)}
    queue = [s for s in SEEDS if s.lower() not in done]
    llm = bc.llm_from_env(args.llm)
    print("%d verified so far; target %d; %d seed requests left; model: %s"
          % (len(kept), args.n, len(queue), llm.label))
    if not bc.find_blender():
        print("No Blender found. Set BLENDER_PATH.")
        return 2

    rejected = 0
    numbered = len(kept) + len(_rows(REJECTS))     # previews from earlier runs keep their names
    while len(kept) < args.n:
        if not queue:
            queue = more_requests(llm, 25, sorted(done))
            if not queue:
                print("no requests left to try")
                break
        request = queue.pop(0)
        done.add(request.lower())
        numbered += 1
        n = numbered
        preview = os.path.join(PREVIEWS, _slug(request, n) + ".png") if args.preview else None
        t0 = time.time()
        print("[%d/%d] %s" % (len(kept) + 1, args.n, request), flush=True)
        try:
            out = bc.generate(request, llm, mode="headless", max_attempts=args.attempts,
                              render_path=preview, log=lambda m: print("    " + m, flush=True),
                              use_examples=args.examples)
        except bc.LLMError as e:
            # Every model failed - out of quota for now. Wait it out once
            # rather than burning through the seed list as failures.
            print("    no model answered (%s); waiting 2 minutes" % str(e).splitlines()[0][:120])
            done.discard(request.lower())
            queue.insert(0, request)
            time.sleep(120)
            continue
        res = out.get("result") or {}
        made = res.get("objects_created") or []
        keep, why = verdict(out)
        if keep:
            row = {"request": request, "code": out["code"], "model": out.get("model"),
                   "attempts": len(out["attempts"]), "objects": len(made),
                   "blender": (res.get("blender") or "").split(" ")[0],
                   "example": out.get("example")}
            if preview and res.get("rendered"):
                row["preview"] = os.path.relpath(preview, DATA_DIR).replace(os.sep, "/")
            _append(PAIRS, row)
            kept.append(row)
            print("    kept: %d objects, %d tr%s, %.0fs" % (len(made), row["attempts"],
                  "y" if row["attempts"] == 1 else "ies", time.time() - t0), flush=True)
        else:
            rejected += 1
            _append(REJECTS, {"request": request, "error": why,
                              "line": res.get("source_line"), "model": out.get("model"),
                              "attempts": len(out.get("attempts") or [])})
            print("    rejected: %s" % why[:150], flush=True)
        if args.gap and len(kept) < args.n:
            time.sleep(args.gap)

    print("\n%d verified examples in %s (%d rejected this run)" % (len(kept), PAIRS, rejected))
    print("Look through %s and delete the lines of any that look wrong." % PREVIEWS)
    print("Then: python -m finetune.train_lora --task blender")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
