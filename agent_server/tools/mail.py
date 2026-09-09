"""Sends email notifications through an Azure Logic App.

Ported from template_databricks_assest_bundle_mcp
(src/apps/mcp_star/server/utils/mail.py and logic_app_mail_send_email in server/tools.py).

CURRENT STATE: disabled by default. The `send_email` tool below returns a
controlled error until LOGIC_APP_MAIL_URL is configured.

-------------------------------------------------------------------------------
SETUP GUIDE (run once Azure access is available)
-------------------------------------------------------------------------------

1. Create (or reuse) an Azure Logic App with an HTTP request trigger
   ("When a HTTP request is received"):
     - Method: POST
     - Request Body JSON Schema:
         {
           "type": "object",
           "properties": {
             "to": {"type": "array", "items": {"type": "string"}},
             "cc": {"type": "array", "items": {"type": "string"}},
             "bcc": {"type": "array", "items": {"type": "string"}},
             "subject": {"type": "string"},
             "body": {"type": "string"},
             "isHtml": {"type": "boolean"},
             "senderDisplayName": {"type": ["string", "null"]}
           },
           "required": ["to", "subject", "body"]
         }

2. Add a "Send an email (V2)" action (Office 365 Outlook / Outlook.com / SMTP
   connector, whichever mailbox should send) with:
     - To / Cc / Bcc: mapped from the trigger's arrays (join with ';' if the
       connector expects a string instead of an array).
     - Subject: mapped from `subject`.
     - Body: mapped from `body`; if `isHtml` is true, enable "Is HTML" on the action.
     - From (optional, connector-dependent): `senderDisplayName`.

3. Save the Logic App and copy the generated "HTTP POST URL" from the
   trigger (it includes a SAS token, e.g. '...&sig=...'). Treat this full URL
   as a secret.

4. Configure the URL on the Databricks App, WITHOUT committing it to the repo:
     - Recommended: as a Databricks secret referenced in databricks.yml:
         env:
           - name: "LOGIC_APP_MAIL_URL"
             valueFrom: "logic_app_mail_url"   # secret name in the app's scope
       (create the secret: `databricks secrets put-secret <scope> logic_app_mail_url`)
     - Quick testing alternative: set the raw value directly in databricks.yml
       (env -> LOGIC_APP_MAIL_URL), but never in a shared repo.

5. (Optional) Adjust LOGIC_APP_MAIL_TIMEOUT_SECONDS (default 30) if the Logic
   App responds slowly.

6. Redeploy the app so it picks up the new env var / secret.

7. Test with the `send_email` tool.

Once configured, `is_configured()` returns True and `send_email_via_logic_app()`
stops raising the "Logic App not configured" error.
"""

import logging
import os
import re

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_configured(logic_app_url: str = None) -> bool:
    """Whether a Logic App URL is configured (via argument or the
    LOGIC_APP_MAIL_URL env var)."""
    url = logic_app_url if logic_app_url is not None else os.environ.get("LOGIC_APP_MAIL_URL", "")
    return bool(url and url.strip())


def validate_recipients(recipients: list) -> list:
    """Validates and normalizes a list of email addresses.

    Raises:
        ValueError: if the list is empty, or contains malformed addresses.
    """
    if not recipients:
        raise ValueError("You must provide at least one recipient in 'to'.")

    cleaned = [addr.strip() for addr in recipients if addr and addr.strip()]
    if not cleaned:
        raise ValueError("You must provide at least one recipient in 'to'.")

    invalid = [addr for addr in cleaned if not _EMAIL_RE.match(addr)]
    if invalid:
        raise ValueError(f"Invalid email addresses: {', '.join(invalid)}")

    return cleaned


def send_email_via_logic_app(
    to: list,
    subject: str,
    body: str,
    cc: list = None,
    bcc: list = None,
    is_html: bool = False,
    sender_display_name: str = "",
    logic_app_url: str = None,
    timeout_seconds: int = None,
) -> requests.Response:
    """Sends an email by posting the payload to the configured Logic App's HTTP trigger.

    Raises:
        ValueError: if no Logic App is configured (see setup guide above), or
            if the recipients are invalid.
    """
    url = logic_app_url if logic_app_url is not None else os.environ.get("LOGIC_APP_MAIL_URL", "")
    if not url or not url.strip():
        raise ValueError(
            "La Logic App de correo no está configurada. Define la variable de entorno "
            "'LOGIC_APP_MAIL_URL' (o el secreto asociado) con la URL del trigger HTTP de la "
            "Logic App. Ver la guía de configuración en el docstring de agent_server/tools/mail.py."
        )

    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else int(os.environ.get("LOGIC_APP_MAIL_TIMEOUT_SECONDS", "30"))
    )

    to_clean = validate_recipients(to)
    cc_clean = validate_recipients(cc) if cc else []
    bcc_clean = validate_recipients(bcc) if bcc else []

    payload = {
        "to": to_clean,
        "cc": cc_clean,
        "bcc": bcc_clean,
        "subject": subject,
        "body": body,
        "isHtml": is_html,
        "senderDisplayName": sender_display_name or None,
    }

    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response


@tool
def send_email(
    to: list,
    subject: str,
    body: str,
    cc: list = None,
    bcc: list = None,
    is_html: bool = False,
    sender_display_name: str = "",
) -> dict:
    """Sends an email through an Azure Logic App configured as the mail-sending service.

    Requires the 'LOGIC_APP_MAIL_URL' env var/secret to be set (see the setup
    guide in agent_server/tools/mail.py). Returns a controlled error until then.

    Args:
        to: List of recipient email addresses (required, at least one).
        subject: Email subject.
        body: Email body (plain text or HTML depending on 'is_html').
        cc: List of CC addresses (optional).
        bcc: List of BCC addresses (optional).
        is_html: If True, the body is sent as HTML; if False (default), as plain text.
        sender_display_name: Display name for the sender, if the connector supports it (optional).

    Returns:
        dict with status, to, subject and message.
    """
    if not subject.strip():
        return {
            "status": "error",
            "error": "Missing parameters",
            "message": "Debes proporcionar un 'subject' para el correo.",
        }
    if not body.strip():
        return {
            "status": "error",
            "error": "Missing parameters",
            "message": "Debes proporcionar un 'body' para el correo.",
        }

    try:
        response = send_email_via_logic_app(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            is_html=is_html,
            sender_display_name=sender_display_name,
        )
        return {
            "status": "success",
            "to": to,
            "subject": subject,
            "http_status": response.status_code,
            "message": f"Correo enviado correctamente a {', '.join(to)} via Logic App.",
        }
    except ValueError as e:
        return {"status": "error", "error": str(e), "message": str(e)}
    except Exception as e:
        logger.exception("Error sending email via Logic App")
        return {
            "status": "error",
            "error": str(e),
            "message": f"Error al enviar el correo via Logic App: {str(e)}",
        }
