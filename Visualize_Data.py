"""Interactive visualizer for a person's My eBird Data CSV export."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pycountry
import streamlit as st
from shapely.geometry import shape

from ebird_tools.boundaries import (
    BoundaryBundle,
    BoundaryError,
    assign_locations_to_boundaries,
    boundary_catalog,
    fetch_boundary_bundle,
    subset_boundary_bundle,
)
from ebird_tools.data import (
    DataValidationError,
    build_annual_accumulation,
    build_checklists,
    build_chronotype,
    build_location_summary,
    build_monthly_average,
    build_species_summary,
    filter_taxa,
    load_and_prepare_csv,
)
from ebird_tools.ebird_api import (
    EbirdAPIError,
    fetch_ebird_taxonomy,
    fetch_recent_observations,
)

MONTH_ORDER = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
ALL_COUNTRIES = "__all_countries__"
ATLAS_INTENSITY_SCALE = [
    [0.0, "#ffffb2"],
    [0.25, "#fecc5c"],
    [0.5, "#fd8d3c"],
    [0.75, "#f03b20"],
    [1.0, "#bd0026"],
]
ATLAS_STATUS_COLORS = {
    "Never birded": "#ffffb2",
    "Birded — not seen": "#fd8d3c",
    "Seen": "#bd0026",
}

st.set_page_config(
    page_title="eBird Data Dive",
    page_icon="🐦",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _country_name(code: str) -> str:
    country = pycountry.countries.get(alpha_2=str(code).upper())
    return country.name if country is not None else str(code)


def _display_date(value: object) -> str:
    if pd.isna(value):
        return "—"
    return pd.Timestamp(value).strftime("%b %d, %Y")


def _map_view(latitude: pd.Series, longitude: pd.Series) -> tuple[dict, float]:
    valid = pd.DataFrame({"lat": latitude, "lon": longitude}).dropna()
    if valid.empty:
        return {"lat": 0.0, "lon": 0.0}, 1.0
    center = {"lat": float(valid["lat"].mean()), "lon": float(valid["lon"].mean())}
    span = max(
        float(valid["lat"].max() - valid["lat"].min()),
        float(valid["lon"].max() - valid["lon"].min()),
    )
    if span < 0.5:
        zoom = 8.0
    elif span < 2:
        zoom = 6.0
    elif span < 6:
        zoom = 4.5
    elif span < 20:
        zoom = 3.5
    elif span < 60:
        zoom = 2.5
    else:
        zoom = 1.0
    return center, zoom


def _boundary_map_view(
    bundle: BoundaryBundle, boundary_ids: set[str] | None = None
) -> tuple[dict, float]:
    """Frame whole selected regions rather than only their observation points."""

    bounds: list[tuple[float, float, float, float]] = []
    for feature in bundle.geojson["features"]:
        if boundary_ids is not None and str(feature["id"]) not in boundary_ids:
            continue
        try:
            geometry = shape(feature["geometry"])
            if not geometry.is_empty:
                bounds.append(geometry.bounds)
        except (TypeError, ValueError):
            continue
    if not bounds:
        return {"lat": 0.0, "lon": 0.0}, 1.0
    return _map_view(
        pd.Series([min(item[1] for item in bounds), max(item[3] for item in bounds)]),
        pd.Series([min(item[0] for item in bounds), max(item[2] for item in bounds)]),
    )


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def _cached_boundary_bundle(country_code: str, admin_level: str) -> BoundaryBundle:
    """Cache public boundaries; no personal observation data enter this cache."""

    return fetch_boundary_bundle(country_code, preferred_levels=(admin_level,))


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def _cached_ebird_taxonomy() -> pd.DataFrame:
    """Cache public eBird taxonomy metadata; no personal data enter this cache."""

    return fetch_ebird_taxonomy()


def _get_boundary_context(
    country_code: str,
    all_observations: pd.DataFrame,
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    file_hash: str,
    admin_level: str,
) -> tuple[BoundaryBundle, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    bundle = _cached_boundary_bundle(country_code, admin_level)
    country_checklists = checklists.loc[checklists["Country Code"] == country_code]
    locations = country_checklists[
        ["Location ID", "Latitude", "Longitude", "State/Province", "County"]
    ].drop_duplicates("Location ID")

    cache_key = f"{file_hash}:{country_code}:{bundle.admin_level}:{bundle.cache_key}"
    assignment_cache = st.session_state.setdefault("boundary_assignment_cache", {})
    if cache_key not in assignment_cache:
        assignment_cache[cache_key] = assign_locations_to_boundaries(locations, bundle)
    assignments = assignment_cache[cache_key]

    mapped_checklists = country_checklists.merge(
        assignments, on="Location ID", how="inner", validate="many_to_one"
    )
    mapped_all_observations = all_observations.loc[
        all_observations["Country Code"] == country_code
    ].merge(assignments, on="Location ID", how="inner", validate="many_to_one")
    mapped_observations = observations.loc[
        observations["Country Code"] == country_code
    ].merge(assignments, on="Location ID", how="inner", validate="many_to_one")
    return bundle, mapped_checklists, mapped_all_observations, mapped_observations


def _country_selector(checklists: pd.DataFrame, key: str) -> str:
    counts = checklists["Country Code"].value_counts()
    country_options = counts.index.dropna().tolist()
    options = (
        [ALL_COUNTRIES, *country_options]
        if len(country_options) > 1
        else country_options
    )

    def format_country(code: str) -> str:
        if code == ALL_COUNTRIES:
            return (
                f"All countries ({len(country_options):,} countries · "
                f"{int(counts.sum()):,} checklists)"
            )
        return f"{_country_name(code)} ({counts.get(code, 0):,} checklists)"

    return st.selectbox(
        "Atlas scope",
        options,
        format_func=format_country,
        key=key,
    )


def _numeric_boundary_map(
    catalog: pd.DataFrame,
    metrics: pd.DataFrame,
    bundle: BoundaryBundle,
    metric_name: str,
    title: str,
    center: dict,
    zoom: float,
) -> tuple[go.Figure, pd.DataFrame]:
    map_data = catalog.merge(metrics, on="Boundary ID", how="left")
    figure = px.choropleth_map(
        map_data,
        geojson=bundle.geojson,
        locations="Boundary ID",
        featureidkey="id",
        color=metric_name,
        hover_name="Boundary Name",
        hover_data={"Boundary ID": False},
        color_continuous_scale=ATLAS_INTENSITY_SCALE,
        map_style="carto-positron",
        center=center,
        zoom=zoom,
        opacity=0.72,
        title=title,
        height=720,
    )
    figure.update_layout(margin={"l": 0, "r": 0, "t": 45, "b": 0})
    return figure, map_data


def _world_catalog() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Country Code": country.alpha_2,
                "ISO3": country.alpha_3,
                "Country": country.name,
            }
            for country in pycountry.countries
        ]
    )


def _numeric_world_map(
    catalog: pd.DataFrame,
    metrics: pd.DataFrame,
    metric_name: str,
    title: str,
) -> tuple[go.Figure, pd.DataFrame]:
    map_data = catalog.merge(metrics, on="Country Code", how="left")
    figure = px.choropleth(
        map_data,
        locations="ISO3",
        locationmode="ISO-3",
        color=metric_name,
        hover_name="Country",
        hover_data={"ISO3": False, "Country Code": False},
        color_continuous_scale=ATLAS_INTENSITY_SCALE,
        projection="natural earth",
        title=title,
        height=720,
    )
    figure.update_geos(
        showframe=False,
        showcoastlines=False,
        showcountries=True,
        bgcolor="rgba(0,0,0,0)",
    )
    figure.update_layout(
        margin={"l": 0, "r": 0, "t": 45, "b": 0},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return figure, map_data


def _render_species_information(
    species_name: str,
    observations: pd.DataFrame,
    species_summary: pd.DataFrame,
) -> None:
    selected = observations.loc[observations["Common Name"] == species_name]
    if selected.empty:
        return
    summary_matches = species_summary.loc[
        species_summary["Common Name"] == species_name
    ]
    summary = summary_matches.iloc[0] if not summary_matches.empty else None
    scientific_values = selected["Scientific Name"].dropna()
    scientific_name = (
        str(scientific_values.iloc[0]) if not scientific_values.empty else species_name
    )

    taxonomy_record: pd.Series | None = None
    taxonomy_error: str | None = None
    try:
        taxonomy = _cached_ebird_taxonomy()
        matches = taxonomy.loc[taxonomy["Scientific Name"] == scientific_name]
        common_matches = matches.loc[matches["Common Name"] == species_name]
        if not common_matches.empty:
            matches = common_matches
        if not matches.empty:
            taxonomy_record = matches.iloc[0]
    except EbirdAPIError as exc:
        taxonomy_error = str(exc)

    with st.expander(f"About {species_name}", expanded=True):
        if taxonomy_record is not None:
            family_common = taxonomy_record.get("Family Common Name")
            family_scientific = taxonomy_record.get("Family Scientific Name")
            order = taxonomy_record.get("Order")
            category_value = taxonomy_record.get("Category")
            category = (
                str(category_value).replace("_", " ")
                if pd.notna(category_value)
                else ""
            )
            taxonomy_parts = [f"*{scientific_name}*"]
            if pd.notna(family_common):
                family = str(family_common)
                if pd.notna(family_scientific):
                    family += f" (*{family_scientific}*)"
                taxonomy_parts.append(family)
            if pd.notna(order):
                taxonomy_parts.append(str(order))
            if category:
                taxonomy_parts.append(category.title())
            st.markdown(" · ".join(taxonomy_parts))
        else:
            st.markdown(f"*{scientific_name}*")
            if taxonomy_error:
                st.caption(taxonomy_error)

        col1, col2, col3, col4 = st.columns(4)
        checklist_count = selected["Submission ID"].nunique()
        location_count = selected["Location ID"].nunique()
        country_count = selected["Country Code"].nunique()
        maximum_count = selected["Count Numeric"].max()
        col1.metric("Your checklists", f"{checklist_count:,}")
        col2.metric("Your locations", f"{location_count:,}")
        col3.metric("Countries", f"{country_count:,}")
        col4.metric(
            "Maximum exact count",
            "—" if pd.isna(maximum_count) else f"{int(maximum_count):,}",
        )

        first_seen = selected["Date"].min()
        last_seen = selected["Date"].max()
        breeding_records = selected.loc[
            selected["Breeding Code"].notna(), "Submission ID"
        ].nunique()
        media_records = selected.loc[
            selected["ML Catalog Numbers"].notna(), "Submission ID"
        ].nunique()
        note_records = selected.loc[
            selected["Observation Details"].notna(), "Submission ID"
        ].nunique()
        st.caption(
            f"First seen {_display_date(first_seen)} · Latest {_display_date(last_seen)} · "
            f"{selected['State/Province'].nunique():,} state/province regions · "
            f"{breeding_records:,} checklists with breeding codes · "
            f"{media_records:,} with media · {note_records:,} with observation notes"
        )

        if summary is not None and int(summary["Presence-only Records"]) > 0:
            st.caption(
                f"{int(summary['Presence-only Records']):,} presence-only records are "
                "included; they do not contribute to exact individual totals."
            )
        if taxonomy_record is not None and pd.notna(
            taxonomy_record.get("Species Code")
        ):
            species_code = str(taxonomy_record["Species Code"])
            st.link_button(
                "Open this species on eBird",
                f"https://ebird.org/species/{species_code}",
            )
            codes = taxonomy_record.get("Banding Codes")
            code_text = f"eBird code: {species_code}"
            if pd.notna(codes) and str(codes):
                code_text += f" · Banding code: {codes}"
            st.caption(code_text)


def _render_world_atlas(
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    species_summary: pd.DataFrame,
    mode: str,
    species_name: str | None,
    key_prefix: str,
) -> None:
    catalog = _world_catalog()
    species_modes = {"Species presence", "Species encounter rate"}

    if mode == "Species presence":
        selected = observations.loc[observations["Common Name"] == species_name]
        visited_codes = set(checklists["Country Code"])
        seen_codes = set(selected["Country Code"])
        map_data = catalog.copy()
        map_data["Status"] = "Never birded"
        map_data.loc[map_data["Country Code"].isin(visited_codes), "Status"] = (
            "Birded — not seen"
        )
        map_data.loc[map_data["Country Code"].isin(seen_codes), "Status"] = "Seen"
        figure = px.choropleth(
            map_data,
            locations="ISO3",
            locationmode="ISO-3",
            color="Status",
            hover_name="Country",
            hover_data={"ISO3": False, "Country Code": False},
            category_orders={"Status": ["Never birded", "Birded — not seen", "Seen"]},
            color_discrete_map=ATLAS_STATUS_COLORS,
            projection="natural earth",
            title=f"{species_name}: country presence",
            height=720,
        )
        figure.update_geos(
            showframe=False,
            showcoastlines=False,
            showcountries=True,
            bgcolor="rgba(0,0,0,0)",
        )
        figure.update_layout(
            margin={"l": 0, "r": 0, "t": 45, "b": 0},
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
        )
    elif mode == "Species encounter rate":
        complete = checklists.loc[checklists["Complete Checklist"]]
        denominators = complete.groupby("Country Code")["Submission ID"].nunique()
        complete_ids = set(complete["Submission ID"])
        detections = observations.loc[
            (observations["Common Name"] == species_name)
            & observations["Submission ID"].isin(complete_ids)
        ]
        numerators = detections.groupby("Country Code")["Submission ID"].nunique()
        metrics = denominators.rename("Complete Checklists").reset_index()
        metrics["Detections"] = (
            metrics["Country Code"].map(numerators).fillna(0).astype(int)
        )
        metrics["Encounter Rate"] = (
            metrics["Detections"] / metrics["Complete Checklists"]
        )
        figure, map_data = _numeric_world_map(
            catalog,
            metrics,
            "Encounter Rate",
            f"{species_name}: detections per complete checklist by country",
        )
        figure.update_coloraxes(colorbar_tickformat=".0%")
    elif mode == "Species richness":
        metrics = observations.groupby("Country Code", as_index=False).agg(
            **{"Species Richness": ("Taxon Key", "nunique")}
        )
        figure, map_data = _numeric_world_map(
            catalog, metrics, "Species Richness", "Personal species richness by country"
        )
    elif mode == "Checklists":
        metrics = checklists.groupby("Country Code", as_index=False).agg(
            Checklists=("Submission ID", "nunique")
        )
        figure, map_data = _numeric_world_map(
            catalog, metrics, "Checklists", "Checklists by country"
        )
    else:
        first_locations = species_summary[["Taxon Key", "First Location ID"]].rename(
            columns={"First Location ID": "Location ID"}
        )
        lifers = first_locations.merge(
            checklists[["Location ID", "Country Code"]].drop_duplicates("Location ID"),
            on="Location ID",
            how="inner",
        )
        metrics = lifers.groupby("Country Code", as_index=False).agg(
            Lifers=("Taxon Key", "nunique")
        )
        figure, map_data = _numeric_world_map(
            catalog, metrics, "Lifers", "First sightings by country"
        )

    st.plotly_chart(figure, width="stretch")
    st.caption(
        "All uploaded countries are included. Choose a specific country in Atlas scope "
        "to open its state/province and county/local-region views."
    )

    visited = (
        checklists.groupby("Country Code", as_index=False)
        .agg(Checklists=("Submission ID", "nunique"))
        .assign(Country=lambda frame: frame["Country Code"].map(_country_name))
        .sort_values("Country")
    )
    selected_country = st.selectbox(
        "Inspect a visited country",
        visited["Country Code"].tolist(),
        format_func=_country_name,
        key=f"{key_prefix}_world_detail",
    )
    if mode in species_modes:
        detail = observations.loc[
            (observations["Country Code"] == selected_country)
            & (observations["Common Name"] == species_name),
            ["Date", "Common Name", "Count", "Location", "Submission ID"],
        ].drop_duplicates(["Submission ID", "Common Name"])
        if detail.empty:
            st.info(f"No {species_name} records in {_country_name(selected_country)}.")
        else:
            st.dataframe(
                detail.sort_values("Date", ascending=False),
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": st.column_config.DateColumn(format="YYYY-MM-DD")
                },
            )
    else:
        country_observations = observations.loc[
            observations["Country Code"] == selected_country
        ]
        top_species = (
            country_observations.groupby("Common Name", as_index=False)["Submission ID"]
            .nunique()
            .rename(columns={"Submission ID": "Checklists"})
            .sort_values(["Checklists", "Common Name"], ascending=[False, True])
            .head(20)
        )
        st.dataframe(top_species, hide_index=True, width="stretch")


def _render_county_atlas(
    all_observations: pd.DataFrame,
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    species_summary: pd.DataFrame,
    file_hash: str,
    key_prefix: str,
    fixed_species: str | None = None,
    fixed_mode: str | None = None,
) -> None:
    country_code = _country_selector(checklists, f"{key_prefix}_country")
    if country_code == ALL_COUNTRIES:
        geographic_level = "Countries"
    else:
        geographic_level = st.radio(
            "Geographic level",
            ["States / provinces", "Counties / local regions"],
            horizontal=True,
            key=f"{key_prefix}_geographic_level",
        )
    species_modes = {"Species presence", "Species encounter rate"}
    if fixed_mode is not None:
        mode = fixed_mode
    elif fixed_species is None:
        mode = st.selectbox(
            "Atlas metric",
            [
                "Species presence",
                "Species encounter rate",
                "Species richness",
                "Checklists",
                "Lifers",
            ],
            key=f"{key_prefix}_mode",
        )
    else:
        mode = "Species presence"

    species_name = fixed_species
    if mode in species_modes and species_name is None:
        species_name = st.selectbox(
            "Species",
            sorted(observations["Common Name"].dropna().unique()),
            key=f"{key_prefix}_species",
        )

    if mode in species_modes and species_name is not None:
        _render_species_information(species_name, observations, species_summary)

    if country_code == ALL_COUNTRIES:
        _render_world_atlas(
            observations,
            checklists,
            species_summary,
            mode,
            species_name,
            key_prefix,
        )
        return

    try:
        with st.spinner(
            "Loading state/province boundaries and assigning eBird locations…"
        ):
            (
                state_bundle,
                state_mapped_checklists,
                _,
                state_mapped_observations,
            ) = _get_boundary_context(
                country_code,
                all_observations,
                observations,
                checklists,
                file_hash,
                "ADM1",
            )
    except BoundaryError as exc:
        st.error(f"State/province boundaries are unavailable: {exc}")
        st.info(
            "The location and species point maps remain available without boundary data."
        )
        return

    country_checklists = checklists.loc[checklists["Country Code"] == country_code]
    scope_checklists = country_checklists
    selected_parent_name: str | None = None

    if geographic_level == "States / provinces":
        bundle = state_bundle
        mapped_checklists = state_mapped_checklists
        mapped_observations = state_mapped_observations
        boundary_label = "state / province"
        boundary_plural = "states / provinces"
    else:
        visited_states = (
            state_mapped_checklists[["Boundary ID", "Boundary Name"]]
            .drop_duplicates()
            .sort_values("Boundary Name")
        )
        if visited_states.empty:
            st.info(
                "No visited state or province could be identified for this country."
            )
            return
        state_names = visited_states.set_index("Boundary ID")["Boundary Name"].to_dict()
        selected_parent_id = st.selectbox(
            "State / province",
            visited_states["Boundary ID"].tolist(),
            format_func=lambda boundary_id: state_names[boundary_id],
            key=f"{key_prefix}_parent_boundary",
        )
        selected_parent_name = state_names[selected_parent_id]
        selected_location_ids = set(
            state_mapped_checklists.loc[
                state_mapped_checklists["Boundary ID"] == selected_parent_id,
                "Location ID",
            ]
        )
        scope_checklists = state_mapped_checklists.loc[
            state_mapped_checklists["Location ID"].isin(selected_location_ids)
        ]
        try:
            with st.spinner(
                f"Loading county/local boundaries for {selected_parent_name}…"
            ):
                (
                    full_child_bundle,
                    full_child_checklists,
                    _,
                    full_child_observations,
                ) = _get_boundary_context(
                    country_code,
                    all_observations,
                    observations,
                    checklists,
                    file_hash,
                    "ADM2",
                )
                bundle = subset_boundary_bundle(
                    full_child_bundle, state_bundle, selected_parent_id
                )
        except BoundaryError as exc:
            st.error(f"County/local boundaries are unavailable: {exc}")
            return

        child_ids = set(boundary_catalog(bundle)["Boundary ID"])

        def in_selected_parent(frame: pd.DataFrame) -> pd.DataFrame:
            return frame.loc[
                frame["Location ID"].isin(selected_location_ids)
                & frame["Boundary ID"].isin(child_ids)
            ]

        mapped_checklists = in_selected_parent(full_child_checklists)
        mapped_observations = in_selected_parent(full_child_observations)
        boundary_label = "county / local region"
        boundary_plural = "counties / local regions"

    mapped_count = mapped_checklists["Location ID"].nunique()
    total_count = scope_checklists["Location ID"].nunique()
    if mapped_count < total_count:
        scope_text = (
            f" in {selected_parent_name}" if selected_parent_name is not None else ""
        )
        st.warning(
            f"Mapped {mapped_count:,} of {total_count:,} locations{scope_text} to "
            f"{bundle.admin_level} {boundary_plural}. Pelagic or border locations may "
            "remain unassigned."
        )

    catalog = boundary_catalog(bundle)
    if geographic_level == "States / provinces":
        center, zoom = _boundary_map_view(bundle, set(mapped_checklists["Boundary ID"]))
    else:
        center, zoom = _boundary_map_view(bundle)

    if mode == "Species presence":
        selected = mapped_observations.loc[
            mapped_observations["Common Name"] == species_name
        ]
        visited_ids = set(mapped_checklists["Boundary ID"])
        seen_ids = set(selected["Boundary ID"])
        map_data = catalog.copy()
        map_data["Status"] = "Never birded"
        map_data.loc[map_data["Boundary ID"].isin(visited_ids), "Status"] = (
            "Birded — not seen"
        )
        map_data.loc[map_data["Boundary ID"].isin(seen_ids), "Status"] = "Seen"
        figure = px.choropleth_map(
            map_data,
            geojson=bundle.geojson,
            locations="Boundary ID",
            featureidkey="id",
            color="Status",
            hover_name="Boundary Name",
            category_orders={"Status": ["Never birded", "Birded — not seen", "Seen"]},
            color_discrete_map=ATLAS_STATUS_COLORS,
            map_style="carto-positron",
            center=center,
            zoom=zoom,
            opacity=0.78,
            title=f"{species_name}: {boundary_label} presence",
            height=720,
        )
        figure.update_layout(margin={"l": 0, "r": 0, "t": 45, "b": 0})
    elif mode == "Species encounter rate":
        complete = mapped_checklists.loc[mapped_checklists["Complete Checklist"]]
        denominators = complete.groupby("Boundary ID")["Submission ID"].nunique()
        complete_ids = set(complete["Submission ID"])
        detections = mapped_observations.loc[
            (mapped_observations["Common Name"] == species_name)
            & mapped_observations["Submission ID"].isin(complete_ids)
        ]
        numerators = detections.groupby("Boundary ID")["Submission ID"].nunique()
        metrics = denominators.rename("Complete Checklists").reset_index()
        metrics["Detections"] = (
            metrics["Boundary ID"].map(numerators).fillna(0).astype(int)
        )
        metrics["Encounter Rate"] = (
            metrics["Detections"] / metrics["Complete Checklists"]
        )
        figure, map_data = _numeric_boundary_map(
            catalog,
            metrics,
            bundle,
            "Encounter Rate",
            f"{species_name}: detections per complete checklist",
            center,
            zoom,
        )
        figure.update_coloraxes(colorbar_tickformat=".0%")
    elif mode == "Species richness":
        metrics = mapped_observations.groupby("Boundary ID", as_index=False).agg(
            **{"Species Richness": ("Taxon Key", "nunique")}
        )
        figure, map_data = _numeric_boundary_map(
            catalog,
            metrics,
            bundle,
            "Species Richness",
            f"Personal species richness by {boundary_label}",
            center,
            zoom,
        )
    elif mode == "Checklists":
        metrics = mapped_checklists.groupby("Boundary ID", as_index=False).agg(
            Checklists=("Submission ID", "nunique")
        )
        figure, map_data = _numeric_boundary_map(
            catalog,
            metrics,
            bundle,
            "Checklists",
            f"Checklists by {boundary_label}",
            center,
            zoom,
        )
    else:
        first_locations = species_summary[["Taxon Key", "First Location ID"]].rename(
            columns={"First Location ID": "Location ID"}
        )
        lifers = first_locations.merge(
            mapped_checklists[["Location ID", "Boundary ID"]].drop_duplicates(
                "Location ID"
            ),
            on="Location ID",
            how="inner",
        )
        metrics = lifers.groupby("Boundary ID", as_index=False).agg(
            Lifers=("Taxon Key", "nunique")
        )
        figure, map_data = _numeric_boundary_map(
            catalog,
            metrics,
            bundle,
            "Lifers",
            f"First sightings by {boundary_label}",
            center,
            zoom,
        )

    st.plotly_chart(figure, width="stretch")
    st.caption(f"{bundle.attribution} Geographic level: {bundle.admin_level}.")
    if geographic_level == "States / provinces":
        st.caption(
            "Choose Counties / local regions above to select a state or province and "
            "zoom into its local boundaries."
        )

    visited_names = (
        mapped_checklists[["Boundary ID", "Boundary Name"]]
        .drop_duplicates()
        .sort_values("Boundary Name")
    )
    if visited_names.empty:
        return
    selected_boundary_name = st.selectbox(
        f"Inspect a visited {boundary_label}",
        visited_names["Boundary Name"].tolist(),
        key=f"{key_prefix}_{bundle.admin_level}_boundary_detail",
    )
    selected_boundary_id = visited_names.loc[
        visited_names["Boundary Name"] == selected_boundary_name, "Boundary ID"
    ].iloc[0]
    if mode in species_modes:
        detail = mapped_observations.loc[
            (mapped_observations["Boundary ID"] == selected_boundary_id)
            & (mapped_observations["Common Name"] == species_name),
            ["Date", "Common Name", "Count", "Location", "Submission ID"],
        ].drop_duplicates(["Submission ID", "Common Name"])
        if detail.empty:
            st.info(f"No {species_name} records in {selected_boundary_name}.")
        else:
            st.dataframe(
                detail.sort_values("Date", ascending=False),
                hide_index=True,
                width="stretch",
                column_config={
                    "Date": st.column_config.DateColumn(format="YYYY-MM-DD")
                },
            )
    else:
        boundary_observations = mapped_observations.loc[
            mapped_observations["Boundary ID"] == selected_boundary_id
        ]
        top_species = (
            boundary_observations.groupby("Common Name", as_index=False)[
                "Submission ID"
            ]
            .nunique()
            .rename(columns={"Submission ID": "Checklists"})
            .sort_values(["Checklists", "Common Name"], ascending=[False, True])
            .head(20)
        )
        st.dataframe(top_species, hide_index=True, width="stretch")


def _render_location_map(observations: pd.DataFrame, checklists: pd.DataFrame) -> None:
    st.subheader("Location map")
    years = sorted(checklists["Year"].dropna().astype(int).unique())
    selected_years = st.sidebar.multiselect(
        "Years", years, default=years, key="map_years"
    )
    locations = build_location_summary(observations, checklists, selected_years)
    locations = locations.dropna(subset=["Latitude", "Longitude"])
    if locations.empty:
        st.warning("No mapped locations are available for that selection.")
        return
    locations["Log Visits"] = np.log10(locations["Location Visits"].clip(lower=1))
    center, zoom = _map_view(locations["Latitude"], locations["Longitude"])
    figure = px.scatter_map(
        locations,
        lat="Latitude",
        lon="Longitude",
        color="Log Visits",
        size="Unique Taxa",
        hover_name="Location",
        hover_data={
            "Unique Taxa": True,
            "Location Visits": True,
            "Log Visits": False,
            "Latitude": False,
            "Longitude": False,
        },
        center=center,
        zoom=zoom,
        height=760,
        color_continuous_scale="Viridis",
        map_style="open-street-map",
        title="Species richness and visits by eBird location",
    )
    st.plotly_chart(figure, width="stretch")


def _render_lifers(
    all_observations: pd.DataFrame,
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    species_summary: pd.DataFrame,
    file_hash: str,
) -> None:
    st.subheader("Lifers over time")
    latest = species_summary.sort_values("First Seen", ascending=False).iloc[0]
    col1, col2, col3 = st.columns(3)
    col1.metric("Life list", f"{len(species_summary):,}")
    col2.metric("Latest lifer", str(latest["Common Name"]))
    col3.metric("Latest lifer date", _display_date(latest["First Seen"]))

    figure = px.line(
        species_summary,
        x="First Seen",
        y="Lifer Count",
        markers=True,
        hover_data={
            "Common Name": True,
            "First Seen": "|%Y-%m-%d",
            "First Location": True,
            "Lifer Count": True,
        },
        labels={"First Seen": "Date", "Lifer Count": "Cumulative lifers"},
        title="Cumulative first sightings",
        height=520,
    )
    st.plotly_chart(figure, width="stretch")

    st.subheader("Most recent lifers")
    st.dataframe(
        species_summary.sort_values("First Seen", ascending=False)[
            ["Common Name", "Scientific Name", "First Seen", "First Location"]
        ].head(20),
        hide_index=True,
        width="stretch",
        column_config={"First Seen": st.column_config.DateColumn(format="YYYY-MM-DD")},
    )

    st.subheader("Lifer geography")
    map_mode = st.radio(
        "Map coloring",
        ["Lifers", "Species richness"],
        horizontal=True,
        key="lifers_map_mode",
    )
    _render_county_atlas(
        all_observations,
        observations,
        checklists,
        species_summary,
        file_hash,
        key_prefix="lifers_atlas",
        fixed_mode=map_mode,
    )


def _render_by_year(observations: pd.DataFrame) -> None:
    st.subheader("Species accumulation by calendar date")
    annual = build_annual_accumulation(observations)
    figure = go.Figure()
    for year, year_data in annual.groupby("Year"):
        figure.add_trace(
            go.Scatterpolar(
                r=year_data["Species Count"],
                theta=year_data["Theta"],
                mode="lines",
                name=str(year),
                line={"width": 4},
                customdata=np.stack(
                    [year_data["Date Label"], year_data["Calendar Day"]], axis=-1
                ),
                hovertemplate=(
                    "%{customdata[0]}<br>Species: %{r}<br>Calendar day: "
                    "%{customdata[1]}<extra>%{fullData.name}</extra>"
                ),
            )
        )
    month_starts = pd.date_range("2000-01-01", periods=12, freq="MS").dayofyear
    figure.update_layout(
        polar={
            "radialaxis": {"title": "Species count"},
            "angularaxis": {
                "rotation": 90,
                "direction": "clockwise",
                "tickmode": "array",
                "tickvals": (month_starts - 1) / 366 * 360,
                "ticktext": [month[:3] for month in MONTH_ORDER],
            },
        },
        height=760,
        title="Species seen by calendar date",
    )
    st.plotly_chart(figure, width="stretch")

    monthly = build_monthly_average(observations)
    monthly_figure = px.line(
        monthly,
        x="Month Name",
        y="Average Species",
        markers=True,
        category_orders={"Month Name": MONTH_ORDER},
        hover_data={"Months Included": True},
        labels={"Average Species": "Average unique taxa", "Month Name": "Month"},
        title="Average monthly richness, including inactive in-range months as zero",
        height=480,
    )
    st.plotly_chart(monthly_figure, width="stretch")


def _render_stats(
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    species_summary: pd.DataFrame,
) -> None:
    daily = observations.groupby("Date", as_index=False).agg(
        **{"Unique Taxa": ("Taxon Key", "nunique")}
    )
    biggest = daily.sort_values(["Unique Taxa", "Date"], ascending=[False, True]).iloc[
        0
    ]
    total_distance = checklists["Distance Traveled (km)"].sum(min_count=1)
    total_duration = checklists["Duration (Min)"].sum(min_count=1)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Taxa", f"{observations['Taxon Key'].nunique():,}")
    col2.metric("Checklists", f"{checklists['Submission ID'].nunique():,}")
    col3.metric("Birding days", f"{checklists['Date'].nunique():,}")
    col4.metric("Locations", f"{checklists['Location ID'].nunique():,}")
    distance_value = 0.0 if pd.isna(total_distance) else float(total_distance)
    duration_value = 0.0 if pd.isna(total_duration) else float(total_duration)
    st.caption(
        f"Biggest day: {_display_date(biggest['Date'])} "
        f"({int(biggest['Unique Taxa']):,} taxa) · Recorded checklist distance: "
        f"{distance_value:,.1f} km · Recorded effort: {duration_value / 60:,.1f} hours"
    )

    table1, table2 = st.columns(2)
    with table1:
        st.subheader("Most frequently reported")
        st.dataframe(
            species_summary.sort_values(
                ["Checklist Count", "Common Name"], ascending=[False, True]
            )[["Common Name", "Checklist Count", "Exact Individuals"]].head(10),
            hide_index=True,
            width="stretch",
        )
    with table2:
        st.subheader("Least frequently reported")
        st.dataframe(
            species_summary.sort_values(
                ["Checklist Count", "Common Name"], ascending=[True, True]
            )[["Common Name", "Checklist Count", "Last Seen"]].head(10),
            hide_index=True,
            width="stretch",
            column_config={
                "Last Seen": st.column_config.DateColumn(format="YYYY-MM-DD")
            },
        )

    st.subheader("Birding chronotype")
    chronotype = build_chronotype(observations, checklists)
    if chronotype.empty:
        st.info("No valid checklist start times are available.")
        return
    figure = px.bar_polar(
        chronotype,
        r="Checklists",
        theta="Theta",
        color="Average Species Per Checklist",
        hover_data={
            "Hour Label": True,
            "Checklists": True,
            "Average Species Per Checklist": ":.1f",
            "Median Species Per Checklist": ":.1f",
            "Theta": False,
        },
        color_continuous_scale="Plasma_r",
        title="Actual checklist starts by time of day",
        height=650,
    )
    figure.update_layout(
        polar={
            "angularaxis": {
                "rotation": 180,
                "direction": "clockwise",
                "tickmode": "array",
                "tickvals": [(hour / 24) * 360 for hour in range(0, 24, 3)],
                "ticktext": [f"{hour:02d}:00" for hour in range(0, 24, 3)],
            }
        }
    )
    st.plotly_chart(figure, width="stretch")


def _render_species_explorer(
    all_observations: pd.DataFrame,
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    species_summary: pd.DataFrame,
    file_hash: str,
) -> None:
    species_name = st.selectbox(
        "Choose a species",
        sorted(observations["Common Name"].dropna().unique()),
        key="explorer_species",
    )
    selected = observations.loc[observations["Common Name"] == species_name]
    selected_ids = set(selected["Submission ID"])
    summary_row = species_summary.loc[
        species_summary["Common Name"] == species_name
    ].iloc[0]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("First seen", _display_date(summary_row["First Seen"]))
    col2.metric("Latest", _display_date(summary_row["Last Seen"]))
    col3.metric("Checklists", f"{int(summary_row['Checklist Count']):,}")
    col4.metric("Locations", f"{int(summary_row['Location Count']):,}")

    overview_tab, county_tab = st.tabs(["Profile", "Atlas"])
    with overview_tab:
        locations = selected.groupby("Location ID", as_index=False).agg(
            Location=("Location", "first"),
            Latitude=("Latitude", "first"),
            Longitude=("Longitude", "first"),
            Checklists=("Submission ID", "nunique"),
            **{
                "First Seen": ("Date", "min"),
                "Last Seen": ("Date", "max"),
                "Maximum Count": ("Count Numeric", "max"),
            },
        )
        mapped_locations = locations.dropna(subset=["Latitude", "Longitude"])
        if not mapped_locations.empty:
            center, zoom = _map_view(
                mapped_locations["Latitude"], mapped_locations["Longitude"]
            )
            map_figure = px.scatter_map(
                mapped_locations,
                lat="Latitude",
                lon="Longitude",
                size="Checklists",
                color="Checklists",
                hover_name="Location",
                hover_data={
                    "First Seen": "|%Y-%m-%d",
                    "Last Seen": "|%Y-%m-%d",
                    "Maximum Count": True,
                    "Latitude": False,
                    "Longitude": False,
                },
                map_style="open-street-map",
                center=center,
                zoom=zoom,
                height=600,
                title=f"{species_name} locations",
                color_continuous_scale="Viridis",
            )
            st.plotly_chart(map_figure, width="stretch")

        complete = checklists.loc[checklists["Complete Checklist"]].copy()
        complete_selected_ids = selected_ids.intersection(complete["Submission ID"])
        month_denominator = complete.groupby("Month")["Submission ID"].nunique()
        month_numerator = (
            complete.loc[complete["Submission ID"].isin(complete_selected_ids)]
            .groupby("Month")["Submission ID"]
            .nunique()
        )
        phenology = pd.DataFrame({"Month": range(1, 13)})
        phenology["Complete Checklists"] = (
            phenology["Month"].map(month_denominator).fillna(0)
        )
        phenology["Detections"] = phenology["Month"].map(month_numerator).fillna(0)
        phenology["Encounter Rate"] = np.where(
            phenology["Complete Checklists"] > 0,
            phenology["Detections"] / phenology["Complete Checklists"],
            np.nan,
        )
        phenology["Month Name"] = [month[:3] for month in MONTH_ORDER]
        phenology_figure = go.Figure()
        phenology_figure.add_bar(
            x=phenology["Month Name"],
            y=phenology["Detections"],
            name="Detections",
        )
        phenology_figure.add_scatter(
            x=phenology["Month Name"],
            y=phenology["Encounter Rate"],
            name="Encounter rate",
            mode="lines+markers",
            yaxis="y2",
            hovertemplate="%{y:.1%}<extra>Encounter rate</extra>",
        )
        phenology_figure.update_layout(
            title=f"{species_name} seasonality",
            yaxis={"title": "Checklists with detection"},
            yaxis2={
                "title": "Detections / complete checklists",
                "overlaying": "y",
                "side": "right",
                "tickformat": ".0%",
                "rangemode": "tozero",
            },
            legend={"orientation": "h"},
            height=480,
        )
        st.plotly_chart(phenology_figure, width="stretch")

        st.subheader("Top locations")
        st.dataframe(
            locations.sort_values(["Checklists", "Location"], ascending=[False, True])[
                ["Location", "Checklists", "First Seen", "Last Seen", "Maximum Count"]
            ].head(20),
            hide_index=True,
            width="stretch",
            column_config={
                "First Seen": st.column_config.DateColumn(format="YYYY-MM-DD"),
                "Last Seen": st.column_config.DateColumn(format="YYYY-MM-DD"),
            },
        )

    with county_tab:
        _render_county_atlas(
            all_observations,
            observations,
            checklists,
            species_summary,
            file_hash,
            key_prefix="species_county",
            fixed_species=species_name,
        )


def _render_rediscovery_and_needs(
    observations: pd.DataFrame,
    checklists: pd.DataFrame,
    species_summary: pd.DataFrame,
) -> None:
    rediscovery_tab, needs_tab = st.tabs(["Rediscovery", "Recent regional needs"])
    with rediscovery_tab:
        category = st.selectbox(
            "Show",
            [
                "Only seen once",
                "Not seen this year",
                "Not seen in at least a year",
                "All taxa by last sighting",
            ],
            key="rediscovery_category",
        )
        current_year = pd.Timestamp.today().year
        if category == "Only seen once":
            candidates = species_summary.loc[species_summary["Checklist Count"] == 1]
        elif category == "Not seen this year":
            candidates = species_summary.loc[
                species_summary["Last Seen"].dt.year < current_year
            ]
        elif category == "Not seen in at least a year":
            candidates = species_summary.loc[species_summary["Days Since Seen"] >= 365]
        else:
            candidates = species_summary
        candidates = candidates.sort_values(
            ["Days Since Seen", "Checklist Count", "Common Name"],
            ascending=[False, True, True],
        )
        st.metric("Rediscovery candidates", f"{len(candidates):,}")
        st.dataframe(
            candidates[
                [
                    "Common Name",
                    "Scientific Name",
                    "Last Seen",
                    "Days Since Seen",
                    "Checklist Count",
                    "Last Location",
                ]
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "Last Seen": st.column_config.DateColumn(format="YYYY-MM-DD")
            },
        )

    with needs_tab:
        st.write(
            "Compare your uploaded history with public recent observations. The API key is "
            "used only for this request and does not provide access to your personal eBird data."
        )
        region_options = sorted(checklists["State/Province"].dropna().unique())
        api_key = st.text_input("eBird API key", type="password", key="ebird_api_key")
        region_code = st.selectbox("Region", region_options, key="needs_region")
        back = st.slider("Days back", 1, 30, 14, key="needs_back")
        scope = st.radio(
            "Find species missing from",
            ["Life list", f"{pd.Timestamp.today().year} list"],
            horizontal=True,
            key="needs_scope",
        )
        if st.button("Find recent needs", type="primary"):
            try:
                with st.spinner("Checking recent public eBird observations…"):
                    recent = fetch_recent_observations(api_key, region_code, back=back)
                if scope == "Life list":
                    owned = set(observations["Scientific Name"].dropna())
                else:
                    owned = set(
                        observations.loc[
                            observations["Year"] == pd.Timestamp.today().year,
                            "Scientific Name",
                        ].dropna()
                    )
                needs = recent.loc[~recent["Scientific Name"].isin(owned)].copy()
                st.session_state["recent_needs"] = needs
                st.session_state["recent_needs_region"] = region_code
            except EbirdAPIError as exc:
                st.error(str(exc))
        needs = st.session_state.get("recent_needs")
        if isinstance(needs, pd.DataFrame):
            st.metric("Recent needs", f"{needs['Scientific Name'].nunique():,}")
            if needs.empty:
                st.success(
                    "No missing species were found in the returned observations."
                )
            else:
                mapped = needs.dropna(subset=["Latitude", "Longitude"])
                if not mapped.empty:
                    center, zoom = _map_view(mapped["Latitude"], mapped["Longitude"])
                    figure = px.scatter_map(
                        mapped,
                        lat="Latitude",
                        lon="Longitude",
                        color="Common Name",
                        hover_name="Common Name",
                        hover_data={
                            "Location": True,
                            "Observed At": "|%Y-%m-%d %H:%M",
                            "Count": True,
                            "Latitude": False,
                            "Longitude": False,
                        },
                        map_style="open-street-map",
                        center=center,
                        zoom=zoom,
                        height=580,
                        title=(
                            "Recent needs in "
                            f"{st.session_state.get('recent_needs_region', '')}"
                        ),
                    )
                    figure.update_layout(showlegend=False)
                    st.plotly_chart(figure, width="stretch")
                st.dataframe(
                    needs[
                        [
                            "Common Name",
                            "Scientific Name",
                            "Location",
                            "Observed At",
                            "Count",
                        ]
                    ],
                    hide_index=True,
                    width="stretch",
                )


st.title("eBird Data Dive")
st.caption("A private, session-scoped explorer for your My eBird Data export.")

with st.expander("Get or select your eBird data", expanded=True):
    st.link_button("Download My Data from eBird", "https://ebird.org/downloadMyData")
    uploaded_file = st.file_uploader(
        "Upload the CSV supplied by eBird",
        type=["csv"],
        max_upload_size=50,
        help="The file is processed in this Streamlit session and is not written to disk.",
    )

if uploaded_file is None:
    st.info(
        "Download your personal data from eBird, then drop the CSV above. The application "
        "does not ask for or store your eBird password."
    )
    st.stop()

file_bytes = uploaded_file.getvalue()
file_hash = hashlib.sha256(file_bytes).hexdigest()
if st.session_state.get("ebird_file_hash") != file_hash:
    try:
        prepared = load_and_prepare_csv(file_bytes)
    except DataValidationError as exc:
        st.error(str(exc))
        st.stop()
    st.session_state["ebird_file_hash"] = file_hash
    st.session_state["ebird_prepared_data"] = prepared
    st.session_state["boundary_assignment_cache"] = {}
    st.session_state.pop("recent_needs", None)

prepared = st.session_state["ebird_prepared_data"]
all_observations = prepared.observations
for warning in prepared.warnings:
    st.warning(warning)

include_ambiguous = st.sidebar.toggle(
    "Include ambiguous taxa",
    value=False,
    help="Include slash taxa, spuhs, and hybrids in species-like totals.",
)
observations = filter_taxa(all_observations, include_ambiguous)
checklists = build_checklists(all_observations)
species_summary = build_species_summary(observations)

if observations.empty:
    st.error("No observations remain under the selected taxon policy.")
    st.stop()

view = st.sidebar.radio(
    "View",
    [
        "Personal Atlas",
        "Species Explorer",
        "Rediscovery & Needs",
        "Location Map",
        "Lifers",
        "By Year",
        "Stats",
    ],
)
st.sidebar.caption(
    f"{checklists['Submission ID'].nunique():,} checklists · "
    f"{observations['Taxon Key'].nunique():,} taxa · "
    f"{checklists['Location ID'].nunique():,} locations"
)

if view == "Personal Atlas":
    st.subheader("Personal geographic atlas")
    st.write(
        "Start with states or provinces, then zoom into a selected area's counties or "
        "local regions. Compare species presence, richness, effort, and lifers at either "
        "level."
    )
    _render_county_atlas(
        all_observations,
        observations,
        checklists,
        species_summary,
        file_hash,
        key_prefix="atlas",
    )
elif view == "Species Explorer":
    _render_species_explorer(
        all_observations,
        observations,
        checklists,
        species_summary,
        file_hash,
    )
elif view == "Rediscovery & Needs":
    _render_rediscovery_and_needs(observations, checklists, species_summary)
elif view == "Location Map":
    _render_location_map(observations, checklists)
elif view == "Lifers":
    _render_lifers(
        all_observations,
        observations,
        checklists,
        species_summary,
        file_hash,
    )
elif view == "By Year":
    _render_by_year(observations)
else:
    _render_stats(observations, checklists, species_summary)
