"""Local (code) tools available to the agent, organized one module per domain:

- `current_time`: trivial date/time tool.
- `charts`: chart generation (generate_chart) + the in-memory PNG cache served
  by the GET /invocations?chart_id=... route in start_server.py.
- `genie`: direct Genie conversations API access (genie_ask) -- a code-tool
  alternative/complement to the managed Genie MCP server wired in agent.py.
- `jobs`: Databricks Jobs execution and monitoring.
- `mail`: email notifications via an Azure Logic App (disabled until
  LOGIC_APP_MAIL_URL is configured -- see agent_server/tools/mail.py).
- `pdf`: PDF report generation, optionally sourced from a Genie answer, saved
  to a Unity Catalog volume.
- `volumes`: generic Unity Catalog Volumes upload helpers, used by `pdf`.
- `common`: health/identity tools.

Ported from template_databricks_assest_bundle_mcp's mcp_star MCP server
(src/apps/mcp_star/server/{tools.py,utils/*.py}) as agent code tools instead
of a separate MCP server -- see AGENTS.md's "Agent Code Tools" tool type.

`ALL_LOCAL_TOOLS` is what agent_server/agent.py adds to the LangChain agent
alongside the Databricks-managed MCP tools (UC functions, Genie MCP, SQL,
system.ai).
"""

from agent_server.tools.charts import generate_chart, get_cached_chart
from agent_server.tools.common import get_current_user, health
from agent_server.tools.current_time import get_current_time
from agent_server.tools.genie import genie_ask
from agent_server.tools.jobs import (
    databricks_jobs_cancel_run,
    databricks_jobs_get_run_status,
    databricks_jobs_list_jobs,
    databricks_jobs_run_job,
    databricks_jobs_run_job_and_wait,
)
from agent_server.tools.mail import send_email
from agent_server.tools.pdf import generate_pdf_from_genie, generate_pdf_to_volume

ALL_LOCAL_TOOLS = [
    get_current_time,
    generate_chart,
    health,
    get_current_user,
    genie_ask,
    send_email,
    generate_pdf_to_volume,
    generate_pdf_from_genie,
    databricks_jobs_list_jobs,
    databricks_jobs_run_job,
    databricks_jobs_get_run_status,
    databricks_jobs_run_job_and_wait,
    databricks_jobs_cancel_run,
]

__all__ = [
    "ALL_LOCAL_TOOLS",
    "get_cached_chart",
    "get_current_time",
    "generate_chart",
    "health",
    "get_current_user",
    "genie_ask",
    "send_email",
    "generate_pdf_to_volume",
    "generate_pdf_from_genie",
    "databricks_jobs_list_jobs",
    "databricks_jobs_run_job",
    "databricks_jobs_get_run_status",
    "databricks_jobs_run_job_and_wait",
    "databricks_jobs_cancel_run",
]
