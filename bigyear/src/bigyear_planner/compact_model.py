"""Complete, mmap-friendly candidate catalogs; no Python list of observations.

Schema BY4 stores fixed 24-byte opportunity records and a contiguous uint32
species list. Metadata stays in a bounded-page-cache SQLite catalog. Source
observations and verbatim coordinate strings remain in the verified archive.
"""

import json
from pathlib import Path
import sqlite3
import struct
import time

import numpy as np

from .eod import attach_dataset
from .config import Config
from .util import ManagedDuckDB, dump, q, read, sha256

RECORD = struct.Struct("<IIQII")
FORMAT = "bigyear-compact-candidates-v1"


def build(dataset, output, config):
    started = time.monotonic()
    dataset = Path(dataset).resolve()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "model.json").exists():
        previous = load(output / "model.json")
        if (
            previous.meta["config"] != config.to_dict()
            or previous.meta["dataset"] != str(dataset)
            or previous.meta["manifest_sha256"] != sha256(dataset / "manifest.json")
        ):
            previous.close()
            raise ValueError("existing compact model belongs to different inputs/configuration")
        return previous
    manifest = read(dataset / "manifest.json")
    if manifest.get('source_format') == 'EBD_PARQUET':
        from .ebd import validate_config

        validate_config(config)
        if config.to_dict() != manifest['config']:
            raise ValueError('EBD model configuration differs from the frozen import')
    binding = {
        "dataset_manifest_sha256": sha256(dataset / "manifest.json"),
        "config": config.to_dict(),
    }
    if (output / "build-binding.json").exists() and read(output / "build-binding.json") != binding:
        raise ValueError("partial compact build belongs to different inputs; choose a new output")
    dump(output / "build-binding.json", binding)
    for name, digest in manifest["outputs"].items():
        if sha256(dataset / name) != digest:
            raise ValueError("dataset component changed: " + name)
    with ManagedDuckDB() as con:
        con.execute(f"SET memory_limit={q(config.memory_limit)}")
        con.execute(f"SET threads={config.threads}")
        con.execute("SET max_temp_directory_size='8GB'")
        con.execute("SET preserve_insertion_order=false")
        _, obs = attach_dataset(con, dataset, manifest)
        verbatim = manifest.get('source_format') == 'EBD_PARQUET'
        coordinate_columns = ',latitude_text,longitude_text' if verbatim else ''
        # A narrow lossless date/location/species projection is staging only.
        # All represented observations remain bound through the source archive.
        narrow = output / "rows.parquet"
        if not narrow.exists():
            con.execute(f"""COPY (SELECT date,location_id,locality_id,locality,lat,lon,state,
               species_id,common_name,source_rows{coordinate_columns} FROM {obs}) TO {q(narrow.with_suffix(".part"))}
               (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)""")
            narrow.with_suffix(".part").replace(narrow)
        con.execute(f"CREATE VIEW compact_rows AS SELECT * FROM read_parquet({q(narrow)})")
        extra_location = ',min(latitude_text) AS latitude_text,min(longitude_text) AS longitude_text' if verbatim else ''
        con.execute(f"""CREATE TABLE compact_locations AS SELECT row_number() OVER (ORDER BY location_id)-1 AS idx,
            location_id AS id,min(locality) AS name,lat,lon,state,locality_id{extra_location}
            FROM compact_rows GROUP BY location_id,lat,lon,state,locality_id""")
        con.execute("""CREATE TABLE compact_species AS SELECT row_number() OVER (ORDER BY species_id)-1 AS idx,
            species_id AS id,min(common_name) AS name FROM compact_rows GROUP BY species_id""")
        species = [
            {"id": r[0], "name": r[1]}
            for r in con.execute("SELECT id,name FROM compact_species ORDER BY idx").fetchall()
        ]
        loc_count = con.execute("SELECT count(*) FROM compact_locations").fetchone()[0]
        if not loc_count or config.target_locations < loc_count:
            raise ValueError("compact model requires the complete cohort")
        catalog = output / "locations.sqlite"
        catalog.unlink(missing_ok=True)
        with sqlite3.connect(catalog) as db, (output / "coordinates.bin").open("wb") as coords:
            db.execute("PRAGMA journal_mode=OFF")
            db.execute("PRAGMA cache_size=-16384")
            db.execute("CREATE TABLE locations(idx INTEGER PRIMARY KEY, metadata TEXT NOT NULL)")
            result = con.execute(
                f"SELECT idx,id,name,lat,lon,state,locality_id{coordinate_columns} FROM compact_locations ORDER BY idx"
            )
            for batch in iter(lambda: result.fetchmany(10000), []):
                db.executemany(
                    "INSERT INTO locations VALUES (?,?)",
                    [
                        (
                            r[0],
                            json.dumps(
                                dict(
                                    zip(
                                        [
                                            "id",
                                            "name",
                                            "lat",
                                            "lon",
                                            "state",
                                            "locality_id",
                                            *(["latitude_text", "longitude_text"] if verbatim else []),
                                            "kind",
                                        ],
                                        [*r[1:], "birding"],
                                    )
                                ),
                                separators=(",", ":"),
                            ),
                        )
                        for r in batch
                    ],
                )
                coords.write(np.asarray([(r[3], r[4]) for r in batch], dtype="<f8").tobytes())
        con.execute("""CREATE VIEW integer_rows AS SELECT date,l.idx AS location,s.idx AS species,source_rows
            FROM compact_rows r JOIN compact_locations l ON r.location_id=l.id
            JOIN compact_species s ON r.species_id=s.id""")
        groups, observations = con.execute(
            "SELECT count(*),sum(source_rows) FROM integer_rows"
        ).fetchone()
        expected_pairs = con.execute(
            "SELECT count(*) FROM (SELECT DISTINCT date,location,species FROM integer_rows)"
        ).fetchone()[0]
        if (
            groups != manifest["statistics"]["dispositions"]["eligible"]
            or loc_count != manifest["statistics"]["locations"]
            or len(species) != manifest["statistics"]["annual_recorded_species_ceiling"]
        ):
            raise ValueError("complete compact model counts differ from independent dataset audit")
        count = 0
        offset = 0
        with (
            (output / "opportunities.bin").open("wb") as opps,
            (output / "species.bin").open("wb") as spp,
        ):
            # A month at a time bounds DuckDB aggregation and spill. Canonical
            # date/location IDs match the legacy complete model exactly.
            for month in range(1, 13):
                result = con.execute(
                    """SELECT date,location,list(DISTINCT species ORDER BY species)
                    FROM integer_rows WHERE month(date)=? GROUP BY 1,2 ORDER BY 1,2""",
                    [month],
                )
                for batch in iter(lambda: result.fetchmany(10000), []):
                    for day, loc, ids in batch:
                        opps.write(
                            RECORD.pack((day - config.first_date).days, loc, offset, len(ids), 0)
                        )
                        spp.write(np.asarray(ids, dtype="<u4").tobytes())
                        offset += len(ids)
                        count += 1
                print(
                    json.dumps(
                        {
                            "stage": "compact",
                            "month": month,
                            "opportunities": count,
                            "species_entries": offset,
                        }
                    ),
                    flush=True,
                )
        # Daily all-observer comparison needs only first occurrence per species.
        first_seen = [
            {"species": r[0], "date": str(r[1])}
            for r in con.execute(
                "SELECT species_id,min(date) FROM compact_rows GROUP BY 1 ORDER BY 2,1"
            ).fetchall()
        ]
        state_counts = [
            dict(
                zip(["state", "presence_groups", "represented_observations", "exact_locations"], r)
            )
            for r in con.execute(
                "SELECT state,count(*),sum(source_rows),count(DISTINCT location_id) FROM compact_rows GROUP BY 1 ORDER BY 1"
            ).fetchall()
        ]
        metadata = {
            "format": FORMAT,
            "config": config.to_dict(),
            "dataset": str(dataset),
            "manifest": manifest,
            "manifest_sha256": sha256(dataset / "manifest.json"),
            "species": species,
            "locations": loc_count,
            "opportunities": count,
            "species_entries": offset,
            "eligible_presence_groups": groups,
            "represented_observations": observations,
            "first_seen": first_seen,
            "state_counts": state_counts,
            "candidate_scope": {
                "states": sorted(__import__("bigyear_planner.config", fromlist=["STATES"]).STATES),
                "from_date": None,
                "to_date": None,
            },
            "reduction": {
                "input_locations": loc_count,
                "selected_birding_locations": loc_count,
                "candidate_species_ceiling": len(species),
                "policy": "all eligible locations and dates; no candidate reduction",
                "protected_locations": 0,
                "grid_representatives": 0,
                "target_is_soft": True,
            },
            "components": {
                name: sha256(output / name)
                for name in [
                    "locations.sqlite",
                    "coordinates.bin",
                    "opportunities.bin",
                    "species.bin",
                ]
            },
            "preparation_seconds": time.monotonic() - started,
        }
        # Each source presence group maps to exactly one opportunity-species pair.
        if offset != expected_pairs:
            raise ValueError(
                "candidate pairs differ from complete source location/date/species combinations"
            )
        dump(output / "model.json", metadata)
        narrow.unlink()
    return load(output / "model.json")


class Catalog:
    def __init__(self, path, count):
        self.db = sqlite3.connect(
            "file:" + str(path) + "?mode=ro&immutable=1", uri=True, check_same_thread=False
        )
        self.db.execute("PRAGMA cache_size=-8192")
        self.count = count

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if not 0 <= index < self.count:
            raise IndexError(index)
        location = json.loads(
            self.db.execute("SELECT metadata FROM locations WHERE idx=?", [int(index)]).fetchone()[
                0
            ]
        )
        # EOD's locality label is made from the verbatim source strings by the
        # adapter. Expose those strings without rounding or duplicating them in
        # two million catalog records.
        if location["name"].startswith("EOD "):
            lat, lon = location["name"][4:].split(",")
            if (float(lat), float(lon)) != (location["lat"], location["lon"]):
                raise ValueError("verbatim catalog coordinates differ from numeric routing point")
            location.update(latitude_text=lat, longitude_text=lon)
        return location

    def close(self):
        self.db.close()


class CompactModel:
    def __init__(self, path, verify=True):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self.meta = read(path)
        if self.meta["format"] != FORMAT:
            raise ValueError("unsupported compact model")
        if verify:
            for name, digest in self.meta["components"].items():
                if sha256(self.root / name) != digest:
                    raise ValueError("compact component changed: " + name)
        self.locations = Catalog(self.root / "locations.sqlite", self.meta["locations"])
        self.coords = np.memmap(
            self.root / "coordinates.bin", mode="r", dtype="<f8", shape=(len(self.locations), 2)
        )
        self.records = np.memmap(
            self.root / "opportunities.bin",
            mode="r",
            dtype=np.dtype(
                [
                    ("day", "<u4"),
                    ("location", "<u4"),
                    ("offset", "<u8"),
                    ("count", "<u4"),
                    ("reserved", "<u4"),
                ]
            ),
        )
        self.species = np.memmap(self.root / "species.bin", mode="r", dtype="<u4")
        if (
            len(self.records) != self.meta["opportunities"]
            or len(self.species) != self.meta["species_entries"]
        ):
            raise ValueError("truncated compact model")
        self.routing_model_sha256 = sha256(self.path)
        calendar_original = None
        if self.meta.get('calendar_source'):
            source=self.meta['calendar_source']
            if sha256(source['path'])!=source['sha256']:
                raise ValueError('calendar control source changed')
            calendar_original=read(source['path'])
            before=Config(**calendar_original['config']).to_dict()
            after=Config(**self.meta['config']).to_dict()
            allowed={'local_miles','mixed_road_birding','drive_minutes'}
            changed={k for k in before if before[k]!=after[k]}
            if not changed or not changed<=allowed or calendar_original['components']!=self.meta['components']:
                raise ValueError('calendar control must preserve every observation, coordinate and access duration')
            if 'drive_minutes' in changed and not (after['mixed_road_birding'] and
                    after['drive_minutes']==after['activity_minutes']==720):
                raise ValueError('driving relaxation must share the 12-hour activity budget')
            if any(calendar_original[k]!=self.meta[k] for k in ['locations','opportunities','species','species_entries']):
                raise ValueError('calendar control changed the complete catalog')
            self.routing_model_sha256=source['sha256']
        if self.meta.get('routing_source'):
            source=self.meta['routing_source']
            if sha256(source['path'])!=source['sha256']:
                raise ValueError('routing source changed')
            original=read(source['path'])
            routing_config=(calendar_original or self.meta)['config']
            if (original['locations']!=self.meta['locations'] or original['config']!=routing_config
                or any(original['components'][name]!=self.meta['components'][name]
                       for name in ['locations.sqlite','coordinates.bin'])):
                raise ValueError('routing derivative changed coordinates, identities or calendar rules')
            self.routing_model_sha256=source['sha256']
        if self.meta.get('countability_source'):
            source = self.meta['countability_source']
            if sha256(source['path'])!=source['sha256']:
                raise ValueError('countability original model changed')
            original = read(source['path'])
            if any(original['components'][name]!=self.meta['components'][name]
                   for name in ['locations.sqlite','coordinates.bin']):
                raise ValueError('countability model changed routing coordinates/identities')
            if original['config']!=self.meta['config'] or original['opportunities']!=len(self.records):
                raise ValueError('countability model changed routing calendar/catalog')
            original_records = np.memmap(Path(source['path']).parent/'opportunities.bin',mode='r',dtype=self.records.dtype)
            try:
                if sha256(Path(source['path']).parent/'opportunities.bin')!=original['components']['opportunities.bin']:
                    raise ValueError('original countability calendar changed')
                for begin in range(0,len(self.records),250000):
                    for field in ['day','location']:
                        if not np.array_equal(self.records[field][begin:begin+250000],original_records[field][begin:begin+250000]):
                            raise ValueError('countability opportunity identities changed')
            finally:
                original_records._mmap.close()
            self.routing_model_sha256 = source['sha256']

    def opportunity(self, index):
        r = self.records[index]
        d = int(r["day"])
        offset = int(r["offset"])
        count = int(r["count"])
        from datetime import timedelta

        result = {
            "id": int(index),
            "day": d,
            "date": str(Config(**self.meta['config']).first_date + timedelta(days=d)),
            "location": int(r["location"]),
            "species": [int(v) for v in self.species[offset : offset + count]],
        }
        if int(r['reserved']):
            result['marine_trip']=self.meta['joint_marine']['trip_ids'][int(r['reserved'])-1]
        return result

    def close(self):
        self.locations.close()
        for array in [self.coords, self.records, self.species]:
            array._mmap.close()


def load(path, verify=True):
    return CompactModel(path, verify)
