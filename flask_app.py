from __future__ import annotations

import base64
import gzip
import json
import math
import os
import secrets
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from flask import Flask, flash, redirect, render_template, request, session, url_for

from cmf_automation import CMFAutomationError, CMFQuotaSession
from metrics_data import METRICS_GZ_B64
from quota_update import load_status, normalize_run, parse_quota_file, persist_quota, validate_quota_file
from series_data_1 import SERIES_GZ_B64_1
from series_data_2 import SERIES_GZ_B64_2
from series_data_3 import SERIES_GZ_B64_3

ROOT = Path(__file__).resolve().parent
SERIES_GZ_B64 = SERIES_GZ_B64_1 + SERIES_GZ_B64_2 + SERIES_GZ_B64_3
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=30)
ADMIN_PIN = os.getenv("ADMIN_PIN", "1405")
CMF_SESSIONS: dict[str, CMFQuotaSession] = {}


def _load_json(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


CONFIG = _load_json("fondos_config.json")["fondos"]
REFERENCE_ROWS = _load_json("panel_metrics_reference.json")
REFERENCE = {str(r["Fondo"]): r for r in REFERENCE_ROWS}


def load_data():
    metrics = json.loads(gzip.decompress(base64.b64decode(METRICS_GZ_B64)).decode("utf-8"))
    historical = json.loads(gzip.decompress(base64.b64decode(SERIES_GZ_B64)).decode("utf-8"))
    frame = pd.DataFrame(metrics)
    for col in ["te_ewma_anual", "te_equiponderado_anual", "IR", "ret_1y_fondo", "ret_1y_pares", "vol_anual", "exceso_1y"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame, historical


df, series = load_data()
EXPECTED_RUNS = {normalize_run(v.get("bci", "")) for v in CONFIG.values() if v.get("bci")}
for v in CONFIG.values():
    EXPECTED_RUNS.update(normalize_run(x) for x in v.get("peers", []))


def pct(x, digits=2, signed=False):
    if x is None or pd.isna(x):
        return "—"
    v = float(x) * 100
    s = f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"
    return s.replace(".", ",") + "%"


def number(x, digits=2):
    if x is None or pd.isna(x):
        return "—"
    return f"{float(x):.{digits}f}".replace(".", ",")


def run_from_series_col(col: str) -> str:
    """Extrae el RUN sin destruir dígitos verificadores, p.ej. 8514-6.

    Las columnas históricas vienen como 'RUN - Nombre'. Antes se hacía split('-')
    y 8514-6 terminaba como 8514, dejando Asia sin serie histórica.
    """
    text = str(col).strip()
    if " - " in text:
        return text.split(" - ", 1)[0].strip()
    return text


def config_by_name(name: str):
    for stem, cfg in CONFIG.items():
        if cfg["nombre"] == name:
            return stem, cfg
    return None, None


def bci_catalog():
    rows = []
    for stem, cfg in sorted(CONFIG.items(), key=lambda kv: (kv[1].get("orden", 999), kv[1]["nombre"])):
        rows.append({"run": str(cfg["bci"]), "fondo": cfg["nombre"], "categoria": cfg["nombre"], "stem": stem})
    return rows


def category_levels(name: str):
    stem, cfg = config_by_name(name)
    if stem is None:
        return pd.DataFrame()
    payload = series.get(stem)
    if payload is None:
        candidates = df[df.categoria.astype(str).str.replace(" *", "", regex=False) == name.replace(" *", "")]
        if not candidates.empty:
            payload = series.get(str(candidates.archivo.iloc[0]))
    if not payload:
        return pd.DataFrame()
    levels = pd.DataFrame(payload["valores"], index=pd.to_datetime(payload["fechas"]))
    return levels.apply(pd.to_numeric, errors="coerce").sort_index()


def configured_columns(name: str, levels: pd.DataFrame):
    _, cfg = config_by_name(name)
    if cfg is None or levels.empty:
        return None, []
    run_map = {normalize_run(run_from_series_col(c)): c for c in levels.columns}
    bci = run_map.get(normalize_run(cfg["bci"]))
    peers = [run_map.get(normalize_run(r)) for r in cfg.get("peers", [])]
    return bci, [c for c in peers if c is not None]


def ewma_te(active: pd.Series, lam=.94, annualize=True):
    x = active.dropna().astype(float).to_numpy()
    if len(x) < 2:
        return float("nan")
    w = (1 - lam) * lam ** np.arange(len(x) - 1, -1, -1, dtype=float)
    w /= w.sum()
    var = float(np.dot(w, x * x))
    return math.sqrt(max(var, 0.0) * (52 if annualize else 1))


def configured_ytd(name: str):
    levels = category_levels(name)
    bci_col, peer_cols = configured_columns(name, levels)
    cols = ([bci_col] if bci_col else []) + peer_cols
    if levels.empty or not cols:
        return pd.Series(dtype=float)
    levels = levels[cols].dropna(how="all")
    end = levels.index.max()
    start = pd.Timestamp(year=end.year, month=1, day=1)
    before = levels[levels.index < start]
    if before.empty:
        return pd.Series(dtype=float)
    base = before.iloc[-1]
    cut = levels.loc[:end].iloc[-1]
    return cut / base - 1.0


def historical_te(name: str):
    levels = category_levels(name)
    bci_col, peer_cols = configured_columns(name, levels)
    if levels.empty or bci_col is None or not peer_cols:
        return {"labels": [], "bci": [], "p75": []}

    funds = [bci_col, *peer_cols]
    returns = levels[funds].pct_change(fill_method=None).dropna(how="any")
    if len(returns) < 8:
        return {"labels": [], "bci": [], "p75": []}

    _, cfg = config_by_name(name)
    annualize = bool(cfg.get("te_anualizado", True))
    bci_hist = pd.Series(index=returns.index, dtype=float)
    peer_hist = pd.DataFrame(index=returns.index, columns=peer_cols, dtype=float)

    for i in range(7, len(returns)):
        window = returns.iloc[: i + 1].tail(52)
        # BCI siempre contra el promedio de los peers configurados en fondos_config.json.
        bci_active = window[bci_col] - window[peer_cols].mean(axis=1)
        bci_hist.loc[returns.index[i]] = ewma_te(bci_active, annualize=annualize)

        # Cada peer se compara contra el resto del conjunto configurado para construir P75.
        for peer in peer_cols:
            others = [c for c in funds if c != peer]
            peer_active = window[peer] - window[others].mean(axis=1)
            peer_hist.loc[returns.index[i], peer] = ewma_te(peer_active, annualize=annualize)

    bci_hist = bci_hist.dropna()
    peer_hist = peer_hist.loc[bci_hist.index]
    p75 = peer_hist.quantile(.75, axis=1)
    return {
        "labels": [d.strftime("%d-%m-%Y") for d in bci_hist.index],
        "bci": [round(float(v) * 100, 4) for v in bci_hist],
        "p75": [None if pd.isna(v) else round(float(v) * 100, 4) for v in p75],
    }


def peer_rows_for(name: str):
    """Métricas de cards usando exclusivamente el peer set del JSON."""
    _, cfg = config_by_name(name)
    levels = category_levels(name)
    bci_col, peer_cols = configured_columns(name, levels)
    if cfg is None or levels.empty or bci_col is None or not peer_cols:
        return []

    funds = [bci_col, *peer_cols]
    returns = levels[funds].pct_change(fill_method=None).dropna(how="any").tail(52)
    if returns.empty:
        return []

    ytd = configured_ytd(name)
    name_by_run = {}
    wanted = {normalize_run(cfg["bci"]), *[normalize_run(r) for r in cfg.get("peers", [])]}
    subset = df[df.run.astype(str).map(normalize_run).isin(wanted)].copy()
    for _, r in subset.iterrows():
        name_by_run[normalize_run(r.run)] = str(r.fondo)

    rows = []
    _, cfg_full = config_by_name(name)
    annualize = bool(cfg_full.get("te_anualizado", True))
    for fund in funds:
        run = run_from_series_col(fund)
        others = [c for c in funds if c != fund]
        active = returns[fund] - returns[others].mean(axis=1)
        te = ewma_te(active, annualize=annualize)
        alpha_1y = float((1 + returns[fund]).prod() - 1 - (((1 + returns[others]).prod() - 1).mean()))
        ir = float(active.mean() * 52 / te) if np.isfinite(te) and te != 0 else float("nan")

        p_label = "—"
        if fund in ytd.index and pd.notna(ytd[fund]):
            competitors = ytd[[c for c in funds if c != fund]].dropna()
            if not competitors.empty:
                p = float((competitors > float(ytd[fund])).mean() * 100)
                p_label = f"{p:.0f}"

        rows.append({
            "fondo": name_by_run.get(normalize_run(run), str(fund).split(" - ", 1)[-1]),
            "es_bci": normalize_run(run) == normalize_run(cfg["bci"]),
            "alpha": pct(alpha_1y, 2, True),
            "ir": number(ir, 2),
            "mer": pct(te, 2),
            "percentil": p_label,
        })

    rows.sort(key=lambda r: (not r["es_bci"], r["fondo"]))
    return rows


def fund_dashboard(selected_run: str):
    catalog = bci_catalog()
    choice = next((x for x in catalog if normalize_run(x["run"]) == normalize_run(selected_run)), catalog[0])
    name = choice["fondo"]
    _, cfg = config_by_name(name)
    ref = REFERENCE.get(name, {})
    percentile = ref.get("Percentil YTD")
    percentile_label = "—" if percentile is None or pd.isna(percentile) else f"{float(percentile) * 100:.0f}"
    quartile = ref.get("Cuartil YTD")
    quartile = None if quartile is None or pd.isna(quartile) else int(quartile)
    return {
        "run": str(cfg["bci"]),
        "fondo": name,
        "categoria": cfg.get("grupo", ""),
        "grupo": cfg.get("grupo", ""),
        "mer": pct(ref.get("TE EWMA anual"), 2),
        "ret_ytd": pct(ref.get("Retorno YTD"), 2, True),
        "alpha_1y": pct(ref.get("Alpha anual"), 2, True),
        "alpha_ytd": pct(ref.get("Alpha YTD"), 2, True),
        "ir_12m": number(ref.get("Information Ratio"), 2),
        "percentil_ytd": percentile_label,
        "cuartil_ytd": quartile,
        "peer_rows": peer_rows_for(name),
        "chart": historical_te(name),
    }


@app.get("/")
def dashboard():
    funds = bci_catalog()
    selected_run = request.args.get("fondo") or funds[0]["run"]
    return render_template("dashboard.html", funds=funds, selected=fund_dashboard(selected_run), status=load_status())


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
    asia = historical_te("Asia")
    cp = fund_dashboard("9060")
    return {
        "ok": True,
        "funds": len(CONFIG),
        "reference_rows": len(REFERENCE),
        "asia_chart_points": len(asia["labels"]),
        "cp_activa_ytd": cp["ret_ytd"],
        "cp_activa_peers": len(cp["peer_rows"]) - 1,
    }, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
