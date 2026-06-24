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
    "date":          (500, 56),

    "producer_l1":   (24, 134), "producer_l2": (24, 143), "producer_l3": (24, 152),
    "producer_l4":   (24, 161), "producer_l5": (24, 170),

    "contact_name":  (400, 127), "contact_phone": (400, 140), "contact_email": (400, 153),

    "insurer_a":     (345, 179), "insurer_b": (345, 191), "insurer_c": (345, 203),
    "naic_a":        (560, 179), "naic_b": (560, 191), "naic_c": (560, 203),

    "insured_l1":    (24, 195), "insured_l2": (24, 204), "insured_l3": (24, 213),
    "dot_mc":        (40, 235),          # DOT# / MC# line under insured

    "cert_number":   (305, 249),

    # AUTOMOBILE LIABILITY row (label y386.6 on this form ~12pt higher than 2025)
    "auto_ltr":      (16, 394),
    "auto_policy":   (250, 394),
    "auto_eff":      (337, 394),
    "auto_exp":      (384, 394),
    "auto_csl":      (573, 390),

    # MOTOR TRUCK CARGO row — clean band below Workers Comp, above Description
    "cargo_ltr":     (16, 540),
    "cargo_label":   (54, 540),
    "cargo_policy":  (250, 540),
    "cargo_eff":     (337, 540),
    "cargo_exp":     (384, 540),
    "cargo_ded":     (452, 540),
    "cargo_limit":   (560, 540),

    # DESCRIPTION OF OPERATIONS (label y566.6)
    "desc1": (24, 580), "desc2": (24, 589), "desc3": (24, 598),
    "desc4": (24, 607), "desc5": (24, 616),

    "holder_l1": (24, 668), "holder_l2": (24, 677), "holder_l3": (24, 686),
    "auth_rep":  (400, 703),
}


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

    auto = next((c for c in carriers if "auto" in str(c.get("line", "")).lower()), None)
    if auto:
        _put(page, C, "auto_ltr", letter_for.get(auto.get("carrier"), ""))
        _put(page, C, "auto_policy", auto.get("policy_number"))
        _put(page, C, "auto_eff", auto.get("eff"))
        _put(page, C, "auto_exp", auto.get("exp"))
        lim = auto.get("limits") or {}
        csl = lim.get("CSL") or next(iter(lim.values()), None)
        if csl is not None:
            _put(page, C, "auto_csl", _money(csl))

    cargo = next((c for c in carriers if "cargo" in str(c.get("line", "")).lower()), None)
    if cargo:
        _put(page, C, "cargo_ltr", letter_for.get(cargo.get("carrier"), ""))
        _put(page, C, "cargo_label", "MOTOR TRUCK CARGO", SIZE_BLOCK)
        _put(page, C, "cargo_policy", cargo.get("policy_number"))
        _put(page, C, "cargo_eff", cargo.get("eff"))
        _put(page, C, "cargo_exp", cargo.get("exp"))
        ded = cargo.get("deductible")
        if ded is not None:
            _put(page, C, "cargo_ded", f"${_money(ded)} Ded")
        lim = cargo.get("limits") or {}
        amt = next(iter(lim.values()), None)
        if amt is not None:
            _put(page, C, "cargo_limit", f"${_money(amt)}")

    # description: ops + any other coverages + stamp
    desc = []
    if content.description_of_operations:
        desc.append(content.description_of_operations.strip())
    for c in carriers:
        if c is auto or c is cargo:
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
            {"line": "Auto Liability", "carrier": "Progressive Mountain Insurance Company",
             "policy_number": "864728807", "eff": "10/22/2025", "exp": "10/22/2026",
             "limits": {"CSL": 1000000}, "naic": "35190"},
            {"line": "Motor Truck Cargo", "carrier": "Progressive Mountain Insurance Company",
             "policy_number": "864728807", "eff": "10/22/2025", "exp": "10/22/2026",
             "limits": {"Limit": 100000}, "deductible": 1000},
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
