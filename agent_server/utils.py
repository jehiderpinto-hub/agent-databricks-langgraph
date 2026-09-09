import logging
from typing import Any, AsyncGenerator, AsyncIterator, Optional
from uuid import uuid4

from databricks.sdk import WorkspaceClient
from databricks_langchain.chat_models import json
from langchain.messages import AIMessageChunk, ToolMessage
from mlflow.genai.agent_server import get_request_headers
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentStreamEvent,
    create_text_delta,
    create_text_output_item,
    output_to_responses_items_stream,
)

# Tools whose output must be rendered as markdown (e.g. embedded chart images)
# rather than shown as raw text inside the collapsed tool-call panel. Their
# ToolMessage content is injected directly as an assistant message item,
# bypassing the LLM (which can't reliably reproduce a multi-KB base64 string).
MARKDOWN_RENDERED_TOOLS = {"generate_chart"}


def get_session_id(request: ResponsesAgentRequest) -> str | None:
    if request.context and request.context.conversation_id:
        return request.context.conversation_id
    if request.custom_inputs and isinstance(request.custom_inputs, dict):
        return request.custom_inputs.get("session_id")
    return None


def get_user_workspace_client() -> WorkspaceClient:
    token = get_request_headers().get("x-forwarded-access-token")
    return WorkspaceClient(token=token, auth_type="pat")


def get_databricks_host_from_env() -> Optional[str]:
    try:
        w = WorkspaceClient()
        return w.config.host
    except Exception as e:
        logging.exception(f"Error getting databricks host from env: {e}")
        return None


async def process_agent_astream_events(
    async_stream: AsyncIterator[Any],
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """
    Generic helper to process agent stream events and yield ResponsesAgentStreamEvent objects.

    Args:
        async_stream: The async iterator from agent.astream()
    """
    async for event in async_stream:
        if event[0] == "updates":
            for node_data in event[1].values():
                if len(node_data.get("messages", [])) > 0:
                    for msg in node_data["messages"]:
                        if isinstance(msg, ToolMessage) and not isinstance(msg.content, str):
                            msg.content = json.dumps(msg.content)
                        if isinstance(msg, ToolMessage) and msg.name in MARKDOWN_RENDERED_TOOLS:
                            item_id = str(uuid4())
                            # Register the item first (empty content) so the client
                            # initializes it as a text part before it sees the "done"
                            # event -- without this, a "done"-only item with no prior
                            # delta/added event isn't reliably recognized as renderable
                            # markdown text by the frontend's streaming parser.
                            yield ResponsesAgentStreamEvent(
                                type="response.output_item.added",
                                item=create_text_output_item(text="", id=item_id),
                            )
                            yield ResponsesAgentStreamEvent(
                                **create_text_delta(delta=msg.content, item_id=item_id)
                            )
                            yield ResponsesAgentStreamEvent(
                                type="response.output_item.done",
                                item=create_text_output_item(text=msg.content, id=item_id),
                            )
                    for item in output_to_responses_items_stream(node_data["messages"]):
                        yield item
        elif event[0] == "messages":
            try:
                chunk = event[1][0]
                if isinstance(chunk, AIMessageChunk) and (content := chunk.content):
                    yield ResponsesAgentStreamEvent(
                        **create_text_delta(delta=content, item_id=chunk.id)
                    )
            except Exception as e:
                logging.exception(f"Error processing agent stream event: {e}")
