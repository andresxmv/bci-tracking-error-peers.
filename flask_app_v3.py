from __future__ import annotations

from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
from flask import flash, redirect, render_template, request, url_for

import flask_app_v2 as base

app = base.app

CMF_DAILY_URL = "https://www.cmfchile.cl/institucional/estadisticas/fm.fm_bpr_dia.php"


def download_cmf_daily(target: date) -> tuple[bytes, str]:
    params = {
        "admins": "0",
        "tipofondo": "0",
        "moneda": "0",
        "dia_select": str(target.day),
        "mes_peri": f"{target.month:02d}",
        "anio_peri": str(target.year),
        "out": "excel",
        "lang": "es",
    }
    url = CMF_DAILY_URL + "?" + urlencode(params)
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
            "Accept": "application/vnd.ms-excel,application/octet-stream,text/html;q=0.9,*/*;q=0.8",
            "Referer": "https://www.cmfchile.cl/institucional/estadisticas/fm.bpr_menu.php",
        },
    )
    with urlopen(req, timeout=35) as resp:
        payload = resp.read()
        status = getattr(resp, "status", 200)
        ctype = (resp.headers.get("Content-Type") or "").lower()
    if status != 200:
        raise RuntimeError(f"CMF respondió HTTP {status}.")
    if len(payload) < 1000:
        raise RuntimeError(f"CMF devolvió una respuesta demasiado pequeña ({len(payload)} bytes).")
    # La descarga histórica de CMF es un .xls; en algunos periodos puede ser HTML compatible con Excel.
    filename = f"cmf_valores_cuota_{target:%Y%m%d}.xls"
    return payload, filename


def _direct_update(target: date, persist: bool = True):
    payload, filename = download_cmf_daily(target)
    frame = base.parse_quota_file(payload, filename)
    validation = base.validate_quota_file(frame, base.EXPECTED_RUNS)
    result = {
        "filename": filename,
        "bytes": len(payload),
        **validation,
    }
    if persist:
        if not validation["ok"]:
            raise RuntimeError(
                f"La descarga llegó, pero la cobertura fue {validation['coverage']:.1%} "
                f"({validation['matched_runs']}/{validation['expected_runs']} RUN). No se guardó."
            )
        result = {**result, **base.persist_quota(frame, filename, validation)}
    return result


def update_quota_direct():
    guard = base.require_admin()
    if guard:
        return guard

    if request.method == "POST":
        target = pd.to_datetime(request.form.get("fecha"), errors="coerce")
        if pd.isna(target):
            flash("Selecciona una fecha válida.", "error")
        else:
            try:
                saved = _direct_update(target.date(), persist=True)
                flash(
                    f"Actualización guardada: {saved['latest_date']} · "
                    f"{saved['matched_runs']}/{saved['expected_runs']} RUN.",
                    "success",
                )
                return redirect(url_for("update_quota"))
            except Exception as exc:
                flash(f"No pude actualizar desde CMF: {exc}", "error")

    return render_template(
        "update.html",
        status=base.load_status(),
        captcha_b64=None,
        prepared_date=None,
    )


# Reemplaza la vista existente sin duplicar la regla /actualizar.
app.view_functions["update_quota"] = update_quota_direct


@app.get("/_cmf_direct_test")
def cmf_direct_test():
    raw = request.args.get("fecha") or "2026-08-25"
    target = pd.to_datetime(raw, errors="coerce")
    if pd.isna(target):
        return {"ok": False, "error": "fecha inválida"}, 400
    try:
        result = _direct_update(target.date(), persist=False)
        return {"ok": True, **result}, 200
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 500


if __name__ == "__main__":
    import os
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        threaded=False,
        use_reloader=False,
        debug=False,
    )
