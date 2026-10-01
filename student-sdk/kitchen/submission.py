"""The submission contract: what a student hands in, and what is checked
before anything is run.

A submission is one file::

    submissions/2412345-alice/
      scheduler.py     required   all of the scheduler's code, in one file
      scheduler.toml   written by the intake, never by the student

**One file, deliberately, and nothing else.** ``scheduler.py`` is loaded by
path, not imported as a package, so a second module next to it is not
importable on the marking server however well it imports on a laptop. Anything
else in the folder - a second ``.py``, a README, a data directory - is rejected
here, before a tournament, with a message that says so.

``scheduler.toml`` is the exception, and it is not a student's to supply: the
intake writes it from the identity Moodle already holds, so that a leaderboard
row carries a name. A folder without one is still a valid submission, known by
its slug.

This module lives in the SDK so that the student's ``check`` command and the
marker's pipeline run byte-identical code. Nothing in it executes submission
code: that is :func:`check_behaviour`, which runs the validator in a subprocess
with a timeout, because a submission that hangs on import must not be able to
hang whoever is checking it.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SUBMISSION_FILE = "scheduler.py"
METADATA_FILE = "scheduler.toml"

#: Slugs travel into file names, JSON keys and URLs, so they are kept boring.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

#: A scheduler file is code, not data.
CODE_LIMIT_BYTES = 256 * 1024

#: The only thing that may sit next to ``scheduler.py``, and the intake writes
#: it. Everything else is an error - see :func:`inspect_folder`.
ALLOWED_EXTRAS = {METADATA_FILE}
#: Editor and tooling droppings. Not part of the submission, not worth an error.
IGNORED_EXTRAS = {"__pycache__", ".git", ".ds_store", "desktop.ini", ".ipynb_checkpoints"}

#: Imports a scheduler has no business making. Flagged, not blocked: there is
#: no sandbox, so a human decides, and the flag says where to look.
SUSPICIOUS_IMPORTS = (
    "subprocess", "socket", "ctypes", "multiprocessing", "threading", "shutil",
    "urllib", "requests", "http.client", "ftplib", "pickle", "marshal",
    "importlib", "pty", "signal", "resource", "webbrowser", "kitchen._engine",
)
SUSPICIOUS_CALLS = ("eval(", "exec(", "__import__(", "os.system", "os.popen", "os.remove", "open(")


class MetadataError(Exception):
    """``scheduler.toml`` could not be read or does not describe a scheduler."""


@dataclass
class Member:
    name: str
    student_number: str


@dataclass
class Metadata:
    name: str
    version: str = "1"
    entry: Optional[str] = None
    members: List[Member] = field(default_factory=list)


@dataclass
class Inspection:
    """Everything discovery could establish about one folder without running it."""

    slug: str
    path: Path
    metadata: Optional[Metadata] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def name(self) -> str:
        return self.metadata.name if self.metadata else self.slug

    @property
    def version(self) -> str:
        return self.metadata.version if self.metadata else "0"

    @property
    def scheduler_file(self) -> Path:
        return self.path / SUBMISSION_FILE

    @property
    def spec(self) -> str:
        """What :func:`kitchen.load_scheduler` is given."""
        entry = self.metadata.entry if self.metadata and self.metadata.entry else SUBMISSION_FILE
        file, _, class_name = entry.partition(":")
        spec = str(self.path / file)
        return f"{spec}:{class_name}" if class_name else spec

    @property
    def fingerprint(self) -> str:
        """A digest of the code and metadata that decide what gets loaded."""
        digest = hashlib.sha256()
        for name in (SUBMISSION_FILE, METADATA_FILE):
            candidate = self.path / name
            if candidate.is_file():
                digest.update(name.encode())
                digest.update(candidate.read_bytes())
        return digest.hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "path": str(self.path),
            "name": self.name,
            "version": self.version,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "flags": list(self.flags),
            "members": [
                {"name": m.name, "student_number": m.student_number}
                for m in (self.metadata.members if self.metadata else [])
            ],
            "fingerprint": self.fingerprint if self.scheduler_file.is_file() else None,
        }


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _parse_toml(text: str) -> Dict[str, Any]:
    """``tomllib`` on 3.11+, ``tomli`` if installed, otherwise a small parser
    covering the subset a metadata file needs: tables, arrays of tables, and
    string or integer values."""
    try:
        import tomllib  # type: ignore[import-not-found]

        return tomllib.loads(text)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore[import-not-found]

        return tomli.loads(text)
    except ImportError:
        pass

    result: Dict[str, Any] = {}
    current: Dict[str, Any] = result
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith('"') else raw.strip()
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            key = line[2:-2].strip()
            table: Dict[str, Any] = {}
            result.setdefault(key, []).append(table)
            current = table
            continue
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1].strip()
            current = result.setdefault(key, {})
            continue
        if "=" not in line:
            raise MetadataError(f"cannot parse line: {raw.strip()}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            current[key] = value[1:-1]
        elif re.fullmatch(r"-?\d+", value):
            current[key] = int(value)
        elif value in ("true", "false"):
            current[key] = value == "true"
        else:
            raise MetadataError(f"unsupported value for '{key}': {value}")
    return result


def load_metadata(path: Path) -> Metadata:
    """Reads ``scheduler.toml``. Raises :class:`MetadataError` with the fix."""
    try:
        data = _parse_toml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise MetadataError(f"cannot read {path.name}: {exc}") from exc
    except MetadataError:
        raise
    except Exception as exc:  # noqa: BLE001 - tomllib's own error type varies
        raise MetadataError(f"{path.name} is not valid TOML: {exc}") from exc

    section = data.get("scheduler")
    if not isinstance(section, dict):
        raise MetadataError(f"{path.name} needs a [scheduler] table with a name")
    name = str(section.get("name", "")).strip()
    if not 1 <= len(name) <= 40:
        raise MetadataError(f"{path.name}: [scheduler] name must be 1-40 characters")
    version = str(section.get("version", "1")).strip() or "1"
    entry = section.get("entry")
    if entry is not None:
        entry = str(entry).strip()
        file, _, class_name = entry.partition(":")
        if file != SUBMISSION_FILE or (class_name and not class_name.isidentifier()):
            raise MetadataError(
                f"{path.name}: entry must be '{SUBMISSION_FILE}' or '{SUBMISSION_FILE}:ClassName'"
            )
    members: List[Member] = []
    for raw in data.get("members", []) or []:
        if not isinstance(raw, dict):
            raise MetadataError(f"{path.name}: each [[members]] entry needs a name and student_number")
        member_name = str(raw.get("name", "")).strip()
        number = str(raw.get("student_number", "")).strip()
        if not member_name:
            raise MetadataError(f"{path.name}: a member is missing a name")
        if not re.fullmatch(r"\d{1,20}", number):
            raise MetadataError(
                f"{path.name}: student_number for {member_name!r} must be digits (up to 20), "
                f"quoted as a string"
            )
        members.append(Member(member_name, number))
    return Metadata(name=name, version=version, entry=entry, members=members)


def write_metadata(path: Path, name: str, members: Sequence[Member], version: str = "1") -> None:
    lines = ["[scheduler]", f'name = "{name}"', f'version = "{version}"', ""]
    for member in members:
        lines += ["[[members]]", f'name = "{member.name}"', f'student_number = "{member.student_number}"', ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def slug_for(student_number: str, name: str) -> str:
    """The folder name the intake derives from Moodle's identity data."""
    tail = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "student"
    number = re.sub(r"\D", "", student_number) or "0"
    return f"{number}-{tail}"


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def _static_review(source: str) -> List[str]:
    flags = []
    for module in SUSPICIOUS_IMPORTS:
        if re.search(rf"^\s*(import|from)\s+{re.escape(module)}\b", source, re.MULTILINE):
            flags.append(f"imports {module}")
    for call in SUSPICIOUS_CALLS:
        if call in source:
            flags.append(f"calls {call.rstrip('(')}")
    return flags


def inspect_folder(path: Path, slug: Optional[str] = None) -> Inspection:
    """Checks a submission folder's structure and metadata. Runs no code."""
    path = Path(path)
    slug = slug or path.name
    inspection = Inspection(slug=slug, path=path)
    if not SLUG_PATTERN.match(slug):
        inspection.errors.append(
            f"folder name '{slug}' must be lowercase letters, digits, '.', '_' or '-', 2-64 characters"
        )
    if not path.is_dir():
        inspection.errors.append(f"{path} is not a folder")
        return inspection

    scheduler = path / SUBMISSION_FILE
    if not scheduler.is_file():
        inspection.errors.append(f"{SUBMISSION_FILE} is missing; it is the whole submission")
    else:
        if scheduler.is_symlink():
            inspection.errors.append(f"{SUBMISSION_FILE} must not be a symlink")
        size = scheduler.stat().st_size
        if size > CODE_LIMIT_BYTES:
            inspection.errors.append(
                f"{SUBMISSION_FILE} is {size // 1024} KB; the limit is {CODE_LIMIT_BYTES // 1024} KB"
            )
        if size == 0:
            inspection.errors.append(f"{SUBMISSION_FILE} is empty")
        try:
            source = scheduler.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            inspection.errors.append(f"{SUBMISSION_FILE} is not UTF-8 text")
            source = ""
        inspection.flags.extend(_static_review(source))
        if "Scheduler" not in source:
            inspection.warnings.append(f"{SUBMISSION_FILE} never mentions Scheduler; does it define a subclass?")

    for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
        lower = entry.name.lower()
        if entry.name == SUBMISSION_FILE or lower in IGNORED_EXTRAS:
            continue
        if entry.is_symlink():
            inspection.errors.append(f"{entry.name} is a symlink; symlinks are not allowed")
            continue
        if entry.suffix == ".py":
            inspection.errors.append(
                f"{entry.name} is a second Python file. A submission is one file: {SUBMISSION_FILE} "
                f"is loaded by path, so nothing next to it can be imported on the server. "
                f"Move the code into {SUBMISSION_FILE}."
            )
            continue
        if lower in ALLOWED_EXTRAS:
            continue
        inspection.errors.append(
            f"{entry.name} is not part of a submission. The hand-in is one file, "
            f"{SUBMISSION_FILE}, and nothing else: no data directory, no notes, no "
            f"second file of any kind. Anything {SUBMISSION_FILE} needs must be inside it."
        )

    metadata_path = path / METADATA_FILE
    if metadata_path.is_file():
        try:
            inspection.metadata = load_metadata(metadata_path)
        except MetadataError as exc:
            inspection.errors.append(str(exc))
    return inspection


def discover(root: Path) -> List[Inspection]:
    """Every folder under ``root``, inspected, in name order."""
    root = Path(root)
    if not root.is_dir():
        return []
    return [
        inspect_folder(p)
        for p in sorted(root.iterdir(), key=lambda p: p.name.lower())
        if p.is_dir() and p.name.lower() not in IGNORED_EXTRAS and not p.name.startswith(".")
    ]


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def check_behaviour(
    inspection: Inspection,
    *,
    timeout: float = 600.0,
    config_path: Optional[str] = None,
    python: Optional[str] = None,
    sdk_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Runs the student-facing validator over the submission, in a subprocess
    with a timeout, and returns its JSON report. A submission that hangs on
    import costs one process rather than the caller."""
    executable = python or sys.executable
    sdk = sdk_path or str(Path(__file__).resolve().parent.parent)
    command = [executable, "-c", (
        "import sys\n"
        f"sys.path.insert(0, {sdk!r})\n"
        "from kitchen import cli\n"
        f"argv = ['validate', {inspection.spec!r}, '--json']\n"
        + (f"argv += ['--config', {config_path!r}]\n" if config_path else "")
        + "raise SystemExit(cli.main(argv))\n"
    )]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "scheduler": inspection.spec,
            "errors": [f"validation did not finish within {timeout:.0f} s; does the scheduler hang?"],
            "warnings": [],
            "stats": {},
        }
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {
        "ok": False,
        "scheduler": inspection.spec,
        "errors": ["the validator produced no report: " + (completed.stderr or "").strip()[-1500:]],
        "warnings": [],
        "stats": {},
    }


STARTER = '''"""Your scheduler. Edit this file - it is the whole submission.

An idiot sandwich: whenever a cook is free it hands them the *biggest* job on
the rail, then drags a cook off their dish for anything bigger still. Every
assignment it makes is legal and it still loses most of the room. See the
student guide for everything `obs` and `Decision` offer.
"""

from kitchen import Decision, Scheduler, fill_idle


class IdiotSandwich(Scheduler):
    name = "idiot_sandwich"
    version = "1"

    def schedule(self, obs):
        est = obs.estimate_remaining
        rail = sorted(obs.ready, key=est, reverse=True)

        decision = Decision()
        rest = fill_idle(decision, obs, rail)

        # `fill_idle` already spent station places, and `free_stations()` still
        # reports what was free before it ran, so take those off the count.
        free = obs.free_stations()
        for order_id in decision.assignments.values():
            if order_id is None:
                continue
            assigned = obs.order(order_id)
            if assigned is not None and assigned.station is not None:
                free[assigned.station] = free.get(assigned.station, 0) - 1

        for core in obs.working_cores:
            current = obs.order_on(core)
            if current is None or not rest:
                continue
            candidate = obs.order(rest[0])
            if candidate is None:
                continue
            station = candidate.station
            if (station is None or free.get(station, 0) > 0) and est(candidate) > est(current):
                decision.assign(core, rest.pop(0))
                if station is not None:
                    free[station] = free.get(station, 0) - 1

        return decision
'''


def scaffold(path: Path, name: Optional[str] = None, members: Sequence[Member] = ()) -> Path:
    """Writes a valid, working submission folder."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=False)
    (path / SUBMISSION_FILE).write_text(STARTER, encoding="utf-8")
    if name:
        write_metadata(path / METADATA_FILE, name, members)
    return path
