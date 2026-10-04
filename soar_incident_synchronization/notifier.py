"""Failure notification via OAuth2 SMTP."""

from __future__ import annotations

import base64
import html
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import NotificationConfig

LOGGER = logging.getLogger(__name__)


def _get_access_token(config: NotificationConfig) -> str:
    import requests  # pylint: disable=import-outside-toplevel

    token_url = f"https://login.microsoftonline.com/{config.oauth_tenant_id}/oauth2/v2.0/token"
    payload = {
        "client_id": config.oauth_client_id,
        "client_secret": config.oauth_client_secret,
        "refresh_token": config.oauth_refresh_token,
        "grant_type": "refresh_token",
        "scope": config.oauth_scope,
    }
    response = requests.post(token_url, data=payload, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def send_error_mail(
    config: NotificationConfig,
    component_name: str,
    function_name: str,
    error_details: dict,
) -> None:
    if not config.enabled:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[IBM SOAR Sync Failure] {component_name} | {function_name} | {timestamp}"
    rows_html = []
    for key, value in error_details.items():
        safe_key = html.escape(str(key))
        safe_value = html.escape(str(value)).replace("\n", "<br>")
        rows_html.append(
            "<tr>"
            f"<td style='padding:8px 14px;border:1px solid #ddd;"
            f"font-weight:bold;background:#f5f5f5;white-space:nowrap;'>{safe_key}</td>"
            f"<td style='padding:8px 14px;border:1px solid #ddd;"
            f"font-family:Consolas,monospace;font-size:13px;"
            f"white-space:pre-wrap;word-break:break-all;'>{safe_value}</td>"
            "</tr>"
        )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;margin:24px;color:#222;">
  <h2 style="color:#c0392b;">IBM SOAR incident synchronization failure</h2>
  <table style="border-collapse:collapse;margin-bottom:20px;">
    <tr><td style="padding:4px 16px 4px 0;font-weight:bold;">Component:</td><td>{html.escape(component_name)}</td></tr>
    <tr><td style="padding:4px 16px 4px 0;font-weight:bold;">Function:</td><td>{html.escape(function_name)}</td></tr>
    <tr><td style="padding:4px 16px 4px 0;font-weight:bold;">Time:</td><td>{timestamp}</td></tr>
  </table>
  <table style="border-collapse:collapse;width:100%;max-width:960px;">
    <thead>
      <tr>
        <th style="padding:10px 14px;border:1px solid #bbb;background:#c0392b;color:#fff;text-align:left;width:200px;">Field</th>
        <th style="padding:10px 14px;border:1px solid #bbb;background:#c0392b;color:#fff;text-align:left;">Value</th>
      </tr>
    </thead>
    <tbody>{"".join(rows_html)}</tbody>
  </table>
</body>
</html>"""

    message = MIMEMultipart()
    message["From"] = config.email_from
    message["To"] = config.email_to
    message["Subject"] = subject
    message.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        access_token = _get_access_token(config)
        server = smtplib.SMTP(config.smtp_server, config.smtp_port)
        server.starttls()
        server.ehlo()
        oauth2_string = f"user={config.smtp_user}\x01auth=Bearer {access_token}\x01\x01"
        encoded = base64.b64encode(oauth2_string.encode("utf-8")).decode("utf-8")
        server.docmd("AUTH", f"XOAUTH2 {encoded}")
        server.sendmail(config.email_from, [config.email_to], message.as_string())
        server.quit()
    except Exception as exc:  # pragma: no cover - failure should never mask primary error
        LOGGER.error("Could not send error mail: %s", exc)
