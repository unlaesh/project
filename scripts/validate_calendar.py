#!/usr/bin/env python3
"""Check the step 10 planning model, not actual participation or product readiness.

Usage: python3 scripts/validate_calendar.py [docs/calendar-step10.json]
Only the Python standard library is required. All comparisons use integer time
units / Decimal; the displayed hundredth-hour figures are informational.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PEOPLE = ("denis", "alina", "maksim", "artem", "ekaterina")
FIRST = date(2026, 11, 12)
LAST = date(2026, 12, 15)
PARTIAL = {55: Decimal("8"), 56: Decimal("8"), 57: Decimal("4")}


def validate(path: Path) -> dict:
    model = json.loads(path.read_text())
    estimates = json.loads((ROOT / "docs/estimates-step5.json").read_text())
    dependencies = json.loads((ROOT / "docs/dependencies-step9.json").read_text())
    expected = {row["issue"]: Decimal(str(row["estimate_hours"])) for row in estimates["entries"]}
    history = {row["issue"] for row in estimates["entries"] if row["basis"] == "retrospective"}
    full_current = {
        row["issue"]
        for row in estimates["entries"]
        if row["basis"] == "planned" and row["milestone"] == "MVP"
    } | {8, 9, 10}
    required_issues = full_current | set(PARTIAL)
    jobs, segments = model["jobs"], model["segments"]
    errors: list[str] = []

    def check(condition, message):
        if not condition:
            errors.append(message)

    check(model["time_unit_seconds"] == 12, "Expected exact 12-second time units")
    unit_seconds = model["time_unit_seconds"]
    origin = datetime.fromisoformat(model["origin"])
    check(origin.isoformat() == "2026-11-12T09:00:00+05:00", "Unexpected origin/timezone")
    per_hour = Decimal(3600) / unit_seconds
    by_issue, by_phase, by_person = defaultdict(list), defaultdict(list), defaultdict(list)
    daily_units = defaultdict(int)
    org_actual = []
    for index, segment in enumerate(segments):
        a, b = segment["start"], segment["end"]
        person, issue = segment["person"], segment["issue"]
        check(
            isinstance(a, int) and isinstance(b, int) and b > a,
            f"Segment {index}: invalid integer interval",
        )
        start, end = (
            origin + timedelta(seconds=a * unit_seconds),
            origin + timedelta(seconds=b * unit_seconds),
        )
        for key, actual in [("start_iso", start), ("end_iso", end)]:
            stated = datetime.fromisoformat(segment[key])
            # Original candidate ISO strings omit the offset; origin defines it.
            if stated.tzinfo is None:
                stated = stated.replace(tzinfo=origin.tzinfo)
            check(stated == actual, f"Segment {index}: {key} does not match integer time")
        check(person in PEOPLE, f"Segment {index}: unknown person {person}")
        check(
            FIRST <= start.date() <= LAST and FIRST <= end.date() <= LAST,
            f"Segment {index}: outside planning horizon",
        )
        check(
            start.date() == end.date() and start.weekday() < 5,
            f"Segment {index}: crosses dates or falls on weekend",
        )
        check(
            time(9) <= start.time() < end.time() <= time(13),
            f"Segment {index}: outside 09:00–13:00 work window",
        )
        actual_hours = Decimal(b - a) / per_hour
        check(
            abs(Decimal(str(segment["hours"])) - actual_hours) <= Decimal("0.00000001"),
            f"Segment {index}: displayed hours inconsistent",
        )
        by_issue[issue].append(segment)
        by_person[person].append(segment)
        daily_units[(person, start.date().isoformat())] += b - a
        if "phase" in segment:
            by_phase[segment["phase"]].append(segment)
        else:
            org_actual.append((issue, person, a, b))

    check(
        set(by_issue) == required_issues,
        "Current issue scope differs from 28 full issues + 3 partial phases",
    )
    check(set(by_phase) == set(jobs), "Jobs and executed phase segments differ")
    for person, rows in by_person.items():
        ordered = sorted(rows, key=lambda row: row["start"])
        for left, right in zip(ordered, ordered[1:], strict=False):
            check(
                left["end"] <= right["start"],
                f"{person}: overlapping work/meeting at {right['start_iso']}",
            )
    for (person, day), units in daily_units.items():
        check(Decimal(units) / per_hour <= 4, f"{person} {day}: exceeds four hours")
    for issue, rows in by_issue.items():
        total = Decimal(sum(row["end"] - row["start"] for row in rows)) / per_hour
        check(
            total == PARTIAL.get(issue, expected[issue]),
            f"Issue #{issue}: effort mismatch ({total})",
        )
    total_hours = Decimal(sum(row["end"] - row["start"] for row in segments)) / per_hour
    check(total_hours == Decimal("296.82"), f"Current scope effort mismatch: {total_hours}")
    check(Decimal(str(model["sum_hours"])) == total_hours, "Stored total differs from segment sum")
    check(
        sum(expected[n] for n in full_current) + sum(PARTIAL.values()) == total_hours,
        "Current scope does not reconcile to baseline Estimate",
    )
    remaining = (
        sum(expected[row["issue"]] for row in estimates["entries"] if row["basis"] == "planned")
        - total_hours
    )
    check(remaining == Decimal("152"), "Deferred effort does not reconcile to 152 h")

    # Rebuild all fixed organisational events independently of candidate generation.
    planning = {"2026-11-12", "2026-11-19", "2026-11-26", "2026-12-03", "2026-12-10"}
    review = {"2026-11-18", "2026-11-25", "2026-12-02", "2026-12-09", "2026-12-15"}
    control = {"2026-11-25", "2026-12-09", "2026-12-15"}
    org_expected = []
    for offset in range((LAST - FIRST).days + 1):
        day = FIRST + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        base, ds = offset * 24 * 300, day.isoformat()
        for person in PEOPLE:
            blocks = [(8, 0, 25)]  # Daily five minutes.
            if ds in planning or ds in review:
                blocks.append((8, 25, 175))  # Thirty minutes.
            if ds in control and person == "denis":
                blocks.append((9, 225, 375))
            if ds in control and person == "alina":
                blocks.extend([(9, 175, 225), (9, 225, 375), (9, 375, 425)])
            org_expected.extend((issue, person, base + a, base + b) for issue, a, b in blocks)
    check(
        sorted(org_actual) == sorted(org_expected),
        "Fixed #8/#9 events differ from agreed 39-hour model",
    )

    phase_start = {key: min(row["start"] for row in rows) for key, rows in by_phase.items()}
    phase_end = {key: max(row["end"] for row in rows) for key, rows in by_phase.items()}
    visiting, ancestors = set(), {}

    def predecessors(key):
        if key in ancestors:
            return ancestors[key]
        if key in visiting:
            raise ValueError(f"Cycle in phase dependencies at {key}")
        visiting.add(key)
        result = set()
        for parent in jobs[key]["deps"]:
            if parent not in jobs:
                raise ValueError(f"Unknown phase dependency: {parent}")
            result.add(parent)
            result.update(predecessors(parent))
        visiting.remove(key)
        ancestors[key] = result
        return result

    for key, job in jobs.items():
        rows = by_phase.get(key, [])
        if not rows:
            continue
        check(
            sum(row["end"] - row["start"] for row in rows) == job["units"],
            f"Phase {key}: effort mismatch",
        )
        check(
            all(row["person"] == job["person"] and row["issue"] == job["issue"] for row in rows),
            f"Phase {key}: inconsistent assignee/issue",
        )
        check(phase_start[key] >= job["earliest"], f"Phase {key}: starts before earliest date")
        for parent in job["deps"]:
            check(
                phase_start[key] >= phase_end[parent] + job["lag_units"],
                f"Phase {key}: violated dependency/lag from {parent}",
            )
        stated = datetime.fromisoformat(model["completion"][key])
        if stated.tzinfo is None:
            stated = stated.replace(tzinfo=origin.tzinfo)
        check(
            stated == origin + timedelta(seconds=phase_end[key] * unit_seconds),
            f"Phase {key}: completion mismatch",
        )
        predecessors(key)

    for end_phase, parent, hours in [("35b", "35launch", 72), ("53b", "53a", 144)]:
        check(parent in jobs[end_phase]["deps"], f"{end_phase}: missing required wait predecessor")
        check(
            jobs[end_phase]["lag_units"] >= hours * per_hour,
            f"{end_phase}: required calendar wait removed",
        )
    # Explicit gates in step 9 for the paid testing stages (preparation can start earlier).
    for gate, issue_numbers in [
        ("55b", [14, 24, 29, 30, 32]),
        ("56b", [24, 25, 27, 29, 32]),
        ("57b", [24, 25, 27]),
    ]:
        for issue in issue_numbers:
            check(f"{issue}z" in ancestors[gate], f"{gate}: missing readiness gate for #{issue}")
    for gate in ["34z", "19z", "52z", "53z", "54z", "55m", "56m", "57m", "35a"]:
        check(gate in ancestors["35launch"], f"Launch: missing gate {gate}")
    check("33z" in ancestors["34z"], "Acceptance can close before parser regression")
    check("35z" in ancestors["10z"], "Final report can close before feedback analysis")

    issue_start = {n: min(row["start"] for row in rows) for n, rows in by_issue.items()}
    issue_end = {n: max(row["end"] for row in rows) for n, rows in by_issue.items()}
    split_fs = []
    checked_edges = 0
    for edge in dependencies["edges"]:
        before, after = edge["predecessor"], edge["successor"]
        if after not in full_current or before in history:
            continue
        check(
            before in full_current,
            f"Current issue #{after} still requires deferred whole issue #{before}",
        )
        if before not in issue_end:
            continue
        checked_edges += 1
        check(
            issue_end[before] <= issue_end[after],
            f"Native completion edge #{before} → #{after} violated",
        )
        if edge["relation"] == "FS" and issue_end[before] > issue_start[after]:
            split_fs.append([before, after])
    # These are explicit preparation/integration refinements, not deletion of completion gates.
    allowed_splits = {(20, 23), (22, 23), (23, 33), (33, 34), (34, 35)}
    check(
        all(tuple(edge) in allowed_splits for edge in split_fs),
        "Unexplained overlap of a baseline FS dependency",
    )
    reserve_summary = None
    if "reserve" in model:
        reserve = model["reserve"]
        current_budget = Decimal(str(reserve["current_scope_scheduled_hours"]))
        future_budget = Decimal(str(reserve["deferred_scope_unscheduled_hours"]))
        pool = Decimal(str(reserve["pool_hours"]))
        check(
            pool == 90 and current_budget == 60 and future_budget == 30,
            "Reserve budget must reconcile to 60 current + 30 future = 90 h",
        )
        check(current_budget + future_budget == pool, "Reserve partitions do not sum to pool")
        check(
            reserve["risk_allocated_hours"] == 0 and reserve["spent_hours"] == 0,
            "This planning model does not establish allocated or spent contingency",
        )
        reserve_people = defaultdict(int)
        combined_daily = daily_units.copy()
        combined_person = {person: list(rows) for person, rows in by_person.items()}
        for index, interval in enumerate(reserve["intervals"]):
            a, b, person = interval["start"], interval["end"], interval["person"]
            check(
                isinstance(a, int) and isinstance(b, int) and b > a,
                f"Reserve {index}: invalid interval",
            )
            check(person in PEOPLE, f"Reserve {index}: unknown person")
            start = origin + timedelta(seconds=a * unit_seconds)
            end = origin + timedelta(seconds=b * unit_seconds)
            for key, actual in [("start_iso", start), ("end_iso", end)]:
                stated = datetime.fromisoformat(interval[key])
                if stated.tzinfo is None:
                    stated = stated.replace(tzinfo=origin.tzinfo)
                check(stated == actual, f"Reserve {index}: inconsistent {key}")
            check(FIRST <= start.date() <= end.date() <= LAST, f"Reserve {index}: outside horizon")
            check(
                start.date() == end.date() and start.weekday() < 5,
                f"Reserve {index}: crosses dates or weekend",
            )
            check(
                time(9) <= start.time() < end.time() <= time(13),
                f"Reserve {index}: outside daily work window",
            )
            check(a >= phase_end["35launch"], f"Reserve {index}: precedes MVP launch")
            hours = Decimal(b - a) / per_hour
            check(
                abs(Decimal(str(interval["hours"])) - hours) <= Decimal("0.00000001"),
                f"Reserve {index}: inconsistent displayed hours",
            )
            reserve_people[person] += b - a
            combined_daily[(person, start.date().isoformat())] += b - a
            combined_person.setdefault(person, []).append(interval)
        scheduled = Decimal(sum(reserve_people.values())) / per_hour
        check(scheduled == current_budget, "Reserved intervals do not sum to 60 h")
        for person, rows in combined_person.items():
            ordered = sorted(rows, key=lambda row: row["start"])
            for left, right in zip(ordered, ordered[1:], strict=False):
                check(
                    left["end"] <= right["start"],
                    f"{person}: reserve overlaps work, meeting or another reserve",
                )
        for (person, day), units in combined_daily.items():
            check(
                Decimal(units) / per_hour <= 4,
                f"{person} {day}: work + meetings + reserve exceeds four hours",
            )
        reserve_summary = {
            person: float(Decimal(reserve_people[person]) / per_hour) for person in PEOPLE
        }
        check(
            reserve_summary
            == {"denis": 8, "alina": 12, "maksim": 14, "artem": 13, "ekaterina": 13},
            "Reserved capacity does not match the documented allocation by role",
        )
        check(
            total_hours + current_budget + remaining + future_budget == Decimal("538.82"),
            "Combined current/future budget differs from step 6 total",
        )
    if errors:
        raise ValueError("\n".join(errors))

    effort = {
        person: float(
            Decimal(sum(row["end"] - row["start"] for row in by_person[person])) / per_hour
        )
        for person in PEOPLE
    }
    final_days = {
        person: {
            day: round(4 - float(Decimal(daily_units[(person, day)]) / per_hour), 6)
            for day in ["2026-12-14", "2026-12-15"]
        }
        for person in PEOPLE
    }
    return {
        "valid": True,
        "kind": "conditional educational plan; not product or staffing confirmation",
        "current_hours": float(total_hours),
        "deferred_hours": float(remaining),
        "full_current_issues": len(full_current),
        "partial_parent_phases": len(PARTIAL),
        "phase_count": len(jobs),
        "checked_native_completion_edges": checked_edges,
        "actual_overlapped_baseline_fs_edges": split_fs,
        "effort_by_person": effort,
        "free_capacity_dec14_15_by_person_day": final_days,
        "launch_end": model["completion"]["35launch"],
        "feedback_end": model["completion"]["35z"],
        "report_end": model["completion"]["10z"],
        "reserved_capacity_hours_by_person": reserve_summary,
    }


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/calendar-step10.json"
    try:
        print(json.dumps(validate(target), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"Calendar validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
