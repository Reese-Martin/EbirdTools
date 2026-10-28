"""All-observer species accumulation, separate from immutable search results."""

from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from .eod import STATE_NAMES, attach_dataset
from .util import ManagedDuckDB, dump, q, read, sha256


COVERAGE_FILE = "observer-species-coverage.json"


def daily_species_counts(first_seen, year):
    """Credit each resolved species once, on its first report in this calendar year."""
    unique = {}
    for row in first_seen:
        day = date.fromisoformat(row["first_date"])
        if day.year != year:
            raise ValueError("species first report falls outside the coverage year")
        species = row["species_id"]
        unique[species] = min(day, unique.get(species, day))
    new = Counter(unique.values())
    result, total, day = [], 0, date(year, 1, 1)
    while day.year == year:
        total += new[day]
        result.append({"date": day.isoformat(), "new_species": new[day],
                       "cumulative_species": total})
        day += timedelta(days=1)
    return result


def build_observer_coverage(dataset, runs):
    """Scan all eligible evidence, without search date/location/candidate filters."""
    dataset, runs = Path(dataset).resolve(), Path(runs).resolve()
    manifest = read(dataset / "manifest.json")
    digest = sha256(dataset / "manifest.json")
    for name, expected in manifest["outputs"].items():
        if sha256(dataset / name) != expected:
            raise ValueError(f"dataset hash mismatch: {name}")
    year = manifest["year"]
    with ManagedDuckDB() as con:
        con.execute("SET memory_limit='1GB'")
        con.execute("SET threads=2")
        evidence, _ = attach_dataset(con, dataset, manifest)
        con.execute(f"CREATE TEMP TABLE coverage_evidence AS SELECT state,species_id,date "
                    f"FROM {evidence} WHERE date >= {q(date(year, 1, 1))}::DATE "
                    f"AND date < {q(date(year + 1, 1, 1))}::DATE")
        states = [r[0] for r in con.execute(
            "SELECT DISTINCT state FROM coverage_evidence ORDER BY state").fetchall()]
        if not states:
            raise ValueError("observer coverage requires eligible source observations")
        rows = con.execute("SELECT species_id,min(date)::VARCHAR FROM coverage_evidence "
                           "GROUP BY species_id ORDER BY species_id").fetchall()
        source_rows = con.execute("SELECT count(*) FROM coverage_evidence").fetchone()[0]
    first_seen = [{"species_id": s, "first_date": d} for s, d in rows]
    daily = daily_species_counts(first_seen, year)
    if daily[-1]["cumulative_species"] != manifest["statistics"]["annual_recorded_species_ceiling"]:
        raise ValueError("all-observer species count differs from the dataset audit")
    state = states[0] if len(states) == 1 else None
    result = {
        "year": year, "state": state, "states": states,
        "scope_label": manifest.get("geographic_scope", {}).get("label")
                       or (STATE_NAMES[state.removeprefix("US-")] if state else ', '.join(states)),
        "data_release": manifest["release"], "taxonomy_policy": manifest["taxonomy_policy"],
        "synthetic": manifest["synthetic"], "dataset_manifest_sha256": digest,
        "annual_species": len(first_seen), "eligible_evidence_rows": source_rows,
        "method": "Distinct eligible species reported by all observers from January 1 through "
                  "the selected date, inclusive. Uses the route dataset's taxonomy, geography, "
                  "land and quality rules, before any search candidate reduction. "
                  "All-observer totals include the whole selected day; observation times are unavailable.",
        "first_seen": first_seen, "days": daily,
    }
    target = runs / COVERAGE_FILE
    supplement = read(target) if target.exists() else {"schema": 1, "datasets": {}}
    if supplement.get("schema") != 1:
        raise ValueError("unsupported observer coverage supplement")
    supplement["datasets"][digest] = result
    dump(target, supplement)
    return result


def attach_observer_coverage(root, route):
    """Only show coverage matching the frozen route's full source cohort and policy."""
    if route.get('marine_extension'):
        # Marine extensions bind their union to a different source catalog.
        # Never overwrite it with the terrestrial-only all-observer curve.
        try:
            coverage=route.get('marine_observer_coverage')
            if (coverage and coverage['annual_species']==route['annual_recorded_species_ceiling']
                    and coverage['days']==daily_species_counts(coverage['first_seen'],route['year'])):
                route['observer_coverage']={k:v for k,v in coverage.items() if k!='first_seen'}
        except (ValueError,TypeError,KeyError):pass
        return route
    target = Path(root) / COVERAGE_FILE
    if not target.exists():
        return route
    try:
        supplement = read(target)
        digest = route.get("provenance", {}).get("dataset_manifest_sha256")
        coverage = supplement["datasets"].get(digest)
        if (supplement["schema"] != 1 or not coverage
                or coverage["dataset_manifest_sha256"] != digest
                or coverage["year"] != route["year"]
                or coverage["data_release"] != route["data_release"]
                or coverage["taxonomy_policy"] != route["taxonomy_policy"]
                or coverage["synthetic"] != route["synthetic"]
                or coverage["annual_species"] != route["annual_recorded_species_ceiling"]
                or coverage["days"] != daily_species_counts(coverage["first_seen"], route["year"])
                or coverage["days"][-1]["cumulative_species"] != coverage["annual_species"]):
            return route
        route["observer_coverage"] = {k: v for k, v in coverage.items() if k != "first_seen"}
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        # Coverage is optional: stale metadata must not make a saved route unusable.
        pass
    return route
