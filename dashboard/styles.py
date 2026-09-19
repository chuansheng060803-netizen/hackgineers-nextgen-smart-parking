"""Colours and CSS for the dashboard.

Palette = the validated data-viz palette (blue / orange / aqua for the 3 chart series, fixed status
colours for good / warning / serious / critical). Light and dark are separate, hand-picked steps.
Status colours are never used for plain series; every status also carries an icon or a text label.
"""

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

TOKENS = {
    "dark": {
        "surface": "#1a1a19", "surface2": "#222220", "ink": "#f4f4f1", "ink2": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "baseline": "#383835", "border": "rgba(255,255,255,0.10)",
        "s1": "#3987e5", "s2": "#d95926", "s3": "#199e70",
        "on_fill": "#ffffff",
    },
    "light": {
        "surface": "#fcfcfb", "surface2": "#f3f3f0", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "baseline": "#c3c2b7", "border": "rgba(11,11,11,0.10)",
        "s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a",
        "on_fill": "#ffffff",
    },
}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
RISK_COLOR = {"Safe": STATUS["good"], "Mid": STATUS["warning"], "High": STATUS["serious"], "Critical": STATUS["critical"]}


def tokens(theme):
    return TOKENS["light" if theme == "light" else "dark"]


def css(theme):
    t = tokens(theme)
    return f"""<style>
:root {{
  --pk-surface:{t['surface']}; --pk-surface2:{t['surface2']}; --pk-ink:{t['ink']}; --pk-ink2:{t['ink2']};
  --pk-muted:{t['muted']}; --pk-grid:{t['grid']}; --pk-base:{t['baseline']}; --pk-border:{t['border']};
  --pk-s1:{t['s1']}; --pk-s2:{t['s2']}; --pk-s3:{t['s3']}; --pk-on:{t['on_fill']};
  --pk-good:{STATUS['good']}; --pk-warn:{STATUS['warning']}; --pk-serious:{STATUS['serious']}; --pk-crit:{STATUS['critical']};
}}
[data-testid="stMainBlockContainer"], .block-container {{ padding-top: 4.5rem !important; padding-bottom: 3rem; max-width: 1560px; }}
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"] .pk-card-title) {{
  background: var(--pk-surface); border-color: var(--pk-border) !important; border-radius: 14px;
}}
.pk, .pk * {{ box-sizing: border-box; font-family: {FONT}; }}
.pk {{ color: var(--pk-ink); }}
.pk-head {{ display:flex; align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:14px; }}
.pk-title {{ font-size: 22px; font-weight: 700; letter-spacing: -.01em; }}
.pk-sub {{ color: var(--pk-ink2); font-size: 13px; margin-top: 2px; }}
.pk-chip {{ display:inline-flex; align-items:center; gap:8px; padding:5px 12px; border-radius:999px; font-size:12.5px;
  color: var(--pk-ink2); background: var(--pk-surface); border:1px solid var(--pk-border); }}
.pk-dot {{ width:8px; height:8px; border-radius:50%; background: var(--pk-muted); display:inline-block; flex:none; }}
.pk-dot.good {{ background: var(--pk-good); }} .pk-dot.warn {{ background: var(--pk-warn); }}
.pk-dot.serious {{ background: var(--pk-serious); }} .pk-dot.crit {{ background: var(--pk-crit); }}
.pk-dot.blue {{ background: var(--pk-s1); }}

.pk-card {{ background: var(--pk-surface); border:1px solid var(--pk-border); border-radius:14px; padding:16px 18px; margin-bottom:16px; }}
.pk-card-h {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:12px; flex-wrap:wrap; }}
.pk-card-title {{ font-size:14.5px; font-weight:650; }}
.pk-meta {{ color: var(--pk-ink2); font-size:12.5px; }}

.pk-kpis {{ display:grid; grid-template-columns: 1.5fr repeat(4, 1fr); gap:14px; margin-bottom:16px; }}
@media (max-width: 1100px) {{ .pk-kpis {{ grid-template-columns: repeat(2, 1fr); }} }}
.pk-kpi {{ background: var(--pk-surface); border:1px solid var(--pk-border); border-radius:14px; padding:14px 18px; position:relative; overflow:hidden; }}
.pk-kpi.hero {{ padding-top:16px; }}
.pk-kpi .lbl {{ color: var(--pk-ink2); font-size:12.5px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; }}
.pk-kpi .val {{ font-size:34px; font-weight:700; letter-spacing:-.02em; margin-top:4px; line-height:1.1; }}
.pk-kpi.hero .val {{ font-size:52px; }}
.pk-kpi .val small {{ font-size:16px; font-weight:500; color: var(--pk-ink2); letter-spacing:0; }}
.pk-kpi .sub {{ color: var(--pk-ink2); font-size:12.5px; margin-top:6px; display:flex; align-items:center; gap:7px; }}
.pk-kpi .bar {{ position:absolute; left:0; top:0; bottom:0; width:4px; background: var(--pk-base); }}
.pk-kpi .bar.good {{ background: var(--pk-good); }} .pk-kpi .bar.warn {{ background: var(--pk-warn); }} .pk-kpi .bar.crit {{ background: var(--pk-crit); }}

.pk-alerts {{ display:grid; gap:10px; margin-bottom:16px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
.pk-alert {{ display:flex; gap:12px; align-items:flex-start; padding:12px 14px; border-radius:12px; background: var(--pk-surface);
  border:1px solid var(--pk-border); border-left:5px solid var(--pk-base); }}
.pk-alert.critical {{ border-left-color: var(--pk-crit); }} .pk-alert.warning {{ border-left-color: var(--pk-warn); }}
.pk-alert.info {{ border-left-color: var(--pk-s1); }}
.pk-alert svg {{ width:20px; height:20px; flex:none; margin-top:1px; }}
.pk-alert.critical svg {{ color: var(--pk-crit); }} .pk-alert.warning svg {{ color: var(--pk-warn); }} .pk-alert.info svg {{ color: var(--pk-s1); }}
.pk-alert .t {{ font-weight:650; font-size:14px; }}
.pk-alert .d {{ color: var(--pk-ink2); font-size:12.5px; margin-top:2px; }}
.pk-alert .tag {{ margin-left:auto; font-size:11px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color: var(--pk-ink2); white-space:nowrap; }}

.pk-legend {{ display:flex; gap:16px; flex-wrap:wrap; color: var(--pk-ink2); font-size:12.5px; }}
.pk-legend i {{ display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:-2px; border:1px solid var(--pk-base); }}
.pk-zone {{ margin-top:16px; }}
.pk-zone:first-of-type {{ margin-top:4px; }}
.pk-zone-h {{ display:flex; align-items:center; gap:12px; margin-bottom:8px; flex-wrap:wrap; }}
.pk-zone-h b {{ font-size:13.5px; }}
.pk-track {{ flex:1; min-width:80px; height:6px; border-radius:99px; background: var(--pk-surface2); border:1px solid var(--pk-grid); overflow:hidden; }}
.pk-track > span {{ display:block; height:100%; background: var(--pk-s1); border-radius:99px; }}
.pk-spots {{ display:grid; grid-template-columns: repeat(10, minmax(0, 1fr)); gap:6px; }}
@media (max-width: 1250px) {{ .pk-spots {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }} }}
.pk-spot {{ position:relative; border-radius:9px; padding:7px 7px 6px; min-height:58px; display:flex; flex-direction:column; justify-content:space-between;
  background: var(--pk-surface2); border:1px solid var(--pk-grid); color: var(--pk-ink2); transition: background .3s, border-color .3s; }}
.pk-spot .n {{ font-weight:700; font-size:12.5px; color: var(--pk-ink); }}
.pk-spot .top {{ display:flex; align-items:center; justify-content:space-between; gap:4px; }}
.pk-spot .l {{ font-size:10.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.pk-spot .k {{ font-size:8px; font-weight:700; letter-spacing:0; padding:0 4px; border-radius:99px;
  border:1px solid var(--pk-base); color: var(--pk-ink2); }}
.pk-spot.occupied {{ background: var(--pk-s1); border-color: var(--pk-s1); color: var(--pk-on); }}
.pk-spot.occupied .n {{ color: var(--pk-on); }} .pk-spot.occupied .k {{ color: var(--pk-on); border-color: rgba(255,255,255,.55); }}
.pk-spot.reserved {{ background: transparent; border:2px solid var(--pk-s1); padding:6px 6px 5px; color: var(--pk-ink); }}
.pk-spot.broken {{ background: var(--pk-crit); border-color: var(--pk-crit); color:#fff; }}
.pk-spot.broken .n, .pk-spot.broken .k {{ color:#fff; border-color: rgba(255,255,255,.55); }}
.pk-spot.maintenance {{ background: var(--pk-warn); border-color: var(--pk-warn); color:#111; }}
.pk-spot.maintenance .n, .pk-spot.maintenance .k {{ color:#111; border-color: rgba(0,0,0,.4); }}
.pk-spot.rogue {{ box-shadow: 0 0 0 2px var(--pk-surface), 0 0 0 4px var(--pk-crit); }}

.pk-row {{ display:flex; align-items:center; gap:10px; padding:9px 0; border-bottom:1px solid var(--pk-grid); font-size:13.5px; }}
.pk-row:last-child {{ border-bottom:0; }}
.pk-row .nm {{ font-weight:650; }} .pk-row .role {{ color: var(--pk-ink2); font-size:12.5px; }}
.pk-row .right {{ margin-left:auto; display:flex; gap:6px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }}
.pk-pill {{ display:inline-flex; align-items:center; gap:6px; padding:2px 10px; border-radius:99px; font-size:12px; font-weight:600;
  border:1px solid var(--pk-border); background: var(--pk-surface2); color: var(--pk-ink); white-space:nowrap; }}
.pk-pill svg {{ width:12px; height:12px; }}
.pk-pill.crit {{ background: color-mix(in srgb, var(--pk-crit) 18%, transparent); border-color: color-mix(in srgb, var(--pk-crit) 60%, transparent); }}
.pk-pill.warn {{ background: color-mix(in srgb, var(--pk-warn) 20%, transparent); border-color: color-mix(in srgb, var(--pk-warn) 65%, transparent); }}
.pk-pill.good {{ background: color-mix(in srgb, var(--pk-good) 16%, transparent); border-color: color-mix(in srgb, var(--pk-good) 55%, transparent); }}
.pk-pill.serious {{ background: color-mix(in srgb, var(--pk-serious) 18%, transparent); border-color: color-mix(in srgb, var(--pk-serious) 60%, transparent); }}

.pk-co {{ padding:8px 0 10px; }}
.pk-co-top {{ display:flex; align-items:center; gap:10px; font-size:13.5px; margin-bottom:6px; }}
.pk-co-top .v {{ margin-left:auto; color: var(--pk-ink2); font-size:12.5px; }}
.pk-meter {{ position:relative; height:8px; border-radius:99px; background: var(--pk-surface2); border:1px solid var(--pk-grid); }}
.pk-meter > span {{ position:absolute; left:0; top:0; bottom:0; border-radius:99px; }}
.pk-meter > i {{ position:absolute; top:-4px; bottom:-4px; width:2px; background: var(--pk-ink2); opacity:.7; }}
.pk-scale {{ display:flex; justify-content:space-between; color: var(--pk-muted); font-size:11px; margin-top:3px; }}

.pk-feed {{ display:grid; gap:2px; max-height:430px; overflow:auto; }}
.pk-feed-item {{ display:grid; grid-template-columns: 62px 74px 1fr; gap:10px; align-items:baseline; padding:7px 2px; border-bottom:1px solid var(--pk-grid); font-size:13px; }}
.pk-feed-item .tm {{ color: var(--pk-muted); font-size:12px; font-variant-numeric: tabular-nums; }}
.pk-feed-item .tg {{ font-size:10.5px; font-weight:700; letter-spacing:.06em; color: var(--pk-ink2); display:flex; align-items:center; gap:6px; }}
.pk-empty {{ color: var(--pk-muted); padding:16px 0; text-align:center; font-size:13px; }}
</style>"""
