"""
main.py — Pinnacle Risk Advisors self-serve certificate portal (backend)

The server-side enforcement point. The browser cannot be trusted: hiding the
"Additional Insured" checkbox in the UI is convenience, but THIS service is the
control. On every issuance request it:

  1. Verifies the caller's Supabase JWT (who are you).
  2. Loads the policy with the SERVICE_ROLE key, then confirms it belongs to
     that client (defense in depth on top of RLS).
  3. Runs evaluate_gate() — the same pure function in gate.py.
  4. Branches on the route:
       auto_issue     -> generate, sign, store, email, write audit, return PDF
       route_to_agent -> create special_request, notify Derrick, write audit
       block          -> write audit (blocked attempt), return the reason
  5. Never issues anything the gate did not approve.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Env:  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_JWT_SECRET,
      RESEND_API_KEY, MAIL_FROM (e.g. "Pinnacle Risk Advisors <certs@pinnacleriskad.com>"),
      AGENT_NOTIFY_EMAIL (dbrown@pinnacleriskad.com),
      ACORD25_TEMPLATE_PATH (optional; omit to use branded fallback),
      SIGNATURE_PNG_PATH (optional)
"""

from __future__ import annotations

import os
import base64
import datetime as dt
from typing import Optional

import httpx
import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from gate import (
    evaluate_gate, PolicySnapshot, CertRequest, PolicyStatus, SpecialWording, GateResult,
)
from cert_generator import generate_certificate, CertContent

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "Pinnacle Risk Advisors <certs@pinnacleriskad.com>")
AGENT_EMAIL = os.environ.get("AGENT_NOTIFY_EMAIL", "dbrown@pinnacleriskad.com")
TEMPLATE_PATH = os.environ.get("ACORD25_TEMPLATE_PATH") or None
SIGNATURE_PATH = os.environ.get("SIGNATURE_PNG_PATH") or None
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

# Supabase now signs auth tokens with asymmetric keys (ES256) by default. We
# verify them against the project's public JWKS. Legacy HS256 shared-secret
# tokens are still accepted as a fallback if SUPABASE_JWT_SECRET is set.
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else ""
_jwk_client = PyJWKClient(JWKS_URL) if JWKS_URL else None

app = FastAPI(title="Pinnacle Self-Serve Certificates")
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)


# --- request/response models --------------------------------------------------
class IssueRequest(BaseModel):
    policy_id: str
    holder_name: str
    holder_address: str
    holder_email: Optional[str] = None
    description_of_operations: str = ""
    requested_special_wording: list[str] = []   # values from SpecialWording enum


# --- auth ---------------------------------------------------------------------
def verify_jwt(authorization: Optional[str]) -> str:
    """Return the authenticated user's id (== clients.id) or raise 401.

    Handles Supabase's current asymmetric signing keys (ES256/RS256) by
    verifying against the project's public JWKS, and falls back to a legacy
    HS256 shared secret for older tokens. Survives key rotation."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token.")
    token = authorization.split(" ", 1)[1]
    try:
        alg = jwt.get_unverified_header(token).get("alg", "")
        if alg.startswith(("ES", "RS")):
            if not _jwk_client:
                raise HTTPException(401, "Auth not configured.")
            key = _jwk_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=[alg], audience="authenticated")
        else:
            claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience="authenticated")
        return claims["sub"]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid or expired session.")


# --- Supabase REST helpers (service role) ------------------------------------
def _sb_headers() -> dict:
    return {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }


async def load_policy(policy_id: str) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/policies"
    params = {"id": f"eq.{policy_id}", "select": "*"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url, headers=_sb_headers(), params=params)
        r.raise_for_status()
        rows = r.json()
    if not rows:
        raise HTTPException(404, "Policy not found.")
    return rows[0]


async def sb_insert(table: str, row: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers={**_sb_headers(), "Prefer": "return=representation"}, json=row)
        r.raise_for_status()
        return r.json()[0]


async def storage_upload(path: str, pdf: bytes) -> str:
    url = f"{SUPABASE_URL}/storage/v1/object/certificates/{path}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers={**_sb_headers(), "Content-Type": "application/pdf"}, content=pdf)
        r.raise_for_status()
    return f"certificates/{path}"


# --- email (Resend) -----------------------------------------------------------
async def send_certificate_email(to: list[str], cert_number: str, insured: str, pdf: bytes):
    """Deliver to the holder under the Pinnacle domain, CC the insured + Derrick."""
    payload = {
        "from": MAIL_FROM,
        "to": to,
        "cc": [AGENT_EMAIL],
        "subject": f"Certificate of Insurance — {insured} ({cert_number})",
        "text": (
            f"Attached is the certificate of insurance for {insured}, "
            f"issued by Pinnacle Risk Advisors.\n\n"
            f"Certificate number: {cert_number}\n"
            f"This certificate is issued as a matter of information only.\n\n"
            f"Pinnacle Risk Advisors LLC · (943) 239-3439 · dbrown@pinnacleriskad.com"
        ),
        "attachments": [{
            "filename": f"COI_{cert_number}.pdf",
            "content": base64.b64encode(pdf).decode(),
        }],
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.resend.com/emails",
                         headers={"Authorization": f"Bearer {RESEND_KEY}"}, json=payload)
        r.raise_for_status()


async def notify_agent_special(req: IssueRequest, policy: dict):
    payload = {
        "from": MAIL_FROM,
        "to": [AGENT_EMAIL],
        "subject": f"[ACTION] Special-wording cert request — {policy['named_insured']}",
        "text": (
            f"A client requested a certificate that needs review (not auto-issued).\n\n"
            f"Insured: {policy['named_insured']}\n"
            f"Holder:  {req.holder_name}\n         {req.holder_address}\n"
            f"Email:   {req.holder_email or '—'}\n"
            f"Wording: {', '.join(req.requested_special_wording)}\n"
            f"Ops:     {req.description_of_operations}\n\n"
            f"Open it in the admin queue to review and send."
        ),
    }
    async with httpx.AsyncClient(timeout=20) as c:
        await c.post("https://api.resend.com/emails",
                     headers={"Authorization": f"Bearer {RESEND_KEY}"}, json=payload)


# --- helpers ------------------------------------------------------------------
def _to_snapshot(policy: dict) -> PolicySnapshot:
    return PolicySnapshot(
        policy_id=policy["id"],
        named_insured=policy["named_insured"],
        status=PolicyStatus(policy["status"]),
        effective_date=dt.date.fromisoformat(policy["effective_date"]),
        expiration_date=dt.date.fromisoformat(policy["expiration_date"]),
        self_serve_enabled=policy["self_serve_enabled"],
        coverages=policy.get("coverages") or {},
        data_current_as_of=dt.date.fromisoformat(policy["data_current_as_of"]),
    )


def _next_cert_number() -> str:
    stamp = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    return f"PRA-{stamp}"


async def write_audit(client_id, policy_id, result: GateResult, holder_name: str):
    await sb_insert("audit_log", {
        "client_id": client_id, "policy_id": policy_id,
        "action": result.route, "audit_codes": result.audit_codes,
        "reasons": result.reasons, "holder_name": holder_name,
    })


# --- endpoints ----------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "template": bool(TEMPLATE_PATH), "signature": bool(SIGNATURE_PATH)}


@app.post("/issue-certificate")
async def issue_certificate(body: IssueRequest, authorization: Optional[str] = Header(None)):
    user_id = verify_jwt(authorization)
    policy = await load_policy(body.policy_id)

    # Defense in depth: even though RLS scopes client reads, the service-role
    # backend can read any policy, so re-check ownership here explicitly.
    if policy["client_id"] != user_id:
        raise HTTPException(403, "This policy is not associated with your account.")

    req = CertRequest(
        holder_name=body.holder_name,
        holder_address=body.holder_address,
        holder_email=body.holder_email,
        description_of_operations=body.description_of_operations,
        requested_special_wording=[SpecialWording(w) for w in body.requested_special_wording],
    )

    result = evaluate_gate(_to_snapshot(policy), req)
    await write_audit(user_id, policy["id"], result, body.holder_name)

    # ---- route_to_agent ----
    if result.route == "route_to_agent":
        await sb_insert("special_requests", {
            "policy_id": policy["id"], "client_id": user_id,
            "holder_name": req.holder_name, "holder_address": req.holder_address,
            "holder_email": req.holder_email,
            "requested_wording": body.requested_special_wording,
            "notes": req.description_of_operations,
        })
        if RESEND_KEY:
            try:
                await notify_agent_special(body, policy)
            except Exception:
                pass  # the special_requests row is the source of truth; email is a nudge
        return {"status": "routed", "message": result.reasons[0]}

    # ---- block ----
    if result.route == "block":
        raise HTTPException(409, detail={"status": "blocked", "message": result.reasons[0]})

    # ---- auto_issue ----
    cert_number = _next_cert_number()
    content = CertContent(
        cert_number=cert_number,
        issue_date=dt.date.today(),
        producer_block=policy["producer_block"],
        insured_name=policy["named_insured"],
        insured_address=policy["insured_address"],
        holder_name=req.holder_name,
        holder_address=req.holder_address,
        description_of_operations=req.description_of_operations,
        coverages=policy.get("carriers") or [],
        data_current_as_of=dt.date.fromisoformat(policy["data_current_as_of"]),
    )
    pdf = generate_certificate(
        content,
        template_path=TEMPLATE_PATH,
        field_map=_build_field_map(policy, req, cert_number) if TEMPLATE_PATH else None,
        signature_png_path=SIGNATURE_PATH,
    )

    pdf_path = None
    if SUPABASE_URL and SERVICE_KEY:
        pdf_path = await storage_upload(f"{user_id}/{cert_number}.pdf", pdf)

    await sb_insert("issued_certificates", {
        "cert_number": cert_number, "policy_id": policy["id"], "client_id": user_id,
        "holder_name": req.holder_name, "holder_address": req.holder_address,
        "holder_email": req.holder_email, "description_of_ops": req.description_of_operations,
        "coverage_snapshot": {"coverages": policy.get("coverages"),
                              "carriers": policy.get("carriers"),
                              "data_current_as_of": policy["data_current_as_of"]},
        "pdf_path": pdf_path,
    })

    # Email is the convenience copy; the cert is the product. A delivery failure
    # (unverified domain, Resend hiccup, bad address) must NEVER discard a cert
    # the client successfully issued. So the send is best-effort and its outcome
    # is reported back, not raised.
    recipients = [r for r in [req.holder_email] if r]
    email_status = "not_sent"
    if recipients and RESEND_KEY:
        try:
            await send_certificate_email(recipients, cert_number, policy["named_insured"], pdf)
            email_status = "sent"
        except Exception as e:
            email_status = f"failed: {type(e).__name__}"

    return {
        "status": "issued",
        "cert_number": cert_number,
        "pdf_base64": base64.b64encode(pdf).decode(),  # client offers immediate download
        "emailed_to": recipients if email_status == "sent" else [],
        "email_status": email_status,
    }


# ============================================================================
# Admin endpoints (Derrick's back office). The service-role key stays here on
# the server; the admin page authenticates as Derrick via Supabase and calls
# these with his bearer token. Access is gated to an explicit allowlist of
# user ids so no other authenticated client can reach them.
# ============================================================================
ADMIN_USER_IDS = set(filter(None, os.environ.get("ADMIN_USER_IDS", "").split(",")))


def require_admin(authorization: Optional[str]) -> str:
    uid = verify_jwt(authorization)
    if uid not in ADMIN_USER_IDS:
        raise HTTPException(403, "Admin access required.")
    return uid


async def sb_get(table: str, params: dict) -> list:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_sb_headers(), params=params)
        r.raise_for_status()
        return r.json()


async def sb_patch(table: str, match: dict, patch: dict) -> list:
    params = {k: f"eq.{v}" for k, v in match.items()}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.patch(f"{SUPABASE_URL}/rest/v1/{table}",
                          headers={**_sb_headers(), "Prefer": "return=representation"},
                          params=params, json=patch)
        r.raise_for_status()
        return r.json()


class PolicyUpsert(BaseModel):
    id: Optional[str] = None
    client_id: str
    named_insured: str
    usdot: Optional[str] = None
    status: str = "active"
    self_serve_enabled: bool = True
    effective_date: str
    expiration_date: str
    producer_block: str
    insured_address: str
    coverages: dict = {}
    carriers: list = []
    data_current_as_of: str


class StatusPatch(BaseModel):
    status: Optional[str] = None
    self_serve_enabled: Optional[bool] = None
    data_current_as_of: Optional[str] = None


@app.get("/admin/policies")
async def admin_list_policies(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return await sb_get("policies", {"select": "*", "order": "named_insured"})


@app.post("/admin/policies")
async def admin_upsert_policy(body: PolicyUpsert, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    row = body.model_dump(exclude_none=True)
    if body.id:
        return (await sb_patch("policies", {"id": body.id}, row))[0]
    row.pop("id", None)
    return await sb_insert("policies", row)


@app.post("/admin/policies/{policy_id}/status")
async def admin_patch_status(policy_id: str, body: StatusPatch, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(400, "Nothing to update.")
    return (await sb_patch("policies", {"id": policy_id}, patch))[0]


@app.get("/admin/special-requests")
async def admin_special(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return await sb_get("special_requests", {"select": "*", "order": "created_at.desc"})


@app.post("/admin/special-requests/{req_id}/status")
async def admin_special_status(req_id: str, body: dict, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    status = body.get("status")
    if status not in ("open", "in_progress", "sent", "declined"):
        raise HTTPException(400, "Invalid status.")
    return (await sb_patch("special_requests", {"id": req_id}, {"status": status}))[0]


@app.get("/admin/audit")
async def admin_audit(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return await sb_get("audit_log", {"select": "*", "order": "occurred_at.desc", "limit": "200"})


@app.get("/admin/certificates")
async def admin_certs(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return await sb_get("issued_certificates", {"select": "*", "order": "issued_at.desc", "limit": "200"})


def _build_field_map(policy: dict, req: CertRequest, cert_number: str) -> dict:
    """Map stored data -> your ACORD 25 field names. Discover the names once
    with cert_generator.dump_field_names(your_template) and fill this in."""
    return {
        # "PRODUCER": policy["producer_block"],
        # "INSURED": f"{policy['named_insured']}\n{policy['insured_address']}",
        # "CERTIFICATE_HOLDER": f"{req.holder_name}\n{req.holder_address}",
        # "DESCRIPTION_OF_OPERATIONS": req.description_of_operations,
        # ...map each coverage line / limit / policy number / dates...
    }
