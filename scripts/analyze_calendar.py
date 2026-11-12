"""Analyse the step-10 calendar with its resource order held fixed.

Run from any directory. The default input/output are repository documentation files.
This is a conditional calendar-network analysis, not an optimal scheduling algorithm.
"""

import argparse
import hashlib
import heapq
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def remove_interval(intervals, start, end):
    """Remove one fixed organisational event from available working intervals."""
    result = []
    for left, right in intervals:
        if right <= start or left >= end:
            result.append((left, right))
        else:
            if left < start:
                result.append((left, start))
            if end < right:
                result.append((end, right))
    return result


def first_fit(intervals, after, duration):
    for left, right in intervals:
        start = max(left, after)
        if start + duration <= right:
            return start
    raise ValueError(f"No working interval after {after} fits {duration} units")


def last_fit(intervals, before, duration):
    for left, right in reversed(intervals):
        end = min(right, before)
        if end - duration >= left:
            return end - duration
    raise ValueError(f"No working interval before {before} fits {duration} units")


def working_gap(intervals, start, end):
    return sum(max(0, min(right, end) - max(left, start)) for left, right in intervals)


def available_intervals(source, origin, units_per_hour):
    """Use weekdays 09:00–13:00, excluding only fixed organisational work.

    Contingency intervals are consumable reserves, not fixed unavailability. A late
    plan may use them; reserve consumption must then be checked and recorded.
    """
    people = sorted({segment["person"] for segment in source["segments"]})
    available = {person: [] for person in people}
    last_day = max(segment["end"] for segment in source["segments"]) // (24 * units_per_hour)
    for day in range(last_day + 15):
        if (origin + timedelta(days=day)).weekday() < 5:
            for intervals in available.values():
                intervals.append((day * 24 * units_per_hour, (day * 24 + 4) * units_per_hour))
    for segment in source["segments"]:
        if "phase" not in segment:
            person = segment["person"]
            available[person] = remove_interval(available[person], segment["start"], segment["end"])
    return available


def build_network(source):
    """Create segment nodes, phase dependencies and fixed per-person order edges."""
    nodes = {}
    by_phase = defaultdict(list)
    by_person = defaultdict(list)
    predecessors = defaultdict(dict)
    successors = defaultdict(dict)

    def add_edge(before, after, lag=0, kind="phase"):
        if before == after:
            raise ValueError(f"Self-dependency: {before}")
        old = predecessors[after].get(before)
        if old is None or old["lag"] < lag:
            edge = {"lag": lag, "kind": kind}
            predecessors[after][before] = edge
            successors[before][after] = edge

    for index, segment in enumerate(source["segments"]):
        if "phase" not in segment:
            continue
        key = f"{segment['phase']}@{index}"
        nodes[key] = {
            **segment,
            "duration": segment["end"] - segment["start"],
            "release": 0,
        }
        by_phase[segment["phase"]].append(key)
        by_person[segment["person"]].append(key)
    for grouping in (by_phase, by_person):
        for keys in grouping.values():
            keys.sort(key=lambda key: (nodes[key]["start"], nodes[key]["end"]))
    for keys in by_person.values():
        for before, after in zip(keys, keys[1:], strict=False):
            add_edge(before, after, kind="resource")
    for phase, keys in by_phase.items():
        for before, after in zip(keys, keys[1:], strict=False):
            add_edge(before, after, kind="continuation")
        job = source["jobs"][phase]
        nodes[keys[0]]["release"] = job["earliest"]
        for dependency in job["deps"]:
            add_edge(by_phase[dependency][-1], keys[0], job["lag_units"], "dependency")

    queue = []
    indegree = {key: len(predecessors[key]) for key in nodes}
    for key, count in indegree.items():
        if count == 0:
            heapq.heappush(queue, (nodes[key]["start"], key))
    order = []
    while queue:
        _, key = heapq.heappop(queue)
        order.append(key)
        for successor in successors[key]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(queue, (nodes[successor]["start"], successor))
    if len(order) != len(nodes):
        raise ValueError("Cycle in the resource/phase network")
    return nodes, by_phase, predecessors, successors, order


def analyse(source, deadline):
    seconds_per_unit = source["time_unit_seconds"]
    if 3600 % seconds_per_unit:
        raise ValueError("The accounting unit must divide one hour exactly")
    units_per_hour = 3600 // seconds_per_unit
    origin = datetime.fromisoformat(source["origin"])
    if (origin.hour, origin.minute) != (9, 0):
        raise ValueError("This calendar analysis requires the 09:00 origin")
    available = available_intervals(source, origin, units_per_hour)
    nodes, by_phase, predecessors, successors, order = build_network(source)

    def iso(value):
        return (origin + timedelta(seconds=value * seconds_per_unit)).isoformat(timespec="seconds")

    earliest_start = {}
    earliest_finish = {}
    for key in order:
        node = nodes[key]
        after = max(
            [node["release"]]
            + [earliest_finish[before] + edge["lag"] for before, edge in predecessors[key].items()]
        )
        earliest_start[key] = first_fit(available[node["person"]], after, node["duration"])
        earliest_finish[key] = earliest_start[key] + node["duration"]
        if earliest_start[key] > node["start"]:
            raise ValueError(f"Original schedule violates the model at {key}")

    def target_analysis(terminal, target=None):
        last = by_phase[terminal][-1]
        ancestors = {last}
        pending = [last]
        while pending:
            key = pending.pop()
            for before in predecessors[key]:
                if before not in ancestors:
                    ancestors.add(before)
                    pending.append(before)
        target = earliest_finish[last] if target is None else target
        latest_start = {}
        latest_finish = {}
        free_float = {}
        for key in reversed(order):
            if key not in ancestors:
                continue
            node = nodes[key]
            following = {
                after: edge for after, edge in successors[key].items() if after in ancestors
            }
            bound = min(
                [latest_start[after] - edge["lag"] for after, edge in following.items()] or [target]
            )
            latest_start[key] = last_fit(available[node["person"]], bound, node["duration"])
            latest_finish[key] = latest_start[key] + node["duration"]
            if latest_start[key] < earliest_start[key]:
                raise ValueError(f"Deadline infeasible at {key}")
            early_bound = min(
                [earliest_start[after] - edge["lag"] for after, edge in following.items()]
                or [target]
            )
            free_float[key] = (
                last_fit(available[node["person"]], early_bound, node["duration"])
                - earliest_start[key]
            )
        rows = []
        for key in order:
            if key not in ancestors:
                continue
            node = nodes[key]
            rows.append(
                {
                    "id": key,
                    "phase": node["phase"],
                    "person": node["person"],
                    "duration_hours": node["duration"] / units_per_hour,
                    "ES": iso(earliest_start[key]),
                    "EF": iso(earliest_finish[key]),
                    "LS": iso(latest_start[key]),
                    "LF": iso(latest_finish[key]),
                    "TF_calendar_hours": (latest_start[key] - earliest_start[key]) / units_per_hour,
                    "TF_available_work_hours": working_gap(
                        available[node["person"]], earliest_start[key], latest_start[key]
                    )
                    / units_per_hour,
                    "FF_calendar_hours": free_float[key] / units_per_hour,
                    "critical": latest_start[key] == earliest_start[key],
                }
            )
        chain = []
        key = last
        while True:
            chain.append(key)
            before = [parent for parent in predecessors[key] if parent in ancestors]
            if not before:
                break
            parent = max(
                before,
                key=lambda parent: earliest_finish[parent] + predecessors[key][parent]["lag"],
            )
            bound = earliest_finish[parent] + predecessors[key][parent]["lag"]
            if nodes[key]["release"] > bound:
                break
            key = parent
        chain.reverse()
        compact = []
        for key in chain:
            phase = nodes[key]["phase"]
            if not compact or compact[-1] != phase:
                compact.append(phase)
        return {
            "terminal_phase": terminal,
            "earliest_finish": iso(earliest_finish[last]),
            "target_finish": iso(target),
            "ancestor_segment_count": len(ancestors),
            "critical_segment_count": sum(row["critical"] for row in rows),
            "binding_chain_segments": chain,
            "binding_chain_phases": compact,
            "rows": rows,
        }

    projection = {key: source[key] for key in ("jobs", "segments", "origin", "time_unit_seconds")}
    digest = hashlib.sha256(
        json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    deadline_units = (deadline - origin).total_seconds() / seconds_per_unit
    if not deadline_units.is_integer():
        raise ValueError("Deadline must align with the accounting unit")
    return {
        "schema_version": 1,
        "source_projection_sha256": digest,
        "source_projection_fields": list(projection),
        "candidate_seed": source["seed"],
        "method": (
            "Fixed-order nonpreemptive work segments, phase dependencies and calendar-hour lags. "
            "Weekdays 09:00–13:00 exclude fixed organisational events. Segment order and sizes "
            "are fixed to the candidate: this evaluates one order, not the global optimum. "
            "TF/FF are elapsed calendar hours; available-working-hour TF is also reported. "
            "Launch target equals the earliest finish under these constraints."
        ),
        "reserve_treatment": (
            "Contingency intervals are consumable, not fixed blocks. Late starts may consume "
            "reserve: do not add individual floats or claim all contingency remains available "
            "after shifting work. Reserve allocation and consumption need separate tracking."
        ),
        "original_schedule_earlier_changes": sum(
            earliest_start[key] != node["start"] for key, node in nodes.items()
        ),
        "nodes": len(nodes),
        "edges": sum(map(len, predecessors.values())),
        "launch": target_analysis("35launch"),
        "final_deadline": target_analysis("10z", int(deadline_units)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "docs/calendar-step10.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/calendar-network-step10.json")
    parser.add_argument("--deadline", default="2026-12-15T13:00:00+05:00")
    arguments = parser.parse_args()
    source = json.loads(arguments.input.read_text())
    result = analyse(source, datetime.fromisoformat(arguments.deadline))
    arguments.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        f"PASS: {result['nodes']} segments; "
        f"{result['original_schedule_earlier_changes']} avoidable gaps under fixed order; "
        f"launch {result['launch']['earliest_finish']}; "
        f"final {result['final_deadline']['earliest_finish']}."
    )
    print(f"Wrote {arguments.output}")


if __name__ == "__main__":
    main()
