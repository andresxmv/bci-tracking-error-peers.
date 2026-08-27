from __future__ import annotations

import os

import pandas as pd

import flask_app_v4 as v4
import flask_app_v2 as base
from quota_update import normalize_run

app = base.app

# Cache de métricas reconstruidas a partir de la serie histórica + cartolas CMF.
# Se regenera completa después de cada actualización exitosa de cuota.
LIVE_REFERENCES: dict[str, dict] = {}
_ORIGINAL_PERSIST_QUOTA = base.persist_quota


def recompute_all_metrics() -> dict[str, dict]:
    """Reconstruye y recalcula todas las métricas del panel para todos los fondos.

    La fuente es siempre la serie histórica completa extendida con las cartolas
    CMF persistidas. No reutiliza YTD/Alpha/TE/IR precalculados como resultado
    final. De esta forma una nueva fecha de cuota cambia todas las métricas que
    correspondan de manera consistente.
    """
    refreshed: dict[str, dict] = {}
    for item in base.bci_catalog():
        name = item["fondo"]
        try:
            ref = v4.compute_live_reference(name)
        except Exception:
            ref = None
        if ref:
            refreshed[name] = dict(ref)
        elif name in base.REFERENCE:
            # Solo fallback si no existe historia suficiente para reconstruir.
            refreshed[name] = dict(base.REFERENCE[name])

    LIVE_REFERENCES.clear()
    LIVE_REFERENCES.update(refreshed)
    return LIVE_REFERENCES


def persist_quota_and_recompute(frame, source_filename: str, validation: dict):
    """Guarda la cartola y, en la misma operación, recalcula el panel completo."""
    saved = _ORIGINAL_PERSIST_QUOTA(frame, source_filename, validation)
    # gross_returns_history.csv ya fue reconstruido por persist_quota;
    # ahora todas las métricas se recalculan usando esa historia actualizada.
    recompute_all_metrics()
    return saved


def compute_live_reference(name: str) -> dict | None:
    ref = LIVE_REFERENCES.get(name)
    if ref is not None:
        return dict(ref)
    # Si por cualquier razón el fondo aún no está en cache, se calcula al vuelo.
    ref = v4.compute_live_reference(name)
    return dict(ref) if ref else None


def live_fund_dashboard(selected_run: str):
    catalog = base.bci_catalog()
    choice = next(
        (x for x in catalog if normalize_run(x["run"]) == normalize_run(selected_run)),
        catalog[0],
    )
    name = choice["fondo"]
    _, cfg = base.config_by_name(name)
    ref = compute_live_reference(name) or base.REFERENCE.get(name, {})

    percentile = ref.get("Percentil YTD")
    percentile_label = "—" if percentile is None or pd.isna(percentile) else f"{float(percentile) * 100:.0f}"
    quartile = ref.get("Cuartil YTD")
    quartile = None if quartile is None or pd.isna(quartile) else int(quartile)

    return {
        "run": str(cfg["bci"]),
        "fondo": name,
        "categoria": cfg.get("grupo", ""),
        "grupo": cfg.get("grupo", ""),
        "mer": base.pct(ref.get("TE EWMA anual"), 2),
        "ret_ytd": base.pct(ref.get("Retorno YTD"), 2, True),
        "alpha_1y": base.pct(ref.get("Alpha anual"), 2, True),
        "alpha_ytd": base.pct(ref.get("Alpha YTD"), 2, True),
        "ir_12m": base.number(ref.get("Information Ratio"), 2),
        "percentil_ytd": percentile_label,
        "cuartil_ytd": quartile,
        "peer_rows": base.peer_rows_for(name),
        "chart": v4.live_historical_te(name),
    }


# Las rutas ya fueron creadas en flask_app_v2. Sustituimos las funciones que
# esas rutas resuelven en runtime para que la actualización sea transaccional:
# guardar cartola -> reconstruir retornos -> recalcular panel completo.
base.persist_quota = persist_quota_and_recompute
base.compute_live_reference = compute_live_reference
base.fund_dashboard = live_fund_dashboard

# Primer cálculo al levantar el servicio con cualquier historia persistida que exista.
recompute_all_metrics()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
