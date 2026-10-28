#!/usr/bin/env python3
"""Build, validate, or submit a compact GBIF eBird daily-presence SQL download.

Default extract: U.S. human bird observations from 2000 onward, grouped by
calendar day, exact coordinates, state, and reported taxon. Returns 14 columns
including a representative occurrence ID, record count, and quality summaries.

Requires Python 3.10+ and curl. Prints SQL by default, without network access.
Use --submit --username YOUR_GBIF_USERNAME to request the extract directly;
curl prompts for your password. No manual JSON request is needed.
Reference: https://techdocs.gbif.org/en/data-use/api-sql-downloads
"""

import argparse
import json
import os
import re
import subprocess
import sys


API = "https://api.gbif.org/v1/occurrence/download"
EOD_DATASET = "4fa7b334-ce0d-4e88-aaae-2e0c138d049e"
RAW_COLUMNS = (
    "gbifid", "occurrenceid", "datasetkey", "license",
    "v_scientificname", "v_taxonconceptid", "scientificname", "taxonrank",
    "species", "eventdate", '"year"', '"month"', '"day"',
    "countrycode", "stateprovince", "county", "locality", "locationid", "eventid",
    "decimallatitude", "decimallongitude", "coordinateuncertaintyinmeters",
    "individualcount", "basisofrecord", "occurrencestatus",
    "hasgeospatialissues", "informationwithheld", "datageneralizations",
    "CONCAT_WS(' | ', issue) AS issues",
)
GROUP_COLUMNS = (
    '"year"', '"month"', '"day"', "stateprovince",
    "v_scientificname", "v_taxonconceptid", "decimallatitude", "decimallongitude",
)
GROUP_SUMMARIES = (
    "MIN(gbifid) AS representative_gbifid",
    "COUNT(*) AS source_record_count",
    "SUM(CASE WHEN hasgeospatialissues = TRUE THEN 1 ELSE 0 END) AS geospatial_flagged_records",
    "SUM(CASE WHEN hasgeospatialissues IS NULL THEN 1 ELSE 0 END) AS geospatial_unknown_records",
    "MAX(coordinateuncertaintyinmeters) AS max_reported_coordinate_uncertainty_m",
    "COUNT(coordinateuncertaintyinmeters) AS records_with_reported_coordinate_uncertainty",
)


def sql_literal(value):
    return "'" + value.replace("'", "''") + "'"


def build_sql(start_year=2000, end_year=None, states=(), mappable=False, count=False, raw=False):
    if not 1 <= start_year <= 9999:
        raise ValueError("start year must be between 1 and 9999")
    if end_year is not None and not start_year <= end_year <= 9999:
        raise ValueError("end year must be at least the start year and at most 9999")
    filters = [
        f"datasetkey = '{EOD_DATASET}'",
        "countrycode = 'US'",
        '"class" = \'Aves\'',
        "basisofrecord = 'HUMAN_OBSERVATION'",
        f'"year" >= {start_year}',
    ]
    if end_year is not None:
        filters.append(f'"year" <= {end_year}')
    if states:
        if any(not state.strip() or any(ord(c) < 32 for c in state) for state in states):
            raise ValueError("state names must be nonempty and contain no control characters")
        values = ", ".join(sql_literal(state.strip()) for state in states)
        filters.append(f"stateprovince IN ({values})")
    if not raw or mappable:
        filters.extend((
            "hascoordinate = TRUE",
            '"month" IS NOT NULL',
            '"day" IS NOT NULL',
            "occurrencestatus = 'PRESENT'",
        ))
    if mappable:
        filters.append("hasgeospatialissues = FALSE")
    columns = '\n  "year", COUNT(*) AS observations' if count else (
        "\n  " + ",\n  ".join(RAW_COLUMNS if raw else GROUP_COLUMNS + GROUP_SUMMARIES)
    )
    query = "SELECT" + columns + "\nFROM occurrence\nWHERE " + "\n  AND ".join(filters)
    if count:
        query += '\nGROUP BY "year"\nORDER BY "year"'
    elif not raw:
        query += "\nGROUP BY " + ", ".join(GROUP_COLUMNS)
    return query


def api_request(path, payload=None, username=None):
    # curl uses the OS certificate store and prompts for the password on the
    # terminal. Passwords never enter this script, its files, or process arguments.
    command = ["curl", "--fail-with-body", "--silent", "--show-error", "--max-time", "120"]
    if username:
        if ":" in username or any(ord(c) < 32 for c in username):
            raise ValueError("provide a GBIF username without a password or control characters")
        command.extend(("--user", username))
    if payload is not None:
        command.extend(("--header", "Content-Type: application/json", "--data-binary", "@-"))
    command.append(API + path)
    try:
        result = subprocess.run(
            command, input=json.dumps(payload) if payload is not None else None,
            text=True, stdout=subprocess.PIPE, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("curl is required; install it and try again") from exc
    if result.returncode:
        detail = result.stdout.strip()
        raise RuntimeError(f"GBIF request failed (curl exit {result.returncode}). {detail}")
    return result.stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2000, help="inclusive; default: 2000")
    parser.add_argument("--end-year", type=int, help="inclusive; default: no upper bound")
    parser.add_argument(
        "--state", action="append", default=[],
        help="exact GBIF stateprovince name, e.g. Alabama; repeat for multiple states",
    )
    parser.add_argument(
        "--mappable", action="store_true",
        help="require coordinates, no GBIF geospatial flags, day/month, and PRESENT status",
    )
    parser.add_argument(
        "--raw", action="store_true", help="export original occurrence rows instead of daily presence groups",
    )
    parser.add_argument(
        "--count", action="store_true", help="request source-record totals by year, before grouping",
    )
    parser.add_argument("--username", default=os.environ.get("GBIF_USER"), help="GBIF username, not email")
    parser.add_argument("--email", default=os.environ.get("GBIF_EMAIL"), help="optional notification email")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--json", action="store_true", help="print the request JSON; no network access")
    action.add_argument("--validate", action="store_true", help="validate with GBIF without creating a job")
    action.add_argument(
        "--submit", action="store_true",
        help="validate and request the default 14-column daily-presence extract (unless --raw/--count)",
    )
    action.add_argument("--status", metavar="KEY", help="show an existing job's status and download link")
    args = parser.parse_args(argv)

    try:
        if args.status:
            if not re.fullmatch(r"[0-9]+-[0-9]+", args.status):
                parser.error("expected a GBIF download key such as 0000379-240229165702484")
            metadata = json.loads(api_request("/" + args.status))
            print(json.dumps({
                field: metadata.get(field)
                for field in ("key", "status", "totalRecords", "size", "doi", "downloadLink")
            }, indent=2))
            return 0
        query = build_sql(args.start_year, args.end_year, args.state, args.mappable, args.count, args.raw)
        payload = {"format": "SQL_TSV_ZIP", "sql": query, "sendNotification": bool(args.email)}
        if args.email:
            payload["notificationAddresses"] = [args.email]
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        if not (args.validate or args.submit):
            print(query)
            return 0
        api_request("/request/validate", payload)
        print("GBIF SQL validation succeeded.", file=sys.stderr)
        if args.validate:
            return 0
        username = args.username
        if not username:
            if not sys.stdin.isatty():
                parser.error("--submit requires --username or GBIF_USER when stdin is not a terminal")
            username = input("GBIF username (not email): ").strip()
        if not username:
            parser.error("a GBIF username is required")
        # No automatic retries: a timed-out POST may already have created a job.
        try:
            result = api_request("/request", payload, username)
        except RuntimeError:
            print("Check https://www.gbif.org/user/downloads before resubmitting.", file=sys.stderr)
            raise
        # Some GBIF deployments return plain text, others a JSON string.
        key = result.strip('"')
        print(f"Download key: {key}")
        print(f"Download page: https://www.gbif.org/occurrence/download/{key}")
        print(f"Check later: python3 {sys.argv[0]} --status {key}")
        print("GBIF prepares the ZIP asynchronously; no bulk data has been downloaded locally.")
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
