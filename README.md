# eBirdTools

Interactive tools for exploring a personal **My eBird Data** CSV export, plus a
separate historical Big Year route-planning project.

## Big Year route planner

The [Big Year project](bigyear/README.md) imports authorized eBird data and
builds auditable historical route experiments. To continue on another computer,
start with the [machine handoff guide](bigyear/docs/MACHINE_HANDOFF.md), then
follow the [2024 EBD import instructions](bigyear/docs/EBD_2024.md).

Source code, tests, configurations, and documentation are versioned here.
Observation downloads, local datasets, saved runs, and compiled tools stay
outside Git; the handoff guide explains which inputs to transfer separately.

## Visualizer features

- Hierarchical personal atlas with state/province overviews and selectable
  county/local-region detail for species presence, encounter rate, richness,
  checklist, and lifer modes
- All-countries atlas overview for exports containing international observations
- Species explorer with sighting maps, seasonality, top locations, eBird taxonomy
  metadata, and the atlas
- Expanded lifer dashboard with recent lifers and richness/lifer geography maps
- Rediscovery lists for one-time and long-unseen taxa
- Optional recent regional "needs" using the public eBird API
- Location, cumulative lifer, annual comparison, monthly, and effort dashboards
- Session-scoped handling of uploaded personal data

The atlas distinguishes regions that have never been birded, regions that have
been birded without a selected species, and regions where that species was
detected. Within a selected country, start with the state/province map, then
switch to county/local-region detail and choose a state or province to zoom in.
International administrative boundaries are loaded from
[geoBoundaries](https://www.geoboundaries.org/) the first time a country or
geographic level is selected.

All atlas choropleths use one yellow-to-orange-to-red intensity scale so the
color meaning remains consistent across presence, richness, lifer, checklist,
and encounter-rate views.

When an export contains more than one country, the atlas starts with a global
country choropleth. Choose a specific country under **Atlas scope** to reach its
state/province and county/local-region views.

## Install and run

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
streamlit run Visualize_Data.py
```

On Windows, activate the environment with `.venv\Scripts\activate`.

In the application, use **Download My Data from eBird** or visit
<https://ebird.org/downloadMyData>, then upload the CSV sent by eBird.

## Optional recent-needs lookup

The Rediscovery & Needs view can compare the uploaded life or current-year list
with public recent observations in an eBird region. This requires a personal
[eBird API key](https://ebird.org/api/keygen). The key is passed to eBird only
when the lookup button is pressed and is not written to disk.

## Privacy and network behavior

- Uploaded CSV data and administrative-boundary assignments are kept in the
  current Streamlit session rather than the shared Streamlit data cache.
- Public administrative boundary files are cached for up to 30 days.
- Public eBird taxonomy metadata is cached for up to 30 days and does not require
  an API key. It supplies taxonomic family, order, species code, and links; the
  personal export remains the source of personal sighting statistics.
- The atlas requires internet access the first time a country's boundaries are loaded.
- A hosted Streamlit server necessarily receives uploaded files for processing;
  review its hosting and retention policy before sharing the application publicly.

## Tests

```bash
python -m unittest discover -s tests -v
```

The regression suite covers presence-only `X` observations, row-associated
first sightings, year-filtered visits, checklist-level chronotype calculations,
leap-year accumulation, CSV validation, and point-in-polygon county assignment.
State/province fallback matching and state-to-county boundary subsetting are
also covered, along with public eBird taxonomy normalization.

## Other tools

`phylo_viewer.py` is an experimental radial phylogenetic life-list view. It has
additional tree-data and Biopython requirements and is not part of the main
visualizer installation yet.
