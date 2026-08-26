from __future__ import annotations

import base64
import gzip
import io
import json
import math

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from metrics_data import METRICS_GZ_B64
from series_data_1 import SERIES_GZ_B64_1
from series_data_2 import SERIES_GZ_B64_2
from series_data_3 import SERIES_GZ_B64_3

SERIES_GZ_B64 = SERIES_GZ_B64_1 + SERIES_GZ_B64_2 + SERIES_GZ_B64_3

st.set_page_config(page_title="Tracking Error Peers · BCI", page_icon="📊", layout="wide")

NAVY = "#003578"
BLUE = "#0B63C5"
BORDER = "#E2E8F0"
TEXT = "#475569"
AMBER = "#F59E0B"
PEER = "#CBD5E1"

st.markdown(
    f"""
    <style>
    .main .block-container{{max-width:96%;padding-top:1.2rem}}
    .bci-header{{background:linear-gradient(135deg,{NAVY},#00224d);padding:1.35rem 1.7rem;border-radius:12px;color:white;margin-bottom:1rem}}
    .bci-header h1{{color:white!important;margin:0!important;font-size:1.8rem!important}}
    .bci-header p{{color:#B9D4F2;margin:.35rem 0 0}}
    .card{{background:white;border:1px solid {BORDER};border-top:3px solid {NAVY};border-radius:10px;padding:1rem 1.1rem}}
    .klabel{{font-size:.75rem;text-transform:uppercase;color:{TEXT};font-weight:700}}
    .kval{{font-size:1.55rem;color:{NAVY};font-weight:750}}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data
def load_data():
    metrics = json.loads(gzip.decompress(base64.b64decode(METRICS_GZ_B64)).decode("utf-8"))
    historical = json.loads(gzip.decompress(base64.b64decode(SERIES_GZ_B64)).decode("utf-8"))
    frame = pd.DataFrame(metrics)
    numeric = [
        "te_ewma_anual",
        "te_equiponderado_anual",
        "IR",
        "ret_1y_fondo",
        "ret_1y_pares",
        "vol_anual",
        "exceso_1y",
    ]
    for col in numeric:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["es_bci"] = frame["es_bci"].astype(bool)
    return frame, historical


def pct(x, d=2, sign=False):
    if pd.isna(x):
        return "N/D"
    fmt = f"{x * 100:+.{d}f}" if sign else f"{x * 100:.{d}f}"
    return fmt.replace(".", ",") + "%"


def num(x, d=2, sign=False):
    if pd.isna(x):
        return "N/D"
    fmt = f"{x:+.{d}f}" if sign else f"{x:.{d}f}"
    return fmt.replace(".", ",")


def run_from_series_col(col: str) -> str:
    return str(col).split("-", 1)[0].strip()


def ewma_tracking_error(active: pd.Series, lam: float = 0.94) -> float:
    """EWMA de la desviación activa, anualizado por sqrt(52)."""
    x = active.dropna().astype(float).to_numpy()
    if len(x) < 2:
        return float("nan")
    weights = lam ** np.arange(len(x) - 1, -1, -1, dtype=float)
    weights /= weights.sum()
    mean = float(np.dot(weights, x))
    variance = float(np.dot(weights, (x - mean) ** 2))
    return math.sqrt(max(variance, 0.0)) * math.sqrt(52.0)


@st.cache_data
def historical_te_for_stem(stem: str, min_obs: int = 8, lam: float = 0.94):
    """Serie semanal de TE leave-one-out y P75 para una categoría.

    Con las 52 semanas históricas incluidas en el ZIP se obtiene una serie
    expandible: parte al completar min_obs retornos y usa hasta 52 retornos
    disponibles en cada fecha.
    """
    payload = series.get(stem)
    if not payload:
        return pd.DataFrame(), pd.Series(dtype=float)

    levels = pd.DataFrame(payload["valores"], index=pd.to_datetime(payload["fechas"]))
    levels = levels.apply(pd.to_numeric, errors="coerce").dropna(how="any")
    returns = levels.pct_change(fill_method=None).dropna(how="any")
    if returns.shape[0] < min_obs or returns.shape[1] < 3:
        return pd.DataFrame(), pd.Series(dtype=float)

    out = pd.DataFrame(index=returns.index, columns=returns.columns, dtype=float)
    for i in range(min_obs - 1, len(returns)):
        window = returns.iloc[: i + 1].tail(52)
        for fund in returns.columns:
            peers = [c for c in returns.columns if c != fund]
            active = window[fund] - window[peers].mean(axis=1)
            out.loc[returns.index[i], fund] = ewma_tracking_error(active, lam=lam)

    out = out.dropna(how="all")
    p75 = out.quantile(0.75, axis=1, interpolation="linear")
    return out, p75


def bci_series_column(category_df: pd.DataFrame, te_hist: pd.DataFrame):
    if te_hist.empty or not category_df.es_bci.any():
        return None
    bci_run = str(category_df.loc[category_df.es_bci, "run"].iloc[0])
    matches = [c for c in te_hist.columns if run_from_series_col(c) == bci_run]
    return matches[0] if matches else None


def te_history_chart(category_name: str, height: int = 390):
    category_df = df[df.categoria == category_name].copy()
    if category_df.empty:
        return None
    stem = str(category_df.archivo.iloc[0])
    te_hist, p75 = historical_te_for_stem(stem)
    if te_hist.empty:
        return None

    bci_col = bci_series_column(category_df, te_hist)
    fig = go.Figure()

    for col in te_hist.columns:
        if col == bci_col:
            continue
        fig.add_trace(
            go.Scatter(
                x=te_hist.index,
                y=te_hist[col],
                mode="lines",
                name=str(col),
                line=dict(width=1.0, color=PEER),
                opacity=0.55,
                showlegend=False,
                hovertemplate="%{x|%d-%m-%Y}<br>TE: %{y:.2%}<extra>" + str(col) + "</extra>",
            )
        )

    if bci_col is not None:
        fig.add_trace(
            go.Scatter(
                x=te_hist.index,
                y=te_hist[bci_col],
                mode="lines",
                name="BCI",
                line=dict(width=3.2, color=NAVY),
                hovertemplate="%{x|%d-%m-%Y}<br>TE BCI: %{y:.2%}<extra>BCI</extra>",
            )
        )

    fig.add_trace(
        go.Scatter(
            x=p75.index,
            y=p75.values,
            mode="lines",
            name="Percentil 75",
            line=dict(width=3.0, color=AMBER, dash="dash"),
            hovertemplate="%{x|%d-%m-%Y}<br>P75: %{y:.2%}<extra>Percentil 75</extra>",
        )
    )

    fig.update_layout(
        title=f"{category_name} · Tracking Error histórico vs P75",
        height=height,
        xaxis_title="",
        yaxis_title="Tracking Error EWMA anualizado",
        yaxis_tickformat=".2%",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        plot_bgcolor="white",
        paper_bgcolor="white",
        margin=dict(l=25, r=20, t=70, b=35),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#F1F5F9")
    fig.update_yaxes(showgrid=True, gridcolor="#F1F5F9")
    return fig


df, series = load_data()
cut = pd.to_datetime(df["fecha_corte"]).max().strftime("%d-%m-%Y")

st.sidebar.markdown("## 🏢 BCI Asset Management")
st.sidebar.caption("Riesgo de Mercado · Monitoreo Peers")
view = st.sidebar.radio(
    "Vista",
    [
        "📊 Dashboard por categoría",
        "📈 TE histórico + P75",
        "🏢 Resumen ejecutivo",
        "🔍 Alfa vs Tracking Error",
        "⚔️ BCI vs Peer",
        "📥 Datos",
    ],
)
groups = ["Todos"] + sorted(df.grupo.dropna().unique().tolist())
group = st.sidebar.selectbox("Grupo", groups)
base = df if group == "Todos" else df[df.grupo == group]
cats = sorted(base.categoria.unique())
cat = st.sidebar.selectbox("Categoría / Peer Group", cats)
st.sidebar.info(f"52 semanas · EWMA λ=0,94 · anualización √52 · corte {cut}")

st.markdown(
    f'<div class="bci-header"><h1>Tracking Error Peers · Leave-One-Out</h1><p>Fondo BCI versus promedio de los demás fondos de cada peer group · corte {cut}</p></div>',
    unsafe_allow_html=True,
)

if view == "📊 Dashboard por categoría":
    d = df[df.categoria == cat].sort_values("te_ewma_anual", ascending=False).copy()
    b = d[d.es_bci].iloc[0] if d.es_bci.any() else None
    st.subheader(cat)

    if b is not None:
        med = d.te_ewma_anual.median()
        p75_now = d.te_ewma_anual.quantile(0.75)
        rank = int(b["rank"])
        n = len(d)
        cols = st.columns(6)
        vals = [
            ("TE EWMA BCI", pct(b.te_ewma_anual, 4)),
            ("P75 categoría", pct(p75_now, 4)),
            ("Ranking", f"#{rank} de {n}"),
            ("Exceso 1A", pct(b.exceso_1y, 2, True)),
            ("Information Ratio", num(b.IR, 2, True)),
            ("Volatilidad", pct(b.vol_anual, 2)),
        ]
        for col, (lab, val) in zip(cols, vals):
            col.markdown(
                f'<div class="card"><div class="klabel">{lab}</div><div class="kval">{val}</div></div>',
                unsafe_allow_html=True,
            )

        fig = px.bar(
            d,
            x="te_ewma_anual",
            y="fondo",
            orientation="h",
            color="es_bci",
            color_discrete_map={True: NAVY, False: "#94A3B8"},
            hover_data=["run", "IR", "exceso_1y"],
        )
        fig.add_vline(x=med, line_dash="dash", line_color=BLUE, annotation_text="Mediana")
        fig.add_vline(x=p75_now, line_dash="dot", line_color=AMBER, annotation_text="P75")
        fig.update_layout(
            height=max(390, 42 * len(d)),
            showlegend=False,
            xaxis_tickformat=".2%",
            yaxis_title="",
            xaxis_title="Tracking Error EWMA anualizado",
        )
        st.plotly_chart(fig, use_container_width=True)

    hist_fig = te_history_chart(cat, height=430)
    if hist_fig is not None:
        st.plotly_chart(hist_fig, use_container_width=True)
        st.caption(
            "La curva histórica se calcula con los retornos semanales disponibles en el ZIP: "
            "EWMA λ=0,94, leave-one-out, anualizado por √52. P75 es el percentil 75 de los TE de la categoría en cada fecha."
        )

    stem = d.archivo.iloc[0]
    hist = series.get(stem)
    if hist:
        h = pd.DataFrame(hist["valores"], index=pd.to_datetime(hist["fechas"]))
        fig2 = px.line(h, title="Series históricas base 100 · 52 semanas")
        fig2.update_layout(height=430, xaxis_title="", yaxis_title="Índice base 100", legend_title="Fondo")
        st.plotly_chart(fig2, use_container_width=True)

    show = d[["fondo", "run", "es_bci", "te_ewma_anual", "exceso_1y", "IR", "vol_anual", "rank"]].copy()
    show.columns = ["Fondo", "RUN", "BCI", "TE EWMA", "Exceso 1A", "IR", "Volatilidad", "Ranking"]
    st.dataframe(
        show,
        use_container_width=True,
        hide_index=True,
        column_config={
            "TE EWMA": st.column_config.NumberColumn(format="%.4f"),
            "Exceso 1A": st.column_config.NumberColumn(format="%.4f"),
            "Volatilidad": st.column_config.NumberColumn(format="%.4f"),
        },
    )

elif view == "📈 TE histórico + P75":
    st.subheader("Tracking Error histórico y percentil 75 por categoría")
    st.write(
        "Cada gráfico muestra los TE leave-one-out de todos los fondos de la categoría. "
        "BCI va destacado y la línea punteada corresponde al percentil 75 transversal de la categoría en cada fecha."
    )
    categories_to_plot = sorted(base.categoria.unique())
    for category_name in categories_to_plot:
        fig = te_history_chart(category_name, height=360)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info(f"{category_name}: no hay historia suficiente para calcular la curva de TE.")

elif view == "🏢 Resumen ejecutivo":
    rows = []
    for category_name, g in df.groupby("categoria"):
        if not g.es_bci.any():
            continue
        b = g[g.es_bci].iloc[0]
        rows.append(
            {
                "Categoría": category_name,
                "Grupo": b.grupo,
                "Fondo BCI": b.fondo,
                "TE BCI": b.te_ewma_anual,
                "Mediana peers": g.te_ewma_anual.median(),
                "P75 peers": g.te_ewma_anual.quantile(0.75),
                "Ranking": int(b["rank"]),
                "N": len(g),
                "Exceso 1A": b.exceso_1y,
                "IR": b.IR,
            }
        )
    r = pd.DataFrame(rows).sort_values("TE BCI", ascending=False)
    a, b, c, d = st.columns(4)
    a.metric("Categorías", len(r))
    b.metric("Fondos mercado", len(df))
    c.metric("BCI bajo P75", int((r["TE BCI"] < r["P75 peers"]).sum()))
    d.metric("Exceso positivo", int((r["Exceso 1A"] > 0).sum()))

    fig = go.Figure()
    fig.add_bar(name="BCI", x=r["Categoría"], y=r["TE BCI"])
    fig.add_scatter(name="P75 peers", x=r["Categoría"], y=r["P75 peers"], mode="markers")
    fig.update_layout(height=480, yaxis_tickformat=".2%", xaxis_tickangle=-35, yaxis_title="Tracking Error")
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(r, use_container_width=True, hide_index=True)

elif view == "🔍 Alfa vs Tracking Error":
    fig = px.scatter(
        df,
        x="te_ewma_anual",
        y="exceso_1y",
        color="categoria",
        symbol="es_bci",
        symbol_map={True: "star", False: "circle"},
        hover_name="fondo",
        hover_data=["run", "IR", "vol_anual"],
    )
    fig.add_hline(y=0, line_color="#64748B")
    fig.update_layout(
        height=650,
        xaxis_tickformat=".2%",
        yaxis_tickformat=".2%",
        xaxis_title="Tracking Error EWMA",
        yaxis_title="Exceso de retorno 1A vs peers",
    )
    st.plotly_chart(fig, use_container_width=True)

elif view == "⚔️ BCI vs Peer":
    d = df[df.categoria == cat]
    b = d[d.es_bci].iloc[0]
    peers = d[~d.es_bci]
    peer_name = st.selectbox("Competidor", peers.fondo.tolist())
    p = peers[peers.fondo == peer_name].iloc[0]
    left, right = st.columns(2)
    for col, row, title in [(left, b, "Fondo BCI"), (right, p, "Peer")]:
        col.subheader(title)
        col.write(f"**{row.fondo}** · RUN {row.run}")
        col.metric("TE EWMA", pct(row.te_ewma_anual, 4))
        col.metric("Exceso 1A", pct(row.exceso_1y, 2, True))
        col.metric("IR", num(row.IR, 2, True))
        col.metric("Volatilidad", pct(row.vol_anual, 2))

    comp = pd.DataFrame(
        {
            "Métrica": ["TE EWMA", "Retorno 1A", "Exceso 1A", "Volatilidad"],
            "BCI": [b.te_ewma_anual, b.ret_1y_fondo, b.exceso_1y, b.vol_anual],
            "Peer": [p.te_ewma_anual, p.ret_1y_fondo, p.exceso_1y, p.vol_anual],
        }
    )
    fig = px.bar(comp, x="Métrica", y=["BCI", "Peer"], barmode="group")
    fig.update_layout(yaxis_tickformat=".2%", height=420)
    st.plotly_chart(fig, use_container_width=True)

else:
    st.subheader("Dataset consolidado")
    st.write(f"{len(df)} fondos · {df.categoria.nunique()} categorías · {int(df.es_bci.sum())} fondos BCI")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "Descargar CSV",
        df.to_csv(index=False).encode("utf-8-sig"),
        "tracking_error_leave_one_out.csv",
        "text/csv",
    )
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Fondos")
    st.download_button(
        "Descargar Excel",
        out.getvalue(),
        "tracking_error_leave_one_out.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

st.markdown("---")
st.caption("BCI Asset Management · Tracking Error Leave-One-Out · datos precalculados del ZIP suministrado")
