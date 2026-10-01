"""One entry point for the student distribution.

    python launch.py                          the dashboard: pick a scheduler, watch the kitchen
    python launch.py play my-scheduler --seed 7
    python launch.py play my-scheduler --config rush --replay
    python launch.py check my-scheduler       is your submission complete, and does it run?
    python launch.py validate my-scheduler    the acceptance checks on their own
    python launch.py evaluate my-scheduler --seeds 1000..1020
    python launch.py compare my-scheduler     against every reference scheduler, same seeds
    python launch.py new second-try           another scheduler folder, to try an idea in
    python launch.py baselines                the reference schedulers you are measured against
    python launch.py replay replays/FILE.rep  what is inside a recording
    python launch.py workload --seed 7        the orders a seed produces
    python launch.py doctor                   check this installation

A scheduler is a folder holding `scheduler.py`, and that file is exactly what
you hand in. Name the folder wherever a scheduler is asked for; a reference
scheduler's name (`gordon_ramsay`, `sous_chef`, ...) works there too. `--config` takes a profile from configs/ by name: `rush`, `one-cook`,
`brigade`, `banquet-night`, `bake-off`, `blind`.

Add `--no-open` to any command to keep it from opening a browser.

On Windows, double-click START.cmd instead. On macOS and Linux, run ./start.sh.
Both land here.
"""

from __future__ import annotations

import os
import re
import runpy
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bootstrap  # noqa: E402  - must follow the sys.path line above

#: Handled by the SDK's own command line, so there is one implementation of
#: them and the distribution cannot drift from the platform.
SDK_COMMANDS = ("play", "validate", "evaluate", "compare", "baselines", "replay", "workload")

#: The SDK commands whose first positional argument names a scheduler.
SCHEDULER_COMMANDS = ("play", "validate", "evaluate", "compare")

DEFAULT_PORT = 8790

SUBMISSION_FILE = "scheduler.py"

#: Directories never worth searching for a submission. The same set the
#: dashboard uses, so the two agree about what "your schedulers" are.
SKIP_DIRECTORIES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "target", "node_modules",
    "sim-core", "python-bindings", "student-sdk", "docs", "configs", "replays", "results",
    "engine", "dist", ".github", ".cargo", "tools", "tests",
}

SERVER = ROOT / "tools" / "launcher" / "server.py"
VIEWER = ROOT / "tools" / "launcher" / "viewer.html"
GUIDE = ROOT / "docs" / "student-guide.md"
CONFIGS = ROOT / "configs"
EXAMPLES = ROOT / "examples"
REPLAYS = ROOT / "replays"


def _fail(message: str) -> int:
    sys.stderr.write("\n{}\n\n".format(message))
    return 1


def _relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Schedulers
# ---------------------------------------------------------------------------


def _submission():
    """The submission contract, as the marker applies it.

    Imported from the SDK copy in this download rather than reimplemented, so
    a submission this says is fine is one the marker also says is fine.
    """
    from kitchen import submission

    return submission


def find_submissions() -> list:
    """Every submission folder in this download, nearest first.

    A submission is a directory holding ``scheduler.py``: ``my-scheduler/`` at
    the top, or one level down for anyone who keeps several in a folder.
    """
    found = []
    for directory in sorted(ROOT.iterdir(), key=lambda p: p.name.lower()):
        if not directory.is_dir() or directory.name.lower() in SKIP_DIRECTORIES:
            continue
        if (directory / SUBMISSION_FILE).is_file():
            found.append(directory)
            continue
        for candidate in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if candidate.is_dir() and (candidate / SUBMISSION_FILE).is_file():
                found.append(candidate)
    return found


def profile_names() -> list:
    if not CONFIGS.is_dir():
        return []
    return sorted(p.stem for p in CONFIGS.glob("*.toml"))


def resolve_scheduler(value: str) -> str:
    """Turns a folder name into what the SDK loads, leaving anything else alone.

    Students think in schedulers - the folder they are editing - while the SDK
    loads a file, optionally with a class pinned. Translating here means a
    reference scheduler's name, a folder, an example and a path all work
    wherever a scheduler is asked for.
    """
    if not value or value.startswith("-"):
        return value

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate

    if candidate.is_dir() and (candidate / SUBMISSION_FILE).is_file():
        # A class named in scheduler.toml wins, if the folder happens to have
        # one: a file with two schedulers in it is otherwise ambiguous, and the
        # marker resolves it from this same field. Students never write one.
        try:
            return _submission().inspect_folder(candidate).spec
        except Exception:  # noqa: BLE001 - a broken scheduler.toml is `check`'s to report
            return str(candidate / SUBMISSION_FILE)

    if candidate.is_file():
        return str(candidate)

    example = EXAMPLES / "{}.py".format(value)
    if example.is_file():
        return str(example)

    return value


def resolve_config(value: str) -> str:
    """A profile by name (``rush``), by file name (``rush.toml``) or by path."""
    candidate = Path(value)
    if candidate.is_file():
        return str(candidate)
    name = value[:-5] if value.endswith(".toml") else value
    profile = CONFIGS / "{}.toml".format(name)
    if profile.is_file():
        return str(profile)
    raise ValueError(
        "No profile called {!r}. The profiles in this download are: {}".format(
            value, ", ".join(profile_names()) or "none"
        )
    )


def _replay_name(scheduler: str, config: "str | None", seed: "str | None") -> Path:
    """Where an unnamed recording goes: replays/<who>-<profile>-s<seed>-<when>.rep."""
    # Only a trailing `:ClassName` is a class pin; `C:\...` is a drive letter.
    target, separator, class_name = scheduler.rpartition(":")
    who = Path(target if separator and class_name.isidentifier() else scheduler)
    label = who.parent.name if who.name == SUBMISSION_FILE else who.stem
    if not label or label in (".", ".."):
        label = scheduler
    profile = Path(config).stem if config else "default"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", "{}-{}-s{}".format(label, profile, seed or "default"))
    return REPLAYS / "{}-{}.rep".format(safe, stamp)


def translate(command: str, rest: list) -> "tuple[list, Path | None]":
    """The launcher's spellings, turned into the SDK's.

    Folder names become file paths, profile names become paths under configs/,
    and a bare ``--replay`` gets a file name in replays/. Everything else goes
    through untouched, so the SDK's own help stays accurate here.
    """
    out = []
    replay = None
    scheduler = None
    config = None
    seed = None
    i = 0
    while i < len(rest):
        token = rest[i]
        following = rest[i + 1] if i + 1 < len(rest) else None

        if token.startswith("--config"):
            value = token.partition("=")[2] if "=" in token else following
            if value is None or value.startswith("-"):
                raise ValueError("--config needs a profile name, for example:  --config rush")
            config = resolve_config(value)
            out += ["--config", config]
            i += 1 if "=" in token else 2
            continue

        if token == "--seed" and following is not None:
            seed = following
            out += [token, following]
            i += 2
            continue

        if command == "play" and token.startswith("--replay"):
            value = token.partition("=")[2] if "=" in token else None
            if value:
                replay = Path(value)
                i += 1
            elif following is not None and following.endswith(".rep"):
                replay = Path(following)
                i += 2
            else:
                replay = None  # named once the scheduler and seed are known
                i += 1
            out.append("--replay")
            out.append(None)  # placeholder, filled in below
            continue

        if not token.startswith("-") and scheduler is None and command in SCHEDULER_COMMANDS:
            scheduler = resolve_scheduler(token)
            out.append(scheduler)
            i += 1
            continue

        out.append(token)
        i += 1

    if None in out:
        if replay is None:
            REPLAYS.mkdir(exist_ok=True)
            replay = _replay_name(scheduler or "run", config, seed)
        elif not replay.is_absolute():
            replay = ROOT / replay
        out[out.index(None)] = str(replay)

    return out, replay


# ---------------------------------------------------------------------------
# The dashboard and the viewer
# ---------------------------------------------------------------------------


def _serve(port: int, no_open: bool, open_url: "str | None" = None) -> int:
    """Runs the dashboard server in this process until Ctrl-C.

    ``open_url`` is a page to open instead of the dashboard itself - a replay
    in the viewer, straight after recording it.
    """
    if not SERVER.exists():
        return _fail("The dashboard is missing from this download ({}).".format(_relative(SERVER)))
    if not VIEWER.exists():
        print("  note: viewer.html is missing from this download, so recordings cannot be")
        print("        played back here. Everything else works.")

    url = "http://127.0.0.1:{}".format(port)
    print("\n  Starting the dashboard. Your browser should open by itself;")
    print("  if it does not, go to {}\n".format(open_url or url))

    argv = [str(SERVER), str(port)]
    if no_open or open_url:
        argv.append("--no-browser")
    if open_url and not no_open:
        threading.Timer(0.8, webbrowser.open, [open_url]).start()

    sys.argv = argv
    try:
        runpy.run_path(str(SERVER), run_name="__main__")
    except OSError as exc:
        return _fail(
            "Could not listen on port {}: {}\n"
            "Another dashboard may already be running - look for it at {} -\n"
            "or pick another port:  python launch.py {}".format(port, exc, url, port + 1)
        )
    return 0


def _dashboard(argv: list, no_open: bool) -> int:
    port = DEFAULT_PORT
    if argv:
        try:
            port = int(argv[0])
        except ValueError:
            return _fail("A port has to be a number, not {!r}.".format(argv[0]))
    return _serve(port, no_open)


def _play(rest: list, no_open: bool) -> int:
    from kitchen.cli import main as sdk_main

    try:
        args, replay = translate("play", rest)
    except ValueError as exc:
        return _fail(str(exc))

    status = sdk_main(["play"] + args)
    if status != 0 or replay is None:
        return status

    if not replay.exists():
        return _fail("The run finished but no recording appeared at {}.".format(replay))
    print("  recorded {}".format(_relative(replay)))

    if no_open:
        print("  watch it later from the dashboard (Recent replays), or with:")
        print("      python launch.py replay {}\n".format(_relative(replay)))
        return 0
    if not VIEWER.exists():
        print("  viewer.html is missing from this download, so it cannot be played back here.\n")
        return 0

    url = "http://127.0.0.1:{}/viewer.html?replay={}".format(DEFAULT_PORT, replay.name)
    print("  opening it in the kitchen viewer (Ctrl-C to stop)")
    return _serve(DEFAULT_PORT, no_open=False, open_url=url)


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------


def _print_block(text: str, indent: str = "    ") -> None:
    for line in text.splitlines():
        print(indent + line if line.strip() else "")


def _check(argv: list) -> int:
    """Reports whether a submission would be accepted, and whether it runs.

    The same two checks the marker runs: the folder's structure, then the
    scheduler actually running on the public seeds, in a separate process with
    a deadline. Finding out here is the entire point of having the platform
    locally.
    """
    try:
        submission = _submission()
        from kitchen.validate import ValidationReport
    except ImportError as exc:
        return _fail("The submission checker is missing from this download: {}".format(exc))

    folders = [Path(name) if Path(name).is_absolute() else ROOT / name for name in argv]
    if not folders:
        folders = find_submissions()
    if not folders:
        return _fail(
            "No schedulers found. A scheduler is a folder holding scheduler.py.\n"
            "Make one with:  python launch.py new my-scheduler"
        )

    worst = 0
    for folder in folders:
        print("\n  {}".format(_relative(folder)))
        if not folder.is_dir():
            print("    ERROR       no such folder")
            worst = 1
            continue

        inspection = submission.inspect_folder(folder)

        if inspection.metadata is not None:
            meta = inspection.metadata
            print("    name        {} v{}".format(meta.name, meta.version))
            if meta.members:
                print("    members     {}".format(
                    ", ".join("{} ({})".format(m.name, m.student_number) for m in meta.members)))

        for error in inspection.errors:
            print("    ERROR       {}".format(error.replace("\n", "\n                ")))
        for warning in inspection.warnings:
            print("    warning     {}".format(warning))
        for flag in inspection.flags:
            print("    note        {} - the marker will look at why".format(flag))

        if inspection.errors:
            worst = 1
            continue
        print("    folder      OK: one scheduler.py, nothing that should not be there")

        # Only worth running the scheduler once the folder itself is sound.
        # Overwritten in place on a terminal, and left out entirely when this
        # is piped to a file, where a carriage return is just litter.
        live = sys.stdout.isatty()
        if live:
            print("    running the public seeds...", end="", flush=True)
        report = submission.check_behaviour(
            inspection, sdk_path=str(ROOT / "student-sdk"),
        )
        if live:
            print("\r" + " " * 40 + "\r", end="")

        try:
            rendered = ValidationReport(**report).render()
        except TypeError:
            rendered = "\n".join(str(e) for e in report.get("errors", []))
        _print_block(rendered)

        if report.get("ok"):
            print("    PASSED      it loads, runs legally and keeps inside the deadline")
            print("    hand in     {}{}scheduler.py, on Moodle".format(folder.name, os.sep))
        else:
            worst = 1

    print()
    return worst


def _new(argv: list) -> int:
    """Scaffolds another submission folder, so there is somewhere to try an idea."""
    if not argv:
        return _fail("Name the scheduler:  python launch.py new second-try")

    try:
        submission = _submission()
    except ImportError as exc:
        return _fail("The scaffolder is missing from this download: {}".format(exc))

    name = argv[0]
    if not submission.SLUG_PATTERN.match(name):
        return _fail(
            "{!r} is not a name the marker accepts. Use lowercase letters, digits,\n"
            "'.', '_' or '-', 2 to 64 characters long: second-try, srtf_v2, ...".format(name)
        )

    folder = ROOT / name
    if folder.exists():
        return _fail("{} already exists.".format(folder.name))

    submission.scaffold(folder)
    print("\n  created {}/".format(folder.name))
    print("    {}/{}".format(folder.name, SUBMISSION_FILE))
    print("\n  Edit {}/{}, then run it:".format(folder.name, SUBMISSION_FILE))
    print("    python launch.py play {}".format(folder.name))
    print("  It appears in the dashboard too.\n")
    return 0


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def _cli_banner(cli: Path) -> "tuple[bool, str]":
    """Runs the command-line engine once, to prove it starts on this machine.

    The failures this catches are the platform ones: a Linux binary built
    against a newer glibc than the machine has, a macOS binary Gatekeeper
    killed, a file that lost its executable bit. Each says what to do.
    """
    try:
        completed = subprocess.run(
            [str(cli), "--help"], capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        return False, "could not start {}: {}".format(cli.name, exc)
    except subprocess.TimeoutExpired:
        return False, "{} did not respond within a minute".format(cli.name)

    if completed.returncode == 0:
        first = next((line for line in completed.stdout.splitlines() if line.strip()), "")
        return True, first.strip() or "runs"

    stderr = (completed.stderr or "").strip()
    hint = ""
    if "GLIBC" in stderr:
        hint = ("\n    this Linux is older than the one the engine was built on; "
                "say which distribution you are on when you ask")
    elif completed.returncode < 0 and sys.platform == "darwin":
        hint = ("\n    macOS killed it - clear the download tag with:  "
                "xattr -dr com.apple.quarantine .")
    return False, "{} exited with {}: {}{}".format(
        cli.name, completed.returncode, stderr.splitlines()[-1] if stderr else "no output", hint)


def _doctor(info: dict) -> int:
    print("\n  Short Order student distribution\n")
    print("  python      {}.{}.{}  ({})".format(*sys.version_info[:3], sys.executable))
    print("  platform    {}".format(info["slot"]))
    print("  engine      {}".format(_relative(info["cli"])))
    print("  extension   {}{}".format(
        _relative(info["extension"]), "  (refreshed)" if info.get("refreshed") else ""))

    ok = True
    try:
        import kitchen

        names = list(kitchen.baseline_names())
        print("  kitchen     {}  ({} reference schedulers; gordon_ramsay is the one to beat)".format(
            kitchen.__version__, len(names)))
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        print("\n  PROBLEM: the engine did not load: {}: {}".format(type(exc).__name__, exc))
        ok = False

    started, banner = _cli_banner(Path(info["cli"]))
    if started:
        print("  kitchen-cli {}".format(banner))
    else:
        print("\n  PROBLEM: {}".format(banner))
        ok = False

    import shutil

    if shutil.which("kitchen-cli") is None:
        print("\n  PROBLEM: kitchen-cli is not on PATH.")
        ok = False

    print("  dashboard   {}".format(
        _relative(SERVER) if SERVER.exists() else "MISSING - tools/launcher/server.py"))
    if VIEWER.exists():
        print("  viewer      {}".format(_relative(VIEWER)))
    else:
        print("  viewer      WARNING: viewer.html is missing; runs can be recorded but not played back")
    print("  guide       {}".format(
        _relative(GUIDE) if GUIDE.exists() else "WARNING: docs/student-guide.md is missing"))

    profiles = profile_names()
    print("  profiles    {}".format(", ".join(profiles) if profiles else "WARNING: none in configs/"))

    submissions = [_relative(p) for p in find_submissions()]
    print("  yours       {}".format(", ".join(submissions) if submissions else "none yet"))

    print("\n  {}\n".format("Everything works." if ok else "Something is wrong - see above."))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def main(argv: "list | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # A console that cannot show a character should show a placeholder rather
    # than stop the run. Windows consoles set to a legacy code page are the
    # usual case; the SDK's reports use a middle dot.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    no_open = False
    for flag in ("--no-open", "--no-browser"):
        while flag in argv:
            argv.remove(flag)
            no_open = True

    # Relative paths are how everything is written, and a double-clicked
    # launcher starts wherever the shell happened to be.
    os.chdir(ROOT)

    try:
        info = bootstrap.ensure_engine(ROOT)
    except bootstrap.BootstrapError as exc:
        return _fail(str(exc))

    command = argv[0] if argv else "dashboard"
    rest = argv[1:]

    if command.isdigit():
        return _dashboard([command], no_open)
    if command in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    if command == "doctor":
        return _doctor(info)
    if command == "check":
        return _check(rest)
    if command == "new":
        return _new(rest)
    if command in ("dashboard", "ui", "web"):
        return _dashboard(rest, no_open)
    if command == "play":
        return _play(rest, no_open)
    if command in SDK_COMMANDS:
        from kitchen.cli import main as sdk_main

        try:
            args, _ = translate(command, rest)
        except ValueError as exc:
            return _fail(str(exc))
        return sdk_main([command] + args)

    return _fail(
        "Unknown command {!r}.\n"
        "Try one of: dashboard, {}, check, new, doctor\n"
        "or run:  python launch.py help".format(command, ", ".join(SDK_COMMANDS))
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nstopped\n")
        raise SystemExit(130)
