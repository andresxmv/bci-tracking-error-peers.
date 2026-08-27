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


def _norm_col(value: object) -> str:
    return str(value).strip().upper().replace(".", "").replace(" ", "")


def _run_aliases(run: object) -> list[str]:
    """RUN oficial y alias histórico sin dígito verificador cuando aplica.

    Caso concreto: fondos_config usa 8514-6, pero la serie embebida está
    rotulada 8514-ASIA. El RUN oficial sigue siendo 8514-6; el alias solo se
    usa para localizar la columna histórica.
    """
    target = normalize_run(run)
    aliases = [target]
    parts = target.rsplit("-", 1)
    if len(parts) == 2 and parts[0].isdigit() and (parts[1].isdigit() or parts[1] == "K"):
        aliases.append(parts[0])
    return aliases


def column_for_run(columns, run: object):
    """Encuentra un RUN dentro de nombres tipo '8514-ASIA' o '9060-FM...' ."""
    for target in _run_aliases(run):
        exact = []
        prefix = []
        for col in columns:
            norm = _norm_col(col)
            if norm == target:
                exact.append(col)
            elif norm.startswith(target + "-") or norm.startswith(target + "_") or norm.startswith(target + ":"):
                prefix.append(col)
            elif norm.startswith(target):
                prefix.append(col)
        if exact:
            return exact[0]
        if prefix:
            return prefix[0]
    return None


def _payload_to_levels(payload):
    if not payload or "valores" not in payload or "fechas" not in payload:
        return pd.DataFrame()
    return (
        pd.DataFrame(payload["valores"], index=pd.to_datetime(payload["fechas"]))
        .apply(pd.to_numeric, errors="coerce")
        .sort_index()
    )


def category_levels(name: str):
    """Busca la serie por contenido (RUNs), no por nombre del archivo."""
    stem, cfg = config_by_name(name)
    if cfg is None:
        return pd.DataFrame()

    candidates = []
    direct = series.get(stem)
    if direct:
        candidates.append(direct)

    meta = df[df.categoria.astype(str).str.replace(" *", "", regex=False) == name.replace(" *", "")]
    if not meta.empty:
        payload = series.get(str(meta.archivo.iloc[0]))
        if payload and payload not in candidates:
            candidates.append(payload)

    best_levels = pd.DataFrame()
    best_score = -1
    all_payloads = candidates + [p for p in series.values() if p not in candidates]
    for payload in all_payloads:
        levels = _payload_to_levels(payload)
        if levels.empty:
            continue
        bci_col = column_for_run(levels.columns, cfg["bci"])
        if bci_col is None:
            continue
        peer_hits = sum(column_for_run(levels.columns, r) is not None for r in cfg.get("peers", []))
        score = 1000 + peer_hits
        if score > best_score:
            best_score = score
            best_levels = levels
            if peer_hits == len(cfg.get("peers", [])):
                break
    return best_levels


def configured_columns(name: str, levels: pd.DataFrame):
    _, cfg = config_by_name(name)
    if cfg is None or levels.empty:
        return None, []
    bci = column_for_run(levels.columns, cfg["bci"])
    peers = [column_for_run(levels.columns, r) for r in cfg.get("peers", [])]
    peers = [c for c in peers if c is not None and c != bci]
    return bci, list(dict.fromkeys(peers))


def ewma_te(active: pd.Series, lam=.94, annualize=True):
    x = active.dropna().astype(float).to_numpy()
    if len(x) < 2:
        return float("nan")
    w = (1 - lam) * lam ** np.arange(len(x) - 1, -1, -1, dtype=float)
    w /= w.sum()
    var = float(np.dot(w, x * x))
    return math.sqrt(max(var, 0.0) * (52 if annualize else 1))


def historical_te(name: str):
    levels = category_levels(name)
    bci_col, peer_cols = configured_columns(name, levels)
    if levels.empty or bci_col is None or not peer_cols:
        return {"labels": [], "bci": [], "p75": []}

    use_cols = [bci_col, *peer_cols]
    returns = levels[use_cols].pct_change(fill_method=None).dropna(how="any")
    if len(returns) < 8:
        return {"labels": [], "bci": [], "p75": []}

    _, cfg = config_by_name(name)
    annualize = bool(cfg.get("te_anualizado", True))
    out = pd.DataFrame(index=returns.index, columns=use_cols, dtype=float)

    for i in range(7, len(returns)):
        window = returns.iloc[: i + 1].tail(52)
        for fund in use_cols:
            others = [c for c in use_cols if c != fund]
            active = window[fund] - window[others].mean(axis=1)
            out.loc[returns.index[i], fund] = ewma_te(active, annualize=annualize)

    out = out.dropna(how="all")
    if out.empty:
        return {"labels": [], "bci": [], "p75": []}
    p75 = out.quantile(.75, axis=1)
    return {
        "labels": [d.strftime("%d-%m-%Y") for d in out.index],
        "bci": [round(float(v) * 100, 4) if pd.notna(v) else None for v in out[bci_col]],
        "p75": [round(float(v) * 100, 4) if pd.notna(v) else None for v in p75],
    }


def peer_rows_for(name: str):
    _, cfg = config_by_name(name)
    if cfg is None or not cfg.get("peers"):
        return []
    runs = [str(cfg["bci"]), *[str(x) for x in cfg.get("peers", [])]]
    wanted = {normalize_run(x) for x in runs}
    subset = df[df.run.astype(str).map(normalize_run).isin(wanted)].copy()
    if subset.empty:
        return []

    rows = []
    for _, p in subset.sort_values("exceso_1y", ascending=False).iterrows():
        rows.append({
            "fondo": str(p.fondo),
            "es_bci": normalize_run(p.run) == normalize_run(cfg["bci"]),
            "alpha": pct(p.exceso_1y, 2, True),
            "ir": number(p.IR, 2),
            "mer": pct(p.te_ewma_anual, 2),
            "percentil": "—",
        })
    return rows


def fund_dashboard(
    selected_run: str,
    peer_runs: list[str] | None = None,
    cutoff_date: str | None = None,
):
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
    peer_runs = request.args.getlist("peer") if request.args.get("peer_config") == "1" else None
    cutoff_date = request.args.get("fecha_corte") if request.args.get("peer_config") == "1" else None
    return render_template(
        "dashboard.html",
        funds=funds,
        selected=fund_dashboard(selected_run, peer_runs, cutoff_date),
        status=load_status(),
    )


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
    europa = historical_te("Europa")
    cp = REFERENCE.get("CP Activa", {})
    return {
        "ok": True,
        "funds": len(CONFIG),
        "reference_rows": len(REFERENCE),
        "asia_chart_points": len(asia["labels"]),
        "europa_chart_points": len(europa["labels"]),
        "cp_activa_ytd": cp.get("Retorno YTD"),
        "cp_activa_peers": len(CONFIG["cp_activa"].get("peers", [])),
    }, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
