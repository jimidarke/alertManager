"""
Tests for pathways/email_pathway.py's Task 10 hardening and Task 11
template redesign:

- _post_sendmail()'s retry decision matrix (202/429/500/400, retry cap,
  Retry-After handling) -- pure HTTP-client control flow, fully testable
  by mocking requests.post and time.sleep, no live network involved.
- EmailLogWriter.record() is called for every send attempt from both the
  immediate (_send_graph_email) and aggregated (_send_aggregated_email)
  paths, including MSAL token-acquisition failures (http_status=None).
- BasePathway.async_errors is incremented when the timer-thread-driven
  _flush_aggregated_alerts() fails, and NOT when the (separate,
  synchronous) shutdown() flush fails.
- _client_context()'s (client, clients) computation for the aggregated
  Jinja context: single client, multiple clients, and the
  sorted-unique-list property.
- Task 11: template_file/aggregated_template defaults, Jinja always
  initialized, version/generated_at in the render context, the
  body_template override still winning (analytics_engine.py's
  pass-through hack), _create_html_body/_create_default_aggregated_body
  genuinely deleted, and the render-failure-returns-False-no-fallback
  path for both the immediate and aggregated send methods.

No live Postgres/MQTT/Graph API is available in this environment (and per
project rule, docker/docker-compose is never invoked from automation), so
every test here mocks requests.post, time.sleep, and
EmailPathway._email_log_writer.record -- exactly what Task 10's own brief
says is fully verifiable without live services, since this is about this
module's control flow, not real network/DB behavior.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))          # email_pathway.py lives in the top-level `pathways` package
sys.path.insert(0, str(_ROOT / "src"))  # email_pathway.py does `from version import __version__` (Task 11)

from pathways.email_pathway import EmailPathway, GraphResult  # noqa: E402
from version import __version__  # noqa: E402
import requests  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _base_config(**overrides):
    config = {
        'type': 'email',
        'enabled': True,
        'tenant_id': 'test-tenant',
        'client_id': 'test-client',
        'client_secret': 'test-secret',
        'from_address': 'alerts@example.com',
        'to_addresses': ['admin@example.com'],
        'subject_template': 'Alert: {title}',
        'html_format': True,
    }
    config.update(overrides)
    return config


@pytest.fixture
def pathway(mocker):
    """
    An EmailPathway with its EmailLogWriter.record() mocked out (no real
    Postgres connection is ever attempted) and _get_access_token() left
    real but easily overridable per-test.
    """
    p = EmailPathway('email_alerts', _base_config())
    mocker.patch.object(p._email_log_writer, 'record')
    return p


def _response(mocker, status_code, text='', headers=None):
    resp = mocker.MagicMock(name=f"response-{status_code}")
    resp.status_code = status_code
    resp.text = text
    resp.headers = dict(headers or {})
    return resp


def _alert(**overrides):
    alert = {
        'timestamp': 1700000000,
        'level': 'critical',
        'category': 'system',
        'title': 'Disk full',
        'message': '/data is at 98%',
        'source': 'monitor',
        'client': 'acme',
    }
    alert.update(overrides)
    return alert


@pytest.fixture(autouse=True)
def _no_real_sleep(mocker):
    """Every test in this file that touches _post_sendmail's retry loop
    must not actually sleep 2s/8s/etc. Patched globally for this file;
    individual tests still assert on how it was called."""
    return mocker.patch('pathways.email_pathway.time.sleep')


# ---------------------------------------------------------------------------
# _post_sendmail(): retry decision matrix
# ---------------------------------------------------------------------------

def test_202_never_retries(pathway, mocker, _no_real_sleep):
    mock_post = mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 202, headers={'request-id': 'req-1'}),
    )

    result = pathway._post_sendmail('url', {}, {})

    assert mock_post.call_count == 1
    assert result == GraphResult(
        ok=True, http_status=202, error_text=None,
        request_id='req-1', duration_ms=result.duration_ms, retries=0,
    )
    _no_real_sleep.assert_not_called()


def test_202_passes_connect_read_timeout(pathway, mocker):
    mock_post = mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 202),
    )

    pathway._post_sendmail('https://example/sendMail', {'a': 'b'}, {'x': 1})

    _, kwargs = mock_post.call_args
    assert kwargs['timeout'] == (5, 30)
    assert kwargs['headers'] == {'a': 'b'}
    assert kwargs['json'] == {'x': 1}


def test_429_retries_honoring_retry_after(pathway, mocker, _no_real_sleep):
    responses = [
        _response(mocker, 429, headers={'Retry-After': '5'}),
        _response(mocker, 202, headers={'request-id': 'req-2'}),
    ]
    mock_post = mocker.patch('pathways.email_pathway.requests.post', side_effect=responses)

    result = pathway._post_sendmail('url', {}, {})

    assert mock_post.call_count == 2
    _no_real_sleep.assert_called_once_with(5)
    assert result.ok is True
    assert result.retries == 1
    assert result.request_id == 'req-2'


def test_429_retry_after_capped_at_30(pathway, mocker, _no_real_sleep):
    responses = [
        _response(mocker, 429, headers={'Retry-After': '9999'}),
        _response(mocker, 202),
    ]
    mocker.patch('pathways.email_pathway.requests.post', side_effect=responses)

    pathway._post_sendmail('url', {}, {})

    _no_real_sleep.assert_called_once_with(30)


def test_429_malformed_retry_after_falls_back_to_fixed_backoff(pathway, mocker, _no_real_sleep):
    responses = [
        _response(mocker, 429, headers={'Retry-After': 'not-a-number'}),
        _response(mocker, 202),
    ]
    mocker.patch('pathways.email_pathway.requests.post', side_effect=responses)

    pathway._post_sendmail('url', {}, {})

    _no_real_sleep.assert_called_once_with(2)  # first-retry fixed schedule


def test_500_retries_with_fixed_backoff(pathway, mocker, _no_real_sleep):
    responses = [
        _response(mocker, 500, text='internal error'),
        _response(mocker, 202),
    ]
    mock_post = mocker.patch('pathways.email_pathway.requests.post', side_effect=responses)

    result = pathway._post_sendmail('url', {}, {})

    assert mock_post.call_count == 2
    _no_real_sleep.assert_called_once_with(2)
    assert result.ok is True
    assert result.retries == 1


def test_503_is_retryable_like_500(pathway, mocker, _no_real_sleep):
    responses = [_response(mocker, 503), _response(mocker, 202)]
    mocker.patch('pathways.email_pathway.requests.post', side_effect=responses)

    result = pathway._post_sendmail('url', {}, {})

    assert result.ok is True
    assert result.retries == 1


def test_400_never_retries(pathway, mocker, _no_real_sleep):
    mock_post = mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 400, text='bad request'),
    )

    result = pathway._post_sendmail('url', {}, {})

    assert mock_post.call_count == 1
    _no_real_sleep.assert_not_called()
    assert result.ok is False
    assert result.http_status == 400
    assert result.error_text == 'bad request'
    assert result.retries == 0


def test_other_4xx_never_retries(pathway, mocker, _no_real_sleep):
    mock_post = mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 403, text='forbidden'),
    )

    result = pathway._post_sendmail('url', {}, {})

    assert mock_post.call_count == 1
    assert result.ok is False
    assert result.http_status == 403


def test_retries_capped_at_two_total(pathway, mocker, _no_real_sleep):
    # 500 every time -- should stop after 1 initial + 2 retries = 3 POSTs.
    mock_post = mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 500, text='still down'),
    )

    result = pathway._post_sendmail('url', {}, {})

    assert mock_post.call_count == 3
    assert result.ok is False
    assert result.retries == 2
    assert _no_real_sleep.call_args_list == [mocker.call(2), mocker.call(8)]


def test_request_exception_is_not_retried(pathway, mocker, _no_real_sleep):
    mock_post = mocker.patch(
        'pathways.email_pathway.requests.post',
        side_effect=requests.exceptions.ConnectionError("connection refused"),
    )

    result = pathway._post_sendmail('url', {}, {})

    assert mock_post.call_count == 1
    _no_real_sleep.assert_not_called()
    assert result.ok is False
    assert result.http_status is None
    assert 'connection refused' in result.error_text
    assert result.retries == 0


# ---------------------------------------------------------------------------
# record() called for every attempt: immediate path
# ---------------------------------------------------------------------------

def test_immediate_send_success_records_accepted(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 202, headers={'request-id': 'req-9'}),
    )

    ok = pathway._send_graph_email(_alert())

    assert ok is True
    pathway._email_log_writer.record.assert_called_once()
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['pathway'] == 'email_alerts'
    assert kwargs['status'] == 'accepted'
    assert kwargs['aggregated'] is False
    assert kwargs['alert_count'] == 1
    assert kwargs['level'] == 'critical'
    assert kwargs['clients'] == ['acme']
    assert kwargs['recipients'] == ['admin@example.com']
    assert kwargs['http_status'] == 202
    assert kwargs['graph_request_id'] == 'req-9'
    assert kwargs['retries'] == 0
    assert kwargs['error'] is None


def test_immediate_send_graph_failure_records_failed(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 400, text='bad address'),
    )

    ok = pathway._send_graph_email(_alert())

    assert ok is False
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['status'] == 'failed'
    assert kwargs['http_status'] == 400
    assert kwargs['error'] == 'bad address'


def test_immediate_send_msal_failure_records_with_null_http_status(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value=None)
    mock_post = mocker.patch('pathways.email_pathway.requests.post')

    ok = pathway._send_graph_email(_alert())

    assert ok is False
    mock_post.assert_not_called()  # never even reached the Graph call
    pathway._email_log_writer.record.assert_called_once()
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['status'] == 'failed'
    assert kwargs['http_status'] is None
    assert kwargs['aggregated'] is False
    assert kwargs['error'] == 'Failed to obtain access token'


# ---------------------------------------------------------------------------
# record() called for every attempt: aggregated path
# ---------------------------------------------------------------------------

def test_aggregated_send_success_records_accepted(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mocker.patch.object(pathway, '_create_aggregated_body', return_value='<html></html>')
    mocker.patch(
        'pathways.email_pathway.requests.post',
        return_value=_response(mocker, 202, headers={'request-id': 'req-agg'}),
    )
    alerts = [_alert(title='a'), _alert(title='b', client='acme')]

    ok = pathway._send_aggregated_email(alerts)

    assert ok is True
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['status'] == 'accepted'
    assert kwargs['aggregated'] is True
    assert kwargs['alert_count'] == 2
    assert kwargs['clients'] == ['acme']
    assert kwargs['graph_request_id'] == 'req-agg'


def test_aggregated_send_msal_failure_records_with_null_http_status(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value=None)
    mock_post = mocker.patch('pathways.email_pathway.requests.post')
    alerts = [_alert(title='a'), _alert(title='b')]

    ok = pathway._send_aggregated_email(alerts)

    assert ok is False
    mock_post.assert_not_called()
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['status'] == 'failed'
    assert kwargs['http_status'] is None
    assert kwargs['aggregated'] is True
    assert kwargs['alert_count'] == 2


def test_aggregated_send_retried_failure_records_retries_and_status(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mocker.patch.object(pathway, '_create_aggregated_body', return_value='<html></html>')
    responses = [
        _response(mocker, 500, text='err1'),
        _response(mocker, 500, text='err2'),
        _response(mocker, 500, text='err3'),
    ]
    mocker.patch('pathways.email_pathway.requests.post', side_effect=responses)

    ok = pathway._send_aggregated_email([_alert()])

    assert ok is False
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['status'] == 'failed'
    assert kwargs['retries'] == 2
    assert kwargs['http_status'] == 500


# ---------------------------------------------------------------------------
# async_errors: incremented on timer-thread flush failure, not on shutdown
# ---------------------------------------------------------------------------

def test_flush_single_alert_failure_increments_async_errors(pathway, mocker):
    mocker.patch.object(pathway, '_send_graph_email', return_value=False)
    pathway._pending_alerts = [_alert()]

    pathway._flush_aggregated_alerts()

    assert pathway.async_errors == 1


def test_flush_aggregated_failure_increments_async_errors(pathway, mocker):
    mocker.patch.object(pathway, '_send_aggregated_email', return_value=False)
    pathway._pending_alerts = [_alert(title='a'), _alert(title='b')]

    pathway._flush_aggregated_alerts()

    assert pathway.async_errors == 1


def test_flush_aggregated_exception_still_increments_async_errors(pathway, mocker):
    mocker.patch.object(pathway, '_send_aggregated_email', side_effect=RuntimeError("boom"))
    pathway._pending_alerts = [_alert(title='a'), _alert(title='b')]

    pathway._flush_aggregated_alerts()  # must not raise

    assert pathway.async_errors == 1


def test_flush_success_does_not_increment_async_errors(pathway, mocker):
    mocker.patch.object(pathway, '_send_aggregated_email', return_value=True)
    pathway._pending_alerts = [_alert(title='a'), _alert(title='b')]

    pathway._flush_aggregated_alerts()

    assert pathway.async_errors == 0


def test_shutdown_flush_failure_does_not_increment_async_errors(pathway, mocker):
    # shutdown() is a separate, synchronous flush path (not the
    # threading.Timer callback) -- its failures are already logged
    # directly and are explicitly out of scope for async_errors per the
    # Task 10 brief ("specifically when the timer-thread-driven
    # aggregated flush fails").
    mocker.patch.object(pathway, '_send_aggregated_email', return_value=False)
    pathway._pending_alerts = [_alert(title='a'), _alert(title='b')]

    pathway.shutdown()

    assert pathway.async_errors == 0


# ---------------------------------------------------------------------------
# _client_context(): single client, multiple clients, sorted-unique property
# ---------------------------------------------------------------------------

def test_client_context_single_client():
    alerts = [_alert(client='acme'), _alert(client='acme')]
    client, clients = EmailPathway._client_context(alerts)
    assert client == 'acme'
    assert clients == ['acme']


def test_client_context_multiple_clients_returns_none_client():
    alerts = [_alert(client='zeta'), _alert(client='acme')]
    client, clients = EmailPathway._client_context(alerts)
    assert client is None
    assert clients == ['acme', 'zeta']  # sorted


def test_client_context_sorted_unique_even_with_duplicates():
    alerts = [_alert(client='zeta'), _alert(client='acme'), _alert(client='zeta'), _alert(client='acme')]
    client, clients = EmailPathway._client_context(alerts)
    assert clients == ['acme', 'zeta']
    assert client is None


def test_client_context_returns_list_even_for_single_alert():
    # "clients (sorted unique list ... even when there's just one)"
    alerts = [_alert(client='acme')]
    client, clients = EmailPathway._client_context(alerts)
    assert client == 'acme'
    assert clients == ['acme']
    assert isinstance(clients, list)


def test_client_context_ignores_missing_or_empty_client():
    alerts = [_alert(client=''), _alert(client=None)]
    client, clients = EmailPathway._client_context(alerts)
    assert client is None
    assert clients == []


def test_aggregated_context_includes_client_and_clients(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mock_body = mocker.patch.object(pathway, '_create_aggregated_body', return_value='<html></html>')
    mocker.patch('pathways.email_pathway.requests.post', return_value=_response(mocker, 202))
    alerts = [_alert(title='a', client='acme'), _alert(title='b', client='acme')]

    pathway._send_aggregated_email(alerts)

    context = mock_body.call_args.args[0]
    assert context['client'] == 'acme'
    assert context['clients'] == ['acme']


def test_aggregated_context_client_is_none_for_mixed_clients(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mock_body = mocker.patch.object(pathway, '_create_aggregated_body', return_value='<html></html>')
    mocker.patch('pathways.email_pathway.requests.post', return_value=_response(mocker, 202))
    alerts = [_alert(title='a', client='acme'), _alert(title='b', client='globex')]

    pathway._send_aggregated_email(alerts)

    context = mock_body.call_args.args[0]
    assert context['client'] is None
    assert context['clients'] == ['acme', 'globex']


# ---------------------------------------------------------------------------
# Task 11: template defaults, Jinja always initialized
# ---------------------------------------------------------------------------

def test_template_file_defaults_to_alert_default():
    p = EmailPathway('email_alerts', _base_config())
    assert p.template_file == 'emails/alert_default.html.j2'


def test_aggregated_template_defaults_to_alert_default_aggregated():
    p = EmailPathway('email_alerts', _base_config())
    assert p.aggregated_template == 'emails/alert_default_aggregated.html.j2'


def test_jinja_env_always_initialized_with_bare_config():
    # A "bare" config -- no template_file, no storm_prevention key at all
    # (like the out-of-the-box email_critical pathway before Task 11) --
    # used to leave _jinja_env as None. It must not anymore.
    p = EmailPathway('email_critical', _base_config())
    assert p._jinja_env is not None
    assert p._jinja_env.get_template(p.template_file) is not None


def test_explicit_template_file_overrides_default():
    p = EmailPathway('email_alerts', _base_config(template_file='emails/revysmc_critical.html.j2'))
    assert p.template_file == 'emails/revysmc_critical.html.j2'


def test_explicit_aggregated_template_overrides_default():
    config = _base_config(storm_prevention={
        'enabled': True,
        'aggregated_template': 'emails/aggregated_alerts.html.j2',
    })
    p = EmailPathway('email_alerts', config)
    assert p.aggregated_template == 'emails/aggregated_alerts.html.j2'


# ---------------------------------------------------------------------------
# Task 11: old inline-HTML methods are genuinely deleted
# ---------------------------------------------------------------------------

def test_create_html_body_deleted():
    assert not hasattr(EmailPathway, '_create_html_body')


def test_create_default_aggregated_body_deleted():
    assert not hasattr(EmailPathway, '_create_default_aggregated_body')


def test_create_text_body_still_present():
    # KEEP _create_text_body -- used when html_format=False.
    assert hasattr(EmailPathway, '_create_text_body')


# ---------------------------------------------------------------------------
# Task 11: _create_email_body() priority (body_template > text > Jinja)
# and render context (version, generated_at)
# ---------------------------------------------------------------------------

def test_create_email_body_context_includes_version_and_generated_at(pathway, mocker):
    mock_template = mocker.MagicMock()
    mock_template.render.return_value = '<html>rendered</html>'
    mocker.patch.object(pathway._jinja_env, 'get_template', return_value=mock_template)

    body = pathway._create_email_body(_alert())

    assert body == '<html>rendered</html>'
    context = mock_template.render.call_args.kwargs
    assert context['version'] == __version__
    assert isinstance(context['generated_at'], int)
    # alert fields still flow through
    assert context['title'] == 'Disk full'


def test_body_template_overrides_default_template_file(pathway, mocker):
    # This is how analytics_engine.py pipes pre-rendered report HTML
    # straight through EmailPathway (body_template = '{message}' swapped
    # in for one send_alert() call). Now that template_file always has a
    # default value, body_template must keep winning over it.
    get_template = mocker.patch.object(pathway._jinja_env, 'get_template')
    pathway.body_template = '{message}'

    body = pathway._create_email_body(_alert(message='<html>PRE-RENDERED</html>'))

    assert body == '<html>PRE-RENDERED</html>'
    get_template.assert_not_called()


def test_html_format_false_uses_text_body_not_jinja(pathway, mocker):
    get_template = mocker.patch.object(pathway._jinja_env, 'get_template')
    pathway.html_format = False

    body = pathway._create_email_body(_alert())

    assert 'Title: Disk full' in body
    get_template.assert_not_called()


def test_create_email_body_render_failure_logs_error_and_returns_none(pathway, caplog):
    pathway.template_file = 'emails/does_not_exist.html.j2'

    with caplog.at_level('ERROR'):
        body = pathway._create_email_body(_alert())

    assert body is None
    assert any('does_not_exist.html.j2' in rec.message for rec in caplog.records)


def test_create_aggregated_body_render_failure_logs_error_and_returns_none(pathway, caplog):
    pathway.aggregated_template = 'emails/does_not_exist_aggregated.html.j2'

    with caplog.at_level('ERROR'):
        body = pathway._create_aggregated_body({'alerts': [_alert()], 'alert_count': 1})

    assert body is None
    assert any('does_not_exist_aggregated.html.j2' in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Task 11: render failure -> hard send failure (no fallback body), per the
# no-failsafes rule -- both the immediate and aggregated send paths.
# ---------------------------------------------------------------------------

def test_send_graph_email_render_failure_returns_false_without_posting(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mocker.patch.object(pathway, '_create_email_body', return_value=None)
    mock_post = mocker.patch('pathways.email_pathway.requests.post')

    ok = pathway._send_graph_email(_alert())

    assert ok is False
    mock_post.assert_not_called()
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['status'] == 'failed'
    assert kwargs['aggregated'] is False
    assert 'template' in kwargs['error'].lower()


def test_send_aggregated_email_render_failure_returns_false_without_posting(pathway, mocker):
    mocker.patch.object(pathway, '_get_access_token', return_value='tok')
    mocker.patch.object(pathway, '_create_aggregated_body', return_value=None)
    mock_post = mocker.patch('pathways.email_pathway.requests.post')
    alerts = [_alert(title='a'), _alert(title='b')]

    ok = pathway._send_aggregated_email(alerts)

    assert ok is False
    mock_post.assert_not_called()
    kwargs = pathway._email_log_writer.record.call_args.kwargs
    assert kwargs['status'] == 'failed'
    assert kwargs['aggregated'] is True
    assert kwargs['alert_count'] == 2
