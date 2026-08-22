"""Runs AI-written Python in a locked-down subprocess and returns what it
printed. This is a real security boundary decision, not a formality -
read the actual guarantees below before treating it as safe to hand to
strangers, because it isn't a full sandbox.

What this actually protects against:
  - Runaway loops/hangs: a hard wall-clock timeout kills the process.
  - Memory exhaustion: on Windows, a Job Object caps the process (and
    anything it spawns) to a fixed memory ceiling and kills the whole
    tree together if that's exceeded. If the Job Object APIs fail for
    any reason, execution still runs with the timeout alone - degraded,
    not broken.
  - Third-party packages: runs with -I -S (isolated mode, site module
    skipped), so nothing pip-installed for this app (flask, torch,
    diffusers, ...) is importable - only the standard library is
    available to the code being run.
  - A throwaway temp directory as its working directory, deleted
    immediately after, so it isn't casually reading/writing this
    project's real files.

What this does NOT protect against, because that genuinely needs a
container or VM, not subprocess flags:
  - Network access. The standard library alone (socket, urllib) is
    enough to make outbound connections. This is the real reason
    app.py gates this route to signed-in accounts only rather than
    opening it to anyone with the link - it's damage control, not a fix.
  - Reading any file this Windows user account can otherwise read.

If exposing this to genuine strangers at scale is ever the actual plan,
the honest upgrade is running this step inside a container with
--network none, not tightening subprocess flags further.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

IS_WINDOWS = sys.platform == "win32"

TIMEOUT_SECONDS = 10
MAX_OUTPUT_CHARS = 8000
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024  # 256MB

# subprocess.CREATE_NO_WINDOW only exists on Windows - referencing it
# unguarded raises AttributeError at import time on Linux, which would
# take the whole app down on a cloud host rather than merely disabling
# the sandbox. Resolved once, here.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _truncate(text):
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n...[output truncated]"
    return text


def _minimal_env(tmp_dir):
    # A near-empty environment - no inherited PATH beyond what's needed
    # to find the interpreter, nothing from this app's own .env, no real
    # HOME pointing at anything the code could read.
    env = {
        "PATH": os.path.dirname(sys.executable),
        "TMPDIR": tmp_dir,
        "HOME": tmp_dir,
    }
    if IS_WINDOWS:
        env["TEMP"] = tmp_dir
        env["TMP"] = tmp_dir
        # Without SystemRoot, Python fails to start at all on Windows.
        env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    return env


def _posix_limits():
    """Resource caps for the child, applied via preexec_fn on POSIX.

    This is the Linux equivalent of the Windows Job Object below: RLIMIT_AS
    caps address space, RLIMIT_CPU caps CPU seconds, and RLIMIT_NPROC stops
    a fork bomb. Without it a cloud deployment would run submitted code
    with no memory or CPU ceiling at all.
    """
    import resource  # POSIX-only; imported lazily so Windows never sees it

    def apply():
        resource.setrlimit(resource.RLIMIT_AS,
                           (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU,
                           (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE,
                           (8 * 1024 * 1024, 8 * 1024 * 1024))
        # New process group, so killing the child takes any grandchildren
        # with it instead of orphaning them.
        os.setsid()

    return apply


def _apply_job_object_limit(pid):
    """Best-effort: caps the process (+ children) to MEMORY_LIMIT_BYTES
    and ensures they die together if the job is torn down. Returns the
    job handle to keep alive, or None if this couldn't be set up - the
    caller still gets the timeout protection either way."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IOCOUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC),
                ("IoInfo", _IOCOUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = MEMORY_LIMIT_BYTES
        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None

        PROCESS_ALL_ACCESS = 0x1F0FFF
        proc_handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not proc_handle:
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, proc_handle):
            kernel32.CloseHandle(proc_handle)
            kernel32.CloseHandle(job)
            return None
        kernel32.CloseHandle(proc_handle)
        return job
    except Exception:
        return None


def run_python(code):
    """-> {stdout, stderr, timed_out, error}. `error` is only set for
    problems on this app's side (e.g. couldn't create the temp dir);
    a script that itself raises still comes back as normal stderr text,
    not `error`."""
    if not code or not code.strip():
        return {"error": "No code to run."}

    tmp_dir = tempfile.mkdtemp(prefix="rgai_run_")
    script_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex[:8]}.py")
    job_handle = None
    proc = None
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)

        # -I: isolated mode (ignores env vars, no user site dir).
        # -S: skips site.py entirely - this is the flag that actually
        # keeps site-packages off sys.path. -I alone does not do this
        # when running outside a venv (verified: without -S, `import
        # requests` succeeds here even with -I set) - only the standard
        # library is importable with both flags together.
        cmd = [sys.executable, "-I", "-S", "-B", script_path]
        popen_kwargs = {
            "cwd": tmp_dir,
            "env": _minimal_env(tmp_dir),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            popen_kwargs["preexec_fn"] = _posix_limits()
        proc = subprocess.Popen(cmd, **popen_kwargs)
        # Job Objects are the Windows-only half; POSIX limits were already
        # applied in the child via preexec_fn above.
        job_handle = _apply_job_object_limit(proc.pid) if IS_WINDOWS else None

        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            timed_out = True

        return {
            "stdout": _truncate(stdout or ""),
            "stderr": _truncate(stderr or ""),
            "timed_out": timed_out,
        }
    except Exception as e:
        return {"error": f"Could not run the sandbox: {e}"}
    finally:
        # Make sure the child is dead and its pipes are closed before
        # anything else. Without this, an exception raised between
        # Popen() succeeding and communicate() returning (a MemoryError,
        # or a failure inside _apply_job_object_limit) leaves an orphaned
        # process holding two open pipe handles - and, because that
        # process still has the temp directory as its cwd, the rmtree
        # below would silently fail too and leak a directory per run.
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        if proc is not None:
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        if job_handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(job_handle)
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
