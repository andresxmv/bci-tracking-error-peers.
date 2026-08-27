# -*- coding: utf-8 -*-
"""Genera seed_gross_returns.csv.gz desde las cartolas CMF ya descargadas.

La semilla son los retornos brutos diarios por RUN, calculados con la MISMA
funcion que usa la app cuando el usuario carga una cartola nueva
(quota_update.gross_fund_returns). No es una fuente distinta ni una formula
distinta: es el mismo calculo, hecho por adelantado, para que el dashboard
tenga el ano completo sin depender de que Railway conserve runtime_data.

Uso:
    py -3 generar_semilla_retornos.py ^
        --cartolas "C:\\Users\\andre\\OneDrive\\Escritorio\\panel_riesgo_mercado\\cache_cmf" ^
        --desde 01-12-2025 --hasta 21-08-2026
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from quota_update import gross_fund_returns, normalize_run, parse_quota_file

MONEDA_CONVERTIDA = "CLP"

AQUI = Path(__file__).resolve().parent


def runs_configurados(config_path: Path) -> set[str]:
    documento = json.loads(config_path.read_text(encoding="utf-8"))
    fondos = documento.get("fondos", documento)
    runs: set[str] = set()
    for cfg in fondos.values():
        if not isinstance(cfg, dict) or not cfg.get("bci"):
            continue
        runs.add(normalize_run(cfg["bci"]))
        runs.update(normalize_run(run) for run in cfg.get("peers", []))
    return {run for run in runs if run}


def leer_cartolas(carpeta: Path, desde: pd.Timestamp, hasta: pd.Timestamp, runs: set[str]) -> pd.DataFrame:
    archivos = sorted(carpeta.glob("cartola_todos_*.txt"))
    if not archivos:
        raise FileNotFoundError(f"No hay cartolas CMF en {carpeta}.")
    partes: list[pd.DataFrame] = []
    for archivo in archivos:
        frame = parse_quota_file(archivo.read_bytes(), archivo.name)
        frame = frame[frame["run"].isin(runs)]
        frame = frame[(frame["fecha"] >= desde) & (frame["fecha"] <= hasta)]
        if not frame.empty:
            partes.append(frame)
        print(f"  {archivo.name}: {len(frame)} filas utiles")
    if not partes:
        raise ValueError("Ninguna cartola aporto filas en el rango pedido.")
    cartola = pd.concat(partes, ignore_index=True)
    return (
        cartola.sort_values(["fecha", "run", "serie"])
        .drop_duplicates(["fecha", "run", "serie"], keep="last")
        .reset_index(drop=True)
    )


def dolar_observado(carpeta: Path, desde: pd.Timestamp, hasta: pd.Timestamp) -> pd.Series:
    """Serie de dolar observado desde la cache del panel (dolar_observado_AAAA.json)."""
    valores: dict[str, float] = {}
    for anio in range(desde.year, hasta.year + 1):
        ruta = carpeta / f"dolar_observado_{anio}.json"
        if ruta.exists():
            valores.update({str(k): float(v) for k, v in json.loads(ruta.read_text(encoding="utf-8")).items()})
    if not valores:
        raise FileNotFoundError(f"No hay dolar_observado_*.json en {carpeta}.")
    serie = pd.Series(valores)
    serie.index = pd.DatetimeIndex(serie.index)
    return serie.sort_index()


def retornos_fx(observado: pd.Series, indice: pd.DatetimeIndex) -> pd.Series:
    """Misma convencion que la app: el dolar aplicable en T se publica al dia habil siguiente."""
    span = pd.date_range(
        min(observado.index.min(), indice.min()) - pd.Timedelta(days=10),
        max(observado.index.max(), indice.max()) + pd.Timedelta(days=10),
        freq="D",
    )
    tasa = observado.reindex(span).shift(-1).bfill().ffill()
    return tasa.pct_change(fill_method=None).reindex(indice)


def a_pesos(gross: pd.DataFrame, observado: pd.Series) -> pd.DataFrame:
    """Convierte a pesos los RUN que informan en dolares (MONEDA = PROM).

    Se hace aqui, offline, para que el dashboard no dependa de una llamada de
    red al tipo de cambio: si esa llamada falla, los peers en dolares quedarian
    sin convertir y el alpha del fondo saldria inflado.
    """
    gross = gross.copy()
    es_prom = gross["moneda"].astype(str).str.upper().eq("PROM")
    if not es_prom.any():
        return gross
    fx = retornos_fx(observado, pd.DatetimeIndex(sorted(gross.loc[es_prom, "fecha"].unique())))
    factor = gross.loc[es_prom, "fecha"].map(fx)
    faltan = int(factor.isna().sum())
    if faltan:
        raise ValueError(f"Falta tipo de cambio para {faltan} filas en dolares.")
    gross.loc[es_prom, "ret_bruta"] = (1.0 + gross.loc[es_prom, "ret_bruta"]) * (1.0 + factor) - 1.0
    gross.loc[es_prom, "moneda"] = MONEDA_CONVERTIDA
    print(f"  convertidos a pesos: {int(es_prom.sum())} filas de {gross.loc[es_prom, 'run'].nunique()} RUN en dolares")
    return gross


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cartolas", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=AQUI / "fondos_config.json")
    parser.add_argument("--salida", type=Path, default=AQUI / "seed_gross_returns.csv.gz")
    parser.add_argument("--desde", default="01-12-2025")
    parser.add_argument("--hasta", default="21-08-2026")
    args = parser.parse_args()

    desde = pd.Timestamp(pd.to_datetime(args.desde, dayfirst=True)).normalize()
    hasta = pd.Timestamp(pd.to_datetime(args.hasta, dayfirst=True)).normalize()
    runs = runs_configurados(args.config)
    print(f"RUN configurados: {len(runs)}")

    cartola = leer_cartolas(args.cartolas, desde, hasta, runs)
    print(f"Cartola combinada: {len(cartola)} filas, {cartola['fecha'].nunique()} dias")

    gross = gross_fund_returns(cartola)
    gross = gross.sort_values(["fecha", "run"]).drop_duplicates(["fecha", "run"], keep="last")
    gross = a_pesos(gross, dolar_observado(args.cartolas, desde, hasta))
    gross["fecha"] = pd.to_datetime(gross["fecha"]).dt.strftime("%Y-%m-%d")
    gross.to_csv(args.salida, index=False, compression="gzip")
    print(
        f"{args.salida.name}: {len(gross)} filas, "
        f"{gross['run'].nunique()} RUN, {gross['fecha'].min()} a {gross['fecha'].max()}"
    )


if __name__ == "__main__":
    main()
