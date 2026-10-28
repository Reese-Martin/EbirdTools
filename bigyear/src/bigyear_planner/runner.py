from collections import defaultdict
from contextlib import ExitStack
from datetime import timedelta
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


from .travel import load_roads, transitions
from .config import Config
from .walking import access_walk, stop_minutes
from .util import ManagedDuckDB, ROOT, dump, hardware, q, read, sha256, write_csv


def compile_engine(ortools_prefix=None):
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    if ortools_prefix:
        cmake = shutil.which("cmake") or str(Path(sys.executable).with_name("cmake"))
        subprocess.run(
            [
                cmake,
                "-S",
                str(ROOT / "cpp"),
                "-B",
                str(build / "ortools"),
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBIGYEAR_ORTOOLS=ON",
                f"-DCMAKE_PREFIX_PATH={ortools_prefix}",
            ],
            check=True,
        )
        subprocess.run([cmake, "--build", str(build / "ortools"), "--parallel"], check=True)
        return build / "ortools" / "bigyear-engine"
    compiler = shutil.which("c++") or shutil.which("g++")
    if not compiler:
        raise ValueError("a C++17 compiler is required; see README.md")
    engine = build / "bigyear-engine"
    sources = [ROOT / "cpp" / "search.cpp", ROOT / "cpp" / "model.h", ROOT / "cpp" / "bounded_roads.h", ROOT / "cpp" / "compact_catalog.h"]
    if not engine.exists() or any(p.stat().st_mtime > engine.stat().st_mtime for p in sources):
        subprocess.run(
            [compiler, "-std=c++17", "-O3", "-pthread", str(sources[0]), "-o", str(engine)],
            check=True,
        )
    return engine


def export_engine_problem(problem, roads, plans, path):
    from .dense_roads import DenseRoads
    from .bounded_roads import BoundedRoads

    c = problem["config"]
    n = len(problem["locations"])
    start = (
        -1
        if c["start_location"] is None
        else next(i for i, l in enumerate(problem["locations"]) if l["id"] == c["start_location"])
    )
    with open(path, "w") as f:
        days = Config(**c).days
        magic = 'BY3' if isinstance(roads, BoundedRoads) else 'BY2' if isinstance(roads, DenseRoads) else 'BY1'
        if c.get('walking_speed_kmh') is not None:
            magic += 'W'
        if c.get('mixed_road_birding', False):
            magic += 'M'
        f.write(
            f"{magic} {days} {n} {len(problem['species'])} {len(problem['opportunities'])} {start} {c['local_miles']} {c['activity_minutes']} {c['drive_minutes']} {c['birding_minutes']}\n"
        )
        for o in problem["opportunities"]:
            f.write(
                f"{o['day']} {o['location']} {len(o['species'])} "
                + " ".join(map(str, o["species"]))
                + "\n"
            )
        if c.get('walking_speed_kmh') is not None:
            for loc in problem['locations']:
                f.write(f'{stop_minutes(loc, c):.17g}\n')
        if isinstance(roads, BoundedRoads):
            import json
            from .travel import road_point

            f.write(f'{roads.address[1]} {json.dumps(roads.token)} {roads.manifest["native_hot_entries"]}\n')
            for loc in problem['locations']:
                anchor = road_point(loc)
                f.write(f'{anchor["lat"]:.17g} {anchor["lon"]:.17g}\n')
            return
        if isinstance(roads, DenseRoads):
            if c['flights']:
                raise ValueError('compact native road matrices currently require flights=false')
            import json

            f.write(json.dumps(str(roads.binary)) + ' ' + str(roads.n) + '\n')
            f.write(' '.join(map(str, roads.nodes)) + '\n')
            return
        f.write(str(len(roads)) + "\n")
        for (a, b), r in roads.items():
            f.write(f"{a} {b} {r['miles']:.12g} {r['minutes']:.12g}\n")
        f.write(str(sum(map(len, plans.values()))) + "\n")
        for (a, b), pp in plans.items():
            for p in pp:
                f.write(f"{a} {b} {p['days']} {p['minutes']:.12g} {p['miles']:.12g}\n")


def materialize(problem, roads, plans, selected, engine, *, calendar_only=False):
    c = problem["config"]
    cfg = Config(**c)
    n = cfg.days
    days = [
        {
            "date": str(cfg.first_date + timedelta(days=d)),
            "kind": "rest",
            "stops": [],
            "legs": [],
            "road_miles": 0,
            "activity_minutes": 0,
            "travel_minutes": 0,
            "new_species": [],
            "cumulative_species": 0,
        }
        for d in range(n)
    ]
    groups = defaultdict(list)
    for i in selected:
        groups[problem["opportunities"][i]["day"]].append(problem["opportunities"][i])
    here = next(
        (i for i, l in enumerate(problem["locations"]) if l["id"] == c["start_location"]), None
    )
    if here is None:
        here = next(iter(groups.values()))[0]["location"] if groups else 0
    initial = here
    previous = -1

    def leg(a, b, mode="road", fraction0=0, fraction1=1, flight_minutes=None):
        road = roads.get((a, b), {})
        return {
            "from": a,
            "to": b,
            "mode": mode,
            "start_fraction": fraction0,
            "end_fraction": fraction1,
            "miles": road.get("miles", 0) * (fraction1 - fraction0) if mode == "road" else 0,
            "minutes": road.get("minutes", 0) * (fraction1 - fraction0)
            if mode == "road"
            else flight_minutes,
            "geometry": road.get("geometry") if mode == "road" else None,
            "schematic": mode == "flight" or not road.get("geometry"),
        }

    for day, opps in sorted(groups.items()):
        first = opps[0]["location"]
        internal = [roads.get((a["location"], b["location"])) for a, b in zip(opps, opps[1:])]
        if any(r is None for r in internal):
            raise ValueError("engine returned an unavailable local road")
        im = sum(r["miles"] for r in internal)
        it = sum(r["minutes"] for r in internal)
        visit_minutes = sum(stop_minutes(problem['locations'][o['location']], c) +
            (problem['joint_marine']['selected_trips'][o['marine_trip']]['activity_minutes']-c['birding_minutes']
             if o.get('marine_trip') else 0.) for o in opps)
        mixed = c.get('mixed_road_birding', False) and not any(o.get('marine_trip') for o in opps)
        if it + visit_minutes > c['activity_minutes'] + 1e-6 or (mixed and it > c['drive_minutes'] + 1e-6):
            raise ValueError('engine returned stops exceeding the activity budget including walking')
        if not mixed and im > c['local_miles'] + 1e-6:
            raise ValueError('engine returned stops exceeding the local road mileage budget')
        direct = roads.get((here, first))
        choices = []
        if (
            direct
            and (mixed or im + direct["miles"] <= c["local_miles"] + 1e-6)
            and (not mixed or it + direct['minutes'] <= c['drive_minutes'] + 1e-6)
            and it + direct["minutes"] + visit_minutes
            <= c["activity_minutes"] + 1e-6
        ):
            choices.append((direct["minutes"], direct["miles"], None))
        available_plans = plans.for_window(here, first, previous, day) if hasattr(plans, 'for_window') else plans.get((here, first), [])
        choices += [
            (p["minutes"], p["miles"], p)
            for p in available_plans
            if p["days"] <= day - previous - 1
        ]
        if not choices:
            raise ValueError("engine returned an infeasible transition")
        _, _, chosen = min(choices, key=lambda x: (x[0], x[1], x[2] is not None))
        if chosen:
            cursor = chosen.get('start_day', day - chosen["days"])
            for segment in chosen["segments"]:
                for k in range(segment["days"]):
                    d = days[cursor]
                    d["kind"] = "flight" if segment["mode"] == "flight" else "road_transit"
                    d["legs"] = [
                        leg(
                            segment["from"],
                            segment["to"],
                            segment["mode"],
                            k / segment["days"],
                            (k + 1) / segment["days"],
                            segment.get("minutes"),
                        )
                    ]
                    d["road_miles"] = d["legs"][0]["miles"]
                    d["travel_minutes"] = d["legs"][0]["minutes"]
                    d["activity_minutes"] = d["travel_minutes"]
                    cursor += 1
        elif here != first:
            days[day]["legs"].append(leg(here, first))
        d = days[day]
        d["kind"] = "birding"
        for j, o in enumerate(opps):
            if j and opps[j - 1]["location"] != o["location"]:
                d["legs"].append(leg(opps[j - 1]["location"], o["location"]))
            l = problem["locations"][o["location"]]
            d["stops"].append(
                {
                    "opportunity": o["id"],
                    "location": o["location"],
                    "name": l["name"],
                    "lat": l["lat"],
                    "lon": l["lon"],
                    "species": [problem["species"][s]["id"] for s in o["species"]],
                    "new_species": [],
                    "evidence": {},
                }
            )
            if c.get('walking_speed_kmh') is not None:
                d['stops'][-1].update({**access_walk(l, c), 'birding_minutes': c['birding_minutes']})
        d["road_miles"] = sum(x["miles"] for x in d["legs"])
        d["travel_minutes"] = sum(x["minutes"] for x in d["legs"])
        d["activity_minutes"] = d["travel_minutes"] + visit_minutes
        if c.get('walking_speed_kmh') is not None:
            d['walking_minutes'] = sum(s['walking_minutes'] for s in d['stops'])
            d['walking_meters'] = sum(s['walking_meters'] for s in d['stops'])
        here = opps[-1]["location"]
        previous = day
    if calendar_only:
        # An optimization reference for actual free dates, not an evidence-
        # backed route. Full materialization and independent audit still run
        # before any saved result is called verified.
        return {'year': c['year'], 'config': c, 'days': days,
                'initial_location': initial, 'calendar_only': True}
    with ManagedDuckDB() as con:
        con.execute(f"SET memory_limit={q(c['memory_limit'])}")
        con.execute(f"SET threads={int(c['threads'])}")
        con.execute("CREATE TABLE selected_stops(date DATE, location_id VARCHAR)")
        keys = sorted(
            {(d["date"], problem["locations"][s["location"]]["id"]) for d in days for s in d["stops"]}
        )
        if keys:
            con.executemany("INSERT INTO selected_stops VALUES (?,?)", keys)
        from .eod import attach_dataset

        evidence, _ = attach_dataset(con, problem["dataset"], problem["manifest"])
        con.execute(
            f"CREATE TABLE evidence AS SELECT e.* FROM {evidence} e JOIN selected_stops USING(date,location_id)"
        )
        con.execute("CREATE INDEX evidence_lookup ON evidence(date,location_id,species_id)")
        seen = set()
        for d in days:
            for s in d["stops"]:
                loc = problem["locations"][s["location"]]
                for species in s["species"]:
                    rows = con.execute(
                        "SELECT DISTINCT checklist_id,event_id,record_id,source_name FROM evidence WHERE date=? AND location_id=? AND species_id=? ORDER BY 1",
                        [d["date"], loc["id"], species],
                    ).fetchall()
                    s["evidence"][species] = [
                        {"checklist": r[0], "event": r[1], "record": r[2], "taxon": r[3]} for r in rows
                    ]
                    if species not in seen:
                        seen.add(species)
                        s["new_species"].append(species)
                        d["new_species"].append(species)
            d["cumulative_species"] = len(seen)
        con.close()
        route = {
            "schema": 1,
            "year": c["year"],
            "synthetic": problem["manifest"]["synthetic"],
            "data_release": problem["manifest"]["release"],
            "source_format": problem["manifest"].get("source_format", "EBD"),
            "taxonomy_policy": problem["manifest"]["taxonomy_policy"],
            "coverage": problem["manifest"]["statistics"],
            "initial_location": initial,
            "status": "optimal primary objective within " + engine["model"] + " candidate model"
            if engine["optimal"]
            else "best route found",
            "species_total": len(seen),
            "annual_recorded_species_ceiling": problem["manifest"]["statistics"][
                "annual_recorded_species_ceiling"
            ],
            "candidate_species_ceiling": problem["reduction"]["candidate_species_ceiling"],
            "candidate_scope": problem.get("candidate_scope", {}),
            "candidate_reduction": problem["reduction"],
            "optimization_upper_bound": None,
            "search": engine,
            "config": c,
            "locations": problem["locations"],
            "species": problem["species"],
            "days": days,
            "assumptions": [
                f"Experiment {cfg.first_date} through {cfg.last_date}: {'freely chosen starting location' if c['start_location'] is None else c['start_location']}; unrestricted finish.",
                (f"Mixed road/birding day: at most {c['activity_minutes'] / 60:g} total activity hours, including birding, walking and measured driving; at most {c['drive_minutes'] / 60:g} driving hours, with no road-mile cap. Boat-day access rules remain unchanged."
                 if c.get('mixed_road_birding', False) else
                 f"Birding day: at most {c['local_miles']} road miles and {c['activity_minutes'] / 60:g} activity hours, including local travel."),
                f"Road transit: at most {c['drive_minutes'] / 60:g} driving hours per day; no species credit. Flights are {'enabled' if c['flights'] else 'disabled'}.",
                "Perfect hindsight; deterministic credit throughout the reported day at the exact recorded location.",
                "No presence interpolated between dates; offshore and unclassified land locations excluded.",
                f"Each visited location-date requires {c['birding_minutes']} birding minutes regardless of species count.",
                "Locality coordinates approximate checklist locations; traveling checklists may have spatial uncertainty.",
                "Road times use the supplied network, not reconstructed historical traffic or closures.",
                (f"For road-anchor gaps over {c.get('walking_threshold_m', 100):g} m, add the full round-trip straight-line access distance at {c['walking_speed_kmh']:g} km/h to on-site birding time. Each visit returns to its road anchor. Smaller gaps remain within the birding allowance; footpaths and legal access are assumed, not verified."
                 if c.get('walking_speed_kmh') is not None else
                 "Where road-access anchors are supplied, roads connect those anchors; reported observation coordinates remain unchanged. Walking and birding share the fixed per-stop allowance."),
                "Long road legs are split into equal-time daily portions; geometry position uses proportional path length.",
                "Within each date stops follow stable opportunity ID order; this restricts the searched itineraries.",
                "Flights use a simplified directed airport network: separate access road days, one flight day, separate egress days.",
                "Candidate reduction can remove the best route. Normal search does not establish global optimality.",
            ] + problem["manifest"].get("limitations", []),
        }
        if problem.get('joint_marine'):
            from .joint_marine import expand_calendar
            return expand_calendar(problem,route)
        return route


def run(*args, **kwargs):
    # All provider sockets, mmaps and database handles close even on a failed run.
    with ExitStack() as resources:
        return _run(*args, **kwargs, resources=resources)


def _run(
    problem_path,
    roads_path,
    output,
    seconds=None,
    threads=None,
    seed=None,
    exact=0,
    resume=None,
    engine_path=None,
    resources=None,
):
    out = Path(output).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("run output must be empty; resume into a new run directory")
    out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    p = read(problem_path)
    c = p["config"]
    if Path(problem_path).stat().st_mode & 0o222 == 0:
        # Read-only, completed models can be shared without one large copy per run.
        try:
            os.link(problem_path, out / 'problem.json')
        except OSError:
            shutil.copyfile(problem_path, out / 'problem.json')
    else:
        shutil.copyfile(problem_path, out / "problem.json")
    # An immutable SQLite backup avoids dependence on the mutable source road cache.
    from .travel import cache

    bounded = str(roads_path).endswith('.bounded.json')
    if bounded:
        from .bounded_roads import BoundedRoads

        roads = BoundedRoads(p, roads_path)
        resources.callback(roads.close)
        resources.enter_context(roads.bridge())
        shutil.copyfile(roads_path, out / 'road-provider.bounded.json')
        frozen_roads = out / 'roads.sqlite'
    elif str(roads_path).endswith('.matrix.json'):
        from .dense_roads import freeze_matrix

        frozen_roads = freeze_matrix(roads_path, out / 'roads.matrix.json')
    else:
        frozen_roads = out / 'roads.sqlite'
        source = cache(roads_path)
        dest = cache(frozen_roads)
        source.backup(dest)
        source.close()
        dest.close()
    if not bounded:
        roads = load_roads(p, frozen_roads)
        resources.callback(roads.close)
    plans = transitions(p, roads)
    export_engine_problem(p, roads, plans, out / "problem.by")
    engine = Path(engine_path) if engine_path else compile_engine()
    shutil.copyfile(engine, out / "engine-snapshot")
    engine = out / "engine-snapshot"
    engine.chmod(0o755)
    duration = seconds if seconds is not None else c["seconds"]
    workers = threads if threads is not None else c["threads"]
    actual_seed = seed if seed is not None else c["seed"]
    if not math.isfinite(duration) or duration <= 0 or workers < 1:
        raise ValueError("positive search duration and thread count required")
    cmd = [
        str(engine),
        str(out / "problem.by"),
        str(out),
        str(duration),
        str(workers),
        str(actual_seed),
        str(c["alternatives"]),
        str(c["near_species"]),
        str(exact),
    ]
    if resume:
        # Resume only an identical frozen model, avoiding ambiguous opportunity IDs.
        previous = Path(resume).parent
        same_model = False if bounded else sha256(previous / 'problem.by') == sha256(out / 'problem.by')
        if bounded:
            # Runtime bridge ports/tokens change. Compare immutable model and
            # provider identity instead; opportunity IDs and source stay fixed.
            same_model = (sha256(previous / 'problem.json') == sha256(out / 'problem.json')
                and sha256(previous / 'road-provider.bounded.json') == sha256(out / 'road-provider.bounded.json'))
        if not same_model and frozen_roads.name.endswith('.matrix.json'):
            # BY2 embeds its run-local matrix path. Its manifest and all other
            # model bytes must still match, including the exact node mapping.
            old_lines = (previous / 'problem.by').read_text().splitlines()
            new_lines = (out / 'problem.by').read_text().splitlines()
            same_model = (old_lines[0].startswith('BY2 ') and new_lines[0].startswith('BY2 ')
                and old_lines[:-2] == new_lines[:-2] and old_lines[-1] == new_lines[-1]
                and sha256(previous / 'roads.matrix.json') == sha256(frozen_roads)
                and sha256(previous / 'problem.json') == sha256(out / 'problem.json'))
        if not same_model:
            raise ValueError("checkpoint problem hash does not match this experiment")
        cmd.append(str(Path(resume).resolve()))
    executable_hash = sha256(engine)
    if bounded:
        # The live bridge needs only compact anchor coordinates and node indices.
        # Do not retain a second Python copy of every opportunity during search.
        del plans, p
        import gc
        gc.collect()
    job = subprocess.Popen(cmd)
    try:
        _, status, usage = os.wait4(job.pid, 0)
        job.returncode = os.waitstatus_to_exitcode(status)
    except KeyboardInterrupt:
        job.terminate()
        job.wait()
        raise
    if job.returncode:
        if bounded:
            dump(out / 'road-cache-metrics.json', roads.metrics())
        raise subprocess.CalledProcessError(job.returncode, cmd)
    result = read(out / "engine.json")
    if bounded:
        p = read(out / 'problem.json')
        plans = transitions(p, roads)
    route = materialize(p, roads, plans, result["selected"], result)
    if route["species_total"] != result["species"]:
        raise ValueError("engine species total differs from independent source reconstruction")
    if bounded and (abs(sum(d['road_miles'] for d in route['days']) - result['road_miles']) > 1e-5
                    or abs(sum(d['travel_minutes'] for d in route['days']) - result['travel_minutes']) > 1e-5):
        raise ValueError('remeasured selected road costs differ from the native search; verify the fixed provider')
    alternatives = []
    for a in result['alternatives']:
        if a['selected'] == result['selected'] and not result['optimal']:
            alternatives.append(route)
        else:
            alternatives.append(materialize(p, roads, plans, a['selected'],
                {**result, 'selected': a['selected'], 'species': a['species'], 'optimal': False}))
    if bounded:
        from .bounded_roads import freeze_used_roads

        proof = freeze_used_roads(p, roads, [route, *alternatives], frozen_roads)
        dump(out / 'road-cache-metrics.json', {**roads.metrics(), 'retained_evidence': proof,
            'provider_manifest_sha256': sha256(out / 'road-provider.bounded.json')})
    route["provenance"] = {
        "problem_sha256": sha256(out / "problem.json"),
        "roads_sha256": sha256(frozen_roads),
        "roads_file": frozen_roads.name,
        "engine_sha256": executable_hash,
        "dataset_manifest_sha256": p["manifest_sha256"],
    }
    if bounded:
        route['provenance']['road_network_sha256'] = roads.manifest['network_sha256']
        route['provenance']['road_matching_policy'] = roads.manifest['matching_policy']
    dump(out / "route.json", route)
    for i, alt in enumerate(alternatives):
        alt["provenance"] = route["provenance"]
        if alt is route:
            (out / 'route.json').chmod(0o444)
            os.link(out / 'route.json', out / f'alternative-{i+1}.json')
        else:
            dump(out / f"alternative-{i + 1}.json", alt)
    audit = [
        {
            "date": d["date"],
            "location": s["name"],
            "species_id": sp,
            "new_credit": int(sp in s["new_species"]),
            "checklists": "|".join(sorted({e["checklist"] for e in s["evidence"][sp]})),
            "source_records": "|".join(sorted({e["record"] for e in s["evidence"][sp] if e["record"]})),
        }
        for d in route["days"]
        for s in d["stops"]
        for sp in s["species"]
    ]
    write_csv(
        out / "species-audit.csv",
        audit,
        ["date", "location", "species_id", "new_credit", "checklists", "source_records"],
    )
    write_csv(
        out / "itinerary.csv",
        [
            {
                "date": d["date"],
                "kind": d["kind"],
                "stops": " | ".join(s["name"] for s in d["stops"]),
                "road_miles": d["road_miles"],
                "activity_minutes": d["activity_minutes"],
                **({'walking_minutes': d.get('walking_minutes', 0),
                    'walking_meters': d.get('walking_meters', 0)}
                   if c.get('walking_speed_kmh') is not None else {}),
                "new_species": len(d["new_species"]),
                "cumulative_species": d["cumulative_species"],
            }
            for d in route["days"]
        ],
        [
            "date",
            "kind",
            "stops",
            "road_miles",
            "activity_minutes",
            "new_species",
            "cumulative_species",
        ] + (['walking_minutes', 'walking_meters'] if c.get('walking_speed_kmh') is not None else []),
    )
    from .validate import validate

    validation = validate(out / "route.json", out / "problem.json", frozen_roads)
    dump(out / "validation.json", validation)
    if not validation["valid"]:
        raise ValueError(f"independent validation failed: {validation['errors'][:5]}")
    benchmark = {
        "synthetic": route["synthetic"],
        "hardware": hardware(),
        "threads": workers,
        "seed": actual_seed,
        "requested_seconds": duration,
        "engine": result,
        "wall_seconds": time.monotonic() - start,
        "engine_peak_rss_bytes": usage.ru_maxrss
        if sys.platform == "darwin"
        else usage.ru_maxrss * 1024,
        "engine_cpu_seconds": usage.ru_utime + usage.ru_stime,
        "model": {
            "locations": len(p["locations"]),
            "opportunities": len(p["opportunities"]),
            "species": len(p["species"]),
            "directed_roads": None if bounded else len(roads),
            "engine_input_bytes": (out / "problem.by").stat().st_size,
        },
        "dataset_preparation_seconds": p["manifest"]["preparation_seconds"],
        "problem_preparation_seconds": p["preparation_seconds"],
        "validation": validation,
    }
    if bounded:
        benchmark['road_cache'] = read(out / 'road-cache-metrics.json')
        benchmark['road_cost_mode'] = 'bounded measured on-demand costs; immutable selected-leg evidence'
        benchmark['proposal_ranking'] = 'geodesic/time proximity only for proposal ranking; never used as feasibility road cost'
    dump(out / "benchmark.json", benchmark)
    if bounded:
        native_input = out / 'problem.by'
        dump(out / 'native-input-provenance.json', {'format': 'BY3W' if c.get('walking_speed_kmh') is not None else 'BY3', 'bytes': native_input.stat().st_size,
            'sha256': sha256(native_input), 'removed_after_validation': True,
            'reproduction': 'Re-export from frozen problem.json and road-provider.bounded.json with a fresh local bridge; runtime socket credentials expire after search.'})
        native_input.unlink()
    return route
