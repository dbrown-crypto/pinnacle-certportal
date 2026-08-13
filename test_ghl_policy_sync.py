"""No-network tests for the GoHighLevel policy-line synchronization endpoint."""

import os

from fastapi.testclient import TestClient

TEST_SECRET = "test-sync-secret-with-at-least-32-bytes"
os.environ["GHL_POLICY_SYNC_SECRET"] = TEST_SECRET

import main


client = TestClient(main.app)
AUTH = {"Authorization": f"Bearer {TEST_SECRET}"}
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
calls = {"get": [], "upsert": []}
client_rows = [{"id": CLIENT_ID, "email": "client@example.com"}]
policy_rows = []


def payload(**changes):
    body = {
        "ghl_policy_id": "ghl-auto-123",
        "named_insured": "Example Trucking LLC",
        "insured_address": "1 Main St, Atlanta, GA 30303",
        "usdot": "1234567",
        "status": "Active",
        "self_serve_enabled": True,
        "effective_date": "2026-01-01",
        "expiration_date": "2027-01-01",
        "data_current_as_of": "2026-08-13",
        "producer_block": "Pinnacle Risk Advisors LLC",
        "line_of_business": "Commercial Auto",
        "carrier": "Progressive Mountain Insurance Company",
        "policy_number": "AUTO-123",
        "coverage_limit": "$1,000,000",
        "carrier_naic": "",
        "auto_symbols": "Scheduled, Hired, Non-Owned",
    }
    body.update(changes)
    return body


async def fake_get(table, params):
    calls["get"].append((table, params))
    if table == "clients":
        return client_rows
    if "id" in params:
        wanted = params["id"].removeprefix("eq.")
        return [p for p in policy_rows if p["id"] == wanted]
    return list(policy_rows)


async def fake_upsert(table, row):
    calls["upsert"].append((table, row))
    policy_rows[:] = [p for p in policy_rows if p["id"] != row["id"]] + [row]
    return row


main.sb_get = fake_get
main.sb_upsert = fake_upsert


async def fake_resolve_contact_email(policy_id):
    assert policy_id.startswith("ghl-")
    return "client@example.com"


main.ghl_policy_sync._resolve_associated_contact_email = fake_resolve_contact_email


def reset():
    calls["get"].clear()
    calls["upsert"].clear()


print("== GoHighLevel policy-line sync ==")

reset()
r = client.post("/api/integrations/ghl/policies", json=payload())
assert r.status_code == 401 and not calls["get"] and not calls["upsert"]

reset()
r = client.post("/api/integrations/ghl/policies", json=payload(), headers={"Authorization": "Bearer wrong"})
assert r.status_code == 401 and not calls["get"] and not calls["upsert"]

# Create the combined portal snapshot with its Auto line.
reset()
r = client.post("/api/integrations/ghl/policies", json=payload(), headers=AUTH)
assert r.status_code == 200, r.text
assert r.json()["action"] == "created" and r.json()["active_lines"] == 1
portal_id = r.json()["policy_id"]
row = policy_rows[0]
assert row["coverages"]["auto_liability"] == 1000000
assert row["carriers"][0]["naic"] == "35190"
assert row["carriers"][0]["autos"] == ["SCHEDULED", "HIRED", "NON-OWNED"]
assert row["self_serve_enabled"] is True
assert calls["get"][0][1]["email"] == "eq.client@example.com"

# Common GHL checkbox renderings normalize without weakening strict fields.
reset()
r = client.post(
    "/api/integrations/ghl/policies",
    json=payload(self_serve_enabled=["true"], auto_symbols='["ANY", "HIRED"]'),
    headers=AUTH,
)
assert r.status_code == 200, r.text
assert policy_rows[0]["self_serve_enabled"] is True
assert policy_rows[0]["carriers"][0]["autos"] == ["ANY", "HIRED"]

# A separate Cargo object merges into the same portal policy.
reset()
cargo = payload(
    ghl_policy_id="ghl-cargo-456", line_of_business="Motor Truck Cargo",
    policy_number="CARGO-456", coverage_limit=100000, deductible=1000,
    self_serve_enabled=False,
)
r = client.post("/api/integrations/ghl/policies", json=cargo, headers=AUTH)
assert r.status_code == 200, r.text
assert r.json()["policy_id"] == portal_id and r.json()["active_lines"] == 2
assert len(policy_rows) == 1
assert policy_rows[0]["coverages"] == {"auto_liability": 1000000.0, "cargo": 100000.0}
assert policy_rows[0]["self_serve_enabled"] is True

# GL carries explicit form and aggregate basis so the ACORD boxes are not guessed.
reset()
gl = payload(
    ghl_policy_id="ghl-gl-789", line_of_business="General Liability",
    policy_number="GL-789", coverage_limit=1000000, aggregate_limit=2000000,
    gl_coverage_form="Occurrence", gl_aggregate_basis="Policy",
)
r = client.post("/api/integrations/ghl/policies", json=gl, headers=AUTH)
assert r.status_code == 200, r.text
assert r.json()["active_lines"] == 3
gl_row = next(c for c in policy_rows[0]["carriers"] if c["ghl_policy_id"] == "ghl-gl-789")
assert gl_row["form"] == "OCCURRENCE" and gl_row["aggregate"] == "POLICY"

# Retrying/updating Auto replaces that line rather than duplicating it.
reset()
r = client.post("/api/integrations/ghl/policies", json=payload(coverage_limit=750000), headers=AUTH)
assert r.status_code == 200, r.text
assert len(policy_rows[0]["carriers"]) == 3
assert policy_rows[0]["coverages"]["auto_liability"] == 750000

# Lapsed/Cancelled/Non-Renewed are inactive line states. Removing Cargo leaves
# Auto active and self-service enabled.
reset()
r = client.post("/api/integrations/ghl/policies", json={**cargo, "status": "Lapsed", "coverage_limit": ""}, headers=AUTH)
assert r.status_code == 200, r.text
assert r.json()["active_lines"] == 2
assert policy_rows[0]["status"] == "active"
assert policy_rows[0]["self_serve_enabled"] is True

# Non-renewing Auto removes the required line and always closes self-service.
reset()
r = client.post("/api/integrations/ghl/policies", json=payload(status="Non-Renewed", coverage_limit=""), headers=AUTH)
assert r.status_code == 200, r.text
assert policy_rows[0]["status"] == "expired"
assert policy_rows[0]["self_serve_enabled"] is False
assert "auto_liability" not in policy_rows[0]["coverages"]

# GHL's Cancelled spelling remains supported and maps to portal cancelled.
reset()
r = client.post("/api/integrations/ghl/policies", json=payload(status="Cancelled", coverage_limit=""), headers=AUTH)
assert r.status_code == 200, r.text
assert policy_rows[0]["status"] == "cancelled"

# Strict payload rejects sensitive or unknown fields before any database work.
reset()
r = client.post("/api/integrations/ghl/policies", json=payload(driver_license="SECRET"), headers=AUTH)
assert r.status_code == 422 and not calls["get"] and not calls["upsert"]

reset()
r = client.post("/api/integrations/ghl/policies", json=payload(client_email="spoof@example.com"), headers=AUTH)
assert r.status_code == 422 and not calls["get"] and not calls["upsert"]

reset()
missing_freshness = payload()
missing_freshness.pop("data_current_as_of")
r = client.post("/api/integrations/ghl/policies", json=missing_freshness, headers=AUTH)
assert r.status_code == 422 and not calls["get"] and not calls["upsert"]

# Brand-only carrier names are refused before a write.
reset()
policy_rows.clear()
r = client.post("/api/integrations/ghl/policies", json=payload(carrier="Progressive"), headers=AUTH)
assert r.status_code == 422 and not calls["upsert"]

# Missing client and ambiguous portal-policy ownership fail closed.
reset()
client_rows.clear()
r = client.post("/api/integrations/ghl/policies", json=payload(), headers=AUTH)
assert r.status_code == 409 and not calls["upsert"]

# Relation parsing supports either direction and ignores unrelated objects.
extract = main.ghl_policy_sync._contact_ids_from_relations
relations = {"relations": [
    {"firstObjectKey": "custom_objects.policy", "firstRecordId": "p1", "secondObjectKey": "contact", "secondRecordId": "c1"},
    {"firstObjectKey": "contacts", "firstRecordId": "c2", "secondObjectKey": "custom_objects.policy", "secondRecordId": "p2"},
    {"firstObjectKey": "custom_objects.policy", "firstRecordId": "p1", "secondObjectKey": "business", "secondRecordId": "b1"},
]}
assert extract(relations, "p1") == {"c1"}
assert extract(relations, "p2") == {"c2"}

client_rows.append({"id": CLIENT_ID, "email": "client@example.com"})
policy_rows[:] = [
    {"id": "11111111-1111-1111-1111-111111111111", "client_id": CLIENT_ID},
    {"id": "22222222-2222-2222-2222-222222222222", "client_id": CLIENT_ID},
]
reset()
r = client.post("/api/integrations/ghl/policies", json=payload(), headers=AUTH)
assert r.status_code == 409 and not calls["upsert"]

print("PASS: secure grouping, line merge/update/cancellation, validation, and ownership checks")
