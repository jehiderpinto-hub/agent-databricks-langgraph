"""PDF report generation and upload to a Unity Catalog Volume.

Ported from template_databricks_assest_bundle_mcp (src/apps/mcp_star/server/utils/pdf.py
and the pdf_generator_* tools in server/tools.py).
"""

import io
import logging
import os
from datetime import datetime
from xml.sax.saxutils import escape as _xml_escape

from databricks.sdk import WorkspaceClient
from langchain_core.tools import tool

from agent_server.tools import genie, volumes

logger = logging.getLogger(__name__)

# PDF_TARGET_VOLUME:
#   Default Unity Catalog volume path PDFs are saved to when the tool call
#   doesn't specify one explicitly. Parametrized like UC_FUNCTIONS_CATALOG --
#   set via databricks.yml config.env, grant WRITE_VOLUME to the app's
#   service principal via a matching uc_securable resource.
PDF_TARGET_VOLUME = os.environ.get("PDF_TARGET_VOLUME", "unset")

# Palette / typography defaults. Override per-call via `style_overrides`
# instead of editing this module.
DEFAULT_STYLE = {
    "title_color": "#1B365D",
    "subtitle_color": "#666666",
    "heading_color": "#2E5B88",
    "body_color": "#222222",
    "rule_color": "#DDDDDD",
    "page_size": "letter",  # "letter" | "A4"
    "margin_pt": 54,
}


def _build_fallback_pdf(title: str, content: str, author: str = "") -> bytes:
    """Minimal dependency-free PDF 1.4 generator, used if reportlab is unavailable."""
    lines = [f"{title.upper()}", f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    if author:
        lines.insert(1, f"Autor: {author}")
    lines.append("")
    lines.extend(content.splitlines())

    text_stream = "BT /F1 12 Tf 50 750 Td 14 TL\n"
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        text_stream += f"({escaped}) '\n"
    text_stream += "ET\n"

    encoded_text_stream = text_stream.encode("latin-1", errors="replace")
    stream_len = len(encoded_text_stream)

    stream_obj = (
        f"4 0 obj\n<</Length {stream_len}>>\nstream\n".encode("latin-1")
        + encoded_text_stream
        + b"endstream\nendobj\n"
    )

    pdf_parts = [
        b"%PDF-1.4\n",
        b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n",
        b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n",
        b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>> endobj\n",
        stream_obj,
        b"5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n",
        b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000224 00000 n \n0000000350 00000 n \n",
        b"trailer <</Size 6 /Root 1 0 R>>\nstartxref\n430\n%%EOF\n",
    ]
    return b"".join(pdf_parts)


def build_pdf_bytes(
    title: str,
    content: str,
    author: str = "",
    style_overrides: dict = None,
) -> bytes:
    """Renders a formatted PDF document from a title + body content.

    Content supports plain text with blank-line-separated paragraphs, plus
    two heading levels via markdown-like prefixes: '# Section title' and
    '## Subtitle'.

    Falls back to a dependency-free minimal generator (limited styling) if
    reportlab isn't installed.
    """
    style = {**DEFAULT_STYLE, **(style_overrides or {})}

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate

        buffer = io.BytesIO()
        margin = style["margin_pt"]
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4 if style["page_size"].upper() == "A4" else letter,
            rightMargin=margin,
            leftMargin=margin,
            topMargin=margin,
            bottomMargin=margin,
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "DocTitle",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            textColor=colors.HexColor(style["title_color"]),
            spaceAfter=6,
        )

        subtitle_style = ParagraphStyle(
            "DocSubtitle",
            parent=styles["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor(style["subtitle_color"]),
            spaceAfter=10,
        )

        heading2_style = ParagraphStyle(
            "DocHeading2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            textColor=colors.HexColor(style["heading_color"]),
            spaceBefore=12,
            spaceAfter=6,
        )

        body_style = ParagraphStyle(
            "DocBody",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=colors.HexColor(style["body_color"]),
            spaceAfter=8,
        )

        story = []

        story.append(Paragraph(_xml_escape(title), title_style))

        meta_items = []
        if author:
            meta_items.append(f"Autor: {_xml_escape(author)}")
        meta_items.append(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        story.append(Paragraph(" | ".join(meta_items), subtitle_style))
        story.append(
            HRFlowable(width="100%", thickness=1, color=colors.HexColor(style["rule_color"]), spaceAfter=14)
        )

        # Paragraphs/sections. Content is escaped (& < >) before being handed
        # to Paragraph: ReportLab treats the text as mini-XML, and unescaped
        # text that "looks like" a tag (e.g. an email address) gets silently
        # dropped otherwise.
        for block in content.split("\n\n"):
            block = block.strip()
            if not block:
                continue
            if block.startswith("## "):
                story.append(Paragraph(_xml_escape(block[3:].strip()), heading2_style))
            elif block.startswith("# "):
                story.append(Paragraph(_xml_escape(block[2:].strip()), title_style))
            else:
                formatted = _xml_escape(block).replace("\n", "<br/>")
                story.append(Paragraph(formatted, body_style))

        doc.build(story)
        return buffer.getvalue()

    except ImportError:
        return _build_fallback_pdf(title, content, author)


@tool
def generate_pdf_to_volume(
    title: str,
    content: str,
    target_volume: str = "",
    filename: str = "",
    volume_path: str = "",
    catalog: str = "",
    uc_schema: str = "",
    volume: str = "",
    author: str = "",
    overwrite: bool = True,
) -> dict:
    """Generates a PDF report from a title and content, and saves it to a Unity Catalog volume.

    Args:
        title: Main title of the PDF document.
        content: Text content (supports line breaks and '# '/'## ' section headings).
        target_volume: Destination Unity Catalog volume path. Defaults to the PDF_TARGET_VOLUME env var.
        filename: PDF filename (e.g. 'report.pdf'). Auto-generated from the title + timestamp if omitted.
        volume_path: Full file path in the volume (e.g. '/Volumes/cat/sch/vol/file.pdf'). Overrides target_volume/filename.
        catalog: Unity Catalog catalog name (optional if using target_volume or volume_path).
        uc_schema: Unity Catalog schema name (optional if using target_volume or volume_path).
        volume: Unity Catalog volume name (optional if using target_volume or volume_path).
        author: Report author/issuing entity (optional).
        overwrite: If True (default), overwrites the file if it already exists.

    Returns:
        dict with status, volume_path, size_bytes and message.
    """
    if not title.strip() or not content.strip():
        return {
            "status": "error",
            "error": "Missing parameters",
            "message": "You must provide at least 'title' and 'content' to generate the PDF document.",
        }
    if PDF_TARGET_VOLUME == "unset" and not any([target_volume, volume_path, catalog and uc_schema and volume]):
        return {
            "status": "error",
            "error": "PDF_TARGET_VOLUME not configured",
            "message": (
                "No destination volume was given and PDF_TARGET_VOLUME isn't set. "
                "Pass 'target_volume'/'volume_path'/'catalog'+'uc_schema'+'volume', "
                "or configure PDF_TARGET_VOLUME."
            ),
        }

    try:
        target_path = volumes.normalize_volume_path(
            default_volume=PDF_TARGET_VOLUME,
            volume_path=volume_path,
            target_volume=target_volume,
            catalog=catalog,
            schema=uc_schema,
            volume=volume,
            filename=filename,
            default_filename_base=volumes.slugify(title),
        )

        pdf_bytes = build_pdf_bytes(title=title, content=content, author=author)

        # Uploaded with the app's service-principal identity, not the calling
        # user's: writing to a volume is a system operation, and the forwarded
        # user token only carries whatever scope the app declares in
        # user_api_scopes (needs account-admin approval to widen).
        w = WorkspaceClient()
        volumes.upload_bytes_to_volume(w, target_path, pdf_bytes, overwrite)

        return {
            "status": "success",
            "message": f"PDF generado y guardado exitosamente en el volumen '{target_path}'.",
            "volume_path": target_path,
            "size_bytes": len(pdf_bytes),
            "title": title,
            "author": author if author else None,
        }
    except Exception as e:
        logger.exception("Error generating/uploading PDF")
        return {
            "status": "error",
            "error": str(e),
            "message": f"Error al generar o guardar el PDF en el volumen: {str(e)}",
        }


@tool
def generate_pdf_from_genie(
    space_id: str,
    question: str,
    conversation_id: str = "",
    title: str = "",
    author: str = "",
    target_volume: str = "",
    filename: str = "",
    volume_path: str = "",
    catalog: str = "",
    uc_schema: str = "",
    volume: str = "",
    overwrite: bool = True,
    timeout_seconds: int = 120,
) -> dict:
    """Asks a Genie Space a question, builds a PDF from the answer (text + result tables),
    and saves it to a Unity Catalog volume.

    Combines genie_ask (agent_server/tools/genie.py) with PDF generation and
    volume upload, without exposing the caller to each layer's details.

    Args:
        space_id: Genie Space identifier (required).
        question: Natural-language question for Genie (required).
        conversation_id: If given, continues an existing conversation instead of starting a new one.
        title: PDF title. Defaults to the question if omitted.
        author: Report author/issuing entity (optional).
        target_volume: Destination Unity Catalog volume path. Defaults to the PDF_TARGET_VOLUME env var.
        filename: PDF filename. Auto-generated if omitted.
        volume_path: Full file path in the volume. Overrides target_volume/filename.
        catalog: Unity Catalog catalog name (optional if using target_volume or volume_path).
        uc_schema: Unity Catalog schema name (optional if using target_volume or volume_path).
        volume: Unity Catalog volume name (optional if using target_volume or volume_path).
        overwrite: If True (default), overwrites the file if it already exists.
        timeout_seconds: Max time to wait for Genie to finish responding.

    Returns:
        dict with status, volume_path, conversation_id, message_id and message.
    """
    if not space_id.strip() or not question.strip():
        return {
            "status": "error",
            "error": "Missing parameters",
            "message": "You must provide 'space_id' and 'question'.",
        }
    if PDF_TARGET_VOLUME == "unset" and not any([target_volume, volume_path, catalog and uc_schema and volume]):
        return {
            "status": "error",
            "error": "PDF_TARGET_VOLUME not configured",
            "message": (
                "No destination volume was given and PDF_TARGET_VOLUME isn't set. "
                "Pass 'target_volume'/'volume_path'/'catalog'+'uc_schema'+'volume', "
                "or configure PDF_TARGET_VOLUME."
            ),
        }

    try:
        w = WorkspaceClient()

        genie_answer = genie.ask_genie(
            w,
            space_id=space_id,
            question=question,
            conversation_id=conversation_id,
            timeout_seconds=timeout_seconds,
        )
        pdf_content = genie.genie_response_to_pdf_content(genie_answer)
        pdf_title = title.strip() if title.strip() else question

        target_path = volumes.normalize_volume_path(
            default_volume=PDF_TARGET_VOLUME,
            volume_path=volume_path,
            target_volume=target_volume,
            catalog=catalog,
            schema=uc_schema,
            volume=volume,
            filename=filename,
            default_filename_base=volumes.slugify(pdf_title),
        )

        pdf_bytes = build_pdf_bytes(title=pdf_title, content=pdf_content, author=author)
        volumes.upload_bytes_to_volume(w, target_path, pdf_bytes, overwrite)

        return {
            "status": "success",
            "message": f"PDF generado a partir de la respuesta de Genie y guardado en '{target_path}'.",
            "volume_path": target_path,
            "conversation_id": genie_answer["conversation_id"],
            "message_id": genie_answer["message_id"],
            "size_bytes": len(pdf_bytes),
        }
    except Exception as e:
        logger.exception("Error generating PDF from Genie")
        return {"status": "error", "error": str(e), "message": f"Error al generar el PDF a partir de Genie: {str(e)}"}
