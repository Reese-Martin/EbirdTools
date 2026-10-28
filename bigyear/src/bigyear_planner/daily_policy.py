"""Create a separately bound daily-policy scenario without copying observations."""

from copy import deepcopy
import os
from pathlib import Path

from .compact_model import load
from .config import Config
from .util import dump, read, sha256


def prepare_mixed_activity(source, output, driving_minutes=720):
    source = Path(source).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('daily-policy output already exists; preserve it and choose a new directory')
    if driving_minutes not in (480, 720):
        raise ValueError('use either the historical 8h control or the shared 12h activity cap')
    original = load(source)
    try:
        config = Config(**original.meta['config'])
        if (config.activity_minutes, config.drive_minutes, config.birding_minutes,
                config.walking_speed_kmh, config.walking_threshold_m) != (720, 480, 120, 2.2, 100):
            raise ValueError('mixed scenario requires the frozen 12h/8h/2h/2.2-km/h baseline')
        if config.mixed_road_birding or original.meta.get('calendar_source'):
            raise ValueError('derive the mixed scenario directly from the original legacy calendar')
        metadata = deepcopy(original.meta)
        metadata['config']['mixed_road_birding'] = True
        metadata['config']['drive_minutes'] = driving_minutes
        metadata['calendar_source'] = {
            'path': str(source), 'sha256': sha256(source),
            'reason': f'Remove the ordinary birding-day road-mile cap; share 12 activity hours between measured driving, birding and walking, with at most {driving_minutes/60:g} driving hours. Flight and boat access policies unchanged.',
        }
        output.mkdir(parents=True)
        for name in metadata['components']:
            # Hard links preserve exact bytes and allocate no second copy of
            # the complete source-point/dated-opportunity catalog.
            os.link(original.root / name, output / name)
        dump(output / 'model.json', metadata)
        derived = load(output / 'model.json')
        derived.close()
        dump(output / 'scenario.json', {
            'model_sha256': sha256(output / 'model.json'),
            'parent_sha256': sha256(source),
            'config_changes': {k: {'before': original.meta['config'].get(k, False), 'after': metadata['config'][k]}
                               for k in ['mixed_road_birding', 'drive_minutes']
                               if original.meta['config'].get(k, False) != metadata['config'][k]},
            'all_observations_retained': True, 'exact_coordinates': True,
            'components': metadata['components'], 'component_storage': 'hard links to frozen inputs',
            'benchmark_scope': '2024 lower 48 and DC; a full-ABA-area total is a different geographic benchmark',
            'score': 'unique countable species first; travel time then road miles break ties',
            'calendar_rearrangement': 'recorded-date moves, stop replacement and regional/calendar-window reconstruction remain enabled; no existing visit is required by default',
        })
        return read(output / 'scenario.json')
    finally:
        original.close()
