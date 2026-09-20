"""The charts (Altair). Thin marks, hairline gridlines, one colour per meaning, tooltips on every mark."""
import altair as alt
import pandas as pd

AXIS_FORMAT = {"15min": "%H:%M", "h": "%H:%M", "D": "%d %b", "MS": "%b %Y"}
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


def _bar_size(n):
    return 24 if n <= 14 else 18 if n <= 31 else 12 if n <= 60 else 8 if n <= 100 else 4


def _style(chart, t, height):
    return (chart.properties(height=height, background=t["surface"], padding={"left": 6, "right": 14, "top": 8, "bottom": 4})
            .configure_view(stroke=None)
            .configure_axis(grid=True, gridColor=t["grid"], gridWidth=1, domainColor=t["axis"], tickColor=t["axis"],
                            labelColor=t["muted"], titleColor=t["muted"], labelFont=FONT, titleFont=FONT,
                            labelFontSize=11, titleFontSize=11)
            .configure_legend(labelColor=t["ink2"], titleColor=t["ink2"], labelFont=FONT, titleFont=FONT,
                              orient="top", symbolType="square", symbolSize=90, padding=2, labelFontSize=12)
            .configure_axisX(grid=False))


def _x(freq, span_days, title=None):
    fmt = AXIS_FORMAT.get(freq, "%d %b")
    if freq in ("15min", "h") and span_days > 1:
        fmt = "%d %b %H:%M"
    return alt.X("start:T", title=title, axis=alt.Axis(format=fmt, labelOverlap=True, labelAngle=0, tickCount=8))


def _span_days(series):
    return (series.index.max() - series.index.min()).days + 1 if len(series) else 1


def _tooltip_time(freq):
    return alt.Tooltip("start:T", title="From", format="%d %b %Y %H:%M" if freq in ("15min", "h") else "%d %b %Y")


def _money_format(*series):
    """Axis number format: whole credits when the values are big, decimals when they are small."""
    top = max([float(abs(s).max()) for s in series if len(s)] or [0.0])
    return ",.0f" if top >= 20 else ",.1f" if top >= 2 else ",.2f"


def income_chart(income, freq, t, height=300):
    """Bars = income per period. (Penalties cost credits, not money, so they are not drawn here.)"""
    money = _money_format(income)
    frame = pd.DataFrame({"start": income.index, "amount": income.values})
    chart = (alt.Chart(frame).mark_bar(size=_bar_size(len(frame)), cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=t["blue"])
             .encode(x=_x(freq, _span_days(income)),
                     y=alt.Y("amount:Q", title="credits", axis=alt.Axis(format=money, tickCount=6)),
                     tooltip=[_tooltip_time(freq), alt.Tooltip("amount:Q", title="Income", format=",.2f")]))
    return _style(chart, t, height)


def lost_chart(counts, freq, t, per_error, height=220):
    """Bars = credits lost per period (errors x credits per error), in the second series colour."""
    frame = pd.DataFrame({"start": counts.index, "errors": counts.values, "lost": counts.values * per_error})
    chart = (alt.Chart(frame).mark_bar(size=_bar_size(len(frame)), cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=t["orange"])
             .encode(x=_x(freq, _span_days(counts)),
                     y=alt.Y("lost:Q", title="credits lost", axis=alt.Axis(format="d", tickMinStep=per_error)),
                     tooltip=[_tooltip_time(freq), alt.Tooltip("errors:Q", title="Errors", format=",.0f"),
                              alt.Tooltip("lost:Q", title="Credits lost", format=",.0f")]))
    return _style(chart, t, height)


def count_chart(counts, freq, t, label="Cars", height=240):
    frame = pd.DataFrame({"start": counts.index, "n": counts.values})
    chart = (alt.Chart(frame).mark_bar(size=_bar_size(len(frame)), cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=t["blue"])
             .encode(x=_x(freq, _span_days(counts)), y=alt.Y("n:Q", title=label, axis=alt.Axis(format="d", tickMinStep=1)),
                     tooltip=[_tooltip_time(freq), alt.Tooltip("n:Q", title=label, format=",.0f")]))
    return _style(chart, t, height)


def occupancy_chart(peak, capacity, freq, t, height=240):
    frame = pd.DataFrame({"start": peak.index, "pct": peak.values}).dropna()
    if frame.empty:
        return None
    frame["taken"] = (frame["pct"] * (capacity or 0) / 100.0).round()
    frame["free"] = (capacity or 0) - frame["taken"]
    x = _x(freq, _span_days(peak))
    y = alt.Y("pct:Q", title="% of bays", scale=alt.Scale(domain=[0, 100]))
    tip = [_tooltip_time(freq), alt.Tooltip("pct:Q", title="Peak occupancy %", format=".0f"),
           alt.Tooltip("taken:Q", title="Bays taken at the peak", format=".0f"),
           alt.Tooltip("free:Q", title="Bays available at the peak", format=".0f")]
    area = alt.Chart(frame).mark_area(color=t["blue"], opacity=0.10, interpolate="step-after").encode(x=x, y=y)
    line = alt.Chart(frame).mark_line(color=t["blue"], strokeWidth=2, interpolate="step-after").encode(x=x, y=y, tooltip=tip)
    last = frame.tail(1)
    dot = alt.Chart(last).mark_point(filled=True, size=70, color=t["blue"], stroke=t["surface"], strokeWidth=2).encode(x=x, y=y, tooltip=tip)
    return _style(alt.layer(area, line, dot), t, height)


def forecast_chart(actual, forecast, freq_label, t, height=300):
    """Recent real values as bars, the estimate as a dashed line with its likely range."""
    a = pd.DataFrame({"start": actual.index, "value": actual.values, "series": "Actual"})
    f = forecast.rename(columns={"time": "start"}).assign(series="Estimate")
    x = alt.X("start:T", title=None, axis=alt.Axis(format="%d %b %H:%M", labelOverlap=True, labelAngle=0, tickCount=8))
    color = alt.Color("series:N", scale=alt.Scale(domain=["Actual", "Estimate"], range=[t["blue"], t["orange"]]),
                      legend=alt.Legend(title=None))
    bars = (alt.Chart(a).mark_bar(size=_bar_size(len(a) + len(f)), cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
            .encode(x=x, y=alt.Y("value:Q", title=freq_label, axis=alt.Axis(format=",.0f")), color=color,
                    tooltip=[alt.Tooltip("start:T", title="Hour", format="%d %b %H:%M"),
                             alt.Tooltip("value:Q", title="Actual", format=",.2f")]))
    band = (alt.Chart(f).mark_area(opacity=0.14, color=t["orange"])
            .encode(x=x, y="low:Q", y2="high:Q"))
    line = (alt.Chart(f).mark_line(strokeWidth=2, strokeDash=[6, 4], point=alt.OverlayMarkDef(size=60, filled=True, stroke=t["surface"], strokeWidth=2))
            .encode(x=x, y="forecast:Q", color=color,
                    tooltip=[alt.Tooltip("start:T", title="Hour", format="%d %b %H:%M"),
                             alt.Tooltip("forecast:Q", title="Estimate", format=",.2f"),
                             alt.Tooltip("low:Q", title="Likely low", format=",.2f"),
                             alt.Tooltip("high:Q", title="Likely high", format=",.2f")]))
    now = (alt.Chart(pd.DataFrame({"start": [f["start"].iloc[0]]})).mark_rule(color=t["axis"], strokeWidth=1).encode(x=x))
    return _style(alt.layer(band, bars, now, line), t, height)


def show(st, chart):
    """Draw a chart across the page width (works on old and new Streamlit)."""
    if chart is None:
        return
    try:
        st.altair_chart(chart, width="stretch")
    except TypeError:
        st.altair_chart(chart, use_container_width=True)
