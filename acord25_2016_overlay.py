"""
acord25_2016_overlay.py — Pinnacle Risk Advisors

Fills the licensed ACORD 25 (2016/03) by overlay-and-flatten, plus an optional
ACORD 101 (2008/01) Additional Remarks Schedule (trailers + drivers). Returns a
1- or 2-page flattened PDF.

The 2016/03 ACORD 25 has no AcroForm fields (overlay needed). The ACORD 101 IS
an AcroForm, so it's widget-filled then flattened.

Calibrated to the 2016/03 label coordinates. Trucking additions vs. the base
cert: DOT#/MC# under the insured, a MOTOR TRUCK CARGO row with deductible, and
the ACORD 101 page.
"""
from __future__ import annotations

import io
from typing import Optional

import fitz  # PyMuPDF

FONT = "helv"
SIZE = 7.0
SIZE_BLOCK = 8.0
INK = (0.0, 0.0, 0.0)

# ACORD 25 (2016/03) coordinates — PDF points, y from TOP. Page 612 x 792.
C25 = {
    "date":          (512, 44),

    "producer_l1":   (24, 134), "producer_l2": (24, 143), "producer_l3": (24, 152),
    "producer_l4":   (24, 161), "producer_l5": (24, 170),

    "contact_name":  (400, 127), "contact_phone": (400, 140), "contact_email": (400, 153),

    "insurer_a":     (345, 179), "insurer_b": (345, 191), "insurer_c": (345, 203),
    "naic_a":        (560, 179), "naic_b": (560, 191), "naic_c": (560, 203),

    "insured_l1":    (24, 195), "insured_l2": (24, 204), "insured_l3": (24, 213),
    "dot_mc":        (40, 235),          # DOT# / MC# line under insured

    "cert_number":   (305, 249),

    # AUTOMOBILE LIABILITY band runs y384-444; policy/dates centered at y414.
    # CSL value belongs in the COMBINED SINGLE LIMIT sub-row at the top (y392).
    "auto_ltr":      (16, 414),
    "auto_policy":   (250, 414),
    "auto_eff":      (337, 414),
    "auto_exp":      (384, 414),
    "auto_csl":      (573, 393),

    # MOTOR TRUCK CARGO band runs y528-564; data centered at y546
    "cargo_ltr":     (16, 546),
    "cargo_label":   (54, 546),
    "cargo_policy":  (250, 546),
    "cargo_eff":     (337, 546),
    "cargo_exp":     (384, 546),
    "cargo_ded":     (452, 546),
    "cargo_limit":   (560, 546),

    # DESCRIPTION OF OPERATIONS (label y566.6)
    "desc1": (24, 580), "desc2": (24, 589), "desc3": (24, 598),
    "desc4": (24, 607), "desc5": (24, 616),

    "holder_l1": (24, 668), "holder_l2": (24, 677), "holder_l3": (24, 686),
    "auth_rep":  (405, 706),
}


def _put_right(page, x_right, y, text, size=SIZE):
    """Right-align text so it ends at x_right (keeps limit values off the border)."""
    if text is None or str(text).strip() == "":
        return
    w = fitz.get_text_length(str(text), fontname=FONT, fontsize=size)
    page.insert_text((x_right - w, y), str(text), fontname=FONT, fontsize=size, color=INK)


def _put_center(page, x_center, y, text, size=SIZE):
    """Center text horizontally on x_center."""
    if text is None or str(text).strip() == "":
        return
    w = fitz.get_text_length(str(text), fontname=FONT, fontsize=size)
    page.insert_text((x_center - w / 2, y), str(text), fontname=FONT, fontsize=size, color=INK)


# Coverage-table column centers (measured from the grid lines)
COL = {"ltr": 27.0, "policy": 271.8, "eff": 354.6, "exp": 401.4}
LIMIT_RIGHT = 592.0          # right edge for all limit values
# Vertical center of each coverage band (from the horizontal grid lines)
BAND_Y = {"gl": 342.0, "auto": 414.0, "cargo": 546.0}
# General-liability limit sub-rows: (display, value-baseline y, accepted keys)
GL_LIMITS = [
    ("EACH OCCURRENCE", 311.2, ["each_occurrence", "EACH OCCURRENCE", "each occurrence", "occurrence"]),
    ("DAMAGE TO RENTED", 323.2, ["damage_rented", "DAMAGE TO RENTED PREMISES", "rented premises", "damage"]),
    ("MED EXP", 335.2, ["med_exp", "MED EXP", "med exp"]),
    ("PERSONAL & ADV", 347.2, ["personal_adv", "PERSONAL & ADV INJURY", "personal", "adv injury"]),
    ("GENERAL AGGREGATE", 359.2, ["general_aggregate", "GENERAL AGGREGATE", "aggregate"]),
    ("PRODUCTS COMP/OP", 371.2, ["products_comp_op", "PRODUCTS COMP/OP AGG", "products", "comp/op"]),
]


def _fill_policy_row(page, y, letter, policy, eff, exp):
    """Place INSR LTR, policy number, eff and exp centered in their columns at row y."""
    _put_center(page, COL["ltr"], y, letter)
    _put_center(page, COL["policy"], y, policy)
    _put_center(page, COL["eff"], y, _date4(eff))
    _put_center(page, COL["exp"], y, _date4(exp))


def _date4(s):
    """Expand a 2-digit year to 4 digits: 01/01/26 -> 01/01/2026. Leaves other formats alone."""
    if not s:
        return s
    parts = str(s).split("/")
    if len(parts) == 3 and len(parts[2]) == 2 and parts[2].isdigit():
        parts[2] = "20" + parts[2]
        return "/".join(parts)
    return str(s)


# Checkbox centers (x, y) measured from the form grid
CHECKBOX = {
    "gl_occur":    (123.0, 318.5),
    "agg_policy":  (40.5, 365.8),
    "agg_project": (84.0, 365.8),
    "agg_loc":     (126.5, 365.8),
    "any_auto":    (40.5, 400.8),
    "owned":       (40.5, 414.0),
    "hired":       (40.5, 427.0),
    "scheduled":   (107.0, 414.0),
    "non_owned":   (107.0, 427.0),
}


def _check(page, key):
    cx, cy = CHECKBOX[key]
    _put_center(page, cx, cy + 3.0, "X", size=8)


def _put(page, coords, key, text, size=SIZE):
    if text is None or str(text).strip() == "":
        return
    x, y = coords[key]
    page.insert_text((x, y), str(text), fontname=FONT, fontsize=size, color=INK)


def _lines(block, n):
    if not block:
        return []
    return [p.strip() for p in str(block).split("\n") if p.strip()][:n]


def _money(v):
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def fill_acord25_2016(content, blank_25_path, signature_png_path=None, signature_rect=None):
    """content is a CertContent-like object. Trucking extras read from optional
    attributes if present: usdot, mc_number, drivers, trailers. Carrier rows may
    carry a `deductible` and `naic`."""
    doc = fitz.open(blank_25_path)
    page = doc[0]
    C = C25

    _put(page, C, "date", content.issue_date.strftime("%m/%d/%Y"))
    _put(page, C, "cert_number", content.cert_number, SIZE_BLOCK)

    plines = _lines(content.producer_block, 5)
    for i, ln in enumerate(plines):
        _put(page, C, f"producer_l{i+1}", ln, SIZE_BLOCK if i == 0 else SIZE)

    import re
    email = next((w for ln in plines for w in ln.replace(",", " ").split() if "@" in w), None)
    phone = None
    for ln in plines:
        m = re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", ln)
        if m:
            phone = m.group(0); break
    _put(page, C, "contact_name", "Derrick Brown")
    _put(page, C, "contact_phone", phone)
    _put(page, C, "contact_email", email)

    _put(page, C, "insured_l1", content.insured_name, SIZE_BLOCK)
    for i, ln in enumerate(_lines(content.insured_address, 2)):
        _put(page, C, f"insured_l{i+2}", ln)

    # DOT# / MC#
    usdot = getattr(content, "usdot", None)
    mc = getattr(content, "mc_number", None)
    dot_mc = "  ".join(x for x in [f"DOT# {usdot}" if usdot else "", f"MC# {mc}" if mc else ""] if x)
    _put(page, C, "dot_mc", dot_mc)

    carriers = content.coverages or []
    insurer_order = []
    for c in carriers:
        if c.get("carrier") and c["carrier"] not in insurer_order:
            insurer_order.append(c["carrier"])
    letter_for = {n: chr(ord("A") + i) for i, n in enumerate(insurer_order[:3])}
    for name, letter in letter_for.items():
        k = letter.lower()
        _put(page, C, f"insurer_{k}", name)
        naic = next((c.get("naic") for c in carriers if c.get("carrier") == name and c.get("naic")), None)
        _put(page, C, f"naic_{k}", naic)

    # ---- COMMERCIAL GENERAL LIABILITY ----
    gl = next((c for c in carriers if "general" in str(c.get("line", "")).lower()
               or str(c.get("line", "")).lower() in ("gl", "cgl")), None)
    if gl:
        _fill_policy_row(page, BAND_Y["gl"], letter_for.get(gl.get("carrier"), ""),
                         gl.get("policy_number"), gl.get("eff"), gl.get("exp"))
        # OCCUR vs CLAIMS-MADE (default OCCUR); aggregate applies-per (default POLICY)
        if "CLAIM" not in str(gl.get("form", "OCCUR")).upper():
            _check(page, "gl_occur")
        agg = str(gl.get("aggregate", "POLICY")).upper()
        if agg.startswith("PROJ"):
            _check(page, "agg_project")
        elif agg == "LOC":
            _check(page, "agg_loc")
        else:
            _check(page, "agg_policy")
        lim = gl.get("limits") or {}
        lim_lc = {str(k).lower(): v for k, v in lim.items()}
        for _label, ry, keys in GL_LIMITS:
            val = None
            for k in keys:
                if k in lim:
                    val = lim[k]; break
                if k.lower() in lim_lc:
                    val = lim_lc[k.lower()]; break
            if val is not None:
                _put_right(page, LIMIT_RIGHT, ry, _money(val))

    # ---- AUTOMOBILE LIABILITY ----
    auto = next((c for c in carriers if "auto" in str(c.get("line", "")).lower()), None)
    if auto:
        _fill_policy_row(page, BAND_Y["auto"], letter_for.get(auto.get("carrier"), ""),
                         auto.get("policy_number"), auto.get("eff"), auto.get("exp"))
        # Auto-type boxes are data-driven (must reflect the actual policy): a list
        # like ["SCHEDULED","HIRED","NON-OWNED"] or ["ANY"]. Nothing marked if absent.
        for a in (auto.get("autos") or []):
            s = str(a).upper()
            if "ANY" in s:
                _check(page, "any_auto")
            elif "SCHED" in s:
                _check(page, "scheduled")
            elif "NON" in s:
                _check(page, "non_owned")
            elif "HIRED" in s:
                _check(page, "hired")
            elif "OWN" in s:
                _check(page, "owned")
        lim = auto.get("limits") or {}
        csl = lim.get("CSL") or next(iter(lim.values()), None)
        if csl is not None:
            _put_right(page, LIMIT_RIGHT, C["auto_csl"][1], _money(csl))

    # ---- MOTOR TRUCK CARGO ----
    cargo = next((c for c in carriers if "cargo" in str(c.get("line", "")).lower()), None)
    if cargo:
        _put_center(page, COL["ltr"], BAND_Y["cargo"], letter_for.get(cargo.get("carrier"), ""))
        _put(page, C, "cargo_label", "MOTOR TRUCK CARGO", SIZE_BLOCK)
        _put_center(page, COL["policy"], BAND_Y["cargo"], cargo.get("policy_number"))
        _put_center(page, COL["eff"], BAND_Y["cargo"], _date4(cargo.get("eff")))
        _put_center(page, COL["exp"], BAND_Y["cargo"], _date4(cargo.get("exp")))
        ded = cargo.get("deductible")
        if ded is not None:
            _put_right(page, 548, BAND_Y["cargo"], f"${_money(ded)} Ded")
        lim = cargo.get("limits") or {}
        amt = next(iter(lim.values()), None)
        if amt is not None:
            _put_right(page, LIMIT_RIGHT, BAND_Y["cargo"], f"${_money(amt)}")

    # description: ops + any other coverages + stamp
    desc = []
    if content.description_of_operations:
        desc.append(content.description_of_operations.strip())
    for c in carriers:
        if c is auto or c is cargo or c is gl:
            continue
        lim = c.get("limits") or {}
        amt = next(iter(lim.values()), None)
        desc.append(f"{c.get('line','')}: {c.get('carrier','')} {c.get('policy_number','')} "
                    f"${_money(amt) if amt is not None else ''}")
    desc.append(f"Coverage data current as of {content.data_current_as_of.strftime('%m/%d/%Y')}. "
                f"Issued as a matter of information only.")
    for i, ln in enumerate(desc[:5]):
        _put(page, C, f"desc{i+1}", ln)

    _put(page, C, "holder_l1", content.holder_name, SIZE_BLOCK)
    for i, ln in enumerate(_lines(content.holder_address, 2)):
        _put(page, C, f"holder_l{i+2}", ln)

    if signature_png_path and signature_rect:
        page.insert_image(fitz.Rect(*signature_rect), filename=signature_png_path)
    else:
        _put(page, C, "auth_rep", "Derrick Brown", SIZE_BLOCK)

    return doc  # return open doc so caller can append the 101 page


def fill_acord101(content, blank_101_path):
    """Fill the ACORD 101 AcroForm with trailers + drivers, return open doc."""
    doc = fitz.open(blank_101_path)
    page = doc[0]
    agency = _lines(content.producer_block, 1)
    agency = agency[0] if agency else "Pinnacle Risk Advisors LLC"

    usdot = getattr(content, "usdot", "")
    mc = getattr(content, "mc_number", "")
    drivers = getattr(content, "drivers", []) or []
    trailers = getattr(content, "trailers", []) or []

    insured_block = content.insured_name
    for ln in _lines(content.insured_address, 2):
        insured_block += "\n" + ln
    if usdot or mc:
        insured_block += f"\nDOT# {usdot}   MC# {mc}"

    # build remark text (trailers + drivers, formatted)
    remark = "TRAILERS / VEHICLES:\n"
    if trailers:
        for t in trailers:
            remark += f"  {t.get('description','')}   VIN {t.get('vin','')}   ${_money(t.get('value',0))}\n"
    else:
        remark += "  (none scheduled)\n"
    remark += "\nDRIVERS:\n"
    if drivers:
        for d in drivers:
            remark += f"  {d.get('first','')} {d.get('last','')}   {d.get('lic_state','')}\n"
    else:
        remark += "  (none scheduled)\n"

    vals = {
        "F[0].P1[0].Form_CurrentPageNumber_A[0]": "2",
        "F[0].P1[0].Form_TotalPageNumber_A[0]": "2",
        "F[0].P1[0].Producer_FullName_A[0]": agency,
        "F[0].P1[0].NamedInsured_FullName_A[0]": content.insured_name,
        "F[0].P1[0].NamedInsured_FullName_B[0]": (_lines(content.insured_address, 2) or [""])[0],
        "F[0].P1[0].NamedInsured_FullName_C[0]": (_lines(content.insured_address, 2) + ["", ""])[1],
        "F[0].P1[0].NamedInsured_FullName_D[0]": f"DOT# {usdot}   MC# {mc}",
        "F[0].P1[0].Policy_EffectiveDate_A[0]": content.issue_date.strftime("%m/%d/%Y"),
        "F[0].P1[0].AdditionalRemark_FormIdentifier_A[0]": "25",
        "F[0].P1[0].AdditionalRemark_FormName_A[0]": "CERTIFICATE OF LIABILITY INSURANCE",
        "F[0].P1[0].AdditionalRemark_RemarkText_A[0]": remark,
    }
    for w in (page.widgets() or []):
        if w.field_name in vals:
            w.field_value = vals[w.field_name]
            w.update()
    doc.bake()  # flatten the AcroForm
    return doc


def generate_trucking_cert(content, blank_25_path, blank_101_path=None,
                           include_101=True, signature_png_path=None, signature_rect=None) -> bytes:
    doc25 = fill_acord25_2016(content, blank_25_path, signature_png_path, signature_rect)
    if include_101 and blank_101_path:
        doc101 = fill_acord101(content, blank_101_path)
        doc25.insert_pdf(doc101)
        doc101.close()
    out = io.BytesIO()
    doc25.save(out, garbage=4, deflate=True)
    doc25.close()
    return out.getvalue()


if __name__ == "__main__":
    from dataclasses import dataclass, field
    from datetime import date

    @dataclass
    class CertContent:
        cert_number: str
        issue_date: date
        producer_block: str
        insured_name: str
        insured_address: str
        holder_name: str
        holder_address: str
        description_of_operations: str
        coverages: list
        data_current_as_of: date
        usdot: str = ""
        mc_number: str = ""
        drivers: list = field(default_factory=list)
        trailers: list = field(default_factory=list)

    content = CertContent(
        cert_number="PRA-20260624-0001",
        issue_date=date(2026, 6, 24),
        producer_block=("Pinnacle Risk Advisors LLC\n"
                        "2700 Cumberland Pkwy SE, Ste 410, Atlanta, GA 30339\n"
                        "(943) 239-3439  certs@pinnacleriskad.com"),
        insured_name="ASHER'S CUP LLC",
        insured_address="1313 LAKE DR\nBAINBRIDGE, GA 39817",
        holder_name="JAKEBRAKE LOGISTICS LLC",
        holder_address="1006 WEST CENTENNIAL ROAD\nPAPILLION, NE 68046",
        description_of_operations="Motor carrier hauling general freight.",
        coverages=[
            {"line": "Commercial General Liability", "carrier": "Progressive Mountain Insurance Company",
             "policy_number": "864728807", "eff": "10/22/25", "exp": "10/22/26",
             "limits": {"EACH OCCURRENCE": 1000000, "DAMAGE TO RENTED PREMISES": 100000,
                        "MED EXP": 5000, "PERSONAL & ADV INJURY": 1000000,
                        "GENERAL AGGREGATE": 2000000, "PRODUCTS COMP/OP AGG": 2000000}, "naic": "35190"},
            {"line": "Auto Liability", "carrier": "Progressive Mountain Insurance Company",
             "policy_number": "864728807", "eff": "10/22/25", "exp": "10/22/26",
             "limits": {"CSL": 1000000}, "naic": "35190", "autos": ["SCHEDULED", "HIRED", "NON-OWNED"]},
            {"line": "Motor Truck Cargo", "carrier": "Canal Insurance Company",
             "policy_number": "864728807", "eff": "10/22/25", "exp": "10/22/26",
             "limits": {"Limit": 100000}, "deductible": 1000, "naic": "11142"},
        ],
        data_current_as_of=date(2026, 6, 24),
        usdot="4428067", mc_number="1741911",
        drivers=[{"first": "BRYAN", "last": "COLLIER", "lic_state": "GA"},
                 {"first": "GARY", "last": "ROYAL", "lic_state": "GA"}],
        trailers=[{"description": "2018 GREAT DANE", "vin": "3AKJGHDV7JSJT1749", "value": 0},
                  {"description": "2024 KENWORTH", "vin": "1XKYDP9X4RJ339355", "value": 0}],
    )

    pdf = generate_trucking_cert(content,
                                 "/mnt/user-data/uploads/acord_25_2016-03.pdf",
                                 "/mnt/user-data/uploads/acord_101.pdf")
    open("/home/claude/trucking_cert.pdf", "wb").write(pdf)
    d = fitz.open("/home/claude/trucking_cert.pdf")
    for i in range(d.page_count):
        d[i].get_pixmap(matrix=fitz.Matrix(2, 2)).save(f"/home/claude/trucking_cert_p{i+1}.png")
    print("ok", len(pdf), "bytes", d.page_count, "pages")
