"""No-network checks for per-VIN deductibles from admin save through issuance."""
import copy
import os
import unittest
from unittest.mock import AsyncMock, patch

import fitz
from fastapi.testclient import TestClient

import main
from acord25_2016_overlay import generate_trucking_cert, TextOverflowError
from test_acord25_overlay_wrap import content, ACORD25, ACORD101
from vehicle_schedule import normalize_units


class VehicleDeductibleTests(unittest.TestCase):
    def test_blank_zero_and_individual_amounts(self):
        rows = normalize_units([
            {"vin": "VIN-A", "comprehensive_deductible": "", "collision_deductible": None},
            {"vin": "VIN-B", "comprehensive_deductible": 0, "collision_deductible": "2500.50", "custom": "kept"},
        ])
        self.assertEqual(rows[0], {"vin": "VIN-A"})
        self.assertEqual(rows[1], {"vin": "VIN-B", "comprehensive_deductible": 0,
                                  "collision_deductible": 2500.5, "custom": "kept"})

    def policy(self, **changes):
        row = dict(client_id="test-client", named_insured="Example Trucking LLC",
                   effective_date="2026-01-01", expiration_date="2027-01-01",
                   producer_block="Pinnacle Risk Advisors LLC", insured_address="1 Test Road",
                   data_current_as_of="2026-09-15", carriers=[],
                   vehicles=[{"vin": "VIN-POWER", "value": 30000, "comprehensive_deductible": 1000,
                              "collision_deductible": 2500, "custom": "preserve"}],
                   trailers=[{"vin": "VIN-TRAILER", "collision_deductible": 500}])
        row.update(changes)
        return row

    def test_admin_save_and_reload_preserve_deductibles(self):
        saved = []

        async def insert(table, row):
            saved.append(copy.deepcopy(row))
            return row

        with patch.object(main, "require_admin"), patch.object(main, "sb_insert", side_effect=insert):
            client = TestClient(main.app)
            response = client.post('/admin/policies', json=self.policy())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(saved[0]["vehicles"], self.policy()["vehicles"])
            self.assertEqual(saved[0]["trailers"], self.policy()["trailers"])
            with patch.object(main, "sb_get", AsyncMock(return_value=saved)):
                loaded = client.get('/admin/policies').json()[0]
            self.assertEqual(loaded["vehicles"], saved[0]["vehicles"])

    def test_invalid_values_rejected_before_storage(self):
        with patch.object(main, "require_admin"), patch.object(main, "sb_insert", AsyncMock()) as insert:
            client = TestClient(main.app)
            for value in (-1, "NaN", "Infinity", "1000 dollars", True, 1.234, 1e15):
                for schedule in ("vehicles", "trailers"):
                    with self.subTest(value=value, schedule=schedule):
                        response = client.post('/admin/policies', json=self.policy(**{
                            schedule: [{"vin": "VIN-A", "collision_deductible": value}]}))
                        self.assertEqual(response.status_code, 422, response.text)
            response = client.post('/admin/policies', json=self.policy(vehicles=[{"collision_deductible": 1000}]))
            self.assertEqual(response.status_code, 422)
            insert.assert_not_called()

    def test_each_vin_has_its_own_deductibles_in_pdf(self):
        vehicles = [
            dict(description="2019 VOLVO", vin="VIN-ONE", value=30000,
                 comprehensive_deductible=1000, collision_deductible=2500.5),
            dict(description="2020 FREIGHTLINER", vin="VIN-TWO", value=40000,
                 comprehensive_deductible=0, collision_deductible=5000),
            dict(description="2021 KENWORTH", vin="VIN-UNKNOWN", value=25000),
        ]
        pdf = generate_trucking_cert(content(vehicles=vehicles, trailers=[
            dict(description="DRY VAN", vin="VIN-TRAILER", value=0, collision_deductible=500)]),
            ACORD25, ACORD101)
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            text = doc[1].get_text()
            self.assertIn("Comp Ded: $1,000 | Collision Ded: $2,500.50", text)
            self.assertIn("Comp Ded: $0 | Collision Ded: $5,000", text)
            unknown = text.split("VIN-UNKNOWN")[1].split("SCHEDULED TRAILERS")[0]
            self.assertNotIn("Ded:", unknown)
            self.assertIn("Collision Ded: $500", text.split("VIN-TRAILER")[1])
            self.assertNotIn("Stated value $0", text)
            self.assertFalse(any(list(page.widgets() or []) for page in doc))
            spans = [s for b in doc[1].get_text('dict')['blocks'] for l in b.get('lines', []) for s in l['spans']]
            vin = next(s for s in spans if 'VIN-ONE' in s['text'])
            ded = next(s for s in spans if 'Collision Ded: $2,500.50' in s['text'])
            self.assertLess(ded['bbox'][1] - vin['bbox'][3], 8)

    def test_deductibles_cannot_be_silently_dropped_without_schedule(self):
        c = content(vehicles=[{"vin": "VIN-A", "collision_deductible": 1000}])
        with self.assertRaisesRegex(TextOverflowError, "ACORD101_TEMPLATE_PATH"):
            generate_trucking_cert(c, ACORD25, include_101=False)
        with fitz.open(stream=generate_trucking_cert(c, ACORD25, ACORD101, include_101=False), filetype='pdf') as doc:
            self.assertEqual(len(doc), 2)

    def test_oversized_schedule_refuses_instead_of_clipping(self):
        c = content(vehicles=[dict(vin=f"VIN-{i}", description="TRUCK", collision_deductible=1000)
                              for i in range(60)])
        with self.assertRaisesRegex(TextOverflowError, "schedule does not fit"):
            generate_trucking_cert(c, ACORD25, ACORD101)

    def test_self_serve_schedule_error_writes_no_certificate(self):
        policy = self.policy(id='policy-test', status='active', self_serve_enabled=True,
                             coverages={'auto_liability': 1000000})
        with patch.object(main, 'verify_jwt', return_value='test-client'), \
             patch.object(main, 'load_policy', AsyncMock(return_value=policy)), \
             patch.object(main, 'write_audit', AsyncMock()), \
             patch.object(main, 'generate_certificate', side_effect=TextOverflowError('schedule does not fit')), \
             patch.object(main, 'sb_insert', AsyncMock()) as insert, \
             patch.object(main, 'storage_upload', AsyncMock()) as upload:
            response = TestClient(main.app).post('/issue-certificate', json={
                'policy_id': 'policy-test', 'holder_name': 'Test Holder', 'holder_address': '1 Test Road'})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['detail']['status'], 'refused')
            insert.assert_not_called()
            upload.assert_not_called()

    def test_ghl_sync_preserves_admin_per_vin_values(self):
        existing = self.policy(id="33333333-3333-3333-3333-333333333333")
        expected_vehicles, expected_trailers = copy.deepcopy(existing['vehicles']), copy.deepcopy(existing['trailers'])
        payload = dict(ghl_policy_id="ghl-auto-test", named_insured="Example Trucking LLC",
                       insured_address="1 Test Road", usdot="1234567", status="Active",
                       self_serve_enabled=True, effective_date="2026-01-01", expiration_date="2027-01-01",
                       data_current_as_of="2026-09-15", producer_block="Pinnacle Risk Advisors LLC",
                       line_of_business="Commercial Auto", carrier="Progressive Mountain Insurance Company",
                       policy_number="AUTO-TEST", coverage_limit=1000000, auto_symbols="Scheduled")
        async def get(table, params):
            return [{"id": "test-client", "email": "test@example.com"}] if table == 'clients' else [existing]
        async def upsert(table, row):
            return row
        with patch.dict(os.environ, {"GHL_POLICY_SYNC_SECRET": "test-secret-for-vehicle-deductibles"}), \
             patch.object(main.ghl_policy_sync, "_resolve_associated_contact_email", AsyncMock(return_value="test@example.com")), \
             patch.object(main, "sb_get", side_effect=get), \
             patch.object(main, "sb_upsert", side_effect=upsert) as saved:
            r = TestClient(main.app).post('/api/integrations/ghl/policies', json=payload,
                                         headers={"Authorization": "Bearer test-secret-for-vehicle-deductibles"})
            self.assertEqual(r.status_code, 200, r.text)
            row = saved.call_args.args[1]
            self.assertEqual(row['vehicles'], expected_vehicles)
            self.assertEqual(row['trailers'], expected_trailers)


if __name__ == '__main__':
    unittest.main()
