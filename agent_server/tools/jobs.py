"""Databricks Jobs execution and monitoring via the Jobs API.

Ported from template_databricks_assest_bundle_mcp
(src/apps/mcp_star/server/utils/jobs.py and the databricks_jobs_* tools in server/tools.py).

Pure helper functions take the WorkspaceClient as an explicit argument (never
instantiate it internally), so the tools below decide which identity runs --
currently always the app's service principal (see each @tool).

Unlike agent_server/tools/genie.py and pdf.py, these stay on the service
principal rather than on-behalf-of-user: Job permissions are workspace-level
ACLs, not Unity Catalog grants, and Databricks Apps' documented
`user_api_scopes` don't include a Jobs API scope as of this writing. Grant
the service principal permission on whichever job(s) it should be able to
run/monitor/cancel directly on the job (Job permissions in the workspace),
not via databricks.yml.
"""

import logging
import json
import os
import time

from databricks.sdk import WorkspaceClient
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

DEFAULT_WAIT_TIMEOUT_SECONDS = 300
DEFAULT_POLL_INTERVAL_SECONDS = 5
EMAIL_DELIVERY_JOB_ID = int(os.environ.get("EMAIL_DELIVERY_JOB_ID", "1043398432719286"))

_TERMINAL_STATES = ("TERMINATED", "SKIPPED", "INTERNAL_ERROR")


def run_to_dict(run) -> dict:
    """Serializes an SDK `Run` object to a plain, JSON-serializable dict."""
    state = run.state
    return {
        "run_id": run.run_id,
        "job_id": run.job_id,
        "run_name": run.run_name,
        "life_cycle_state": state.life_cycle_state.value if state and state.life_cycle_state else None,
        "result_state": state.result_state.value if state and state.result_state else None,
        "state_message": state.state_message if state else None,
        "run_page_url": run.run_page_url,
        "start_time": run.start_time,
        "end_time": run.end_time,
    }


def list_jobs(w: WorkspaceClient, name: str = "", limit: int = 20) -> list:
    """Lists workspace jobs, optionally filtered by name (case-insensitive substring)."""
    jobs = []
    for job in w.jobs.list(name=name or None, limit=limit):
        jobs.append(
            {
                "job_id": job.job_id,
                "name": job.settings.name if job.settings else None,
                "creator_user_name": job.creator_user_name,
            }
        )
        if len(jobs) >= limit:
            break
    return jobs


def run_job(
    w: WorkspaceClient,
    job_id: int,
    notebook_params: dict = None,
    job_parameters: dict = None,
):
    """Triggers a job run (Jobs Run Now) and returns it immediately, without waiting."""
    waiter = w.jobs.run_now(
        job_id=job_id,
        notebook_params=notebook_params or None,
        job_parameters=job_parameters or None,
    )
    return w.jobs.get_run(run_id=waiter.run_id)


def get_run_status(w: WorkspaceClient, run_id: int):
    """Fetches the current state of a run. Returns the SDK `Run` object."""
    return w.jobs.get_run(run_id=run_id)


def run_job_and_wait(
    w: WorkspaceClient,
    job_id: int,
    notebook_params: dict = None,
    job_parameters: dict = None,
    timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict:
    """Triggers a job and polls until it finishes, times out, or fails.

    Returns:
        dict with `status` ("success" | "failed" | "timeout") and `run` (via run_to_dict).
    """
    waiter = w.jobs.run_now(
        job_id=job_id,
        notebook_params=notebook_params or None,
        job_parameters=job_parameters or None,
    )
    run_id = waiter.run_id

    deadline = time.monotonic() + timeout_seconds
    run = w.jobs.get_run(run_id=run_id)
    while run.state and run.state.life_cycle_state and run.state.life_cycle_state.value not in _TERMINAL_STATES:
        if time.monotonic() >= deadline:
            return {"status": "timeout", "run": run_to_dict(run)}
        time.sleep(poll_interval_seconds)
        run = w.jobs.get_run(run_id=run_id)

    result_state = run.state.result_state.value if run.state and run.state.result_state else None
    return {"status": "success" if result_state == "SUCCESS" else "failed", "run": run_to_dict(run)}


def cancel_run(w: WorkspaceClient, run_id: int) -> None:
    """Cancels an in-progress run and waits for the cancellation to complete."""
    w.jobs.cancel_run(run_id=run_id).result()


def _build_email_payload(
    to: str,
    cc: str,
    bcc: str,
    subject: str,
    body: str,
    attachments: list | None,
) -> dict:
    if not to.strip():
        raise ValueError("Debes proporcionar al menos un destinatario en 'to'.")
    if not subject.strip():
        raise ValueError("Debes proporcionar un 'subject' para el correo.")
    if not body.strip():
        raise ValueError("Debes proporcionar un 'body' para el correo.")

    normalized_attachments = attachments or []
    if not isinstance(normalized_attachments, list):
        raise ValueError("'attachments' debe ser una lista.")

    cleaned_attachments = []
    for attachment in normalized_attachments:
        if not isinstance(attachment, str) or not attachment.strip():
            raise ValueError("Cada elemento de 'attachments' debe ser un string no vacío.")
        cleaned_attachments.append(attachment.strip())

    return {
        "to": to.strip(),
        "cc": cc.strip(),
        "bcc": bcc.strip(),
        "subject": subject.strip(),
        "body": body.strip(),
        "attachments": cleaned_attachments,
    }


@tool
def databricks_jobs_list_jobs(name: str = "", limit: int = 20) -> dict:
    """Lists the Databricks jobs available in the workspace.

    Args:
        name: Filter jobs whose name contains this text (optional, case-insensitive).
        limit: Maximum number of jobs to return (default 20).

    Returns:
        dict with status, jobs (list of {job_id, name, creator_user_name}) and message.
    """
    try:
        # Listed with the app's service identity: the Jobs API doesn't
        # support user-authorization scope in Databricks Apps.
        w = WorkspaceClient()
        jobs = list_jobs(w, name=name, limit=limit)
        return {"status": "success", "jobs": jobs, "message": f"{len(jobs)} job(s) encontrado(s)."}
    except Exception as e:
        logger.exception("Error listing jobs")
        return {"status": "error", "error": str(e), "message": f"Error al listar jobs: {str(e)}"}


@tool
def send_email_via_job(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    attachments: list = None,
    wait_for_completion: bool = False,
    timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> dict:
    """Queues an email delivery by running the Databricks job configured for outbound mail.

    The job receives one parameter named `email_payload` with this JSON shape:
    {"to": "", "cc": "", "bcc": "", "subject": "", "body": "", "attachments": []}
    """
    try:
        payload = _build_email_payload(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            attachments=attachments,
        )
        job_parameters = {"email_payload": json.dumps(payload, ensure_ascii=False)}
        w = WorkspaceClient()

        if wait_for_completion:
            result = run_job_and_wait(
                w,
                job_id=EMAIL_DELIVERY_JOB_ID,
                job_parameters=job_parameters,
                timeout_seconds=timeout_seconds,
            )
            result["job_id"] = EMAIL_DELIVERY_JOB_ID
            run_id = result["run"]["run_id"]
            if result["status"] == "timeout":
                result["message"] = (
                    f"El pipeline de correo {EMAIL_DELIVERY_JOB_ID} se disparó (run_id={run_id}) "
                    f"pero no terminó dentro de {timeout_seconds}s."
                )
            elif result["status"] == "success":
                result["message"] = (
                    f"El pipeline de correo {EMAIL_DELIVERY_JOB_ID} finalizó correctamente "
                    f"(run_id={run_id})."
                )
            else:
                result["message"] = (
                    f"El pipeline de correo {EMAIL_DELIVERY_JOB_ID} terminó con estado "
                    f"{result['run']['result_state']} (run_id={run_id})."
                )
            return result

        run = run_job(
            w,
            job_id=EMAIL_DELIVERY_JOB_ID,
            job_parameters=job_parameters,
        )
        return {
            "status": "success",
            "job_id": EMAIL_DELIVERY_JOB_ID,
            "run_id": run.run_id,
            "run_page_url": run.run_page_url,
            "message": (
                f"Solicitud de correo enviada al pipeline {EMAIL_DELIVERY_JOB_ID} "
                f"(run_id={run.run_id})."
            ),
        }
    except ValueError as e:
        return {"status": "error", "error": str(e), "message": str(e)}
    except Exception as e:
        logger.exception("Error queuing email delivery job")
        return {
            "status": "error",
            "error": str(e),
            "message": f"Error al ejecutar el pipeline de correo {EMAIL_DELIVERY_JOB_ID}: {str(e)}",
        }


@tool
def databricks_jobs_run_job(job_id: int, notebook_params: dict = None, job_parameters: dict = None) -> dict:
    """Triggers a Databricks job run (Jobs Run Now) and returns immediately with the run_id,
    without waiting for it to finish.

    Args:
        job_id: Identifier of the Databricks job to run.
        notebook_params: Key-value parameters for notebook-type tasks (optional).
        job_parameters: Job-level key-value parameters (optional).

    Returns:
        dict with status, run_id, run_page_url and message.
    """
    try:
        w = WorkspaceClient()
        run = run_job(w, job_id=job_id, notebook_params=notebook_params, job_parameters=job_parameters)
        return {
            "status": "success",
            "run_id": run.run_id,
            "job_id": job_id,
            "run_page_url": run.run_page_url,
            "message": f"Ejecución del job {job_id} disparada correctamente (run_id={run.run_id}).",
        }
    except Exception as e:
        logger.exception("Error running job %s", job_id)
        return {"status": "error", "error": str(e), "message": f"Error al ejecutar el job {job_id}: {str(e)}"}


@tool
def databricks_jobs_get_run_status(run_id: int) -> dict:
    """Checks the current status of a Databricks job run.

    Args:
        run_id: Identifier of the run to check.

    Returns:
        dict with status and the run's data (life_cycle_state, result_state, etc.).
    """
    try:
        w = WorkspaceClient()
        run = get_run_status(w, run_id=run_id)
        return {"status": "success", "run": run_to_dict(run)}
    except Exception as e:
        logger.exception("Error checking run %s", run_id)
        return {"status": "error", "error": str(e), "message": f"Error al consultar el run {run_id}: {str(e)}"}


@tool
def databricks_jobs_run_job_and_wait(
    job_id: int,
    notebook_params: dict = None,
    job_parameters: dict = None,
    timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> dict:
    """Triggers a Databricks job run and polls (waits) until it finishes or the timeout elapses.

    Args:
        job_id: Identifier of the Databricks job to run.
        notebook_params: Key-value parameters for notebook-type tasks (optional).
        job_parameters: Job-level key-value parameters (optional).
        timeout_seconds: Maximum wait time in seconds (default 300).

    Returns:
        dict with status, run (final or last-known state) and message.
    """
    try:
        result = run_job_and_wait(
            WorkspaceClient(),
            job_id=job_id,
            notebook_params=notebook_params,
            job_parameters=job_parameters,
            timeout_seconds=timeout_seconds,
        )
        run_id = result["run"]["run_id"]
        if result["status"] == "timeout":
            result["message"] = f"Timeout de {timeout_seconds}s alcanzado esperando el run {run_id}."
        else:
            result["message"] = f"Run {run_id} finalizado con result_state={result['run']['result_state']}."
        return result
    except Exception as e:
        logger.exception("Error running and waiting on job %s", job_id)
        return {"status": "error", "error": str(e), "message": f"Error al ejecutar y esperar el job {job_id}: {str(e)}"}


@tool
def databricks_jobs_cancel_run(run_id: int) -> dict:
    """Cancels an in-progress Databricks job run.

    Args:
        run_id: Identifier of the run to cancel.

    Returns:
        dict with status and message.
    """
    try:
        cancel_run(WorkspaceClient(), run_id=run_id)
        return {"status": "success", "run_id": run_id, "message": f"Run {run_id} cancelado correctamente."}
    except Exception as e:
        logger.exception("Error cancelling run %s", run_id)
        return {"status": "error", "error": str(e), "message": f"Error al cancelar el run {run_id}: {str(e)}"}
