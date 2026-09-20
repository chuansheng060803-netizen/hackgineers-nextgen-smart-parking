"""Colours and page styling. One place, so light and dark stay a matched pair.

The chart colours are the validated palette (blue and orange pass the colour-blind and
contrast checks in both modes). Status colours are fixed and always shown with an icon
and a word, never colour alone.
"""

LIGHT = {
    "name": "light", "page": "#f9f9f7", "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e",
    "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "ring": "rgba(11,11,11,0.10)",
    "blue": "#2a78d6", "orange": "#eb6834", "blue_wash": "rgba(42,120,214,0.14)",
    "good_text": "#006300",
}
DARK = {
    "name": "dark", "page": "#0d0d0d", "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7",
    "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "ring": "rgba(255,255,255,0.10)",
    "blue": "#3987e5", "orange": "#d95926", "blue_wash": "rgba(57,135,229,0.20)",
    "good_text": "#0ca30c",
}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}


def tokens(streamlit):
    """The palette matching the theme Streamlit is showing (light unless it reports dark)."""
    try:
        kind = streamlit.context.theme.type
    except Exception:                       # older Streamlit, or no browser attached yet
        kind = None
    if kind is None:
        try:
            kind = streamlit.get_option("theme.base")
        except Exception:
            kind = None
    return DARK if kind == "dark" else LIGHT


def css(t):
    s = STATUS
    return f"""
<style>
:root {{ --k-page:{t['page']}; --k-surface:{t['surface']}; --k-ink:{t['ink']}; --k-ink2:{t['ink2']};
        --k-muted:{t['muted']}; --k-grid:{t['grid']}; --k-ring:{t['ring']}; --k-blue:{t['blue']};
        --k-orange:{t['orange']}; --k-wash:{t['blue_wash']}; }}
.stApp {{ background: var(--k-page); }}
[data-testid="stMainBlockContainer"], .block-container {{ padding-top: 4.5rem; padding-bottom: 4rem; max-width: 1240px; }}
html, body, [class*="st-"], .stApp {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
h1, h2, h3 {{ letter-spacing: -0.01em; }}

.k-eyebrow {{ font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--k-muted); margin: 0; }}
.k-title {{ font-size: 28px; font-weight: 650; color: var(--k-ink); margin: 2px 0 0 0; line-height: 1.15; }}
.k-section {{ font-size: 18px; font-weight: 650; color: var(--k-ink); margin: 34px 0 2px 0; }}
.k-sub {{ font-size: 13px; color: var(--k-ink2); margin: 0 0 10px 0; }}

.k-card {{ background: var(--k-surface); border: 1px solid var(--k-ring); border-radius: 10px; padding: 16px 18px; height: 100%; }}
.k-hero-card {{ min-height: 205px; box-sizing: border-box; }}
.k-label {{ font-size: 13px; color: var(--k-ink2); margin: 0 0 6px 0; }}
.k-hero {{ font-size: 56px; font-weight: 650; color: var(--k-ink); line-height: 1.05; margin: 0; }}
.k-hero small {{ font-size: 16px; font-weight: 500; color: var(--k-muted); margin-left: 6px; }}
.k-value {{ font-size: 30px; font-weight: 650; color: var(--k-ink); line-height: 1.1; margin: 0; }}
.k-foot {{ font-size: 13px; color: var(--k-ink2); margin: 8px 0 0 0; }}
.k-up {{ color: {t['good_text']}; font-weight: 600; }}
.k-down {{ color: {s['critical']}; font-weight: 600; }}
.k-meter {{ height: 8px; border-radius: 4px; background: var(--k-wash); margin-top: 12px; overflow: hidden; }}
.k-meter > span {{ display: block; height: 100%; border-radius: 4px; background: var(--k-blue); }}

.k-pill {{ display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; padding: 4px 10px;
          border-radius: 999px; border: 1px solid var(--k-ring); background: var(--k-surface); color: var(--k-ink); }}
.k-dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
.k-banner {{ border-radius: 8px; padding: 10px 14px; font-size: 14px; color: var(--k-ink); border: 1px solid var(--k-ring);
            background: var(--k-surface); margin: 6px 0 14px 0; }}
.k-banner b {{ font-weight: 650; }}

.k-bays {{ display: grid; grid-template-columns: repeat(10, minmax(0, 1fr)); gap: 6px; }}
.k-bay {{ border: 1px solid var(--k-ring); border-radius: 6px; padding: 6px 4px; text-align: center; background: var(--k-surface);
         min-height: 50px; display: flex; flex-direction: column; justify-content: center; }}
.k-bay .n {{ font-size: 11px; color: var(--k-muted); }}
.k-bay .p {{ font-size: 10px; font-weight: 600; color: var(--k-ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.k-bay.taken {{ background: var(--k-wash); }}
.k-bay.broken {{ background: {s['critical']}; }} .k-bay.broken .n, .k-bay.broken .p {{ color: #fff; }}
.k-bay.maint {{ background: {s['serious']}; }} .k-bay.maint .n, .k-bay.maint .p {{ color: #0b0b0b; }}
.k-gap {{ height: 22px; }}
.k-legend {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--k-ink2); margin-top: 10px; }}
.k-sw {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; vertical-align: -2px; margin-right: 5px;
        border: 1px solid var(--k-ring); }}

.k-row {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 0;
         border-bottom: 1px solid var(--k-grid); font-size: 14px; color: var(--k-ink); }}
.k-row:last-child {{ border-bottom: 0; }}
.k-feed {{ font-size: 13px; color: var(--k-ink); line-height: 1.55; max-height: 330px; overflow-y: auto; }}
.k-feed .t {{ color: var(--k-muted); font-variant-numeric: tabular-nums; margin-right: 8px; }}
.k-feed .money {{ font-weight: 600; }}

@media (max-width: 900px) {{ .k-bays {{ grid-template-columns: repeat(5, minmax(0, 1fr)); }} .k-hero {{ font-size: 44px; }} }}
</style>
"""


def pill(kind, label):
    """A status pill: colour dot + icon-like word. kind: good / warning / serious / critical / neutral."""
    color = STATUS.get(kind, "#898781")
    return f'<span class="k-pill"><span class="k-dot" style="background:{color}"></span>{label}</span>'
