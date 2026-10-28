"""Geographic scoring envelopes, without changing source coordinate strings.

Envelopes operationalize documented populations; they are modeling assumptions,
not official ABA population boundaries or certification of individual wildness.
"""

import json


def region_supported(taxon, state, lat, lon, date):
    policy = taxon.get('eligible_regions')
    if not policy:
        return True
    return any(
        r['state'] == state and r['min_lat'] <= lat <= r['max_lat']
        and r['min_lon'] <= lon <= r['max_lon']
        and r['first'] <= str(date) <= r['last']
        for r in json.loads(policy)
    )


REGION_SCHEMA = '[{"state":"VARCHAR","min_lat":"DOUBLE","max_lat":"DOUBLE",'
REGION_SCHEMA += '"min_lon":"DOUBLE","max_lon":"DOUBLE","first":"VARCHAR","last":"VARCHAR"}]'


def region_sql(alias='t', row='r'):
    # A scalar list predicate neither duplicates observations for overlapping
    # envelopes nor creates a correlated join over the national observation set.
    return f"""(coalesce({alias}.eligible_regions,'')='' OR len(list_filter(
        from_json(coalesce(nullif({alias}.eligible_regions,''),'[]'),
        '{REGION_SCHEMA}'), rule -> rule.state={row}.state
        AND {row}.lat BETWEEN rule.min_lat AND rule.max_lat
        AND {row}.lon BETWEEN rule.min_lon AND rule.max_lon
        AND {row}.date BETWEEN try_cast(rule.first AS DATE) AND try_cast(rule.last AS DATE)))>0)"""
