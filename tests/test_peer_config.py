import json
import unittest
from pathlib import Path

from quota_update import normalize_run


CONFIG_PATH = Path(__file__).resolve().parents[1] / "fondos_config.json"


class ExcelPeerConfigTests(unittest.TestCase):
    """The configured P-groups stay identical to the delivered dictionary.

    The workbook contains both the BCI fund and its peers in each category;
    the dashboard configuration stores the BCI RUN separately, so the
    expected peer set is the workbook category minus that BCI RUN.
    """

    EXCEL_CATEGORIES = {
        "DINAMICA ACTIVA": {
            "8448", "8640", "8740", "8993", "9193", "9473", "9576", "9593", "9648", "9873",
        },
        "PATRIMONIAL ACTIVA": {"10064", "8435", "8785", "8844", "8908", "9060"},
        "DINAMICA BALANCEADA": {
            "8116", "8639", "9006", "9043", "9190", "9429", "9474", "9577", "9594", "9646",
        },
        "PATRIMONIAL BALANCEADA": {"8336", "8845", "8911", "8992", "9062"},
        "DINAMICA CONSERVADORA": {
            "8377", "8638", "8741", "9192", "9575", "9595", "9649", "9768", "9872",
        },
        "PATRIMONIAL CONSERVADORA": {"8295", "8306", "8773", "8910", "8994", "9063"},
        "RV ASIA EMERGENTE": {"8159", "8438", "8457", "8514", "8517", "8820", "9023"},
        "RV GLOBAL EMERGENTE": {
            "8054", "8058", "8299", "8323", "8475", "8625", "8916", "9024", "9922", "9987",
        },
        "RV ESTADOS UNIDOS": {
            "8078", "8113", "8183", "8205", "8300", "8437", "8458", "8479", "8480", "8488", "8712", "8915", "8987", "9932",
        },
        "RV EUROPA": {"8097", "8114", "8129", "8456", "8484", "8513", "9027", "9187", "9254"},
        "RV GLOBAL": {
            "8088", "8090", "8294", "8301", "8427", "8621", "8707", "8710", "8744", "8822", "8924", "8971", "9005", "9651", "9931",
        },
        "RV NAC TOP PICKS": {
            "10068", "10331", "8038", "8043", "8076", "8142", "8289", "8305", "8372", "8380", "8381", "8490", "8536", "8685", "8723", "8787", "8819", "8872", "8898", "8912", "8918", "8982", "9019", "9362", "9414", "9489", "9537", "9685",
        },
        "RV NACIONAL": {
            "10068", "10331", "8076", "8142", "8289", "8305", "8381", "8430", "8536", "8685", "8912", "9019", "9489", "9537",
        },
        "RF<365NCLP": {"10026", "10208", "8250", "8274", "8280", "8304", "8316", "8327", "8352", "8615", "8755", "8902", "8932", "9106"},
        "RF<365NUF": {"10261", "10461", "10515", "8127", "8941", "8986", "8991", "9222", "9291"},
        "RF<365 OF": {"10519", "8363", "8881", "8976", "9056", "9222", "9539"},
        "RF>365NCLP": {"8029", "8055", "8082", "8174", "8292", "8358", "8411", "8422", "8807"},
        "RF>365NUF<3": {"8064", "8077", "8106", "8118", "8141", "8152", "8263", "8317", "8853", "8897", "9084", "9597"},
        "RF>365NUF>3<5": {"8089", "8108", "8125", "8203", "8287", "8346", "8375", "8421", "8676", "9074", "9154", "9238"},
        "RF>365NUF>5": {"8023", "8387", "8460", "8956", "9033", "9054", "9934"},
        "RF>365OF (Dinamica Ahorro)": {"10482", "10520", "8240", "8251", "8959", "9021", "9034", "9108", "9228", "9692", "9693"},
        "RF>365OF (Patrimonial Ahorro)": {"10482", "10520", "8240", "8251", "8959", "9021", "9034", "9061", "9108", "9692", "9693"},
        "DEUDA CORP LOCAL": {"8251", "8287", "8315", "8806", "8950", "9021", "9201", "9226", "9248"},
    }

    def test_config_matches_excel_categories(self):
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["fondos"]
        configured_groups = {}
        for item in config.values():
            peers = item.get("peers")
            if peers:
                configured_groups.setdefault(item["grupo"], []).append(item)

        self.assertEqual(set(configured_groups), set(self.EXCEL_CATEGORIES))
        for group, expected in self.EXCEL_CATEGORIES.items():
            with self.subTest(group=group):
                for item in configured_groups[group]:
                    bci = normalize_run(item["bci"])
                    actual = {normalize_run(run) for run in item["peers"]}
                    self.assertEqual(actual, expected - {bci})
                    self.assertNotIn(bci, actual)

    def test_excel_run_10331_is_not_silently_excluded(self):
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["fondos"]
        top = next(item for item in config.values() if item["nombre"] == "Top Picks")
        chile = next(item for item in config.values() if item["nombre"] == "Acciones Chilenas")
        self.assertIn("10331", top["peers"])
        self.assertIn("10331", chile["peers"])


if __name__ == "__main__":
    unittest.main()
