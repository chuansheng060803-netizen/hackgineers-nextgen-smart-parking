"""Plotly charts. Transparent backgrounds so they sit on the card surface, hairline grids, thin marks.

One series -> no legend. Three zones -> one hue per zone (fixed order, blue/orange/aqua) plus an
end-of-line label, so colour is never the only way to tell them apart.
"""
import plotly.graph_objects as go

from styles import tokens, FONT

CONGESTION = 0.85     # occupancy line drawn at 85%
CO_MID = 50.0         # organisers: 50 ppm counts as "medium" risk


def _base(fig, theme, height=260, ytitle=None):
    t = tokens(theme)
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=12, color=t["ink2"]), hovermode="x unified", showlegend=False,
        hoverlabel=dict(bgcolor=t["surface2"], bordercolor=t["baseline"], font=dict(color=t["ink"], family=FONT, size=12)),
    )
    fig.update_xaxes(showgrid=False, linecolor=t["baseline"], tickcolor=t["baseline"], tickfont=dict(color=t["muted"], size=11),
                     tickformat="%H:%M", automargin=True)
    fig.update_yaxes(gridcolor=t["grid"], gridwidth=1, zeroline=False, linecolor="rgba(0,0,0,0)", tickfont=dict(color=t["muted"], size=11),
                     title=dict(text=ytitle, font=dict(size=11, color=t["muted"])) if ytitle else None, automargin=True)
    return fig


def occupancy_chart(history, theme):
    t = tokens(theme)
    rows = history.get("occupancy", [])
    fig = go.Figure()
    if rows:
        x = [r["t"] for r in rows]
        y = [round(100 * r["occupied"] / r["total"], 1) if r["total"] else 0 for r in rows]
        fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name="Occupancy", line=dict(color=t["s1"], width=2, shape="hv"),
                                 fill="tozeroy", fillcolor="rgba(57,135,229,0.14)" if theme != "light" else "rgba(42,120,214,0.12)",
                                 hovertemplate="%{y:.0f}% full<extra></extra>"))
        fig.add_hline(y=CONGESTION * 100, line=dict(color=t["muted"], width=1, dash="dot"),
                      annotation_text="85% = congested", annotation_position="top left",
                      annotation_font=dict(size=11, color=t["muted"]))
    _base(fig, theme, ytitle="% of spots taken")
    fig.update_yaxes(range=[0, 105], ticksuffix="%")
    return fig


def co_chart(history, theme):
    t = tokens(theme)
    rows = history.get("co", [])
    fig = go.Figure()
    colours = [t["s1"], t["s2"], t["s3"], t["s4"], t["s5"], t["s6"]]
    zones = sorted({r["zone"] for r in rows})
    for i, z in enumerate(zones):
        pts = [r for r in rows if r["zone"] == z]
        x, y = [p["t"] for p in pts], [p["ppm"] for p in pts]
        fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name=z, line=dict(color=colours[i % 6], width=2),
                                 hovertemplate="%{y:.0f} ppm<extra>" + z + "</extra>"))
    if zones:
        fig.add_hline(y=CO_MID, line=dict(color=t["muted"], width=1, dash="dot"),
                      annotation_text="50 ppm = Mid", annotation_position="top left", annotation_font=dict(size=11, color=t["muted"]))
    _base(fig, theme, ytitle="CO (ppm)")
    fig.update_layout(showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.03, x=0, font=dict(size=11, color=t["ink2"])),
                      margin=dict(l=8, r=8, t=56, b=8))
    fig.update_yaxes(rangemode="tozero")
    return fig


def arrivals_chart(history, theme):
    t = tokens(theme)
    rows = history.get("arrivals", [])
    fig = go.Figure()
    if rows:
        fig.add_trace(go.Bar(x=[r["t"] for r in rows], y=[r["count"] for r in rows], marker=dict(color=t["s1"], cornerradius=4),
                             hovertemplate="%{y} cars<extra></extra>"))
    _base(fig, theme, ytitle="cars per 5 min")
    fig.update_layout(bargap=0.35, hovermode="x")
    return fig


def daily_chart(days, theme):
    """Visits per day for the last 30 days (oldest on the left, today on the right)."""
    t = tokens(theme)
    rows = list(reversed(days))
    fig = go.Figure()
    if rows:
        fig.add_trace(go.Bar(x=[r["date"] for r in rows], y=[r["visits"] for r in rows], marker=dict(color=t["s1"], cornerradius=3),
                             customdata=[[r["income"], r["peak_pct"]] for r in rows],
                             hovertemplate="%{x}<br>%{y} visits<br>income %{customdata[0]:,.0f}<br>peak %{customdata[1]}% full<extra></extra>"))
    _base(fig, theme, height=230, ytitle="visits per day")
    fig.update_xaxes(tickformat="%d %b", nticks=10)
    fig.update_layout(bargap=0.3, hovermode="closest")
    return fig
