"""
admin_issue.py — Owner (admin) issue-on-behalf. Phase 1 + Phase 2.

Phase 1: POST /admin/issue-certificate — owner issues a plain informational
ACORD 25 for any client's policy (full gate enforced).

Phase 2 (E&O carrier cleared; wording text approved by Derrick Brown):
  * The same endpoint now accepts special wording (additional insured, waiver
    of subrogation, primary & non-contributory, custom language) ONLY when:
      - the caller is on ADMIN_USER_IDS,
      - the policy passes the same objective gate as any client issuance
        (active, in-date, kill switch, coverage present),
      - every requested wording is flagged on the policy's `endorsements`
        (custom language excepted — it requires explicit custom text),
      - the owner explicitly set confirm_wording=true after seeing the final
        text (UI enforces the preview; server enforces the confirmation).
  * Signature is applied ONLY when apply_signature=true (per-cert decision,
    default off). Logged as SIGNATURE_APPLIED_BY_OWNER.
  * The approved clause templates below are the ONLY generated wording.
  * Audit: OWNER_ISSUED_BY_<uid> + AUTHORIZED_WORDING_<KIND> codes, and the
    full final wording stored in the certificate's coverage_snapshot.
  * Fulfilling a queued client special_request marks it status=sent.
  * POST /admin/policies/{id}/endorsements — owner keys per-policy endorsement
    flags (the enforcement source of truth).

Wire-up (end of main.py):  import admin_issue ; admin_issue.register(app)
"""

import base64
import datetime as dt
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from gate import SpecialWording

# --- Approved wording templates (Derrick Brown, licensed agent) --------------
WORDING_TEMPLATES = {
    "additional_insured": (
        "The certificate holder is included as an additional insured under the "
        "{line} policy, but only with respect to operations of the named insured, "
        "only where required by written contract, and subject to the terms, "
        "conditions, and exclusions of the policy{form_clause}."
    ),
    "waiver_of_subrogation": (
        "A waiver of subrogation applies in favor of the certificate holder under "
        "the {line} policy, but only where required by written contract and as "
        "permitted by{form_clause2}."
    ),
    "primary_non_contributory": (
        "Coverage afforded to the certificate holder under the {line} policy is "
        "primary and non-contributory, but only where required by written contract "
        "and per{form_clause2}."
    ),
}
FLAGGABLE = set(WORDING_TEMPLATES)  # custom (special_language) handled separately


def _render_clause(kind: str, line: str, form: Optional[str]) -> str:
    t = WORDING_TEMPLATES[kind]
    fc = f" and endorsement {form}" if form else ""
    fc2 = f" endorsement {form}" if form else " the applicable endorsement"
    return t.format(line=line, form_clause=fc, form_clause2=fc2)


class AdminIssueRequest(BaseModel):
    policy_id: str
    holder_name: str
    holder_address: str
    holder_email: Optional[str] = None
    description_of_operations: str = ""
    requested_special_wording: list[SpecialWording] = Field(default_factory=list)
    wording_line: str = "General Liability"
    custom_wording: Optional[str] = None
    confirm_wording: bool = False
    apply_signature: bool = False
    special_request_id: Optional[str] = None


class EndorsementsPatch(BaseModel):
    endorsements: dict


def register(app):
    import main as m  # late import: register() runs after main's module body

    @app.post("/admin/issue-certificate")
    async def admin_issue_certificate(
        body: AdminIssueRequest,
        authorization: Optional[str] = Header(None),
    ):
        admin_id = m.require_admin(authorization)
        wording = list(dict.fromkeys(body.requested_special_wording))
        unknown = [w for w in wording if w not in FLAGGABLE and w != "special_language"]
        if unknown:
            raise HTTPException(422, f"Unknown wording option(s): {', '.join(unknown)}")

        policy = await m.load_policy(body.policy_id)

        # --- Phase 2 enforcement: wording must be flagged on the policy ------
        clauses: list[str] = []
        if wording:
            if not body.confirm_wording:
                raise HTTPException(
                    400, "Special wording requires explicit owner confirmation "
                         "(confirm_wording) after reviewing the final text.")
            endo = policy.get("endorsements") or {}
            missing = [w for w in wording if w in FLAGGABLE and w not in endo]
            if missing:
                raise HTTPException(409, detail={
                    "status": "refused",
                    "message": "Policy is not flagged as carrying: "
                               + ", ".join(missing)
                               + ". Set the policy's endorsement flags first "
                                 "(or the policy does not carry this endorsement).",
                })
            for w in wording:
                if w in FLAGGABLE:
                    clauses.append(_render_clause(
                        w, body.wording_line, (endo.get(w) or {}).get("form")))
            if "special_language" in wording:
                if not (body.custom_wording or "").strip():
                    raise HTTPException(400, "Custom wording selected but no text provided.")
                clauses.append(body.custom_wording.strip())

        req = m.CertRequest(
            holder_name=body.holder_name,
            holder_address=body.holder_address,
            holder_email=body.holder_email,
            description_of_operations=body.description_of_operations,
            requested_special_wording=[],
        )
        # Objective gate — identical checks to client issuance; never overridden.
        result = m.evaluate_gate(m._to_snapshot(policy), req)

        result.audit_codes.append(f"OWNER_ISSUED_BY_{admin_id}")
        for w in wording:
            result.audit_codes.append("AUTHORIZED_WORDING_" + w.upper())
        if wording and body.apply_signature:
            result.audit_codes.append("SIGNATURE_APPLIED_BY_OWNER")
        await m.write_audit(policy["client_id"], policy["id"], result, body.holder_name)

        if result.route != "auto_issue":
            raise HTTPException(409, detail={
                "status": result.route, "message": result.reasons[0],
                "owner_note": "Owner issuance does not override policy status, "
                              "dates, or the kill switch.",
            })

        final_ops = body.description_of_operations.strip()
        if clauses:
            final_ops = (final_ops + "\n\n" if final_ops else "") + "\n\n".join(clauses)

        cert_number = m._next_cert_number()
        content = m.CertContent(
            cert_number=cert_number,
            issue_date=dt.date.today(),
            producer_block=policy["producer_block"],
            insured_name=policy["named_insured"],
            insured_address=policy["insured_address"],
            holder_name=req.holder_name,
            holder_address=req.holder_address,
            description_of_operations=final_ops,
            coverages=policy.get("carriers") or [],
            data_current_as_of=dt.date.fromisoformat(policy["data_current_as_of"]),
            usdot=policy.get("usdot") or "",
            mc_number=policy.get("mc_number") or "",
            vehicles=policy.get("vehicles") or [],
            drivers=policy.get("drivers") or [],
            trailers=policy.get("trailers") or [],
        )
        # Signature: standard certs keep existing behavior; special-wording
        # certs apply it only on the owner's per-cert choice.
        sig_path = m.SIGNATURE_PATH if (not wording or body.apply_signature) else None
        pdf = m.generate_certificate(
            content,
            template_path=m.TEMPLATE_PATH,
            field_map=m._build_field_map(policy, req, cert_number) if m.TEMPLATE_PATH else None,
            signature_png_path=sig_path,
        )

        pdf_path = None
        if m.SUPABASE_URL and m.SERVICE_KEY:
            pdf_path = await m.storage_upload(f"{policy['client_id']}/{cert_number}.pdf", pdf)

        await m.sb_insert("issued_certificates", {
            "cert_number": cert_number, "policy_id": policy["id"],
            "client_id": policy["client_id"],
            "holder_name": req.holder_name, "holder_address": req.holder_address,
            "holder_email": req.holder_email, "description_of_ops": final_ops,
            "coverage_snapshot": {
                "coverages": policy.get("coverages"),
                "carriers": policy.get("carriers"),
                "data_current_as_of": policy["data_current_as_of"],
                "owner_issued_by": admin_id,
                "authorized_wording": clauses,
                "wording_kinds": wording,
                "signature_authorized": bool(wording and body.apply_signature),
            },
            "pdf_path": pdf_path,
        })

        if body.special_request_id:
            try:
                await m.sb_patch("special_requests", {"id": body.special_request_id},
                                 {"status": "sent"})
            except Exception:
                pass  # cert is issued; request-status is bookkeeping

        recipients = [r for r in [req.holder_email] if r]
        email_status = "not_sent"
        if recipients and m.RESEND_KEY:
            try:
                await m.send_certificate_email(recipients, cert_number,
                                               policy["named_insured"], pdf)
                email_status = "sent"
            except Exception as e:
                email_status = f"failed: {type(e).__name__}"

        return {
            "status": "issued", "cert_number": cert_number,
            "pdf_base64": base64.b64encode(pdf).decode(),
            "emailed_to": recipients if email_status == "sent" else [],
            "email_status": email_status,
            "issued_for_client_id": policy["client_id"],
            "issued_by_owner": True,
            "wording_applied": clauses,
            "signature_applied": bool(wording and body.apply_signature),
        }

    @app.post("/admin/policies/{policy_id}/endorsements")
    async def admin_set_endorsements(
        policy_id: str, body: EndorsementsPatch,
        authorization: Optional[str] = Header(None),
    ):
        m.require_admin(authorization)
        rows = await m.sb_patch("policies", {"id": policy_id},
                                {"endorsements": body.endorsements})
        if not rows:
            raise HTTPException(404, "Policy not found.")
        return rows[0]

    return admin_issue_certificate
