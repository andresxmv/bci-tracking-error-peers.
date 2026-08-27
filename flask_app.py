from __future__ import annotations

import base64
import gzip
import json
import math
import os
import secrets
from datetime import timedelta

import numpy as np
import pandas as pd
from flask import Flask, flash, redirect, render_template, request, session, url_for

from cmf_automation import CMFAutomationError, CMFQuotaSession
from metrics_data import METRICS_GZ_B64
from quota_update import load_status, normalize_run, parse_quota_file, persist_quota, validate_quota_file
from series_data_1 import SERIES_GZ_B64_1
from series_data_2 import SERIES_GZ_B64_2
from series_data_3 import SERIES_GZ_B64_3

SERIES_GZ_B64 = SERIES_GZ_B64_1 + SERIES_GZ_B64_2 + SERIES_GZ_B64_3

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=30)
ADMIN_PIN = os.getenv("ADMIN_PIN", "1405")
CMF_SESSIONS: dict[str, CMFQuotaSession] = {}

# RUNs BCI confirmados desde la fuente original. Nunca se debe inferir un
# fondo BCI por cercanía de métricas cuando conocemos su RUN.
BCI_RUNS = {
    "CP Activa": "9060",
}

# Valores del panel Python original al 25-08-2026. Solo sirven como fallback
# para categorías cuyo RUN BCI aún no está incorporado en BCI_RUNS.
BCI_REFERENCE = {
    "CD Activa": {"te": .0163, "alpha1y": .0259, "ir": 1.47},
    "CD Balanceada": {"te": .0126, "alpha1y": .0128, "ir": .96},
    "CD Conservadora": {"te": .0075, "alpha1y": .0074, "ir": .91},
    "CP Activa": {"te": .0147, "alpha1y": .0280, "ir": 1.86},
    "CP Balanceada": {"te": .0126, "alpha1y": .0327, "ir": 2.36},
    "CP Conservadora": {"te": .0062, "alpha1y": .0059, "ir": .78},
    "Estratégico $ H 1 Año": {"te": .0015, "alpha1y": .0008, "ir": .56},
    "Estratégico UF H 1 Año": {"te": .0015, "alpha1y": -.0004, "ir": -.17},
    "Estratégico UF H 3 Años": {"te": .0015, "alpha1y": -.0011, "ir": -.63},
    "Estratégico UF H 5 Años": {"te": .0019, "alpha1y": -.0010, "ir": -.44},
    "Estratégico UF > 5 Años": {"te": .0020, "alpha1y": -.0003, "ir": .07},
    "Asia": {"te": .0402, "alpha1y": -.0047, "ir": -.06},
    "Europa": {"te": .0259, "alpha1y": .0407, "ir": 1.55},
    "Emergente Global": {"te": .0490, "alpha1y": .0095, "ir": .16},
    "Estados Unidos": {"te": .0339, "alpha1y": .0224, "ir": .71},
    "Global Titan": {"te": .0395, "alpha1y": .0542, "ir": 1.36},
    "Acciones Chilenas *": {"te": .0270, "alpha1y": .0226, "ir": 1.20},
    "Top Picks *": {"te": .0416, "alpha1y": .0326, "ir": 1.12},
    "Acciones Globales *": {"te": .0858, "alpha1y": .0043, "ir": .10},
    "América Latina *": {"te": .0152, "alpha1y": -.0206, "ir": -.29},
}


def load_data():
    metrics = json.loads(gzip.decompress(base64.b64decode(METRICS_GZ_B64)).decode("utf-8"))
    historical = json.loads(gzip.decompress(base64.b64decode(SERIES_GZ_B64)).decode("utf-8"))
    frame = pd.DataFrame(metrics)
    for col in ["te_ewma_anual", "te_equiponderado_anual", "IR", "ret_1y_fondo", "ret_1y_pares", "vol_anual", "exceso_1y"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["es_bci"] = frame["es_bci"].astype(bool)
    return frame, historical


df, series = load_data()
EXPECTED_RUNS = {normalize_run(x) for x in df["run"].dropna().astype(str)}


def pct(x, digits=2, signed=False):
    if pd.isna(x):
        return "—"
    v = float(x) * 100
    s = f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"
    return s.replace(".", ",") + "%"


def number(x, digits=2):
    if pd.isna(x):
        return "—"
    return f"{float(x):.{digits}f}".replace(".", ",")


def percentile_high_good(values: pd.Series, value: float) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty or pd.isna(value):
        return None
    return float((clean <= float(value)).mean() * 100.0)


def quartile_from_percentile(p: float | None) -> int | None:
    if p is None or pd.isna(p):
        return None
    if p >= 75:
        return 1
    if p >= 50:
        return 2
    if p >= 25:
        return 3
    return 4


def run_from_series_col(col: str) -> str:
    return str(col).split("-", 1)[0].strip()


def category_levels(category: str) -> pd.DataFrame:
    g = df[df.categoria == category]
    if g.empty:
        return pd.DataFrame()
    payload = series.get(str(g.archivo.iloc[0]))
    if not payload:
        return pd.DataFrame()
    levels = pd.DataFrame(payload["valores"], index=pd.to_datetime(payload["fechas"]))
    return levels.apply(pd.to_numeric, errors="coerce").sort_index()


def map_run_to_col(levels: pd.DataFrame) -> dict[str, str]:
    return {run_from_series_col(c): c for c in levels.columns}


def bci_row_for_category(category: str):
    peers = df[df.categoria == category].copy()
    if peers.empty:
        return None

    exact_run = BCI_RUNS.get(category)
    if exact_run is not None:
        exact = peers[peers.run.astype(str).map(normalize_run) == normalize_run(exact_run)]
        if not exact.empty:
            return exact.iloc[0]

    ref = BCI_REFERENCE.get(category)
    if ref is None:
        marked = peers[peers.es_bci]
        return marked.iloc[0] if not marked.empty else peers.iloc[0]
    score = (
        (peers.te_ewma_anual.sub(ref["te"]).abs() / .01)
        + (peers.exceso_1y.sub(ref["alpha1y"]).abs() / .02)
        + (peers.IR.sub(ref["ir"]).abs() / 1.0)
    )
    return peers.loc[score.idxmin()]


def bci_catalog():
    rows = []
    for category in df.categoria.dropna().unique():
        row = bci_row_for_category(str(category))
        if row is not None:
            rows.append({"run": str(row.run), "fondo": str(category), "categoria": str(category)})
    return sorted(rows, key=lambda x: x["fondo"])


def ytd_metrics(category: str) -> pd.DataFrame:
    """R_YTD = VC_corte / VC_cierre_anterior - 1, como el panel Python."""
    g = df[df.categoria == category].copy()
    levels = category_levels(category)
    if g.empty or levels.empty:
        return pd.DataFrame()
    end = levels.index.max()
    year_start = pd.Timestamp(year=end.year, month=1, day=1)
    before = levels[levels.index < year_start]
    current = levels[levels.index <= end]
    if current.empty:
        return pd.DataFrame()
    close_prior = before.iloc[-1] if not before.empty else current.iloc[0]
    close_cut = current.iloc[-1]
    rets = close_cut / close_prior - 1.0
    run_map = map_run_to_col(levels)
    rows = []
    for _, r in g.iterrows():
        run = str(r.run)
        col = run_map.get(run)
        if not col or col not in rets.index or pd.isna(rets[col]):
            continue
        fund_ret = float(rets[col])
        peer_cols = [run_map.get(str(x)) for x in g.run if str(x) != run]
        peer_cols = [c for c in peer_cols if c in rets.index and pd.notna(rets[c])]
        peer_ret = float(rets[peer_cols].mean()) if peer_cols else float("nan")
        rows.append({"run": run, "ret_ytd": fund_ret, "peer_ytd": peer_ret, "alpha_ytd": fund_ret - peer_ret})
    return pd.DataFrame(rows)


def active_weekly(category: str, selected_run: str) -> pd.Series:
    levels = category_levels(category)
    if levels.empty:
        return pd.Series(dtype=float)
    rets = levels.pct_change(fill_method=None).dropna(how="all")
    run_map = map_run_to_col(levels)
    selected_col = run_map.get(str(selected_run))
    if not selected_col or selected_col not in rets.columns:
        return pd.Series(dtype=float)
    peer_cols = [c for r, c in run_map.items() if r != str(selected_run) and c in rets.columns]
    if not peer_cols:
        return pd.Series(dtype=float)
    return (rets[selected_col] - rets[peer_cols].mean(axis=1)).dropna().tail(52)


def ewma_tracking_error(active: pd.Series, lam: float = .94) -> float:
    x = active.dropna().astype(float).to_numpy()
    if len(x) < 2:
        return float("nan")
    w = lam ** np.arange(len(x) - 1, -1, -1, dtype=float)
    w /= w.sum()
    return math.sqrt(max(float(np.dot(w, x**2)), 0.0)) * math.sqrt(52)


def information_ratio_12m(category: str, selected_run: str) -> float:
    active = active_weekly(category, selected_run)
    if active.empty:
        return float("nan")
    te = ewma_tracking_error(active)
    if not np.isfinite(te) or te == 0:
        return float("nan")
    return float(active.mean() * 52.0 / te)


def historical_te(category: str, selected_run: str):
    levels = category_levels(category)
    if levels.empty:
        return {"labels": [], "bci": [], "p75": []}
    returns = levels.pct_change(fill_method=None).dropna(how="any")
    if returns.shape[0] < 8:
        return {"labels": [], "bci": [], "p75": []}
    out = pd.DataFrame(index=returns.index, columns=returns.columns, dtype=float)
    for i in range(7, len(returns)):
        window = returns.iloc[: i + 1].tail(52)
        for fund in returns.columns:
            peers = [c for c in returns.columns if c != fund]
            out.loc[returns.index[i], fund] = ewma_tracking_error(window[fund] - window[peers].mean(axis=1))
    out = out.dropna(how="all")
    p75 = out.quantile(.75, axis=1)
    selected_col = next((c for c in out.columns if run_from_series_col(c) == str(selected_run)), None)
    chosen = out[selected_col] if selected_col else pd.Series(index=out.index, dtype=float)
    return {
        "labels": [d.strftime("%d-%m-%Y") for d in out.index],
        "bci": [None if pd.isna(v) else round(float(v) * 100, 4) for v in chosen],
        "p75": [None if pd.isna(v) else round(float(v) * 100, 4) for v in p75],
    }


def fund_dashboard(selected_run: str):
    catalog = bci_catalog()
    choice = next((x for x in catalog if x["run"] == str(selected_run)), catalog[0])
    category = choice["categoria"]
    row = bci_row_for_category(category)
    peers = df[df.categoria == category].copy()

    ytd = ytd_metrics(category)
    selected_ytd = ytd[ytd.run.astype(str) == str(row.run)] if not ytd.empty else pd.DataFrame()
    ret_ytd = alpha_ytd = float("nan")
    pytd = None
    qytd = None
    if not selected_ytd.empty:
        ret_ytd = float(selected_ytd.ret_ytd.iloc[0])
        alpha_ytd = float(selected_ytd.alpha_ytd.iloc[0])
        pytd = percentile_high_good(ytd.ret_ytd, ret_ytd)
        qytd = quartile_from_percentile(pytd)

    peer_rows = []
    for _, p in peers.sort_values("exceso_1y", ascending=False).iterrows():
        peer_rows.append({
            "fondo": p.fondo,
            "es_bci": normalize_run(str(p.run)) == normalize_run(str(row.run)),
            "alpha": pct(p.exceso_1y, 2, True),
            "ir": number(p.IR, 2),
            "mer": pct(p.te_ewma_anual, 2),
            "percentil": f"{percentile_high_good(peers.exceso_1y, p.exceso_1y):.0f}",
        })

    ir = information_ratio_12m(category, str(row.run))
    te_current = ewma_tracking_error(active_weekly(category, str(row.run)))

    return {
        "run": str(row.run),
        "fondo": category,
        "categoria": category,
        "grupo": row.grupo,
        "mer": pct(te_current if np.isfinite(te_current) else row.te_ewma_anual, 2),
        "ret_ytd": pct(ret_ytd, 2, True),
        "alpha_1y": pct(row.exceso_1y, 2, True),
        "alpha_ytd": pct(alpha_ytd, 2, True),
        "ir_12m": number(ir if np.isfinite(ir) else row.IR, 2),
        "percentil_ytd": "—" if pytd is None else f"{pytd:.0f}",
        "cuartil_ytd": qytd,
        "peer_rows": peer_rows,
        "chart": historical_te(category, str(row.run)),
    }


@app.get("/")
def dashboard():
    funds = bci_catalog()
    selected_run = request.args.get("fondo") or funds[0]["run"]
    selected = fund_dashboard(selected_run)
    return render_template("dashboard.html", funds=funds, selected=selected, status=load_status())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("pin", "") == ADMIN_PIN:
            session.permanent = True
            session["admin_ok"] = True
            return redirect(url_for("update_quota"))
        flash("PIN incorrecto", "error")
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))


def require_admin():
    if not session.get("admin_ok"):
        return redirect(url_for("login"))
    return None


@app.route("/actualizar", methods=["GET", "POST"])
def update_quota():
    guard = require_admin()
    if guard:
        return guard
    captcha_b64 = None
    prepared_date = session.get("prepared_date")
    if request.method == "POST" and request.form.get("action") == "prepare":
        target = pd.to_datetime(request.form.get("fecha"), errors="coerce")
        if pd.isna(target):
            flash("Selecciona una fecha válida.", "error")
        else:
            token = secrets.token_urlsafe(18)
            cmf = CMFQuotaSession()
            try:
                prepared = cmf.prepare(target.date())
                old = session.get("cmf_token")
                if old and old in CMF_SESSIONS:
                    CMF_SESSIONS.pop(old).close()
                CMF_SESSIONS[token] = cmf
                session["cmf_token"] = token
                session["prepared_date"] = target.strftime("%Y-%m-%d")
                captcha_b64 = base64.b64encode(prepared.image).decode("ascii")
            except Exception as exc:
                cmf.close()
                flash(f"No pude preparar CMF: {exc}", "error")
    elif request.method == "POST" and request.form.get("action") == "submit":
        token = session.get("cmf_token")
        cmf = CMF_SESSIONS.get(token)
        if cmf is None:
            flash("La sesión CMF venció. Vuelve a preparar el captcha.", "error")
        else:
            try:
                payload, filename = cmf.submit_captcha(request.form.get("captcha", ""))
                frame = parse_quota_file(payload, filename)
                validation = validate_quota_file(frame, EXPECTED_RUNS)
                if not validation["ok"]:
                    flash(f"CMF respondió, pero la cobertura fue {validation['coverage']:.1%}. No se guardó.", "error")
                else:
                    saved = persist_quota(frame, filename, validation)
                    flash(f"Actualización guardada: {saved['latest_date']} · {saved['matched_runs']}/{saved['expected_runs']} RUN.", "success")
                    session.pop("cmf_token", None)
                    session.pop("prepared_date", None)
            except CMFAutomationError as exc:
                flash(str(exc), "error")
            except Exception as exc:
                flash(f"No pude completar la actualización: {exc}", "error")
            finally:
                try:
                    cmf.close()
                except Exception:
                    pass
                CMF_SESSIONS.pop(token, None)
    return render_template("update.html", status=load_status(), captcha_b64=captcha_b64, prepared_date=prepared_date or session.get("prepared_date"))


@app.get("/health")
def health():
    return {"ok": True}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
