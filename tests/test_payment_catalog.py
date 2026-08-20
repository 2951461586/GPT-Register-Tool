import json
import tempfile
import unittest
from pathlib import Path

from sms_tool import payment_catalog
from sms_tool.payment_catalog import load_payment_catalog


def _catalog_payload(**overrides):
    payload = {
        "schema": payment_catalog.CATALOG_SCHEMA,
        "default_method": "gopay",
        "methods": [
            {
                "id": "gopay",
                "display_name": "GoPay",
                "country": "ID",
                "currency": "IDR",
                "adapter": "wallet",
            },
            {
                "id": "grabpay",
                "display_name": "GrabPay",
                "country": "PH",
                "currency": "PHP",
                "adapter": "wallet",
            },
        ],
    }
    payload.update(overrides)
    return payload


class PaymentCatalogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(load_payment_catalog.cache_clear)

    def _write_catalog(self, payload) -> Path:
        path = Path(self._tmp.name) / "payment_methods.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def load(self, payload):
        return load_payment_catalog(self._write_catalog(payload))

    def test_top_level_country_lists_default_onto_methods(self):
        catalog = self.load(_catalog_payload(
            checkout_countries=["ID", "PH"],
            approve_countries=["JP", "TR"],
        ))

        self.assertEqual(catalog.checkout_countries, ("ID", "PH"))
        self.assertEqual(catalog.approve_countries, ("JP", "TR"))
        self.assertEqual(catalog.methods["gopay"].checkout_countries, ("ID", "PH"))
        self.assertEqual(catalog.methods["gopay"].approve_countries, ("JP", "TR"))
        self.assertEqual(catalog.methods["grabpay"].approve_countries, ("JP", "TR"))

    def test_country_list_entries_may_be_code_label_objects(self):
        # The desktop writes display-shaped entries: {"code": "JP", "label": ...}.
        catalog = self.load(_catalog_payload(
            approve_countries=[
                {"code": "jp", "label": "日本 JP"},
                {"code": "TR", "label": "土耳其 TR"},
            ],
        ))

        self.assertEqual(catalog.approve_countries, ("JP", "TR"))
        self.assertEqual(catalog.methods["gopay"].approve_countries, ("JP", "TR"))

    def test_invalid_country_object_code_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "approve_countries"):
            self.load(_catalog_payload(approve_countries=[{"label": "missing code"}]))

    def test_method_level_country_list_overrides_top_level_default(self):
        payload = _catalog_payload(approve_countries=["JP", "TR"])
        payload["methods"][0]["approve_countries"] = ["JP"]
        payload["methods"][1]["checkout_countries"] = ["ph"]  # normalized to upper
        catalog = self.load(payload)

        self.assertEqual(catalog.methods["gopay"].approve_countries, ("JP",))
        self.assertEqual(catalog.methods["grabpay"].approve_countries, ("JP", "TR"))
        self.assertEqual(catalog.methods["grabpay"].checkout_countries, ("PH",))
        self.assertEqual(catalog.methods["gopay"].checkout_countries, ())

    def test_missing_country_lists_resolve_to_empty_tuples(self):
        catalog = self.load(_catalog_payload())

        self.assertEqual(catalog.checkout_countries, ())
        self.assertEqual(catalog.approve_countries, ())
        self.assertEqual(catalog.methods["gopay"].checkout_countries, ())
        self.assertEqual(catalog.methods["gopay"].approve_countries, ())

    def test_invalid_method_country_code_names_the_method_id(self):
        payload = _catalog_payload()
        payload["methods"][0]["approve_countries"] = ["JPN"]
        with self.assertRaisesRegex(ValueError, "gopay"):
            self.load(payload)

    def test_invalid_top_level_country_code_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "approve_countries"):
            self.load(_catalog_payload(approve_countries=["J1"]))

    def test_non_array_country_list_is_rejected(self):
        payload = _catalog_payload()
        payload["methods"][1]["checkout_countries"] = "PH"
        with self.assertRaisesRegex(ValueError, "grabpay"):
            self.load(payload)

    def test_unknown_keys_remain_tolerated(self):
        payload = _catalog_payload(future_field={"anything": True})
        payload["methods"][0]["future_method_field"] = "x"
        catalog = self.load(payload)

        self.assertEqual(catalog.default_method, "gopay")
        self.assertEqual(set(catalog.methods), {"gopay", "grabpay"})

    def test_declarative_artifact_and_reconciliation_contracts_are_loaded(self):
        payload = _catalog_payload()
        payload["methods"][0].update({
            "artifact_validator": "url_or_qr",
            "probe_output_kind": "availability",
            "reconciliation_policy": "provider_status",
        })
        definition = self.load(payload).methods["gopay"]
        self.assertEqual(definition.artifact_validator, "url_or_qr")
        self.assertEqual(definition.probe_output_kind, "availability")
        self.assertEqual(definition.reconciliation_policy, "provider_status")

    def test_unknown_artifact_validator_is_rejected(self):
        payload = _catalog_payload()
        payload["methods"][0]["artifact_validator"] = "arbitrary_code"
        with self.assertRaisesRegex(ValueError, "gopay"):
            self.load(payload)


if __name__ == "__main__":
    unittest.main()
