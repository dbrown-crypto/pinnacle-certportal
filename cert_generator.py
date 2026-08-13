"""
cert_generator.py — Pinnacle Risk Advisors self-serve certificate portal

Turns an approved (gate-passed) request into a finished certificate PDF.

Two modes:
  1. PRODUCTION: fills your licensed ACORD 25 PDF template. Uses the
     widget-flatten approach you already use elsewhere — fill the form fields,
     then flatten so the values render reliably as static content on every
     viewer (no dependence on the reader honoring AcroForm appearances).
  2. FALLBACK (demo/dev): if no template is supplied, draws a clean,
     Pinnacle-branded certificate so the pipeline is runnable end to end.
     This is NOT the ACORD 25 and is watermarked SAMPLE. Never ship it to a
     real holder — supply your licensed template in production.

The authorized-rep signature is applied here, automatically, because by the
time a request reaches this module the gate has already guaranteed it is a
plain informational cert on verified-active, in-date coverage. The signature
is only safe because the cert can only ever be the safe kind.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import fitz  # PyMuPDF


# Pinnacle client-facing colorway (navy / gold / cream), from the approved
# proposal palette.
NAVY = (0x11 / 255, 0x24 / 255, 0x3F / 255)
NAVY_DEEP = (0x0E / 255, 0x1E / 255, 0x36 / 255)
GOLD = (0xC9 / 255, 0xA9 / 255, 0x4E / 255)
GOLD_DARK = (0xA1 / 255, 0x83 / 255, 0x2F / 255)
CREAM = (0xFB / 255, 0xF8 / 255, 0xF1 / 255)
INK = (0x15 / 255, 0x26 / 255, 0x3F / 255)
SLATE = (0x3F / 255, 0x47 / 255, 0x54 / 255)


@dataclass
class CertContent:
    """Everything that prints on the certificate. The API builds this from the
    stored policy snapshot + the holder the client entered."""
    cert_number: str
    issue_date: date
    producer_block: str           # Pinnacle name / address / contact
    insured_name: str
    insured_address: str
    holder_name: str
    holder_address: str
    description_of_operations: str
    coverages: list[dict]         # [{line, carrier, policy_number, eff, exp, limits:{label:amount}}]
    data_current_as_of: date

    # Trucking-specific fields (optional, backward-compatible)
    usdot: str = ""
    mc_number: str = ""
    drivers: list = field(default_factory=list)
    vehicles: list = field(default_factory=list)   # power units / trucks
    trailers: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# PRODUCTION PATH: fill a licensed ACORD 25 template
# ---------------------------------------------------------------------------
def fill_acord25_template(
    template_path: str,
    field_map: dict[str, str],
    signature_png_path: Optional[str] = None,
    signature_rect: Optional[tuple[float, float, float, float]] = None,
) -> bytes:
    """Fill your licensed ACORD 25 AcroForm template and flatten it.

    field_map maps the template's form-field names -> string values. Run the
    one-time helper dump_field_names() below against your template to discover
    the exact field names, then build field_map in the API.

    signature_rect is (x0, y0, x1, y1) in PDF points for the Authorized
    Representative box on your template.
    """
    doc = fitz.open(template_path)
    for page in doc:
        for widget in (page.widgets() or []):
            if widget.field_name in field_map:
                widget.field_value = str(field_map[widget.field_name])
                widget.update()

    # Apply signature before flattening so it becomes part of the page.
    if signature_png_path and signature_rect:
        page = doc[0]
        page.insert_image(fitz.Rect(*signature_rect), filename=signature_png_path)

    # Flatten: render form field values as static content and drop the widgets,
    # so the filled values are reliable across all PDF viewers.
    flat = io.BytesIO()
    doc.bake()  # converts form fields to page content (PyMuPDF >= 1.23)
    doc.save(flat, garbage=4, deflate=True)
    doc.close()
    return flat.getvalue()


def dump_field_names(template_path: str) -> list[str]:
    """One-time utility: list the AcroForm field names in your ACORD 25 so you
    can build field_map. Run once, paste the names into the API's mapper."""
    doc = fitz.open(template_path)
    names = []
    for page in doc:
        for w in (page.widgets() or []):
            names.append(w.field_name)
    doc.close()
    return names


# ---------------------------------------------------------------------------
# FALLBACK PATH: branded sample certificate (runnable without a template)
# ---------------------------------------------------------------------------
def render_fallback_certificate(
    content: CertContent,
    signature_png_path: Optional[str] = None,
) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)  # US Letter
    M = 48

    def text(x, y, s, size=9, color=INK, font="helv", bold=False):
        page.insert_text((x, y), s, fontsize=size,
                         fontname=("hebo" if bold else font), color=color)

    # Header band
    page.draw_rect(fitz.Rect(0, 0, 612, 92), color=None, fill=NAVY)
    # Gold diamond-outline "P" monogram
    cx, cy, r = 78, 46, 22
    page.draw_polyline([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r)],
                       color=GOLD, width=1.6, closePath=True)
    text(cx - 6, cy + 7, "P", size=20, color=GOLD, bold=True)
    text(118, 40, "PINNACLE RISK ADVISORS", size=15, color=CREAM, bold=True)
    text(118, 58, "Certificate of Liability Insurance", size=10, color=GOLD)
    text(430, 40, f"Certificate No.  {content.cert_number}", size=9, color=CREAM)
    text(430, 58, f"Issued  {content.issue_date.strftime('%m/%d/%Y')}", size=9, color=CREAM)

    y = 120
    # Producer / Insured row
    def labeled_box(x, w, label, body, height=84):
        nonlocal_y = y
        page.draw_rect(fitz.Rect(x, nonlocal_y, x + w, nonlocal_y + height),
                       color=GOLD_DARK, width=0.6, fill=CREAM)
        text(x + 8, nonlocal_y + 16, label, size=7.5, color=GOLD_DARK, bold=True)
        ty = nonlocal_y + 30
        from acord25_2016_overlay import _wrap_block as _wb
        for _para in body.split("\n"):
            for line in _wb(_para, 8.5, w - 16):
                if not line:
                    continue
                text(x + 8, ty, line, size=8.5, color=INK)
                ty += 12

    labeled_box(M, 250, "PRODUCER", content.producer_block)
    labeled_box(M + 266, 250, "INSURED", f"{content.insured_name}\n{content.insured_address}")
    y += 100

    # Coverage table
    page.draw_rect(fitz.Rect(M, y, 612 - M, y + 20), color=None, fill=NAVY)
    text(M + 6, y + 14, "COVERAGES", size=8.5, color=CREAM, bold=True)
    y += 20
    headers = [("COVERAGE", M + 6), ("CARRIER", M + 150), ("POLICY #", M + 270),
               ("EFF", M + 360), ("EXP", M + 415), ("LIMIT", M + 470)]
    page.draw_rect(fitz.Rect(M, y, 612 - M, y + 16), color=GOLD_DARK, width=0.5, fill=(0.96, 0.93, 0.86))
    for h, x in headers:
        text(x, y + 11, h, size=7, color=NAVY, bold=True)
    y += 16
    for cov in content.coverages:
        limit_str = "  ".join(f"{k} ${v:,.0f}" for k, v in cov["limits"].items())
        row = [(cov["line"], M + 6), (cov["carrier"], M + 150), (cov["policy_number"], M + 270),
               (cov["eff"], M + 360), (cov["exp"], M + 415)]
        page.draw_rect(fitz.Rect(M, y, 612 - M, y + 26), color=GOLD_DARK, width=0.3)
        for v, x in row:
            text(x, y + 11, str(v), size=7.5, color=INK)
        text(M + 470, y + 11, limit_str.split("  ")[0], size=7, color=INK)
        if len(limit_str.split("  ")) > 1:
            text(M + 470, y + 21, limit_str.split("  ")[1], size=7, color=INK)
        y += 26
    y += 14

    # Description of operations -- width-aware, never truncated.
    # This used to draw content.description_of_operations[:120] as one
    # unwrapped line: past 120 characters the wording was silently dropped,
    # and what survived could still print outside the box. Wrap to the box
    # width and let the box grow. One- and two-line descriptions are
    # positioned exactly as before.
    from acord25_2016_overlay import _wrap_block, _fit_size
    _desc_text = content.description_of_operations or ""
    _desc_w = (612 - M - 8) - (M + 8)
    _desc_size = _fit_size([_desc_text], _desc_w, 4, size=8.0, min_size=6.0, step=0.5)
    _desc_lines = _wrap_block(_desc_text, _desc_size, _desc_w) if _desc_text else []
    _desc_h = max(50, 30 + len(_desc_lines) * 10)
    page.draw_rect(fitz.Rect(M, y, 612 - M, y + _desc_h), color=GOLD_DARK, width=0.6, fill=CREAM)
    text(M + 8, y + 14, "DESCRIPTION OF OPERATIONS / LOCATIONS / VEHICLES", size=7, color=GOLD_DARK, bold=True)
    for _i, _ln in enumerate(_desc_lines):
        if _ln:
            text(M + 8, y + 30 + _i * 10, _ln, size=_desc_size, color=INK)
    y += _desc_h + 16

    # Certificate holder
    page.draw_rect(fitz.Rect(M, y, M + 300, y + 70), color=GOLD_DARK, width=0.6, fill=CREAM)
    text(M + 8, y + 14, "CERTIFICATE HOLDER", size=7.5, color=GOLD_DARK, bold=True)
    ty = y + 30
    _hold_w = (M + 300 - 8) - (M + 8)
    for _para in f"{content.holder_name}\n{content.holder_address}".split("\n"):
        for line in _wrap_block(_para, 8.5, _hold_w):
            if not line:
                continue
            text(M + 8, ty, line, size=8.5, color=INK)
            ty += 12

    # Authorized representative
    page.draw_rect(fitz.Rect(M + 316, y, 612 - M, y + 70), color=GOLD_DARK, width=0.6, fill=CREAM)
    text(M + 324, y + 14, "AUTHORIZED REPRESENTATIVE", size=7.5, color=GOLD_DARK, bold=True)
    sig_rect = fitz.Rect(M + 324, y + 24, 612 - M - 8, y + 56)
    if signature_png_path:
        page.insert_image(sig_rect, filename=signature_png_path)
    else:
        text(M + 324, y + 46, "Derrick Brown — Pinnacle Risk Advisors", size=9, color=NAVY, bold=True)
    page.draw_line((M + 324, y + 58), (612 - M - 8, y + 58), color=SLATE, width=0.5)
    y += 86

    text(M, y + 6, f"Coverage data current as of {content.data_current_as_of.strftime('%m/%d/%Y')}.",
         size=7, color=SLATE)
    text(M, y + 16, "This certificate is issued as a matter of information only and confers no rights upon the holder.",
         size=7, color=SLATE)

    # SAMPLE watermark — fallback only, must never reach a real holder
    tw = fitz.TextWriter(page.rect)
    tw.append((120, 460), "SAMPLE", font=fitz.Font("hebo"), fontsize=96)
    pivot = fitz.Point(306, 430)
    matrix = fitz.Matrix(1, 0, 0, 1, 0, 0).prerotate(45)
    tw.write_text(page, color=(0.82, 0.82, 0.82), opacity=0.18,
                  morph=(pivot, matrix))

    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out.getvalue()


def generate_certificate(content, template_path=None, field_map=None,
                         signature_png_path=None, signature_rect=None):
    """Single entry point the API calls after the gate returns auto_issue."""
    if template_path:
        from acord25_2016_overlay import generate_trucking_cert
        return generate_trucking_cert(
            content, template_path,
            blank_101_path=os.environ.get("ACORD101_TEMPLATE_PATH"),
            include_101=bool(getattr(content, "vehicles", None) or
                             getattr(content, "trailers", None) or
                             getattr(content, "drivers", None)),
            signature_png_path=signature_png_path,
            signature_rect=signature_rect)
    return render_fallback_certificate(content, signature_png_path)
