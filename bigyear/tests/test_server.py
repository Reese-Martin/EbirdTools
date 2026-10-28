from bigyear_planner.server import route_entries
from bigyear_planner.util import dump


def test_route_catalog_survives_audit_reports_and_incomplete_runs(tmp_path):
    route = {"year": 2024, "species_total": 224, "synthetic": False, "days": []}
    folder = tmp_path / "seed-1"
    dump(folder / "route.json", route)
    dump(folder / "alternative-1.json", route)
    dump(folder / "alternative-12.json", route)
    # The real regression: an array report sharing the old alternative-*.json glob.
    dump(folder / "alternative-validation.json", [{"audit": {"valid": True}}])
    dump(folder / "alternative-summary.json", {"year": 2024})
    (folder / "alternative-2.json").write_text('{"year":')
    dump(folder / "alternative-3.json", [])
    dump(tmp_path / "unfinished/route.json", {"year": 2024})
    dump(tmp_path / "another-run/route.json", {**route, "year": 2023})

    entries = route_entries(tmp_path)
    assert {entry["path"] for entry in entries} == {
        "seed-1/route.json", "seed-1/alternative-1.json", "seed-1/alternative-12.json",
        "another-run/route.json"}
    assert next(e for e in entries if e["path"] == "seed-1/alternative-1.json") == {
        "path": "seed-1/alternative-1.json", "year": 2024, "species": 224,
        "synthetic": False, "label": "seed-1 / alternative-1"}
