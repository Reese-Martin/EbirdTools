"""Full-cohort compact search and small, independently auditable saved routes."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sys
import time

from .config import Config
from .coverage import COVERAGE_FILE, daily_species_counts
from .road_matching import verify_evidence
from .runner import compile_engine, materialize
from .travel import cache, load_roads, Transitions, road_point
from .util import dump, read, sha256, write_csv
from .validate import validate


def export_native(model, roads, target):
    m = model.meta
    c = m["config"]
    cfg = Config(**c)
    if cfg.walking_speed_kmh is None:
        raise ValueError("regional compact search requires an explicit walking speed")
    if c["start_location"] is not None:
        raise ValueError("compact rollout requires a freely chosen start")
    with Path(target).open("w") as stream:
        stream.write(
            f"BY4W{'M' if cfg.mixed_road_birding else ''} {cfg.days} {m['locations']} {len(m['species'])} {m['opportunities']} -1 {c['local_miles']} {c['activity_minutes']} {c['drive_minutes']} {c['birding_minutes']}\n"
        )
        stream.write(
            " ".join(
                [
                    json.dumps(str(model.root / "opportunities.bin")),
                    json.dumps(str(model.root / "species.bin")),
                    str(m["species_entries"]),
                    json.dumps(str(model.root / "coordinates.bin")),
                    str(roads.address[1]),
                    json.dumps(roads.token),
                    "65536",
                ]
            )
            + "\n"
        )
        max_gap_km = max(cfg.walking_threshold_m / 1000,
                         (cfg.activity_minutes - cfg.birding_minutes) * cfg.walking_speed_kmh / 120)
        # Additional slack covers returned network-distance rounding.
        stream.write(f"SCREEN {max_gap_km / 1.609344 + .001:.17g} {int(c['flights'])} {getattr(model,'minimum_flight_minutes',0):.17g}\n")
        if m.get('joint_marine'):
            stream.write('BOATS '+json.dumps(str(model.root/'marine-visits.txt'))+'\n')


def selected_problem(model, roads, selected):
    m = model.meta
    opps = [model.opportunity(i) for i in selected]
    indices = sorted({o["location"] for o in opps})
    mapping = {old: new for new, old in enumerate(indices)}
    if not indices:
        raise ValueError(
            "search found no nonempty route; keep checkpoint and inspect routing diagnostics"
        )
    locations = [roads.location(i) for i in indices]
    for index, o in enumerate(opps):
        o.update(id=index, location=mapping[o["location"]])
    problem = {
        "schema": 1,
        "config": m["config"],
        "dataset": m["dataset"],
        "manifest": m["manifest"],
        "manifest_sha256": m["manifest_sha256"],
        "locations": locations,
        "access_policy": roads.manifest['access_policy'],
        "species": m["species"],
        "candidate_scope": m["candidate_scope"],
        "opportunities": opps,
        "flight_links": [],
        "reduction": m["reduction"],
        "preparation_seconds": m["preparation_seconds"],
        "compact_source": {
            "path": str(model.path),
            "sha256": sha256(model.path),
            "selected_opportunities": selected,
            "selected_locations": indices,
            "components": m["components"],
        },
        "display_catalog": "Only selected locations/opportunities are embedded here; complete frozen search catalogs are bound in compact_source. Display size does not reduce the search cohort.",
    }
    if hasattr(roads, 'selection'):
        roads.selection(problem)
    return problem


def freeze_selected(p, roads, target):
    selected = p.get('routing_locations', p["compact_source"]["selected_locations"])

    # Materialization may ask costs for any pair of selected stops; no all-pairs
    # query is allowed. Only pairs used by the final chronological schedule.
    class SelectedRoads:
        def get(self, key, default=None):
            a, b = key
            if a == b:
                return {
                    "miles": 0.0,
                    "minutes": 0.0,
                    "geometry": None,
                    "source": "identity",
                    "network_release": "identity",
                }
            try:
                if hasattr(roads, 'cached_costs'):
                    cost = roads.cached_costs(selected[a], selected[b])
                    if cost is None:
                        return default
                    return {'miles': cost[0], 'minutes': cost[1], 'geometry': None,
                            'source': roads.manifest['network_release'], 'network_release': roads.manifest['network_release']}
                return roads[selected[a], selected[b]]
            except KeyError:
                return default

    adapter = SelectedRoads()
    return adapter


def settle_selected_calendar(model, roads, output, result, max_rescores=3):
    """Recheck fixed visits when pending costs finish during bridge shutdown.

    A feasible flight can be scored while a direct road is still pending. If
    that road subsequently finishes, serialization may prefer it. Keep the
    strict totals check, but re-evaluate the identical visits with the native
    evaluator before freezing evidence. This is never an additional search.
    """
    from .mobility import WindowTransitions
    from .regional_exploration import NativeEvaluator

    output = Path(output)
    original = dict(result)
    selected = list(result['selected'])
    rescored = []
    for attempt in range(max_rescores+1):
        problem = selected_problem(model, roads, selected)
        adapter = freeze_selected(problem, roads, None)
        plans = WindowTransitions(problem) if problem.get('window_transitions') is not None else Transitions(problem, adapter)
        calendar = materialize(problem, adapter, plans, list(range(len(selected))), result, calendar_only=True)
        miles = sum(d['road_miles'] for d in calendar['days'])
        minutes = sum(d['travel_minutes'] for d in calendar['days'])
        # calendar_only intentionally leaves modeled boat visits unexpanded.
        minutes += sum(problem['joint_marine']['selected_trips'][o['marine_trip']]['sailing_minutes']
            for o in problem['opportunities'] if o.get('marine_trip'))
        if abs(miles-result['road_miles']) <= 1e-5 and abs(minutes-result['travel_minutes']) <= 1e-5:
            if rescored:
                dump(output/'native-search-result-before-rescore.json', original)
                dump(output/'fixed-calendar-rescores.json', {'model_sha256':sha256(model.path),
                    'selected':selected,'search_performed':False,'rescores':rescored,
                    'reason':'A measured cost completed after the native snapshot; identical source visits re-evaluated.'})
                dump(output/'engine.json', result)
            return result, problem, adapter
        if attempt == max_rescores:
            raise ValueError('selected calendar costs did not stabilize after native fixed-visit replays')
        with NativeEvaluator(model, roads, output/f'fixed-calendar-rescore-{attempt+1}').open() as evaluator:
            score = evaluator.evaluate(selected, timeout=300)
        if score['pending'] or not score['feasible'] or score['species'] != result['species']:
            raise ValueError('fixed-visit native replay did not preserve the feasible species count')
        rescored.append({'before':{k:result[k] for k in ['species','road_miles','travel_minutes']},
            'native_score':score,'calendar_miles_before_replay':miles,'calendar_minutes_before_replay':minutes})
        result = {**result,'road_miles':score['miles'],'travel_minutes':score['minutes'],
            'fixed_calendar_rescores':len(rescored)}


def save_coverage(model, root):
    m = model.meta
    if m.get('joint_marine'):
        # Marine comparisons are embedded and explicitly partial; do not
        # overwrite terrestrial coverage under the unchanged dataset key.
        return
    manifest = m["manifest"]
    rows = [{"species_id": r["species"], "first_date": r["date"]} for r in m["first_seen"]]
    coverage = {
        "year": m["config"]["year"],
        "state": None,
        "states": m["candidate_scope"]["states"],
        "scope_label": "Lower 48 states and DC",
        "data_release": manifest["release"],
        "taxonomy_policy": manifest["taxonomy_policy"],
        "synthetic": manifest["synthetic"],
        "dataset_manifest_sha256": m["manifest_sha256"],
        "annual_species": len(rows),
        "eligible_evidence_rows": m["eligible_presence_groups"],
        "first_seen": rows,
        "days": daily_species_counts(rows, m["config"]["year"]),
        "method": "Distinct eligible species reported by all observers, January 1 through the selected day inclusive; complete national eligible cohort, same quality/land/taxonomy rules as search.",
    }
    target = Path(root)/COVERAGE_FILE
    existing = read(target) if target.exists() else {'schema':1,'datasets':{}}
    if existing.get('schema')!=1:
        raise ValueError('unsupported existing coverage file')
    existing['datasets'][m['manifest_sha256']] = coverage
    dump(target,existing)


def run(
    model_path, roads, output, seconds=600, threads=2, seed=20240101, resume=None, seed_file=None, blocks_path=None
):
    started = time.monotonic()
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / "route.json").exists():
        raise ValueError("finished result exists; choose another run directory")
    model = roads.model
    if Path(model_path).resolve() != model.path:
        raise ValueError("regional roads and requested compact model differ")
    executable = compile_engine()
    snapshot = out / "engine-snapshot"
    shutil.copyfile(executable, snapshot)
    snapshot.chmod(0o755)
    dump(
        out / "search-binding.json",
        {
            "model": str(model.path),
            "sha256": sha256(model.path),
            "road_provider": roads.manifest,
            "seconds": seconds,
            "threads": threads,
            "seed": seed,
        },
    )
    native = out / "problem.by"
    try:
        with roads.bridge():
            export_native(model, roads, native)
            cmd = [
                str(snapshot),
                str(native),
                str(out),
                str(seconds),
                str(threads),
                str(seed),
                "1",
                "2",
                "0",
            ]
            if seed_file:
                if resume:
                    raise ValueError("choose a checkpoint or a source-derived seed")
                initial = read(seed_file)
                if initial["model_sha256"] != sha256(model.path):
                    raise ValueError("warm-start model differs")
                source_path = initial.get('source_file', initial.get('source_route'))
                source_digest = initial.get('source_sha256', initial.get('source_route_sha256'))
                if not source_path or sha256(source_path) != source_digest:
                    raise ValueError("warm-start source changed")
                dump(out / "warm-start-source.json", initial)
                dump(out / "warm-start.json", initial["ids"])
                mode = initial.get('mode','greedy')
                if mode not in ['greedy','complete']:
                    raise ValueError('invalid source seed mode')
                cmd += [str(out / "warm-start.json")]
                if mode=='greedy':
                    cmd.append('greedy')
            elif resume:
                source_folder = Path(resume).resolve().parent
                previous = read(source_folder / 'route.json')
                if not read(source_folder / 'validation.json').get('valid'):
                    raise ValueError('resume requires an independently verified route')
                if previous['provenance']['compact_model_sha256'] != sha256(model.path):
                    raise ValueError('resume model differs')
                if previous['search']['selected_model_opportunities'] != read(resume):
                    raise ValueError('resume IDs differ from the verified route')
                if previous['provenance']['road_network_sha256'] != roads.manifest['network_sha256']:
                    raise ValueError('resume road network differs')
                dump(out / 'resume-binding.json', {
                    'route': str(source_folder / 'route.json'), 'route_sha256': sha256(source_folder / 'route.json'),
                    'checkpoint_sha256': sha256(resume), 'model_sha256': sha256(model.path),
                    'method': 'Complete incumbent restored once with measured current cached costs; no insertion rebuild.'})
                cmd.append(str(Path(resume).resolve()))
            environment = dict(os.environ)
            environment.pop('BIGYEAR_BLOCK_PROPOSALS',None)
            if blocks_path is not None:
                blocks_path = Path(blocks_path).resolve()
                dump(out/'regional-block-binding.json',{'path':str(blocks_path),'sha256':sha256(blocks_path),
                      'model_sha256':sha256(model.path),'purpose':'Regional macro proposals only; full candidate model retained.'})
                environment['BIGYEAR_BLOCK_PROPOSALS'] = str(blocks_path)
            job = __import__("subprocess").Popen(cmd,env=environment)
            try:
                _, status, usage = os.wait4(job.pid, 0)
                job.returncode = os.waitstatus_to_exitcode(status)
            except BaseException:
                job.terminate()
                job.wait()
                raise
            if job.returncode:
                raise RuntimeError("compact search failed; see router and cache diagnostics")
        result = read(out / "engine.json")
        if hasattr(roads, 'prepare_audit'):
            roads.prepare_audit()
        result, p, adapter = settle_selected_calendar(model, roads, out, result)
        selected = result["selected"]
        dump(out / "problem.json", p)
        # Build the calendar using original archive evidence, then freeze every
        # selected road's complete geometry and its independent endpoint proof.
        from .mobility import WindowTransitions

        plans = WindowTransitions(p) if p.get('window_transitions') is not None else Transitions(p, adapter)
        route = materialize(p, adapter, plans, list(range(len(selected))), result)
        pairs = {
            (leg["from"], leg["to"])
            for d in route["days"]
            for leg in d["legs"]
            if leg["mode"] == "road"
        }
        with cache(out / "roads.sqlite") as db:
            for a, b in sorted(pairs):
                x, y = p["locations"][a], p["locations"][b]
                ar, br = road_point(x), road_point(y)
                r = adapter.get((a, b))
                proof = roads.endpoint_evidence(
                    p.get('routing_locations', p["compact_source"]["selected_locations"])[a],
                    p.get('routing_locations', p["compact_source"]["selected_locations"])[b],
                    r,
                )
                verify_evidence(proof, [[ar["lon"], ar["lat"]], [br["lon"], br["lat"]]], r)
                geom = proof["routes"][0]["geometry"]
                db.execute(
                    "INSERT INTO roads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        x["id"],
                        y["id"],
                        r["miles"],
                        r["minutes"],
                        r["source"],
                        r["network_release"],
                        json.dumps(geom),
                        x["lat"],
                        x["lon"],
                        y["lat"],
                        y["lon"],
                        ar["lat"],
                        ar["lon"],
                        br["lat"],
                        br["lon"],
                    ),
                )
                db.execute(
                    "INSERT INTO road_endpoints VALUES (?,?,?)",
                    (x["id"], y["id"], json.dumps(proof)),
                )
        with closing(load_roads(p, out / "roads.sqlite")) as frozen:
            route = materialize(
                p, frozen, WindowTransitions(p) if p.get('window_transitions') is not None else Transitions(p, frozen), list(range(len(selected))), result
            )
        if route["species_total"] != result["species"]:
            raise ValueError("source species differ from native score")
        if (
            abs(sum(d["road_miles"] for d in route["days"]) - result["road_miles"]) > 1e-5
            or abs(sum(d["travel_minutes"] for d in route["days"]) - result["travel_minutes"])
            > 1e-5
        ):
            raise ValueError("selected road costs differ from native search")
        if hasattr(roads, 'finalize_route'):
            roads.finalize_route(route, p, out)
            dump(out/'problem.json',p)
        route["search"]["selected_model_opportunities"] = selected
        route["search"]["selected"] = list(range(len(selected)))
        route["provenance"] = {
            "problem_sha256": sha256(out / "problem.json"),
            "roads_sha256": sha256(out / "roads.sqlite"),
            "roads_file": "roads.sqlite",
            "engine_sha256": sha256(snapshot),
            "dataset_manifest_sha256": p["manifest_sha256"],
            "compact_model_sha256": sha256(model.path),
            "road_network_sha256": roads.manifest["network_sha256"],
            "road_matching_policy": roads.manifest["matching_policy"],
            "access_policy": roads.manifest['access_policy'],
        }
        route["assumptions"] += [
            roads.release["snapshot_caveat"],
            "Car routing opens regional tiles on demand across tile boundaries. Unresolved routing proposals are deferred; optimality is not established.",
            p["display_catalog"],
        ]
        if p.get('flight_network'):
            route['assumptions'] += [roads.model.network['policy'],
                f"Flight proposals consider {getattr(roads,'airport_shortlist_count',8)} nearby airports at each end, rank observed nonstop connections by service time and estimated ground access, and measure up to {getattr(roads,'airport_pair_limit',2)} alternatives. This restricts flight search choices; it does not reduce the observation cohort."]
        dump(out / "route.json", route)
        audit = validate(out / "route.json", out / "problem.json", out / "roads.sqlite")
        dump(out / "validation.json", audit)
        if not audit["valid"]:
            raise ValueError("independent audit failed: " + str(audit["errors"][:5]))
        write_csv(
            out / "itinerary.csv",
            [
                {
                    "date": d["date"],
                    "kind": d["kind"],
                    "road_miles": d["road_miles"],
                    "activity_minutes": d["activity_minutes"],
                    "walking_minutes": d.get("walking_minutes", 0),
                    "new_species": len(d["new_species"]),
                    "cumulative_species": d["cumulative_species"],
                }
                for d in route["days"]
            ],
            [
                "date",
                "kind",
                "road_miles",
                "activity_minutes",
                "walking_minutes",
                "new_species",
                "cumulative_species",
            ],
        )
        save_coverage(model, out.parent)
        dump(
            out / "benchmark.json",
            {
                "wall_seconds": time.monotonic() - started,
                "requested_seconds": seconds,
                "engine_peak_rss_bytes": usage.ru_maxrss
                if sys.platform == "darwin"
                else usage.ru_maxrss * 1024,
                "engine_cpu_seconds": usage.ru_utime + usage.ru_stime,
                "engine": result,
                "cache": roads.metrics(),
                "model": {
                    k: model.meta[k]
                    for k in [
                        "locations",
                        "opportunities",
                        "species_entries",
                        "eligible_presence_groups",
                        "represented_observations",
                    ]
                },
                "saved_route_bytes": (out / "route.json").stat().st_size,
                "validation": audit,
            },
        )
        return route
    finally:
        dump(out / "road-cache-metrics.json", roads.metrics())
        if native.exists():
            dump(
                out / "native-input-provenance.json",
                {
                    "format": "BY4W",
                    "sha256": sha256(native),
                    "bytes": native.stat().st_size,
                    "removed_expired_bridge_credentials": True,
                },
            )
            native.unlink()
