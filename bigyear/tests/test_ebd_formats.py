import csv

import duckdb
import pytest

from bigyear_planner.config import Config
from bigyear_planner.demo import fixtures, synthetic_roads
from bigyear_planner.ingest import prepare
from bigyear_planner.problem import build_problem
from bigyear_planner.runner import run
from bigyear_planner.util import read, write_csv


@pytest.mark.parametrize("qualified", [False, True])
def test_domestic_requires_exact_taxon_mapping_and_still_rejects_escapees(tmp_path, qualified):
    raw = fixtures(tmp_path / "raw", years=(2024,), small=True)
    ebd = raw / "ebd-2024.tsv"
    with ebd.open(newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
        names = list(reader.fieldnames)
    names += [
        "TAXON CONCEPT ID",
        "SUBSPECIES SCIENTIFIC NAME",
        "OBSERVATION TYPE",
        "PROTOCOL NAME",
        "PROTOCOL CODE",
        "COUNTY CODE",
        "DURATION MINUTES",
        "EFFORT DISTANCE KM",
        "REVIEWED",
    ]
    for suffix, taxon_id, exotic in [
        ("feral", "avibase-533DD36C", "N"),
        ("escapee", "avibase-533DD36C", "X"),
        ("unmapped-form", "different-test-concept", "N"),
    ]:
        row = dict(rows[0])
        row.update(
            {
                "CATEGORY": "domestic",
                "SCIENTIFIC NAME": "Columba livia",
                "TAXON CONCEPT ID": taxon_id,
                "SUBSPECIES SCIENTIFIC NAME": "Columba livia (Feral Pigeon)",
                "EXOTIC CODE": exotic,
                "GLOBAL UNIQUE IDENTIFIER": "TEST:" + suffix,
                "SAMPLING EVENT IDENTIFIER": "DEMO-" + suffix,
                "OBSERVATION TYPE": "Stationary",
                "PROTOCOL NAME": "Stationary",
                "PROTOCOL CODE": "P21",
                "COUNTY CODE": "US-CA-053",
                "DURATION MINUTES": "20",
                "EFFORT DISTANCE KM": "1.5",
                "REVIEWED": "0",
            }
        )
        rows.append(row)
    # Modern exports have a trailing empty column; keep it in this fixture too.
    with ebd.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=names + [""], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    with (raw / "taxonomy.csv").open(newline="") as f:
        mappings = list(csv.DictReader(f))
    generic = {
        "source_name": "Columba livia",
        "species_id": "pigeon",
        "common_name": "Rock Pigeon",
        "aba_eligible": "1",
        "eligible_states": "*",
        "reference": "SYNTHETIC test mapping",
        # Review workbooks can include observation-like metadata columns. They
        # must not collide with or replace actual EBD evidence in the SQL join.
        "category": "review metadata only",
        "approved": "0",
        "exotic_code": "X",
    }
    mappings.append(generic)
    if qualified:
        mappings.append({**generic, "source_taxon_id": "avibase-533DD36C"})
    write_csv(raw / "taxonomy.csv", mappings, list(generic) + ["source_taxon_id"])
    out = tmp_path / "dataset"
    manifest = prepare(
        ebd,
        raw / "taxonomy.csv",
        raw / "land.csv",
        out,
        Config(),
        "test-v1.16",
        "test",
        synthetic=True,
    )
    stats = manifest["statistics"]["dispositions"]
    assert stats["domestic_taxon_review"] == (1 if qualified else 3)
    if qualified:
        assert stats["exotic_record_review"] == 1
    con = duckdb.connect()
    evidence = con.execute(
        """SELECT record_id,taxon_concept_id,subspecies_scientific_name,
        observation_type,protocol_code,county_code,duration_minutes,effort_distance_km,reviewed
        FROM read_parquet(?) WHERE species_id='pigeon'""",
        [str(out / "evidence.parquet")],
    ).fetchall()
    assert len(evidence) == int(qualified)
    if qualified:
        assert evidence[0] == (
            "TEST:feral",
            "avibase-533DD36C",
            "Columba livia (Feral Pigeon)",
            "Stationary",
            "P21",
            "US-CA-053",
            "20",
            "1.5",
            "0",
        )
        problem = tmp_path / "problem.json"
        build_problem(out, problem, Config())
        synthetic_roads(problem, tmp_path / "roads.csv", tmp_path / "roads.sqlite")
        route = run(
            problem, tmp_path / "roads.sqlite", tmp_path / "run", seconds=2, threads=1, exact=1
        )
        assert "pigeon" in {sp for d in route["days"] for s in d["stops"] for sp in s["species"]}
        assert read(tmp_path / "run" / "validation.json")["valid"]


def test_exact_taxon_denial_overrides_eligible_parent(tmp_path):
    raw = fixtures(tmp_path / "raw", years=(2024,), small=True)
    path = raw / "ebd-2024.tsv"
    with path.open(newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        rows = list(reader)
        fields = list(reader.fieldnames)
    rows[0]["TAXON CONCEPT ID"] = "TEST-DENIED"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields + ["TAXON CONCEPT ID"], delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    with (raw / "taxonomy.csv").open(newline="") as f:
        mappings = list(csv.DictReader(f))
    mappings.append(
        {
            **mappings[0],
            "source_taxon_id": "TEST-DENIED",
            "aba_eligible": "0",
            "reference": "SYNTHETIC exact-taxon denial",
        }
    )
    write_csv(raw / "taxonomy.csv", mappings, list(mappings[0]) + ["source_taxon_id"])
    result = prepare(
        path,
        raw / "taxonomy.csv",
        raw / "land.csv",
        tmp_path / "dataset",
        Config(),
        "test",
        "test",
        synthetic=True,
    )
    assert result["statistics"]["dispositions"]["aba_ineligible"] == 1
