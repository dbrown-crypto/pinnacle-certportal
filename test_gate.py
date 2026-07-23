"""
test_gate.py — Pinnacle Risk Advisors self-serve certificate gate.

The gate (gate.py) is the single server-side control that decides whether a
client-initiated certificate may be auto-issued, must route to a human, or must
be blocked. These tests pin that behavior so it cannot silently regress.

Dependency-free. Run:  python test_gate.py   (exit 0 = all pass)
"""

from datetime import date

from gate import (
    evaluate_gate,
    PolicySnapshot,
    CertRequest,
    PolicyStatus,
    SpecialWording,
)

TODAY = date(2026, 7, 1)


def _policy(**over):
    base = dict(
        policy_id="p1",
        named_insured="Acme Trucking LLC",
        status=PolicyStatus.ACTIVE,
        effective_date=date(2026, 1, 1),
        expiration_date=date(2026, 12, 31),
        self_serve_enabled=True,
        coverages={"auto_liability": 1_000_000.0},
        data_current_as_of=date(2026, 6, 30),
    )
    base.update(over)
    return PolicySnapshot(**base)


def _request(**over):
    base = dict(
        holder_name="Shipper Co",
        holder_address="100 Dock St, Atlanta, GA 30303",
        holder_email="ap@shipper.example",
        description_of_operations="",
        requested_special_wording=[],
    )
    base.update(over)
    return CertRequest(**base)


# (name, policy, request, expected_route, expected_allow, must_contain_audit_code)
CASES = [
    ("plain valid -> auto_issue",
     _policy(), _request(), "auto_issue", True, "AUTO_ISSUE_OK"),

    ("additional insured -> route",
     _policy(), _request(requested_special_wording=[SpecialWording.ADDITIONAL_INSURED]),
     "route_to_agent", False, "ROUTE_SPECIAL_WORDING"),

    ("waiver of subrogation -> route",
     _policy(), _request(requested_special_wording=[SpecialWording.WAIVER_OF_SUBROGATION]),
     "route_to_agent", False, "ROUTE_SPECIAL_WORDING"),

    ("primary & non-contributory -> route",
     _policy(), _request(requested_special_wording=[SpecialWording.PRIMARY_NON_CONTRIBUTORY]),
     "route_to_agent", False, "ROUTE_SPECIAL_WORDING"),

    ("free-text special language -> route",
     _policy(), _request(requested_special_wording=[SpecialWording.SPECIAL_LANGUAGE]),
     "route_to_agent", False, "ROUTE_SPECIAL_WORDING"),

    ("special wording beats a cancelled policy -> route (Derrick sees intent)",
     _policy(status=PolicyStatus.CANCELLED),
     _request(requested_special_wording=[SpecialWording.ADDITIONAL_INSURED]),
     "route_to_agent", False, "ROUTE_SPECIAL_WORDING"),

    ("kill switch off -> block",
     _policy(self_serve_enabled=False), _request(), "block", False, "BLOCK_SELF_SERVE_DISABLED"),

    ("status cancelled -> block",
     _policy(status=PolicyStatus.CANCELLED), _request(), "block", False, "BLOCK_STATUS_CANCELLED"),

    ("status pending -> block",
     _policy(status=PolicyStatus.PENDING), _request(), "block", False, "BLOCK_STATUS_PENDING"),

    ("status expired flag -> block",
     _policy(status=PolicyStatus.EXPIRED), _request(), "block", False, "BLOCK_STATUS_EXPIRED"),

    ("not yet effective -> block",
     _policy(effective_date=date(2026, 8, 1)), _request(), "block", False, "BLOCK_NOT_YET_EFFECTIVE"),

    ("expired by date (status stale) -> block",
     _policy(expiration_date=date(2026, 6, 30)), _request(), "block", False, "BLOCK_EXPIRED_BY_DATE"),

    ("missing auto liability -> block",
     _policy(coverages={}), _request(), "block", False, "BLOCK_MISSING_COVERAGE_AUTO_LIABILITY"),

    ("zero auto liability limit -> block",
     _policy(coverages={"auto_liability": 0}), _request(), "block", False, "BLOCK_MISSING_COVERAGE_AUTO_LIABILITY"),

    ("blank holder name -> block",
     _policy(), _request(holder_name="   "), "block", False, "BLOCK_INCOMPLETE_HOLDER"),

    ("blank holder address -> block",
     _policy(), _request(holder_address=""), "block", False, "BLOCK_INCOMPLETE_HOLDER"),

    ("boundary: today == effective_date -> auto_issue",
     _policy(effective_date=TODAY), _request(), "auto_issue", True, "AUTO_ISSUE_OK"),

    ("boundary: today == expiration_date -> auto_issue",
     _policy(expiration_date=TODAY), _request(), "auto_issue", True, "AUTO_ISSUE_OK"),
]


def run():
    passed = failed = 0
    for name, policy, request, exp_route, exp_allow, exp_code in CASES:
        res = evaluate_gate(policy, request, today=TODAY)
        ok = (res.route == exp_route and res.allow == exp_allow and exp_code in res.audit_codes)
        if ok:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}")
            print(f"        got route={res.route!r} allow={res.allow} audit={res.audit_codes}")
            print(f"        want route={exp_route!r} allow={exp_allow} code~{exp_code}")

    # extra invariant: auto_issue stamps a coverage-as-of date for the audit trail
    res = evaluate_gate(_policy(data_current_as_of=date(2026, 6, 30)), _request(), today=TODAY)
    if any(c.startswith("COVERAGE_SNAPSHOT_ASOF_2026-06-30") for c in res.audit_codes):
        passed += 1
        print("  PASS  auto_issue stamps data_current_as_of in audit")
    else:
        failed += 1
        print("  FAIL  auto_issue stamps data_current_as_of in audit")

    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
