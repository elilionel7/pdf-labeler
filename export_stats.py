"""
Aggregate ~/.pdf-labeler/metrics.jsonl into a small public stats file for
the website.

Usage:
    python3 export_stats.py                         # writes ./pdf-labeler-stats.json
    python3 export_stats.py --out ~/code/site/public/data/pdf-labeler-stats.json
    python3 export_stats.py --include-incomplete    # also count cleared batches

Benchmark records ("kind": "benchmark") are re-runs of folders that were
already labelled by hand: they count toward AI time and accuracy, but their
review time is an estimate, so it is reported separately and never mixed
into the measured work/review averages.

The output only has totals, per-type / per-model breakdowns and one entry
per batch (date only). It never includes names, paths or times of day.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_METRICS = Path.home() / ".pdf-labeler" / "metrics.jsonl"
SCHEMA_VERSION = 2


def load_sessions(path: Path, include_incomplete: bool) -> List[Dict[str, Any]]:
    sessions = []
    if not path.exists():
        return sessions
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            s = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not s.get("files_renamed"):
            continue
        if not s.get("completed") and not include_incomplete:
            continue
        sessions.append(s)
    return sessions


def _ratio(num: float, den: float, digits: int = 1) -> float | None:
    return round(num / den, digits) if den else None


def _is_measured(s: Dict[str, Any]) -> bool:
    return s.get("kind", "batch") != "benchmark" and s.get("active_seconds") is not None


def build_stats(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    files_renamed = sum(s["files_renamed"] for s in sessions)
    files_edited  = sum(s.get("files_edited", 0) for s in sessions)
    ai     = sum(s.get("ai_seconds", 0) for s in sessions)
    ai_n   = sum(s.get("ai_files", 0) for s in sessions)
    # Human time only from batches where the timer actually ran.
    measured = [s for s in sessions if _is_measured(s)]
    measured_files = sum(s["files_renamed"] for s in measured)
    work   = sum(s.get("active_seconds") or 0 for s in measured)
    review = sum(s.get("review_seconds") or 0 for s in measured)

    by_type: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_model: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in sessions:
        model = s.get("model") or s.get("provider") or "unknown"
        m = by_model[model]
        m["files"] += s["files_renamed"]
        m["edited"] += s.get("files_edited", 0)
        m["ai_seconds"] += s.get("ai_seconds", 0)
        m["ai_files"] += s.get("ai_files", 0)
        for f in s.get("files", []):
            if not f.get("renamed"):
                continue
            t = by_type[f.get("type") or "unknown"]
            t["files"] += 1
            t["edited"] += 1 if f.get("edited") else 0
            t["ai_seconds"] += f.get("ai_seconds", 0)
            if f.get("review_seconds") is not None:
                t["review_seconds"] += f["review_seconds"]
                t["review_files"] += 1

    batches = []
    for s in sorted(sessions, key=lambda s: s.get("started_at", "")):
        n = s["files_renamed"]
        timed = _is_measured(s)
        entry = {
            "date":            (s.get("started_at") or "")[:10],
            "kind":            s.get("kind", "batch"),
            "completed":       bool(s.get("completed")),
            "model":           s.get("model") or s.get("provider") or "unknown",
            "files":           n,
            "edited":          s.get("files_edited", 0),
            "work_seconds":    round(s["active_seconds"]) if timed else None,
            "ai_seconds":      round(s.get("ai_seconds", 0), 1),
            "review_seconds":  round(s["review_seconds"]) if timed else None,
            "seconds_per_file": _ratio(s["active_seconds"], n) if timed else None,
        }
        if s.get("review_seconds_estimate"):
            entry["review_seconds_estimate"] = s["review_seconds_estimate"]
        batches.append(entry)

    benchmarks = [s for s in sessions if s.get("kind") == "benchmark"]
    estimate = None
    if benchmarks:
        est = [s.get("review_seconds_estimate") or {} for s in benchmarks]
        estimate = {
            "files": sum(s["files_renamed"] for s in benchmarks),
            **{k: sum(e.get(k, 0) for e in est) for k in ("low", "mid", "high")},
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "totals": {
            "batches":                 len(sessions),
            "timed_batches":           len(measured),
            "files_renamed":           files_renamed,
            "timed_files":             measured_files,
            "files_edited":            files_edited,
            "ai_accepted_rate":        _ratio(files_renamed - files_edited, files_renamed, 3),
            "work_seconds":            round(work),
            "ai_seconds":              round(ai, 1),
            "review_seconds":          round(review),
            "avg_work_seconds_per_file":   _ratio(work, measured_files),
            "avg_ai_seconds_per_file":     _ratio(ai, ai_n, 2),
            "avg_review_seconds_per_file": _ratio(review, measured_files),
            "files_per_hour":          _ratio(measured_files * 3600, work),
            "estimated_review_seconds": estimate,
        },
        "by_type": {
            k: {
                "files": int(v["files"]),
                "edited": int(v["edited"]),
                "ai_accepted_rate": _ratio(v["files"] - v["edited"], v["files"], 3),
                "avg_ai_seconds": _ratio(v["ai_seconds"], v["files"], 2),
                "avg_review_seconds": _ratio(v["review_seconds"], v["review_files"]),
            } for k, v in sorted(by_type.items())
        },
        "by_model": {
            k: {
                "files": int(v["files"]),
                "edited": int(v["edited"]),
                "ai_accepted_rate": _ratio(v["files"] - v["edited"], v["files"], 3),
                "avg_ai_seconds": _ratio(v["ai_seconds"], v["ai_files"], 2),
            } for k, v in sorted(by_model.items())
        },
        "batches": batches,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    ap.add_argument("--out", type=Path, default=Path("pdf-labeler-stats.json"))
    ap.add_argument("--include-incomplete", action="store_true")
    args = ap.parse_args()

    stats = build_stats(load_sessions(args.metrics.expanduser(), args.include_incomplete))
    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, indent=2) + "\n")
    t = stats["totals"]
    print(f"Wrote {out}: {t['batches']} batch(es), {t['files_renamed']} file(s).")


if __name__ == "__main__":
    main()
