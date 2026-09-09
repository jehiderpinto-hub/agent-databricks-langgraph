"""Health/identity tools, not tied to any particular domain.

Ported from template_databricks_assest_bundle_mcp (server/tools.py).
"""

import logging

from langchain_core.tools import tool

from agent_server.utils import get_user_workspace_client

logger = logging.getLogger(__name__)


@tool
def health() -> dict:
    """Checks that the agent's local tool layer is up and responding."""
    return {"status": "healthy", "message": "agent-databricks-langgraph tools are healthy."}


@tool
def get_current_user() -> dict:
    """Returns the authenticated user making the current request to the agent."""
    try:
        w = get_user_workspace_client()
        user = w.current_user.me()
        return {
            "display_name": user.display_name,
            "user_name": user.user_name,
            "active": user.active,
        }
    except Exception as e:
        logger.exception("Error retrieving current user")
        return {"error": str(e), "message": "Failed to retrieve user information"}
