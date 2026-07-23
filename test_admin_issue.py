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

# 2. special wording without owner confirmation is rejected (Phase 2 contract)
_reset()
r = client.post("/admin/issue-certificate",
                json={**BODY, "requested_special_wording": ["additional_insured"]}, headers=HDR)
check("special wording without confirmation rejected with 400", r.status_code == 400, f"got {r.status_code}")
check("no audit/insert side effects on rejection", not calls["audit"] and not calls["inserts"])

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

print(f"\n[phase1 subtotal] {passed} passed, {failed} failed")

# ===================== Phase 2 tests =====================
POLICY_ENDO = {**POLICY, "endorsements": {
    "additional_insured": {"lines": ["general_liability"], "form": "CG 20 10 04 13"},
    "waiver_of_subrogation": {"lines": ["auto_liability"], "form": "CA 04 44"},
}}
gen_calls = []
_real_gen = main.generate_certificate
def _spy_gen(content, **kw):
    gen_calls.append({"ops": content.description_of_operations, "sig": kw.get("signature_png_path")})
    return b"%PDF-1.4 spy " + content.cert_number.encode()
main.generate_certificate = _spy_gen
main.SIGNATURE_PATH = "/srv/sig.png"
patches = []
async def fake_sb_patch(table, match, patch):
    patches.append((table, match, patch)); return [{"id": match.get("id"), **patch}]
main.sb_patch = fake_sb_patch

W = {**BODY, "requested_special_wording": ["additional_insured"], "confirm_wording": True,
     "wording_line": "General Liability"}

# P2-1 wording without confirm -> 400
_reset(); main.sb_patch = fake_sb_patch
r = client.post("/admin/issue-certificate", json={**W, "confirm_wording": False}, headers=HDR)
check("P2: wording without confirmation rejected 400", r.status_code == 400, f"got {r.status_code}")

# P2-2 wording not flagged on policy -> 409 refused
_reset(); main.sb_patch = fake_sb_patch
r = client.post("/admin/issue-certificate", json=W, headers=HDR)
check("P2: wording refused when policy has no endorsement flags", r.status_code == 409 and "not flagged" in r.text, f"got {r.status_code}")

# P2-3 flagged policy -> 200, approved clause + form number in ops, audit codes, unsigned by default
_reset(policy_overrides={"endorsements": POLICY_ENDO["endorsements"]}); main.sb_patch = fake_sb_patch
gen_calls.clear()
r = client.post("/admin/issue-certificate", json=W, headers=HDR)
check("P2: AI wording issues 200 on flagged policy", r.status_code == 200, f"got {r.status_code} {r.text[:150]}")
if r.status_code == 200:
    d = r.json()
    check("P2: approved AI clause printed with form number",
          "included as an additional insured" in gen_calls[-1]["ops"] and "CG 20 10 04 13" in gen_calls[-1]["ops"])
    check("P2: signature NOT applied by default", gen_calls[-1]["sig"] is None and d["signature_applied"] is False)
    check("P2: audit has AUTHORIZED_WORDING code",
          any("AUTHORIZED_WORDING_ADDITIONAL_INSURED" in c for c in calls["audit"][0][2]))
    check("P2: wording stored in coverage_snapshot",
          any(t == "issued_certificates" and row["coverage_snapshot"]["wording_kinds"] == ["additional_insured"] for t, row in calls["inserts"]))

# P2-4 signature on request -> applied + logged
_reset(policy_overrides={"endorsements": POLICY_ENDO["endorsements"]}); main.sb_patch = fake_sb_patch
gen_calls.clear()
r = client.post("/admin/issue-certificate", json={**W, "apply_signature": True}, headers=HDR)
check("P2: signature applied when owner opts in",
      r.status_code == 200 and gen_calls[-1]["sig"] == "/srv/sig.png" and
      any("SIGNATURE_APPLIED_BY_OWNER" in c for c in calls["audit"][0][2]), f"got {r.status_code}")

# P2-5 custom wording verbatim; missing text -> 400
_reset(policy_overrides={"endorsements": {}}); main.sb_patch = fake_sb_patch
r = client.post("/admin/issue-certificate", json={**BODY, "requested_special_wording": ["special_language"], "confirm_wording": True}, headers=HDR)
check("P2: custom wording without text rejected 400", r.status_code == 400, f"got {r.status_code}")
gen_calls.clear()
r = client.post("/admin/issue-certificate", json={**BODY, "requested_special_wording": ["special_language"], "confirm_wording": True, "custom_wording": "Verbatim contract clause XYZ."}, headers=HDR)
check("P2: custom wording prints verbatim", r.status_code == 200 and "Verbatim contract clause XYZ." in gen_calls[-1]["ops"], f"got {r.status_code}")

# P2-6 fulfilling a special request marks it sent
_reset(policy_overrides={"endorsements": POLICY_ENDO["endorsements"]}); main.sb_patch = fake_sb_patch
patches.clear()
r = client.post("/admin/issue-certificate", json={**W, "special_request_id": "req-9"}, headers=HDR)
check("P2: special request marked sent",
      r.status_code == 200 and any(t == "special_requests" and mt.get("id") == "req-9" and p.get("status") == "sent" for t, mt, p in patches))

# P2-7 gate still blocks special wording on cancelled policy
_reset(policy_overrides={"status": "cancelled", "endorsements": POLICY_ENDO["endorsements"]}); main.sb_patch = fake_sb_patch
r = client.post("/admin/issue-certificate", json=W, headers=HDR)
check("P2: cancelled policy still blocked 409 even with wording", r.status_code == 409, f"got {r.status_code}")

# P2-8 endorsements endpoint: admin sets flags; non-admin rejected
_reset(); main.sb_patch = fake_sb_patch
r = client.post("/admin/policies/pol-1/endorsements", json={"endorsements": {"waiver_of_subrogation": {"form": "CA 04 44"}}}, headers=HDR)
check("P2: endorsements patch works for admin", r.status_code == 200, f"got {r.status_code}")
_reset(caller=CLIENT); main.sb_patch = fake_sb_patch
r = client.post("/admin/policies/pol-1/endorsements", json={"endorsements": {}}, headers=HDR)
check("P2: endorsements patch rejected for non-admin", r.status_code == 403, f"got {r.status_code}")

# P2-9 regression: plain owner issuance keeps signature (standard behavior)
_reset(); main.sb_patch = fake_sb_patch
gen_calls.clear()
r = client.post("/admin/issue-certificate", json=BODY, headers=HDR)
check("P2: plain cert keeps standard signature behavior",
      r.status_code == 200 and gen_calls[-1]["sig"] == "/srv/sig.png", f"got {r.status_code}")

print(f"\nFINAL: {passed} passed, {failed} failed, {passed + failed} total")
import sys as _s; _s.exit(0 if failed == 0 else 1)
