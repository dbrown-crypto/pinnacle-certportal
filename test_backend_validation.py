"""
Backend validation + CORS tests for the COI portal.

Isolated: no network, no Supabase, no production data. All side effects are
monkeypatched and counted so we can assert that rejected requests do nothing.
Run: python test_backend_validation.py
"""
import importlib
import os
import sys

from fastapi.testclient import TestClient

import gate

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def load_main(allowed_origins=None):
    """Import/reload main.py with a chosen ALLOWED_ORIGINS env."""
    if allowed_origins is None:
        os.environ.pop("ALLOWED_ORIGINS", None)
    else:
        os.environ["ALLOWED_ORIGINS"] = allowed_origins
    if "main" in sys.modules:
        return importlib.reload(sys.modules["main"])
    import main
    return main


VALID_WORDING = [w.value for w in gate.SpecialWording]
TRUSTED = "https://coi.pinnacleriskad.com"


def issue_body(wording):
    return {
        "policy_id": "11111111-1111-1111-1111-111111111111",
        "holder_name": "Acme Freight",
        "holder_address": "1 Main St",
        "description_of_operations": "hauling",
        "requested_special_wording": wording,
    }


print("== special wording validation ==")
m = load_main()
client = TestClient(m.app)

# Count every side effect the issue path could reach.
calls = {"audit": 0, "insert": 0, "upload": 0, "policy": 0, "jwt": 0, "email": 0}


async def _no_audit(*a, **k):
    calls["audit"] += 1


async def _no_insert(*a, **k):
    calls["insert"] += 1
    return {}


async def _no_upload(*a, **k):
    calls["upload"] += 1
    return ""


async def _no_policy(*a, **k):
    calls["policy"] += 1
    return {}


async def _no_email(*a, **k):
    calls["email"] += 1


def _no_jwt(auth):
    calls["jwt"] += 1
    return "22222222-2222-2222-2222-222222222222"


m.write_audit = _no_audit
m.sb_insert = _no_insert
m.storage_upload = _no_upload
m.load_policy = _no_policy
m.send_certificate_email = _no_email
m.verify_jwt = _no_jwt

for w in VALID_WORDING:
    try:
        parsed = m.IssueRequest(**issue_body([w]))
        ok = parsed.requested_special_wording == [gate.SpecialWording(w)]
    except Exception:
        ok = False
    check(f"valid wording accepted by validation: {w}", ok)

try:
    parsed = m.IssueRequest(**issue_body(VALID_WORDING))
    ok = [x.value for x in parsed.requested_special_wording] == VALID_WORDING
except Exception:
    ok = False
check("all four wording values together accepted", ok)

try:
    m.IssueRequest(**issue_body(["totally_made_up"]))
    ok = False
except Exception:
    ok = True
check("unknown wording rejected at model level", ok)

check("wording field default is an empty list (no shared mutable)",
      m.IssueRequest(**{k: v for k, v in issue_body([]).items()
                        if k != "requested_special_wording"}).requested_special_wording == [])

calls.update({k: 0 for k in calls})
r = client.post("/issue-certificate", json=issue_body(["totally_made_up"]))
check("unknown wording -> 422", r.status_code == 422)
check("unknown wording -> zero audit writes", calls["audit"] == 0)
check("unknown wording -> zero db inserts", calls["insert"] == 0)
check("unknown wording -> zero storage uploads", calls["upload"] == 0)
check("unknown wording -> zero policy loads", calls["policy"] == 0)
check("unknown wording -> zero certificate emails", calls["email"] == 0)

r = client.post("/issue-certificate", json=issue_body(["<img src=x onerror=alert(1)>"]))
check("markup payload as wording -> 422", r.status_code == 422)

print("== special-request status validation ==")
patches = []


async def _fake_patch(table, match, payload):
    patches.append((table, match, payload))
    return [{"id": match.get("id"), "status": payload.get("status")}]


m.sb_patch = _fake_patch
m.require_admin = lambda auth: "admin-uid"

for st in ["open", "in_progress", "sent", "declined"]:
    patches.clear()
    r = client.post("/admin/special-requests/abc/status", json={"status": st})
    check(f"valid status accepted: {st}", r.status_code == 200)
    check(f"valid status written unchanged: {st}",
          len(patches) == 1 and patches[0][2].get("status") == st)

for bad in [{"status": "nope"}, {}, {"status": None}, {"status": 5}]:
    patches.clear()
    r = client.post("/admin/special-requests/abc/status", json=bad)
    check(f"invalid status -> 422: {bad}", r.status_code == 422)
    check(f"invalid status -> zero supabase patches: {bad}", len(patches) == 0)

print("== admin auth still enforced ==")
m2 = load_main()
c2 = TestClient(m2.app)
r = c2.get("/admin/policies")
check("admin endpoint without auth -> 401", r.status_code == 401)
r = c2.get("/admin/policies", headers={"Authorization": "Bearer garbage"})
check("admin endpoint with bad token -> 401", r.status_code == 401)

print("== CORS ==")
md = load_main()
check("default origin list is exactly the COI portal", md.ALLOWED_ORIGINS == [TRUSTED])
check("default never contains wildcard", "*" not in md.ALLOWED_ORIGINS)
cd = TestClient(md.app)
r = cd.get("/healthz", headers={"Origin": TRUSTED})
check("trusted origin gets allow-origin header",
      r.headers.get("access-control-allow-origin") == TRUSTED)
r = cd.get("/healthz", headers={"Origin": "https://evil.example.com"})
check("untrusted origin gets no allow-origin header",
      r.headers.get("access-control-allow-origin") is None)
check("no allow-credentials header emitted",
      r.headers.get("access-control-allow-credentials") is None)

mt = load_main("  https://a.example.com , ,https://b.example.com  ")
check("configured origins trimmed and blanks dropped",
      mt.ALLOWED_ORIGINS == ["https://a.example.com", "https://b.example.com"])
mb = load_main("   ")
check("blank-only config falls back to COI portal", mb.ALLOWED_ORIGINS == [TRUSTED])
check("wildcard never combined with credentials",
      not ("*" in mb.ALLOWED_ORIGINS and getattr(mb, "ALLOW_CREDENTIALS", False)))

print("== customer creation response ==")
mc = load_main()


class _Resp:
    status_code = 200

    def json(self):
        return {"id": "33333333-3333-3333-3333-333333333333"}

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _Resp()

    async def get(self, *a, **k):
        return _Resp()

    async def put(self, *a, **k):
        return _Resp()


async def _fake_upsert(*a, **k):
    return [{}]


mc.httpx.AsyncClient = _FakeClient
mc.sb_upsert = _fake_upsert
mc.require_admin = lambda auth: "admin-uid"
cc = TestClient(mc.app)
r = cc.post("/admin/customers", json={"email": "x@example.com", "insured_name": "X Co", "password": "supersecret1"})
check("customer creation returns 200", r.status_code == 200)
body = r.json() if r.status_code == 200 else {}
check("response omits password", "password" not in body)
check("response keeps client_id", "client_id" in body)
check("response keeps email", "email" in body)
check("no password value leaked anywhere in body", "supersecret1" not in r.text)

print("== backend email phone number ==")
src = open("main.py").read()
check("main.py contains corrected phone", "(770) 758-3197" in src)
check("main.py has no old phone", "(943) 239-3439" not in src)
repo_old = os.popen(
    "grep -rn '(943) 239-3439' . --include=* 2>/dev/null "
    "| grep -v '^./.git/' | grep -v 'test_backend_validation.py' | wc -l"
).read().strip()
check("old phone absent repo-wide", repo_old == "0")

print("== admin issue-on-behalf endpoint validation ==")
mai = load_main()
import admin_issue as ai


def admin_body(wording):
    return {
        "policy_id": "11111111-1111-1111-1111-111111111111",
        "holder_name": "Acme Freight",
        "holder_address": "1 Main St",
        "requested_special_wording": wording,
    }


for w in VALID_WORDING:
    try:
        parsed = ai.AdminIssueRequest(**admin_body([w]))
        ok = parsed.requested_special_wording == [gate.SpecialWording(w)]
    except Exception:
        ok = False
    check(f"AdminIssueRequest accepts valid wording: {w}", ok)

try:
    parsed = ai.AdminIssueRequest(**admin_body(VALID_WORDING))
    ok = [x.value for x in parsed.requested_special_wording] == VALID_WORDING
except Exception:
    ok = False
check("AdminIssueRequest accepts all four wording values together", ok)

check("AdminIssueRequest wording default is empty list",
      ai.AdminIssueRequest(**{k: v for k, v in admin_body([]).items()
                              if k != "requested_special_wording"}).requested_special_wording == [])

try:
    ai.AdminIssueRequest(**admin_body(["totally_made_up"]))
    ok = False
except Exception:
    ok = True
check("AdminIssueRequest rejects unknown wording at model level", ok)

# Count every side effect reachable from the admin issue path.
acalls = {"admin_auth": 0, "policy": 0, "audit": 0, "insert": 0, "patch": 0,
          "upload": 0, "certgen": 0, "email": 0, "gate": 0}


def _a_admin(auth):
    acalls["admin_auth"] += 1
    return "admin-uid"


async def _a_policy(*a, **k):
    acalls["policy"] += 1
    return {}


async def _a_audit(*a, **k):
    acalls["audit"] += 1


async def _a_insert(*a, **k):
    acalls["insert"] += 1
    return {}


async def _a_patch(*a, **k):
    acalls["patch"] += 1
    return [{}]


async def _a_upload(*a, **k):
    acalls["upload"] += 1
    return ""


def _a_certgen(*a, **k):
    acalls["certgen"] += 1
    return b""


async def _a_email(*a, **k):
    acalls["email"] += 1


def _a_gate(*a, **k):
    acalls["gate"] += 1
    raise AssertionError("gate should not run for an invalid request")


mai.require_admin = _a_admin
mai.load_policy = _a_policy
mai.write_audit = _a_audit
mai.sb_insert = _a_insert
mai.sb_patch = _a_patch
mai.storage_upload = _a_upload
mai.generate_certificate = _a_certgen
mai.send_certificate_email = _a_email
mai.evaluate_gate = _a_gate

cai = TestClient(mai.app)
r = cai.post("/admin/issue-certificate", json=admin_body(["totally_made_up"]),
             headers={"Authorization": "Bearer whatever"})
check("admin endpoint rejects unknown wording -> 422", r.status_code == 422)
check("admin rejection -> zero admin authorization calls", acalls["admin_auth"] == 0)
check("admin rejection -> zero policy loads", acalls["policy"] == 0)
check("admin rejection -> zero audit writes", acalls["audit"] == 0)
check("admin rejection -> zero db inserts", acalls["insert"] == 0)
check("admin rejection -> zero db patches", acalls["patch"] == 0)
check("admin rejection -> zero storage uploads", acalls["upload"] == 0)
check("admin rejection -> zero certificate generation", acalls["certgen"] == 0)
check("admin rejection -> zero email calls", acalls["email"] == 0)
check("admin rejection -> zero gate evaluations", acalls["gate"] == 0)

r = cai.post("/admin/issue-certificate", json=admin_body(["<img src=x onerror=alert(1)>"]),
             headers={"Authorization": "Bearer whatever"})
check("admin endpoint rejects markup wording -> 422", r.status_code == 422)

print(f"\nFINAL: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
sys.exit(1 if FAIL else 0)
