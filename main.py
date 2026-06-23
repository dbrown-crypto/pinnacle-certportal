"""
gate.py — Pinnacle Risk Advisors self-serve certificate portal
The validation gate. This is the product.

A client-initiated certificate may ONLY be issued automatically when it is a
plain, informational ACORD 25 against currently-bound, in-force coverage.
Anything else routes to a human (Derrick). This module is the single source of
truth for that decision. It is deliberately dependency-free so it can be unit
tested in isolation and audited at a glance.

NOTHING in the frontend is a security control. Hiding the "Additional Insured"
checkbox is UX. THIS file is the control. The API must call evaluate_gate()
server-side on every issuance request and refuse to generate a certificate
unless it returns allow=True.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# --- Special-wording flags ----------------------------------------------------
# If a holder's contract requires ANY of these, the certificate is no longer a
# plain informational cert. It makes a representation about coverage behavior
# that the gate cannot verify is actually endorsed onto the policy, so it must
# go to a human. These are the flags the client form will surface as a single
# "this holder requires special wording" path — never as auto-issuable options.
class SpecialWording(str, Enum):
    ADDITIONAL_INSURED = "additional_insured"
    WAIVER_OF_SUBROGATION = "waiver_of_subrogation"
    PRIMARY_NON_CONTRIBUTORY = "primary_non_contributory"
    SPECIAL_LANGUAGE = "special_language"  # any free-text wording request


class PolicyStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    PENDING = "pending"
    EXPIRED = "expired"  # may be set explicitly; also derived from dates


# Coverage lines that MUST carry a non-zero limit for a trucking COI to be
# meaningful. A cert that shows blank Auto Liability is worse than no cert.
REQUIRED_COVERAGE_LINES = ("auto_liability",)


@dataclass
class PolicySnapshot:
    """The policy row as stored in Supabase, passed in by the API after it has
    loaded the record the authenticated client is actually entitled to (RLS
    guarantees they can only load their own)."""
    policy_id: str
    named_insured: str
    status: PolicyStatus
    effective_date: date
    expiration_date: date
    self_serve_enabled: bool
    coverages: dict[str, Optional[float]] = field(default_factory=dict)  # line -> limit
    data_current_as_of: Optional[date] = None


@dataclass
class CertRequest:
    """What the client submitted from the Add Certificate Holder form."""
    holder_name: str
    holder_address: str
    holder_email: Optional[str]
    description_of_operations: str = ""
    requested_special_wording: list[SpecialWording] = field(default_factory=list)


@dataclass
class GateResult:
    allow: bool
    route: str               # "auto_issue" | "route_to_agent" | "block"
    reasons: list[str]       # human-readable, safe to show client and to log
    audit_codes: list[str]   # machine codes for the audit trail


def evaluate_gate(
    policy: PolicySnapshot,
    request: CertRequest,
    today: Optional[date] = None,
) -> GateResult:
    """Decide what happens to a certificate request. Pure function: no I/O.

    Returns one of three routes:
      - auto_issue       -> safe to generate, sign, send, log
      - route_to_agent   -> create a request task for Derrick; issue NOTHING
      - block            -> tell client to contact the agency; issue NOTHING
    """
    today = today or date.today()
    reasons: list[str] = []
    audit: list[str] = []

    # 1. Special wording is a hard route-to-agent, checked first. Even on a
    #    cancelled policy we route rather than block, so Derrick sees the intent.
    if request.requested_special_wording:
        names = ", ".join(w.value for w in request.requested_special_wording)
        reasons.append(
            "This certificate requires special wording "
            f"({names}) and will be reviewed and sent by our office."
        )
        audit.append("ROUTE_SPECIAL_WORDING")
        return GateResult(False, "route_to_agent", reasons, audit)

    # 2. Self-serve switched off for this policy (Derrick's per-client kill
    #    switch). Hard block — something is wrong with the account.
    if not policy.self_serve_enabled:
        reasons.append("Self-service is unavailable on this policy. Please contact our office.")
        audit.append("BLOCK_SELF_SERVE_DISABLED")
        return GateResult(False, "block", reasons, audit)

    # 3. Status must be active.
    if policy.status != PolicyStatus.ACTIVE:
        reasons.append("This policy is not currently active. Please contact our office.")
        audit.append(f"BLOCK_STATUS_{policy.status.value.upper()}")
        return GateResult(False, "block", reasons, audit)

    # 4. Date window — the quiet save. Catches the most common staleness case
    #    (an expired policy whose status flag was never updated) automatically.
    if today < policy.effective_date:
        reasons.append("This policy has not taken effect yet. Please contact our office.")
        audit.append("BLOCK_NOT_YET_EFFECTIVE")
        return GateResult(False, "block", reasons, audit)
    if today > policy.expiration_date:
        reasons.append("This policy has expired. Please contact our office to renew.")
        audit.append("BLOCK_EXPIRED_BY_DATE")
        return GateResult(False, "block", reasons, audit)

    # 5. Required coverage data present and non-zero.
    missing = [
        line for line in REQUIRED_COVERAGE_LINES
        if not policy.coverages.get(line)
    ]
    if missing:
        reasons.append("Coverage details are incomplete on this policy. Please contact our office.")
        audit.append("BLOCK_MISSING_COVERAGE_" + "_".join(m.upper() for m in missing))
        return GateResult(False, "block", reasons, audit)

    # 6. Basic holder sanity — never issue a cert to a blank holder.
    if not request.holder_name.strip() or not request.holder_address.strip():
        reasons.append("Certificate holder name and address are required.")
        audit.append("BLOCK_INCOMPLETE_HOLDER")
        return GateResult(False, "block", reasons, audit)

    # All gates passed: plain informational cert on verified-active, in-date
    # coverage. This — and only this — is what the pre-applied authorized-rep
    # signature is authorized to sign.
    reasons.append("Standard certificate issued.")
    audit.append("AUTO_ISSUE_OK")
    audit.append(f"COVERAGE_SNAPSHOT_ASOF_{(policy.data_current_as_of or today).isoformat()}")
    return GateResult(True, "auto_issue", reasons, audit)
