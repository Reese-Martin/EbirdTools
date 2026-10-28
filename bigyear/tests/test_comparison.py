from dataclasses import replace

import pytest

from bigyear_planner.config import Config
from bigyear_planner.demo import demo
from bigyear_planner.compare import compare
from bigyear_planner.util import read, dump


def test_comparison_keeps_coverage_and_rejects_rule_drift(tmp_path):
    demo(tmp_path / "demo", seconds=0.05, threads=1)
    dirs = [tmp_path / "demo" / "run-2024", tmp_path / "demo" / "run-2023"]
    result = compare(dirs, tmp_path / "comparison.json")
    assert len(result["years"]) == 2
    assert all(y["synthetic"] for y in result["years"])
    assert all(y["observation_coverage"]["coverage_by_month"] for y in result["years"])
    assert result["years"][0]["destination_prevalence"]
    p = read(dirs[1] / "problem.json")
    p["config"]["local_miles"] = 31
    dump(dirs[1] / "problem.json", p)
    with pytest.raises(ValueError, match="matching"):
        compare(dirs, tmp_path / "bad.json")


def test_config_rejects_unknown_keys_and_invalid_limits(tmp_path):
    dump(tmp_path / "unknown.json", {"unexpected": 1})
    with pytest.raises(ValueError, match="unknown"):
        Config.read(tmp_path / "unknown.json")
    for kwargs in [
        {"threads": 0},
        {"local_miles": float("nan")},
        {"birding_minutes": 481},
        {"drive_minutes": 1441},
        {"year": 2024.5},
    ]:
        with pytest.raises(ValueError):
            replace(Config(), **kwargs)


def test_comparison_rejects_different_observation_polygons(tmp_path):
    demo(tmp_path / "demo", seconds=.05, threads=1)
    directories = [tmp_path / "demo" / "run-2024", tmp_path / "demo" / "run-2023"]
    for i, directory in enumerate(directories):
        problem = read(directory / "problem.json")
        problem["manifest"]["geographic_scope"] = {
            "boundary_sha256": f"different-county-{i}",
            "boundary_rule": "covers exact point", "travel_rule": "stops inside polygon"}
        dump(directory / "problem.json", problem)
    with pytest.raises(ValueError, match="matching"):
        compare(directories, tmp_path / "comparison.json")
