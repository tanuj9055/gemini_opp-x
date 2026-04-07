"""
PDF Rendering Service.

Provides utilities to generate a PDF from plain text sections,
and to merge multiple PDFs into a single continuous stream.
"""

import io
from typing import List, Any, Tuple

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import inch
from reportlab.lib import colors

from app.logging_cfg import logger

_log = logger.getChild("pdf_renderer")

def render_section_to_pdf(section_key: str, content: Any, bid_id: str) -> bytes:
    """
    Renders pure text content or structured table data into a single PDF byte stream.
    Adds a header indicating the section and bid ID.
    """
    _log.info("Rendering section '%s' to PDF for bid_id=%s", section_key, bid_id)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=inch,
        leftMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = styles["Heading1"]
    header_style = ParagraphStyle(
        "HeaderStyle",
        parent=styles["Normal"],
        fontSize=10,
        textColor="#555555",
        spaceAfter=20,
    )
    body_style = ParagraphStyle(
        "BodyStyle",
        parent=styles["Normal"],
        fontSize=11,
        leading=14,
        spaceAfter=10,
    )

    story = []

    formatted_title = section_key.replace("_", " ").title()
    
    # Add Header
    story.append(Paragraph(f"<b>GeM Bid ID:</b> {bid_id} | <b>Section:</b> {formatted_title}", header_style))
    story.append(Paragraph(formatted_title, title_style))
    story.append(Spacer(1, 0.2 * inch))

    if isinstance(content, list) and len(content) > 0 and isinstance(content[0], dict):
        # Render as a table
        headers = list(content[0].keys())
        table_data = [[h.replace("_", " ").title() for h in headers]]
        
        for row in content:
            row_data = [str(row.get(key, "Not Specified in Bid")) for key in headers]
            # Wrap in Paragraphs for text wrapping
            wrapped_row = [Paragraph(cell.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"), body_style) for cell in row_data]
            table_data.append(wrapped_row)
        
        t = Table(table_data, colWidths=[(A4[0] - 2 * inch) / len(headers)] * len(headers))
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        story.append(t)
    else:
        # Process as string
        if not isinstance(content, str):
            content = str(content)
            
        # Clean string if empty
        if not content or content.strip() == "" or content.strip().lower() == "not provided":
            content = "Not Specified in Bid"

        # Process content by lines to retain some formatting (newlines)
        for line in content.split('\n'):
            # Escape HTML chars for ReportLab to avoid XML parsing errors
            line_clean = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            
            if line_clean.strip() == "":
                story.append(Spacer(1, 0.1 * inch))
            else:
                story.append(Paragraph(line_clean, body_style))

    try:
        doc.build(story)
    except Exception as e:
        _log.warning("Table layout failed, falling back to paragraph rendering: %s", e)
        # Fallback to paragraph rendering if table is too large for page
        story = []
        story.append(Paragraph(f"<b>GeM Bid ID:</b> {bid_id} | <b>Section:</b> {formatted_title}", header_style))
        story.append(Paragraph(formatted_title, title_style))
        story.append(Spacer(1, 0.2 * inch))
        
        if isinstance(content, list) and len(content) > 0 and isinstance(content[0], dict):
            for row in content:
                for k, v in row.items():
                    k_str = str(k).replace("_", " ").title()
                    v_str = str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    story.append(Paragraph(f"<b>{k_str}:</b> {v_str}", body_style))
                story.append(Spacer(1, 0.2 * inch))
        else:
            story.append(Paragraph("Failed to render content.", body_style))
            
        doc.build(story)
    
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

def merge_pdfs(pdf_list: List[Tuple[str, bytes]]) -> bytes:
    """
    Merge a list of (filename, PDF byte sequences) into a single PDF byte sequence.
    """
    _log.info("Merging %d PDFs", len(pdf_list))
    writer = PdfWriter()

    for filename, pdf_data in pdf_list:
        try:
            reader = PdfReader(io.BytesIO(pdf_data))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as e:
            _log.error("Failed to merge PDF part '%s': %s", filename, e)

    out_buffer = io.BytesIO()
    writer.write(out_buffer)
    merged_bytes = out_buffer.getvalue()
    out_buffer.close()

    return merged_bytes
