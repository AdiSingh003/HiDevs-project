"""Render a compiled contract to PDF with ReportLab (platypus)."""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6b7280")
ACCENT = colors.HexColor("#b45309")
RULE = colors.HexColor("#e5e7eb")
FILL = colors.HexColor("#f9fafb")


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22,
                                textColor=INK, alignment=TA_LEFT, spaceAfter=2),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"], fontSize=10, leading=13, textColor=MUTED),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, leading=15,
                             textColor=ACCENT, spaceBefore=10, spaceAfter=4),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=9.5, leading=12,
                             textColor=INK, spaceBefore=6, spaceAfter=2),
        "body": ParagraphStyle("body", parent=base["Normal"], fontName="Helvetica", fontSize=8.8, leading=12,
                               textColor=INK),
        "small": ParagraphStyle("small", parent=base["Normal"], fontName="Helvetica", fontSize=7.4, leading=9.5,
                                textColor=MUTED),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontName="Helvetica", fontSize=8, leading=10,
                               textColor=INK),
        "cellb": ParagraphStyle("cellb", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8, leading=10,
                                textColor=INK),
    }


def _p(text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def _table(rows: list[list[Any]], widths: list[float], header: bool = True) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), FILL), ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK)]
    t.setStyle(TableStyle(style))
    return t


def _fmt_value(contract: dict[str, Any], key: str, value: float) -> str:
    issue = next((i for i in contract.get("issues", []) if i["key"] == key), None)
    decimals = issue["decimals"] if issue else 2
    unit = issue["unit"] if issue else ""
    number = f"{value:,.{decimals}f}"
    if unit in ("USD", "EUR", "INR", "GBP"):
        return f"{unit} {number}"
    return f"{number}{unit}" if unit == "%" else f"{number} {unit}".strip()


def render_contract_pdf(contract: dict[str, Any]) -> bytes:
    s = _styles()
    buf = io.BytesIO()
    integrity = contract["integrity"]
    short_hash = integrity["content_hash"][:16]

    def footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(18 * mm, 10 * mm, f"{contract['contract_id']} v{contract['version']}  |  sha256 {short_hash}...  "
                                            f"|  {contract['status'].replace('_', ' ').upper()}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=contract["title"], author="Legal Arbiter (Safe AI)",
                            subject=contract["contract_id"])
    ct = contract["commercial_terms"]
    parties = contract["parties"]
    width = A4[0] - 36 * mm
    story: list[Any] = []

    story.append(_p("SUPPLY AGREEMENT" + (f" - AMENDMENT {contract['version'] - 1}" if contract.get("amendment") else ""),
                    s["title"]))
    story.append(_p(contract["title"], s["subtitle"]))
    story.append(Spacer(1, 6))
    meta = [
        [_p("Contract ID", s["cellb"]), _p(contract["contract_id"], s["cell"]),
         _p("Version", s["cellb"]), _p(contract["version"], s["cell"])],
        [_p("Status", s["cellb"]), _p(contract["status"].replace("_", " "), s["cell"]),
         _p("Effective date", s["cellb"]), _p(contract["effective_date"], s["cell"])],
        [_p("RFQ", s["cellb"]), _p(contract["rfq"]["reference"], s["cell"]),
         _p("Content hash", s["cellb"]), _p(short_hash + "...", s["cell"])],
    ]
    if contract.get("parent_hash"):
        meta.append([_p("Amends", s["cellb"]), _p(contract["parent_hash"][:16] + "...", s["cell"]), "", ""])
    story.append(_table(meta, [0.16 * width, 0.34 * width, 0.16 * width, 0.34 * width], header=False))

    story.append(_p("Parties", s["h2"]))
    rows = [[_p("Buyer", s["cellb"]), _p("Supplier", s["cellb"])]]
    for role_rows in zip(
        [parties["buyer"]["name"], parties["buyer"].get("address", ""),
         f"Signatory: {parties['buyer']['signatory_name']}, {parties['buyer']['signatory_title']}"],
        [parties["supplier"]["name"] + (" (MSME)" if parties["supplier"].get("is_msme") else ""),
         parties["supplier"].get("address", ""),
         f"Signatory: {parties['supplier']['signatory_name']}, {parties['supplier']['signatory_title']}"],
    ):
        rows.append([_p(role_rows[0], s["cell"]), _p(role_rows[1], s["cell"])])
    story.append(_table(rows, [width / 2, width / 2]))

    story.append(_p("Key commercial terms", s["h2"]))
    rows = [[_p("Term", s["cellb"]), _p("Agreed value", s["cellb"])],
            [_p("Goods", s["cell"]), _p(f"{ct['quantity']:,.0f} {ct['quantity_unit']} of {ct['item']}", s["cell"])],
            [_p("Contract value", s["cell"]), _p(f"{ct['currency']} {ct['total_value']:,.2f}", s["cell"])],
            [_p("Delivery", s["cell"]), _p(f"{ct['incoterm']} - {ct['delivery_location']}", s["cell"])]]
    labels = {i["key"]: i["label"] for i in contract.get("issues", [])}
    for key, value in contract["terms"].items():
        rows.append([_p(labels.get(key, key), s["cell"]), _p(_fmt_value(contract, key, value), s["cell"])])
    story.append(_table(rows, [0.35 * width, 0.65 * width]))

    amendment = contract.get("amendment")
    if amendment:
        story.append(_p("Amendment record", s["h2"]))
        ev = amendment.get("event", {})
        story.append(_p(f"Trigger: {ev.get('event_type', '')} reported by {ev.get('source', '')} "
                        f"({ev.get('description', '')}). {amendment.get('assessment', {}).get('reason', '')}", s["body"]))
        rows = [[_p("Term", s["cellb"]), _p("Previous", s["cellb"]), _p("Amended", s["cellb"])]]
        for key, change in amendment.get("changed_terms", {}).items():
            rows.append([_p(labels.get(key, key), s["cell"]), _p(_fmt_value(contract, key, change["from"]), s["cell"]),
                         _p(_fmt_value(contract, key, change["to"]), s["cell"])])
        story.append(_table(rows, [0.4 * width, 0.3 * width, 0.3 * width]))
        if amendment.get("ld_waiver"):
            story.append(Spacer(1, 3))
            story.append(_p(amendment["ld_waiver"], s["body"]))

    story.append(_p("Clauses", s["h2"]))
    for i, clause in enumerate(contract["clauses"], start=1):
        story.append(KeepTogether([_p(f"{i}. {clause['title']}", s["h3"]), _p(clause["text"], s["body"])]))

    story.append(_p("Schedule A - Executable SLA rules", s["h2"]))
    rows = [[_p("Rule", s["cellb"]), _p("Trigger", s["cellb"]), _p("Formula", s["cellb"]), _p("Parameters", s["cellb"])]]
    for rule in contract["executable"]["sla_rules"]:
        params = ", ".join(f"{k}={v}" for k, v in rule["params"].items())
        rows.append([_p(rule["id"], s["cell"]), _p(rule["trigger"], s["cell"]), _p(rule["formula"], s["cell"]),
                     _p(params, s["cell"])])
    story.append(_table(rows, [0.14 * width, 0.24 * width, 0.40 * width, 0.22 * width]))

    neg = contract["negotiation"]
    legal = contract["legal"]
    drafting = contract["drafting"]
    story.append(_p("Schedule B - Negotiation and compliance record", s["h2"]))
    rows = [[_p("Item", s["cellb"]), _p("Record", s["cellb"])]]
    for label, value in [
        ("Negotiation", f"{neg.get('negotiation_id')} - {neg.get('outcome', '').replace('_', ' ')} in "
                        f"{neg.get('rounds')} rounds"),
        ("Guardrail interventions", f"{neg.get('interventions', 0)} (blocked moves: {neg.get('blocked_moves', 0)}, "
                                    f"redactions: {neg.get('redactions', 0)})"),
        ("Rulebook", f"{legal['rulebook_version']} - {legal['jurisdiction_name']}: {', '.join(legal['rules_applied'])}"),
        ("Audit chain head", neg.get("audit_head", "")),
        ("Envelope commitments", "; ".join(f"{k}: {v[:23]}..." for k, v in neg.get("envelope_commitments", {}).items())),
        ("Drafting", f"{drafting['engine']} ({drafting['model']}); legal review "
                     f"{'approved' if drafting['review'].get('approved') else 'with findings'}"),
    ]:
        rows.append([_p(label, s["cell"]), _p(value, s["cell"])])
    story.append(_table(rows, [0.3 * width, 0.7 * width]))
    story.append(Spacer(1, 4))
    story.append(_p("Executive summary: " + drafting.get("executive_summary", ""), s["body"]))

    story.append(_p("Signatures (Ed25519)", s["h2"]))
    rows = [[_p("Role", s["cellb"]), _p("Signatory", s["cellb"]), _p("Key fingerprint", s["cellb"]),
             _p("Signature", s["cellb"]), _p("Signed at (UTC)", s["cellb"])]]
    for sig in integrity["signatures"]:
        rows.append([_p(sig["role"].replace("_", " "), s["cell"]), _p(f"{sig['name']}, {sig['title']}", s["cell"]),
                     _p(sig["fingerprint"], s["cell"]), _p(sig["signature"][:28] + "...", s["cell"]),
                     _p(sig["signed_at"][:19].replace("T", " "), s["cell"])])
    if contract["status"] == "pending_cfo_approval":
        rows.append([_p("buyer cfo", s["cell"]), _p("PENDING - value exceeds delegated authority", s["cell"]), "", "", ""])
    story.append(_table(rows, [0.13 * width, 0.30 * width, 0.17 * width, 0.22 * width, 0.18 * width]))
    story.append(Spacer(1, 6))
    story.append(_p(f"Each signature covers '{contract['contract_id']}|v{contract['version']}|<content hash>'. "
                    f"Full content hash: {integrity['content_hash']}", s["small"]))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
