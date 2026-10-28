"""Explicit, round-trip access walking separate from on-site birding time."""

from .road_matching import gap_m


def access_walk(location, config):
    speed = config.get('walking_speed_kmh')
    anchor = location.get('road_access', location)
    gap = gap_m([location['lon'], location['lat']], [anchor['lon'], anchor['lat']])
    distance = 2*gap if speed is not None and gap > config.get('walking_threshold_m', 100) else 0.
    return {'access_gap_m': gap, 'walking_meters': distance,
            'walking_minutes': distance/(speed*1000)*60 if distance else 0.}


def stop_minutes(location, config):
    return config['birding_minutes'] + access_walk(location, config)['walking_minutes']
