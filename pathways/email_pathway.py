"""
Email Pathway

Handles sending alerts via email using Office 365 Graph API with MSAL authentication.
Includes email storm prevention to aggregate multiple critical alerts into a single email.
"""

import os
import msal
import pytz
import requests
import threading
import time
from dataclasses import dataclass
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from jinja2 import Environment, FileSystemLoader, select_autoescape
from .base_pathway import BasePathway
from .email_log import EmailLogWriter

# email_pathway.py lives in the top-level `pathways` package, but
# version.py is a flat module in src/ (no __init__.py there -- see
# src/http_gateway.py's own `from version import __version__`). This
# resolves at real runtime because every entry point that imports this
# pathway (src/alert_router.py, src/analytics_engine.py) is itself run as
# `python3 src/<script>.py`, which puts src/ on sys.path[0] for the whole
# process. Tests must add src/ to sys.path explicitly (see
# tests/test_email_pathway.py).
from version import __version__

# Mountain time -- see docs/TIMEZONE.md. Fixed regardless of container TZ
# so email timestamps are never accidentally UTC or host-local; pytz
# resolves MST/MDT automatically for whatever instant is being formatted.
EMAIL_DISPLAY_TIMEZONE = 'America/Edmonton'


@dataclass
class GraphResult:
    """Outcome of a (possibly retried) Microsoft Graph sendMail POST."""
    ok: bool
    http_status: Optional[int]
    error_text: Optional[str]
    request_id: Optional[str]
    duration_ms: int
    retries: int


class EmailPathway(BasePathway):
    """Email pathway for sending alerts via O365 Graph API with MSAL authentication"""

    # Graph sendMail HTTP client behavior (see _post_sendmail)
    GRAPH_TIMEOUT = (5, 30)  # (connect, read) seconds
    MAX_RETRIES = 2
    RETRY_BACKOFF_SECONDS = [2, 8]  # fixed schedule for the 1st/2nd retry, used when no Retry-After header
    RETRY_AFTER_CAP_SECONDS = 30

    # Defaults chosen so a bare pathway config (no template_file /
    # storm_prevention.aggregated_template key at all -- e.g. the
    # out-of-the-box email_critical pathway) gets the redesigned
    # templates with zero config change.
    DEFAULT_TEMPLATE_FILE = 'emails/alert_default.html.j2'
    DEFAULT_AGGREGATED_TEMPLATE_FILE = 'emails/alert_default_aggregated.html.j2'

    def __init__(self, name: str, config: Dict[str, Any]):
        """
        Initialize email pathway

        Args:
            name: Pathway instance name
            config: Configuration dictionary containing:
                - tenant_id: Azure tenant ID
                - client_id: Azure app client ID
                - client_secret: Azure app client secret
                - from_address: Sender email address
                - to_addresses: List of recipient email addresses
                - subject_template: Email subject template
                - body_template: Email body template (optional -- an explicit
                  format-string override that always wins over Jinja
                  rendering; this is how analytics_engine.py pipes
                  pre-rendered report HTML straight through)
                - html_format: Send as HTML email (default: True)
                - template_file: Path to Jinja2 template file (optional,
                  defaults to DEFAULT_TEMPLATE_FILE)
        """
        super().__init__(name, config)

        # O365 Graph API settings
        self.tenant_id = config.get('tenant_id')
        self.client_id = config.get('client_id')
        self.client_secret = config.get('client_secret')

        # Email settings
        self.from_address = config.get('from_address')
        self.to_addresses = config.get('to_addresses', [])
        self.subject_template = config.get('subject_template', 'Alert: {title}')
        self.body_template = config.get('body_template')
        self.html_format = config.get('html_format', True)
        self.template_file = config.get('template_file', self.DEFAULT_TEMPLATE_FILE)

        # MSAL app instance (will be initialized on first use)
        self._msal_app = None
        self._access_token = None
        self._token_expires = None

        # Jinja2 environment: always initialized now (template_file always
        # has a value -- see above), used for both the single-alert body
        # and, when storm prevention is enabled, the aggregated body.
        self._jinja_env = None
        self._setup_jinja_environment()

        # Storm prevention settings
        storm_config = config.get('storm_prevention', {})
        self.storm_prevention_enabled = storm_config.get('enabled', False)
        self.aggregation_window = storm_config.get('window_seconds', 10)
        self.aggregation_levels = storm_config.get('levels', ['critical'])
        self.aggregated_template = storm_config.get('aggregated_template', self.DEFAULT_AGGREGATED_TEMPLATE_FILE)

        # Aggregation state (thread-safe)
        self._pending_alerts: List[Dict[str, Any]] = []
        self._aggregation_lock = threading.Lock()
        self._aggregation_timer: Optional[threading.Timer] = None
        self._shutdown = False

        # Load secrets from environment if not in config
        self._load_env_secrets()

        # Records every send attempt (success or failure) to the email_log
        # table. Fail-open by design (see pathways/email_log.py) -- a
        # tracking-DB outage must never block or fail an actual send, so
        # no try/except is needed around calls to it.
        self._email_log_writer = EmailLogWriter()

    def _setup_jinja_environment(self):
        """Setup Jinja2 template environment"""
        template_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'src',
            'templates'
        )

        self._jinja_env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(['html', 'xml']),
            trim_blocks=True,
            lstrip_blocks=True
        )

        # Add custom filter for datetime formatting
        self._jinja_env.filters['datetime_format'] = self._datetime_format_filter

        self.logger.debug(f"Jinja2 environment initialized with template directory: {template_dir}")

    def _datetime_format_filter(self, timestamp: int, format: str = '%Y-%m-%d %H:%M:%S %Z') -> str:
        """
        Jinja2 filter: format a Unix epoch integer as Mountain time.

        Pinned to EMAIL_DISPLAY_TIMEZONE (America/Edmonton) regardless of
        the container's TZ env var or host clock -- per docs/TIMEZONE.md,
        display-time conversion must happen explicitly at render time from
        the stored UTC epoch, not depend on ambient local time. Converting
        via an explicit UTC-aware datetime (fromtimestamp(ts, tz=UTC)) and
        then .astimezone() lets pytz resolve MST vs. MDT correctly for
        that specific instant (DST-safe), unlike a naive
        tz.localize(datetime.fromtimestamp(ts)) which assumes the host's
        local wall-clock interpretation of the epoch.
        """
        try:
            tz = pytz.timezone(EMAIL_DISPLAY_TIMEZONE)
            dt_utc = datetime.fromtimestamp(int(timestamp), tz=pytz.UTC)
            return dt_utc.astimezone(tz).strftime(format)
        except (TypeError, ValueError, OSError):
            return str(timestamp)

    def _load_env_secrets(self):
        """Load sensitive configuration from environment variables"""
        if not self.from_address:
            self.from_address = os.getenv('EMAIL_FROM_ADDRESS')
        if not self.client_secret:
            self.client_secret = os.getenv('O365_CLIENT_SECRET')
        if not self.tenant_id:
            self.tenant_id = os.getenv('O365_TENANT_ID')
        if not self.client_id:
            self.client_id = os.getenv('O365_CLIENT_ID')

    def validate_config(self) -> bool:
        """
        Validate email pathway configuration

        Returns:
            bool: True if configuration is valid
        """
        if not self.from_address:
            self.logger.error("from_address not specified")
            return False

        if not self.to_addresses:
            self.logger.error("to_addresses not specified")
            return False

        if not all([self.tenant_id, self.client_id, self.client_secret]):
            self.logger.error("Graph API configuration incomplete: missing tenant_id, client_id, or client_secret")
            return False

        return True

    def send_alert(self, alert: Dict[str, Any], pathway_config: Dict[str, Any] = None) -> bool:
        """
        Send alert via email using O365 Graph API

        Args:
            alert: Alert dictionary
            pathway_config: Optional pathway-specific configuration

        Returns:
            bool: True if email was sent successfully (or queued for aggregation)
        """
        if not self.should_send_alert(alert):
            return False

        # Check if storm prevention applies
        if self.storm_prevention_enabled and alert.get('level') in self.aggregation_levels:
            return self._queue_for_aggregation(alert)

        # Normal immediate send
        try:
            return self._send_graph_email(alert)
        except Exception as e:
            error_msg = f"Error sending email via Graph API: {e}"
            self.log_send_attempt(alert, False, error_msg)
            return False

    def _queue_for_aggregation(self, alert: Dict[str, Any]) -> bool:
        """
        Queue alert for aggregation during storm prevention window.

        Args:
            alert: Alert dictionary to queue

        Returns:
            bool: True if alert was queued successfully
        """
        with self._aggregation_lock:
            if self._shutdown:
                # Service is shutting down, send immediately
                return self._send_graph_email(alert)

            # Add alert to pending queue
            self._pending_alerts.append(alert.copy())
            self.logger.debug(f"Queued alert for aggregation, pending count: {len(self._pending_alerts)}")

            # Cancel existing timer if any
            if self._aggregation_timer is not None:
                self._aggregation_timer.cancel()

            # Start new timer (rolling window)
            self._aggregation_timer = threading.Timer(
                self.aggregation_window,
                self._flush_aggregated_alerts
            )
            self._aggregation_timer.daemon = True
            self._aggregation_timer.start()

        return True

    def _flush_aggregated_alerts(self):
        """
        Timer callback to flush aggregated alerts.
        Sends single email for one alert, aggregated email for multiple.

        This runs on a background threading.Timer thread, not in the main
        request path -- a failure here has no caller to return False to,
        so it's tracked via BasePathway.increment_async_errors() instead
        (surfaced by AlertRouter.publish_service_status()'s pathway_errors
        merge). _send_graph_email()/_send_aggregated_email() already catch
        their own exceptions and return bool, but the try/except here is
        kept as a last-resort net so this callback can never raise into
        the Timer thread.
        """
        with self._aggregation_lock:
            if not self._pending_alerts:
                return

            alerts_to_send = self._pending_alerts.copy()
            self._pending_alerts.clear()
            self._aggregation_timer = None

        # Send appropriate email based on alert count
        if len(alerts_to_send) == 1:
            # Single alert - use normal email format
            self.logger.info("Storm prevention: sending single queued alert")
            try:
                success = self._send_graph_email(alerts_to_send[0])
            except Exception as e:
                self.logger.error(f"Error sending queued alert: {e}")
                success = False
        else:
            # Multiple alerts - send aggregated summary
            self.logger.info(f"Storm prevention: sending aggregated email for {len(alerts_to_send)} alerts")
            try:
                success = self._send_aggregated_email(alerts_to_send)
            except Exception as e:
                self.logger.error(f"Error sending aggregated email: {e}")
                success = False

        if not success:
            self.increment_async_errors()

    def _send_aggregated_email(self, alerts: List[Dict[str, Any]]) -> bool:
        """
        Send aggregated storm alert email.

        Args:
            alerts: List of alert dictionaries to aggregate

        Returns:
            bool: True if email was sent successfully
        """
        # Get access token
        access_token = self._get_access_token()
        if not access_token:
            error_msg = "Failed to obtain access token for aggregated email"
            self.logger.error(error_msg)
            self._record_send_attempt(
                status='failed', aggregated=True, alerts=alerts,
                subject=None, http_status=None, error=error_msg,
                graph_request_id=None, duration_ms=None, retries=0,
            )
            return False

        try:
            # Build aggregation context
            timestamps = [a.get('timestamp', 0) for a in alerts]
            sources = list(set(a.get('source', 'Unknown') for a in alerts))
            first_timestamp = min(timestamps) if timestamps else 0
            last_timestamp = max(timestamps) if timestamps else 0
            duration_seconds = last_timestamp - first_timestamp if first_timestamp and last_timestamp else 0
            client, clients = self._client_context(alerts)

            context = {
                'alert_count': len(alerts),
                'alerts': alerts,
                'first_timestamp': first_timestamp,
                'last_timestamp': last_timestamp,
                'unique_sources': sources,
                'duration_seconds': duration_seconds,
                'aggregation_window': self.aggregation_window,
                'level': alerts[0].get('level', 'critical') if alerts else 'critical',
                'client': client,
                'clients': clients,
                'version': __version__,
                'generated_at': int(time.time()),
            }

            body = self._create_aggregated_body(context)
            if body is None:
                # _create_aggregated_body() already logged the ERROR with
                # the render exception. Per this project's no-failsafes
                # rule there is no fallback body -- a render failure is a
                # hard send failure.
                error_msg = f"Failed to render aggregated email body (template: {self.aggregated_template})"
                self._record_send_attempt(
                    status='failed', aggregated=True, alerts=alerts,
                    subject=None, http_status=None, error=error_msg,
                    graph_request_id=None, duration_ms=None, retries=0,
                )
                return False

            # Build subject line
            subject = f"STORM ALERT: {len(alerts)} Critical Alerts in {self.aggregation_window}s"

            # Prepare email data
            email_data = {
                "message": {
                    "subject": subject,
                    "body": {
                        "contentType": "HTML" if self.html_format else "Text",
                        "content": body
                    },
                    "toRecipients": [
                        {"emailAddress": {"address": addr}} for addr in self.to_addresses
                    ]
                }
            }

            # Send email
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }

            url = f'https://graph.microsoft.com/v1.0/users/{self.from_address}/sendMail'
            result = self._post_sendmail(url, headers, email_data)

            self._record_send_attempt(
                status='accepted' if result.ok else 'failed', aggregated=True, alerts=alerts,
                subject=subject, http_status=result.http_status, error=result.error_text,
                graph_request_id=result.request_id, duration_ms=result.duration_ms, retries=result.retries,
            )

            if result.ok:
                self.logger.info(f"Aggregated storm alert email sent successfully ({len(alerts)} alerts)")
                return True
            else:
                self.logger.error(f"Graph API error for aggregated email: {result.http_status} - {result.error_text}")
                return False

        except Exception as e:
            error_msg = f"Error sending aggregated email: {e}"
            self.logger.error(error_msg)
            self._record_send_attempt(
                status='failed', aggregated=True, alerts=alerts,
                subject=None, http_status=None, error=error_msg,
                graph_request_id=None, duration_ms=None, retries=0,
            )
            return False

    def _create_aggregated_body(self, context: Dict[str, Any]) -> Optional[str]:
        """
        Render the aggregated ("storm") email body via Jinja2.

        No inline-HTML fallback: per this project's no-failsafes rule, a
        render failure is a hard failure. Logs ERROR and returns None;
        the caller (_send_aggregated_email) must not send anything if
        this returns None.

        Args:
            context: Dictionary with aggregation context (see
                _send_aggregated_email)

        Returns:
            Optional[str]: rendered HTML email body, or None on failure.
        """
        try:
            template = self._jinja_env.get_template(self.aggregated_template)
            return template.render(**context)
        except Exception as e:
            self.logger.error(f"Error rendering aggregated template {self.aggregated_template}: {e}")
            return None

    def shutdown(self):
        """
        Gracefully shutdown the email pathway.
        Flushes any pending alerts before stopping.
        """
        self.logger.info(f"Shutting down email pathway '{self.name}'")

        with self._aggregation_lock:
            self._shutdown = True

            # Cancel timer
            if self._aggregation_timer is not None:
                self._aggregation_timer.cancel()
                self._aggregation_timer = None

            # Flush pending alerts
            if self._pending_alerts:
                alerts_to_send = self._pending_alerts.copy()
                self._pending_alerts.clear()

                self.logger.info(f"Flushing {len(alerts_to_send)} pending alerts on shutdown")

                # Send outside lock
                if len(alerts_to_send) == 1:
                    try:
                        self._send_graph_email(alerts_to_send[0])
                    except Exception as e:
                        self.logger.error(f"Error flushing single alert on shutdown: {e}")
                elif len(alerts_to_send) > 1:
                    try:
                        self._send_aggregated_email(alerts_to_send)
                    except Exception as e:
                        self.logger.error(f"Error flushing aggregated alerts on shutdown: {e}")

    def _get_msal_app(self) -> msal.ConfidentialClientApplication:
        """Get or create MSAL application instance"""
        if self._msal_app is None:
            authority = f"https://login.microsoftonline.com/{self.tenant_id}"
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=authority
            )
        return self._msal_app

    def _get_access_token(self) -> Optional[str]:
        """
        Get O365 access token using MSAL with client credentials flow

        Returns:
            str: Access token if successful, None otherwise
        """
        try:
            # Check if we have a valid cached token
            if self._access_token and self._token_expires:
                if datetime.utcnow() < self._token_expires - timedelta(minutes=5):
                    return self._access_token

            # Get MSAL app instance
            app = self._get_msal_app()

            # Request new token
            scopes = ["https://graph.microsoft.com/.default"]
            result = app.acquire_token_for_client(scopes=scopes)

            if "access_token" in result:
                self._access_token = result["access_token"]
                expires_in = result.get("expires_in", 3600)
                self._token_expires = datetime.utcnow() + timedelta(seconds=expires_in)

                self.logger.debug("Successfully obtained O365 access token via MSAL")
                return self._access_token
            else:
                error = result.get("error")
                error_desc = result.get("error_description")
                self.logger.error(f"MSAL token acquisition failed: {error} - {error_desc}")
                return None

        except Exception as e:
            self.logger.error(f"Error getting access token via MSAL: {e}")
            return None

    def _send_graph_email(self, alert: Dict[str, Any]) -> bool:
        """Send email via Microsoft Graph API"""
        # Get access token
        access_token = self._get_access_token()
        if not access_token:
            error_msg = "Failed to obtain access token"
            self.log_send_attempt(alert, False, error_msg)
            self._record_send_attempt(
                status='failed', aggregated=False, alerts=[alert],
                subject=None, http_status=None, error=error_msg,
                graph_request_id=None, duration_ms=None, retries=0,
            )
            return False

        body = self._create_email_body(alert)
        if body is None:
            # _create_email_body() already logged the ERROR with the
            # render exception (when html_format triggered Jinja
            # rendering). Per this project's no-failsafes rule there is
            # no fallback body -- a render failure is a hard send
            # failure.
            error_msg = f"Failed to render email body (template: {self.template_file})"
            self.log_send_attempt(alert, False, error_msg)
            self._record_send_attempt(
                status='failed', aggregated=False, alerts=[alert],
                subject=None, http_status=None, error=error_msg,
                graph_request_id=None, duration_ms=None, retries=0,
            )
            return False

        try:
            subject = self.subject_template.format(**alert)

            # Prepare email data
            email_data = {
                "message": {
                    "subject": subject,
                    "body": {
                        "contentType": "HTML" if self.html_format else "Text",
                        "content": body
                    },
                    "toRecipients": [
                        {"emailAddress": {"address": addr}} for addr in self.to_addresses
                    ]
                }
            }

            # Send email
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }

            url = f'https://graph.microsoft.com/v1.0/users/{self.from_address}/sendMail'
            result = self._post_sendmail(url, headers, email_data)

            self._record_send_attempt(
                status='accepted' if result.ok else 'failed', aggregated=False, alerts=[alert],
                subject=subject, http_status=result.http_status, error=result.error_text,
                graph_request_id=result.request_id, duration_ms=result.duration_ms, retries=result.retries,
            )

            if result.ok:
                self.log_send_attempt(alert, True)
                return True
            else:
                error_msg = f"Graph API error: {result.http_status} - {result.error_text}"
                self.log_send_attempt(alert, False, error_msg)
                return False

        except Exception as e:
            error_msg = f"Graph API error: {e}"
            self.log_send_attempt(alert, False, error_msg)
            self._record_send_attempt(
                status='failed', aggregated=False, alerts=[alert],
                subject=None, http_status=None, error=error_msg,
                graph_request_id=None, duration_ms=None, retries=0,
            )
            return False

    def _post_sendmail(self, url: str, headers: Dict[str, str], email_data: Dict[str, Any]) -> GraphResult:
        """
        POST to Microsoft Graph's sendMail endpoint, retrying transient
        failures.

        Retries only HTTP 429 or 5xx responses, up to MAX_RETRIES times
        total (so at most MAX_RETRIES + 1 POSTs are made). A 202
        (accepted) never retries, and any other 4xx (client error) never
        retries either -- those won't succeed on a second attempt. Honors
        a Retry-After response header (capped at RETRY_AFTER_CAP_SECONDS)
        when present; otherwise sleeps RETRY_BACKOFF_SECONDS[retry_index]
        between attempts (2s before the 1st retry, 8s before the 2nd). A
        `requests` exception (connection error, timeout, etc.) is treated
        as an immediate failed attempt -- not retried, since there's no
        HTTP status to base a 429/5xx decision on.

        Args:
            url: Graph sendMail URL.
            headers: Request headers (Authorization, Content-Type).
            email_data: JSON body for the request.

        Returns:
            GraphResult: outcome of the (possibly retried) send.
        """
        start = time.monotonic()
        retries = 0

        while True:
            try:
                response = requests.post(url, headers=headers, json=email_data, timeout=self.GRAPH_TIMEOUT)
            except requests.RequestException as e:
                return GraphResult(
                    ok=False,
                    http_status=None,
                    error_text=str(e),
                    request_id=None,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    retries=retries,
                )

            request_id = response.headers.get('request-id')

            if response.status_code == 202:
                return GraphResult(
                    ok=True,
                    http_status=response.status_code,
                    error_text=None,
                    request_id=request_id,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    retries=retries,
                )

            is_retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if not is_retryable or retries >= self.MAX_RETRIES:
                return GraphResult(
                    ok=False,
                    http_status=response.status_code,
                    error_text=response.text,
                    request_id=request_id,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    retries=retries,
                )

            time.sleep(self._retry_delay_seconds(response, retries))
            retries += 1

    @classmethod
    def _retry_delay_seconds(cls, response, retry_index: int) -> float:
        """
        Delay before the next retry: the response's Retry-After header
        (capped at RETRY_AFTER_CAP_SECONDS) when present and parseable,
        else RETRY_BACKOFF_SECONDS[retry_index].
        """
        retry_after = response.headers.get('Retry-After')
        if retry_after is not None:
            try:
                return min(int(retry_after), cls.RETRY_AFTER_CAP_SECONDS)
            except ValueError:
                pass
        return cls.RETRY_BACKOFF_SECONDS[retry_index]

    @staticmethod
    def _client_context(alerts: List[Dict[str, Any]]):
        """
        Compute (client, clients) for a batch of alerts.

        clients: sorted unique list of non-empty client names across the
            batch (used for both the aggregated-email Jinja context and
            EmailLogWriter.record()'s `clients` column).
        client: the single client name if every client-bearing alert in
            the batch shares it, else None -- for the aggregated-email
            Jinja context only ("clients" is what EmailLogWriter uses).

        Returns:
            Tuple[Optional[str], List[str]]: (client, clients)
        """
        clients = sorted({a.get('client') for a in alerts if a.get('client')})
        client = clients[0] if len(clients) == 1 else None
        return client, clients

    def _record_send_attempt(
        self,
        *,
        status: str,
        aggregated: bool,
        alerts: List[Dict[str, Any]],
        subject: Optional[str],
        http_status: Optional[int],
        error: Optional[str],
        graph_request_id: Optional[str],
        duration_ms: Optional[int],
        retries: int,
    ) -> None:
        """
        Record one send attempt (success or failure) via EmailLogWriter.
        Called for every attempt from both the immediate and aggregated
        send paths, including MSAL token failures (http_status=None).

        Args:
            status: 'accepted' or 'failed'.
            aggregated: True for a storm-prevention aggregated send.
            alerts: The alert(s) this send represents; drives alert_count,
                level (from the first alert), and clients.
            subject/http_status/error/graph_request_id/duration_ms/retries:
                See EmailLogWriter.record().
        """
        _, clients = self._client_context(alerts)
        level = alerts[0].get('level') if alerts else None

        self._email_log_writer.record(
            pathway=self.name,
            status=status,
            aggregated=aggregated,
            alert_count=len(alerts),
            level=level,
            clients=clients,
            recipients=list(self.to_addresses),
            subject=subject,
            http_status=http_status,
            error=error,
            graph_request_id=graph_request_id,
            duration_ms=duration_ms,
            retries=retries,
        )

    def _create_email_body(self, alert: Dict[str, Any]) -> Optional[str]:
        """
        Create the email body for a single alert.

        Priority:
          1. body_template, if configured: an explicit format-string
             override that always wins, regardless of html_format or
             template_file. This is how analytics_engine.py pipes
             pre-rendered, already-inlined analytics report HTML straight
             through the email pathway (it swaps
             `body_template = '{message}'` in for the duration of one
             send_alert() call) -- that mechanism must keep working now
             that template_file always has a value (see __init__), so
             body_template has to keep taking priority over it.
          2. Plain text via _create_text_body(), when html_format=False.
             Graph's sendMail takes one contentType per send, so a
             non-HTML pathway has no business invoking the HTML Jinja
             template at all.
          3. Jinja2 rendering via self.template_file (defaults to
             DEFAULT_TEMPLATE_FILE). No inline-HTML fallback on render
             failure: per this project's no-failsafes rule, a render
             failure is a hard failure -- logged as ERROR here, and the
             caller (_send_graph_email) must not send anything if this
             returns None.

        Returns:
            Optional[str]: the email body, or None if Jinja rendering
                failed.
        """
        if self.body_template:
            return self.body_template.format(**alert)

        if not self.html_format:
            return self._create_text_body(alert)

        context = {**alert, 'version': __version__, 'generated_at': int(time.time())}
        try:
            template = self._jinja_env.get_template(self.template_file)
            return template.render(**context)
        except Exception as e:
            self.logger.error(f"Error rendering Jinja2 template {self.template_file}: {e}")
            return None

    def _create_text_body(self, alert: Dict[str, Any]) -> str:
        """Create plain text email body"""
        body = f"""Alert Details:

Title: {alert.get('title', 'N/A')}
Level: {alert.get('level', 'N/A')}
Category: {alert.get('category', alert.get('type', 'N/A'))}
Message: {alert.get('message', 'N/A')}
Source: {alert.get('source', 'N/A')}
Timestamp: {alert.get('timestamp', 'N/A')}

"""

        # Add metadata if present
        if 'metadata' in alert and alert['metadata']:
            body += "Additional Information:\n"
            for key, value in alert['metadata'].items():
                body += f"{key}: {value}\n"
            body += "\n"

        body += "This is an automated alert from your Alert Manager system."
        return body
