# this script creates some basic interactive visualizations for your birding data
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import numpy as np

np.set_printoptions(legacy='1.25')  # I hate the np.int64() display option


# specify path to eBird data, cache to avoid streamlit reloading data everytime a change is made


@st.cache_data
def load_data():
    loadDF = pd.read_csv("data/MyEBirdData.csv")
    loadDF['Date'] = pd.to_datetime(loadDF['Date'])
    return loadDF


df = load_data()

# Add day-of-year and year columns
df['Year'] = df['Date'].dt.year
df['DayOfYear'] = df['Date'].dt.dayofyear

st.title("eBird Data Dive")

# Sidebar filters
# Sidebar species selector
all_species = sorted(df['Common Name'].unique())
selected_species = st.sidebar.multiselect("Select Species", all_species, default=all_species)

selected_years = st.sidebar.multiselect("Select Year", df['Date'].dt.year.unique(), default=df['Date'].dt.year.unique())

# Filtered Data
filtered_df = df[(df['Common Name'].isin(selected_species)) & (df['Date'].dt.year.isin(selected_years))]

# Map of observations
st.subheader(f"Map of sightings")
if filtered_df.empty:
    st.warning("No data available for this selection.")
else:
    fig = px.scatter_map(filtered_df, lat="Latitude", lon="Longitude", hover_name="Location",
                         hover_data=["Date", "Count"], color_discrete_sequence=["blue"], zoom=3, height=500)
    fig.update_layout(mapbox_style="open-street-map")
    st.plotly_chart(fig)

# Time series
first_seen = df.groupby('Common Name')['Date'].min().reset_index()
first_seen = first_seen.sort_values('Date')
first_seen['Lifer Count'] = [i + 1 for i in range(len(all_species))]

st.subheader(f"Lifers Over Time")
fig_lifers = px.line(first_seen, x='Date', y='Lifer Count', markers=True,
                     labels={'Date': 'Date', 'Lifer Count': 'Cumulative Lifers'},
                     hover_data={'Common Name': True, 'Date': True, 'Lifer Count': True}, title='Lifers Over Time')
st.plotly_chart(fig_lifers)

# Group by date and year to get daily species counts
daily_species = df.groupby(['Year', 'Date']).agg({'Common Name': lambda x: x.nunique()})
daily_species.reset_index(inplace=True)
# daily_species.rename(columns={'Common Name': 'Daily Count'}, inplace=True)
# Recalculate day of year (for unique dates only)
daily_species['DayOfYear'] = daily_species['Date'].dt.dayofyear

test = df[df["Year"] == 2023].sort_values(by="DayOfYear")
years = sorted(daily_species['Year'].unique())

fig = go.Figure()
for year in years:
    year_data = df[df['Year'] == year].sort_values(by="DayOfYear").drop_duplicates('Common Name')
    unqDays = year_data['DayOfYear'].unique()
    year_count = np.cumsum([len(year_data[year_data['DayOfYear'] == i]) for i in unqDays])

    fig.add_trace(go.Scatterpolar(r=year_count, theta=(unqDays / 365) * 360,
                                  mode='lines+markers', name=str(year), line=dict(shape='spline'),
                                  customdata=unqDays, hovertemplate="Day: %{customdata}<br>"
                                                                       + "Species Count: %{r}<br>" +
                                                                       "Year: " + str(year)))
fig.update_layout(
    polar=dict(radialaxis_title="Species Count",
               angularaxis=dict(rotation=90, direction="clockwise", tickmode='array',
                                tickvals=np.linspace(0, 360, 12, endpoint=False),
                                ticktext=['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])),
    title="Species Seen by Day of Year (Polar Plot)",
    showlegend=True)

st.plotly_chart(fig)
