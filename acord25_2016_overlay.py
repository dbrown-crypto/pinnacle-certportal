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
import os
from typing import Optional

import fitz  # PyMuPDF

FONT = "helv"
SIZE = 7.0
SIZE_BLOCK = 8.0
INK = (0.0, 0.0, 0.0)


class TextOverflowError(ValueError):
    """A line would print past its box border, or a block will not fit.

    Raised instead of drawing, because text that runs off the right edge is
    invisible in the rendered PDF: the cert looks fine and the wording is
    simply gone. Callers should route to manual issuance rather than ship.
    """


# Free-form text boxes, measured off the 2016/03 template. x_right is the
# printed border less the 6pt inset, so max width = x_right - x. y0 is the
# first baseline and matches the coordinates already in C25 -- no field has
# moved. `lines` is how many baselines fit above the box's bottom border.
BOX_DESC = {"x": 24.0, "x_right": 588.0, "y0": 580.0, "leading": 9.0, "lines": 8}
BOX_HOLDER = {"x": 24.0, "x_right": 300.0, "y0": 668.0, "leading": 9.0, "lines": 3}
BOX_INSURED = {"x": 24.0, "x_right": 325.0, "y0": 195.0, "leading": 9.0, "lines": 3}
BOX_PRODUCER = {"x": 24.0, "x_right": 325.0, "y0": 134.0, "leading": 9.0, "lines": 5}

# Description shrinks 7.0 -> 6.5 -> 6.0 before anything is carried to the 101.
MIN_SIZE = 6.0
SIZE_STEP = 0.5
OVERFLOW_NOTE = "See ACORD 101 Additional Remarks Schedule attached."
CONTINUATION_HEADING = "DESCRIPTION OF OPERATIONS (continued):"
# Name/address blocks step both sizes together rather than carrying anywhere.
NAME_BLOCK_LADDER = ((SIZE_BLOCK, SIZE), (7.5, 6.5), (7.0, 6.5), (6.5, 6.0), (6.0, 6.0))

# Producer e-mail printed when the producer block omits one (shared COI inbox).
PRODUCER_EMAIL_FALLBACK = "certs@pinnacleriskad.com"

# Authoritative NAIC for Pinnacle's appointed carriers, keyed by normalized
# carrier name. Corrects wrong/missing seed data on OUTPUT so a cert never ships
# a bad NAIC for a core carrier. NAIC must match the issuing entity on the dec
# page; Progressive writes GA commercial auto under several entities, so only
# exact-named entities are mapped here (extend as appointments are added).
CARRIER_NAIC = {
    "progressive mountain": "35190",
    "progressive mountain insurance company": "35190",
    "canal insurance": "10464",
    "canal insurance company": "10464",
}

# AUTHORIZED REPRESENTATIVE signature. If no signature image is passed explicitly,
# the module looks for `pinnacle_signature.png` next to this file (commit it to the
# repo root) and stamps it in the signing box. If that file is absent too, it falls
# back to text. The AUTHORIZED REPRESENTATIVE box is the bottom strip of the
# CANCELLATION cell: top divider y=707.9, "AUTHORIZED REPRESENTATIVE" label
# y=708-716, bottom border y=743.9, walls x=306 and x=594. The signature goes in
# the open signing area BELOW the label (not up in the cancellation clause),
# centered at x=450 / y~730. fitz centers the image within the rect.
DEFAULT_SIGNATURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "pinnacle_signature.png")
SIGNATURE_RECT = (377.0, 723.0, 522.0, 737.0)

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
    # Slots 6-8 were always inside the box (bottom border y=648) but unused,
    # so long wording spilled to the 101 three lines earlier than it needed to.
    "desc6": (24, 625), "desc7": (24, 634), "desc8": (24, 643),

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

# CHECKBOX square CENTERS (PDF points, y from top), measured off the ACORD 25
# (2016/03) form squares (each ~14x12pt). An "X" is drawn centered in the named
# box. GL OCCUR + aggregate POLICY default ON when a GL line is present; the
# auto-type boxes are marked ONLY from the carrier's `autos` list, never guessed
# (a cert must not imply coverage the policy doesn't actually grant — E&O).
CHECKBOX = {
    "gl_claims_made": (57.6, 318.0),
    "gl_occur":       (122.4, 318.0),
    "gl_agg_policy":  (43.2, 366.0),
    "gl_agg_project": (86.4, 366.0),
    "gl_agg_loc":     (129.6, 366.0),
    "auto_any":       (43.2, 402.0),
    "auto_owned":     (43.2, 414.0),
    "auto_hired":     (43.2, 426.0),
    "auto_scheduled": (111.6, 414.0),
    "auto_non_owned": (111.6, 426.0),
}


def _check(page, key, size=9.0):
    """Mark a CHECKBOX square: draw an X centered in box `key`."""
    cx, cy = CHECKBOX[key]
    w = fitz.get_text_length("X", fontname=FONT, fontsize=size)
    page.insert_text((cx - w / 2, cy + size * 0.35), "X",
                     fontname=FONT, fontsize=size, color=INK)


def _resolve_signature(signature_png_path):
    """Find the signature image. Order: explicit arg, then pinnacle_signature.png
    next to this module, then in the current working directory. Returns a path or
    None. If nothing is found, logs WHERE it looked to stderr so a missing image
    shows up in the Render logs instead of silently falling back to text."""
    import sys
    if signature_png_path and os.path.exists(signature_png_path):
        return signature_png_path
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "pinnacle_signature.png"),
                  os.path.join(os.getcwd(), "pinnacle_signature.png")]
    for c in candidates:
        if os.path.exists(c):
            return c
    print("[acord25] signature image NOT found; checked: "
          + "; ".join(candidates) + " -- falling back to text.", file=sys.stderr)
    return None



def _naic_for(carrier_name, provided):
    """Authoritative NAIC for a known appointed carrier (corrects wrong/missing
    seed data), else the value provided in the policy data."""
    return CARRIER_NAIC.get(str(carrier_name or "").strip().lower(), provided)


def _expand_year(d):
    """Display 2-digit years as 4 digits ('01/01/26' -> '01/01/2026').
    Leaves 4-digit years and any non-MM/DD/YY value untouched (no corruption)."""
    if not d:
        return d
    parts = str(d).strip().split("/")
    if len(parts) == 3 and len(parts[2]) == 2 and parts[2].isdigit():
        parts[2] = "20" + parts[2]
        return "/".join(parts)
    return str(d)


def _fill_policy_row(page, y, letter, policy, eff, exp):
    """Place INSR LTR, policy number, eff and exp centered in their columns at row y."""
    _put_center(page, COL["ltr"], y, letter)
    _put_center(page, COL["policy"], y, policy)
    _put_center(page, COL["eff"], y, eff)
    _put_center(page, COL["exp"], y, exp)


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


# ---------------------------------------------------------------------------
# Width-aware text fitting
#
# Every free-form block goes through here. Widths come from the real font
# metrics via fitz.get_text_length, never character counts, so the measured
# width is exactly what PyMuPDF will draw.
# ---------------------------------------------------------------------------

def _text_width(s, size):
    return fitz.get_text_length(str(s), fontname=FONT, fontsize=size)


def _break_token(word, size, max_width):
    """Split one token that is wider than the box (long VIN, e-mail, URL)."""
    parts = []
    while _text_width(word, size) > max_width:
        cut = len(word)
        while cut > 1 and _text_width(word[:cut], size) > max_width:
            cut -= 1
        if cut == 1 and _text_width(word[0], size) > max_width + 0.01:
            raise TextOverflowError(
                "glyph %r is %.1fpt at %.1fpt and cannot fit a %.1fpt box"
                % (word[0], _text_width(word[0], size), size, max_width))
        parts.append(word[:cut])
        word = word[cut:]
    if word:
        parts.append(word)
    return parts


def _wrap_paragraph(text, size, max_width):
    """Wrap one paragraph on word boundaries. Never returns an over-wide line."""
    words = str(text).split()
    if not words:
        return [""]
    lines, cur = [], ""
    for word in words:
        trial = word if not cur else cur + " " + word
        if _text_width(trial, size) <= max_width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if _text_width(word, size) > max_width:
            parts = _break_token(word, size, max_width)
            lines.extend(parts[:-1])
            cur = parts[-1]
        else:
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _wrap_block(text, size, max_width):
    """Wrap a block, preserving explicit newlines as paragraph breaks."""
    out = []
    for para in str(text).split("\n"):
        if not para.strip():
            out.append("")
        else:
            out.extend(_wrap_paragraph(para, size, max_width))
    return out


def _fit_size(paragraphs, max_width, max_lines, size=SIZE,
              min_size=MIN_SIZE, step=SIZE_STEP):
    """Largest size in [min_size, size] whose wrap fits, else min_size."""
    s = size
    while True:
        n = sum(len(_wrap_block(p, s, max_width)) for p in paragraphs)
        if n <= max_lines or round(s - step, 2) < min_size:
            return s
        s = round(s - step, 2)


def _assert_fits(lines, size, max_width, label):
    """Guard: refuse to draw anything that would cross the border."""
    for ln in lines:
        if not ln:
            continue
        w = _text_width(ln, size)
        if w > max_width + 0.01:
            raise TextOverflowError(
                "%s: line is %.1fpt in a %.1fpt box (over by %.1fpt): %r"
                % (label, w, max_width, w - max_width, ln))
    return True


def _put_block(page, box, lines, size, label):
    """Draw pre-wrapped lines down a box. Guards every line before drawing."""
    max_width = box["x_right"] - box["x"]
    _assert_fits(lines, size, max_width, label)
    if len(lines) > box["lines"]:
        raise TextOverflowError("%s: %d lines will not fit %d slots"
                                % (label, len(lines), box["lines"]))
    for i, ln in enumerate(lines):
        if not ln:
            continue
        page.insert_text((box["x"], box["y0"] + i * box["leading"]), ln,
                         fontname=FONT, fontsize=size, color=INK)


def _put_name_block(page, box, name, address, label):
    """Name on the first baseline at SIZE_BLOCK, address below at SIZE.

    Both are width-wrapped. If the wrapped result overruns the box, both sizes
    step down together rather than silently dropping the tail, which is what
    _lines(block, n) used to do.
    """
    max_width = box["x_right"] - box["x"]
    for nsize, asize in NAME_BLOCK_LADDER:
        nlines = _wrap_block(name, nsize, max_width) if name else []
        alines = _wrap_block(address, asize, max_width) if address else []
        if len(nlines) + len(alines) <= box["lines"]:
            break
    else:
        raise TextOverflowError(
            "%s: %d lines will not fit %d slots even at %.1fpt"
            % (label, len(nlines) + len(alines), box["lines"], NAME_BLOCK_LADDER[-1][0]))

    _assert_fits(nlines, nsize, max_width, label + " name")
    _assert_fits(alines, asize, max_width, label + " address")
    y = box["y0"]
    for ln in nlines:
        page.insert_text((box["x"], y), ln, fontname=FONT, fontsize=nsize, color=INK)
        y += box["leading"]
    for ln in alines:
        page.insert_text((box["x"], y), ln, fontname=FONT, fontsize=asize, color=INK)
        y += box["leading"]


def _draw_description(page, paragraphs, stamp):
    """Draw DESCRIPTION OF OPERATIONS, width-wrapped.

    The data-currency stamp is reserved and always printed as the last line.
    Previously the block was assembled as a list and truncated with desc[:5],
    and because the stamp was appended last it was the first thing dropped.

    Returns text that must be carried to the ACORD 101, or "" if all fit.
    """
    box = BOX_DESC
    max_width = box["x_right"] - box["x"]
    size = _fit_size(list(paragraphs) + [stamp], max_width, box["lines"])

    stamp_lines = _wrap_block(stamp, size, max_width)
    # Track actual source paragraphs, including explicit blank paragraphs.
    # A top-level item may itself contain newlines (the UI assembles wording
    # that way), so using only the item's list index would erase those breaks
    # when rebuilding text for the wider ACORD 101 remarks field.
    body = []
    source_idx = 0
    for block in paragraphs:
        for para in str(block).split("\n"):
            for ln in _wrap_paragraph(para, size, max_width):
                body.append((source_idx, ln))
            source_idx += 1

    overflow = ""
    room = box["lines"] - len(stamp_lines)
    if len(body) > room:
        keep = max(room - 1, 0)
        carried, groups = body[keep:], []
        for idx, ln in carried:
            if groups and groups[-1][0] == idx:
                groups[-1][1].append(ln)
            else:
                groups.append((idx, [ln]))
        # Join wrapped fragments inside each source paragraph with spaces, but
        # restore explicit paragraph boundaries with newlines. Do not strip
        # the result: a blank paragraph is meaningful and consumes a slot.
        overflow = "\n".join(" ".join(g[1]).strip() for g in groups)
        body = body[:keep] + [(None, OVERFLOW_NOTE)]

    _put_block(page, box, [ln for _, ln in body] + stamp_lines, size,
               "description of operations")
    return overflow


def fill_acord25_2016(content, blank_25_path, signature_png_path=None, signature_rect=None,
                      overflow_out=None):
    """content is a CertContent-like object. Trucking extras read from optional
    attributes if present: usdot, mc_number, drivers, trailers. Carrier rows may
    carry a `deductible` and `naic`.

    overflow_out: optional list. Any description text that will not fit the
    ACORD 25 box is appended to it for the caller to carry to the ACORD 101.
    Kept as an out-parameter so the return value stays the open doc and
    existing callers are unaffected."""
    doc = fitz.open(blank_25_path)
    page = doc[0]
    C = C25

    _put(page, C, "date", content.issue_date.strftime("%m/%d/%Y"))
    _put(page, C, "cert_number", content.cert_number, SIZE_BLOCK)

    # Do not use _lines(..., n) for a free-form block: its slice silently
    # discards data before the fitting guard can see it.
    plines = [p.strip() for p in str(content.producer_block or "").split("\n")
              if p.strip()]
    _put_name_block(page, BOX_PRODUCER, plines[0] if plines else "",
                    "\n".join(plines[1:]), "producer")

    import re
    email = next((w for ln in plines for w in ln.replace(",", " ").split() if "@" in w), None) \
        or PRODUCER_EMAIL_FALLBACK
    phone = None
    for ln in plines:
        m = re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", ln)
        if m:
            phone = m.group(0); break
    _put(page, C, "contact_name", "Derrick Brown")
    _put(page, C, "contact_phone", phone)
    _put(page, C, "contact_email", email)

    _put_name_block(page, BOX_INSURED, content.insured_name,
                    content.insured_address, "insured")

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
        _put(page, C, f"naic_{k}", _naic_for(name, naic))

    # ---- COMMERCIAL GENERAL LIABILITY ----
    gl = next((c for c in carriers if "general" in str(c.get("line", "")).lower()
               or str(c.get("line", "")).lower() in ("gl", "cgl")), None)
    if gl:
        _fill_policy_row(page, BAND_Y["gl"], letter_for.get(gl.get("carrier"), ""),
                         gl.get("policy_number"),
                         _expand_year(gl.get("eff")), _expand_year(gl.get("exp")))
        # Coverage-form box: OCCUR is the trucking default; CLAIMS-MADE only when
        # the data declares it. GEN'L AGGREGATE APPLIES PER: POLICY default,
        # override to PROJECT/LOC. Data-driven so the form basis isn't misstated.
        _check(page, "gl_claims_made"
               if str(gl.get("form", "")).strip().upper() == "CLAIMS-MADE" else "gl_occur")
        _agg = str(gl.get("aggregate", "")).strip().upper()
        _check(page, {"PROJECT": "gl_agg_project", "LOC": "gl_agg_loc"}.get(_agg, "gl_agg_policy"))
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
                         auto.get("policy_number"),
                         _expand_year(auto.get("eff")), _expand_year(auto.get("exp")))
        # Auto-type boxes come from `autos`. DEFAULT is SCHEDULED only, since most
        # of the book writes scheduled autos. Pass an explicit `autos` list to
        # override per policy, e.g. ["ANY"] or ["HIRED","NON-OWNED"]. The default
        # will be WRONG for any policy that isn't scheduled-auto, so set `autos`
        # from the dec page whenever it differs (a misstated symbol is E&O).
        _auto_box = {"ANY": "auto_any", "OWNED": "auto_owned", "HIRED": "auto_hired",
                     "SCHEDULED": "auto_scheduled", "NON-OWNED": "auto_non_owned"}
        _autos = auto.get("autos") or ["SCHEDULED"]
        for _a in _autos:
            _k = _auto_box.get(str(_a).strip().upper())
            if _k:
                _check(page, _k)
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
        _put_center(page, COL["eff"], BAND_Y["cargo"], _expand_year(cargo.get("eff")))
        _put_center(page, COL["exp"], BAND_Y["cargo"], _expand_year(cargo.get("exp")))
        # Limit and deductible stack on two baselines, both right-aligned to
        # LIMIT_RIGHT. On this free-form OTHER row the LIMITS column divider
        # sits at x~514.8; the old code placed the deductible right-aligned to
        # x=548 on the SAME baseline as the limit, so a ~33pt string started
        # near x=515 and straddled the divider (the "$" printed on the line).
        # Stacking each short string right-aligned to 592 keeps both clear of
        # the divider and inside the right border. Band is y528-564 (center 546);
        # 542/554 sit one line above/below center.
        lim = cargo.get("limits") or {}
        amt = next(iter(lim.values()), None)
        if amt is not None:
            _put_right(page, LIMIT_RIGHT, 542.0, f"${_money(amt)}")
        ded = cargo.get("deductible")
        if ded is not None:
            _put_right(page, LIMIT_RIGHT, 554.0, f"${_money(ded)} Ded")

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
    stamp = (f"Coverage data current as of {content.data_current_as_of.strftime('%m/%d/%Y')}. "
             f"Issued as a matter of information only.")
    desc_overflow = _draw_description(page, desc, stamp)
    if desc_overflow:
        if overflow_out is None:
            raise TextOverflowError(
                "description of operations overflows the ACORD 25 box and no "
                "overflow_out was supplied; %d characters would be lost"
                % len(desc_overflow))
        overflow_out.append(desc_overflow)

    _put_name_block(page, BOX_HOLDER, content.holder_name,
                    content.holder_address, "certificate holder")

    # Signature: explicit arg > pinnacle_signature.png (next to module or CWD)
    # > text fallback. Resolver logs to stderr if the image can't be found.
    sig_path = _resolve_signature(signature_png_path)
    if sig_path:
        page.insert_image(fitz.Rect(*(signature_rect or SIGNATURE_RECT)),
                          filename=sig_path, keep_proportion=True)
    else:
        _put(page, C, "auth_rep", "Derrick Brown", SIZE_BLOCK)

    return doc  # return open doc so caller can append the 101 page


def fill_acord101(content, blank_101_path, description_continued=""):
    """Fill the ACORD 101 AcroForm with trailers + drivers, return open doc.

    description_continued: text carried over from the ACORD 25 description
    box, printed first under its own heading so nothing is lost."""
    doc = fitz.open(blank_101_path)
    page = doc[0]
    agency = _lines(content.producer_block, 1)
    agency = agency[0] if agency else "Pinnacle Risk Advisors LLC"

    usdot = getattr(content, "usdot", "")
    mc = getattr(content, "mc_number", "")
    drivers = getattr(content, "drivers", []) or []
    trailers = getattr(content, "trailers", []) or []
    vehicles = getattr(content, "vehicles", []) or []

    insured_block = content.insured_name
    for ln in _lines(content.insured_address, 2):
        insured_block += "\n" + ln
    if usdot or mc:
        insured_block += f"\nDOT# {usdot}   MC# {mc}"

    # build remark text (carried description + power units + trailers + drivers)
    remark = ""
    if description_continued:
        remark += CONTINUATION_HEADING + "\n" + description_continued + "\n\n"
    remark += "POWER UNITS / TRUCKS:\n"
    if vehicles:
        for v in vehicles:
            remark += f"  {v.get('description','')}   VIN {v.get('vin','')}   ${_money(v.get('value',0))}\n"
    else:
        remark += "  (none scheduled)\n"
    remark += "\nTRAILERS:\n"
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
    carried = []
    doc25 = fill_acord25_2016(content, blank_25_path, signature_png_path, signature_rect,
                              overflow_out=carried)
    carry_text = "\n".join(x for x in carried if x)

    # The 101 is normally attached only for scheduled units, but overflowing
    # description wording is the other reason to need one, so turn it on
    # rather than fail when there is somewhere to put the text.
    want_101 = bool(include_101 or carry_text)
    if carry_text and not blank_101_path:
        doc25.close()
        raise TextOverflowError(
            "description of operations overflows the ACORD 25 box and no ACORD 101 "
            "template is available (set ACORD101_TEMPLATE_PATH); %d characters "
            "would be lost. Shorten the wording or configure the 101."
            % len(carry_text))
    if want_101 and blank_101_path:
        doc101 = fill_acord101(content, blank_101_path, description_continued=carry_text)
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
                        "(770) 758-3197  certs@pinnacleriskad.com"),
        insured_name="ASHER'S CUP LLC",
        insured_address="1313 LAKE DR\nBAINBRIDGE, GA 39817",
        holder_name="JAKEBRAKE LOGISTICS LLC",
        holder_address="1006 WEST CENTENNIAL ROAD\nPAPILLION, NE 68046",
        description_of_operations="Motor carrier hauling general freight.",
        coverages=[
            {"line": "Commercial General Liability", "carrier": "Progressive Mountain Insurance Company",
             "policy_number": "864728807", "eff": "10/22/2025", "exp": "10/22/2026",
             "limits": {"EACH OCCURRENCE": 1000000, "DAMAGE TO RENTED PREMISES": 100000,
                        "MED EXP": 5000, "PERSONAL & ADV INJURY": 1000000,
                        "GENERAL AGGREGATE": 2000000, "PRODUCTS COMP/OP AGG": 2000000}, "naic": "35190"},
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
