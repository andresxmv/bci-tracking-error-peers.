import unittest

import pandas as pd

import flask_app_v2 as base
import flask_app_v5 as app_v5


class OfficialReportAlignmentTests(unittest.TestCase):
    """Aceptación del corte 31-07-2026 contra el HTML oficial."""

    # Retorno YTD en porcentaje, percentil publicado y cuartil publicado.
    # Acciones Globales no aparece en el HTML entregado; Top Picks y Acciones
    # Chilenas son las excepciones solicitadas para el ranking.
    REPORT = {
        "estrategico $ h 1 ano": (3.56, "24", 1),
        "estrategico uf h 1 ano": (4.29, "50", 2),
        "mediano plazo": (3.94, "18", 1),
        "estrategico $ > 1 ano": (3.14, "63", 3),
        "estrategico uf h 3 anos": (4.33, "46", 2),
        "estrategico uf h 5 anos": (4.02, "28", 2),
        "estrategico uf > 5 anos": (3.29, "67", 3),
        "dinamica ahorro": (3.87, "41", 2),
        "patrimonial ahorro": (3.89, "41", 2),
        "deuda corp. estr.": (4.03, "50", 2),
        "cd activa": (11.66, "45", 2),
        "cp activa": (11.00, "1", 1),
        "cd balanceada": (8.08, "45", 2),
        "cp balanceada": (8.48, "1", 1),
        "cd conservadora": (5.55, "13", 1),
        "cp conservadora": (5.68, "41", 2),
        "asia": (20.65, "60", 3),
        "emergente global": (18.95, "34", 2),
        "estados unidos": (9.72, "42", 2),
        "europa": (12.92, "26", 1),
        "global titan": (10.83, "62", 3),
        "top picks": (7.63, "32", 2),
        "acciones chilenas": (6.64, "28", 2),
    }
    EXCEPTIONS = {"top picks", "acciones chilenas", "acciones globales"}

    def test_cutoff_matches_official_report(self):
        cutoff = pd.Timestamp("2026-07-31")
        seen = set()
        for item in base.bci_catalog():
            name = item["fondo"]
            key = app_v5._report_name_key(name)
            if key not in self.REPORT:
                continue
            seen.add(key)
            expected_return, expected_percentile, expected_quartile = self.REPORT[key]
            with self.subTest(fondo=name):
                ref = app_v5.compute_reference(name, cutoff_date=cutoff)
                self.assertIsNotNone(ref)
                self.assertLessEqual(
                    abs(float(ref["Retorno YTD"]) * 100.0 - expected_return),
                    0.01,
                    "Retorno YTD supera 1 pb",
                )
                dashboard = app_v5.live_fund_dashboard(item["run"], cutoff_date="2026-07-31")
                self.assertEqual(dashboard["cuartil_ytd"], expected_quartile)
                if key not in self.EXCEPTIONS:
                    self.assertEqual(dashboard["percentil_ytd"], expected_percentile)

        self.assertEqual(seen, set(self.REPORT))


if __name__ == "__main__":
    unittest.main()
