# this script creates some basic interactive visualizations for your birding data
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import numpy as np
from collections import Counter
from datetime import datetime
import re

np.set_printoptions(legacy='1.25')  # I hate the np.int64() display option


@st.cache_data
def load_data(file):
    loadDF = pd.read_csv(file)
    loadDF['Date'] = pd.to_datetime(loadDF['Date'])
    return loadDF


# configure page
st.set_page_config(layout="wide")
with st.expander("Select eBird Data File"):
    uploaded_file = st.file_uploader("Upload an eBird csv file", type=["csv"])

if uploaded_file:
    # load file
    df = load_data(uploaded_file)
    # remove / entries to avoid double counts of species in lifer totals
    df = df[[True if '/' not in i else False for i in df['Common Name']]]
    # remove sp. entries because I am only interested in counting species with certain IDs
    df = df[[True if 'sp.' not in i else False for i in df['Common Name']]]
    # remove the ( because this can lead to double counting (i.e. great blue heron and great blue heron (great blue)
    # by the way, what is up with that people? don't submit great blue heron (great blue)
    df['Common Name'] = [i if '(' not in i else re.sub(r' \(.*\)', '', i) for i in df['Common Name']]
    # remove entries where count was 'X'
    df = df[[True if 'X' not in i else False for i in df['Count']]]

    # Add month, month name, day-of-year, and year columns
    df['Month'] = df['Date'].dt.month
    df['Month Name'] = df['Date'].dt.strftime('%B')
    df['Year'] = df['Date'].dt.year
    df['DayOfYear'] = df['Date'].dt.dayofyear
    all_species = sorted(df['Common Name'].unique())

    # organize data by individual birds
    first_seen = df.groupby('Common Name')[['Scientific Name', 'Date', 'Location']].min().reset_index()
    first_seen['total_observed'] = [sum(df[df['Common Name'] == i]['Count'].values.astype(int)) for i in all_species]
    first_seen = first_seen.sort_values('Date')
    first_seen['Lifer Count'] = [i + 1 for i in range(len(all_species))]

    mostSeen = first_seen.sort_values(by=['total_observed', 'Date'], ascending=[False,True])
    mostSeen.index = [first_seen[first_seen['Common Name'] == i]['Lifer Count'].values[0] for i in
                      mostSeen['Common Name']]
    mostSeen.index.name = "Lifer Count"

    st.title("eBird Data Dive")
    # Sidebar filters
    # Sidebar species selector
    view = st.sidebar.radio("View", ["Map", "Lifers", "By Year", "Stats"])
    if view == "Map":
        # selected_species = st.sidebar.multiselect("Select Species", all_species, default=all_species)
        selected_years = st.sidebar.multiselect("Select Year", df['Date'].dt.year.unique(),
                                                default=df['Date'].dt.year.unique())

        # Filtered Data
        filtered_df = df[(df['Date'].dt.year.isin(selected_years))]

        hotspot_species = filtered_df.groupby(['Location', 'Latitude', 'Longitude'])[
            'Common Name'].nunique().reset_index(name='Unique Species Count')
        cls = df.groupby('Submission ID').first()

        tmp = []
        for i, j in zip(hotspot_species['Latitude'], hotspot_species['Longitude']):
            tmp.append(len(cls[(cls['Latitude'] == i) & (cls['Longitude'] == j)]))

        hotspot_species['Log10(Location Visits)'] = np.log10(tmp)
        hotspot_species['Location Visits'] = tmp

        # Map of observations
        st.subheader(f"Map of sightings")
        if filtered_df.empty:
            st.warning("No data available for this selection.")
        else:
            fig = px.scatter_map(hotspot_species, lat='Latitude', lon='Longitude', color='Log10(Location Visits)',
                                 size='Unique Species Count', hover_name='Location',
                                 hover_data={'Unique Species Count': True, 'Location Visits': True}, zoom=3,
                                 height=800, color_continuous_scale='Viridis')
            fig.update_layout(mapbox_style="open-street-map", autosize=True)
            st.plotly_chart(fig)

    elif view == "Lifers":
        # Time series
        st.subheader(f"Lifers Over Time")
        fig_lifers = px.line(first_seen, x='Date', y='Lifer Count', markers=True,
                             labels={'Date': 'Date', 'Lifer Count': 'Cumulative Lifers'},
                             hover_data={'Common Name': True, 'Date': True, 'Lifer Count': True, 'Location': True},
                             title='Lifers Over Time')
        fig_lifers.update_layout(autosize=True, width=100, height=500)
        st.plotly_chart(fig_lifers, use_container_width=True)

    elif view == "By Year":
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
        fig.update_layout(polar=dict(radialaxis_title="Species Count",
                                     angularaxis=dict(rotation=90, direction="clockwise", tickmode='array',
                                                      tickvals=np.linspace(0, 360, 12, endpoint=False),
                                                      ticktext=['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                                                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])),
                          title="Species Seen by Day of Year (Polar Plot)",
                          autosize=True, width=800, height=800, showlegend=True)

        st.plotly_chart(fig)

        monthly_species = (df.groupby(['Year', 'Month'])['Common Name'].nunique().reset_index(name='Species Count'))
        monthly_avg = (monthly_species.groupby('Month')['Species Count'].mean().reset_index())
        monthly_avg['Month Name'] = monthly_avg['Month'].apply(
            lambda x: pd.to_datetime(str(x), format='%m').strftime('%B'))
        monthly_avg = monthly_avg.sort_values('Month')

        st.subheader("Average Species Seen per Calendar Month")

        fig = px.line(monthly_avg, x='Month Name', y='Species Count', markers=True,
                      labels={'Species Count': 'Avg. Unique Species', 'Month Name': 'Month'},
                      title='Average Number of Unique Species per Month')

        fig.update_layout(height=500, xaxis_title='Month', yaxis_title='Species Count')
        st.plotly_chart(fig, use_container_width=True)

    elif view == "Stats":
        birdingDays = df['Date'].unique()
        biggestDay = birdingDays[
            np.argmax(np.array([len(df[df['Date'] == i]['Common Name'].unique()) for i in birdingDays]))]
        BDspecies = len(df[df['Date'] == biggestDay]['Common Name'].unique())

        col1, col2 = st.columns(2)
        with st.container():
            with col1:
                st.header("Most Seen")
                st.table(mostSeen[['Common Name', 'total_observed']][0:5])
            with col2:
                st.header("least Seen")
                st.table(mostSeen[['Common Name', 'total_observed']][-5:])
        with st.container():
            with col1:
                dists = [df[df['Submission ID'] == i]['Distance Traveled (km)'].values[0] for i in
                         df['Submission ID'].unique()]
                st.text(f"You have traveled {np.nansum(dists):.2f} kilometers while birding,"
                        f" that is {np.nansum(dists) / .0035:.2f} wandering albatross wingspans")
                st.text(
                    f"Your biggest day was {biggestDay.year}-{biggestDay.month}-{biggestDay.day} when you saw {BDspecies} unique species!")
                st.text(f"You have gone birding on {len(birdingDays)} days")

            with col2:
                df['Hour'] = pd.to_datetime(df['Time']).dt.hour
                times = df.groupby('Date')['Time'].min()
                timeList = []
                for tm in times:
                    timeList.append(datetime.strptime(tm, '%I:%M %p').hour)

                timeCounts = pd.DataFrame(Counter(timeList).items(), columns=['Hour', 'Checklists']).sort_values('Hour')
                timeCounts['Hour Label'] = timeCounts['Hour'].apply(lambda h: f"{int(h):02d}:00")
                timeCounts['Hour'] = (timeCounts['Hour'].values / 24) * 360
                timeCounts['Unique Species'] = df.groupby('Hour')['Common Name'].nunique().values
                timeCounts['Unique Species Per Checklist'] = timeCounts['Unique Species'] / timeCounts['Checklists']

                fig = px.bar_polar(timeCounts, r="Checklists", theta="Hour", template="plotly_dark",
                                   hover_data={'Hour Label': True, 'Checklists': True, 'Hour': False},
                                   color="Unique Species Per Checklist", color_discrete_sequence=px.colors.sequential.Plasma_r)

                fig.update_layout(
                    polar=dict(angularaxis=dict(rotation=180, direction='clockwise', tickmode='array',
                                                tickvals=[(h / 24) * 360 for h in range(0, 24, 3)],
                                                ticktext=[f"{h:02d}:00" for h in range(0, 24, 3)])),
                                                title="Your Birding Chronotype \n (Checklists by time of day)")
                st.plotly_chart(fig)