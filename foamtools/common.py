"""Paths and small helpers shared by every module (standard library only)."""
import json
import os
import re
import subprocess
import sys
import threading

# The tools work inside a checkout: cases/, .env and the Docker mount are all under ROOT.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isfile(os.path.join(ROOT, "pyproject.toml")):
    raise ImportError(f"{ROOT} is not a checkout of the repository: install it with `uv pip install -e .`")
CASES = os.path.join(ROOT, "cases")
NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"   # a number as OpenFOAM writes it
SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def load_env(path=os.path.join(ROOT, ".env")):
    """Read KEY=VALUE lines of a .env file into os.environ; variables already set win.
    Values are never printed or logged."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def clean_env(**extra):
    """The environment for a child process, without credentials (names ending in
    _API_KEY, _TOKEN, _SECRET, _PASSWORD), plus `extra`."""
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith(SECRET_SUFFIXES)}
    return dict(env, **extra)


def read_text(path, limit=None):
    """File contents ('' when it cannot be read)."""
    try:
        with open(path, errors="replace") as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return ""


def read_json(path, default=None):
    """Parsed JSON, or `default` when the file is missing or not JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    """Write through a temporary file and a rename, so a reader never sees half a file."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def is_finite(x):
    """A real number that is neither NaN nor infinite (bool is not a number here)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def time_dirs(case_dir):
    """The time directories of a case as (time, name), in time order (0.orig is not one)."""
    try:
        names = os.listdir(case_dir)
    except OSError:
        return []
    return sorted((float(d), d) for d in names
                  if re.fullmatch(NUM, d) and os.path.isdir(os.path.join(case_dir, d)))


def round_sig(x, digits=5):
    """A number rounded to `digits` significant digits for display; NaN and infinity become
    None, and anything that is not a number is returned as it is."""
    if x is None or (isinstance(x, float) and (x != x or abs(x) == float("inf"))):
        return None
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(f"{x:.{digits}g}")
    return x


def run_sim(*args, timeout=None, image=None, tail=1500):
    """Run `sim ARGS` (foamtools.sim) and return its JSON object; never raises.
    `image` overrides OF_IMAGE, the Docker image sim runs OpenFOAM in."""
    try:
        p = subprocess.run([sys.executable, "-m", "foamtools.sim", *[str(a) for a in args]],
                           capture_output=True, text=True, timeout=timeout, cwd=ROOT,
                           env=clean_env(**({"OF_IMAGE": image} if image else {})), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"sim {args[0]} did not return within {timeout}s"}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "sim produced no JSON",
                "stdout": p.stdout[-tail:], "stderr": p.stderr[-tail:]}
