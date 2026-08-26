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

ADMIN_PIN = os.getenv("ADMIN_PIN", "147654")
CMF_SESSIONS: dict[str, CMFQuotaSession] = {}


def load_data():
    metrics = json.loads(gzip.decompress(base64.b64decode(METRICS_GZ_B64)).decode("utf-8"))
    historical = json.loads(gzip.decompress(base64.b64decode(SERIES_GZ_B64)).decode("utf-8"))
    df = pd.DataFrame(metrics)
    for col in ["te_ewma_anual", "te_equiponderado_anual", "IR", "ret_1y_fondo", "ret_1y_pares", "vol_anual", "exceso_1y"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["es_bci"] = df["es_bci"].astype(bool)
    return df, historical


df, series = load_data()
EXPECTED_RUNS = {normalize_run(x) for x in df["run"].dropna().astype(str)}


def pct(x, digits=2, signed=False):
    if pd.isna(x):
        return "—"
    v = x * 100
    s = f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"
    return s.replace(".", ",") + "%"


def quartile(rank, n):
    if pd.isna(rank) or not n:
        return None
    return max(1, min(4, math.ceil(float(rank) / n * 4)))


def ewma_tracking_error(active: pd.Series, lam: float = .94) -> float:
    x = active.dropna().astype(float).to_numpy()
    if len(x) < 2:
        return float("nan")
    w = lam ** np.arange(len(x) - 1, -1, -1, dtype=float)
    w /= w.sum()
    mu = float(np.dot(w, x))
    var = float(np.dot(w, (x - mu) ** 2))
    return math.sqrt(max(var, 0)) * math.sqrt(52)


def historical_te(category: str):
    g = df[df.categoria == category].copy()
    if g.empty:
        return {"labels": [], "bci": [], "p75": []}
    payload = series.get(str(g.archivo.iloc[0]))
    if not payload:
        return {"labels": [], "bci": [], "p75": []}
    levels = pd.DataFrame(payload["valores"], index=pd.to_datetime(payload["fechas"]))
    levels = levels.apply(pd.to_numeric, errors="coerce").dropna(how="any")
    returns = levels.pct_change(fill_method=None).dropna(how="any")
    if returns.shape[0] < 8:
        return {"labels": [], "bci": [], "p75": []}
    out = pd.DataFrame(index=returns.index, columns=returns.columns, dtype=float)
    for i in range(7, len(returns)):
        window = returns.iloc[:i+1].tail(52)
        for fund in returns.columns:
            peers = [c for c in returns.columns if c != fund]
            out.loc[returns.index[i], fund] = ewma_tracking_error(window[fund] - window[peers].mean(axis=1))
    out = out.dropna(how="all")
    p75 = out.quantile(.75, axis=1)
    bci_run = str(g[g.es_bci].run.iloc[0]) if g.es_bci.any() else None
    bci_col = None
    if bci_run:
        for c in out.columns:
            if str(c).split("-", 1)[0].strip() == bci_run:
                bci_col = c
                break
    return {
        "labels": [d.strftime("%d-%m-%Y") for d in out.index],
        "bci": [None if pd.isna(v) else round(float(v) * 100, 4) for v in (out[bci_col] if bci_col else pd.Series(index=out.index, dtype=float))],
        "p75": [None if pd.isna(v) else round(float(v) * 100, 4) for v in p75],
    }


@app.get("/")
def dashboard():
    categories = sorted(df.categoria.unique())
    category = request.args.get("categoria") or categories[0]
    g = df[df.categoria == category].sort_values("te_ewma_anual", ascending=False).copy()
    rows = []
    for _, r in g.iterrows():
        n = len(g)
        rows.append({
            "fondo": r.fondo,
            "grupo": r.grupo,
            "es_bci": bool(r.es_bci),
            "te": pct(r.te_ewma_anual, 2),
            "te2": pct(r.te_equiponderado_anual, 2),
            "alpha": pct(r.exceso_1y, 2, True),
            "ir": "—" if pd.isna(r.IR) else f"{r.IR:.2f}".replace(".", ","),
            "vol": pct(r.vol_anual, 1),
            "ret": pct(r.ret_1y_fondo, 1),
            "q": quartile(r.get("rank"), n),
        })
    return render_template("dashboard.html", categories=categories, category=category, rows=rows, chart=historical_te(category), status=load_status())


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
                    CMF_SESSIONS.pop(token, None)
                    session.pop("cmf_token", None)
                    session.pop("prepared_date", None)
            except CMFAutomationError as exc:
                flash(str(exc), "error")
            except Exception as exc:
                flash(f"No pude completar la actualización: {exc}", "error")
            finally:
                if token in CMF_SESSIONS and request.form.get("captcha"):
                    try:
                        CMF_SESSIONS[token].close()
                    except Exception:
                        pass
                    CMF_SESSIONS.pop(token, None)

    return render_template("update.html", status=load_status(), captcha_b64=captcha_b64, prepared_date=prepared_date or session.get("prepared_date"))


@app.get("/health")
def health():
    return {"ok": True}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
