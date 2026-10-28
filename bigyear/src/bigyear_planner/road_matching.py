"""Strict OSRM anchor matching and portable selected-route endpoint evidence."""

import json
import math
import urllib.error
import urllib.request

POLICY = {'id': 'osrm-road-anchor-1m-v1', 'radius_m': 1,
          'endpoint_tolerance_m': 1.2, 'snapping': 'default',
          'note': '1 m search radius; 0.2 m allowance for returned coordinate rounding'}


BROUTER_POLICY = {'id':'brouter-car-anchor-1m-v1','radius_m':1,'endpoint_tolerance_m':1.2,
                  'snapping':'fixed car-profile waypoint matcher; dynamic range and correction disabled',
                  'note':'Validate actual full-path endpoints, all coordinates and measured costs independently'}


def gap_m(a, b):
    """Distance between two GeoJSON longitude/latitude coordinates."""
    for point in (a, b):
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)
                or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
            raise ValueError('invalid road endpoint coordinate')
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    value = (math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)
             * math.sin(math.radians(b[0]-a[0])/2)**2)
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(value)))


def check_endpoints(data, anchors, geometry=False, policy=POLICY):
    """Recompute endpoint gaps; never trust a provider's reported distance field."""
    waypoints = data.get('waypoints')
    if not isinstance(waypoints, list) or len(waypoints) != 2:
        raise ValueError('road response lacks two matched waypoints')
    gaps = [gap_m(a, w['location']) for a, w in zip(anchors, waypoints)]
    if geometry:
        shape = data['routes'][0]['geometry']
        if (shape.get('type') != 'LineString' or len(shape.get('coordinates', [])) < 2):
            raise ValueError('road response lacks full LineString geometry')
        points = shape['coordinates']
        for point in points:
            gap_m(point, point)  # Check every vertex, including nonfinite values.
        gaps.extend(gap_m(a, b) for a, b in zip(anchors, (points[0], points[-1])))
    if max(gaps) > policy['endpoint_tolerance_m']:
        raise ValueError(f'road endpoint mismatch: {max(gaps):.3f} m exceeds strict anchor tolerance')
    return gaps


def request_route(endpoint, anchors, geometry=False):
    coords = ';'.join(f'{lon},{lat}' for lon, lat in anchors)
    options = ('overview=full&geometries=geojson&steps=true' if geometry
               else 'overview=false&steps=false')
    url = (endpoint.rstrip('/') + '/route/v1/driving/' + coords + '?' + options
           + '&radiuses=1;1&snapping=default&generate_hints=false')
    try:
        with urllib.request.urlopen(url, timeout=45) as stream:
            data = json.load(stream)
    except urllib.error.HTTPError as error:
        if error.code != 400:
            raise
        data = json.load(error)
        if data.get('code') not in {'NoRoute', 'NoSegment'}:
            raise
    if data.get('code') in {'NoRoute', 'NoSegment'}:
        return data
    if data.get('code') != 'Ok' or not data.get('routes'):
        raise ValueError('road provider did not return a measured route: ' + str(data.get('code')))
    route = data['routes'][0]
    if not all(type(route.get(k)) in (int, float) and math.isfinite(route[k]) and route[k] >= 0
               for k in ('distance', 'duration')):
        raise ValueError('invalid measured road cost')
    check_endpoints(data, anchors, geometry)
    return data


def evidence(data, anchors, network_sha256, policy=POLICY):
    check_endpoints(data, anchors, geometry=True, policy=policy)
    route = data['routes'][0]
    return {'policy': policy, 'anchors': anchors, 'network_sha256': network_sha256,
            'waypoints': data['waypoints'],
            'routes': [{k: route[k] for k in ('distance', 'duration', 'geometry')}],
            'vehicle_ferry_segments': [
                {'name': step.get('name', 'Vehicle ferry'), 'minutes': step['duration']/60,
                 'miles': step['distance']/1609.344}
                for leg in route.get('legs', []) for step in leg.get('steps', [])
                if step.get('mode') == 'ferry']}


def verify_evidence(proof, anchors, road):
    if (not proof or proof.get('policy') not in (POLICY, BROUTER_POLICY) or proof.get('anchors') != anchors
            or not proof.get('network_sha256')):
        raise ValueError('road lacks current endpoint matching evidence; rebuild affected costs')
    gaps = check_endpoints(proof, anchors, geometry=True, policy=proof['policy'])
    measured = proof['routes'][0]
    if (abs(measured['distance'] - road['miles']*1609.344) > .11
            or abs(measured['duration'] - road['minutes']*60) > .11
            or not all(math.isfinite(measured[k]) for k in ('distance', 'duration'))):
        raise ValueError('endpoint evidence differs from frozen driving costs')
    return gaps
