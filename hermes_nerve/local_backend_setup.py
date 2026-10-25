"""Explicit, local-only one-click setup for supported Nerve decision sidecars.

No install occurs on page load, backend selection or health check. Install is
a separate reviewed, token-authorized action. Workers run unprivileged under
a dedicated per-Hermes-home virtualenv and never modify Hermes credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

RECIPES = {
    "laya": {
        "port": 8765,
        "model": "convaiinnovations/laya-typed-decisions",
        "packages": ("laya==0.3.29",),
        "service": "hermes_nerve.reflex.laya_service",
        "disk_min_gb": 5,
        "approx_download": "Python ML packages and a model checkpoint (size depends on platform).",
        "description": "Local Laya decision model. Installs Python dependencies and downloads its checkpoint on first start.",
        "remote_code": False,
    },
    "d1": {
        "port": 8080,
        "model": "LiquidAI/d1-3B",
        "packages": ("transformers>=5.14,<6", "torch", "torchvision", "pillow"),
        "service": "hermes_nerve.reflex.d1_service",
        "disk_min_gb": 16,
        "approx_download": "At least 6.25 GB of model weights plus PyTorch and Python dependencies.",
        "description": "Local Liquid D1 typed-decision model. Requires substantial RAM/VRAM and executes reviewed upstream model code after consent.",
        "remote_code": True,
    },
}
MANUAL_GUIDES = {
    "openjev": {
        "supported": False,
        "description": "OpenJev's full model server needs substantial GPU memory and a separate vLLM/MLX installation.",
        "guide_url": "https://huggingface.co/openjev/openjev/blob/main/serve/SERVE.md",
        "message": "Follow the hardware-specific OpenJev guide, start its local System One helper on port 3000, then run a real test.",
    },
    "systemone": {
        "supported": False,
        "description": "System One is a protocol, not an installable model.",
        "guide_url": "https://huggingface.co/LiquidAI/d1-3B",
        "message": "Start a compatible local /v1/systemone server, enter its loopback URL, then run a real test.",
    },
}
PHASES = {"queued", "creating_environment", "installing_packages", "loading_model",
          "verifying_inference", "ready", "stopped", "failed"}


class LocalSetupError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root(home: Path, backend: str) -> Path:
    return home / "nerve" / "local-backends" / backend


def _status_path(home: Path, backend: str) -> Path:
    return _root(home, backend) / "status.json"


def _write_status(home: Path, backend: str, phase: str, message: str, *, pid: int | None = None):
    if phase not in PHASES:
        raise LocalSetupError("Unknown installer phase")
    path = _status_path(home, backend)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"backend": backend, "phase": phase, "message": message[:250],
               "updated_at": _now()}
    if pid:
        payload["pid"] = int(pid)
        # Linux process start ticks distinguish a reused PID from the model
        # process originally launched by the installer.
        try:
            stat_data = Path("/proc") / str(pid) / "stat"
            payload["start_ticks"] = stat_data.read_text().rsplit(") ", 1)[1].split()[19]
        except (OSError, ValueError, IndexError):
            pass
    fd, temp = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def status(home: Path, backend: str):
    if backend not in RECIPES:
        return {"backend": backend, "phase": "unsupported", "message": "Manual setup only."}
    path = _status_path(home, backend)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"backend": backend, "phase": "not_installed", "message": "No managed installation yet."}
    if not isinstance(data, dict) or data.get("phase") not in PHASES:
        return {"backend": backend, "phase": "unknown", "message": "Installer status is invalid."}
    if sys.platform == "linux" and data.get("phase") == "ready" and data.get("pid"):
        if not _valid_process(home, backend, data):
            data["phase"] = "stopped"
            data["message"] = "Managed model process is no longer running."
    return {key: data[key] for key in ("backend", "phase", "message", "updated_at")
            if key in data}


def _valid_process(home: Path, backend: str, record: dict) -> bool:
    if sys.platform != "linux":
        # We never send a kill signal to unverified processes.
        return False
    try:
        pid = int(record["pid"])
        proc = Path("/proc") / str(pid)
        stat_info = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
        return (
            pid > 1 and proc.stat().st_uid == os.getuid()
            and str(record.get("start_ticks", "")) == stat_info[19]
            and RECIPES[backend]["service"].encode() in cmdline
            and str((_root(home, backend) / "venv")).encode() in cmdline
        )
    except (ValueError, TypeError, OSError, IndexError, KeyError):
        return False


def stop(home: Path, backend: str):
    if backend not in RECIPES:
        raise LocalSetupError("Only managed local models can be stopped.")
    try:
        data = json.loads(_status_path(home, backend).read_text())
    except (OSError, ValueError):
        raise LocalSetupError("No managed local process was recorded.")
    if not _valid_process(home, backend, data):
        raise LocalSetupError("Process identity could not be verified. Nothing was stopped.")
    os.kill(int(data["pid"]), signal.SIGTERM)
    _write_status(home, backend, "stopped", "Sent a stop signal to the verified managed model.")
    return {"ok": True, "backend": backend, "message": "Stopping the managed local model."}


def _port_busy(port: int):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _python_path():
    """Do not attempt heavyweight ML installs on an unsupported Python version."""
    options = [sys.executable] + [shutil.which(name) for name in
                                  ("python3.12", "python3.11", "python3.13", "python3.10")]
    seen = set()
    for candidate in options:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run([candidate, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                                    capture_output=True, text=True, timeout=4, check=True)
            major, minor = map(int, result.stdout.strip().split("."))
            if major == 3 and 10 <= minor <= 13:
                return candidate
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def preview(home: Path, backend: str):
    if backend in MANUAL_GUIDES:
        return {"backend": backend, **MANUAL_GUIDES[backend]}
    if backend not in RECIPES:
        raise LocalSetupError("Unsupported backend.")
    recipe = RECIPES[backend]
    original_home = Path(home).expanduser()
    if original_home.is_symlink():
        raise LocalSetupError("Refusing to install through a symlinked Hermes home.")
    home = original_home.resolve()
    if not home.is_dir():
        raise LocalSetupError("Hermes home must be an existing ordinary directory.")
    if (_root(home, backend)).is_symlink():
        raise LocalSetupError("Refusing to install through a symlinked runtime directory.")
    python = _python_path()
    free = shutil.disk_usage(home).free
    busy = _port_busy(recipe["port"])
    active = status(home, backend)
    preparing = active["phase"] in ("queued", "creating_environment", "installing_packages",
                                   "loading_model", "verifying_inference")
    possible = bool(python and free >= recipe["disk_min_gb"] * (1024**3)
                    and not busy and not preparing)
    plan = {
        "backend": backend, "model": recipe["model"], "packages": recipe["packages"],
        "port": recipe["port"], "home": str(home), "python": python,
        "runtime_dir": str(_root(home, backend)), "source": "PyPI and Hugging Face",
    }
    return {
        "supported": True, "backend": backend, "model": recipe["model"],
        "description": recipe["description"], "download_note": recipe["approx_download"],
        "requires_remote_code_consent": recipe["remote_code"],
        "packages": list(recipe["packages"]), "port": recipe["port"],
        "url": "http://127.0.0.1:" + str(recipe["port"]),
        "runtime_dir": str(_root(home, backend)),
        "free_gb": round(free / (1024**3), 1),
        "min_free_gb": recipe["disk_min_gb"], "python": python,
        "port_busy": busy, "job": active, "can_install": possible,
        "plan_id": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        "requirements": [
            "Python 3.10-3.13 and a working Python venv/pip installation.",
            "Internet to download dependencies and model weights; no paid API needed.",
            "Localhost port " + str(recipe["port"]) + " must be free.",
        ] + (["D1: substantial RAM/VRAM; executes Hugging Face trust_remote_code=True."] if recipe["remote_code"] else []),
        "message": ("Already listening on the default port; run the live test instead."
                    if busy else "Installer already running." if preparing else
                    "Insufficient disk space." if free < recipe["disk_min_gb"]*(1024**3) else
                    "Supported Python 3.10-3.13 unavailable." if not python else
                    "Ready for explicit Install & start confirmation."),
    }


def launch(home: Path, backend: str, request: dict, package_root: Path):
    plan = preview(home, backend)
    if not plan.get("supported") or not plan.get("can_install"):
        raise LocalSetupError(plan.get("message", "Backend installation is unavailable."))
    if (not isinstance(request, dict) or request.get("plan_id") != plan["plan_id"]
            or request.get("approve_downloads") is not True
            or (plan["requires_remote_code_consent"] and request.get("approve_remote_code") is not True)):
        raise LocalSetupError("Review the install plan and explicitly approve downloads/model code first.")
    home = Path(home).expanduser().resolve()
    package_root = Path(package_root).resolve()
    if not (package_root / "hermes_nerve" / "local_backend_setup.py").is_file():
        raise LocalSetupError("Backend installer assets are missing.")
    _write_status(home, backend, "queued", "Preparing an isolated Python environment.")
    logs = _root(home, backend) / "install.log"
    python = plan["python"]
    args = [python, "-m", "hermes_nerve.local_backend_setup", "--worker",
            "--backend", backend, "--home", str(home)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(package_root)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        with logs.open("ab", buffering=0) as out:
            os.chmod(logs, 0o600)
            child = subprocess.Popen(args, cwd=str(home), env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=subprocess.STDOUT, close_fds=True,
                                     start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        _write_status(home, backend, "failed", "Unable to launch the local installer.")
        raise LocalSetupError("Could not start the installer: " + type(exc).__name__) from exc
    return {"ok": True, "backend": backend, "phase": "queued", "worker_pid": child.pid,
            "message": "Installer launched. Progress remains available if this browser tab closes."}


def _safe_run(args: list[str], cwd: Path, *, timeout: int):
    # Fixed args only; never run a shell or untrusted user-provided command.
    log_path = cwd / "install.log"
    with log_path.open("ab", buffering=0) as log:
        os.chmod(log_path, 0o600)
        result = subprocess.run(args, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("Dependency installation returned status " + str(result.returncode) +
                           ". Inspect the owner-only install.log for details.")


def worker(home: Path, backend: str):
    recipe = RECIPES[backend]
    place = _root(home, backend)
    venv = place / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    server = None
    try:
        _write_status(home, backend, "creating_environment", "Creating isolated Python environment.")
        if not python.exists():
            interpreter = _python_path()
            if not interpreter:
                raise RuntimeError("Compatible Python not found.")
            _safe_run([interpreter, "-m", "venv", str(venv)], place, timeout=240)
        _write_status(home, backend, "installing_packages", "Installing Python packages from PyPI.")
        _safe_run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                   "--prefer-binary", *recipe["packages"]], place, timeout=1400)
        if _port_busy(recipe["port"]):
            raise RuntimeError("Local port is occupied. Existing services will not be replaced.")
        _write_status(home, backend, "loading_model",
                      "Starting model. First launch downloads weights; this may take several minutes.")
        log = place / "server.log"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        env["PYTHONUNBUFFERED"] = "1"
        env["USE_TF"] = "0"
        with log.open("ab", buffering=0) as out:
            os.chmod(log, 0o600)
            server = subprocess.Popen([str(python), "-m", recipe["service"], "--host",
                                       "127.0.0.1", "--port", str(recipe["port"])],
                                      cwd=str(place), env=env, stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=subprocess.STDOUT,
                                      start_new_session=True, close_fds=True)
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + (1500 if backend == "d1" else 900)
        url = "http://127.0.0.1:" + str(recipe["port"])
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError("Model server exited while loading. Inspect the owner-only server.log.")
            try:
                with opener.open(Request(url + "/healthz", method="GET"), timeout=2) as response:
                    body = json.loads(response.read(8192))
                    if (response.status == 200 and isinstance(body, dict) and body.get("ok")
                            and recipe["model"] in str(body.get("model", ""))):
                        break
            except (HTTPError, URLError, OSError, ValueError, TimeoutError):
                pass
            time.sleep(3)
        else:
            raise RuntimeError("Model did not become ready within the setup timeout.")
        _write_status(home, backend, "verifying_inference", "Making a real local System One decision.")
        question = {"setup_probe": {"type": "choice",
                    "instructions": "Route this customer request.",
                    "criteria": {"billing": "Payments and refunds", "technical": "App errors and bugs"}}}
        body = json.dumps({"state": "Please refund the duplicate charge.",
                           "questions": question, "model": recipe["model"]}).encode()
        with opener.open(Request(url + "/v1/systemone", data=body, method="POST",
                                 headers={"Content-Type": "application/json"}), timeout=90) as response:
            answer = json.loads(response.read(262145))
        decision = answer.get("answers", {}).get("setup_probe", {})
        if decision.get("choice") not in {"billing", "technical"}:
            raise RuntimeError("Local model did not return a valid typed choice.")
        _write_status(home, backend, "ready", "Local model is running and real decision inference passed.", pid=server.pid)
        return 0
    except Exception as exc:
        if server is not None and server.poll() is None:
            server.terminate()
        _write_status(home, backend, "failed", str(exc)[:200])
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--backend", choices=RECIPES)
    parser.add_argument("--home", default=os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
    args = parser.parse_args(argv)
    if not args.worker or not args.backend:
        parser.error("Only the managed --worker entrypoint is supported.")
    return worker(Path(args.home).expanduser().resolve(), args.backend)


if __name__ == "__main__":
    raise SystemExit(main())
