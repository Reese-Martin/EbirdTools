from datetime import date
import math
from pathlib import Path
import time


from .util import ManagedDuckDB, csv_rows, dump, q, read, sha256, write_csv


def haversine(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2 - lat1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.7613 * 2 * math.asin(min(1, math.sqrt(h)))


def build_problem(
    dataset,
    output,
    config,
    keep=(),
    expand_route=None,
    expand_miles=50,
    airports=None,
    flights=None,
    from_date=None,
    to_date=None,
    states=(),
    access_catalog=None,
):
    start = time.monotonic()
    dataset = Path(dataset).resolve()
    manifest = read(dataset / "manifest.json")
    if manifest["year"] != config.year:
        raise ValueError("dataset year and configuration year differ")
    for name, digest in manifest["outputs"].items():
        if sha256(dataset / name) != digest:
            raise ValueError(f"dataset hash mismatch: {name}")
    with ManagedDuckDB() as con:
        con.execute(f"SET memory_limit={q(config.memory_limit)}")
        con.execute(f"SET threads={config.threads}")
        clauses = [f"date >= {q(config.first_date)}::DATE", f"date <= {q(config.last_date)}::DATE"]
        for value, op in [(from_date, ">="), (to_date, "<=")]:
            if value:
                d = date.fromisoformat(value)
                if d.year != config.year:
                    raise ValueError("candidate date filters must fall in the configured year")
                clauses.append(f"date {op} {q(value)}::DATE")
        from .config import STATES

        if set(states) - STATES:
            raise ValueError("candidate states must be lower-48/DC US-XX codes")
        if states:
            clauses.append("state IN (" + ",".join(q(s) for s in sorted(states)) + ")")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        from .eod import attach_dataset

        _, observations = attach_dataset(con, dataset, manifest)
        con.execute(f"CREATE VIEW obs AS SELECT * FROM {observations}{where}")
        locations = [
            dict(zip(("id", "name", "lat", "lon", "state", "locality_id"), r))
            for r in con.execute(
                "SELECT location_id,min(locality),lat,lon,state,locality_id FROM obs GROUP BY ALL ORDER BY 1"
            ).fetchall()
        ]
        by_id = {l["id"]: l for l in locations}
        if not locations:
            raise ValueError("no eligible observations; inspect review.csv and the import manifest")
        requested = set(keep)
        if config.start_location:
            requested.add(config.start_location)
        if requested - set(by_id):
            raise ValueError(f"unknown retained/start location IDs: {sorted(requested - set(by_id))}")
        if config.target_locations >= len(locations):
            # Retaining everything needs no rarity/ranking hash tables or sorting.
            selected, protected, grid = set(by_id), requested, {}
        else:
            # Protect every location supporting rare species or short observed windows. No interpolated presence.
            protected = {
                r[0]
                for r in con.execute(
                    """WITH rare AS (
                SELECT species_id FROM obs GROUP BY 1 HAVING count(DISTINCT (date,location_id))<=?
                OR date_diff('day',min(date),max(date))<=?)
                SELECT DISTINCT location_id FROM obs JOIN rare USING(species_id)""",
                    [config.rare_opportunities, config.short_window_days],
                ).fetchall()
            }
            protected |= requested
            if expand_route:
                route = read(expand_route)
                anchors = [s for d in route["days"] for s in d["stops"]]
                protected |= {
                    l["id"] for l in locations if any(haversine(l, a) <= expand_miles for a in anchors)
                }
            ranked = [
                r[0]
                for r in con.execute("""WITH n AS (SELECT species_id,count(DISTINCT (date,location_id)) AS n
                FROM obs GROUP BY 1), pairs AS (SELECT DISTINCT location_id,species_id FROM obs)
                SELECT location_id FROM pairs JOIN n USING(species_id) GROUP BY 1
                ORDER BY sum(1.0/n) DESC,count(*) DESC,location_id""").fetchall()
            ]
            # Grid representatives retain geographically distinctive combinations and possible connecting sites.
            grid = {}
            for loc in ranked:
                l = by_id[loc]
                cell = (
                    math.floor(l["lat"] / config.grid_degrees),
                    math.floor(l["lon"] / config.grid_degrees),
                )
                grid.setdefault(cell, loc)
            selected = protected | set(grid.values())
            for loc in ranked:
                if len(selected) >= config.target_locations:
                    break
                selected.add(loc)
        chosen = sorted((by_id[i] for i in selected), key=lambda l: l["id"])
        for l in chosen:
            l["kind"] = "birding"
        access_source = None
        if access_catalog:
            access_source = {
                "path": str(Path(access_catalog).resolve()),
                "sha256": sha256(access_catalog),
            }
            entries = {}
            for a in csv_rows(access_catalog):
                key = (a["locality_id"], float(a["latitude"]), float(a["longitude"]))
                if key in entries:
                    raise ValueError("duplicate exact-point road access entry")
                lat, lon, offset = (
                    float(a["road_latitude"]),
                    float(a["road_longitude"]),
                    float(a["offset_meters"]),
                )
                if (
                    not all(math.isfinite(v) for v in (lat, lon, offset))
                    or not 24 <= lat <= 50
                    or not -125 <= lon <= -66
                    or offset < 0
                    or not a["source"]
                ):
                    raise ValueError("invalid road access entry")
                entries[key] = {
                    "lat": lat,
                    "lon": lon,
                    "offset_meters": offset,
                    "source": a["source"],
                    "walking_policy": a["walking_policy"],
                }
            for l in chosen:
                key = (l["locality_id"], l["lat"], l["lon"])
                if key not in entries:
                    raise ValueError("road access catalog must cover every selected birding location")
                l["road_access"] = entries[key]
        air_index = {}
        if airports:
            for a in csv_rows(airports):
                if a.get("on_land") != "1":
                    raise ValueError("airports require explicit on_land=1 and lower-48 state")
                from .config import STATES

                if (
                    a["state"] not in STATES
                    or not 24 <= float(a["latitude"]) <= 50
                    or not -125 <= float(a["longitude"]) <= -66
                ):
                    raise ValueError("airport outside lower 48")
                if a["airport_id"] in air_index:
                    raise ValueError("duplicate airport ID")
                air_index[a["airport_id"]] = len(chosen)
                chosen.append(
                    {
                        "id": "airport:" + a["airport_id"],
                        "name": a["name"],
                        "lat": float(a["latitude"]),
                        "lon": float(a["longitude"]),
                        "state": a["state"],
                        "locality_id": "",
                        "kind": "airport",
                    }
                )
        loc_index = {l["id"]: i for i, l in enumerate(chosen)}
        species = [
            dict(zip(("id", "name"), r))
            for r in con.execute(
                "SELECT species_id,min(common_name) FROM obs GROUP BY 1 ORDER BY 1"
            ).fetchall()
        ]
        species_index = {s["id"]: i for i, s in enumerate(species)}
        selected_ids = sorted(selected)
        con.execute('CREATE TABLE selected AS SELECT unnest(?::VARCHAR[]) AS id,unnest(?::INTEGER[]) AS location',
                    [selected_ids,[loc_index[i] for i in selected_ids]])
        con.execute('CREATE TABLE planning_species AS SELECT unnest(?::VARCHAR[]) AS species_id,unnest(?::INTEGER[]) AS species',
                    [list(species_index),list(species_index.values())])
        con.execute('''CREATE TEMP TABLE planning_rows AS
            SELECT date,selected.location,species FROM obs
            JOIN selected ON location_id=selected.id JOIN planning_species USING(species_id)''')
        # Aggregate compact integer identifiers, not repeated SHA/Latin-name strings.
        # Large cohorts are processed in calendar-month windows with stable IDs and
        # the same canonical date/location order; every observed pair is retained.
        windows = [(config.first_date, config.last_date)]
        if manifest['statistics']['dispositions'].get('eligible', 0) > 1000000:
            windows = []
            cursor = config.first_date
            from datetime import timedelta
            while cursor <= config.last_date:
                next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
                last = min(config.last_date, next_month - timedelta(days=1))
                windows.append((cursor,last))
                cursor = last + timedelta(days=1)
        opportunities = []
        for first,last in windows:
            result = con.execute(f"""SELECT date,location,list(DISTINCT species ORDER BY species)
                FROM planning_rows
                WHERE date BETWEEN {q(first)}::DATE AND {q(last)}::DATE
                GROUP BY 1,2 ORDER BY 1,2""")
            for batch in iter(lambda: result.fetchmany(1000), []):
                for day,location,ids in batch:
                    opportunities.append({'id':len(opportunities),'day':(day-config.first_date).days,
                                          'date':str(day),'location':location,'species':ids})
        flight_links = []
        if config.flights:
            if not airports or not flights:
                raise ValueError(
                    "flights enabled: both airport CSV and directed flight-network CSV are required"
                )
            for f in csv_rows(flights):
                minutes = float(f["minutes"])
                if not math.isfinite(minutes) or not 0 < minutes <= 1440:
                    raise ValueError(
                        "flight plus airport overhead must fit one positive 24-hour transit day"
                    )
                flight_links.append(
                    {
                        "from": air_index[f["from_airport"]],
                        "to": air_index[f["to_airport"]],
                        "minutes": minutes,
                    }
                )
        con.close()
        ceiling = len({s for o in opportunities for s in o["species"]})
        problem = {
            "schema": 1,
            "config": config.to_dict(),
            "dataset": str(dataset),
            "manifest": manifest,
            "manifest_sha256": sha256(dataset / "manifest.json"),
            "locations": chosen,
            "species": species,
            "candidate_scope": {"from_date": from_date, "to_date": to_date, "states": sorted(states)},
            "opportunities": opportunities,
            "flight_links": flight_links,
            "access_catalog": access_source,
            "reduction": {
                "input_locations": len(locations),
                "selected_birding_locations": len(selected),
                "protected_locations": len(protected),
                "grid_representatives": len(grid),
                "target_is_soft": True,
                "candidate_species_ceiling": ceiling,
                "policy": ("all eligible locations and dates; no candidate reduction" if len(selected) == len(locations)
                           else "all dates; rare/short-window protection, inverse-opportunity ranking, grid connectors"),
                "warning": "reduction can remove the best itinerary; this ceiling is restricted to retained candidates",
            },
            "preparation_seconds": time.monotonic() - start,
        }
        dump(output, problem)
        write_csv(
            Path(output).with_suffix(".locations.csv"),
            [
                {
                    "id": l["id"],
                    "name": l["name"],
                    "latitude": l["lat"],
                    "longitude": l["lon"],
                    "state": l["state"],
                }
                for l in chosen
            ],
            ["id", "name", "latitude", "longitude", "state"],
        )
        return problem
