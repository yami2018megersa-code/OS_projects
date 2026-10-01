"""Local launcher for Short Order.

Serves a dashboard on 127.0.0.1 where you pick a scheduler and a service, and
press a button. The page cannot start processes by itself, so this server does
it: the browser posts a request here, and this process runs the scheduler
through the SDK in a worker process and hands the result back.

Run it:

    python tools/launcher/server.py

then open http://127.0.0.1:8790 .

A run of the kitchen takes milliseconds, so there is no "live" mode: every run
is recorded and the browser viewer plays the recording, which is
indistinguishable from watching it live and works for a Python scheduler too.

Security posture: this binds the loopback interface only and never invokes a
shell. Scheduler names are resolved against the engine's own baseline list or
checked to be a real ``.py`` file inside the project, and configuration names
against the files in ``configs/``, so the page cannot be talked into running an
arbitrary program. It is a developer convenience, not something to expose on a
network.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SDK = ROOT / "student-sdk"
PAGE = HERE / "index.html"
VIEWER = HERE / "viewer.html"
REPLAYS = ROOT / "replays"
CONFIGS = ROOT / "configs"
VISUALS = ROOT / "assets" / "visuals"
SPRITES = ROOT / "assets" / "sprites"

DEFAULT_PORT = 8790
#: A run is milliseconds; a Python scheduler a few hundred milliseconds. Bounded
#: well above that so a wedged process cannot occupy a worker for ever.
RUN_TIMEOUT_SECONDS = 600
#: A seed set of a Python scheduler is the slower case.
BATCH_TIMEOUT_SECONDS = 3600


def _sdk_path() -> str:
    return str(SDK)


def _load_engine_facts() -> Dict[str, Any]:
    """Asked of the SDK rather than hardcoded, so a baseline that is added
    cannot go missing from the dashboard."""
    try:
        if _sdk_path() not in sys.path:
            sys.path.insert(0, _sdk_path())
        import kitchen  # noqa: WPS433 - deliberate late import

        return {
            "engine_version": kitchen.ENGINE_VERSION,
            "baselines": [
                {"name": name, "description": kitchen.describe_baseline(name)}
                for name in kitchen.baseline_names()
            ],
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - the launcher must still start
        return {"engine_version": None, "baselines": [], "error": str(exc)}


FACTS = _load_engine_facts()
BASELINES = [b["name"] for b in FACTS["baselines"]]


class LauncherError(Exception):
    """Something the user can fix, reported to the page as a plain message."""


# ---------------------------------------------------------------------------
# Locating things
# ---------------------------------------------------------------------------

#: Directories never worth searching for a submission.
SKIP_DIRECTORIES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "target", "node_modules",
    "sim-core", "python-bindings", "student-sdk", "docs", "configs", "replays", "results",
    "engine", "dist", ".github", ".cargo", "tools", "tests",
}

SUBMISSION_FILE = "scheduler.py"


def find_submissions() -> List[Dict[str, str]]:
    """Every submission folder that can be run, nearest first.

    A submission is a directory holding ``scheduler.py`` - the shape the marker
    reads. Two levels deep: ``my-scheduler/scheduler.py`` for a student working
    in the distribution, and ``submissions/2412345-alice/scheduler.py`` for a
    marker looking at a class.
    """
    found: List[Dict[str, str]] = []
    for directory in sorted(ROOT.iterdir(), key=lambda p: p.name.lower()):
        if not directory.is_dir() or directory.name.lower() in SKIP_DIRECTORIES:
            continue
        candidates = [directory]
        if not (directory / SUBMISSION_FILE).is_file():
            candidates = sorted(
                (p for p in directory.iterdir() if p.is_dir()), key=lambda p: p.name.lower()
            )
        for candidate in candidates:
            module = candidate / SUBMISSION_FILE
            if module.is_file():
                found.append({
                    "label": candidate.name,
                    "path": str(module.relative_to(ROOT)).replace("\\", "/"),
                })
    # The distribution's starter lives under tools/dist in the platform tree.
    starter = ROOT / "tools" / "dist" / "my-scheduler" / SUBMISSION_FILE
    if starter.is_file() and not any(f["path"].endswith("my-scheduler/scheduler.py") for f in found):
        found.append({
            "label": "my-scheduler (starter)",
            "path": str(starter.relative_to(ROOT)).replace("\\", "/"),
        })
    return found


def find_examples() -> List[Dict[str, str]]:
    directory = SDK / "examples"
    if not directory.is_dir():
        return []
    return [
        {"label": p.stem, "path": str(p.relative_to(ROOT)).replace("\\", "/")}
        for p in sorted(directory.glob("*.py"))
    ]


def _first_comment(path: Path) -> str:
    """The first comment line of a profile: its one-line description."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith("#"):
                return text.lstrip("#").strip()
            break
    except OSError:
        pass
    return ""


def find_configs() -> List[Dict[str, Any]]:
    """The profiles in ``configs/``, with their kitchen name and hash."""
    out: List[Dict[str, Any]] = []
    if not CONFIGS.is_dir():
        return out
    try:
        import kitchen  # noqa: WPS433

        loader = kitchen.load_config
    except Exception:  # noqa: BLE001
        loader = None
    for path in sorted(CONFIGS.glob("*.toml")):
        entry: Dict[str, Any] = {
            "file": path.name,
            "label": path.stem,
            "description": _first_comment(path),
        }
        if loader is not None:
            try:
                config = loader(str(path))
                entry.update({
                    "name": config["kitchen"]["name"],
                    "cores": config["kitchen"]["cores"],
                    "switch_cost": config["kitchen"]["switch_cost"],
                    "max_ticks": config["kitchen"]["max_ticks"],
                    "known_durations": config["kitchen"]["known_durations"],
                    "config_hash": config["config_hash"],
                })
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)
        out.append(entry)
    # default.toml first: it is the course profile.
    out.sort(key=lambda e: (e["file"] != "default.toml", e["file"]))
    return out


def list_replays() -> List[Dict[str, Any]]:
    if not REPLAYS.is_dir():
        return []
    entries = []
    for path in REPLAYS.glob("*.rep"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append({"name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return entries[:200]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def resolve_scheduler(value: str) -> str:
    """A baseline name, or a ``.py`` file (optionally ``:Class``) inside the
    project. Anything else is rejected rather than handed to a subprocess."""
    value = (value or "").strip()
    if not value:
        raise LauncherError("a scheduler must be chosen")
    if value in BASELINES:
        return value
    head, separator, tail = value.rpartition(":")
    path_part = head if separator and tail.isidentifier() else value
    if not path_part.endswith(".py"):
        raise LauncherError(
            f"'{value}' is not a reference scheduler or a .py file. "
            f"Reference schedulers: {', '.join(BASELINES)}"
        )
    candidate = Path(path_part)
    candidate = (ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError:
        raise LauncherError(f"'{path_part}' is outside the project directory; refusing to run it") from None
    if not candidate.is_file():
        raise LauncherError(f"no such file: {path_part}")
    return str(candidate) + (f":{tail}" if separator and tail.isidentifier() else "")


def resolve_config(value: Optional[str]) -> Optional[str]:
    """A file name in ``configs/``, or None for the built-in defaults."""
    value = (value or "").strip()
    if not value or value == "default.toml":
        return None
    if "/" in value or "\\" in value or not value.endswith(".toml"):
        raise LauncherError(f"'{value}' is not a profile name")
    path = (CONFIGS / value).resolve()
    try:
        path.relative_to(CONFIGS.resolve())
    except ValueError:
        raise LauncherError("profile is outside the configs directory") from None
    if not path.is_file():
        raise LauncherError(f"no such profile: {value}")
    return str(path)


def resolve_int(value: Any, name: str, low: int, high: int, default: Optional[int]) -> Optional[int]:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise LauncherError(f"{name} must be a whole number") from None
    if not low <= number <= high:
        raise LauncherError(f"{name} must be between {low} and {high}")
    return number


def short_name(spec: str) -> str:
    """A scheduler's name for a file name: the folder for a submission, the
    stem for an example, the name itself for a baseline."""
    text = str(spec)
    if not re.search(r"[\\/]", text):
        return text
    parts = re.sub(r":[A-Za-z_]\w*$", "", text.replace("\\", "/")).split("/")
    file = parts.pop() or text
    folder = parts.pop() if parts else ""
    return folder if file == SUBMISSION_FILE and folder else file[:-3] if file.endswith(".py") else file


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _kitchen_kwargs(request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "config_path": resolve_config(request.get("config")),
        "cores": resolve_int(request.get("cores"), "cores", 1, 16, None),
        "switch_cost": resolve_int(request.get("switch_cost"), "switch cost", 0, 100, None),
        "max_ticks": resolve_int(request.get("ticks"), "ticks", 10, 1_000_000, None),
        "known_durations": False if request.get("blind") else None,
    }


def _run_python(script: str, timeout: int, marker: str) -> Any:
    """Runs a snippet in a worker process and returns the JSON it prints
    after ``marker``. Out of process deliberately: a student scheduler is
    untrusted enough that it should not be imported into the launcher."""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise LauncherError((completed.stderr or "").strip()[-3000:] or "the run failed")
    for line in completed.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise LauncherError("the run produced no result:\n" + (completed.stderr or "").strip()[-2000:])


def run_play(request: Dict[str, Any]) -> Dict[str, Any]:
    scheduler = resolve_scheduler(request.get("scheduler", ""))
    seed = resolve_int(request.get("seed"), "seed", 0, 2**31, None)
    kwargs = _kitchen_kwargs(request)
    watch = bool(request.get("watch", True))

    replay_path: Optional[str] = None
    if watch:
        REPLAYS.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        profile = Path(kwargs["config_path"]).stem if kwargs["config_path"] else "default"
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{short_name(scheduler)}-{profile}-s{seed if seed is not None else 'default'}")
        replay_path = str(REPLAYS / f"{safe}-{stamp}.rep")

    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {_sdk_path()!r})\n"
        "import kitchen\n"
        f"out = kitchen.play({scheduler!r}, {seed!r}, record_replay={replay_path!r}, **{kwargs!r})\n"
        "print('@@RESULT@@' + json.dumps(out.result))\n"
    )
    result = _run_python(script, RUN_TIMEOUT_SECONDS, "@@RESULT@@")
    response: Dict[str, Any] = {"kind": "result", "result": result}
    if replay_path and result.get("replay_path"):
        name = Path(result["replay_path"]).name
        speed = resolve_int(request.get("speed"), "speed", 1, 64, 4)
        response["open"] = f"/viewer.html?replay={name}&speed={speed}"
        response["replay"] = name
        response["message"] = f"Recorded {name}; opening it in the viewer."
    else:
        response["message"] = f"{result['policy']} scored {result['score']['total']:.1f} on seed {result['seed']}."
    return response


def run_evaluate(request: Dict[str, Any]) -> Dict[str, Any]:
    scheduler = resolve_scheduler(request.get("scheduler", ""))
    start = resolve_int(request.get("seed"), "seed", 0, 2**31, 1000) or 1000
    count = resolve_int(request.get("seeds"), "seeds", 1, 500, 20) or 20
    kwargs = _kitchen_kwargs(request)
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {_sdk_path()!r})\n"
        "import kitchen\n"
        f"report = kitchen.evaluate({scheduler!r}, range({start}, {start + count}), **{kwargs!r})\n"
        # Per-seed scores are small and worth plotting; the rest of each row is not.
        "report['results'] = [{'seed': r['seed'], 'score': r['score'], 'served': r.get('metrics', {}).get('served'),"
        " 'abandoned': r.get('metrics', {}).get('abandoned'), 'platform_error': r.get('platform_error'),"
        " 'failures': r.get('failures', [])[:3]} for r in report['results']]\n"
        "print('@@REPORT@@' + json.dumps(report))\n"
    )
    report = _run_python(script, BATCH_TIMEOUT_SECONDS, "@@REPORT@@")
    return {
        "kind": "batch",
        "report": report,
        "message": f"{report['policy']} over {count} seeds: mean score {report['summary'].get('mean_score', 0):.1f}.",
    }


def run_compare(request: Dict[str, Any]) -> Dict[str, Any]:
    scheduler = request.get("scheduler", "")
    scheduler = resolve_scheduler(scheduler) if scheduler else None
    start = resolve_int(request.get("seed"), "seed", 0, 2**31, 1000) or 1000
    count = resolve_int(request.get("seeds"), "seeds", 1, 200, 10) or 10
    kwargs = _kitchen_kwargs(request)
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {_sdk_path()!r})\n"
        "import kitchen\n"
        "reports = []\n"
        f"names = ([{scheduler!r}] if {scheduler!r} else []) + list(kitchen.baseline_names())\n"
        "for name in names:\n"
        f"    r = kitchen.evaluate(name, range({start}, {start + count}), **{kwargs!r})\n"
        "    reports.append({k: v for k, v in r.items() if k != 'results'} | {'yours': name == names[0] and bool("
        f"{scheduler!r})}})\n"
        "print('@@COMPARE@@' + json.dumps(reports))\n"
    )
    reports = _run_python(script, BATCH_TIMEOUT_SECONDS, "@@COMPARE@@")
    return {"kind": "compare", "reports": reports, "message": f"Compared over {count} seeds."}


def run_validate(request: Dict[str, Any]) -> Dict[str, Any]:
    scheduler = resolve_scheduler(request.get("scheduler", ""))
    if scheduler in BASELINES:
        raise LauncherError("validation is for your own scheduler; a reference scheduler is already valid")
    kwargs = _kitchen_kwargs(request)
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {_sdk_path()!r})\n"
        "import kitchen\n"
        f"report = kitchen.validate({scheduler!r}, config_path={kwargs['config_path']!r})\n"
        "print('@@VALID@@' + json.dumps(report.to_dict()))\n"
    )
    report = _run_python(script, RUN_TIMEOUT_SECONDS, "@@VALID@@")
    return {
        "kind": "validation",
        "report": report,
        "message": "Submission check passed." if report["ok"] else "Submission check FAILED - see below.",
    }


ACTIONS = {
    "play": run_play,
    "evaluate": run_evaluate,
    "compare": run_compare,
    "validate": run_validate,
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ShortOrderLauncher/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _serve_replay(self, query: Dict[str, List[str]]) -> None:
        """Decodes a replay with the engine's own reader and returns JSON.
        Deliberately not re-implemented in JavaScript."""
        name = (query.get("name") or [""])[0]
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self._json(400, {"error": "a replay name is required"})
            return
        path = (REPLAYS / name).resolve()
        try:
            path.relative_to(REPLAYS.resolve())
        except ValueError:
            self._json(400, {"error": "replay is outside the replays directory"})
            return
        if not path.exists():
            self._json(404, {"error": f"no such replay: {name}"})
            return
        try:
            if _sdk_path() not in sys.path:
                sys.path.insert(0, _sdk_path())
            from kitchen import _engine  # noqa: WPS433

            data = _engine.read_replay(str(path), True)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"cannot read replay: {exc}"})
            return
        self._json(200, data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not PAGE.exists():
                self._send(500, b"index.html is missing", "text/plain")
                return
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path.startswith("/assets/visuals/"):
            name = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.png", name):
                self._send(404, b"not found", "text/plain")
                return
            asset = VISUALS / name
            if not asset.is_file():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, asset.read_bytes(), "image/png")
            return
        if path.startswith("/assets/sprites/"):
            name = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.png", name):
                self._send(404, b"not found", "text/plain")
                return
            asset = SPRITES / name
            if not asset.is_file():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, asset.read_bytes(), "image/png")
            return
        if path == "/viewer.html":
            if not VIEWER.exists():
                self._send(500, b"viewer.html is missing", "text/plain")
                return
            self._send(200, VIEWER.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/environment":
            self._json(200, {
                "engine_version": FACTS["engine_version"],
                "engine_error": FACTS["error"],
                "baselines": FACTS["baselines"],
                "examples": find_examples(),
                "submissions": find_submissions(),
                "configs": find_configs(),
                "replays": list_replays()[:12],
                "python": sys.version.split()[0],
            })
            return
        if path == "/api/replays":
            self._json(200, {"replays": [r["name"] for r in list_replays()], "details": list_replays()})
            return
        if path == "/api/replay":
            self._serve_replay(query)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/api/launch":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64_000:
                raise LauncherError("request too large")
            request = json.loads(self.rfile.read(length) or b"{}")
            action = request.get("action", "play")
            handler = ACTIONS.get(action)
            if handler is None:
                raise LauncherError(f"unknown action '{action}'")
            self._json(200, {"ok": True, **handler(request)})
        except LauncherError as exc:
            self._json(200, {"ok": False, "error": str(exc)})
        except subprocess.TimeoutExpired:
            self._json(200, {"ok": False, "error": "the run timed out"})
        except Exception as exc:  # noqa: BLE001 - never take the server down
            self._json(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = DEFAULT_PORT
    open_browser = True
    for arg in argv:
        if arg == "--no-browser":
            open_browser = False
        elif arg.isdigit():
            port = int(arg)
    if FACTS["error"]:
        print(f"  engine   NOT READY - {FACTS['error']}")
        print("           build it with:  maturin develop --release")
    else:
        print(f"  engine   {FACTS['engine_version']} ({len(BASELINES)} reference schedulers)")
    print(f"  viewer   {'ready' if VIEWER.exists() else 'viewer.html is missing'}")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Short Order launcher on {url}\n  Ctrl-C to stop.\n")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
