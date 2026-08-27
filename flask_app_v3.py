from __future__ import annotations

import base64
import math
import os
import secrets
from pathlib import Path

import numpy as np
import pandas as pd
from flask import flash, redirect, render_template, request, session, url_for

import flask_app_v2 as base
from cmf_cartola_http import CMFCartolaError, CMFCartolaSession, gross_returns_by_run, merge_cartola_history, parse_cartola

app = base.app
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/runtime_data"))
CARTOLA_HISTORY = DATA_DIR / "cartola_history.csv"
CMF_SESSIONS: dict[str, CMFCartolaSession] = {}
_ORIGINAL_CATEGORY_LEVELS = base.category_levels


def _load_cartola_history() -> pd.DataFrame:
    if not CARTOLA_HISTORY.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(CARTOLA_HISTORY, parse_dates=["FECHA"])
        return df
    except Exception:
        return pd.DataFrame()


def _save_cartola_history(frame: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CARTOLA_HISTORY.with_suffix(".tmp")
    frame.to_csv(tmp, index=False)
    tmp.replace(CARTOLA_HISTORY)


def _run_from_column(col: str) -> str:
    text = str(col).strip()
    import re
    m = re.match(r"^(\d+)", text)
    return m.group(1) if m else text


def _configured_col(columns, run: object):
    return base.column_for_run(columns, run)


def category_levels_adjusted(name: str) -> pd.DataFrame:
    levels = _ORIGINAL_CATEGORY_LEVELS(name).copy()
    if levels.empty:
        return levels
    cartola = _load_cartola_history()
    if cartola.empty:
        return levels
    _, cfg = base.config_by_name(name)
    if cfg is None:
        return levels
    for run in [cfg.get("bci"), *cfg.get("peers", [])]:
        col = _configured_col(levels.columns, run)
        if col is None:
            continue
        gross = gross_returns_by_run(cartola, str(run).split("-", 1)[0])
        if gross.empty:
            continue
        existing = levels[col].dropna()
        if existing.empty:
            continue
        last_base_date = existing.index.max()
        last_level = float(existing.iloc[-1])
        new_ret = gross[gross.index > last_base_date]
        if new_ret.empty:
            continue
        vals = []
        level = last_level
        for dt, r in new_ret.items():
            level *= 1.0 + float(r)
            vals.append((dt, level))
        ext = pd.Series({dt: val for dt, val in vals}, name=col)
        full_idx = levels.index.union(ext.index).sort_values()
        levels = levels.reindex(full_idx)
        levels.loc[ext.index, col] = ext.values
    return levels.sort_index()


base.category_levels = category_levels_adjusted


def _cum_return(series: pd.Series, start_date: pd.Timestamp, end_date: pd.Timestamp) -> float:
    s = series.dropna().loc[:end_date]
    if s.empty:
        return float("nan")
    prior = s.loc[s.index <= start_date]
    base_val = float(prior.iloc[-1]) if not prior.empty else float(s.iloc[0])
    return float(s.iloc[-1] / base_val - 1.0)


def _metrics_for_peer_group(name: str) -> dict | None:
    levels = category_levels_adjusted(name)
    bci_col, peer_cols = base.configured_columns(name, levels)
    if levels.empty or bci_col is None or not peer_cols:
        return None
    use = [bci_col, *peer_cols]
    metric_levels = levels[use].dropna(how="any")
    if len(metric_levels) < 10:
        return None
    ref_date = metric_levels.index.max()
    weekly = metric_levels.resample("W-FRI").last().pct_change(fill_method=None).dropna(how="any")
    if len(weekly) < 2:
        return None
    weekly52 = weekly.tail(52)
    port = weekly52[bci_col]
    bench = weekly52[peer_cols].mean(axis=1)
    active = port - bench
    te = base.ewma_te(active, annualize=True)
    ir = float(active.mean() * 52 / te) if np.isfinite(te) and te > 0 else float("nan")

    ytd_start = pd.Timestamp(ref_date.year, 1, 1)
    ytd = {}
    for c in use:
        s = metric_levels[c].dropna()
        prior = s[s.index < ytd_start]
        base_val = float(prior.iloc[-1]) if not prior.empty else float(s.iloc[0])
        ytd[c] = float(s.iloc[-1] / base_val - 1.0)
    ret_ytd = ytd[bci_col]
    peer_ytd = pd.Series({c: ytd[c] for c in peer_cols})
    percentile = float((peer_ytd > ret_ytd).sum() / len(peer_ytd)) if len(peer_ytd) else float("nan")
    quartile = max(1, min(4, math.ceil(percentile * 4))) if np.isfinite(percentile) else None
    alpha_ytd = ret_ytd - float(pd.Series({c: ytd[c] for c in use}).mean())

    one_year_start = ref_date - pd.DateOffset(years=1)
    port_1y = _cum_return(metric_levels[bci_col], one_year_start, ref_date)
    peer_1y = [_cum_return(metric_levels[c], one_year_start, ref_date) for c in peer_cols]
    alpha_1y = port_1y - float(np.nanmean(peer_1y))

    return {
        "ref_date": ref_date,
        "te": te,
        "ir": ir,
        "ret_ytd": ret_ytd,
        "alpha_ytd": alpha_ytd,
        "alpha_1y": alpha_1y,
        "percentile": percentile,
        "quartile": quartile,
    }


def fund_dashboard_dynamic(selected_run: str):
    catalog = base.bci_catalog()
    choice = next((x for x in catalog if base.normalize_run(x["run"]) == base.normalize_run(selected_run)), catalog[0])
    name = choice["fondo"]
    _, cfg = base.config_by_name(name)
    ref = base.REFERENCE.get(name, {})
    dyn = _metrics_for_peer_group(name) if cfg and cfg.get("peers") else None

    if dyn:
        mer = base.pct(dyn["te"], 2)
        ret_ytd = base.pct(dyn["ret_ytd"], 2, True)
        alpha_1y = base.pct(dyn["alpha_1y"], 2, True)
        alpha_ytd = base.pct(dyn["alpha_ytd"], 2, True)
        ir = base.number(dyn["ir"], 2)
        percentile_label = f"{dyn['percentile'] * 100:.0f}"
        quartile = dyn["quartile"]
    else:
        mer = base.pct(ref.get("TE EWMA anual"), 2)
        ret_ytd = base.pct(ref.get("Retorno YTD"), 2, True)
        alpha_1y = base.pct(ref.get("Alpha anual"), 2, True)
        alpha_ytd = base.pct(ref.get("Alpha YTD"), 2, True)
        ir = base.number(ref.get("Information Ratio"), 2)
        p = ref.get("Percentil YTD")
        percentile_label = "—" if p is None or pd.isna(p) else f"{float(p) * 100:.0f}"
        q = ref.get("Cuartil YTD")
        quartile = None if q is None or pd.isna(q) else int(q)

    return {
        "run": str(cfg["bci"]),
        "fondo": name,
        "categoria": cfg.get("grupo", ""),
        "grupo": cfg.get("grupo", ""),
        "mer": mer,
        "ret_ytd": ret_ytd,
        "alpha_1y": alpha_1y,
        "alpha_ytd": alpha_ytd,
        "ir_12m": ir,
        "percentil_ytd": percentile_label,
        "cuartil_ytd": quartile,
        "peer_rows": base.peer_rows_for(name),
        "chart": base.historical_te(name),
    }


base.fund_dashboard = fund_dashboard_dynamic


def _persist_cartola(payload: bytes, filename: str) -> dict:
    new = parse_cartola(payload)
    old = _load_cartola_history()
    combined = merge_cartola_history(old, new)
    _save_cartola_history(combined)
    latest_date = pd.Timestamp(new["FECHA"].max())
    expected = {str(v.get("bci", "")).split("-", 1)[0] for v in base.CONFIG.values() if v.get("bci")}
    for v in base.CONFIG.values():
        expected.update(str(x).split("-", 1)[0] for x in v.get("peers", []))
    present = set(new.loc[new["FECHA"] == latest_date, "RUN_FM"].astype(str))
    matched = len(expected & present)
    status = {
        "latest_date": latest_date.strftime("%Y-%m-%d"),
        "matched_runs": matched,
        "expected_runs": len(expected),
        "coverage": matched / max(len(expected), 1),
        "source_filename": filename,
        "history_rows": int(len(combined)),
        "methodology": "CMF cartola bruta: VC*factor_reparto*factor_ajuste + remuneraciones/gastos, ponderado por patrimonio previo",
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "quota_status.json").write_text(__import__("json").dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return status


def update_quota_http():
    guard = base.require_admin()
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
            cmf = CMFCartolaSession()
            try:
                prepared = cmf.prepare(target.date(), lookback_days=10)
                old = session.get("cmf_token")
                if old and old in CMF_SESSIONS:
                    CMF_SESSIONS.pop(old).close()
                CMF_SESSIONS[token] = cmf
                session["cmf_token"] = token
                session["prepared_date"] = target.strftime("%Y-%m-%d")
                captcha_b64 = base64.b64encode(prepared.image).decode("ascii")
                flash(f"Captcha CMF preparado. Se descargarán cartolas {prepared.start:%d-%m-%Y} a {prepared.end:%d-%m-%Y}.", "success")
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
                payload, filename = cmf.submit(request.form.get("captcha", ""))
                saved = _persist_cartola(payload, filename)
                flash(f"Actualización guardada: {saved['latest_date']} · {saved['matched_runs']}/{saved['expected_runs']} RUN. Rentabilidad bruta ajustada por remuneración aplicada.", "success")
                session.pop("cmf_token", None)
                session.pop("prepared_date", None)
            except CMFCartolaError as exc:
                flash(str(exc), "error")
            except Exception as exc:
                flash(f"No pude completar la actualización: {exc}", "error")
            finally:
                try:
                    cmf.close()
                except Exception:
                    pass
                CMF_SESSIONS.pop(token, None)

    return render_template("update.html", status=base.load_status(), captcha_b64=captcha_b64, prepared_date=prepared_date or session.get("prepared_date"))


app.view_functions["update_quota"] = update_quota_http


@app.get("/_validation")
def validation():
    rows = []
    for item in base.bci_catalog():
        name = item["fondo"]
        d = fund_dashboard_dynamic(item["run"])
        ref = base.REFERENCE.get(name, {})
        rows.append({
            "fondo": name,
            "ret_ytd_web": d["ret_ytd"],
            "ret_ytd_panel": base.pct(ref.get("Retorno YTD"), 2, True),
            "te_web": d["mer"],
            "te_panel": base.pct(ref.get("TE EWMA anual"), 2),
            "ir_web": d["ir_12m"],
            "ir_panel": base.number(ref.get("Information Ratio"), 2),
        })
    return {"ok": True, "rows": rows}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=False, use_reloader=False, debug=False)
