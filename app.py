from __future__ import annotations

import base64
import gzip
import html
import io
import json
import math

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from metrics_data import METRICS_GZ_B64
from series_data_1 import SERIES_GZ_B64_1
from series_data_2 import SERIES_GZ_B64_2
from series_data_3 import SERIES_GZ_B64_3

SERIES_GZ_B64 = SERIES_GZ_B64_1 + SERIES_GZ_B64_2 + SERIES_GZ_B64_3

st.set_page_config(page_title="BCI · Tracking Error Peers", page_icon="📊", layout="wide", initial_sidebar_state="collapsed")

NAVY = "#064A91"
NAVY_DARK = "#0B2F4A"
BLUE = "#006BFF"
LIGHT_BLUE = "#E8F2FF"
GRID = "#D8E2EA"
TEXT = "#3E5566"
GREEN = "#168A14"
RED = "#ED1C24"
AMBER = "#FFC400"
PEER = "#B8C5CF"
BG = "#F4F7F9"

st.markdown(
    f"""
<style>
html, body, [class*="css"] {{ font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
.stApp {{ background:{BG}; }}
.block-container {{ max-width: 1460px; padding: .55rem 1.25rem 2rem; }}
header[data-testid="stHeader"] {{ background: transparent; }}
#MainMenu, footer {{ visibility:hidden; }}
.topbar {{ background:{NAVY}; border-bottom:4px solid {BLUE}; min-height:68px; margin:0 -1.25rem 1.1rem; padding:.55rem 1.9rem; display:flex; align-items:center; justify-content:space-between; gap:1rem; }}
.topbar .brand {{ color:white; font-weight:800; font-size:1.15rem; letter-spacing:.01em; }}
.cutbox {{ border:1px solid #9FC7EF; border-radius:7px; color:white; padding:.45rem .75rem; min-width:155px; text-align:left; }}
.cutbox span {{ display:block; font-size:.62rem; text-transform:uppercase; opacity:.9; }}
.cutbox strong {{ font-size:.95rem; font-weight:500; }}
.control-card {{ background:white; border:1px solid {GRID}; border-radius:8px; padding:.65rem .8rem .2rem; margin-bottom:.8rem; }}
.monitor-wrap {{ background:white; border:1px solid {GRID}; border-radius:8px; overflow:hidden; }}
.monitor-scroll {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
.risk-table {{ width:100%; border-collapse:collapse; min-width:1050px; color:{TEXT}; font-size:.82rem; }}
.risk-table th {{ background:#405868; color:white; padding:.65rem .55rem; text-align:center; font-weight:650; line-height:1.15; }}
.risk-table th:first-child {{ text-align:left; }}
.risk-table td {{ padding:.68rem .55rem; border-bottom:1px solid #EDF2F5; text-align:center; white-space:nowrap; }}
.risk-table td:first-child {{ text-align:left; font-weight:600; }}
.risk-table tr.group-start td {{ border-top:1.5px dashed #6B89A1; }}
.risk-table td.te-main {{ background:{LIGHT_BLUE}; color:{BLUE}; font-weight:750; }}
.alpha-cell {{ position:relative; min-width:92px; }}
.alpha-zero {{ position:absolute; left:50%; top:18%; bottom:18%; border-left:1px solid #95AAB9; }}
.alpha-bar {{ position:absolute; top:31%; bottom:31%; opacity:.62; }}
.alpha-val {{ position:relative; z-index:2; }}
.ir-dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:9px; vertical-align:middle; }}
.ret-pos {{ color:{GREEN}; font-weight:700; }}
.qbadge {{ display:inline-flex; align-items:center; justify-content:center; width:23px; height:23px; border-radius:50%; font-size:.72rem; color:white; border:1px solid rgba(0,0,0,.12); }}
.q1 {{ background:#FFD000; color:#2E3A43; }} .q2 {{ background:#7894A5; }} .q3 {{ background:#2F80ED; }} .q4 {{ background:#D95C5C; }}
.bci-tag {{ color:{NAVY}; font-weight:800; }}
.mobile-cards {{ display:none; }}
.mcard {{ background:white; border:1px solid {GRID}; border-radius:10px; margin-bottom:.7rem; overflow:hidden; }}
.mcard-head {{ padding:.72rem .8rem; border-bottom:1px solid #EDF2F5; display:flex; justify-content:space-between; gap:.5rem; align-items:flex-start; }}
.mcard-title {{ font-weight:750; color:#344B5A; line-height:1.2; }}
.mcard-class {{ font-size:.72rem; color:#718595; margin-top:.18rem; }}
.mgrid {{ display:grid; grid-template-columns:1fr 1fr; }}
.mmetric {{ padding:.68rem .8rem; border-bottom:1px solid #EDF2F5; min-width:0; }}
.mmetric:nth-child(odd) {{ border-right:1px solid #EDF2F5; }}
.mlabel {{ color:#718595; font-size:.66rem; text-transform:uppercase; font-weight:700; }}
.mval {{ color:#344B5A; font-size:1rem; margin-top:.1rem; font-weight:650; overflow-wrap:anywhere; }}
.mval.te {{ color:{BLUE}; }} .mval.ret {{ color:{GREEN}; }}
.section-title {{ color:#344B5A; font-size:1rem; font-weight:800; margin:1rem 0 .45rem; }}
.note-box {{ background:white; border:1px solid {GRID}; border-radius:8px; padding:.75rem .9rem; color:{TEXT}; font-size:.8rem; margin-top:.8rem; }}
.legend-row {{ display:flex; gap:1rem; flex-wrap:wrap; align-items:center; margin-top:.5rem; }}
.legend-row span {{ white-space:nowrap; }}
[data-testid="stMetric"] {{ background:white; border:1px solid {GRID}; border-radius:8px; padding:.55rem .7rem; }}
[data-testid="stMetricLabel"] {{ font-size:.72rem; color:#647888; }}
[data-testid="stMetricValue"] {{ color:{NAVY_DARK}; font-size:1.25rem; }}
@media (max-width: 720px) {{
  .block-container {{ padding:.25rem .55rem 1.5rem; }}
  .topbar {{ margin:0 -.55rem .7rem; padding:.6rem .7rem; min-height:58px; }}
  .topbar .brand {{ font-size:.95rem; line-height:1.15; max-width:55%; }}
  .cutbox {{ min-width:125px; padding:.35rem .55rem; }}
  .cutbox strong {{ font-size:.82rem; }}
  .monitor-scroll {{ display:none; }}
  .mobile-cards {{ display:block; padding:.55rem; }}
  .control-card {{ padding:.45rem .55rem .15rem; }}
  div[data-testid="stHorizontalBlock"] {{ gap:.45rem; }}
  .section-title {{ font-size:.92rem; }}
  .note-box {{ font-size:.74rem; }}
}}
</style>
""",
    unsafe_allow_html=True,
)

@st.cache_data
def load_data():
    metrics = json.loads(gzip.decompress(base64.b64decode(METRICS_GZ_B64)).decode("utf-8"))
    historical = json.loads(gzip.decompress(base64.b64decode(SERIES_GZ_B64)).decode("utf-8"))
    frame = pd.DataFrame(metrics)
    for col in ["te_ewma_anual", "te_equiponderado_anual", "IR", "ret_1y_fondo", "ret_1y_pares", "vol_anual", "exceso_1y"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["es_bci"] = frame["es_bci"].astype(bool)
    return frame, historical

def pct(x, digits=2, signed=False):
    if pd.isna(x): return "—"
    v = x * 100
    s = f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"
    return s.replace(".", ",") + "%"

def number(x, digits=2):
    if pd.isna(x): return "—"
    return f"{x:.{digits}f}".replace(".", ",")

def ir_color(x):
    if pd.isna(x) or x < 0: return RED
    if x < .5: return AMBER
    if x < 1: return GREEN
    return NAVY

def quartile(rank, n):
    if pd.isna(rank) or not n: return None
    return max(1, min(4, math.ceil(float(rank) / n * 4)))

def run_from_series_col(col: str) -> str:
    return str(col).split("-", 1)[0].strip()

def ewma_tracking_error(active: pd.Series, lam: float = .94) -> float:
    x = active.dropna().astype(float).to_numpy()
    if len(x) < 2: return float("nan")
    w = lam ** np.arange(len(x) - 1, -1, -1, dtype=float)
    w /= w.sum()
    mu = float(np.dot(w, x))
    var = float(np.dot(w, (x - mu) ** 2))
    return math.sqrt(max(var, 0)) * math.sqrt(52)

@st.cache_data
def historical_te_for_stem(stem: str, min_obs: int = 8, lam: float = .94):
    payload = series.get(stem)
    if not payload: return pd.DataFrame(), pd.Series(dtype=float)
    levels = pd.DataFrame(payload["valores"], index=pd.to_datetime(payload["fechas"]))
    levels = levels.apply(pd.to_numeric, errors="coerce").dropna(how="any")
    returns = levels.pct_change(fill_method=None).dropna(how="any")
    if returns.shape[0] < min_obs or returns.shape[1] < 3: return pd.DataFrame(), pd.Series(dtype=float)
    out = pd.DataFrame(index=returns.index, columns=returns.columns, dtype=float)
    for i in range(min_obs - 1, len(returns)):
        window = returns.iloc[: i + 1].tail(52)
        for fund in returns.columns:
            peers = [c for c in returns.columns if c != fund]
            out.loc[returns.index[i], fund] = ewma_tracking_error(window[fund] - window[peers].mean(axis=1), lam)
    out = out.dropna(how="all")
    return out, out.quantile(.75, axis=1, interpolation="linear")

def bci_series_column(category_df, hist_df):
    if hist_df.empty or not category_df.es_bci.any(): return None
    run = str(category_df.loc[category_df.es_bci, "run"].iloc[0])
    matches = [c for c in hist_df.columns if run_from_series_col(c) == run]
    return matches[0] if matches else None

def te_altair(category_name: str):
    g = df[df.categoria == category_name].copy()
    if g.empty: return None
    hist_df, p75 = historical_te_for_stem(str(g.archivo.iloc[0]))
    if hist_df.empty: return None
    bci_col = bci_series_column(g, hist_df)
    long = hist_df.reset_index(names="fecha").melt("fecha", var_name="fondo", value_name="te").dropna()
    long["tipo"] = np.where(long["fondo"].eq(bci_col), "BCI", "Peers")
    p75_df = pd.DataFrame({"fecha": p75.index, "te": p75.values, "fondo": "P75", "tipo": "P75"})
    all_df = pd.concat([long, p75_df], ignore_index=True)
    peers = alt.Chart(all_df[all_df.tipo == "Peers"]).mark_line(color=PEER, opacity=.45, strokeWidth=1).encode(
        x=alt.X("fecha:T", title=None), y=alt.Y("te:Q", title="Tracking Error EWMA", axis=alt.Axis(format=".1%")), detail="fondo:N",
        tooltip=[alt.Tooltip("fecha:T", title="Fecha", format="%d-%m-%Y"), alt.Tooltip("fondo:N", title="Fondo"), alt.Tooltip("te:Q", title="TE", format=".2%")])
    layers = [peers]
    if bci_col is not None:
        layers.append(alt.Chart(all_df[all_df.tipo == "BCI"]).mark_line(color=NAVY, strokeWidth=3).encode(x="fecha:T", y="te:Q", tooltip=[alt.Tooltip("fecha:T", format="%d-%m-%Y"), alt.Tooltip("te:Q", format=".2%")]))
    layers.append(alt.Chart(all_df[all_df.tipo == "P75"]).mark_line(color=AMBER, strokeWidth=3, strokeDash=[7,5]).encode(x="fecha:T", y="te:Q", tooltip=[alt.Tooltip("fecha:T", format="%d-%m-%Y"), alt.Tooltip("te:Q", title="P75", format=".2%")] ))
    return alt.layer(*layers).properties(height=310, title=f"{category_name} · TE histórico vs P75").interactive(bind_y=False)

def alpha_bar(value, max_abs):
    if pd.isna(value) or max_abs <= 0: return ""
    mag = min(abs(value) / max_abs * 48, 48)
    if value >= 0: return f'<span class="alpha-bar" style="left:50%;width:{mag:.1f}%;background:#3490FF"></span>'
    return f'<span class="alpha-bar" style="right:50%;width:{mag:.1f}%;background:#F56A6A"></span>'

def monitor_html(data: pd.DataFrame):
    rows, mobile = [], []
    max_abs = max(float(data.exceso_1y.abs().max(skipna=True) or 0), .0001)
    prev_group = None
    for _, r in data.iterrows():
        group_name = str(r.get("grupo", ""))
        group_start = prev_group is not None and group_name != prev_group
        prev_group = group_name
        ncat = int((df.categoria == r.categoria).sum())
        q = quartile(r.get("rank"), ncat)
        qhtml = f'<span class="qbadge q{q}">{q}</span>' if q else "—"
        irc = ir_color(r.IR)
        fname = html.escape(str(r.fondo)); klass = html.escape(group_name)
        bci = '<span class="bci-tag">BCI</span>' if bool(r.es_bci) else ""
        alpha = pct(r.exceso_1y, 2, True); cls = "group-start" if group_start else ""
        rows.append(f'<tr class="{cls}"><td>{fname} {bci}</td><td>{klass}</td><td class="te-main">{pct(r.te_ewma_anual,2)}</td><td>{pct(r.te_equiponderado_anual,2)}</td><td class="alpha-cell"><span class="alpha-zero"></span>{alpha_bar(r.exceso_1y,max_abs)}<span class="alpha-val">{alpha}</span></td><td><span class="ir-dot" style="background:{irc}"></span>{number(r.IR,2)}</td><td>{pct(r.vol_anual,1)}</td><td class="ret-pos">{pct(r.ret_1y_fondo,1)}</td><td>{qhtml}</td></tr>')
        mobile.append(f'<div class="mcard"><div class="mcard-head"><div><div class="mcard-title">{fname}</div><div class="mcard-class">{klass} {bci}</div></div>{qhtml}</div><div class="mgrid"><div class="mmetric"><div class="mlabel">TE EWMA</div><div class="mval te">{pct(r.te_ewma_anual,2)}</div></div><div class="mmetric"><div class="mlabel">TE equipond.</div><div class="mval">{pct(r.te_equiponderado_anual,2)}</div></div><div class="mmetric"><div class="mlabel">Alpha / Exceso 1Y</div><div class="mval">{alpha}</div></div><div class="mmetric"><div class="mlabel">Information Ratio</div><div class="mval"><span class="ir-dot" style="background:{irc}"></span>{number(r.IR,2)}</div></div><div class="mmetric"><div class="mlabel">Volatilidad</div><div class="mval">{pct(r.vol_anual,1)}</div></div><div class="mmetric"><div class="mlabel">Retorno 1Y</div><div class="mval ret">{pct(r.ret_1y_fondo,1)}</div></div></div></div>')
    return '<div class="monitor-wrap"><div class="monitor-scroll"><table class="risk-table"><thead><tr><th>Fondo</th><th>Clase<br>de Activo</th><th>TE EWMA</th><th>TE Equipond.</th><th>Alpha<br>1Y</th><th>Information<br>Ratio</th><th>Volatilidad<br>Anual</th><th>Retorno<br>1Y</th><th>Cuartil<br>TE</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div><div class="mobile-cards">' + ''.join(mobile) + '</div></div>'

df, series = load_data()
cut_dt = pd.to_datetime(df["fecha_corte"], errors="coerce").max()
cut = cut_dt.strftime("%d-%m-%Y") if pd.notna(cut_dt) else "—"
st.markdown(f'<div class="topbar"><div class="brand">BCI · MONITOR TRACKING ERROR PEERS</div><div class="cutbox"><span>Fecha de corte</span><strong>{cut}</strong></div></div>', unsafe_allow_html=True)
st.markdown('<div class="control-card">', unsafe_allow_html=True)
c1, c2, c3 = st.columns([1,1,1.25])
with c1: view = st.selectbox("Vista", ["Monitor", "TE histórico + P75", "Resumen ejecutivo", "Datos"])
with c2:
    group_options = ["Todos"] + sorted(df.grupo.dropna().astype(str).unique().tolist())
    group = st.selectbox("Clase de activo", group_options)
base = df if group == "Todos" else df[df.grupo.astype(str) == group]
with c3:
    category_options = sorted(base.categoria.dropna().astype(str).unique().tolist())
    category = st.selectbox("Categoría / Peer group", ["Todas"] + category_options)
st.markdown('</div>', unsafe_allow_html=True)
filtered = base if category == "Todas" else base[base.categoria.astype(str) == category]

if view == "Monitor":
    monitor = filtered.copy().sort_values(["grupo","categoria","te_ewma_anual"], ascending=[True,True,True])
    st.markdown(monitor_html(monitor), unsafe_allow_html=True)
    st.markdown(f'<div class="note-box"><b>NOTAS METODOLÓGICAS</b><br>TE EWMA: tracking error leave-one-out, λ=0,94, anualizado por √52. &nbsp; TE Equipond.: versión equiponderada disponible en el dataset. &nbsp; Alpha 1Y: exceso de retorno del fondo versus sus pares.<div class="legend-row"><span><i class="ir-dot" style="background:#ED1C24"></i>IR &lt; 0</span><span><i class="ir-dot" style="background:#FFC400"></i>0 ≤ IR &lt; 0,50</span><span><i class="ir-dot" style="background:#168A14"></i>0,50 ≤ IR &lt; 1,00</span><span><i class="ir-dot" style="background:#064A91"></i>IR ≥ 1,00</span></div></div>', unsafe_allow_html=True)
elif view == "TE histórico + P75":
    st.markdown('<div class="section-title">TRACKING ERROR HISTÓRICO Y PERCENTIL 75</div>', unsafe_allow_html=True)
    cats = category_options if category == "Todas" else [category]
    selected = st.multiselect("Categorías a mostrar", cats, default=cats[:4], max_selections=6) if category == "Todas" and len(cats) > 6 else cats
    for cat in selected:
        chart = te_altair(cat)
        if chart is not None: st.altair_chart(chart, width="stretch")
        else: st.info(f"{cat}: no hay historia suficiente para calcular TE dinámico.")
    st.caption("Interactivo: zoom horizontal y tooltip. Azul = BCI; gris = peers; amarillo discontinuo = P75.")
elif view == "Resumen ejecutivo":
    rows = []
    for cat, g in base.groupby("categoria"):
        if not g.es_bci.any(): continue
        b = g[g.es_bci].iloc[0]
        rows.append({"Categoría":cat,"Clase":b.grupo,"Fondo BCI":b.fondo,"TE BCI":b.te_ewma_anual,"P75":g.te_ewma_anual.quantile(.75),"IR":b.IR,"Alpha 1Y":b.exceso_1y,"Retorno 1Y":b.ret_1y_fondo})
    r = pd.DataFrame(rows)
    a,b,c,d = st.columns(4)
    a.metric("Categorías", len(r)); b.metric("Fondos mercado", len(base)); c.metric("BCI bajo P75", int((r["TE BCI"] <= r["P75"]).sum()) if not r.empty else 0); d.metric("Alpha positivo", int((r["Alpha 1Y"] > 0).sum()) if not r.empty else 0)
    if not r.empty:
        chart_data = r.melt(id_vars=["Categoría"], value_vars=["TE BCI","P75"], var_name="Serie", value_name="TE")
        summary_chart = alt.Chart(chart_data).mark_bar().encode(x=alt.X("Categoría:N", sort="-y", axis=alt.Axis(labelAngle=-35)), y=alt.Y("TE:Q", axis=alt.Axis(format=".1%")), color=alt.Color("Serie:N", scale=alt.Scale(domain=["TE BCI","P75"], range=[NAVY,AMBER])), xOffset="Serie:N", tooltip=["Categoría:N","Serie:N",alt.Tooltip("TE:Q",format=".2%")]).properties(height=350).interactive(bind_y=False)
        st.altair_chart(summary_chart, width="stretch")
        st.dataframe(r, width="stretch", hide_index=True)
else:
    st.dataframe(filtered, width="stretch", hide_index=True)
    st.download_button("Descargar CSV", filtered.to_csv(index=False).encode("utf-8-sig"), "tracking_error_peers.csv", "text/csv")
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer: filtered.to_excel(writer, index=False, sheet_name="Fondos")
    st.download_button("Descargar Excel", out.getvalue(), "tracking_error_peers.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
st.caption("BCI Asset Management · Tracking Error Leave-One-Out · datos precalculados del ZIP suministrado")
