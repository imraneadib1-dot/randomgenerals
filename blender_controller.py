"""Drives Blender for the AI: the open window when one is listening, a
background Blender when not, and a loop that hands tracebacks back to
the model until its script runs.

    python blender_controller.py ping
    python blender_controller.py status
    python blender_controller.py run script.py [--clear] [--render out.png]
    python blender_controller.py generate "a low-poly tree" [--render out.png]

TWO WAYS TO REACH BLENDER
  live      blender-addon/blender_listener.py running inside an open
            Blender (Scripting tab > Run Script, or installed as an
            add-on). The model appears in the viewport the user is
            looking at, and Ctrl+Z removes it in one step.
  headless  No window listening: `blender -b --factory-startup` runs the
            same listener file once per script and exits. The model only
            exists in the --save / --render outputs.
  "auto" pings the bridge first and only falls back when nothing answers.

WHY THIS IS NOT A ROUTE ON THE WEBSITE
A Blender script is unsandboxed Python: os, subprocess, the filesystem.
Running model-written bpy code on the public server would let any
prompt that talks the model round run code there. The bridge listens on
127.0.0.1 of the machine Blender is on, and this module is meant to run
on that machine too - the desktop build, or a CLI next to Blender.

THE MODEL
With no --llm, every provider that is set up is tried in this order,
and one that fails (out of credits, busy, down) hands over to the next.
No new key is needed: the first is the app's own engine, and the last
needs none.
  pip             pip 1.0, from pip_server.py     none (PIP_ENGINE_URL, PIP_ENGINE_KEY)
  randomgenerals  randomgenerals.com/v1           RG_API_KEY (RG_BASE_URL)
  openrouter      qwen/qwen3-coder-next           OPENROUTER_API_KEY
  ollama          the local Qwen Coder            none - Ollama running
  custom          any OpenAI-compatible server    BLENDER_LLM_BASE_URL (+_API_KEY)
pip 1.0 leads because it is ours. Local qwen2.5-coder:7b, on the same
low-poly tree prompt (2026-10-06, Blender 5.1), needed a retry, took
nine minutes on this machine's CPU, and laid the trunk on its side - it
is there for when nothing else answers, not as a choice.
--llm (or BLENDER_LLM) picks one; --model (or BLENDER_LLM_MODEL) then
overrides its model.
"""
import argparse
import functools
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
LISTENER = HERE / "blender-addon" / "blender_listener.py"
# Verified scripts from finetune/make_blender_data.py: shown to the model
# as examples, and the LoRA training set.
DATASET = HERE / "finetune" / "data" / "blender.jsonl"
BRIDGE_FILE = Path(os.environ.get("RG_BLENDER_BRIDGE_FILE")
                   or Path.home() / ".randomgenerals" / "blender-bridge.json")
DEFAULT_PORT = 8128
# Not urllib's default. Cloudflare in front of randomgenerals.com refuses
# "Python-urllib/x" with a 403 "error code: 1010", which is what broke
# the Blender add-on's connection to the site.
USER_AGENT = "pip-BlenderController/1"
EXEC_TIMEOUT = 300

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Bridge calls never go through a proxy. With HTTP_PROXY set and no
# NO_PROXY entry for loopback, urllib would send 127.0.0.1 to the proxy,
# which answers 502 for a server only this machine can see.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BlenderError(Exception):
    """Blender could not be reached at all - not a script failing."""


# ----------------------------------------------------------- the bridge

def bridge_address() -> tuple[str, int, str]:
    """-> (host, port, token) from the file the bridge writes on start.
    RG_BLENDER_PORT / RG_BLENDER_TOKEN win over the file."""
    info: dict[str, Any] = {}
    try:
        info = json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    port = int(os.environ.get("RG_BLENDER_PORT") or info.get("port") or DEFAULT_PORT)
    token = os.environ.get("RG_BLENDER_TOKEN") or info.get("token") or ""
    return info.get("host") or "127.0.0.1", port, token


def _bridge(method: str, path: str, body: dict | None = None,
            timeout: float = 10.0) -> tuple[int, dict]:
    host, port, token = bridge_address()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        "http://%s:%d%s" % (host, port, path), data=data, method=method,
        headers={"Content-Type": "application/json", "X-Bridge-Token": token,
                 "User-Agent": USER_AGENT})
    try:
        with _LOCAL.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {"ok": False, "error": "HTTP %d" % e.code}


def ping(timeout: float = 1.5) -> dict | None:
    """The bridge's /ping, or None when nothing is listening."""
    try:
        status, body = _bridge("GET", "/ping", timeout=timeout)
    except (OSError, ValueError):
        return None
    if status != 200 or body.get("service") != "randomgenerals-blender-bridge":
        return None
    return body


def status(timeout: float = 10.0) -> dict:
    if ping() is None:
        raise BlenderError(_not_listening())
    code, body = _bridge("GET", "/status", timeout=timeout)
    if code == 401:
        raise BlenderError(_bad_token())
    return body


def _not_listening() -> str:
    _, port, _ = bridge_address()
    return ("No Blender is listening on 127.0.0.1:%d. In Blender: Scripting "
            "tab > Open %s > Run Script." % (port, LISTENER))


def _bad_token() -> str:
    return ("The bridge is running but refused the token. Its file %s is "
            "missing or from an earlier run - run blender_listener.py again "
            "to rewrite it." % BRIDGE_FILE)


def run_live(code: str, clear_scene: bool = False, rollback: bool = True,
             save_path: str | None = None, render_path: str | None = None,
             timeout: float = EXEC_TIMEOUT, structure: bool = False) -> dict:
    payload = {"code": code, "clear_scene": clear_scene, "rollback": rollback,
               "structure": structure,
               "save_path": _abs(save_path), "render_path": _abs(render_path),
               "timeout": timeout}
    try:
        # The bridge stops waiting at `timeout` and answers 504; the extra
        # seconds here are so that answer arrives instead of a socket
        # timeout that says less.
        code_, body = _bridge("POST", "/exec", payload, timeout=timeout + 15)
    except (OSError, ValueError) as e:
        raise BlenderError("Lost the connection to Blender: %s" % e) from e
    if code_ == 401:
        raise BlenderError(_bad_token())
    body["mode"] = "live"
    return body


# ------------------------------------------------------------- headless

def _version_key(path: Path) -> tuple:
    m = re.search(r"(\d+)\.(\d+)", str(path))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def find_blender() -> str | None:
    """BLENDER_PATH, then PATH, then the usual install places, newest first."""
    env = os.environ.get("BLENDER_PATH")
    if env and Path(env).is_file():
        return env
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    found: list[Path] = []
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
        for root in filter(None, roots):
            found += Path(root).glob("Blender Foundation/Blender*/blender.exe")
            found += Path(root).glob("Steam/steamapps/common/Blender/blender.exe")
    elif sys.platform == "darwin":
        found += Path("/Applications").glob("Blender*.app/Contents/MacOS/Blender")
    else:
        found += [Path(p) for p in ("/usr/bin/blender", "/usr/local/bin/blender",
                                    "/snap/bin/blender")]
        found += Path("/opt").glob("blender*/blender")
    found = [p for p in found if p.is_file()]
    return str(max(found, key=_version_key)) if found else None


@functools.lru_cache(maxsize=4)
def blender_version(blender: str) -> str:
    try:
        out = subprocess.run([blender, "--version"], capture_output=True, text=True,
                             timeout=60, creationflags=_CREATE_NO_WINDOW).stdout
        m = re.search(r"Blender (\d+\.\d+(?:\.\d+)?)", out)
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    m = re.search(r"(\d+\.\d+)", blender)
    return m.group(1) if m else "4.2+"


def run_headless(code: str, clear_scene: bool = True, rollback: bool = True,
                 save_path: str | None = None, render_path: str | None = None,
                 timeout: float = EXEC_TIMEOUT, blend_file: str | None = None,
                 structure: bool = False) -> dict:
    blender = find_blender()
    if not blender:
        raise BlenderError("No Blender bridge is listening and no blender "
                           "executable was found. Set BLENDER_PATH.")
    work = Path(tempfile.mkdtemp(prefix="rg-blender-"))
    try:
        script, result_file = work / "script.py", work / "result.json"
        script.write_text(code, encoding="utf-8")
        # --factory-startup: the same default scene every time, and none
        # of the user's add-ons - including the bridge add-on, which has
        # no business starting a server in a process about to exit.
        cmd = [blender, "-b"] + ([str(Path(blend_file).resolve())] if blend_file else []) + [
            "--factory-startup", "--python", str(LISTENER), "--",
            "--run", str(script), "--result", str(result_file)]
        if clear_scene:
            cmd.append("--clear")
        if not rollback:
            cmd.append("--keep-failed")
        if structure:
            cmd.append("--structure")
        if save_path:
            cmd += ["--save", _abs(save_path)]
        if render_path:
            cmd += ["--render", _abs(render_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout,
                                  creationflags=_CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"ok": False, "mode": "headless",
                    "error": "Headless Blender did not finish within %ds." % timeout}
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
        else:
            # Blender itself fell over (a crash inside an operator, a bad
            # .blend) before the runner could write anything.
            result = {"ok": False, "error": "Blender exited with code %s and wrote no "
                      "result." % proc.returncode,
                      "stderr": ((proc.stdout or "") + (proc.stderr or ""))[-4000:]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result["mode"] = "headless"
    return result


def _abs(path: str | None) -> str | None:
    # Blender's working directory is not ours; a relative path would land
    # wherever it was started from.
    return str(Path(path).resolve()) if path else None


def pick_mode(mode: str = "auto") -> str:
    """-> "live" or "headless". Decided once per task, so every retry of
    one model lands in the same Blender."""
    if mode not in ("auto", "live", "headless"):
        raise ValueError("mode is auto, live or headless")
    if mode == "headless":
        return mode
    if ping() is not None:
        return "live"
    if mode == "live":
        raise BlenderError(_not_listening())
    return "headless"


def execute(code: str, mode: str = "auto", clear_scene: bool | None = None,
            rollback: bool = True, save_path: str | None = None,
            render_path: str | None = None, timeout: float = EXEC_TIMEOUT,
            blend_file: str | None = None, structure: bool = False) -> dict:
    """Run one script. -> the listener's result dict plus "mode".

    clear_scene defaults to True headless (the factory scene's cube is in
    the way of anything built) and False live (the scene is the user's).
    """
    target = pick_mode(mode)
    if clear_scene is None:
        clear_scene = target == "headless"
    if target == "live":
        return run_live(code, clear_scene, rollback, save_path, render_path, timeout, structure)
    return run_headless(code, clear_scene, rollback, save_path, render_path,
                        timeout, blend_file, structure)


# ------------------------------------------------------------ the model
# The prompt, the retry loop and the model clients live in the listener,
# which carries them anyway for the pop-up inside Blender - one copy, so
# the CLI and the pop-up cannot drift apart. Loaded by path: the listener
# has to stay a single file that installs as an add-on.

def _load_listener():
    spec = importlib.util.spec_from_file_location("blender_listener", LISTENER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


L = _load_listener()
LLMError, RateLimited = L.LLMError, L.RateLimited
SYSTEM_PROMPT, system_prompt = L.SYSTEM_PROMPT, L.system_prompt
extract_code, check_syntax, feedback = L.extract_code, L.check_syntax, L.feedback
openai_llm, ollama_llm, ollama_models = L.openai_llm, L.ollama_llm, L.ollama_models
PROVIDERS, _retry_after, _local_coder = L.PROVIDERS, L._retry_after, L.local_coder

# What --keep-scene leaves in a headless run: --factory-startup's scene.
_FACTORY_SCENE = {"objects": 3, "object_names": ["Cube", "Light", "Camera"],
                  "camera": "Camera", "lights": 1}


def _log(msg: str) -> None:
    print("[blender] " + msg, file=sys.stderr, flush=True)


def _live_summary() -> dict:
    try:
        return status()
    except BlenderError:
        return {}


def generate(prompt: str, llm: Callable[[list], str], mode: str = "auto",
             clear_scene: bool | None = None, max_attempts: int = 3,
             save_path: str | None = None, render_path: str | None = None,
             timeout: float = EXEC_TIMEOUT,
             log: Callable[[str], None] = lambda _m: None,
             use_examples: bool = True) -> dict:
    """Ask for a script, run it, and on failure send the error back.
    -> build_loop's {"ok", "code", "result", "attempts", "model"} + "mode"
    + "example", the request of the verified script the model was shown."""
    target = pick_mode(mode)
    if clear_scene is None:
        clear_scene = target == "headless"
    if target == "live":
        version = (ping() or {}).get("blender", "")
        summary = {} if clear_scene else _live_summary()
    else:
        version = blender_version(find_blender() or "")
        summary = {} if clear_scene else _FACTORY_SCENE
    version = version.split(" ")[0] or "4.2+"
    log("Blender %s, %s mode%s" % (version, target, ", clearing the scene" if clear_scene else ""))

    def run(code: str, structure: bool = False) -> dict:
        return execute(code, mode=target, clear_scene=clear_scene, save_path=save_path,
                       render_path=render_path, timeout=timeout, structure=structure)

    example = L.pick_example(prompt, L.load_examples(DATASET, L.USER_EXAMPLES))         if use_examples else None
    out = L.build_loop(prompt, llm, run, version, L.scene_note(summary, clear_scene),
                       max_attempts, clear_scene or not summary.get("objects"), log,
                       example=example)
    out["mode"] = target
    out["example"] = example["request"] if example else None
    return out


def llm_from_env(choice: str | None = None, model: str | None = None) -> Callable[[list], str]:
    """--llm (or BLENDER_LLM) picks one provider, and --model (or
    BLENDER_LLM_MODEL) its model; neither means Auto, every provider that
    is set up in turn. See THE MODEL in the module docstring."""
    choice = choice or os.environ.get("BLENDER_LLM") or None
    if choice:
        model = model or os.environ.get("BLENDER_LLM_MODEL")
    return L.make_llm(choice, os.environ, model, log=_log)


# ------------------------------------------------------------------ CLI

def _summary(result: dict) -> str:
    lines = ["%s (%s mode, %ss)" % ("OK" if result.get("ok") else "FAILED",
                                    result.get("mode"), result.get("seconds", "?"))]
    if result.get("error"):
        lines.append("error: " + result["error"])
    if result.get("line"):
        lines.append("line %s: %s" % (result["line"], (result.get("source_line") or "").strip()))
    for o in result.get("objects_created") or []:
        lines.append("  + %-28s %-6s %s" % (o["name"], o["type"],
                     "%d verts, %d faces" % (o["vertices"], o["faces"]) if "vertices" in o else ""))
    for key in ("stdout", "stderr"):
        if (result.get(key) or "").strip():
            lines.append("%s: %s" % (key, result[key].strip()[-800:]))
    for key in ("saved", "rendered", "save_error", "render_error"):
        if result.get(key):
            lines.append("%s: %s" % (key, result[key]))
    if result.get("render_notes"):
        lines.append("render: " + "; ".join(result["render_notes"]))
    return "\n".join(lines)


def _mesh(args, log) -> int:
    """The pop-up's Generate, from the command line."""
    folder = L.generation_folder(args.prompt)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        image = args.image or L.reference_image(args.prompt, folder / "reference.jpg")
        log("picture: %s" % image)
        glb = L.trellis(image, folder / "model.glb", token=os.environ.get("HF_TOKEN"), log=log)
    except L.GenerateError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    height = args.height
    if not height:
        try:
            height = L.estimate_height(args.prompt, llm_from_env())
        except LLMError:
            height = 1.0
    log("importing %s at %.2f m" % (glb, height))
    name = args.prompt.strip()[:1].upper() + args.prompt.strip()[1:40]
    result = execute(L.import_script(glb, name, height), args.mode, clear_scene=False,
                     save_path=args.save, render_path=args.render)
    print(_summary(result))
    print("files: %s" % folder)
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env")
    except ImportError:
        pass
    p = argparse.ArgumentParser(description="Run AI-written bpy code in Blender.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping", help="is a Blender window listening?")
    mp = sub.add_parser("mesh", help="a real textured model from TRELLIS, imported into Blender")
    mp.add_argument("prompt", help='what to make, e.g. "a wooden rocking chair"')
    mp.add_argument("--image", help="build from this picture instead of making one")
    mp.add_argument("--height", type=float, help="metres tall (default: the model estimates)")
    mp.add_argument("--mode", default="auto", choices=["auto", "live", "headless"])
    mp.add_argument("--save", help="save the .blend here")
    mp.add_argument("--render", help="render a PNG preview here")
    sub.add_parser("status", help="what the listening Blender has open")
    for name in ("run", "generate"):
        sp = sub.add_parser(name)
        if name == "run":
            sp.add_argument("script", help="a .py file to run")
        else:
            sp.add_argument("prompt", help='what to build, e.g. "a low-poly tree"')
            sp.add_argument("--attempts", type=int, default=3)
            sp.add_argument("--llm", choices=["pip"] + list(PROVIDERS) + ["ours", "ollama", "custom"],
                            help="one provider instead of trying each in turn")
            sp.add_argument("--model", help="with --llm, the model to ask for")
            sp.add_argument("--show-code", action="store_true")
            sp.add_argument("--no-examples", dest="examples", action="store_false",
                            help="do not show the model a verified example")
        sp.add_argument("--mode", default="auto", choices=["auto", "live", "headless"])
        sp.add_argument("--clear", action="store_true", default=None,
                        help="empty the scene first (default: only headless)")
        sp.add_argument("--keep-scene", dest="clear", action="store_false")
        sp.add_argument("--save", help="save the .blend here")
        sp.add_argument("--render", help="render a PNG preview here")
        sp.add_argument("--timeout", type=float, default=EXEC_TIMEOUT)
        sp.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = p.parse_args(argv)

    def log(msg):
        print("[blender] " + msg, file=sys.stderr, flush=True)

    try:
        if args.cmd == "ping":
            info = ping()
            if not info:
                print(_not_listening())
                return 1
            print(json.dumps(info, indent=2))
            return 0
        if args.cmd == "status":
            print(json.dumps(status(), indent=2))
            return 0
        if args.cmd == "mesh":
            return _mesh(args, log)
        if args.cmd == "run":
            code = Path(args.script).read_text(encoding="utf-8")
            result = check_syntax(code) or execute(
                code, args.mode, args.clear, save_path=args.save,
                render_path=args.render, timeout=args.timeout)
            print(json.dumps(result, indent=2) if args.json else _summary(result))
            return 0 if result.get("ok") else 1
        llm = llm_from_env(args.llm, args.model)
        log("model: " + getattr(llm, "label", "?"))
        out = generate(args.prompt, llm, args.mode, args.clear, args.attempts,
                       args.save, args.render, args.timeout, log, args.examples)
        out["model"] = getattr(llm, "label", "?")
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            if args.show_code:
                print(out["code"] + "\n")
            print("%d attempt%s. " % (len(out["attempts"]), "" if len(out["attempts"]) == 1 else "s")
                  + _summary(out["result"]))
        return 0 if out["ok"] else 1
    except (BlenderError, LLMError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
