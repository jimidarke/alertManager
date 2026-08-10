"""
Tests for Task 11's email template redesign
(src/templates/emails/{alert_default,alert_default_aggregated,
revysmc_critical,revysmc_aggregated}.html.j2 and
components/alert_macros.html.j2).

Covers what the Task 11 brief calls out as meaningful to test without a
real mail client or headless browser:

- Template rendering doesn't throw for valid canned single-alert /
  storm-alert data, across every template in the family (including the
  pre-existing aggregated_alerts.html.j2, which alert_macros.html.j2
  changes don't touch but could in principle break via a shared Jinja
  env misconfiguration).
- The datetime_format filter (pathways/email_pathway.py) produces
  correct Mountain-time (America/Edmonton) output for known epochs,
  including the exact instant of the spring-forward and fall-back DST
  transitions -- this is the "DST-boundary case" the brief asks for.
- The metadata table is correctly omitted (no heading, no
  em-metadata-table markup) when metadata is empty/absent, and present
  when it isn't.
- level_badge uses THEME_SPECIFICATION.md's exact hex values for every
  level (critical/warning/notice/info verbatim from the light-mode
  email table; debug's light pair is a documented derivation -- see
  components/alert_macros.html.j2's module docstring).
- client_banner's three states (single client / multiple clients /
  neither) and chip's "render nothing for a falsy value" behavior.

No live Postgres/MQTT/Graph API is involved -- these tests only touch
EmailPathway._jinja_env (built in __init__, no network calls), exactly
like tools/tests/render_email_preview.py.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))          # `pathways` is a top-level package
sys.path.insert(0, str(_ROOT / "src"))  # email_pathway.py does `from version import __version__`

from pathways.email_pathway import EmailPathway  # noqa: E402


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
    """An EmailPathway used only for its _jinja_env -- no network calls happen in this file."""
    p = EmailPathway('email_alerts', _base_config())
    mocker.patch.object(p._email_log_writer, 'record')
    return p


def _alert(**overrides):
    alert = {
        'timestamp': 1710061200,
        'level': 'critical',
        'category': 'system',
        'title': 'Disk full',
        'message': 'line one\nline two',
        'source': 'monitor-01',
        'client': 'acme',
    }
    alert.update(overrides)
    return alert


def _render_single(pathway, template_file, **alert_overrides):
    alert = _alert(**alert_overrides)
    context = {**alert, 'version': '9.9.9-test', 'generated_at': 1710061300}
    template = pathway._jinja_env.get_template(template_file)
    return template.render(**context)


def _render_storm(pathway, template_file, alerts=None, **context_overrides):
    if alerts is None:
        alerts = [_alert(title='a'), _alert(title='b', source='db-01')]
    timestamps = [a['timestamp'] for a in alerts]
    client, clients = EmailPathway._client_context(alerts)
    context = {
        'alert_count': len(alerts),
        'alerts': alerts,
        'first_timestamp': min(timestamps),
        'last_timestamp': max(timestamps),
        'unique_sources': sorted({a['source'] for a in alerts}),
        'duration_seconds': max(timestamps) - min(timestamps),
        'aggregation_window': 10,
        'level': alerts[0]['level'],
        'client': client,
        'clients': clients,
        'version': '9.9.9-test',
        'generated_at': 1710061300,
    }
    context.update(context_overrides)
    template = pathway._jinja_env.get_template(template_file)
    return template.render(**context)


SINGLE_TEMPLATES = ['emails/alert_default.html.j2', 'emails/revysmc_critical.html.j2']
STORM_TEMPLATES = [
    'emails/alert_default_aggregated.html.j2',
    'emails/revysmc_aggregated.html.j2',
    'emails/aggregated_alerts.html.j2',  # pre-existing, unmodified by Task 11 -- must still render
]


# ---------------------------------------------------------------------------
# Rendering doesn't throw for valid canned data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('template_file', SINGLE_TEMPLATES)
def test_single_alert_template_renders_without_error(pathway, template_file):
    html = _render_single(pathway, template_file)
    assert '<html' in html.lower()
    assert 'Disk full' in html


@pytest.mark.parametrize('template_file', STORM_TEMPLATES)
def test_storm_template_renders_without_error(pathway, template_file):
    html = _render_storm(pathway, template_file)
    assert '<html' in html.lower()


@pytest.mark.parametrize('template_file', SINGLE_TEMPLATES)
def test_single_alert_template_renders_for_every_level(pathway, template_file):
    for level in ['critical', 'warning', 'notice', 'info', 'debug']:
        html = _render_single(pathway, template_file, level=level)
        assert 'Disk full' in html


def test_default_templates_are_table_based_no_flex_or_grid(pathway):
    for template_file in ['emails/alert_default.html.j2', 'emails/alert_default_aggregated.html.j2']:
        html = _render_single(pathway, template_file) if template_file in SINGLE_TEMPLATES else _render_storm(pathway, template_file)
        assert 'display:flex' not in html.replace(' ', '')
        assert 'display:grid' not in html.replace(' ', '')
        assert '<link' not in html  # no external stylesheet
        assert '<img' not in html   # no remote images


# ---------------------------------------------------------------------------
# datetime_format filter: Mountain time, DST-boundary correctness
# ---------------------------------------------------------------------------

# Precise UTC instants of America/Edmonton's 2024 DST transitions (from
# pytz's own transition table), one second either side of each:
SPRING_FORWARD_MST = 1710061199  # 2024-03-10 01:59:59 MST (UTC-7)
SPRING_FORWARD_MDT = 1710061200  # 2024-03-10 03:00:00 MDT (UTC-6) -- same instant as above +1s
FALL_BACK_MDT = 1730620799       # 2024-11-03 01:59:59 MDT (UTC-6)
FALL_BACK_MST = 1730620800       # 2024-11-03 01:00:00 MST (UTC-7) -- same instant as above +1s


@pytest.mark.parametrize('epoch,expected', [
    (SPRING_FORWARD_MST, '2024-03-10 01:59:59 MST'),
    (SPRING_FORWARD_MDT, '2024-03-10 03:00:00 MDT'),
    (FALL_BACK_MDT, '2024-11-03 01:59:59 MDT'),
    (FALL_BACK_MST, '2024-11-03 01:00:00 MST'),
])
def test_datetime_format_filter_dst_boundaries(pathway, epoch, expected):
    result = pathway._datetime_format_filter(epoch, '%Y-%m-%d %H:%M:%S %Z')
    assert result == expected


def test_datetime_format_filter_default_format_includes_tz_abbreviation(pathway):
    result = pathway._datetime_format_filter(1710061200)
    assert result.endswith('MDT')


def test_datetime_format_filter_invalid_input_returns_str_not_raise(pathway):
    result = pathway._datetime_format_filter('not-a-timestamp')
    assert result == 'not-a-timestamp'


def test_datetime_format_filter_none_returns_str_not_raise(pathway):
    result = pathway._datetime_format_filter(None)
    assert result == 'None'


# ---------------------------------------------------------------------------
# metadata_table: omitted when empty, present when not
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('template_file', SINGLE_TEMPLATES)
@pytest.mark.parametrize('empty_metadata', [{}, None])
def test_metadata_section_omitted_when_empty(pathway, template_file, empty_metadata):
    html = _render_single(pathway, template_file, metadata=empty_metadata)
    assert 'Additional Information' not in html
    assert 'em-metadata-table' not in html


@pytest.mark.parametrize('template_file', SINGLE_TEMPLATES)
def test_metadata_section_present_when_populated(pathway, template_file):
    html = _render_single(pathway, template_file, metadata={'disk_path': '/data', 'percent_full': 98})
    assert 'Additional Information' in html
    assert 'em-metadata-table' in html
    assert 'Disk Path' in html  # key|replace('_',' ')|title
    assert '/data' in html
    assert '98' in html


# ---------------------------------------------------------------------------
# level_badge: exact THEME_SPECIFICATION.md hex values, every level
# ---------------------------------------------------------------------------

LEVEL_LIGHT_COLORS = {
    'critical': '#c0392b',
    'warning': '#d68910',
    'notice': '#148f77',
    'info': '#007acc',
    'debug': '#487731',  # derived -- see alert_macros.html.j2 docstring
}


@pytest.mark.parametrize('level,hex_color', LEVEL_LIGHT_COLORS.items())
def test_level_badge_uses_exact_theme_spec_color(pathway, level, hex_color):
    html = _render_single(pathway, 'emails/alert_default.html.j2', level=level)
    assert hex_color in html
    assert f'em-badge-{level}' in html


def test_dark_mode_css_defines_override_for_every_level(pathway):
    html = _render_single(pathway, 'emails/alert_default.html.j2')
    assert '@media (prefers-color-scheme: dark)' in html
    for level in LEVEL_LIGHT_COLORS:
        assert f'.em-badge-{level} {{' in html
        assert f'.em-level-border-{level} {{' in html


# ---------------------------------------------------------------------------
# client_banner: single client / multiple clients / neither
# ---------------------------------------------------------------------------

def test_client_banner_single_client(pathway):
    html = _render_single(pathway, 'emails/alert_default.html.j2', client='acme')
    assert 'CLIENT: ACME' in html


def test_client_banner_multiple_clients_on_storm_template(pathway):
    alerts = [_alert(title='a', client='acme'), _alert(title='b', client='globex')]
    html = _render_storm(pathway, 'emails/alert_default_aggregated.html.j2', alerts=alerts)
    assert 'Multiple clients: acme, globex' in html


def test_client_banner_absent_when_no_client(pathway):
    html = _render_single(pathway, 'emails/alert_default.html.j2', client='')
    assert 'em-client-banner' not in html


# ---------------------------------------------------------------------------
# chip: renders nothing for a falsy value (tested directly via the macro)
# ---------------------------------------------------------------------------

def test_chip_macro_renders_nothing_for_empty_value(pathway):
    template = pathway._jinja_env.from_string(
        "{% import 'emails/components/alert_macros.html.j2' as macros %}"
        "[{{ macros.chip('Source', source) }}]"
    )
    html = template.render(source='')
    assert html.strip() == '[]'


def test_chip_macro_renders_value_when_present(pathway):
    template = pathway._jinja_env.from_string(
        "{% import 'emails/components/alert_macros.html.j2' as macros %}"
        "{{ macros.chip('Source', source) }}"
    )
    html = template.render(source='monitor-01')
    assert 'monitor-01' in html
    assert 'em-chip' in html
