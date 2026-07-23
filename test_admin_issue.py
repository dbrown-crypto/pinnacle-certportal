"""
test_admin_issue.py — tests for the Phase 1 owner issue-on-behalf endpoint.

Verifies, without any network access:
  1. Non-admin callers are rejected (403).
  2. Special-wording requests are rejected in Phase 1 (422).
  3. The gate still blocks owner issuance on a cancelled policy (409).
  4. Valid owner issuance returns the cert, records the client (not the
     owner) as the certificate's client, and stamps OWNER_ISSUED_BY_<uid>
     in the audit trail.
  5. The client self-serve endpoint still works untouched (regression).

Run:  python test_admin_issue.py    (exit 0 = all pass)
"""

import asyncio
import base64

from fastapi.testclient import TestClient

import main
import admin_issue

# main.py wire-up normally does this at module end; do it here for tests.
admin_issue.register(main.app)

OWNER = "owner-uuid-1"
CLIENT = "client-uuid-7"

POLICY = {
    "id": "pol-1", "client_id": CLIENT, "named_insured": "Acme Trucking LLC",
    "status": "active", "effective_date": "2026-01-01", "expiration_date": "2026-12-31",
    "self_serve_enabled": True, "coverages": {"auto_liability": 1000000},
    "carriers": [], "producer_block": "Pinnacle Risk Advisors LLC",
    "insured_address": "22700 Cumberland Pkwy SE STE 410, Atlanta, GA 30339",
    "data_current_as_of": "2026-06-30",
}

calls = {"audit": [], "inserts": [], "uploads": []}


def _reset(policy_overrides=None, caller=OWNER):
    calls["audit"].clear(); calls["inserts"].clear(); calls["uploads"].clear()
    pol = {**POLICY, **(policy_overrides or {})}

    async def fake_load_policy(policy_id): return dict(pol)
    async def fake_write_audit(client_id, policy_id, result, holder_name):
        calls["audit"].append((client_id, policy_id, list(result.audit_codes), holder_name))
    async def fake_sb_insert(table, row):
        calls["inserts"].append((table, row)); return {**row, "id": "row-1"}
    async def fake_storage_upload(path, pdf):
        calls["uploads"].append(path); return f"certificates/{path}"

    main.load_policy = fake_load_policy
    main.write_audit = fake_write_audit
    main.sb_insert = fake_sb_insert
    main.storage_upload = fake_storage_upload
    main.verify_jwt = lambda auth: caller
    main.ADMIN_USER_IDS = {OWNER}
    main.SUPABASE_URL = "http://stub"; main.SERVICE_KEY = "stub"
    main.RESEND_KEY = ""  # email off in tests


client = TestClient(main.app, raise_server_exceptions=False)
HDR = {"Authorization": "Bearer test"}

BODY = {
    "policy_id": "pol-1", "holder_name": "Shipper Co",
    "holder_address": "100 Dock St, Atlanta, GA", "holder_email": "ap@shipper.example",
    "description_of_operations": "", "requested_special_wording": [],
}

passed = failed = 0
def check(name, cond, extra=""):
    global passed, failed
    if cond: passed += 1; print(f"  PASS  {name}")
    else: failed += 1; print(f"  FAIL  {name} {extra}")

# 1. non-admin rejected
_reset(caller=CLIENT)
r = client.post("/admin/issue-certificate", json=BODY, headers=HDR)
check("non-admin caller rejected with 403", r.status_code == 403, f"got {r.status_code}")

# 2. special wording rejected in Phase 1
_reset()
r = client.post("/admin/issue-certificate",
                json={**BODY, "requested_special_wording": ["additional_insured"]}, headers=HDR)
check("special wording rejected with 422 (Phase 2 pending)", r.status_code == 422, f"got {r.status_code}")
check("no audit/insert side effects on 422", not calls["audit"] and not calls["inserts"])

# 3. gate still blocks owner on cancelled policy
_reset(policy_overrides={"status": "cancelled"})
r = client.post("/admin/issue-certificate", json=BODY, headers=HDR)
check("cancelled policy blocked with 409", r.status_code == 409, f"got {r.status_code}")
check("blocked attempt audited with owner attribution",
      len(calls["audit"]) == 1 and any(c.startswith("OWNER_ISSUED_BY_") for c in calls["audit"][0][2]))
check("no certificate row written when blocked",
      not any(t == "issued_certificates" for t, _ in calls["inserts"]))

# 3b. expired-by-date also blocked
_reset(policy_overrides={"expiration_date": "2026-01-31"})
r = client.post("/admin/issue-certificate", json=BODY, headers=HDR)
check("expired policy blocked with 409", r.status_code == 409, f"got {r.status_code}")

# 4. valid owner issuance
_reset()
r = client.post("/admin/issue-certificate", json=BODY, headers=HDR)
check("valid owner issuance returns 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
if r.status_code == 200:
    data = r.json()
    check("response marked issued_by_owner", data.get("issued_by_owner") is True)
    check("pdf_base64 decodes to a PDF",
          base64.b64decode(data["pdf_base64"]).startswith(b"%PDF"))
    check("cert recorded for the CLIENT, not the owner",
          any(t == "issued_certificates" and row["client_id"] == CLIENT
              for t, row in calls["inserts"]))
    check("owner attribution stored in coverage_snapshot",
          any(t == "issued_certificates" and row["coverage_snapshot"].get("owner_issued_by") == OWNER
              for t, row in calls["inserts"]))
    check("audit row attributed to client with OWNER_ISSUED_BY code",
          calls["audit"][0][0] == CLIENT and
          any(c == f"OWNER_ISSUED_BY_{OWNER}" for c in calls["audit"][0][2]))
    check("pdf stored under the client's folder",
          calls["uploads"] and calls["uploads"][0].startswith(f"{CLIENT}/"))

# 5. regression: client self-serve path still works
_reset(caller=CLIENT)
r = client.post("/issue-certificate", json=BODY, headers=HDR)
check("client self-serve issuance still returns 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")

print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
raise SystemExit(0 if failed == 0 else 1)
