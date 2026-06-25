"""
acord25_cargo_fix.py
--------------------------------------------------------------------------
Drop-in helpers for acord25_2016_overlay.py addressing the MOTOR TRUCK CARGO
(OTHER-row) limit misalignment, plus carrier-field validation.

WHY THIS EXISTS
    On the rendered test cert (PRA-20260625045915) the cargo limit/deductible
    text was placed using the Workers-Comp two-column limit coordinates. On the
    free-form OTHER row that put:
        - the deductible's leading "$" ON TOP of the LIMITS column divider, and
        - "$100,000" hard against (slightly over) the form's right border.

COORDINATE SYSTEM
    ReportLab canvas: origin is BOTTOM-LEFT, units are points (1/72").
    Page is US Letter, 612 x 792 pt. All x/y below are in points.

    Measured off the 300-DPI render of the live form (px / (300/72) = pt):
        RIGHT_BORDER_X      = 593.8   # inner right edge of the coverage grid
        LIMITS_DIVIDER_X    = 514.8   # vertical line splitting label | $ value
        POLICY_EXP_RIGHT_X  = 424.8   # right edge of the POLICY EXP column
    The usable "clean" value zone on the OTHER row is therefore the band to the
    RIGHT of the divider and INSIDE the border. We right-align into it.

THE FIX
    Right-align both values to RIGHT_ALIGN_X (588 pt -> ~6 pt inside the border)
    on two separate baselines (limit above, deductible below), vertically
    centered in the row. Guard rails assert nothing renders left of the divider
    or past the border before the page is saved, so a future coordinate edit
    can't silently reintroduce the collision.
"""

from __future__ import annotations

# --- Measured form geometry (points, bottom-left origin) ---------------------
RIGHT_BORDER_X = 593.8      # inner right edge of the grid
LIMITS_DIVIDER_X = 514.8    # label | $value divider running down the LIMITS col
RIGHT_ALIGN_X = 588.0       # right-align target: ~6 pt of margin inside border
MIN_CLEAR_X = 516.0         # nothing should start left of this on the OTHER row

# Known-correct NAIC codes for Pinnacle's two core carriers. Used as a sanity
# guard ONLY (warn on mismatch) -- the authoritative value is the dec page.
KNOWN_NAIC = {
    "progressive mountain": "35190",       # NOT 41580
    "canal insurance": "10464",            # NOT 11142
    "canal insurance company": "10464",
}


def expand_year(date_str: str) -> str:
    """Expand a 2-digit year to 4 digits for display ('01/01/26' -> '01/01/2026').

    Leaves already-4-digit years untouched. Raises ValueError on a date that
    isn't MM/DD/YY or MM/DD/YYYY so a malformed value fails loudly, not silently.
    """
    if not isinstance(date_str, str):
        raise ValueError(f"date must be a string, got {type(date_str).__name__}")
    parts = date_str.strip().split("/")
    if len(parts) != 3:
        raise ValueError(f"date '{date_str}' is not MM/DD/YY or MM/DD/YYYY")
    mm, dd, yy = (p.strip() for p in parts)
    if len(yy) == 2 and yy.isdigit():
        yy = f"20{yy}"            # 2-digit -> 20xx (certs are current-century)
    elif len(yy) != 4 or not yy.isdigit():
        raise ValueError(f"date '{date_str}' has a bad year segment '{yy}'")
    return f"{mm}/{dd}/{yy}"


def validate_carrier(carrier: dict, *, index: int | None = None) -> list[str]:
    """Validate one carrier object. Returns a list of human-readable problems.

    Identifies WHICH carrier and WHICH field failed so an upstream caller can
    surface a clear message instead of a stack trace. An empty list == valid.
    Non-fatal NAIC mismatches are returned as 'WARN:' prefixed strings so the
    caller can decide whether to block or just log.
    """
    who = carrier.get("carrier") or carrier.get("line") or (
        f"carrier[{index}]" if index is not None else "carrier")
    problems: list[str] = []

    # Required scalar fields
    for key in ("naic", "eff", "exp"):
        if not carrier.get(key):
            problems.append(f"{who}: missing required field '{key}'")

    # NAIC format: 3-5 digit code
    naic = str(carrier.get("naic", "")).strip()
    if naic and not (naic.isdigit() and 3 <= len(naic) <= 5):
        problems.append(f"{who}: NAIC '{naic}' is not a 3-5 digit code")

    # NAIC sanity vs known carriers (warn, don't hard-fail -- dec page wins)
    name_key = str(carrier.get("carrier", "")).strip().lower()
    if name_key in KNOWN_NAIC and naic and naic != KNOWN_NAIC[name_key]:
        problems.append(
            f"WARN: {who}: NAIC '{naic}' != expected '{KNOWN_NAIC[name_key]}' "
            f"for '{carrier.get('carrier')}'. Confirm against the dec page."
        )

    # Dates must expand cleanly
    for key in ("eff", "exp"):
        if carrier.get(key):
            try:
                expand_year(str(carrier[key]))
            except ValueError as e:
                problems.append(f"{who}: {key} {e}")

    # autos, if present, must be a list of recognized box names
    valid_autos = {"ANY", "OWNED", "SCHEDULED", "HIRED", "NON-OWNED"}
    autos = carrier.get("autos")
    if autos is not None:
        if not isinstance(autos, list):
            problems.append(f"{who}: 'autos' must be a list, got {type(autos).__name__}")
        else:
            bad = [a for a in autos if a not in valid_autos]
            if bad:
                problems.append(
                    f"{who}: unrecognized autos {bad}; allowed: {sorted(valid_autos)}")

    return problems


def draw_other_row_limits(c, limit, deductible=None, *, row_center_y,
                          font="Helvetica", size=8):
    """Render the OTHER/cargo-row limit + deductible, cleanly right-aligned.

    Parameters
    ----------
    c            : reportlab canvas (the overlay canvas you draw the page on)
    limit        : the coverage limit, e.g. 100000 or "$100,000"
    deductible   : optional deductible, e.g. 1000 or "$1,000"
    row_center_y : vertical center of the OTHER row in points (bottom-origin).
                   Measured on the current form at ~257 pt; confirm against your
                   own row geometry and pass it in rather than hard-coding.

    Layout: limit on the upper baseline, deductible ~9 pt below, both
    right-aligned to RIGHT_ALIGN_X. Asserts the rendered strings clear the
    LIMITS divider and stay inside the border before drawing.
    """
    def _money(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return f"${v:,.0f}"
        s = str(v).strip()
        return s if s.startswith("$") else f"${s}"

    limit_s = _money(limit)
    ded_s = f"{_money(deductible)} Ded" if deductible is not None else None

    c.setFont(font, size)

    # Guard rail: a right-aligned string of width W ends at RIGHT_ALIGN_X and
    # starts at RIGHT_ALIGN_X - W. That start must clear the divider.
    for label, text in (("limit", limit_s), ("deductible", ded_s)):
        if not text:
            continue
        w = c.stringWidth(text, font, size)
        start_x = RIGHT_ALIGN_X - w
        if start_x < MIN_CLEAR_X:
            raise ValueError(
                f"OTHER-row {label} '{text}' is {w:.1f} pt wide; right-aligned "
                f"at {RIGHT_ALIGN_X} it starts at {start_x:.1f} pt, crossing the "
                f"LIMITS divider ({LIMITS_DIVIDER_X}). Shorten the text or widen "
                f"the band (drop the divider for the free-form OTHER row)."
            )
        if RIGHT_ALIGN_X > RIGHT_BORDER_X:
            raise ValueError("RIGHT_ALIGN_X is past the form's right border")

    # Two baselines, vertically centered around row_center_y.
    if limit_s and ded_s:
        c.drawRightString(RIGHT_ALIGN_X, row_center_y + 5, limit_s)
        c.drawRightString(RIGHT_ALIGN_X, row_center_y - 6, ded_s)
    elif limit_s:
        c.drawRightString(RIGHT_ALIGN_X, row_center_y - 2, limit_s)


# --- Integration notes -------------------------------------------------------
# In acord25_2016_overlay.py, where the cargo / OTHER row currently writes its
# limit text, replace that block with a single call:
#
#     draw_other_row_limits(
#         canvas,
#         limit=carrier["limits"]["cargo_limit"],     # e.g. 100000
#         deductible=carrier["limits"].get("deductible"),  # e.g. 1000
#         row_center_y=OTHER_ROW_CENTER_Y,            # ~257 pt; use your real value
#     )
#
# And before generating any page, validate every carrier and stop on hard errors:
#
#     issues = []
#     for i, cx in enumerate(policy["carriers"]):
#         issues += validate_carrier(cx, index=i)
#     hard = [m for m in issues if not m.startswith("WARN:")]
#     if hard:
#         raise ValueError("Carrier validation failed:\n  - " + "\n  - ".join(hard))
#     for w in (m for m in issues if m.startswith("WARN:")):
#         log.warning(w)   # NAIC mismatches surface here, don't silently ship
