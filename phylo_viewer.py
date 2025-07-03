# this script creates some basic interactive visualizations for your birding data
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import numpy as np
from Bio import Phylo
import math

np.set_printoptions(legacy='1.25')  # I hate the np.int64() display option


# specify path to eBird data, cache to avoid streamlit reloading data everytime a change is made

def polar_to_cartesian(r, theta):
    return r * math.cos(theta), r * math.sin(theta)


def layout(clade, x=0, y=0, depth=0):
    children = clade.clades
    if not children:
        coords[clade] = (x, y)
        return y + 1
    y_start = y
    for child in children:
        y = layout(child, x + (clade.branch_length or 0.1), y, depth + 1)
    y_mid = (y_start + y - 1) / 2
    coords[clade] = (x, y_mid)
    return y

def radial_layout(clade, depth=0, angle_offset=0):
    if clade in tip_angles:
        angle = tip_angles[clade]
    else:
        children_angles = [radial_layout(c, depth + (clade.branch_length or 0.1)) for c in clade.clades]
        angle = sum(children_angles) / len(children_angles)
    radius = depth + (clade.branch_length or 0.1)
    coords[clade] = (radius, angle)
    return angle


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
    # remove entries where count was 'X'
    df = df[[True if 'X' not in i else False for i in df['Count']]]

    # Add day-of-year and year columns
    df['Year'] = df['Date'].dt.year
    df['DayOfYear'] = df['Date'].dt.dayofyear
    all_species = sorted(df['Common Name'].unique())

    # organize data by individual birds
    first_seen = df.groupby('Common Name')[['Scientific Name', 'Date', 'Location']].min().reset_index()
    first_seen['total_observed'] = [sum(df[df['Common Name'] == i]['Count'].values.astype(int)) for i in all_species]
    first_seen = first_seen.sort_values('Date')
    first_seen['Lifer Count'] = [i + 1 for i in range(len(all_species))]

    mostSeen = first_seen.sort_values(by=['total_observed', 'Common Name'], ascending=False)
    mostSeen.index = [first_seen[first_seen['Common Name'] == i]['Lifer Count'].values[0] for i in
                      mostSeen['Common Name']]
    mostSeen.index.name = "Lifer Count"

    st.title("Your eBird Phylogenetic Tree")
    tree = Phylo.read("data/summary_dated_clements.nex", "nexus")
    life_list = set(df['Scientific Name'].str.replace(" ", "_"))  # Assuming underscores in tree

    # Loop over all tip labels and remove those not in life list
    for tip in tree.get_terminals():
        if tip.name not in life_list:
            tree.prune(target=tip)

    tips = tree.get_terminals()
    n_tips = len(tips)
    tip_angles = {tip: i * 2 * math.pi / n_tips for i, tip in enumerate(tips)}
    # Create layout coordinates
    coords = {}
    radial_layout(tree.root)

    edges = []
    for clade in coords:
        r1, theta1 = coords[clade]
        x1, y1 = polar_to_cartesian(r1, theta1)
        for child in clade.clades:
            r2, theta2 = coords[child]
            x2, y2 = polar_to_cartesian(r2, theta2)
            edges.append(((x1, x2), (y1, y2)))
    fig = go.Figure()

    # Draw branches
    for x_vals, y_vals in edges:
        fig.add_trace(go.Scatter(
            x=x_vals, y=y_vals,
            mode='lines',
            line=dict(color='black'),
            hoverinfo='skip',
            showlegend=False
        ))

    # Draw tip labels
    for clade, (r, theta) in coords.items():
        if not clade.clades:  # terminal
            x, y = polar_to_cartesian(r, theta)
            label = clade.name.replace("_", " ")
            fig.add_trace(go.Scatter(
                x=[x], y=[y],
                mode='text',
                text=[label],
                textposition='middle right',
                textfont=dict(size=10),
                showlegend=False,
                hoverinfo='text'
            ))

    fig.update_layout(
        title="Radial Phylogenetic Tree of Your Life List",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(l=0, r=0, t=50, b=0),
        height=1200
    )

    st.plotly_chart(fig)
