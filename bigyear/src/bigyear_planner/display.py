"""Verified road geometry for display without rewriting frozen search artifacts."""

import hashlib
import csv
import json
import math
from pathlib import Path

from .travel import load_roads, road_point
from .road_matching import POLICY, check_endpoints, evidence, request_route, verify_evidence
from .util import dump, read, sha256


DISPLAY_FILE = "road-display-geometries.json"


def attach_scoring_tiers(path,route):
    """Derive display totals from the exact frozen taxonomy, never relabel a score."""
    problem=Path(path).parent/'problem.json'
    try:
        if sha256(problem)!=route['provenance']['problem_sha256']:return route
        p=read(problem);dataset=Path(p['dataset']);manifest=read(dataset/'manifest.json')
        if sha256(dataset/'manifest.json')!=route['provenance']['dataset_manifest_sha256']:return route
        if sha256(dataset/'taxonomy.csv')!=manifest['inputs']['taxonomy']['sha256']:return route
        with (dataset/'taxonomy.csv').open() as stream:
            reader=csv.DictReader(stream)
            if 'score_tier' not in reader.fieldnames:return route
            tiers={r['species_id']:r['score_tier'] for r in reader if r['score_tier']!='held'}
        seen={s for day in route['days'] for stop in day['stops'] for s in stop['species']}
        if len(seen)!=route['species_total'] or not seen<=tiers.keys():return route
        provisional=sorted(s for s in seen if tiers[s].startswith('provisional'))
        route['scoring_tiers']={'primary':len(seen)-len(provisional),'provisional':len(provisional),
            'provisional_species_ids':provisional,'taxonomy_sha256':manifest['inputs']['taxonomy']['sha256']}
    except (OSError,ValueError,TypeError,KeyError):
        pass
    return route


def attach_count_bound(root, route):
    """Display a separate analysis certificate only for its exact frozen inputs."""
    path = Path(root) / 'count-bound.json'
    if not path.exists():
        return route
    try:
        proof = read(path)
        bound = proof['upper_bound']
        provenance = route.get('provenance', {})
        if (proof.get('valid') is True and type(bound) is int
                and proof['problem_sha256'] == provenance.get('problem_sha256')
                and proof['roads_manifest_sha256'] == provenance.get('roads_sha256')
                and route['species_total'] <= bound <= route['annual_recorded_species_ceiling']
                and proof['eligible_species_ceiling'] == route['annual_recorded_species_ceiling']):
            route['analytical_count_bound'] = {'upper_bound': bound, 'scope': proof['scope'],
                                              'certificate': '/count-bound.json'}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return route


def valid_geometry(geometry):
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        return False
    points = geometry.get("coordinates")
    return (isinstance(points, list) and len(points) >= 2
            and all(isinstance(p, list) and len(p) == 2
                    and all(isinstance(v, (int, float)) and math.isfinite(v) for v in p)
                    and -180 <= p[0] <= 180 and -90 <= p[1] <= 90 for p in points))


def build_display_roads(root, endpoint, network_release):
    from .server import route_entries

    root = Path(root).resolve()
    bindings, shapes, verified_caches = {}, {}, {}
    for entry in route_entries(root):
        path = root / entry["path"]
        route = read(path)
        roads_path = path.parent / route.get("provenance", {}).get("roads_file", "roads.sqlite")
        if roads_path not in verified_caches:
            verified_caches[roads_path] = sha256(roads_path)
        digest = verified_caches[roads_path]
        if digest != route["provenance"]["roads_sha256"]:
            raise ValueError("road snapshot changed before display geometry preparation")
        binding = {"route_sha256": sha256(path), "roads_sha256": digest, "legs": {}}
        roads = load_roads({"locations": route["locations"], "manifest": {"synthetic": route["synthetic"]}}, roads_path)
        try:
            for day in route["days"]:
                for leg in day["legs"]:
                    if leg["mode"] != "road" or leg.get("geometry"):
                        continue
                    a, b = [route["locations"][leg[k]] for k in ("from", "to")]
                    road = roads.get((leg['from'], leg['to']))
                    cached = (road['miles'], road['minutes'], road['network_release']) if road else None
                    if cached is None or cached[2] != network_release:
                        raise ValueError("display geometry must use the frozen road network release")
                    origin, destination = road_point(a), road_point(b)
                    key_data = [a["id"], origin["lat"], origin["lon"], b["id"],
                                destination["lat"], destination["lon"], *cached]
                    key = hashlib.sha256(json.dumps(key_data, separators=(",", ":")).encode()).hexdigest()
                    if key not in shapes:
                        anchors = [[p['lon'], p['lat']] for p in (origin, destination)]
                        proof = road.get('endpoint_evidence')
                        if proof:
                            verify_evidence(proof, anchors, road)
                        else:
                            result = request_route(endpoint, anchors, geometry=True)
                            if result.get('code') != 'Ok':
                                raise ValueError('no measured driving route available at the requested endpoints')
                            # Legacy snapshots can gain display evidence, never retroactive search validity.
                            proof = evidence(result, anchors, 'legacy-display-only')
                        measured = proof["routes"][0]
                        dm = measured["distance"] - cached[0] * 1609.344
                        ds = measured["duration"] - cached[1] * 60
                        if (not all(math.isfinite(v) for v in (dm, ds))
                                or abs(dm) > .11 or abs(ds) > .11
                                or not valid_geometry(measured["geometry"])):
                            raise ValueError("road geometry differs from frozen driving costs")
                        shapes[key] = {"geometry": measured["geometry"],
                                       "miles": cached[0], "minutes": cached[1],
                                       "distance_difference_m": dm, "duration_difference_s": ds,
                                       "network_release": network_release,
                                       "matching_policy": POLICY, 'waypoints': proof['waypoints'],
                                       "vehicle_ferry_segments": proof.get('vehicle_ferry_segments', [])}
                    binding["legs"][f"{leg['from']}:{leg['to']}"] = key
        finally:
            roads.close()
        bindings[entry["path"]] = binding
    result = {"schema": 1, "routes": bindings, "shapes": shapes,
              "source": endpoint, "network_release": network_release,
              "purpose": "Display only; original route, costs, source coordinates and provenance stay frozen"}
    dump(root / DISPLAY_FILE, result)
    return {"routes": len(bindings), "unique_driving_shapes": len(shapes),
            "max_distance_difference_m": max((abs(s["distance_difference_m"]) for s in shapes.values()), default=0),
            "output": str(root / DISPLAY_FILE)}


def display_route(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    route = read(path)
    supplement = root / DISPLAY_FILE
    if not supplement.exists():
        return route
    try:
        data = read(supplement)
        binding = data["routes"].get(str(path.relative_to(root)), {})
        if (data["schema"] != 1 or binding.get("route_sha256") != sha256(path)
                or binding.get("roads_sha256") != route.get("provenance", {}).get("roads_sha256")):
            return route
        attached = 0
        for day in route["days"]:
            for leg in day["legs"]:
                if leg["mode"] != "road" or leg.get("geometry"):
                    continue
                key = binding["legs"].get(f"{leg['from']}:{leg['to']}")
                shape = data["shapes"].get(key)
                fraction = leg.get("end_fraction", 1) - leg.get("start_fraction", 0)
                if (not shape or not valid_geometry(shape.get("geometry"))
                        or shape.get('matching_policy') != POLICY
                        or not 0 < fraction <= 1
                        or abs(shape["miles"] * fraction - leg["miles"]) > 1e-7
                        or abs(shape["minutes"] * fraction - leg["minutes"]) > 1e-7):
                    continue
                anchors = [[road_point(route['locations'][leg[k]])['lon'],
                            road_point(route['locations'][leg[k]])['lat']] for k in ('from', 'to')]
                check_endpoints({'waypoints': shape['waypoints'],
                                 'routes': [{'geometry': shape['geometry']}]}, anchors, geometry=True)
                leg["geometry"] = shape["geometry"]
                leg["schematic"] = False
                if shape.get("vehicle_ferry_segments"):
                    leg["vehicle_ferry_segments"] = shape["vehicle_ferry_segments"]
                attached += 1
        if attached:
            route["display_geometry"] = {"verified_road_legs": attached,
                "original_route_sha256": binding["route_sha256"],
                "network_release": data["network_release"],
                "note": ("Road lines follow the checked car network; identified vehicle ferries are labeled separately. Observation pins retain their original coordinates. "
                         + ("Round-trip access walking is added to the daily activity budget; footpaths and legal access are assumed."
                            if route.get('config', {}).get('walking_speed_kmh') is not None else
                            "Walking to them is part of the stop allowance."))}
        return route
    except (OSError, ValueError, TypeError, KeyError):
        # A missing/stale optional display supplement must not hide saved routes.
        return read(path)
