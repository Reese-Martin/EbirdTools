from collections import Counter, defaultdict
from pathlib import Path
import sqlite3

from .util import dump, read


def compare(run_dirs, output):
    settings = []
    manifests = []
    years = []
    for directory in map(Path, run_dirs):
        problem = read(directory / "problem.json")
        best = read(directory / "route.json")
        if not read(directory / "validation.json")["valid"]:
            raise ValueError("all compared runs must pass validation")
        cfg = problem["config"]
        # Search effort is reported; geography/start/travel/availability rules must match.
        rules = {
            k: v
            for k, v in cfg.items()
            if k not in {"year", "seed", "seconds", "threads", "start_date", "end_date"}
        }
        rules["experiment_window"] = {
            k: cfg[k][-5:] if cfg.get(k) else None for k in ("start_date", "end_date")
        }
        roads_file = best.get('provenance', {}).get('roads_file', 'roads.sqlite')
        if roads_file.endswith('.matrix.json'):
            rules['road_network_releases'] = [read(directory / roads_file)['network_release']]
        else:
            network = sqlite3.connect(
                f"file:{(directory / roads_file).resolve()}?mode=ro", uri=True
            )
            rules["road_network_releases"] = sorted(
                r[0] for r in network.execute("SELECT DISTINCT network_release FROM roads")
            )
            network.close()
        rules["flight_network"] = sorted(
            (
                problem["locations"][f["from"]]["id"],
                problem["locations"][f["to"]]["id"],
                f["minutes"],
            )
            for f in problem["flight_links"]
        )
        rules["airports"] = sorted(
            (l["id"], l["lat"], l["lon"]) for l in problem["locations"] if l["kind"] == "airport"
        )
        scope = problem.get("candidate_scope", {})
        rules["candidate_scope"] = {
            k: (scope.get(k)[-5:] if scope.get(k) else None) for k in ("from_date", "to_date")
        }
        rules["candidate_scope"]["states"] = scope.get("states", [])
        polygon = problem["manifest"].get("geographic_scope")
        rules["observation_polygon"] = (
            {k: polygon[k] for k in ("boundary_sha256", "boundary_rule", "travel_rule")}
            if polygon else None
        )
        settings.append(rules)
        m = problem["manifest"]
        manifests.append(
            (m["release"], m["taxonomy_policy"], m["inputs"]["taxonomy"]["sha256"],
             m["synthetic"], m.get("source_format", "EBD"))
        )
        routes = [best] + [read(p) for p in sorted(directory.glob("alternative-*.json"))
                           if p.stem.removeprefix('alternative-').isdigit()]
        unique = {
            tuple((d["date"], tuple(s["location"] for s in d["stops"])) for d in r["days"]): r
            for r in routes
        }
        regions = Counter()
        destinations = Counter()
        corridors = Counter()
        arrivals = defaultdict(list)
        detours = defaultdict(list)
        destination_dates = defaultdict(list)
        region_days = defaultdict(list)
        destination_metadata = {}
        for route in unique.values():
            visited = set()
            visited_destinations = set()
            edges = set()
            previous = None
            daily_counts = Counter()
            current = route["initial_location"]
            for d in route["days"]:
                if d["legs"]:
                    final = d["legs"][-1]
                    current = final["to"] if final["end_fraction"] == 1 else None
                if d["stops"]:
                    current = d["stops"][-1]["location"]
                if current is not None and d["kind"] not in {"road_transit", "flight"}:
                    daily_counts[route["locations"][current]["state"]] += 1
                for stop in d["stops"]:
                    loc = route["locations"][stop["location"]]
                    state = loc["state"]
                    visited.add(state)
                    visited_destinations.add(loc["id"])
                    destination_dates[loc["id"]].append(d["date"][5:])
                    destination_metadata[loc["id"]] = {
                        k: loc[k] for k in ("name", "lat", "lon", "state")
                    }
                    arrivals[state].append(d["date"][5:])
                    if previous and previous != state:
                        edges.add(previous + " → " + state)
                    previous = state
                    for sp in stop["new_species"]:
                        detours[sp].append(
                            {
                                "date": d["date"],
                                "region": state,
                                "daily_road_miles": d["road_miles"],
                                "interpretation": "associated stop travel, not a causal marginal detour estimate",
                            }
                        )
            regions.update(visited)
            corridors.update(edges)
            destinations.update(visited_destinations)
            for state, count in daily_counts.items():
                region_days[state].append(count)
        years.append(
            {
                "year": cfg["year"],
                "synthetic": m["synthetic"],
                "alternative_routes": len(unique),
                "best_species": best["species_total"],
                "search": read(directory / "benchmark.json"),
                "region_prevalence": {k: v / len(unique) for k, v in sorted(regions.items())},
                "destination_prevalence": {
                    k: {
                        **destination_metadata[k],
                        "fraction": v / len(unique),
                        "earliest_visit": min(destination_dates[k]),
                        "latest_visit": max(destination_dates[k]),
                    }
                    for k, v in sorted(destinations.items())
                },
                "destinations_in_every_saved_route": [
                    k for k, v in destinations.items() if v == len(unique)
                ],
                "region_nontransit_days": {
                    k: {
                        "minimum": min(v) if len(v) == len(unique) else 0,
                        "maximum": max(v),
                        "mean": sum(v) / len(unique),
                    }
                    for k, v in region_days.items()
                },
                "arrival_date_ranges": {
                    k: {"earliest": min(v), "latest": max(v), "stop_visits": len(v)}
                    for k, v in arrivals.items()
                },
                "corridor_prevalence": {k: v / len(unique) for k, v in corridors.items()},
                "species_travel_context": detours,
                "observation_coverage": m["statistics"],
            }
        )
    if any(s != settings[0] for s in settings) or any(m != manifests[0] for m in manifests):
        raise ValueError(
            "comparison requires matching rules, starting location, EBD release, taxonomy file/policy and synthetic status"
        )
    result = {
        "schema": 1,
        "years": years,
        "settings": settings[0],
        "limitations": [
            "Frequencies describe saved search alternatives, not a probability distribution over optimal routes.",
            "Coverage, reporting intensity and unequal search convergence can resemble ecological change.",
            "Travel context is descriptive; causal species detours require a separate ablation experiment.",
        ],
    }
    dump(output, result)
    return result
