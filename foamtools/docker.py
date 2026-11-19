"""Run OpenFOAM commands in Docker.

The repository root is mounted at /work and a command runs in a directory relative
to it, so `cases/<name>` works the same on the host and in the container. The
container runs as the host user: files stay owned by you, and OpenFOAM refuses to
compile #codeStream as root. OF_IMAGE picks the image. Docker and its containers
never see credentials from the environment (common.clean_env).

As a command (`of`):

    of                                   # version check
    of blockMesh -case cases/pitzDaily
    of bash -c 'cd cases/pitzDaily && ./Allrun'
"""
import os
import shutil
import subprocess
import sys
import threading
import time

from .common import ROOT, clean_env

DEFAULT_IMAGE = "opencfd/openfoam-default:2512"
_lock = threading.Lock()
_ok_until = 0.0


class DockerError(RuntimeError):
    pass


def image():
    return os.environ.get("OF_IMAGE") or DEFAULT_IMAGE


def cli(*args, timeout=30):
    """Run the docker client; a failure to start it comes back as exit code 1."""
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=clean_env())
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def ensure_running():
    """Make sure the Docker daemon answers, starting Colima if it does not. Runs once at
    a time across threads; a positive answer is trusted for 30 s. Raises DockerError."""
    global _ok_until
    with _lock:
        if time.time() < _ok_until:
            return
        if not shutil.which("docker"):
            raise DockerError("docker is not installed (not on PATH)")
        if cli("info", timeout=20).returncode != 0:
            if not shutil.which("colima"):
                raise DockerError("Docker is not running (start Docker Desktop or Colima)")
            try:
                r = subprocess.run(["colima", "start", "--cpu", "4", "--memory", "4", "--disk", "20"],
                                   capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL,
                                   env=clean_env())
            except subprocess.TimeoutExpired:
                raise DockerError("Docker is not running and did not start within 240 s (colima start)")
            if r.returncode != 0:
                raise DockerError("Docker is not running and could not be started: "
                                  + (r.stderr or r.stdout).strip()[-400:])
        _ok_until = time.time() + 30.0


def command(args, cwd=".", name=None, image_name=None, tty=False):
    """argv that runs `args` in the container, in `cwd` (relative to the repository root)."""
    return ["docker", "run", "--rm", "-i", *(["-t"] if tty else []), *(["--name", name] if name else []),
            "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{ROOT}:/work", "-w", "/work",
            image_name or image(), "bash", "-c", 'cd "/work/$1" && shift && exec "$@"', "of", cwd, *args]


def popen(args, cwd=".", name=None, image_name=None, **kwargs):
    """Start `args` in a container (see command) once Docker answers; raises DockerError."""
    ensure_running()
    return subprocess.Popen(command(args, cwd=cwd, name=name, image_name=image_name),
                            stdin=subprocess.DEVNULL, env=clean_env(), **kwargs)


def kill(name):
    return cli("kill", name, timeout=20).returncode == 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv) or [
        "bash", "-c", 'echo "OpenFOAM $WM_PROJECT_VERSION in Docker, tutorials at $FOAM_TUTORIALS"']
    try:
        ensure_running()
    except DockerError as e:
        sys.exit(f"of: {e}")
    os.execvpe("docker", command(args, tty=sys.stdin.isatty() and sys.stdout.isatty()), clean_env())


if __name__ == "__main__":
    main()
