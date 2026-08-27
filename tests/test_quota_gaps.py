import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from flask_app_v4 import _merge_return_segments
import flask_app_v5 as app_v5
import quota_update
from quota_update import gross_fund_returns


class QuotaGapTests(unittest.TestCase):
    @staticmethod
    def _cartola() -> pd.DataFrame:
        dates = pd.to_datetime(["2026-07-30", "2026-07-31", "2026-08-21", "2026-08-24"])
        values = [100.0, 101.0, 120.0, 121.2]
        return pd.DataFrame({
            "fecha": dates,
            "run": "8640",
            "serie": "A",
            "valor_cuota": values,
            "patrimonio_neto": 1_000.0,
            "rem_fija": 0.0,
            "rem_variable": 0.0,
            "gastos_afectos": 0.0,
            "gastos_no_afectos": 0.0,
            "factor_ajuste": 1.0,
            "factor_reparto": 1.0,
            "moneda": "$$",
        })

    def test_gross_returns_drop_discontinuous_jump(self):
        result = gross_fund_returns(self._cartola())
        self.assertEqual(result["fecha"].dt.strftime("%Y-%m-%d").tolist(), ["2026-07-31", "2026-08-24"])
        self.assertAlmostEqual(float(result.iloc[0].ret_bruta), 0.01)
        self.assertAlmostEqual(float(result.iloc[1].ret_bruta), 0.01)

    def test_historical_block_keeps_cutoff_and_resets_overlap_anchor(self):
        levels = pd.DataFrame(
            {"8640-FONDO": [100.0, 999.0, 110.0]},
            index=pd.to_datetime(["2026-07-24", "2026-07-31", "2026-08-14"]),
        )
        returns = pd.Series(
            [0.01, 0.01, 0.05, 0.01],
            index=pd.to_datetime(["2026-07-27", "2026-07-31", "2026-08-24", "2026-08-25"]),
        )
        merged = _merge_return_segments(levels, "8640-FONDO", returns, pd.Timestamp("2026-08-21"))
        # El 31-07 ya existe en la serie validada: la cartola solapada no lo
        # vuelve a capitalizar ni cambia el YTD del corte.
        self.assertAlmostEqual(float(merged.loc["2026-07-31", "8640-FONDO"]), 999.0)
        self.assertAlmostEqual(float(merged.loc["2026-08-21", "8640-FONDO"]), 110.0)
        self.assertAlmostEqual(float(merged.loc["2026-08-25", "8640-FONDO"]), 116.655)

    def test_gross_history_deduplicates_run_date(self):
        with TemporaryDirectory() as temp:
            gross_path = Path(temp) / "gross_returns_history.csv"
            pd.DataFrame({
                "fecha": ["2026-07-31", "2026-07-31", "2026-08-01"],
                "run": ["9060", "9060", "9060"],
                "ret_bruta": [0.01, 0.02, 0.03],
                "moneda": ["$$", "$$", "$$"],
            }).to_csv(gross_path, index=False)
            old_path = quota_update.GROSS_RETURNS_PATH
            quota_update.GROSS_RETURNS_PATH = gross_path
            try:
                result = quota_update.load_gross_returns()
                self.assertEqual(result[["fecha", "run"]].drop_duplicates().shape[0], 2)
                self.assertEqual(result["fecha"].dt.strftime("%Y-%m-%d").tolist(), ["2026-07-31", "2026-08-01"])
                self.assertAlmostEqual(float(result.iloc[0].ret_bruta), 0.02)
            finally:
                quota_update.GROSS_RETURNS_PATH = old_path

    def test_custom_cutoff_uses_calendar_ytd_baseline(self):
        levels = pd.DataFrame(
            {
                "9060-FM BCI CP ACTIVA": [100.0, 110.0],
                "8908-BANKING AGRESIVO": [100.0, 110.0],
                "8435-AGRESIVO ESTRAT\u00c9GICO": [100.0, 110.0],
                "8785-BICE AGRESIVO": [100.0, 110.0],
                "8844-PERFIL A": [100.0, 110.0],
                "10064-CARTERA AGRESIVO": [100.0, 110.0],
            },
            index=pd.to_datetime(["2026-07-31", "2026-08-21"]),
        )
        old_levels = app_v5.v4.live_category_levels
        app_v5.v4.live_category_levels = lambda name, peer_runs=None: levels.copy()
        try:
            cfg = app_v5.base.config_by_name("CP Activa")[1]
            live = {"Fecha": pd.Timestamp("2026-07-31"), "Retorno YTD": 0.0219}
            corrected = app_v5._correct_custom_calendar_ytd(
                "CP Activa", cfg, live, cfg["peers"]
            )
            expected = (1.0 + app_v5.BASELINE_REFERENCE["CP Activa"]["Retorno YTD"]) / 1.1 - 1.0
            self.assertAlmostEqual(float(corrected["Retorno YTD"]), expected)
        finally:
            app_v5.v4.live_category_levels = old_levels

    def test_cached_cartola_date_is_detected(self):
        with TemporaryDirectory() as temp:
            history = Path(temp) / "quota_history.csv"
            latest = Path(temp) / "latest_quota.csv"
            history.write_text("fecha,run\n2026-08-25,8640\n", encoding="utf-8")
            old_history, old_latest = quota_update.HISTORY_PATH, quota_update.LATEST_PATH
            quota_update.HISTORY_PATH, quota_update.LATEST_PATH = history, latest
            try:
                self.assertTrue(quota_update.has_quota_date("2026-08-25"))
                self.assertFalse(quota_update.has_quota_date("2026-08-24"))
            finally:
                quota_update.HISTORY_PATH, quota_update.LATEST_PATH = old_history, old_latest


if __name__ == "__main__":
    unittest.main()
