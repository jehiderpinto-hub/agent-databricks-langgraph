import logging
import os
from typing import AsyncGenerator, Optional

import mlflow
from databricks.sdk import WorkspaceClient
from databricks_langchain import ChatDatabricks, DatabricksMCPServer, DatabricksMultiServerMCPClient
from langchain.agents import create_agent
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
    to_chat_completions_input,
)

from agent_server.tools import ALL_LOCAL_TOOLS
from agent_server.utils import (
    get_databricks_host_from_env,
    get_session_id,
    get_user_workspace_client,
    process_agent_astream_events,
)

# ---------------------------------------------------------------------------
# Agent Instructions / System Prompt
# ---------------------------------------------------------------------------
AGENT_INSTRUCTIONS = """Eres un asistente inteligente y analista de datos experto en Databricks.

# REGLAS ESTRICTAS DE RESPUESTA Y FORMATO:
1. **NUNCA muestres JSON crudo, payloads técnicos ni metadatos de ejecución en el chat:**
   - Queda estrictamente prohibido responder con salidas directas como `{"query": "SHOW CATALOGS"} Result [{"type": "text", ...}]` o estructuras con `statement_id`, `status`, `manifest`, `data_array`, `run_id`, `life_cycle_state` u otros campos técnicos crudos.
   - Esto aplica a TODAS las herramientas (SQL, UC Functions, MCPs, Genie, Jobs, PDF, correo, etc.): siempre procesa, limpia e interpreta internamente sus resultados (incluyendo los dicts con `status`/`message` que devuelven las tools de Jobs, PDF y correo) antes de responder en lenguaje natural.

2. **Presentación Clara y Profesional (en Español):**
   - Presenta los datos de forma legible usando tablas Markdown bien estructuradas, listas con viñetas o resúmenes ejecutivos.
   - Explica de forma concisa los hallazgos y el contexto de los datos solicitados.

3. **Visualizaciones y Gráficas:**
   - Cuando el usuario solicite analizar tendencias, comparaciones, distribuciones, métricas o visualizaciones, o cuando una gráfica aporte claridad al análisis de datos numéricos o categóricos, utiliza SIEMPRE la herramienta `generate_chart`.
   - Selecciona el tipo de gráfica más adecuado (`bar`, `horizontal_bar`, `line`, `pie`, `donut`, `area`, `scatter`, `histogram`).
   - `generate_chart` devuelve una línea markdown como `![título](/invocations?chart_id=...)`. Debes incluir esa línea EXACTAMENTE como la devolvió la herramienta, sin modificarla, al inicio de tu respuesta -- de lo contrario la gráfica no se renderiza en el chat.
   - Acompaña siempre la gráfica generada con un breve análisis o conclusiones clave después de la línea de imagen.

4. **Otras herramientas disponibles:** además de SQL/UC Functions/Genie (MCP) y `generate_chart`, tienes `genie_ask` (Genie por API directa), `send_email` (correo vía Logic App), `generate_pdf_to_volume` / `generate_pdf_from_genie` (reportes PDF a un volumen de Unity Catalog) y las tools `databricks_jobs_*` (listar, ejecutar, monitorear y cancelar Jobs). Úsalas cuando la solicitud del usuario lo requiera explícitamente (ej. "envíame esto por correo", "genera un PDF con esto", "ejecuta el job X"), y resume siempre su resultado en lenguaje natural.
"""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Module-level logger used throughout this file (MCP fetch failures, etc.).
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MLflow tracing
# ---------------------------------------------------------------------------
# Autologging instruments every LangChain/LangGraph call (LLM calls, tool
# calls, agent steps) and emits an MLflow trace automatically -- no manual
# span code needed anywhere below. Traces land in the experiment configured
# via MLFLOW_EXPERIMENT_ID (see databricks.yml -> resources.apps.*.resources
# with name 'experiment', wired through config.env.MLFLOW_EXPERIMENT_ID via
# `value_from: "experiment"`).
mlflow.langchain.autolog()
# Autologging emits a noisy "please pin your MLflow version" style warning on
# every request at INFO/WARNING level; silence it so app logs stay readable.
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Service-principal Databricks client
# ---------------------------------------------------------------------------
# WorkspaceClient() with no args authenticates using the credentials injected
# into the app's runtime environment by Databricks Apps (the app's service
# principal) -- this is the identity used for every backend call (MCP tool
# calls, this client's own API calls) unless on-behalf-of-user auth is used
# instead (see `get_user_workspace_client()` in stream_handler below).
# Whatever this identity can access in Unity Catalog is what UC Function /
# Genie / Vector Search MCP tools will be able to read or execute -- grant
# permissions to it via `resources:` entries in databricks.yml, NOT by
try:
    sp_workspace_client = WorkspaceClient()
except Exception:
    sp_workspace_client = None
# Managed MCP resource configuration (env-driven, no hardcoded IDs)
# ---------------------------------------------------------------------------
# These env vars let you point the agent at different Unity Catalog
# schemas / Genie spaces per workspace (dev/staging/prod) without touching
# code -- set them in databricks.yml under config.env (see README for the
# matching `resources:` permission entries each one requires).
#
# UC_FUNCTIONS_CATALOG / UC_FUNCTIONS_SCHEMA:
#   Every SQL UDF (User-Defined Function) registered in this catalog.schema
#   becomes an agent tool automatically via the UC Functions MCP server --
#   this is how the agent runs governed "SQL queries" (each UDF wraps a
#   parameterized SQL statement) without giving it raw, ungoverned SQL access.
UC_FUNCTIONS_CATALOG = os.environ.get("UC_FUNCTIONS_CATALOG", "main")
UC_FUNCTIONS_SCHEMA = os.environ.get("UC_FUNCTIONS_SCHEMA", "default")

# GENIE_SPACE_IDS:
#   Comma-separated list of Genie Space IDs. Each Genie space becomes a small
#   toolset (ask a natural-language question, get back data/insights) backed
#   by the tables that space was configured against. Set to "unset" (the
#   default) to skip wiring Genie entirely (e.g. while the space hasn't been
#   created yet). NOTE: the literal sentinel "unset" is used instead of an
#   empty string because Databricks Apps silently drops any config.env entry
#   whose resolved value is "" (see databricks.yml).
_genie_space_ids_raw = os.environ.get("GENIE_SPACE_IDS", "unset")
GENIE_SPACE_IDS = (
    [] if _genie_space_ids_raw == "unset" else [s.strip() for s in _genie_space_ids_raw.split(",") if s.strip()]
)


def init_mcp_client(workspace_client: WorkspaceClient) -> DatabricksMultiServerMCPClient:
    """Build the set of Databricks-managed MCP servers this agent can call.

    Each `DatabricksMCPServer` below is a *managed* MCP server -- Databricks
    hosts it, and it exposes one governed capability as a set of tools. The
    agent authenticates to every one of them using `workspace_client`'s
    identity, so access control is entirely enforced by Unity Catalog / app
    resource permissions, not by anything in this file.
    """
    host_name = get_databricks_host_from_env()
    mcp_servers = [
        # --- system.ai built-in tools (code interpreter, etc.) ---
        # Exposes Databricks' built-in UC functions under the `system.ai`
        # schema, most notably `system.ai.python_exec` (a sandboxed Python
        # code interpreter tool). Safe default to always include.
        DatabricksMCPServer(
            name="system-ai",
            url=f"{host_name}/api/2.0/mcp/functions/system/ai",
            workspace_client=workspace_client,
            handle_tool_error=True,  # return tool errors to the LLM instead of crashing the agent
        ),
        # --- Unity Catalog Functions (SQL UDFs / custom "SQL query" tools) ---
        # Every function in UC_FUNCTIONS_CATALOG.UC_FUNCTIONS_SCHEMA is
        # exposed as one tool. This is the governed way to give the agent
        # "run this SQL" capability: each UDF is a vetted, parameterized SQL
        # statement (or Python UDF) registered in Unity Catalog -- the agent
        # can only call what's explicitly defined there, never arbitrary SQL.
        # Requires granting EXECUTE on the schema/functions in databricks.yml
        # (see README -> "Managed MCP tools").
        DatabricksMCPServer(
            name="uc-functions",
            url=f"{host_name}/api/2.0/mcp/functions/{UC_FUNCTIONS_CATALOG}/{UC_FUNCTIONS_SCHEMA}",
            workspace_client=workspace_client,
            handle_tool_error=True,
        ),
        # --- SQL MCP server (run governed SQL against Unity Catalog) ---
        # Exposes execute_sql / execute_sql_read_only / poll_sql_result --
        # unlike the UC Functions server above, this lets the agent write
        # arbitrary SQL rather than only calling pre-vetted UDFs. The URL
        # takes no warehouse ID; Databricks routes execution to a warehouse
        # the caller's identity has CAN_USE on (see the 'sql_warehouse'
        # resource entry in databricks.yml, which grants that to the app's
        # service principal).
        DatabricksMCPServer(
            name="sql",
            url=f"{host_name}/api/2.0/mcp/sql",
            workspace_client=workspace_client,
            handle_tool_error=True,
        ),
    ]

    # --- Genie Spaces (natural-language SQL over curated tables) ---
    # One MCP server per Genie space -- lets the agent delegate a
    # data question to Genie (which writes and runs the SQL against the
    # tables that space was configured with, and returns a natural-language
    # answer). Skipped entirely if GENIE_SPACE_IDS is empty.
    for space_id in GENIE_SPACE_IDS:
        mcp_servers.append(
            DatabricksMCPServer(
                name=f"genie-{space_id}",
                url=f"{host_name}/api/2.0/mcp/genie/{space_id}",
                workspace_client=workspace_client,
                handle_tool_error=True,
                timeout=60.0,  # Genie can take longer than simple UC function calls
            )
        )

    return DatabricksMultiServerMCPClient(mcp_servers)


async def init_agent(workspace_client: Optional[WorkspaceClient] = None):
    """Assemble the LangGraph agent: local tools + MCP tools + LLM.

    Called once per request in `stream_handler` below (cheap: tool-fetching
    is the only I/O, the LLM client itself is stateless).
    """
    tools = list(ALL_LOCAL_TOOLS)

    ws_client = workspace_client or sp_workspace_client
    if ws_client is None:
        try:
            ws_client = WorkspaceClient()
        except Exception:
            logger.warning("Could not initialize WorkspaceClient for MCP tools.", exc_info=True)

    if ws_client:
        mcp_client = init_mcp_client(ws_client)
        try:
            tools.extend(await mcp_client.get_tools())
        except Exception:
            logger.warning("Failed to fetch MCP tools. Continuing without MCP tools.", exc_info=True)

    return create_agent(
        tools=tools,
        # `endpoint` names a Databricks Model Serving endpoint (a foundation
        # model or a custom-served model) -- it must exist in the *target*
        # workspace; endpoint names are not shared across workspaces.
        model=ChatDatabricks(
            endpoint="databricks-claude-sonnet-5",
            # Claude's "extended thinking" is on by default for this
            # endpoint and returns reasoning blocks with an empty text
            # summary (only an opaque signature). The chat UI renders any
            # unhandled output item literally, so those blocks show up as
            # noise before the real answer. Disabling thinking avoids
            # emitting them. `extra_params` is spread as top-level kwargs
            # into the OpenAI client call, so provider-specific params (like
            # Anthropic's `thinking`) must be nested under `extra_body`,
            # which the OpenAI client forwards to the API unvalidated.
            extra_params={"extra_body": {"thinking": {"type": "disabled"}}},
        ),
    )


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    """Non-streaming entrypoint: drains stream_handler and returns the final
    response in one shot. Required by the `@invoke()` decorator's contract
    (see MLflow's ResponsesAgent spec) for clients that don't want SSE.
    """
    outputs = [
        event.item
        async for event in stream_handler(request)
        if event.type == "response.output_item.done"
    ]
    return ResponsesAgentResponse(output=outputs)


@stream()
async def stream_handler(
    request: ResponsesAgentRequest,
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Streaming entrypoint (Server-Sent Events). This is the source of
    truth handler -- `invoke_handler` above just consumes this one.
    """
    # Tags every span in this request's MLflow trace with the conversation's
    # session id (if the client sent one), so traces for the same
    # conversation can be grouped/filtered in the MLflow UI.
    if session_id := get_session_id(request):
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})

    # On-behalf-of-user auth: every MCP call (UC Functions, SQL, Genie) and
    # every local tool that touches Unity Catalog (agent_server/tools/genie.py,
    # pdf.py) runs with the *calling user's* permissions, not the app's
    # service principal -- so a user only ever sees/does what they're
    # already allowed to in Unity Catalog. Requires:
    #   1. `user_api_scopes` declared on the app resource in databricks.yml
    #      (sql, genie, unity-catalog, files).
    #   2. A workspace admin approving the app's requested OAuth scopes the
    #      first time (Databricks Apps -> app -> Authorization).
    # Falls back to the service principal (sp_workspace_client) if the
    # request carries no forwarded user token (e.g. local `uv run start-app`
    # without going through Databricks Apps) -- see init_agent().
    try:
        user_client = get_user_workspace_client()
    except Exception:
        logger.warning("Could not build on-behalf-of-user WorkspaceClient; falling back to service principal.")
        user_client = None
    agent = await init_agent(workspace_client=user_client)
    user_messages = to_chat_completions_input([i.model_dump() for i in request.input])
    # Prepend system instructions to establish persona, formatting rules, and visualization guidelines
    messages = {"messages": [{"role": "system", "content": AGENT_INSTRUCTIONS}] + user_messages}

    async for event in process_agent_astream_events(
        agent.astream(input=messages, stream_mode=["updates", "messages"])
    ):
        yield event
