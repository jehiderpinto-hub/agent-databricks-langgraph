"""Direct consumption of the Genie (Databricks AI/BI Genie) conversations API.

Ported from template_databricks_assest_bundle_mcp
(src/apps/mcp_star/server/utils/genie.py and the genie_ask tool in server/tools.py).

This is a *code tool* alternative to the managed Genie MCP server wired in
agent_server/agent.py (GENIE_SPACE_IDS / init_mcp_client) -- it calls the
Genie SDK API directly rather than going through Databricks' MCP endpoint,
which is what generate_pdf_from_genie (agent_server/tools/pdf.py) needs to
combine a Genie answer with PDF generation in one tool call.

All functions take the WorkspaceClient and space_id as explicit arguments
(never a global), so the same code can target different Genie spaces from
different tool calls.
"""

import logging
from datetime import timedelta

from databricks.sdk import WorkspaceClient
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120


def start_conversation(
    w: WorkspaceClient,
    space_id: str,
    question: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
):
    """Starts a new conversation in a Genie space and waits for the first response."""
    return w.genie.start_conversation_and_wait(
        space_id=space_id,
        content=question,
        timeout=timedelta(seconds=timeout_seconds),
    )


def send_message(
    w: WorkspaceClient,
    space_id: str,
    conversation_id: str,
    question: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
):
    """Sends a follow-up message in an existing conversation and waits for the response."""
    return w.genie.create_message_and_wait(
        space_id=space_id,
        conversation_id=conversation_id,
        content=question,
        timeout=timedelta(seconds=timeout_seconds),
    )


def _query_result_to_markdown_table(statement_response) -> str:
    """Converts a Genie query's tabular result to a markdown table."""
    manifest = getattr(statement_response, "manifest", None)
    result = getattr(statement_response, "result", None)
    if not manifest or not result or not result.data_array:
        return ""

    columns = [col.name for col in manifest.schema.columns]
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    rows = [
        "| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |"
        for row in result.data_array
    ]
    return "\n".join([header, separator, *rows])


def extract_message_content(
    w: WorkspaceClient,
    space_id: str,
    message,
    include_query_results: bool = True,
) -> dict:
    """Extracts text and (optionally) tabular results from a Genie message.

    A Genie message can carry several "attachments": free text and/or a
    generated SQL query with its result. This normalizes all of that into a
    plain dict, ready to feed into pdf.build_pdf_bytes() or return directly.

    Returns:
        dict with conversation_id, message_id, text (concatenated free text),
        and query_results (list of {query, description, table_markdown}).
    """
    text_parts = []
    query_results = []

    for attachment in message.attachments or []:
        if attachment.text and attachment.text.content:
            text_parts.append(attachment.text.content)

        if attachment.query and include_query_results:
            query_result = w.genie.get_message_attachment_query_result(
                space_id=space_id,
                conversation_id=message.conversation_id,
                message_id=message.id,
                attachment_id=attachment.attachment_id,
            )
            query_results.append(
                {
                    "query": attachment.query.query,
                    "description": attachment.query.description,
                    "table_markdown": _query_result_to_markdown_table(query_result.statement_response),
                }
            )

    return {
        "conversation_id": message.conversation_id,
        "message_id": message.id,
        "text": "\n\n".join(text_parts),
        "query_results": query_results,
    }


def ask_genie(
    w: WorkspaceClient,
    space_id: str,
    question: str,
    conversation_id: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    include_query_results: bool = True,
) -> dict:
    """High-level helper: asks Genie a question (new or follow-up conversation)
    and returns the already-extracted response. See extract_message_content()."""
    if conversation_id:
        message = send_message(w, space_id, conversation_id, question, timeout_seconds)
    else:
        message = start_conversation(w, space_id, question, timeout_seconds)

    return extract_message_content(w, space_id, message, include_query_results)


def genie_response_to_pdf_content(genie_answer: dict) -> str:
    """Converts an ask_genie() response to the section-formatted text expected
    by pdf.build_pdf_bytes()."""
    blocks = []
    if genie_answer.get("text"):
        blocks.append(genie_answer["text"])

    for i, qr in enumerate(genie_answer.get("query_results", []), start=1):
        heading = f"## {qr['description'] or f'Resultado {i}'}"
        blocks.append(f"{heading}\n\n{qr['table_markdown']}")

    return "\n\n".join(blocks)


@tool
def genie_ask(
    space_id: str,
    question: str,
    conversation_id: str = "",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    include_query_results: bool = True,
) -> dict:
    """Sends a natural-language question to a Genie Space and returns its answer.

    This calls the Genie conversations API directly (code tool). For most data
    questions, prefer the managed Genie MCP tools already wired for the
    configured GENIE_SPACE_IDS -- use this one when you need explicit control
    over conversation_id/timeout, or to target a Genie space by id at
    call-time rather than a pre-configured one.

    Args:
        space_id: Genie Space identifier (visible in the space's URL).
        question: Natural-language question for Genie.
        conversation_id: If given, continues that conversation instead of starting a new one.
        timeout_seconds: Max time to wait for Genie to finish responding.
        include_query_results: If True, includes result tables for any queries Genie generates.

    Returns:
        dict with status, conversation_id, message_id, text, and query_results
        (list of {query, description, table_markdown}).
    """
    try:
        w = WorkspaceClient()
        answer = ask_genie(
            w,
            space_id=space_id,
            question=question,
            conversation_id=conversation_id,
            timeout_seconds=timeout_seconds,
            include_query_results=include_query_results,
        )
        return {"status": "success", **answer}
    except Exception as e:
        logger.exception("Error querying Genie")
        return {"status": "error", "error": str(e), "message": f"Error al consultar Genie: {str(e)}"}
