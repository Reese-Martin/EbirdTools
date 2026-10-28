"""Audit ocean crossings separately from the slow-birding walking allowance.

Coastline evidence can disprove an assumed straight-line land walk. It does not
prove public foot access, terrain passability, tides, ferry operation or seating.
"""
from datetime import datetime
import math

from .road_matching import gap_m
from .config import Config


def open_water_meters(line,land):
    water=line.difference(land)
    pieces=[water] if water.geom_type=='LineString' else list(getattr(water,'geoms',[]))
    return sum(gap_m(list(a),list(b)) for piece in pieces if piece.geom_type=='LineString'
               for a,b in zip(piece.coords,list(piece.coords)[1:]))


def audit_walk(location,config,land):
    from shapely.geometry import LineString
    access=location.get('road_access',location)
    a=[location['lon'],location['lat']];b=[access['lon'],access['lat']]
    gap=gap_m(a,b)
    water=open_water_meters(LineString([a,b]),land) if gap>config['walking_threshold_m'] else 0.
    return {'gap_meters':gap,'open_water_meters':water,
      'walking_minutes':2*gap/(config['walking_speed_kmh']*1000)*60 if gap>config['walking_threshold_m'] else 0.,
      'status':'marine_access_required' if water>100 else 'no_large_ocean_crossing_detected',
      'access_certified':False}


def validate_boat_trip(trip,config):
    """Require complete dated evidence before accepting a marine trip block.

    This checks a supplied access plan. It is not a router or an assertion that
    every bird in a geographic radius can be observed on the vessel's track.
    """
    for field in ['id','date','departure_port','arrival_port','departure','arrival',
                  'service_reference','operated_evidence','exact_observations']:
        if not trip.get(field):raise ValueError('boat trip missing '+field)
    depart=datetime.fromisoformat(trip['departure']);arrive=datetime.fromisoformat(trip['arrival'])
    cfg=Config(**config)
    if not cfg.first_date<=depart.date()<=cfg.last_date:
        raise ValueError('boat trip lies outside the experiment calendar')
    if depart.tzinfo is None or arrive.tzinfo is None or arrive<=depart:
        raise ValueError('boat times need explicit offsets and positive duration')
    if str(depart.date())!=trip['date'] or str(arrive.date())!=trip['date']:
        raise ValueError('multi-day boats require explicit daily blocks')
    sailing=(arrive-depart).total_seconds()/60
    birding=float(trip.get('separate_birding_minutes',0))
    walk=float(trip.get('foot_roundtrip_meters',0))/(config['walking_speed_kmh']*1000)*60
    port_minutes=float(trip.get('port_access_minutes',0))
    if any(not math.isfinite(v) or v<0 for v in [sailing,birding,walk,port_minutes]):
        raise ValueError('invalid marine access duration')
    activity=sailing+birding+walk+port_minutes
    if activity>config['activity_minutes']:raise ValueError('full boat trip exceeds daily activity budget')
    if trip.get('private_land_required') and not trip.get('permission_reference'):
        raise ValueError('private island access requires permission evidence')
    for o in trip['exact_observations']:
        if o.get('date')!=trip['date'] or not o.get('source_record') or not o.get('access_reference'):
            raise ValueError('each exact observation needs dated source and vessel/foot access evidence')
        if not all(math.isfinite(float(o[k])) for k in ['latitude','longitude']):
            raise ValueError('marine observation needs exact coordinates')
        if o.get('jurisdiction_status')!='verified_lower48_waters':
            raise ValueError('nearest-land/200-mile jurisdiction remains unresolved')
    return {'id':trip['id'],'feasible':True,'activity_minutes':activity,'sailing_minutes':sailing,
      'walking_minutes':walk,'observations':len(trip['exact_observations']),
      'search_integration':'validated access block only; native boat-mode integration still required'}
