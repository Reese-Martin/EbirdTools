"""Pure data preparation and aggregation functions for eBird exports."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = {
    "Submission ID",
    "Common Name",
    "Scientific Name",
    "Count",
    "State/Province",
    "Location ID",
    "Location",
    "Latitude",
    "Longitude",
    "Date",
    "Protocol",
    "All Obs Reported",
    "Number of Observers",
}

OPTIONAL_COLUMNS = {
    "County": pd.NA,
    "Time": pd.NA,
    "Taxonomic Order": pd.NA,
    "Duration (Min)": pd.NA,
    "Distance Traveled (km)": pd.NA,
    "Area Covered (ha)": pd.NA,
    "Breeding Code": pd.NA,
    "Observation Details": pd.NA,
    "Checklist Comments": pd.NA,
    "ML Catalog Numbers": pd.NA,
}


class DataValidationError(ValueError):
    """Raised when a CSV is not a usable My eBird Data export."""


@dataclass(frozen=True)
class PreparedData:
    """Prepared observations plus non-fatal import warnings."""

    observations: pd.DataFrame
    warnings: tuple[str, ...]


def _read_csv(source: bytes | bytearray | str | Path | BinaryIO) -> pd.DataFrame:
    if isinstance(source, (bytes, bytearray)):
        source = BytesIO(source)
    try:
        return pd.read_csv(source, low_memory=False)
    except (OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise DataValidationError(f"The CSV could not be read: {exc}") from exc


def _normalize_complete_checklist(series: pd.Series) -> pd.Series:
    values = series.astype("string").str.strip().str.upper()
    return values.isin({"1", "Y", "YES", "TRUE", "T"})


def load_and_prepare_csv(
    source: bytes | bytearray | str | Path | BinaryIO,
) -> PreparedData:
    """Read, validate, and enrich a My eBird Data CSV export.

    Presence-only counts such as ``X`` are deliberately retained. A nullable
    ``Count Numeric`` column is provided for calculations requiring abundance.
    """

    df = _read_csv(source)
    if df.empty:
        raise DataValidationError("The uploaded CSV contains no observation rows.")

    df.columns = [str(column).strip() for column in df.columns]
    missing = sorted(REQUIRED_COLUMNS.difference(df.columns))
    if missing:
        raise DataValidationError(
            "This does not look like a My eBird Data export. Missing columns: "
            + ", ".join(missing)
        )

    for column, default in OPTIONAL_COLUMNS.items():
        if column not in df.columns:
            df[column] = default

    warnings: list[str] = []
    df = df.copy()

    text_columns = [
        "Submission ID",
        "Common Name",
        "Scientific Name",
        "State/Province",
        "County",
        "Location ID",
        "Location",
        "Time",
        "Protocol",
        "Breeding Code",
        "Observation Details",
        "Checklist Comments",
        "ML Catalog Numbers",
    ]
    for column in text_columns:
        df[column] = df[column].astype("string").str.strip()

    parsed_dates = pd.to_datetime(df["Date"], errors="coerce")
    invalid_dates = int(parsed_dates.isna().sum())
    if invalid_dates:
        warnings.append(f"Ignored {invalid_dates:,} rows with invalid dates.")
        df = df.loc[parsed_dates.notna()].copy()
        parsed_dates = parsed_dates.loc[parsed_dates.notna()]
    if df.empty:
        raise DataValidationError("No rows had a valid observation date.")
    df["Date"] = parsed_dates.astype("datetime64[ns]")

    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")
    invalid_coordinates = ~(
        df["Latitude"].between(-90, 90) & df["Longitude"].between(-180, 180)
    )
    if invalid_coordinates.any():
        warnings.append(
            f"{int(invalid_coordinates.sum()):,} rows have missing or invalid coordinates; "
            "they will be omitted from maps."
        )
        df.loc[invalid_coordinates, ["Latitude", "Longitude"]] = np.nan

    df["Count Numeric"] = pd.to_numeric(df["Count"], errors="coerce")
    count_text = df["Count"].astype("string").str.strip().str.upper()
    df["Count Is Presence Only"] = df["Count Numeric"].isna() & count_text.isin(
        {"X", "+", "*"}
    )
    unknown_count_tokens = (
        df["Count Numeric"].isna() & count_text.notna() & ~df["Count Is Presence Only"]
    )
    if unknown_count_tokens.any():
        warnings.append(
            f"Treated {int(unknown_count_tokens.sum()):,} unrecognized counts as "
            "presence-only observations."
        )
        df.loc[unknown_count_tokens, "Count Is Presence Only"] = True

    time_values = pd.to_datetime(df["Time"], format="%I:%M %p", errors="coerce")
    df["Hour"] = time_values.dt.hour.astype("Int64")
    df["Start Minute"] = (
        time_values.dt.hour.mul(60).add(time_values.dt.minute).astype("Int64")
    )

    numeric_columns = [
        "Duration (Min)",
        "Distance Traveled (km)",
        "Area Covered (ha)",
        "Number of Observers",
        "Taxonomic Order",
    ]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["Complete Checklist"] = _normalize_complete_checklist(df["All Obs Reported"])
    df["Taxon Key"] = df["Scientific Name"].fillna(df["Common Name"])
    df["Display Name"] = df["Common Name"].fillna(df["Scientific Name"])
    df["Country Code"] = df["State/Province"].str.split("-").str[0].str.upper()

    common_lower = df["Common Name"].str.lower().fillna("")
    scientific_lower = df["Scientific Name"].str.lower().fillna("")
    df["Ambiguous Taxon"] = (
        common_lower.str.contains("/", regex=False)
        | common_lower.str.contains(r"\bsp\.$", regex=True)
        | common_lower.str.contains("hybrid", regex=False)
        | common_lower.str.contains(r"\sx\s|×", regex=True)
        | scientific_lower.str.contains(r"\sx\s|×", regex=True)
    )

    df["Month"] = df["Date"].dt.month
    df["Month Name"] = df["Date"].dt.strftime("%B")
    df["Year"] = df["Date"].dt.year
    df["Day Of Year"] = df["Date"].dt.dayofyear
    leap_reference = pd.to_datetime(
        {
            "year": np.full(len(df), 2000),
            "month": df["Date"].dt.month.to_numpy(),
            "day": df["Date"].dt.day.to_numpy(),
        }
    )
    df["Calendar Day"] = leap_reference.dt.dayofyear.to_numpy()

    df = df.sort_values(
        ["Date", "Start Minute", "Submission ID", "Taxonomic Order"],
        na_position="last",
    ).reset_index(drop=True)
    return PreparedData(df, tuple(warnings))


def filter_taxa(observations: pd.DataFrame, include_ambiguous: bool) -> pd.DataFrame:
    """Return observations matching the selected taxon policy."""

    if include_ambiguous:
        return observations.copy()
    return observations.loc[~observations["Ambiguous Taxon"]].copy()


def build_checklists(observations: pd.DataFrame) -> pd.DataFrame:
    """Create one effort/location row per submission."""

    if observations.empty:
        return observations.copy()
    ordered = observations.sort_values(
        ["Date", "Start Minute", "Submission ID"], na_position="last"
    )
    return ordered.drop_duplicates("Submission ID", keep="first").reset_index(drop=True)


def build_species_summary(
    observations: pd.DataFrame, reference_date: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Summarize each taxon while preserving row-associated first/last fields."""

    columns = [
        "Taxon Key",
        "Common Name",
        "Scientific Name",
        "First Seen",
        "First Location",
        "First Location ID",
        "Last Seen",
        "Last Location",
        "Last Location ID",
        "Checklist Count",
        "Location Count",
        "Exact Individuals",
        "Presence-only Records",
        "Lifer Count",
        "Days Since Seen",
    ]
    if observations.empty:
        return pd.DataFrame(columns=columns)

    ordered = observations.sort_values(
        ["Date", "Start Minute", "Submission ID", "Taxonomic Order"],
        na_position="last",
    )
    first = ordered.drop_duplicates("Taxon Key", keep="first")[
        [
            "Taxon Key",
            "Common Name",
            "Scientific Name",
            "Date",
            "Location",
            "Location ID",
        ]
    ].rename(
        columns={
            "Date": "First Seen",
            "Location": "First Location",
            "Location ID": "First Location ID",
        }
    )
    last = ordered.drop_duplicates("Taxon Key", keep="last")[
        ["Taxon Key", "Date", "Location", "Location ID"]
    ].rename(
        columns={
            "Date": "Last Seen",
            "Location": "Last Location",
            "Location ID": "Last Location ID",
        }
    )
    totals = observations.groupby("Taxon Key", as_index=False).agg(
        **{
            "Checklist Count": ("Submission ID", "nunique"),
            "Location Count": ("Location ID", "nunique"),
            "Exact Individuals": (
                "Count Numeric",
                lambda values: values.sum(min_count=1),
            ),
            "Presence-only Records": ("Count Is Presence Only", "sum"),
        }
    )
    summary = first.merge(last, on="Taxon Key", how="left").merge(
        totals, on="Taxon Key", how="left"
    )
    summary = summary.sort_values(
        ["First Seen", "Common Name", "Scientific Name"], na_position="last"
    ).reset_index(drop=True)
    summary["Lifer Count"] = np.arange(1, len(summary) + 1)
    if reference_date is None:
        reference_date = pd.Timestamp.today().normalize()
    reference_date = pd.Timestamp(reference_date).normalize()
    summary["Days Since Seen"] = (reference_date - summary["Last Seen"]).dt.days
    return summary


def build_location_summary(
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    years: list[int] | tuple[int, ...] | np.ndarray | None = None,
) -> pd.DataFrame:
    """Summarize location richness and visits from the same filtered period."""

    selected_checklists = checklists
    if years is not None:
        selected_checklists = checklists.loc[checklists["Year"].isin(years)]
    selected_ids = selected_checklists["Submission ID"]
    selected_observations = observations.loc[
        observations["Submission ID"].isin(selected_ids)
    ]
    if selected_checklists.empty:
        return pd.DataFrame(
            columns=[
                "Location ID",
                "Location",
                "Latitude",
                "Longitude",
                "Unique Taxa",
                "Location Visits",
            ]
        )

    locations = selected_checklists.groupby("Location ID", as_index=False).agg(
        Location=("Location", "first"),
        Latitude=("Latitude", "first"),
        Longitude=("Longitude", "first"),
        **{"Location Visits": ("Submission ID", "nunique")},
    )
    richness = selected_observations.groupby("Location ID", as_index=False).agg(
        **{"Unique Taxa": ("Taxon Key", "nunique")}
    )
    return locations.merge(richness, on="Location ID", how="left").fillna(
        {"Unique Taxa": 0}
    )


def build_annual_accumulation(observations: pd.DataFrame) -> pd.DataFrame:
    """Create leap-safe cumulative annual taxon counts for polar charts."""

    frames: list[pd.DataFrame] = []
    for year, year_data in observations.groupby("Year", sort=True):
        annual_firsts = (
            year_data.sort_values(
                ["Calendar Day", "Start Minute", "Submission ID", "Taxonomic Order"],
                na_position="last",
            )
            .drop_duplicates("Taxon Key", keep="first")
            .groupby("Calendar Day")
            .size()
        )
        last_day = int(year_data["Calendar Day"].max())
        days = pd.RangeIndex(1, last_day + 1, name="Calendar Day")
        cumulative = annual_firsts.reindex(days, fill_value=0).cumsum()
        frame = cumulative.rename("Species Count").reset_index()
        frame["Year"] = int(year)
        frame["Theta"] = (frame["Calendar Day"] - 1) / 366 * 360
        date_reference = pd.Timestamp("2000-01-01") + pd.to_timedelta(
            frame["Calendar Day"] - 1, unit="D"
        )
        frame["Date Label"] = date_reference.dt.strftime("%b %d")
        frames.append(frame)
    if not frames:
        return pd.DataFrame(
            columns=["Calendar Day", "Species Count", "Year", "Theta", "Date Label"]
        )
    return pd.concat(frames, ignore_index=True)


def build_monthly_average(observations: pd.DataFrame) -> pd.DataFrame:
    """Average monthly richness, counting inactive in-range months as zero."""

    if observations.empty:
        return pd.DataFrame(
            columns=["Month", "Month Name", "Average Species", "Months Included"]
        )
    periods = observations["Date"].dt.to_period("M")
    monthly = (
        observations.assign(Period=periods).groupby("Period")["Taxon Key"].nunique()
    )
    full_periods = pd.period_range(periods.min(), periods.max(), freq="M")
    monthly = monthly.reindex(full_periods, fill_value=0)
    table = monthly.rename_axis("Period").reset_index(name="Species Count")
    table["Month"] = table["Period"].dt.month
    result = table.groupby("Month", as_index=False).agg(
        **{
            "Average Species": ("Species Count", "mean"),
            "Months Included": ("Species Count", "size"),
        }
    )
    result["Month Name"] = pd.to_datetime(
        result["Month"].astype(str), format="%m"
    ).dt.strftime("%B")
    return result.sort_values("Month").reset_index(drop=True)


def build_chronotype(
    observations: pd.DataFrame, checklists: pd.DataFrame
) -> pd.DataFrame:
    """Summarize actual checklist starts and average checklist richness by hour."""

    valid_checklists = checklists.loc[
        checklists["Hour"].notna(), ["Submission ID", "Hour"]
    ]
    if valid_checklists.empty:
        return pd.DataFrame(
            columns=[
                "Hour",
                "Checklists",
                "Average Species Per Checklist",
                "Median Species Per Checklist",
                "Hour Label",
                "Theta",
            ]
        )
    species_counts = observations.groupby("Submission ID", as_index=False).agg(
        **{"Species Per Checklist": ("Taxon Key", "nunique")}
    )
    per_checklist = valid_checklists.merge(
        species_counts, on="Submission ID", how="left", validate="one_to_one"
    )
    per_checklist["Species Per Checklist"] = per_checklist[
        "Species Per Checklist"
    ].fillna(0)
    result = per_checklist.groupby("Hour", as_index=False).agg(
        Checklists=("Submission ID", "nunique"),
        **{
            "Average Species Per Checklist": ("Species Per Checklist", "mean"),
            "Median Species Per Checklist": ("Species Per Checklist", "median"),
        },
    )
    result["Hour"] = result["Hour"].astype(int)
    result["Hour Label"] = result["Hour"].map(lambda hour: f"{hour:02d}:00")
    result["Theta"] = result["Hour"] / 24 * 360
    return result.sort_values("Hour").reset_index(drop=True)
