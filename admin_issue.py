"""
admin_issue.py — Owner (admin) issue-on-behalf. Phase 1.

Adds POST /admin/issue-certificate: lets an ADMIN_USER_IDS account issue a
PLAIN INFORMATIONAL ACORD 25 on behalf of any client, using the exact same
gate, generator, storage, email, and audit paths as client self-serve.

Phase 1 scope — deliberate:
  * Special wording (additional insured / waiver / P&NC / custom) is REJECTED
    with 422. Rendering + signature policy for special wording is Phase 2 and
    blocked on the E&O-approved wording spec. Nothing here loosens the gate.
  * The FULL gate still runs (status, effective/expiration dates, kill switch,
    required coverage, holder sanity). The owner path can issue nothing that
    the gate would not allow a client to self-issue on that policy.
  * Owner attribution is recorded via an OWNER_ISSUED_BY_<uid> audit code on
    the existing audit_log row — no schema change required for Phase 1.

Wire-up (end of main.py):
    import admin_issue
    admin_issue.register(app)
"""

import base64
import datetime as dt
from typing import Optional

from fastapi import Header, HTTPException


def register(app):
    """Attach the owner issue-on-behalf endpoint to the existing FastAPI app.

    Called at the END of main.py, so main's module globals are fully
    initialized. All helpers are looked up on main at request time, which
    also lets tests monkeypatch them exactly as they do for the client path.
    """
    import main as m  # late import: safe because register() runs after main's body

    @app.post("/admin/issue-certificate")
    async def admin_issue_certificate(
        body: m.IssueRequest,
        authorization: Optional[str] = Header(None),
    ):
        admin_id = m.require_admin(authorization)

        # ---- Phase 1: special wording is explicitly out of scope ----
        if body.requested_special_wording:
            raise HTTPException(
                422,
                detail=(
                    "Special-wording certificates (additional insured, waiver, "
                    "primary & non-contributory, custom language) cannot be "
                    "issued through owner issuance yet. Pending E&O-approved "
                    "wording spec (Phase 2)."
                ),
            )

        policy = await m.load_policy(body.policy_id)

        req = m.CertRequest(
            holder_name=body.holder_name,
            holder_address=body.holder_address,
            holder_email=body.holder_email,
            description_of_operations=body.description_of_operations,
            requested_special_wording=[],
        )

        # Full gate — owner-issued certs obey the same objective checks
        # (active status, in-date, kill switch, coverage present, holder sane).
        result = m.evaluate_gate(m._to_snapshot(policy), req)

        # Owner attribution in the audit trail, using existing columns.
        result.audit_codes.append(f"OWNER_ISSUED_BY_{admin_id}")
        await m.write_audit(policy["client_id"], policy["id"], result, body.holder_name)

        if result.route != "auto_issue":
            raise HTTPException(
                409,
                detail={
                    "status": result.route,
                    "message": result.reasons[0],
                    "owner_note": (
                        "The gate refused this issuance. Owner issuance does not "
                        "override policy status, dates, or the kill switch."
                    ),
                },
            )

        # ---- identical issuance pipeline to the client path ----
        cert_number = m._next_cert_number()
        content = m.CertContent(
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
            usdot=policy.get("usdot") or "",
            mc_number=policy.get("mc_number") or "",
            vehicles=policy.get("vehicles") or [],
            drivers=policy.get("drivers") or [],
            trailers=policy.get("trailers") or [],
        )
        pdf = m.generate_certificate(
            content,
            template_path=m.TEMPLATE_PATH,
            field_map=m._build_field_map(policy, req, cert_number) if m.TEMPLATE_PATH else None,
            signature_png_path=m.SIGNATURE_PATH,
        )

        pdf_path = None
        if m.SUPABASE_URL and m.SERVICE_KEY:
            pdf_path = await m.storage_upload(
                f"{policy['client_id']}/{cert_number}.pdf", pdf
            )

        await m.sb_insert("issued_certificates", {
            "cert_number": cert_number,
            "policy_id": policy["id"],
            "client_id": policy["client_id"],
            "holder_name": req.holder_name,
            "holder_address": req.holder_address,
            "holder_email": req.holder_email,
            "description_of_ops": req.description_of_operations,
            "coverage_snapshot": {
                "coverages": policy.get("coverages"),
                "carriers": policy.get("carriers"),
                "data_current_as_of": policy["data_current_as_of"],
                "owner_issued_by": admin_id,
            },
            "pdf_path": pdf_path,
        })

        recipients = [r for r in [req.holder_email] if r]
        email_status = "not_sent"
        if recipients and m.RESEND_KEY:
            try:
                await m.send_certificate_email(
                    recipients, cert_number, policy["named_insured"], pdf
                )
                email_status = "sent"
            except Exception as e:  # best-effort, same as client path
                email_status = f"failed: {type(e).__name__}"

        return {
            "status": "issued",
            "cert_number": cert_number,
            "pdf_base64": base64.b64encode(pdf).decode(),
            "emailed_to": recipients if email_status == "sent" else [],
            "email_status": email_status,
            "issued_for_client_id": policy["client_id"],
            "issued_by_owner": True,
        }

    return admin_issue_certificate
