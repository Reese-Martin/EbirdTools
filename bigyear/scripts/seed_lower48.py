"""Map a verified regional route into the complete national model as a seed."""

import argparse
import bisect
from pathlib import Path
import json

from bigyear_planner.compact_model import load
from bigyear_planner.util import ROOT, read, dump, sha256

DATA = ROOT / "data/lower48-eod-2024"
OUT = ROOT / "runs/lower48-eod-2024"
SOURCE = ROOT / "runs/west-coast-walking-2p2-2024/mmap-600s/route.json"


def main(source_route=SOURCE, output=OUT / "west-derived-seed.json"):
    source_route = Path(source_route).resolve()
    if not read(source_route.parent / "validation.json")["valid"]:
        raise ValueError("regional source is not verified")
    previous = read(source_route)
    if previous["config"]["walking_speed_kmh"] != 2.2:
        raise ValueError("source walking rule differs")
    stops = [
        (d["date"], previous["locations"][s["location"]])
        for d in previous["days"]
        for s in d["stops"]
    ]
    del previous
    model = load(DATA / "model/model.json")
    try:
        mapped = []
        held = []
        days = model.records["day"]
        from datetime import date

        for day, source in stops:
            lo, hi = 0, len(model.locations)
            while lo < hi:
                mid = (lo + hi) // 2
                if model.locations[mid]["id"] < source["id"]:
                    lo = mid + 1
                else:
                    hi = mid
            if lo >= len(model.locations) or model.locations[lo]["id"] != source["id"]:
                held.append(
                    {"date": day, "id": source["id"], "reason": "absent national source locality"}
                )
                continue
            loc = model.locations[lo]
            if (loc["lat"], loc["lon"]) != (source["lat"], source["lon"]):
                raise ValueError("source coordinates differ")
            ordinal = (date.fromisoformat(day) - date(2024, 1, 1)).days
            first = bisect.bisect_left(days, ordinal)
            last = bisect.bisect_right(days, ordinal)
            locations = model.records["location"][first:last]
            at = first + bisect.bisect_left(locations, lo)
            if at >= last or int(model.records[at]["location"]) != lo:
                held.append(
                    {"date": day, "id": source["id"], "reason": "absent national observation date"}
                )
                continue
            mapped.append(at)
        # Prioritize source opportunities that add the most as-yet-unrepresented
        # species, across the whole year. This is only a seed insertion order;
        # the full national candidate model and objective are unchanged.
        sets = {i: set(model.opportunity(i)["species"]) for i in mapped}
        remaining = set(mapped)
        seen = set()
        order = []
        while remaining:
            chosen = max(remaining, key=lambda i: (len(sets[i] - seen), len(sets[i]), -i))
            order.append(chosen)
            seen |= sets[chosen]
            remaining.remove(chosen)
        seed = {
            "model_sha256": sha256(model.path),
            "source_route": str(source_route),
            "source_route_sha256": sha256(source_route),
            "ids": order,
            "source_stops": len(stops),
            "mapped_stops": len(mapped),
            "held": held,
            "source_species_covered": len(seen),
            "method": "Greedy species-cover insertion order from a verified West Coast route. Native engine must recheck every stop/road/walk against the complete national model; no old costs reused. All national opportunities remain available.",
        }
        dump(output, seed)
        print(
            json.dumps(
                {
                    k: seed[k]
                    for k in ["source_stops", "mapped_stops", "held", "source_species_covered"]
                }
            )
        )
    finally:
        model.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUT / "west-derived-seed.json")
    args = parser.parse_args()
    main(args.source, args.output)
