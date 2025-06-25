# this script creates some basic interactive visualizations for your birding data
import streamlit as st
import pandas as pd
import plotly.express as px

# Load your eBird data
@st.cache
def load_data():
    df = pd.read_csv("DMyEBirdData.csv")
    df['Observation Date'] = pd.to_datetime(df['Observation Date'])
    return df

df = load_data()

st.title("🦉 My eBird Dashboard")

# Sidebar filters
species = st.sidebar.selectbox("Select a Species", sorted(df['Common Name'].unique()))
year = st.sidebar.slider("Select Year", int(df['Observation Date'].dt.year.min()), int(df['Observation Date'].dt.year.max()), step=1)

# Filtered Data
filtered_df = df[(df['Common Name'] == species) & (df['Observation Date'].dt.year == year)]

# Map of observations
st.subheader(f"Map of {species} sightings in {year}")
if not filtered_df.empty:
    fig = px.scatter_mapbox(filtered_df,
                            lat="Latitude",
                            lon="Longitude",
                            hover_name="Location",
                            hover_data=["Observation Date", "Count"],
                            color_discrete_sequence=["blue"],
                            zoom=3,
                            height=500)
    fig.update_layout(mapbox_style="open-street-map")
    st.plotly_chart(fig)
else:
    st.warning("No data available for this selection.")

# Time series
st.subheader(f"Sightings over time")
ts_data = filtered_df.groupby(filtered_df['Observation Date'].dt.date).size()
st.line_chart(ts_data)
