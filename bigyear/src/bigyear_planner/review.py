"""Single-stop counterfactuals with all other selected dates and stops held fixed."""

from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

from .runner import materialize
from .travel import load_roads, transitions
from .util import dump, read, sha256, write_csv
from .validate import validate


def review_route(run_dir, output=None):
    root = Path(run_dir).resolve()
    report_path = Path(output) if output else root / "detour-review.json"
    roads_file = read(root / 'route.json').get('provenance', {}).get('roads_file', 'roads.sqlite')
    problem_path, roads_path = root / "problem.json", root / roads_file
    baseline = validate(root / "route.json", problem_path, roads_path)
    if not baseline["valid"]:
        raise ValueError("detour review requires an independently validated saved route")
    p, route, engine = read(problem_path), read(root / "route.json"), read(root / "engine.json")
    labels = {s["id"]: s["name"] for s in p["species"]}
    stops = [(d["date"], stop) for d in route["days"] for stop in d["stops"]]
    visits = defaultdict(list)
    for index, (_, stop) in enumerate(stops):
        for species in stop["species"]:
            visits[species].append(index)
    selected = [s["opportunity"] for _, s in stops]
    roads = load_roads(p, roads_path)
    plans = transitions(p, roads)
    rows = []
    with TemporaryDirectory(prefix="bigyear-detour-") as temporary:
        probe_path = Path(temporary) / "probe.json"
        for index, (when, stop) in enumerate(stops):
            lost = sorted(s for s in stop["species"] if visits[s] == [index])
            repeated = sorted(s for s in stop["new_species"] if any(i > index for i in visits[s]))
            row = {
                "date": when,
                "location": stop["name"],
                "opportunity": stop["opportunity"],
                "first_credits_seen_later": [labels[s] for s in repeated],
                "species_lost_if_removed": [labels[s] for s in lost],
                "species_preserving": not lost,
                "removal_feasible": False,
                "minutes_saved": None,
                "road_miles_saved": None,
            }
            try:
                probe = materialize(
                    p,
                    roads,
                    plans,
                    [o for o in selected if o != stop["opportunity"]],
                    {**engine, "optimal": False},
                )
                probe["provenance"] = route["provenance"]
                dump(probe_path, probe)
                audit = validate(probe_path, problem_path, roads_path)
                row["removal_feasible"] = audit["valid"]
                if audit["valid"]:
                    if audit["species_total"] != baseline["species_total"] - len(lost):
                        raise RuntimeError("counterfactual species coverage does not reconcile")
                    row["minutes_saved"] = baseline["travel_minutes"] - audit["travel_minutes"]
                    row["road_miles_saved"] = baseline["road_miles"] - audit["road_miles"]
                else:
                    row["feasibility_reason"] = audit["errors"][:3]
            except ValueError as error:
                row["feasibility_reason"] = [str(error)]
            rows.append(row)
    roads.con.close()
    candidates = [r for r in rows if r["species_preserving"] and r["removal_feasible"]]
    improvements = [
        r
        for r in candidates
        if r["minutes_saved"] > 1e-6
        or (abs(r["minutes_saved"]) <= 1e-6 and r["road_miles_saved"] > 1e-6)
    ]
    report = {
        "schema": 1,
        "route_sha256": sha256(root / "route.json"),
        "species_total": baseline["species_total"],
        "stops_reviewed": len(rows),
        "feasible_species_preserving_removals": len(candidates),
        "travel_improving_removals": len(improvements),
        "method": "Remove one selected stop at a time, retain all other stops/dates, recompute connecting travel, and independently validate the entire experiment calendar.",
        "limitation": "Counterfactuals are independent. Savings cannot be added or all deletions applied together. This does not reoptimize the remaining route or prove a global minimum.",
        "stops": rows,
    }
    dump(report_path, report)
    fields = [
        "date",
        "location",
        "opportunity",
        "first_credits_seen_later",
        "species_lost_if_removed",
        "species_preserving",
        "removal_feasible",
        "minutes_saved",
        "road_miles_saved",
    ]
    csv_rows = [
        {
            **{k: r[k] for k in fields},
            "first_credits_seen_later": " | ".join(r["first_credits_seen_later"]),
            "species_lost_if_removed": " | ".join(r["species_lost_if_removed"]),
        }
        for r in rows
    ]
    write_csv(report_path.with_suffix(".csv"), csv_rows, fields)
    return report
