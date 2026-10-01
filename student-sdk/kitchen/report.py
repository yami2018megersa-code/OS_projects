"""Rendering result records and batch reports as text, for the CLI."""

from __future__ import annotations

from typing import Any, Dict, List


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_result(r: Dict[str, Any]) -> str:
    m = r["metrics"]
    s = r["score"]
    capacity = max(1, r["cores"]) * max(1, r["ticks"])
    lines: List[str] = []
    lines.append("")
    lines.append(
        f"  {r['policy']} on \"{r['config_name']}\"  seed {r['seed']}  cores {r['cores']}"
        f"  switch cost {r.get('switch_cost', '?')}"
    )
    w = s.get("weights") or {"completion": 0.30, "response": 0.125, "turnaround": 0.125,
                             "slowdown": 0.15, "switching": 0.15, "fairness": 0.15}
    scale = 100.0 / max(sum(w.values()), 1e-9)
    parts = " + ".join(
        f"{k} {s.get(k, 0.0) * w.get(k, 0.0) * scale:.1f}"
        for k in ("completion", "response", "turnaround", "slowdown", "switching", "fairness")
        if w.get(k)
    )
    lines.append(f"  score {s['total']:.1f}   = {parts}")
    lines.append("")

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<22}{value}")

    row("orders", f"{m['orders']}    served {m['served']} · abandoned {m['abandoned']} · unfinished {m['unfinished']}")
    ap = m["abandoned_by_priority"]
    row(
        "completion",
        f"{_pct(m['completion'])}   priority-weighted {_pct(m['priority_completion'])}"
        f"   abandoned by priority 1/2/3: {ap[1]}/{ap[2]}/{ap[3]}",
    )
    row(
        "response mean/p95",
        f"{m['response']['mean']:.1f} / {m['response']['p95']:.0f} ticks   ({m.get('never_started', 0)} never got a cook)",
    )
    row("turnaround mean/p95", f"{m['turnaround']['mean']:.1f} / {m['turnaround']['p95']:.0f} ticks")
    row("waiting mean/max", f"{m['waiting']['mean']:.1f} / {m['waiting']['max']:.0f} ticks")
    b = m.get("bounded_slowdown", m["slowdown"])
    row(
        "slowdown mean/max",
        f"{m['slowdown']['mean']:.2f} / {m['slowdown']['max']:.2f}   bounded {b['mean']:.2f} / {b['max']:.2f}",
    )
    row(
        "context switches",
        f"{m['context_switches']}    {m.get('necessary_switches', '?')} necessary + {m.get('extra_switches', '?')} extra"
        f"   ({m['switch_ticks']} ticks, {_pct(m['switch_ticks'] / capacity)} of capacity)   preemptions {m['preemptions']}",
    )
    row(
        "utilisation",
        f"{_pct(m['utilisation'])}   idle {_pct(m['idle_ticks'] / capacity)}   wasted work {m['wasted_work_ticks']} ticks",
    )
    for station in m.get("stations", []):
        places = "place" if station["capacity"] == 1 else "places"
        row(
            f"station {station['name']}",
            f"{station['capacity']} {places}   {_pct(station['utilisation'])} in use   "
            f"full {_pct(station['saturation'])} of the service",
        )
    if m.get("stations"):
        row(
            "station stalls",
            f"{m.get('station_conflicts', 0)} assignments refused (a full station)   "
            f"{m.get('station_bumps', 0)} orders sent back to the rail",
        )
    row("fairness (Jain)", f"{m['fairness']:.3f}")
    row("throughput", f"{m['throughput']:.2f} served per 100 ticks   makespan {m['makespan']}")
    d = r["decision_time_ms"]
    mean_ms = d["total_ms"] / d["count"] if d["count"] else 0.0
    row(
        "decisions",
        f"{r['decisions']}    mean {mean_ms:.3f} ms · slowest {d['max_ms']:.3f} ms"
        f" · errors {r['policy_errors']} · timeouts {r['policy_timeouts']} · rejected {r['invalid_assignments']}",
    )
    if r.get("failures"):
        lines.append("")
        lines.append("  first failures:")
        for f in r["failures"]:
            lines.append(f"    {f}")
    lines.append("")
    lines.append(
        f"  {r['ticks']} ticks in {r['wall_time_ms']:.1f} ms, ended {r['terminal_reason']}"
        f" · engine {r['engine_version']} · config {r['config_hash']} · seed {r['seed']}"
    )
    if r.get("replay_path"):
        lines.append(f"  replay written to {r['replay_path']}")
    return "\n".join(lines) + "\n"


def render_summary(s: Dict[str, Any]) -> str:
    lines: List[str] = []

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<24}{value}")

    if not s:
        return "  (no completed runs)\n"
    row(
        "score",
        f"{s['mean_score']:.1f} ± {s['score_ci95']:.1f} (95% CI)   min {s['score_min']:.1f} · max {s['score_max']:.1f}",
    )
    row(
        "completion",
        f"{_pct(s['completion'])}   ({s['served']} of {s['orders']} served, {s['abandoned']} abandoned)",
    )
    row("response mean/p95", f"{s['mean_response']:.1f} / {s['p95_response']:.1f} ticks")
    row("turnaround mean", f"{s['mean_turnaround']:.1f} ticks")
    row("slowdown mean", f"{s['mean_slowdown']:.2f}   bounded {s.get('mean_bounded_slowdown', 0):.2f}")
    row(
        "switches per run",
        f"{s['context_switches_per_run']:.1f}   {s.get('necessary_switch_share', 1) * 100:.0f}% necessary"
        f"   overhead {_pct(s['overhead'])} of capacity   preemptions {s['preemptions_per_run']:.1f}",
    )
    row("utilisation", _pct(s["utilisation"]))
    row("fairness (Jain)", f"{s['fairness']:.3f}")
    row(
        "reliability",
        f"errors {s['policy_errors']} · timeouts {s['policy_timeouts']}"
        f" · rejected {s['invalid_assignments']} · forfeits {s['forfeits']}",
    )
    row("decision time", f"mean {s['decision_mean_ms']:.4f} ms · slowest {s['decision_max_ms']:.3f} ms")
    return "\n".join(lines) + "\n"


def render_comparison(reports: List[Dict[str, Any]]) -> str:
    header = (
        f"  {'scheduler':<18}{'score':>7}{'±ci':>7}{'served':>9}{'response':>10}"
        f"{'slowdown':>10}{'switches':>10}{'necessary':>10}{'fairness':>10}"
    )
    lines = [header]
    ordered = sorted(reports, key=lambda r: -(r.get("summary") or {}).get("mean_score", -1.0))
    for r in ordered:
        s = r.get("summary") or {}
        if not s:
            lines.append(f"  {r['policy']:<18}{'failed':>7}")
            continue
        lines.append(
            f"  {r['policy']:<18}{s['mean_score']:>7.1f}{s['score_ci95']:>7.1f}{s['completion'] * 100:>8.1f}%"
            f"{s['mean_response']:>10.1f}{s.get('mean_bounded_slowdown', s['mean_slowdown']):>10.2f}"
            f"{s['context_switches_per_run']:>10.1f}{s.get('necessary_switch_share', 1) * 100:>9.0f}%"
            f"{s['fairness']:>10.3f}"
        )
    return "\n".join(lines) + "\n"
