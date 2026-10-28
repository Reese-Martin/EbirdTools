"""Summarize completed national evidence and retained laptop storage."""

from collections import Counter
from html import escape
import json
from pathlib import Path
import shutil
import sqlite3

from bigyear_planner.util import ROOT, dump, read, sha256, write_csv

DATA = ROOT / "data/lower48-eod-2024"
OUT = ROOT / "runs/lower48-eod-2024"
RUN = OUT / "regional-600s"


def size(path):
    return sum(p.stat().st_size for p in Path(path).rglob("*") if p.is_file())


def main():
    global RUN
    choices = [
        p.parent
        for p in OUT.glob("*/route.json")
        if (p.parent / "benchmark.json").exists() and read(p.parent / "validation.json")["valid"]
    ]

    def objective(path):
        saved = read(path / "route.json")
        return (
            saved["species_total"],
            -sum(d["travel_minutes"] for d in saved["days"]),
            -sum(d["road_miles"] for d in saved["days"]),
        )

    RUN = max(choices, key=objective)
    route = read(RUN / "route.json")
    audit = read(RUN / "validation.json")
    if not audit["valid"]:
        raise ValueError("national route is not independently verified")
    old_cache = DATA / "routing-cache"
    if RUN.name == "regional-seeded-600s" and old_cache.exists():
        expected = {"provider.json", "costs.bin", "access.bin"}
        if old_cache.is_symlink() or {p.name for p in old_cache.iterdir()} != expected:
            raise ValueError(
                "unexpected files in obsolete geometric cache; preserve for inspection"
            )
        provider = read(old_cache / "provider.json")
        if provider.get("access_policy") or provider.get("format") != "bigyear-regional-roads-v1":
            raise ValueError("obsolete cache policy differs; preserve for inspection")
        dump(
            OUT / "invalidated-geometric-cache.json",
            {
                "provider": provider,
                "removed_bytes": size(old_cache),
                "reason": "Replaced geometric nearest-way anchors with locally routed car anchors. Completed baseline remains independently auditable through its frozen selected-road SQLite evidence.",
            },
        )
        for name in expected:
            (old_cache / name).unlink()
        old_cache.rmdir()
    if not (RUN / "router-stopped.json").exists():
        shutil.copyfile(OUT / "national-router-stopped.json", RUN / "router-stopped.json")
    model = read(DATA / "model/model.json")
    metrics = read(RUN / "benchmark.json")
    resources = read(OUT / (RUN.name + "-resources.json"))
    with sqlite3.connect(
        "file:" + str(DATA / "model/locations.sqlite") + "?mode=ro&immutable=1", uri=True
    ) as con:
        con.execute("PRAGMA cache_size=-8192")
        states = con.execute(
            "SELECT json_extract(metadata,'$.state'),count(*) FROM locations GROUP BY 1 ORDER BY 1"
        ).fetchall()
    from bigyear_planner.config import STATES

    if {r[0] for r in states} != STATES or sum(r[1] for r in states) != model["locations"]:
        raise ValueError("full lower48/DC coverage differs")
    write_csv(
        OUT / "model-state-locations.csv",
        [{"state": s, "exact_locations": n} for s, n in states],
        ["state", "exact_locations"],
    )
    config = route["config"]
    selected = {s["location"] for d in route["days"] for s in d["stops"]}
    stop_states = dict(sorted(Counter(
        route["locations"][s["location"]]["state"]
        for d in route["days"] for s in d["stops"]
    ).items()))
    # The benchmark remaps IDs into its selected-only display problem. Compare
    # the original full-model IDs with the seed's full-model IDs instead.
    engine = read(RUN / "engine.json")
    seed_path = RUN / "warm-start.json"
    seed_ids = set(read(seed_path)) if seed_path.exists() else None
    assessment = {
        "input_jurisdictions": len(states),
        "birding_visits_by_state": stop_states,
        "warm_start_seconds": engine.get("warm_start", {}).get("seconds", 0),
        "native_elapsed_seconds": engine["elapsed_seconds"],
        "selected_opportunities_outside_seed": (
            len(set(engine["selected"]) - seed_ids) if seed_ids is not None else None
        ),
    }
    dump(RUN / "search-assessment.json", assessment)
    location_rows = []
    for i in selected:
        l = route["locations"][i]
        a = l["road_access"]
        location_rows.append(
            {
                "location": i,
                "source_id": l["id"],
                "state": l["state"],
                "latitude_text": l.get("latitude_text", l["lat"]),
                "longitude_text": l.get("longitude_text", l["lon"]),
                "road_latitude": a["lat"],
                "road_longitude": a["lon"],
                "anchor_gap_m": a["offset_meters"],
                "walking_policy": "Full round trip above 100 m at 2.2 km/h; footpaths/legal access assumed.",
            }
        )
    write_csv(
        OUT / "selected-access-review.csv",
        sorted(location_rows, key=lambda r: -r["anchor_gap_m"]),
        list(location_rows[0]),
    )
    baseline = read(ROOT / "runs/west-coast-walking-2p2-2024/mmap-600s/benchmark.json")
    oldresources = read(
        ROOT / "runs/west-coast-walking-2p2-2024/walking-full-search-resources.json"
    )
    summary = {
        "valid": True,
        "year": 2024,
        "scope": "Lower 48 states and DC",
        "states_in_complete_model": len(states),
        "eligible_presence_groups": model["eligible_presence_groups"],
        "represented_observations": model["represented_observations"],
        "exact_locations": model["locations"],
        "location_date_opportunities": model["opportunities"],
        "eligible_species": len(model["species"]),
        "requested_seconds": metrics["requested_seconds"],
        "source_archive_manifest_sha256": sha256(
            ROOT / "data/eod-us-since-2000-zstd3/manifest.json"
        ),
        "model_sha256": sha256(DATA / "model/model.json"),
        "route_sha256": sha256(RUN / "route.json"),
        "route_run": RUN.name,
        "species_found": route["species_total"],
        "candidate_evaluations": metrics["engine"]["candidate_evaluations"],
        "deferred_candidates": metrics["engine"].get("deferred_candidates", 0),
        "routing": metrics["cache"],
        "warm_start": metrics["engine"].get("warm_start", {}),
        "search_assessment": assessment,
        "initial_national_baseline": {
            "species": read(OUT / "regional-600s/engine.json")["species"],
            "deferred_candidates": read(OUT / "regional-600s/engine.json")["deferred_candidates"],
        },
        "road_miles": sum(d["road_miles"] for d in route["days"]),
        "walking_hours": sum(d.get("walking_minutes", 0) for d in route["days"]) / 60,
        "birding_hours": sum(len(d["stops"]) for d in route["days"])
        * config["birding_minutes"]
        / 60,
        "days": dict(Counter(d["kind"] for d in route["days"])),
        "visited_states": sorted({route["locations"][i]["state"] for i in selected}),
        "search_resources": resources,
        "preparation_resources": read(OUT / "compact-preparation-resources.json"),
        "retained_storage": {
            "new_data_bytes": size(DATA),
            "new_run_bytes": size(OUT),
            "model_bytes": size(DATA / "model"),
            "routing_tile_bytes": read(DATA / "roads/release.json")["tile_bytes"],
            "bounded_caches_bytes": size(DATA / "routing-cache-connected"),
            "original_archive_bytes": 12765684370,
            "free_bytes": shutil.disk_usage(DATA).free,
        },
        "cleanup": {
            "original_archive_unchanged": sha256(
                ROOT / "data/eod-us-since-2000-zstd3/manifest.json"
            )
            == "aa981ebaa48a8a0db9760f99a888441399c7c88b8e9faf38687b88fc1e039879",
            "narrow_preparation_parquet_removed": not (DATA / "model/rows.parquet").exists(),
            "land_staging_removed": not (DATA / "land-staging").exists(),
            "native_bridge_credentials_removed": not (RUN / "problem.by").exists(),
            "routing_process_stopped": read(RUN / "router-stopped.json")["stopped"],
        },
        "west_comparison": {
            "species": baseline["engine"]["species"],
            "candidate_evaluations": baseline["engine"]["candidate_evaluations"],
            "requested_seconds": baseline["requested_seconds"],
            "combined_peak_rss_bytes": oldresources["combined_peak_rss_bytes"],
            "provider": "Memory-mapped OSRM, Western US graph",
            "interpretation": "Different road provider, full cohort, and proposal index. Species scores are search outcomes, not comparable ecological ceilings. This national run tests capacity; slow/deferred routing limits solution quality.",
        },
        "limitations": [
            f"Best found {metrics['requested_seconds']:g}-second heuristic route; no global optimality claim.",
            "Every eligible observation/candidate remains available. Deferred provider queries are unknown, not demonstrated unreachable roads.",
            "BRouter car-vario current regional OSM tiles and modeled speeds; no reconstructed 2024 traffic, closures, or ferry timetables.",
            "Straight-line access walking assumes a walkable/legal connection. Exact observation pins remain separate from road anchors.",
            "Presence-only experimental taxonomy policy, not ABA-certified historical eligibility.",
        ],
    }
    if not all(summary["cleanup"].values()):
        raise ValueError("completion cleanup/source integrity check failed")
    dump(OUT / "completion.json", summary)
    storage = summary["retained_storage"]
    n = summary
    rss = resources["combined_peak_rss_bytes"] / 2**30
    failures = metrics["cache"].get("deferred_queries", 0)
    geography_note = "Birding visits by state: " + ", ".join(
        f"{state.removeprefix('US-')} {count}" for state, count in stop_states.items()
    ) + ". All 49 jurisdictions remain in the input candidate model."
    seed_note = ""
    if seed_ids is not None:
        seed_note = (
            f"Rebuilding the source-derived starting itinerary took {assessment['warm_start_seconds'] / 60:.1f} "
            f"of {assessment['native_elapsed_seconds'] / 60:.1f} native runtime minutes. "
            f"The selected result contains {assessment['selected_opportunities_outside_seed']} opportunities outside that seed. "
            "The selected geography reflects search progress, not a national ecological limit."
        )
    styles = "body{font:17px/1.55 system-ui;background:#f5f6f8;color:#15202b;max-width:1080px;margin:32px auto;padding:0 22px}section{background:white;border-radius:14px;padding:20px 24px;margin:18px 0}h1,h2{line-height:1.2}h2{font-size:23px}table{border-collapse:collapse;width:100%}th,td{padding:10px;text-align:left;border-bottom:1px solid #ddd}.note{background:#fff3d5}a{color:#085ca8}.stats{display:flex;gap:22px;flex-wrap:wrap}.stat{min-width:150px}.stat b{display:block;font-size:30px}"
    html = f"""<!doctype html><html lang="en" data-run="{RUN.name}"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>2024 national laptop run</title><style>{styles}</style>
    <h1>2024 lower 48 and DC · full-cohort laptop run</h1><p id="live-status" role="status"></p>
    <p><a href="/?run={RUN.name}/route.json">Open the verified route and cumulative curves</a> · <a href="/selected-access-review.csv">Exact observation pins and access walks</a></p>
    <section><div class="stats"><div class="stat"><b>{n["represented_observations"]:,}</b>represented observations</div><div class="stat"><b>{n["exact_locations"]:,}</b>exact locations</div><div class="stat"><b>{n["location_date_opportunities"]:,}</b>location-date opportunities</div><div class="stat"><b>{n["eligible_species"]}</b>eligible species reported</div></div>
    <p>All eligible observations and exact source locality identities are retained. The original verified Parquet archive is unchanged. The native model uses memory-mapped arrays; the viewer embeds only selected display locations, bound to the complete model.</p></section>
    <section class="note"><h2>Capacity works; routing throughput limits search progress</h2>
    <p>The {metrics["requested_seconds"]:g}-second search found <strong>{n["species_found"]} species</strong> with {n["candidate_evaluations"]:,} completed candidate evaluations and {n["deferred_candidates"]:,} deferred candidates. The provider deferred {failures:,} queries because they could not be resolved under the fixed query limit and strict endpoint checks.</p>
    <p><strong>{escape(geography_note)}</strong> {escape(seed_note)}</p>
    <p>This pass uses locally routed car anchors and a source-derived starting itinerary; all stops, roads and walking are rechecked. Deferred queries receive no invented travel costs. Their endpoints remain in the complete candidate model. This is a feasible, independently verified heuristic route; global optimality is not established. The all-observer curve shows the full eligible national total through each date.</p></section>
    <section><h2>Runtime and memory</h2><table><tr><th>Measure</th><th>National · regional BRouter</th><th>West Coast · mapped OSRM</th></tr>
    <tr><td>Native search budget</td><td>{metrics["requested_seconds"]:g} seconds · 2 threads</td><td>600 seconds · 2 threads</td></tr>
    <tr><td>Completed candidate evaluations</td><td>{n["candidate_evaluations"]:,}</td><td>{baseline["engine"]["candidate_evaluations"]:,}</td></tr>
    <tr><td>Peak sampled process-tree RSS</td><td>{rss:.2f} GiB</td><td>{oldresources["combined_peak_rss_bytes"] / 2**30:.2f} GiB</td></tr>
    <tr><td>Best species count</td><td>{n["species_found"]} / {n["eligible_species"]}</td><td>491 / 570</td></tr></table>
    <p>Full national model preparation took {n["preparation_resources"]["wall_seconds"] / 60:.1f} minutes and peaked at {n["preparation_resources"]["combined_peak_rss_bytes"] / 2**30:.2f} GiB sampled RSS. The query allowance is 3 GiB, Java heap 3 GiB, and the process-tree guard 18 GiB. Source preparation/download time is additional.</p>
    <p>Different road providers and search throughput make score comparisons a solver-performance result, not evidence that the larger cohort contains fewer birds.</p></section>
    <section><h2>Verified calendar and walking</h2><p>{n["road_miles"]:,.0f} road miles · {n["birding_hours"]:.1f} hours birding · {n["walking_hours"]:.1f} hours walking.</p>
    <p>Each visit adds a full round-trip walk at 2.2 km/h when its road-anchor gap exceeds 100 m. Walking, on-site birding and local driving must fit the 12-hour birding-day activity limit. Separate road-transit days allow eight driving hours. Flights remain disabled.</p>
    <p>All 366 days, original observation evidence, exact pins, selected road costs/full geometry, endpoint matching, and walking budgets pass the independent validator. Footpaths and legal access are assumed.</p></section>
    <section><h2>Storage and cleanup</h2><p>New retained data: {storage["new_data_bytes"] / 2**30:.2f} GiB, including {storage["model_bytes"] / 2**30:.2f} GiB for the complete model and {storage["routing_tile_bytes"] / 2**30:.2f} GiB for regional tiles. Fixed routing/access caches total {storage["bounded_caches_bytes"] / 2**20:.1f} MiB. Free disk: {storage["free_bytes"] / 2**30:.1f} GiB.</p>
    <p>The 6.4 GiB preparation projection and land staging tiles are removed. Query spill is cleaned automatically. Checkpoints, reusable inputs, bounded caches and verified selected-road evidence remain. The router stops after completion. Viewing this result needs only the small route and coverage artifacts; it does not need running map services or the national map tiles.</p></section>
    <section><h2>Model assumptions</h2><ul>{"".join("<li>" + escape(t) + "</li>" for t in n["limitations"])}</ul></section></html>"""
    html += """<script>async function updateRunStatus(){try{const r=await fetch('/run-status.json');if(!r.ok)return;const s=await r.json();const e=document.getElementById('live-status');e.textContent=s.message||s.stage;if(s.stage==='completed'&&s.best_run&&s.best_run!==document.documentElement.dataset.run)location.reload();}catch{}}updateRunStatus();setInterval(updateRunStatus,10000);</script>"""
    (OUT / "pilot-report.html").write_text(html)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in [
                    "valid",
                    "species_found",
                    "eligible_species",
                    "candidate_evaluations",
                    "deferred_candidates",
                    "retained_storage",
                ]
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
