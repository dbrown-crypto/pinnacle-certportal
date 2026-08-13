"""No-network tests for the GoHighLevel policy synchronization endpoint."""

import os

from fastapi.testclient import TestClient

TEST_SECRET = "test-sync-secret-with-at-least-32-bytes"
os.environ["GHL_POLICY_SYNC_SECRET"] = TEST_SECRET

import main


client = TestClient(main.app)
AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
CLIENT_ID = "22222222-2222-2222-2222-222222222222"


def payload(**changes):
    body = {
        "ghl_policy_id": "ghl-policy-123",
        "client_email": " Client@Example.com ",
        "named_insured": "Example Trucking LLC",
        "usdot": "1234567",
        "status": "active",
        "self_serve_enabled": True,
        "effective_date": "2026-01-01",
        "expiration_date": "2027-01-01",
        "producer_block": "Pinnacle Risk Advisors LLC",
        "insured_address": "1 Main St, Atlanta, GA 30303",
        "coverages": {"auto_liability": 1000000},
        "carriers": [{
            "line": "Auto Liability",
            "carrier": "Progressive Mountain Insurance Company",
            "policy_number": "TEST-123",
        }],
        "drivers": [{"first": "Alex", "last": "Driver", "lic_state": "GA"}],
        "data_current_as_of": "2026-08-13",
    }
    body.update(changes)
    return body


calls = {"get": [], "upsert": []}
policy_exists = False
client_rows = [{"id": CLIENT_ID, "email": "client@example.com"}]


async def fake_get(table, params):
    calls["get"].append((table, params))
    if table == "clients":
        return client_rows
    if table == "policies" and policy_exists:
        return [{"id": params["id"].removeprefix("eq."), "client_id": CLIENT_ID}]
    return []


async def fake_upsert(table, row):
    calls["upsert"].append((table, row))
    return row


main.sb_get = fake_get
main.sb_upsert = fake_upsert


def reset_calls():
    calls["get"].clear()
    calls["upsert"].clear()


print("== GoHighLevel policy sync ==")

reset_calls()
r = client.post("/api/integrations/ghl/policies", json=payload())
assert r.status_code == 401, r.text
assert not calls["get"] and not calls["upsert"]

reset_calls()
r = client.post(
    "/api/integrations/ghl/policies",
    json=payload(),
    headers={"Authorization": "Bearer wrong"},
)
assert r.status_code == 401, r.text
assert not calls["get"] and not calls["upsert"]

reset_calls()
policy_exists = False
r = client.post("/api/integrations/ghl/policies", json=payload(), headers=AUTH)
assert r.status_code == 200, r.text
assert r.json()["action"] == "created"
created_id = r.json()["policy_id"]
assert len(calls["upsert"]) == 1
row = calls["upsert"][0][1]
assert row["id"] == created_id
assert row["client_id"] == CLIENT_ID
assert row["self_serve_enabled"] is True
assert row["carriers"][0]["naic"] == "35190"
assert row["drivers"] == [{"first": "Alex", "last": "Driver", "lic_state": "GA"}]
assert calls["get"][0][1]["email"] == "eq.client@example.com"

reset_calls()
policy_exists = True
r = client.post("/api/integrations/ghl/policies", json=payload(), headers=AUTH)
assert r.status_code == 200, r.text
assert r.json()["action"] == "updated"
assert r.json()["policy_id"] == created_id

reset_calls()
policy_exists = False
r = client.post(
    "/api/integrations/ghl/policies",
    json=payload(status="canceled", self_serve_enabled=True),
    headers=AUTH,
)
assert r.status_code == 200, r.text
row = calls["upsert"][0][1]
assert row["status"] == "cancelled"
assert row["self_serve_enabled"] is False

reset_calls()
r = client.post(
    "/api/integrations/ghl/policies",
    json=payload(drivers=[{
        "first": "Alex",
        "last": "Driver",
        "lic_state": "GA",
        "date_of_birth": "1990-01-01",
    }]),
    headers=AUTH,
)
assert r.status_code == 422, r.text
assert not calls["get"] and not calls["upsert"]

reset_calls()
missing_freshness = payload()
missing_freshness.pop("data_current_as_of")
r = client.post(
    "/api/integrations/ghl/policies",
    json=missing_freshness,
    headers=AUTH,
)
assert r.status_code == 422, r.text
assert not calls["get"] and not calls["upsert"]

reset_calls()
r = client.post(
    "/api/integrations/ghl/policies",
    json=payload(carriers=[{"line": "Auto", "carrier": "Progressive"}]),
    headers=AUTH,
)
assert r.status_code == 422, r.text
assert not calls["upsert"]

reset_calls()
client_rows = []
r = client.post("/api/integrations/ghl/policies", json=payload(), headers=AUTH)
assert r.status_code == 409, r.text
assert not calls["upsert"]

reset_calls()
client_rows = [{"id": CLIENT_ID, "email": "client@example.com"}]
policy_exists = True


async def mismatched_policy_get(table, params):
    calls["get"].append((table, params))
    if table == "clients":
        return client_rows
    return [{"id": "some-id", "client_id": "33333333-3333-3333-3333-333333333333"}]


main.sb_get = mismatched_policy_get
r = client.post("/api/integrations/ghl/policies", json=payload(), headers=AUTH)
assert r.status_code == 409, r.text
assert not calls["upsert"]

print("PASS: authenticated create/update, cancellation lockout, validation, and ownership checks")
