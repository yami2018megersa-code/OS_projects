"""Command-line front end for students.

    python -m kitchen.cli play my_scheduler.py
    python -m kitchen.cli play my_scheduler.py --replay out.rep --seed 7
    python -m kitchen.cli validate my_scheduler.py
    python -m kitchen.cli evaluate my_scheduler.py --seeds 1000..1020
    python -m kitchen.cli compare my_scheduler.py --seeds 1000..1010
    python -m kitchen.cli baselines
    python -m kitchen.cli replay out.rep --events
    python -m kitchen.cli workload --seed 1234
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import _engine as _native
from .errors import SubmissionError
from .report import render_comparison, render_result, render_summary
from .runner import baseline_names, describe_baseline, evaluate, is_offered, play
from .validate import PUBLIC_SEEDS, validate


def _add_kitchen_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="TOML profile (default: the course profile)")
    parser.add_argument("--cores", type=int, default=None, help="override the number of cooks")
    parser.add_argument("--switch-cost", type=int, default=None, help="override the context-switch cost")
    parser.add_argument("--ticks", type=int, default=None, help="override the length of the run")
    parser.add_argument("--blind", action="store_true", help="hide step durations from the scheduler")


def _kitchen_kwargs(args) -> dict:
    return {
        "config_path": args.config,
        "cores": args.cores,
        "switch_cost": args.switch_cost,
        "max_ticks": args.ticks,
        "known_durations": False if args.blind else None,
    }


def _parse_seeds(text: str) -> List[int]:
    start, _, end = text.partition("..")
    if not end:
        return [int(start)]
    return list(range(int(start), int(end)))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kitchen.cli",
        description="Play, validate and evaluate kitchen schedulers.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("play", help="run one service")
    p.add_argument("scheduler", help="file.py[:Class], module[:Class], or a reference scheduler")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--replay", default=None, help="record a replay to this path")
    p.add_argument("--json", action="store_true", help="print the raw result record")
    _add_kitchen_options(p)

    v = sub.add_parser("validate", help="check a submission before uploading")
    v.add_argument("scheduler")
    v.add_argument("--deadline-ms", type=float, default=None)
    v.add_argument("--config", default=None)
    v.add_argument("--json", action="store_true", help="print the report as JSON")

    e = sub.add_parser("evaluate", help="evaluate over a seed set")
    e.add_argument("scheduler")
    e.add_argument("--seeds", default="1000..1020", help="range START..END")
    e.add_argument("--workers", type=int, default=None)
    e.add_argument("--jsonl", default=None, help="write result rows to this file")
    e.add_argument("--json", action="store_true")
    _add_kitchen_options(e)

    c = sub.add_parser("compare", help="your scheduler against every reference scheduler")
    c.add_argument("scheduler", nargs="?", default=None)
    c.add_argument("--seeds", default="1000..1010")
    c.add_argument("--workers", type=int, default=None)
    c.add_argument("--json", action="store_true")
    _add_kitchen_options(c)

    r = sub.add_parser("replay", help="inspect a recorded replay")
    r.add_argument("file")
    r.add_argument("--events", action="store_true")

    w = sub.add_parser("workload", help="print the orders a seed produces")
    w.add_argument("--seed", type=int, default=1234)
    _add_kitchen_options(w)

    sub.add_parser("baselines", help="list the reference schedulers")

    args = parser.parse_args(argv)

    try:
        spec = getattr(args, "scheduler", None)
        if spec is not None and not is_offered(spec):
            raise SubmissionError(
                f"'{spec}' is not a scheduler. Give a .py file path, or one of the reference "
                f"schedulers: {', '.join(baseline_names())}"
            )
        if args.command == "play":
            return _command_play(args)
        if args.command == "validate":
            return _command_validate(args)
        if args.command == "evaluate":
            return _command_evaluate(args)
        if args.command == "compare":
            return _command_compare(args)
        if args.command == "replay":
            return _command_replay(args)
        if args.command == "workload":
            return _command_workload(args)
        if args.command == "baselines":
            return _command_baselines()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - a CLI should not show a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _command_play(args) -> int:
    outcome = play(args.scheduler, args.seed, record_replay=args.replay, **_kitchen_kwargs(args))
    if args.json:
        print(json.dumps(outcome.result, indent=2))
    else:
        print(render_result(outcome.result), end="")
    return 0


def _command_validate(args) -> int:
    report = validate(args.scheduler, config_path=args.config, deadline_ms=args.deadline_ms)
    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        print(report.render())
    return 0 if report.ok else 1


def _command_evaluate(args) -> int:
    seeds = _parse_seeds(args.seeds)
    report = evaluate(args.scheduler, seeds, workers=args.workers, **_kitchen_kwargs(args))
    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as handle:
            for row in report["results"]:
                handle.write(json.dumps(row) + "\n")
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    s = report["summary"]
    print(
        f"\n  {report['policy']} on \"{report['config_name']}\" over {len(seeds)} seeds "
        f"({report['workers']} workers, {report['wall_time_ms'] / 1000:.1f} s)\n"
    )
    print(render_summary(s), end="")
    failures = [r for r in report["results"] if r.get("platform_error")]
    if failures:
        print(f"\n  warning: {len(failures)} run(s) failed at the platform level")
        for row in failures[:3]:
            print(f"    seed {row['seed']}: {row['platform_error']}")
    print(f"\n  engine {report['engine_version']} · config {report['config_hash']}")
    if args.jsonl:
        print(f"  results written to {args.jsonl}")
    return 0


def _command_compare(args) -> int:
    seeds = _parse_seeds(args.seeds)
    kwargs = _kitchen_kwargs(args)
    reports = []
    if args.scheduler:
        reports.append(evaluate(args.scheduler, seeds, workers=args.workers, **kwargs))
    for name in baseline_names():
        reports.append(evaluate(name, seeds, workers=args.workers, **kwargs))
    if args.json:
        print(json.dumps(reports, indent=2))
        return 0
    first = reports[0]
    print(
        f"\n  \"{first['config_name']}\" · {len(seeds)} seeds · engine {first['engine_version']}"
        f" · config {first['config_hash']}\n"
    )
    print(render_comparison(reports))
    return 0


def _command_replay(args) -> int:
    document = _native.read_replay(args.file, False)
    header = document["header"]
    print(args.file)
    print(f"  format version   {header['replay_format_version']}")
    print(f"  engine           {header['engine_version']}")
    print(f"  config           {header['config_hash']} (\"{header['config']['kitchen']['name']}\")")
    print(f"  seed             {header['seed']}")
    print(f"  scheduler        {header['policy']} v{header['policy_version']}")
    print(f"  cooks            {header['config']['kitchen']['cores']}")
    print(f"  orders           {len(header['orders'])}")
    print(f"  events           {len(document['events'])}")
    result = document.get("result")
    if result:
        m = result["metrics"]
        print(
            f"  result           score {result['score']['total']:.1f}, {m['served']} served, "
            f"{m['abandoned']} abandoned, ended {result['terminal_reason']}"
        )
    if args.events:
        print("\nevents:")
        for event in document["events"]:
            who = ""
            if event.get("order") is not None and event.get("core") is not None:
                who = f"order {event['order']} on core {event['core']}"
            elif event.get("order") is not None:
                who = f"order {event['order']}"
            elif event.get("core") is not None:
                who = f"core {event['core']}"
            print(f"  tick {event['tick']:>6}  {event['kind']:<11} {who} {event.get('message', '')}")
    return 0


def _command_workload(args) -> int:
    kwargs = _kitchen_kwargs(args)
    orders = _native.generate_workload(
        args.seed,
        kwargs["config_path"],
        kwargs["cores"],
        kwargs["switch_cost"],
        kwargs["max_ticks"],
        kwargs["known_durations"],
    )
    print(f"\n  {len(orders)} orders for seed {args.seed}\n")
    print(f"  {'id':>4} {'arrives':>8} {'deadline':>9} {'prio':>5} {'recipe':<13} steps")
    for o in orders:
        steps = " ".join(
            f"{s['label']}:{s['duration']}{'w' if s['kind'] == 'wait' else ''}" for s in o["steps"]
        )
        print(f"  {o['id']:>4} {o['arrival']:>8} {o['deadline']:>9} {o['priority']:>5} {o['recipe']:<13} {steps}")
    print()
    return 0


def _command_baselines() -> int:
    print("reference schedulers:")
    for name in baseline_names():
        print(f"  {name:<14} {describe_baseline(name)}")
    print(f"\npublic practice seeds: {', '.join(str(s) for s in PUBLIC_SEEDS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
