import unittest

import pandas as pd

import flask_app_v2 as base
import flask_app_v5 as app_v5


class OfficialReportAlignmentTests(unittest.TestCase):
    """Aceptación del corte 31-07-2026 contra el HTML oficial."""

    # Valores publicados en la captura oficial del panel del 31-07-2026.
    # Los porcentajes de retorno/alpha/TE se comparan en puntos porcentuales
    # (la tolerancia de 0,01 equivale a un punto base). Top Picks y Acciones
    # Chilenas son las únicas excepciones solicitadas.
    REPORT = {
        "estrategico $ h 1 ano": (3.56, "23", 1, 0.16, 0.06, 0.11, 0.42),
        "estrategico uf h 1 ano": (4.29, "50", 2, 0.17, -0.03, 0.05, -0.26),
        "mediano plazo": (3.94, "17", 1, 0.43, 0.15, 0.29, 0.40),
        "estrategico $ > 1 ano": (3.14, "62", 3, 0.25, -0.00, 0.03, 0.08),
        "estrategico uf h 3 anos": (4.33, "45", 2, 0.16, -0.18, -0.02, -1.17),
        "estrategico uf h 5 anos": (4.02, "27", 2, 0.20, -0.02, 0.08, -0.06),
        "estrategico uf > 5 anos": (3.29, "67", 3, 0.23, -0.24, -0.10, -1.09),
        "dinamica ahorro": (3.87, "40", 2, 0.26, 0.35, 0.06, 1.29),
        "patrimonial ahorro": (3.89, "40", 2, 0.28, 0.33, 0.08, 1.19),
        "deuda corp. estr.": (4.03, "50", 2, 0.31, 0.09, 0.03, 0.59),
        "cd activa": (11.66, "44", 2, 1.54, 2.14, 0.42, 1.36),
        "cp activa": (11.00, "0", 1, 1.51, 3.28, 1.10, 2.28),
        "cd balanceada": (8.08, "44", 2, 1.22, 1.30, -0.00, 1.18),
        "cp balanceada": (8.48, "0", 1, 1.40, 2.79, 1.21, 2.31),
        "cd conservadora": (5.55, "12", 1, 0.75, 0.76, 0.00, 1.16),
        "cp conservadora": (5.68, "40", 2, 0.70, 0.63, 0.07, 0.99),
        "asia": (20.65, "60", 3, 3.71, 0.13, -0.38, -0.01),
        "emergente global": (18.95, "33", 2, 5.86, 1.48, 0.09, 0.30),
        "estados unidos": (9.72, "42", 2, 3.53, 2.36, 1.15, 0.64),
        "europa": (12.92, "25", 1, 2.78, 4.63, 2.48, 1.78),
        "global titan": (10.83, "62", 3, 4.32, 5.25, 1.28, 1.43),
        # Estas dos filas son las excepciones expresas del usuario; sólo se
        # conserva el retorno/cuadrícula como control de que siguen cargando.
        "acciones globales": (11.7701617, None, None, 9.61, -0.15, -0.15, 0.00),
        "top picks": (7.63, "36", 2, None, None, None, None),
        "acciones chilenas": (6.64, "27", 2, None, None, None, None),
        "america latina": (13.5151136, None, None, 1.52, -2.82, -2.82, -0.41),
    }
    EXCEPTIONS = {"top picks", "acciones chilenas"}

    def test_cutoff_matches_official_report(self):
        cutoff = pd.Timestamp("2026-07-31")
        seen = set()
        for item in base.bci_catalog():
            name = item["fondo"]
            key = app_v5._report_name_key(name)
            if key not in self.REPORT:
                continue
            seen.add(key)
            (
                expected_return,
                expected_percentile,
                expected_quartile,
                expected_te,
                expected_alpha_1y,
                expected_alpha_ytd,
                expected_ir,
            ) = self.REPORT[key]
            with self.subTest(fondo=name):
                ref = app_v5.compute_reference(name, cutoff_date=cutoff)
                self.assertIsNotNone(ref)
                self.assertLessEqual(
                    abs(float(ref["Retorno YTD"]) * 100.0 - expected_return),
                    0.01,
                    "Retorno YTD supera 1 pb",
                )
                if key not in self.EXCEPTIONS:
                    for field, expected in (
                        ("TE EWMA anual", expected_te),
                        ("Alpha anual", expected_alpha_1y),
                        ("Alpha YTD", expected_alpha_ytd),
                    ):
                        self.assertAlmostEqual(
                            float(ref[field]) * 100.0,
                            expected,
                            delta=0.01,
                            msg=f"{field} supera 1 pb",
                        )
                    self.assertAlmostEqual(
                        float(ref["Information Ratio"]),
                        expected_ir,
                        delta=0.01,
                        msg="Information Ratio supera 0,01",
                    )
                dashboard = app_v5.live_fund_dashboard(item["run"], cutoff_date="2026-07-31")
                if expected_quartile is None:
                    self.assertIsNone(dashboard["cuartil_ytd"])
                else:
                    self.assertEqual(dashboard["cuartil_ytd"], expected_quartile)
                if expected_percentile is None:
                    self.assertEqual(dashboard["percentil_ytd"], "—")
                elif key not in self.EXCEPTIONS:
                    self.assertEqual(dashboard["percentil_ytd"], expected_percentile)

        self.assertEqual(seen, set(self.REPORT))


if __name__ == "__main__":
    unittest.main()
