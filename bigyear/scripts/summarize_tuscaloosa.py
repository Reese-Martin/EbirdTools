"""Summarize completed, validated pilot runs without changing their frozen outputs."""

from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
import json
import re

from bigyear_planner.util import dump, read, sha256, write_csv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/tuscaloosa-2025-03"
RUNS = ROOT / "runs/tuscaloosa-2025-03"


def summarize():
    rows = []
    for folder in sorted(RUNS.glob("*")):
        if not folder.is_dir() or not (folder / "validation.json").exists():
            continue
        audit, r, b = [
            read(folder / name) for name in ("validation.json", "route.json", "benchmark.json")
        ]
        assert audit["valid"] and r["species_total"] == audit["species_total"]
        history = [
            json.loads(line) for line in (folder / "improvements.jsonl").read_text().splitlines()
        ]
        kinds = Counter(d["kind"] for d in r["days"])
        rows.append(
            {
                "run": folder.name,
                "seed": b["seed"],
                "search_seconds": b["engine"]["elapsed_seconds"],
                "search_export_validation_wall_seconds": b["wall_seconds"],
                "engine_peak_rss_mib": b["engine_peak_rss_bytes"] / 1024**2,
                "python_peak_rss_mib": b["hardware"]["python_peak_rss_bytes"] / 1024**2,
                "species": r["species_total"],
                "road_miles": audit["road_miles"],
                "driving_hours": audit["travel_minutes"] / 60,
                "stops": sum(len(d["stops"]) for d in r["days"]),
                "birding_days": kinds["birding"],
                "transit_days": kinds["road_transit"],
                "rest_days": kinds["rest"],
                "unique_locations": len({s["location"] for d in r["days"] for s in d["stops"]}),
                "maximum_activity_hours": max(d["activity_minutes"] for d in r["days"]) / 60,
                "reached_121_on": next(
                    (d["date"] for d in r["days"] if d["cumulative_species"] == 121), None
                ),
                "seconds_to_121": next(
                    (h["elapsed_seconds"] for h in history if h["species"] == 121), None
                ),
                "evaluations": b["engine"]["candidate_evaluations"],
                "valid": True,
                "evidence_rows_checked": audit["evidence_rows_checked"],
                "route_sha256": sha256(folder / "route.json"),
            }
        )
    seeds = [r for r in rows if r["run"].startswith("seed-")]
    if len(seeds) != 3:
        raise ValueError(
            "all three ten-minute seed runs must finish and validate before summarizing"
        )
    best = min(seeds, key=lambda r: (-r["species"], r["driving_hours"], r["road_miles"]))
    requests = []
    for line in (DATA / "router.log").read_text().splitlines():
        match = re.match(r"\[([^]]+)\].*?/(?:table|route)/v1/", line)
        if match:
            requests.append(datetime.fromisoformat(match[1]))
    timing = {
        "dataset_preparation_seconds": read(DATA / "dataset/manifest.json")["preparation_seconds"],
        "candidate_preparation_seconds": read(DATA / "problem.json")["preparation_seconds"],
        "road_query_log_span_seconds": (max(requests) - min(requests)).total_seconds(),
        "road_timing_scope": "Span of local table/geometry request log timestamps, at one-second resolution; excludes graph preparation and final cache writing.",
        "search_scope": "Three 600-second, four-thread searches ran concurrently: about 606 seconds of elapsed wall time for that stage, not 30 elapsed minutes. A separate 60-second smoke test took about 65 seconds including export and validation.",
        "unmeasured": "No complete end-to-end stopwatch. Source downloads, installation, land preparation, graph construction, review and report generation are outside the benchmark wall times. Graph preparation took roughly 45 seconds in the session tool logs; this is an approximate observation, not a separately saved benchmark.",
    }
    report = {
        "scope": "Tuscaloosa County observation sample, March 1–31, 2025; road paths may leave the county.",
        "policy": "Fixed ABA 8.19, approved Alabama crosswalk; historical deterministic reported-day availability.",
        "best_run": best["run"],
        "timing": timing,
        "runs": rows,
        "config": read(DATA / "config.json"),
        "input_counts": read(DATA / "dataset/manifest.json")["statistics"],
        "access": read(DATA / "access-summary.json"),
        "road_network": "Geofabrik Alabama OSM 2025-01-01; OSRM 26.10.0 upstream car profile, MLD.",
        "land": read(DATA / "land-provenance.json"),
        "interpretation": "121 equals the eligible sample species ceiling, establishing the primary count ceiling is attainable under this model. Minimum travel is not proven. Agreement across seeds is convergence evidence, not a global travel-optimality certificate.",
        "limitations": [
            "Two hours per stop includes walking and birding; precise footpaths and access rights are not reconstructed.",
            "Original pins remain exact evidence keys; nearest road anchors determine driving costs.",
            "The land mask is a current coastline product, not a reconstructed historical shoreline; inland-water pins are walking proxies under the agreed policy.",
            "Road profile times do not reconstruct historical traffic or closures.",
            "Same-day stop order follows stable opportunity ID; species availability is deterministic throughout the recorded day.",
            "This is a March county sample, not full-year or national coverage.",
        ],
    }
    dump(RUNS / "pilot-summary.json", report)
    write_csv(RUNS / "run-summary.csv", rows, list(rows[0]))
    table = "".join(
        f"<tr><td>{escape(r['run'])}</td><td>{r['species']}</td><td>{r['road_miles']:.2f}</td><td>{r['driving_hours']:.2f}</td><td>{r['stops']}</td><td>{r['maximum_activity_hours']:.2f}</td><td>{r['seconds_to_121']:.2f}</td><td>Passed</td></tr>"
        for r in rows
    )
    notes = "".join(f"<li>{escape(n)}</li>" for n in report["limitations"])
    timing_rows = "".join(
        f"<tr><td>{escape(r['run'])}</td><td>{r['search_seconds']:.2f}</td><td>{r['search_export_validation_wall_seconds']:.2f}</td><td>{r['engine_peak_rss_mib']:.2f}</td><td>{r['python_peak_rss_mib']:.1f}</td></tr>"
        for r in rows
    )
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8"><title>Tuscaloosa historical pilot</title>
<style>body{{font:16px system-ui,sans-serif;line-height:1.6;color:#000;background:#fff;margin:40px auto;max-width:1100px;padding:0 24px;border-top:5px solid #36824b}}h1{{font:42px Georgia,serif}}h2{{font-size:22px;margin-top:32px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:10px 14px;border-bottom:1px solid #dbe9df;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#f0f7f2}}a{{color:#000;text-decoration-color:#36824b}}.number{{font-size:48px;font-family:Georgia,serif}}.cards{{display:flex;gap:60px;margin:24px 0}}small{{display:block;color:#000}}code{{font-size:13px}}</style>
<h1>Tuscaloosa historical pilot</h1><p>March 1–31, 2025. Fixed ABA 8.19. All 106 observation locations and 244 reported location/date opportunities retained.</p>
<div class="cards"><div><span class="number">{best["species"]}</span><small>distinct species / 121 eligible</small></div><div><span class="number">{best["road_miles"]:.1f}</span><small>road miles</small></div><div><span class="number">{best["driving_hours"]:.2f}</span><small>driving hours across March</small></div></div>
<p>{escape(report["interpretation"])}</p><p><a href="http://127.0.0.1:8766/">Open the interactive route map</a></p>
<h2>Controlled searches</h2><table><thead><tr><th>Run</th><th>Species</th><th>Road miles</th><th>Drive hours</th><th>Stops</th><th>Peak activity hours/day</th><th>Seconds to 121</th><th>Validation</th></tr></thead><tbody>{table}</tbody></table>
<p>Each long run received 600 seconds and four threads, with seeds 20250301–20250303. The three long searches ran concurrently on the same machine. The smoke run received 60 seconds.</p>
<h2>Measured runtime</h2><p>Machine: Apple M4 Pro, 14 logical CPUs, 24 GiB RAM. Import and taxonomy filtering took {timing['dataset_preparation_seconds']:.3f} seconds; candidate preparation took {timing['candidate_preparation_seconds']:.3f} seconds. The 11,236-cell road matrix and 11,130 pair geometries have a local request-log span of approximately {timing['road_query_log_span_seconds']:.0f} seconds, measured at one-second resolution and excluding graph preparation and final cache writing.</p>
<table><thead><tr><th>Run</th><th>Search seconds</th><th>Search + export + audit seconds</th><th>C++ peak MiB</th><th>Python peak MiB</th></tr></thead><tbody>{timing_rows}</tbody></table>
<p>{escape(timing['search_scope'])} {escape(timing['unmeasured'])}</p>
<h2>Scaling to the continental experiment</h2><p>The county problem has 106 locations and 244 dated opportunities. Directed road costs grow with the square of retained locations: 500 locations require 250,000 cells (22× this pilot), 1,000 require 1 million (89×), and 2,000 require 4 million (356×). These are pair counts, not runtime forecasts. Keep numeric costs for candidate pairs, then fetch detailed shapes for selected and alternative itineraries. A continental road graph needs its own memory benchmark; the Alabama build cannot establish whether a whole-country build fits this machine.</p>
<p>National search quality is the larger uncertainty. All 121 pilot species were found in under 1.6 seconds in each long run; the remaining budget improved travel. The national problem adds a full calendar, many more dated opportunities and competing seasonal windows. Next, measure a real full-year regional baseline, then 500/1,000/2,000-location scopes at 10-minute, one-hour and six-hour budgets, with repeat seeds and candidate-coverage audits. The location target is soft and protected rare or short-window opportunities may exceed it.</p>
<h2>Pelagic edge-case tests with the current sample</h2><p>This inland sample can support software fixtures, but cannot validate historical pelagic occurrence or detectability. Boat mode remains deferred. Build a separate, clearly synthetic test copy using existing species sets and road costs; treat selected locations as mock ports and add explicit offshore trip records with fabricated IDs. Keep the pilot evidence and its 121-species result unchanged.</p>
<ul><li>Require explicit port access and departure/return to the same port; missing road routes never imply boat access.</li><li>Charge an explicit half-day minimum once per excursion, including its at-sea portion; use a longer declared duration when supplied. Six hours would be half of the pilot's 12-hour activity limit; keep this configurable and compare four-, six- and eight-hour assumptions. Road access plus the excursion must fit the daily budget.</li><li>One excursion can credit multiple species without multiplying the half-day charge. Overlapping trips, offshore teleportation and species on incompatible dates must fail independent validation.</li><li>Preserve inland-water pins as walking proxies. Test an unreachable departure, a missing port, a trip that exceeds the remaining day and a next-day return.</li><li>For a tiny fixture, compare the search with exhaustive enumeration after boat mode exists. Later use coastal EBD records and documented port/trip durations to test realism.</li></ul>
<h2>Access and daily rules</h2><p>12 activity hours per birding day; 30 road miles per birding day; two hours per stop including walking; eight driving hours per transit day. Start freely on March 1, unrestricted finish on March 31. No flights or pelagic excursions.</p>
<p>Road access is separate from the original sighting coordinate. Median pin offset is 18 m; 12 pins exceed 100 m, three exceed 500 m, and the maximum is 963 m. All driving pairs and geometries were computed locally from the frozen Alabama road network.</p>
<h2>Saved evidence</h2><p><a href="{best["run"]}/itinerary.csv">Daily itinerary</a> · <a href="{best["run"]}/species-audit.csv">Species and checklist audit</a> · <a href="{best["run"]}/detour-review.csv">Single-stop detour review</a> · <a href="run-summary.csv">Search comparison</a> · <a href="{best["run"]}/validation.json">Independent validation</a></p>
<h2>Scope and assumptions</h2><ul>{notes}</ul>
<h2>Sources</h2><p>User-supplied EBD_relMar-2025 sample and approved crosswalk; <a href="https://download.geofabrik.de/north-america/us/alabama.html">Geofabrik Alabama archive</a>; <a href="https://github.com/Project-OSRM/osrm-backend">OSRM source and driving profile</a>; <a href="https://github.com/e-kotov/osrm-binaries">portable macOS build</a>; <a href="https://osmdata.openstreetmap.de/data/land-polygons.html">OSM land polygons</a>. The Census county outline is display context only.</p></html>'''
    (RUNS / "pilot-report.html").write_text(html)
    print(json.dumps({"best": best, "report": str(RUNS / "pilot-report.html")}, indent=2))
    return report


if __name__ == "__main__":
    summarize()
