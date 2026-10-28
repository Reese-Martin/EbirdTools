#!/usr/bin/env python3
"""Convert grouped EOD TSV to verified Zstandard-3 Parquet, retaining the original."""

import argparse
import json

from bigyear_planner.eod_archive import convert


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument("--chunk-mib", type=int, default=64)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    manifest = convert(
        args.source,
        args.output,
        args.expected_rows,
        args.chunk_mib,
        args.resume,
        lambda value: print(json.dumps(value), flush=True),
    )
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "status",
                    "rows",
                    "bytes",
                    "source_sha256",
                    "verified_mismatches",
                    "conversion_seconds",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
