"""Independent calendar/road/evidence audit. Never calls the search feasibility evaluator."""

from contextlib import closing
import csv
from datetime import date, timedelta
import io
import math
import sqlite3
import zipfile
from pathlib import Path

import duckdb

from .config import STATES
from .travel import load_roads, road_point
from .road_matching import POLICY, gap_m, verify_evidence
from .config import Config
from .util import ManagedDuckDB, csv_rows, q, read, sha256


def validate(route_path, problem_path, roads_path):
    try:
        if read(problem_path).get('schema')=='marine-extension-v1':
            from .marine_validate import validate_marine
            return validate_marine(route_path,problem_path,roads_path)
        return _validate(route_path, problem_path, roads_path)
    except (KeyError, IndexError, TypeError, ValueError, OSError, zipfile.BadZipFile, duckdb.Error) as error:
        return {"valid": False, "errors": [f"malformed or unavailable audit input: {error}"]}


def _validate(route_path, problem_path, roads_path):
    errors = []
    endpoint_pairs = set()
    endpoint_gaps = []
    flight_archives_checked = []
    joint_context = None

    def check(condition, message):
        if not condition:
            errors.append(message)

    r, p = read(route_path), read(problem_path)
    c = p["config"]
    Config(**c)
    locs = p["locations"]
    if p.get('access_policy'):
        check(r.get('provenance',{}).get('access_policy')==p['access_policy'],
              'route surface access policy differs')
        holds=p['access_policy'].get('surface_access_holds')
        if holds:
            check(sha256(holds['path'])==holds['sha256'],'surface access hold evidence changed')
            blocked={tuple(v) for v in read(holds['path'])['exact_coordinate_holds']}
            for loc in locs:
                check((loc['lat'],loc['lon']) not in blocked,
                      'selected location has unresolved open-water access: '+loc['id'])
    dataset = Path(p["dataset"])
    manifest = read(dataset / "manifest.json")
    check(sha256(dataset / "manifest.json") == p["manifest_sha256"], "dataset manifest changed")
    for name, digest in manifest["outputs"].items():
        check(sha256(dataset / name) == digest, f"dataset changed: {name}")
    countability_decisions={}
    if manifest.get('countability', {}).get('adapter') == 'ebd-parquet-v1':
        from .ebd import verify_policy

        binding = manifest['inputs']['policy']
        policy = verify_policy(Path(binding['path']).parent)
        check(policy['id'] == manifest['taxonomy_policy'], 'EBD countability policy ID differs')
        check(sha256(binding['path']) == binding['sha256'] == manifest['countability']['policy_sha256'],
              'EBD countability policy binding differs')
    elif manifest.get('countability'):
        policy=read(dataset/'countability-policy.json')
        check(policy['id']==manifest['taxonomy_policy'], 'countability policy ID differs')
        check(sha256(dataset/'countability-policy.json')==manifest['countability']['policy_sha256'],
              'countability policy binding differs')
        source_root=dataset.parent
        for name,digest in policy['sources'].items():
            check(sha256(source_root/'sources'/name)==digest,'countability rule source changed: '+name)
        retrieval=read(source_root/'source-retrieval.json')
        check(sha256(source_root/'source-retrieval.json')==policy['source_retrieval_sha256'],
              'countability retrieval binding differs')
        for key,item in retrieval['records'].items():
            if item.get('retrieved'):
                check(sha256(source_root/'sources/selected-verbatim'/(key+'.json'))==item['sha256'],
                      'countability publisher record changed: '+key)
        for item in csv_rows(dataset/'countability-evidence.csv'):
            key=tuple(item[k] for k in ['representative_gbifid','source_name','source_taxon_id','date','latitude','longitude'])
            check(key not in countability_decisions,'duplicate countability evidence key')
            check(item['decision'] in {'supported','unresolved','excluded'},'invalid countability evidence decision')
            countability_decisions[key]=item['decision']
    check(r["config"] == c, "route configuration differs from frozen problem")
    check(
        r["year"] == c["year"] and r["synthetic"] == manifest["synthetic"],
        "year or synthetic label differs",
    )
    check(r.get("source_format", "EBD") == manifest.get("source_format", "EBD"),
          "source format label differs from frozen dataset")
    check(
        r["locations"] == locs and r["species"] == p["species"],
        "route metadata differs from frozen problem",
    )
    if p.get('compact_source'):
        from .compact_model import load

        source = p['compact_source']
        check(sha256(source['path']) == source['sha256'], 'complete compact source changed')
        model = load(source['path'])
        try:
            check(source['components'] == model.meta['components'], 'compact component binding differs')
            expected_config = dict(model.meta['config'])
            if p.get('flight_network'):
                expected_config['flights'] = True
            check(c == expected_config and p['species'] == model.meta['species'],
                  'selected catalog differs from complete search configuration/species')
            check(p['reduction'] == model.meta['reduction'], 'complete candidate retention differs')
            check(p['manifest_sha256'] == model.meta['manifest_sha256'], 'compact source dataset differs')
            indices = source['selected_locations']
            check((len(indices) <= len(locs) if p.get('flight_network') else len(indices) == len(locs)) and len(set(indices)) == len(indices),
                  'selected location binding is incomplete or duplicated')
            for i, original in enumerate(indices):
                check({k:v for k,v in locs[i].items() if k != 'road_access'} == model.locations[original],
                      'selected exact coordinates/metadata differ from full source catalog')
            mapping = {original:i for i,original in enumerate(indices)}
            check(len(source['selected_opportunities']) == len(p['opportunities']),
                  'selected opportunity binding incomplete')
            for i, original in enumerate(source['selected_opportunities']):
                opportunity = model.opportunity(original)
                if opportunity['location'] not in mapping:
                    check(False, 'selected opportunity location is absent from the bound complete model selection')
                    continue
                opportunity.update(id=i, location=mapping[opportunity['location']])
                check(p['opportunities'][i] == opportunity, 'selected opportunity differs from complete model')
            check(r.get('provenance', {}).get('compact_model_sha256') == source['sha256'],
                  'route lacks complete model identity')
            check(r.get('search', {}).get('selected_model_opportunities') == source['selected_opportunities'],
                  'native selected opportunity identity differs')
        finally:
            model.close()
    if p.get('joint_marine'):
        from .joint_validate import context
        joint_context=context(p,r,Path(problem_path).parent,check)
    if p.get('flight_network'):
        network = p['flight_network']
        folder = Path(network['path'])
        check(sha256(folder/'manifest.json') == network['manifest_sha256'], 'flight manifest changed')
        flight_manifest = read(folder/'manifest.json')
        check(sha256(folder/'airports.json') == flight_manifest['airports_sha256'], 'flight airports changed')
        check(sha256(folder/'flights.sqlite') == flight_manifest['database_sha256'], 'dated flight network changed')
        airports = {v['id']: v for v in read(folder/'airports.json')}
        for loc in locs[len(p['compact_source']['selected_locations']):]:
            if loc.get('kind')=='airport':
                check({k:v for k,v in loc.items() if k != 'road_access'} == airports.get(loc['id']), 'selected airport differs from frozen coordinates')
            elif loc.get('kind')=='island_landing':
                check(bool(joint_context) and locs.index(loc) in p['joint_marine'].get('landing_indices',{}).values(),
                      'unbound island landing display location')
            else:
                check(bool(joint_context) and loc.get('kind') in {'pelagic_observation','island_observation'} and
                      locs.index(loc) in p['joint_marine']['pin_indices'].values(),
                      'unbound extra routing/display location')
        with sqlite3.connect('file:' + str(folder/'flights.sqlite') + '?mode=ro&immutable=1', uri=True) as db:
            for flight in p['flight_links']:
                a, b = [locs[flight[k]] for k in ['from', 'to']]
                row = db.execute('SELECT minutes FROM flights WHERE origin=? AND destination=? AND day=?',
                                 [a['code'], b['code'], flight['service_day']]).fetchone()
                check(row is not None and abs(row[0]-flight['minutes']) < 1e-7, 'flight date/duration lacks original operated-service evidence')
        if not manifest['synthetic']:
            check(flight_manifest['year']==c['year'], 'flight source year differs from experiment')
            wanted = {}
            for flight in p['flight_links']:
                day = date(c['year'],1,1) + timedelta(days=flight['service_day'])
                key = (locs[flight['from']]['code'],locs[flight['to']]['code'],str(day))
                wanted.setdefault(day.month,{})[key] = flight['minutes']
            for month, expected in wanted.items():
                sources = [v for v in flight_manifest['sources'] if v['file'].endswith(f"{c['year']}_{month}.zip")]
                check(len(sources)==1, 'original monthly flight source is ambiguous')
                if len(sources)!=1:
                    continue
                path = folder/sources[0]['file']
                check(sha256(path)==sources[0]['sha256'], 'original monthly flight archive changed')
                flight_archives_checked.append(sources[0]['file'])
                totals = {key:[0.,0] for key in expected}
                with zipfile.ZipFile(path) as archive:
                    members = [name for name in archive.namelist() if name.lower().endswith('.csv')]
                    check(len(members)==1, 'original monthly flight CSV is ambiguous')
                    if len(members)!=1:
                        continue
                    with archive.open(members[0]) as raw, io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as text:
                        for row in csv.DictReader(text):
                            key = (row['Origin'],row['Dest'],row['FlightDate'][:10])
                            if key in totals and not float(row['Cancelled'] or 0) and not float(row['Diverted'] or 0) and row['ActualElapsedTime']:
                                minutes = float(row['ActualElapsedTime'])
                                if 0<minutes<=1260:
                                    totals[key][0] += minutes
                                    totals[key][1] += 1
                for key,(minutes,count) in totals.items():
                    check(count>0 and math.ceil(minutes/count)+180==expected[key],
                          'flight service/duration differs from original BTS flight records')
    if "provenance" in r:
        check(r["provenance"]["problem_sha256"] == sha256(problem_path), "problem hash mismatch")
        check(r["provenance"]["roads_sha256"] == sha256(roads_path), "roads hash mismatch")
    with closing(load_roads(p, roads_path)) as roads:
        mappings = {
            (m["source_name"], m.get("source_taxon_id", "") or ""): m
            for m in csv_rows(dataset / "taxonomy.csv")
        }
        with ManagedDuckDB() as con:
            con.execute(f"SET memory_limit={q(c['memory_limit'])}")
            con.execute(f"SET threads={int(c['threads'])}")
            con.execute("CREATE TABLE selected_stops(date DATE,location_id VARCHAR)")
            keys = sorted({(d["date"], locs[s["location"]]["id"]) for d in r["days"] for s in d["stops"]})
            if keys:
                con.executemany("INSERT INTO selected_stops VALUES (?,?)", keys)
            from .eod import SOURCE_FORMAT, attach_dataset, locality_id

            is_eod = manifest.get("source_format") == SOURCE_FORMAT
            is_ebd_parquet = manifest.get("source_format") == "EBD_PARQUET"
            scope_mask = None
            if is_eod and manifest.get("geographic_scope"):
                from .eod import polygon_mask

                scope_mask = polygon_mask(dataset / "scope.geojson")
                check(sha256(dataset / "scope.geojson") ==
                      manifest["geographic_scope"]["boundary_sha256"], "geographic boundary changed")
            evidence, _ = attach_dataset(con, dataset, manifest)
            con.execute(
                f"CREATE TABLE evidence AS SELECT e.* FROM {evidence} e JOIN selected_stops USING(date,location_id)"
            )
            con.execute("CREATE INDEX evidence_lookup ON evidence(date,location_id,species_id)")
            evidence_columns = {row[0] for row in con.execute("DESCRIBE evidence").fetchall()}
            taxon_column = "taxon_concept_id" if "taxon_concept_id" in evidence_columns else "NULL"
            verbatim_land = is_eod or is_ebd_parquet
            land_lat = "e.latitude_text" if verbatim_land else "e.lat"
            land_lon = "e.longitude_text" if verbatim_land else "e.lon"
            catalog_lat = "l.latitude" if verbatim_land else "try_cast(l.latitude AS DOUBLE)"
            catalog_lon = "l.longitude" if verbatim_land else "try_cast(l.longitude AS DOUBLE)"
            land = {
                (row[0], row[1], row[2]): row[3]
                for row in con.execute(f"""SELECT DISTINCT e.locality_id,{land_lat},{land_lon},l.on_land
                    FROM evidence e JOIN read_csv({q(dataset / "land.csv")},all_varchar=true) l
                    ON e.locality_id=l.locality_id AND {land_lat}={catalog_lat}
                    AND {land_lon}={catalog_lon}""").fetchall()
            }
            first_date = (
                date.fromisoformat(c["start_date"]) if c.get("start_date") else date(c["year"], 1, 1)
            )
            last_date = date.fromisoformat(c["end_date"]) if c.get("end_date") else date(c["year"], 12, 31)
            check(
                first_date.year == last_date.year == c["year"] and first_date <= last_date,
                "invalid experiment window",
            )
            n = (last_date - first_date).days + 1
            check(len(r["days"]) == n, "calendar must cover every experiment day exactly once")
            here = r["initial_location"]
            check(isinstance(here, int) and 0 <= here < len(locs), "invalid initial location")
            if c["start_location"]:
                check(locs[here]["id"] == c["start_location"], "fixed starting location violated")
            seen = set()
            total_miles = 0
            total_minutes = 0
            total_walking_minutes = total_walking_meters = 0.
            checked_evidence = 0
            end_positions = []
            for i, d in enumerate(r["days"]):
                prefix = d["date"]
                check(
                    d["date"] == str(first_date + timedelta(days=i)),
                    f"{prefix}: missing, repeated or out-of-order date",
                )
                mode = d["kind"]
                if mode in {'pelagic','island'} and joint_context is not None:
                    if mode=='island':
                        from .island_validate import audit_day
                    else:
                        from .joint_validate import audit_day
                    here,miles,minutes,wm,wd=audit_day(joint_context,d,here,p,r,roads,seen,check,
                        endpoint_pairs,endpoint_gaps)
                    total_miles+=miles;total_minutes+=minutes
                    total_walking_minutes+=wm;total_walking_meters+=wd
                    end_positions.append(here)
                    continue
                check(mode in {"rest", "birding", "road_transit", "flight"}, f"{prefix}: unknown day mode")
                if mode != "birding":
                    check(
                        not d["stops"] and not d["new_species"],
                        f"{prefix}: species credited on non-birding day",
                    )
                miles = minutes = 0
                for leg in d["legs"]:
                    a, b = leg["from"], leg["to"]
                    f0, f1 = leg["start_fraction"], leg["end_fraction"]
                    check(
                        all(
                            isinstance(x, (int, float)) and math.isfinite(x)
                            for x in [f0, f1, leg["miles"], leg["minutes"]]
                        ),
                        f"{prefix}: nonfinite leg",
                    )
                    check(0 <= f0 < f1 <= 1, f"{prefix}: invalid road progress")
                    if leg["mode"] == "road":
                        road = roads.get((a, b))
                        check(road is not None, f"{prefix}: road has no cached network evidence")
                        if road:
                            if not manifest['synthetic'] and (a, b) not in endpoint_pairs:
                                anchors = [[road_point(locs[i])['lon'], road_point(locs[i])['lat']]
                                           for i in (a, b)]
                                try:
                                    endpoint_gaps.extend(verify_evidence(road.get('endpoint_evidence'), anchors, road))
                                    check(r.get('provenance', {}).get('road_matching_policy') == road['endpoint_evidence']['policy'],
                                          f'{prefix}: route endpoint matching policy missing or changed')
                                    check(road['endpoint_evidence']['network_sha256'] ==
                                          r.get('provenance', {}).get('road_network_sha256'),
                                          f'{prefix}: endpoint evidence network differs from route')
                                    endpoint_pairs.add((a, b))
                                except (KeyError, TypeError, ValueError) as error:
                                    check(False, f'{prefix}: endpoint audit failed: {error}')
                            check(
                                abs(leg["miles"] - road["miles"] * (f1 - f0)) < 1e-5,
                                f"{prefix}: road mileage mismatch",
                            )
                            check(
                                abs(leg["minutes"] - road["minutes"] * (f1 - f0)) < 1e-5,
                                f"{prefix}: road time mismatch",
                            )
                            check(leg["geometry"] == road["geometry"], f"{prefix}: road geometry mismatch")
                            check(
                                leg["schematic"] == not_bool(road["geometry"]),
                                f"{prefix}: schematic label mismatch",
                            )
                        expected = a if f0 == 0 else (a, b, f0)
                        check(positions_equal(here, expected), f"{prefix}: discontinuous road position")
                        here = b if f1 == 1 else (a, b, f1)
                    elif leg["mode"] == "flight":
                        matches = [f for f in p["flight_links"] if f["from"] == a and f["to"] == b
                                   and ('service_day' not in f or f['service_day'] == i)]
                        check(
                            c["flights"] and mode == "flight" and bool(matches),
                            f"{prefix}: unauthorized airport connection",
                        )
                        check(here == a and f0 == 0 and f1 == 1, f"{prefix}: discontinuous flight")
                        check(
                            locs[a]["kind"] == locs[b]["kind"] == "airport",
                            f"{prefix}: flight endpoints must be airports",
                        )
                        check(
                            any(abs(f["minutes"] - leg["minutes"]) < 1e-5 for f in matches),
                            f"{prefix}: flight time mismatch",
                        )
                        check(0 < leg["minutes"] <= 1440, f"{prefix}: flight exceeds a calendar day")
                        check(
                            leg["miles"] == 0 and leg["schematic"],
                            f"{prefix}: flight display/accounting mismatch",
                        )
                        here = b
                    else:
                        check(False, f"{prefix}: unknown travel mode")
                    miles += leg["miles"]
                    minutes += leg["minutes"]
                if mode == "rest":
                    check(not d["legs"], f"{prefix}: travel on rest day")
                if mode in {"road_transit", "flight"}:
                    check(
                        len(d["legs"]) == 1, f"{prefix}: a transit day must contain exactly one modeled leg"
                    )
                    check(
                        not d["legs"]
                        or d["legs"][0]["mode"] == ("road" if mode == "road_transit" else "flight"),
                        f"{prefix}: transit mode mismatch",
                    )
                    if mode == "road_transit":
                        check(
                            minutes <= c["drive_minutes"] + 1e-5, f"{prefix}: driving-day budget exceeded"
                        )
                added = []
                walking_minutes = walking_meters = 0.
                if mode == "birding":
                    check(bool(d["stops"]), f"{prefix}: empty birding day")
                    if c.get('mixed_road_birding', False):
                        check(minutes <= c['drive_minutes'] + 1e-5,
                              f'{prefix}: mixed-day driving budget exceeded')
                    else:
                        check(miles <= c["local_miles"] + 1e-5, f"{prefix}: local road-mile budget exceeded")
                    check(
                        all(
                            x["mode"] == "road" and x["start_fraction"] == 0 and x["end_fraction"] == 1
                            for x in d["legs"]
                        ),
                        f"{prefix}: birding travel must be complete local roads",
                    )
                    # Recreate the exact local chain, including movement to the first stop.
                    previous_position = r["initial_location"] if i == 0 else end_positions[-1]
                    expected = []
                    for stop in d["stops"]:
                        dest = stop["location"]
                        if previous_position != dest:
                            expected.append((previous_position, dest))
                        previous_position = dest
                    check(
                        expected == [(x["from"], x["to"]) for x in d["legs"]],
                        f"{prefix}: stops do not follow local road chain",
                    )
                    check(here == previous_position, f"{prefix}: day does not end at last stop")
                    for stop in d["stops"]:
                        oi = stop["opportunity"]
                        check(
                            isinstance(oi, int) and 0 <= oi < len(p["opportunities"]),
                            f"{prefix}: invalid opportunity",
                        )
                        if not isinstance(oi, int) or not 0 <= oi < len(p["opportunities"]):
                            continue
                        opp = p["opportunities"][oi]
                        check(
                            opp["date"] == prefix and opp["location"] == stop["location"],
                            f"{prefix}: opportunity/date/location mismatch",
                        )
                        l = locs[stop["location"]]
                        if c.get('walking_speed_kmh') is not None:
                            anchor = road_point(l)
                            gap = gap_m([l['lon'], l['lat']], [anchor['lon'], anchor['lat']])
                            distance = 2*gap if gap > c.get('walking_threshold_m', 100) else 0.
                            walk = distance/(c['walking_speed_kmh']*1000)*60
                            for key,value in [('access_gap_m',gap), ('walking_meters',distance),
                                              ('walking_minutes',walk), ('birding_minutes',c['birding_minutes'])]:
                                check(abs(stop.get(key, math.inf)-value) < 1e-5,
                                      f'{prefix}: stop {key} differs from access walking audit')
                            walking_minutes += walk
                            walking_meters += distance
                        if scope_mask is not None:
                            from shapely.geometry import Point

                            check(scope_mask.covers(Point(l["lon"], l["lat"])),
                                  f"{prefix}: observation stop outside geographic boundary")
                        check(
                            (stop["lat"], stop["lon"], stop["name"]) == (l["lat"], l["lon"], l["name"]),
                            f"{prefix}: stop metadata mismatch",
                        )
                        expected_species = {
                            row[0]
                            for row in con.execute(
                                "SELECT DISTINCT species_id FROM evidence WHERE date=? AND location_id=?",
                                [prefix, l["id"]],
                            ).fetchall()
                        }
                        check(
                            set(stop["species"]) == expected_species,
                            f"{prefix}: species differ from independent location/date evidence",
                        )
                        check(
                            set(stop["species"]) == {p["species"][s]["id"] for s in opp["species"]},
                            f"{prefix}: frozen problem species mismatch",
                        )
                        new = []
                        for sp in stop["species"]:
                            extra = (",source_record_count,geospatial_flagged_records,geospatial_unknown_records,"
                                     "latitude_text,longitude_text") if is_eod else ""
                            if is_ebd_parquet:
                                extra = ",latitude_text,longitude_text"
                            rows = con.execute(
                                f"""SELECT checklist_id,event_id,record_id,source_name,approved,category,country,state,lat,lon,
                                locality_id,bird_count,exotic_code,{taxon_column}{extra} FROM evidence WHERE date=? AND location_id=? AND species_id=?""",
                                [prefix, l["id"], sp],
                            ).fetchall()
                            support = {(x[0], x[1], x[2], x[3]) for x in rows}
                            claimed = {
                                (e["checklist"], e["event"], e["record"], e["taxon"])
                                for e in stop["evidence"].get(sp, [])
                            }
                            check(
                                bool(claimed) and claimed == support,
                                f"{prefix}: missing or fabricated source checklists for {sp}",
                            )
                            for x in rows:
                                tax = mappings.get((x[3], x[13] or ""), mappings.get((x[3], ""), {}))
                                decision=countability_decisions.get((x[2][5:],x[3],x[13],prefix,x[17],x[18])) if is_eod else None
                                supported_override=decision=='supported' and tax.get('aba_eligible')!='0'
                                check(decision not in {'unresolved','excluded'},f'{prefix}: unresolved/excluded countability record credited')
                                if is_eod:
                                    check(
                                        x[0] == "" and x[4] is None and x[5] is None and x[12] is None
                                        and tax.get("source_taxon_id") == x[13]
                                        and (tax.get("eod_presence_eligible") == "1" or supported_override),
                                        f"{prefix}: EOD metadata policy missing or fabricated approval/checklist",
                                    )
                                    check(
                                        x[14] > 0 and x[15] == 0 and x[16] == 0
                                        and x[10] == locality_id(x[7], x[17], x[18])
                                        and x[2].startswith("GBIF:") and x[2][5:].isdigit()
                                        and x[1] == "gbif-group:" + x[2][5:],
                                        f"{prefix}: EOD presence/point/source evidence fails",
                                    )
                                else:
                                    check(
                                        x[4] in {"1", "true", "TRUE"}
                                        and (
                                            x[5] in {"species", "issf", "subspecies", "form"}
                                            or (
                                                x[5] == "domestic"
                                                and bool(x[13])
                                                and tax.get("source_taxon_id") == x[13]
                                            )
                                        ),
                                        f"{prefix}: unvalidated or unresolved taxon",
                                    )
                                check(
                                    x[6] == "US"
                                    and x[7] in STATES
                                    and (x[8], x[9]) == (l["lat"], l["lon"]),
                                    f"{prefix}: geographic eligibility mismatch",
                                )
                                check(
                                    land.get((x[10], x[17], x[18]) if is_eod else
                                             (x[10], x[14], x[15]) if is_ebd_parquet else
                                             (x[10], x[8], x[9])) == "1",
                                    f"{prefix}: land eligibility missing",
                                )
                                check(
                                    tax.get("species_id") == sp
                                    and (tax.get("aba_eligible") == "1" or supported_override)
                                    and (
                                        tax.get("eligible_states") == "*"
                                        or x[7] in tax.get("eligible_states", "").split("|")
                                    ),
                                    f"{prefix}: ABA taxonomy mapping fails",
                                )
                                if is_eod or is_ebd_parquet:
                                    from .countability import region_supported
                                    check(region_supported(tax,x[7],x[8],x[9],prefix),
                                          f'{prefix}: population/provenance envelope fails')
                                if not is_eod:
                                    exotic_ok = x[12] in {None, "", "N"}
                                    if is_ebd_parquet:
                                        exotic_ok |= x[12] == 'P' and tax.get('score_tier', '').startswith('provisional')
                                        check(tax.get('eod_presence_eligible') == '1',
                                              f'{prefix}: source-specific countability review unresolved')
                                    check(exotic_ok, f"{prefix}: exotic record requires review")
                                    try:
                                        present = x[11].upper() == "X" or float(x[11]) > 0
                                    except (ValueError, AttributeError):
                                        present = False
                                    check(present, f"{prefix}: not a presence record")
                                checked_evidence += 1
                            if sp not in seen:
                                seen.add(sp)
                                new.append(sp)
                                added.append(sp)
                        check(stop["new_species"] == new, f"{prefix}: stop new-species audit mismatch")
                activity = minutes + len(d["stops"]) * c["birding_minutes"] + walking_minutes
                if c.get('walking_speed_kmh') is not None:
                    check(abs(d.get('walking_minutes', 0)-walking_minutes) < 1e-5
                          and abs(d.get('walking_meters', 0)-walking_meters) < 1e-5,
                          f'{prefix}: daily access walking totals mismatch')
                check(
                    abs(d["road_miles"] - miles) < 1e-5 and abs(d["travel_minutes"] - minutes) < 1e-5,
                    f"{prefix}: daily travel totals mismatch",
                )
                check(abs(d["activity_minutes"] - activity) < 1e-5, f"{prefix}: activity total mismatch")
                if mode == "birding":
                    check(activity <= c["activity_minutes"] + 1e-5, f"{prefix}: activity budget exceeded")
                check(
                    d["new_species"] == added and d["cumulative_species"] == len(seen),
                    f"{prefix}: cumulative credit mismatch",
                )
                total_miles += miles
                total_minutes += minutes
                total_walking_minutes += walking_minutes
                total_walking_meters += walking_meters
                end_positions.append(here)
            check(r["species_total"] == len(seen), "distinct-species total mismatch")
            joint_sources=None
            if joint_context is not None:
                check(joint_context['boat_days']==r['marine_extension']['boat_days']==len(p['joint_marine']['selected_trips']),
                      'joint charter count differs')
                check(abs(r['search']['travel_minutes']-total_minutes)<1e-5 and
                      abs(r['search']['road_miles']-total_miles)<1e-5,'joint native/calendar travel totals differ')
                check({s['opportunity'] for d in r['days'] for s in d['stops']}==set(range(len(p['opportunities']))),
                      'joint selected opportunities not all represented in calendar')
                if not errors:
                    from .joint_validate import audit_sources
                    joint_sources=audit_sources(joint_context,p,r,check)
            con.close()
            return {
                "valid": not errors,
                "errors": errors,
                "species_total": len(seen),
                "days_checked": len(r["days"]),
                "evidence_rows_checked": checked_evidence,
                "road_miles": total_miles,
                "travel_minutes": total_minutes,
                'walking': {'enabled': c.get('walking_speed_kmh') is not None,
                            'speed_kmh': c.get('walking_speed_kmh'),
                            'threshold_m': c.get('walking_threshold_m', 100),
                            'round_trip': True, 'minutes': total_walking_minutes,
                            'meters': total_walking_meters,
                            'distance_model': 'straight-line source pin to road anchor, full distance if above threshold'},
                "endpoint_matching": {'applicable': not manifest['synthetic'],
                                      'policy': r.get('provenance',{}).get('road_matching_policy',POLICY), 'pairs_checked': len(endpoint_pairs),
                                      'max_gap_m': max(endpoint_gaps, default=0)},
                'flight_service': {'enabled': bool(p.get('flight_network')),
                                   'links_checked': len(p.get('flight_links',[])),
                                   'original_monthly_archives_checked': flight_archives_checked},
                'countability': {'enabled':bool(manifest.get('countability')),
                                 'policy':manifest.get('countability',{}).get('policy'),
                                 'evidence_decisions_bound':len(countability_decisions),
                                 'unresolved_or_excluded_credit_allowed':False,
                                 'individual_wild_status_certified':False},
                **({'route_sha256':sha256(route_path),'joint_transport': {'native_joint_search':True,'boat_days_checked':joint_context['boat_days'],
                    'max_boat_day_activity_minutes':joint_context['max_activity'],'publisher_sources':joint_sources,
                    'boat_only_species_holds':sorted(joint_context['held']),
                    'terrestrial_access_walks':joint_context['terrestrial_access_walks'],
                    'historical_charter_operation_certified':False}} if joint_context is not None else {}),
                "scope": "calendar continuity, cached road costs and returned driving endpoints, budgets, original eligible evidence and frozen taxonomy/land catalogs; physical access from road anchors to observation pins is not certified",
            }


def positions_equal(a, b):
    if isinstance(a, tuple) and isinstance(b, tuple):
        return a[:2] == b[:2] and abs(a[2] - b[2]) < 1e-8
    return a == b


def not_bool(value):
    return not bool(value)
