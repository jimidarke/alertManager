#!/usr/bin/env python3
"""
Render Email Preview

Renders one of Alert Manager's pathway alert-email Jinja2 templates with
canned single-alert or storm-alert data and prints the resulting HTML to
stdout, for manual inspection in a browser. No Graph API call, SMTP
send, or Postgres write happens -- this only exercises
EmailPathway._jinja_env (the same Jinja2 environment/filters production
sends use), so what you see is exactly what a real send would produce
for the given input.

This project has no headless browser or real mail-sending available in
automated/CI environments, so this script -- plus careful manual
HTML/CSS reading of its output -- is the primary way to verify a
template renders sane markup before shipping. It is NOT a substitute
for real mail-client verification: Gmail, Outlook (desktop + web), and
iOS Mail each have their own quirks (especially around
@media (prefers-color-scheme: dark) support and CSS class handling) that
only show up when an actual client renders the HTML. Do that manual
check, in both light and dark mode, before relying on these templates
for a real send.

Usage:
    tools/tests/render_email_preview.py --list
    tools/tests/render_email_preview.py --template alert_default > /tmp/preview.html
    tools/tests/render_email_preview.py --template alert_default --level warning --no-metadata
    tools/tests/render_email_preview.py --template alert_default_aggregated --clients acme,globex
    tools/tests/render_email_preview.py --template revysmc_critical --client revysmc --out /tmp/rsc.html
"""

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))          # `pathways` is a top-level package
sys.path.insert(0, str(_ROOT / "src"))  # email_pathway.py does `from version import __version__`

from pathways.email_pathway import EmailPathway  # noqa: E402


# Shortcut name -> (template file relative to src/templates/, 'single'|'storm')
TEMPLATES = {
    'alert_default': ('emails/alert_default.html.j2', 'single',
                       'Redesigned single-alert template (default for a bare email pathway)'),
    'alert_default_aggregated': ('emails/alert_default_aggregated.html.j2', 'storm',
                                  'Redesigned storm/aggregated template (default aggregated_template)'),
    'revysmc_critical': ('emails/revysmc_critical.html.j2', 'single',
                          'RSC-branded single-alert template'),
    'revysmc_aggregated': ('emails/revysmc_aggregated.html.j2', 'storm',
                            'RSC-branded storm/aggregated template'),
    'aggregated_alerts': ('emails/aggregated_alerts.html.j2', 'storm',
                           'Older generic storm template (still referenced by docker.json email_critical)'),
}

ALERT_TITLES = [
    ('Disk usage critical on monitor-01', 'Disk /data is at 98% capacity. Auto-cleanup did not free enough space.', 'monitor-01', 'hardware'),
    ('Database connection lost', 'db-01 refused connection after 3 retries over 6 seconds.', 'db-01', 'system'),
    ('API gateway 5xx spike', 'Error rate at 42% over the last minute for /api/v1/orders.', 'api-gateway', 'application'),
]


def _dummy_pathway() -> EmailPathway:
    """
    An EmailPathway instance used only for its configured Jinja2
    environment (_jinja_env, including the datetime_format filter) --
    no network call is ever made, so the Graph credentials below are
    unused placeholders.
    """
    config = {
        'type': 'email',
        'enabled': True,
        'tenant_id': 'preview-tenant',
        'client_id': 'preview-client',
        'client_secret': 'preview-secret',
        'from_address': 'alerts@example.com',
        'to_addresses': ['preview@example.com'],
    }
    return EmailPathway('render_email_preview', config)


def _canned_single_alert(args: argparse.Namespace) -> dict:
    title, message, source, category = ALERT_TITLES[0]
    alert = {
        'timestamp': args.epoch,
        'level': args.level,
        'category': args.category or category,
        'title': title,
        'message': (
            f"{message}\n"
            "Manual intervention required within 1 hour.\n"
            "See runbook: https://internal.example.com/runbooks/disk-cleanup"
        ),
        'source': source,
        'client': args.client,
    }
    if not args.no_metadata:
        alert['metadata'] = {
            'disk_path': '/data',
            'percent_full': 98,
            'free_bytes': 1048576,
            'auto_cleanup_attempted': True,
        }
    return alert


def _canned_storm_alerts(args: argparse.Namespace) -> list:
    clients = [c.strip() for c in args.clients.split(',')] if args.clients else [args.client]
    alerts = []
    for i, (title, message, source, category) in enumerate(ALERT_TITLES):
        alerts.append({
            'timestamp': args.epoch + i * 3,
            'level': args.level,
            'category': args.category or category,
            'title': title,
            'message': message,
            'source': source,
            'client': clients[i % len(clients)],
        })
    return alerts


def _storm_context(alerts: list, window: int) -> dict:
    timestamps = [a['timestamp'] for a in alerts]
    client, clients = EmailPathway._client_context(alerts)
    return {
        'alert_count': len(alerts),
        'alerts': alerts,
        'first_timestamp': min(timestamps),
        'last_timestamp': max(timestamps),
        'unique_sources': sorted({a['source'] for a in alerts}),
        'duration_seconds': max(timestamps) - min(timestamps),
        'aggregation_window': window,
        'level': alerts[0]['level'],
        'client': client,
        'clients': clients,
        'version': '0.0.0-preview',
        'generated_at': int(time.time()),
    }


def render(template_name: str, args: argparse.Namespace) -> str:
    if template_name not in TEMPLATES:
        known = ', '.join(sorted(TEMPLATES))
        raise SystemExit(f"Unknown template '{template_name}'. Known: {known}")

    template_file, kind, _description = TEMPLATES[template_name]
    pathway = _dummy_pathway()
    template = pathway._jinja_env.get_template(template_file)

    if kind == 'single':
        alert = _canned_single_alert(args)
        context = {**alert, 'version': '0.0.0-preview', 'generated_at': int(time.time())}
    else:
        alerts = _canned_storm_alerts(args)
        context = _storm_context(alerts, args.window)

    return template.render(**context)


def list_templates() -> None:
    print(f"{'Shortcut':<28} {'Kind':<8} Description")
    print("-" * 90)
    for name, (path, kind, description) in sorted(TEMPLATES.items()):
        print(f"{name:<28} {kind:<8} {description}")
        print(f"{'':<28} {'':<8} (file: {path})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Render an Alert Manager email template with canned data and print HTML to stdout.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--template', '-t', choices=sorted(TEMPLATES), help='Template shortcut to render (see --list)')
    parser.add_argument('--list', '-l', action='store_true', help='List available template shortcuts and exit')
    parser.add_argument('--out', '-o', help='Write HTML to this file instead of stdout')
    parser.add_argument('--level', default='critical', help="Alert level (default: critical)")
    parser.add_argument('--category', default=None, help='Alert category override (default: varies per canned alert)')
    parser.add_argument('--client', default='acme', help="Client name for single-alert templates / fallback for storm (default: acme)")
    parser.add_argument('--clients', default=None, help="Comma-separated client list for storm templates, to preview the 'Multiple clients: a, b' case (e.g. --clients acme,globex)")
    parser.add_argument('--no-metadata', action='store_true', help='Omit the metadata dict, to preview the metadata_table omission behavior')
    parser.add_argument('--epoch', type=int, default=1710061200, help='Base Unix epoch timestamp (default: 2024-03-10 03:00:00 MDT, just past the spring-forward DST boundary)')
    parser.add_argument('--window', type=int, default=10, help='Storm aggregation_window seconds shown in storm templates (default: 10)')

    args = parser.parse_args()

    if args.list:
        list_templates()
        return

    if not args.template:
        parser.error('--template is required (or pass --list to see available shortcuts)')

    html = render(args.template, args)

    if args.out:
        Path(args.out).write_text(html, encoding='utf-8')
        print(f"Wrote {len(html)} bytes to {args.out}", file=sys.stderr)
    else:
        print(html)


if __name__ == '__main__':
    main()
