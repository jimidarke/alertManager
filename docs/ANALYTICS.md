# Analytics Engine - Scheduled Email Reports

## Overview

The Analytics Engine processes log files on a schedule and generates professional HTML email reports. It's designed as a reusable framework for creating any type of log analysis and summary reporting.

**Use Cases:**
- Weekly barcode lookup failure reports
- Daily error summaries
- Monthly usage statistics
- Security audit reports
- Any pattern-based log analysis

## Quick Start

### Run a Report Immediately (Dry Run)

```bash
# Test an analyzer without waiting for schedule
docker exec alert-analytics-engine python3 src/analytics_engine.py \
  --config /data/config/docker.json \
  --run-once <analyzer-name>

# Example: Run barcode analyzer
docker exec alert-analytics-engine python3 src/analytics_engine.py \
  --config /data/config/docker.json \
  --run-once acme-barcode-not-found
```

### View Logs

```bash
# Watch analytics engine logs
docker logs -f alert-analytics-engine

# Or view log file directly
tail -f /data/logs/ops/analytics-engine.$(date +%Y-%m-%d).log
```

### Start the Service

```bash
# Start with analytics profile
docker-compose --profile analytics up -d

# Rebuild after code changes
docker-compose build --no-cache analytics-engine
docker-compose --profile analytics restart analytics-engine
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Analytics Engine                            │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │   Scheduler  │───>│   Analyzer   │───>│ Email Pathway │      │
│  │  (schedule)  │    │  (analysis)  │    │   (O365)     │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│         │                   │                                    │
│         v                   v                                    │
│  ┌──────────────┐    ┌──────────────┐                          │
│  │    Config    │    │ Query Def    │                          │
│  │ (docker.json)│    │   (.json)    │                          │
│  └──────────────┘    └──────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
         │                   │
         v                   v
   /data/config/      /data/logs/file/
```

### Components

| Component | File | Purpose |
|-----------|------|---------|
| Analytics Engine | `src/analytics_engine.py` | Scheduler, orchestrator, email delivery |
| Base Analyzer | `src/analyzers/base_analyzer.py` | Log parsing, template rendering, shared utilities |
| Query Definitions | `src/query_definitions/*.json` | Pattern matching and extraction rules |
| Email Templates | `src/templates/emails/*.j2` | Jinja2 HTML email templates |
| Email Pathway | `src/pathways/email_pathway.py` | O365 Graph API integration |

## Configuration

### Main Config (docker.json)

```json
{
  "analytics": {
    "enabled": true,
    "run_on_startup": false,
    "logging": {
      "level": "INFO",
      "file": "/data/logs/ops/analytics-engine.{date}.log"
    },
    "email": {
      "enabled": true,
      "from_address": "alerts@yourdomain.com",
      "html_format": true
    },
    "analyzers": {
      "my-analyzer": {
        "enabled": true,
        "type": "barcode_not_found",
        "client": "clientname",
        "query_definition": "barcode_not_found.json",
        "log_path_pattern": "/data/logs/file/clientname/{date}.log",
        "rotation_period": "weekly",
        "analysis_period_days": 7,
        "schedule": {
          "frequency": "weekly",
          "day_of_week": "monday",
          "time": "09:00"
        },
        "email_recipients": ["recipient@example.com"],
        "email_subject": "Weekly Report - My Analysis"
      }
    }
  }
}
```

### Configuration Reference

#### Analytics Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | boolean | true | Enable/disable entire analytics engine |
| `run_on_startup` | boolean | false | Run all analyzers immediately when container starts |
| `logging.level` | string | INFO | Log level: DEBUG, INFO, WARNING, ERROR |
| `logging.file` | string | - | Log file path (supports `{date}` placeholder) |

#### Important Note: Time-Based Filtering

**All analyzers filter events by timestamp**, not by log file boundaries. This means:

- **`analysis_period_days: 7`** analyzes the last 7 days of events, even if log files contain more
- **Weekly log files** (Monday-Sunday) can be used for daily reports - only events from the target day are counted
- **Timestamp filtering** happens at the event level during analysis, ensuring accurate date-based aggregation
- Log files are selected first (based on `rotation_period`), then events within those files are filtered by timestamp

**Example:** A daily report with `analysis_period_days: 1` reading from a weekly log file will only count events from the last 24 hours, not the entire week's data in that file.

#### Email Settings

| Field | Type | Description |
|-------|------|-------------|
| `email.enabled` | boolean | Enable/disable email sending |
| `email.from_address` | string | Sender email address |
| `email.html_format` | boolean | Send HTML emails (should be true) |

#### Analyzer Settings

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Enable/disable this analyzer |
| `type` | string | Analyzer type (e.g., `barcode_not_found`) |
| `client` | string | Client name for log filtering and display |
| `query_definition` | string | Query definition filename in `src/query_definitions/` |
| `log_path_pattern` | string | Path to log files (supports `{date}`, `{client}`) |
| `rotation_period` | string | `daily` or `weekly` - must match log rotation |
| `analysis_period_days` | integer | Number of days to analyze (default: 7) |
| `schedule.frequency` | string | `daily`, `weekly`, or `monthly` |
| `schedule.day_of_week` | string | Day for weekly schedules (e.g., `monday`) |
| `schedule.day_of_month` | integer | Day for monthly schedules (1-28) |
| `schedule.time` | string | Time in 24-hour format (e.g., `09:00`) |
| `email_recipients` | array | List of email addresses |
| `email_subject` | string | Email subject line |

### Environment Variables

Email credentials are provided via `.env` file:

```bash
# Microsoft Graph API (O365) - Required
O365_TENANT_ID=your-azure-tenant-id
O365_CLIENT_ID=your-app-client-id
O365_CLIENT_SECRET=your-app-client-secret

# Optional override
EMAIL_FROM_ADDRESS=alerts@yourdomain.com
```

## Query Definitions

Query definitions separate pattern matching logic from code, making analyzers reusable and configurable.

### Location

`src/query_definitions/<name>.json`

### Structure

```json
{
  "query_name": "my_query",
  "query_version": "1.0.0",
  "description": "What this query finds",

  "log_pattern_matching": {
    "message_pattern": "Error occurred",
    "message_pattern_flags": ["IGNORECASE"],
    "level_filter": ["warning", "error", "critical"],
    "category_filter": ["application"],
    "source_filter": null,
    "client_filter": null
  },

  "data_extraction": {
    "fields": [
      {
        "name": "error_code",
        "pattern": "code=(\\d+)",
        "type": "string",
        "required": true,
        "description": "Error code from message"
      },
      {
        "name": "user_id",
        "pattern": "user=([a-zA-Z0-9]+)",
        "type": "string",
        "required": false,
        "default": "unknown",
        "description": "User ID if present"
      }
    ]
  },

  "aggregation": {
    "group_by": ["error_code"],
    "sort_by": "count",
    "sort_order": "desc"
  },

  "email_template": {
    "template_name": "my_report.html.j2",
    "report_title": "Error Report",
    "table_config": {
      "max_rows": 100,
      "columns": [
        {"header": "Error Code", "field": "error_code", "width": "50%"},
        {"header": "Count", "field": "count", "width": "50%", "align": "center"}
      ]
    }
  }
}
```

### Pattern Matching Fields

| Field | Description |
|-------|-------------|
| `title_pattern` | Regex to match in log line title field |
| `title_pattern_flags` | Flags like `["IGNORECASE"]` |
| `message_pattern` | Regex to match in log line message field |
| `message_pattern_flags` | Flags like `["IGNORECASE"]` |
| `level_filter` | Array of levels to include (e.g., `["warning", "error"]`) |
| `category_filter` | Array of categories to include |
| `source_filter` | Regex to filter by source (or null for all) |
| `client_filter` | Regex to filter by client (or null for all) |

### Data Extraction Fields

| Field | Description |
|-------|-------------|
| `name` | Field name in extracted data |
| `pattern` | Regex with capture group to extract value |
| `type` | Data type: `string`, `integer`, `float` |
| `required` | If true, skip record if field not found |
| `default` | Default value if not required and not found |

### Log Line Format

The base analyzer expects this log format:

```
timestamp [level] [category] source [client]: title - message
```

Example:
```
1764614071 [warning] [application] priceChecker.py:24 [acme-co]: get_price - Price check failed: Product not found for UPC=081702034609, device=d2f00c044f86d416
```

Parsed fields:
- `timestamp`: Unix epoch (integer)
- `level`: warning, error, info, etc.
- `category`: application, system, security, etc.
- `source`: Origin of the log entry
- `client`: Client/tenant identifier
- `title`: Short description (before ` - `)
- `message`: Full message content (after ` - `)

## Creating a New Analyzer

### Step 1: Create Query Definition

`src/query_definitions/my_analysis.json`:

```json
{
  "query_name": "my_analysis",
  "query_version": "1.0.0",
  "description": "Track specific events",

  "log_pattern_matching": {
    "message_pattern": "Event occurred",
    "message_pattern_flags": ["IGNORECASE"],
    "level_filter": ["info", "warning"],
    "category_filter": null
  },

  "data_extraction": {
    "fields": [
      {
        "name": "event_type",
        "pattern": "type=(\\w+)",
        "required": true
      }
    ]
  },

  "aggregation": {
    "group_by": ["event_type"],
    "sort_by": "count",
    "sort_order": "desc"
  },

  "email_template": {
    "template_name": "generic_report.html.j2",
    "report_title": "Event Summary",
    "table_config": {
      "columns": [
        {"header": "Event Type", "field": "event_type", "width": "60%"},
        {"header": "Count", "field": "count", "width": "40%"}
      ]
    }
  }
}
```

### Step 2: Create Analyzer Class (if needed)

For simple aggregations, you may be able to use an existing analyzer. For custom logic, create `src/analyzers/my_analyzer.py`:

```python
from typing import Dict, Any, List
from collections import defaultdict
from .base_analyzer import BaseAnalyzer


class MyAnalyzer(BaseAnalyzer):
    """Analyzer for tracking specific events"""

    def analyze(self) -> Dict[str, Any]:
        """Perform analysis on log files"""
        log_files = self.get_log_files()

        if not log_files:
            return self._empty_results()

        events = defaultdict(list)

        for log_file in log_files:
            try:
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        # Parse log line
                        parsed = self.parse_log_line(line)
                        if not parsed or not self.matches_query_pattern(parsed):
                            continue

                        # Extract fields using query definition
                        extracted = self.extract_fields(parsed)
                        if not extracted:
                            continue

                        # Aggregate by event type
                        event_type = extracted.get('event_type')
                        if event_type:
                            events[event_type].append({
                                'timestamp': extracted['timestamp']
                            })

            except Exception as e:
                self.logger.error(f"Error processing {log_file}: {e}")

        return {
            'total_events': sum(len(v) for v in events.values()),
            'unique_types': len(events),
            'events': dict(events)
        }

    def _empty_results(self) -> Dict[str, Any]:
        return {'total_events': 0, 'unique_types': 0, 'events': {}}

    def generate_email_report(self, results: Dict[str, Any]) -> str:
        """Generate HTML email report"""
        context = {
            'report_title': self.config.get('email_subject', 'Event Report'),
            'report_subtitle': f"Last {self.config.get('analysis_period_days', 7)} days",
            'total_events': results['total_events'],
            'unique_types': results['unique_types'],
            'has_data': bool(results['events']),
            # Build table data sorted by count descending
            'headers': [
                {'label': 'Event Type', 'width': '60%'},
                {'label': 'Count', 'width': '40%'}
            ],
            'rows': [
                {'cells': [
                    {'value': event_type, 'style': 'font-weight: bold;'},
                    {'value': len(occurrences), 'style': 'text-align: center;'}
                ]}
                for event_type, occurrences in sorted(
                    results['events'].items(),
                    key=lambda x: len(x[1]),
                    reverse=True
                )
            ]
        }

        return self.render_email_template('emails/generic_report.html.j2', context)
```

### Step 3: Register Analyzer

Edit `src/analytics_engine.py`, add to `_load_analyzers()`:

```python
elif analyzer_type == 'my_analyzer':
    from analyzers.my_analyzer import MyAnalyzer
    analyzer = MyAnalyzer(analyzer_name, analyzer_config, self.logger)
```

### Step 4: Add Configuration

Add to `docker.json`:

```json
{
  "analytics": {
    "analyzers": {
      "my-weekly-report": {
        "enabled": true,
        "type": "my_analyzer",
        "client": "myclient",
        "query_definition": "my_analysis.json",
        "log_path_pattern": "/data/logs/file/myclient/{date}.log",
        "rotation_period": "weekly",
        "analysis_period_days": 7,
        "schedule": {
          "frequency": "weekly",
          "day_of_week": "monday",
          "time": "09:00"
        },
        "email_recipients": ["admin@example.com"],
        "email_subject": "Weekly Event Summary"
      }
    }
  }
}
```

### Step 5: Test

```bash
# Rebuild container
docker-compose build --no-cache analytics-engine

# Run analyzer once to test
docker exec alert-analytics-engine python3 src/analytics_engine.py \
  --config /data/config/docker.json \
  --run-once my-weekly-report

# Check logs for errors
docker logs alert-analytics-engine --tail 50
```

## Email Templates

### Template Location

`src/templates/emails/`

### Base Template

All email templates extend `base.html.j2` which provides:
- HTML5 document structure
- 600px centered email wrapper
- Header with title and subtitle
- Footer with generation timestamp
- Inline CSS for email client compatibility

### Creating a Template

`src/templates/emails/my_report.html.j2`:

```jinja2
{% extends "emails/base.html.j2" %}

{% block content %}
<div style="margin-bottom: 20px;">
    <h2 style="color: #2c3e50; margin: 0;">Summary</h2>
    <p>Total events: <strong>{{ total_events }}</strong></p>
</div>

{% if has_data %}
{% include "emails/components/data_table.html.j2" %}
{% else %}
<p style="color: #7f8c8d;">No events found in the analysis period.</p>
{% endif %}
{% endblock %}
```

### Available Components

- `components/summary_card.html.j2` - Metric display cards
- `components/insight_box.html.j2` - Colored insight/alert boxes
- `components/data_table.html.j2` - Professional data tables

### Template Context Variables

The analyzer's `generate_email_report()` method should provide:

```python
context = {
    'report_title': 'Report Title',
    'report_subtitle': 'Subtitle or date range',
    'generation_time': '2024-01-15 09:00:00 UTC',
    'client_name': 'ClientName',
    'has_data': True,
    # For data_table.html.j2:
    'table_title': 'Data Table',
    'table_description': 'Description of the data',
    'headers': [{'label': 'Column', 'width': '50%'}],
    'rows': [{'cells': [{'value': 'data', 'style': 'css'}]}],
    'table_footer': 'Optional footer HTML'
}
```

## Testing & Debugging

### Dry Run (Run Once)

```bash
# Run specific analyzer immediately
docker exec alert-analytics-engine python3 src/analytics_engine.py \
  --config /data/config/docker.json \
  --run-once <analyzer-name>
```

### Enable Debug Logging

In `docker.json`:
```json
{
  "analytics": {
    "logging": {
      "level": "DEBUG"
    }
  }
}
```

### Check Log Files Exist

```bash
# List log files the analyzer will process
ls -la /data/logs/file/clientname/

# Check date format matches
ls /data/logs/file/clientname/*.log
```

### Verify Pattern Matching

Test your regex patterns against actual log lines:

```bash
# Find matching lines
grep -E "Price check failed" /data/logs/file/acme-co/*.log | head -5

# Test extraction
echo "1764614071 [warning] [application] priceChecker.py:24 [acme-co]: get_price - Price check failed: Product not found for UPC=081702034609" | \
  grep -oP "UPC=\K\d+"
```

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| No data in report | Pattern not matching | Check `message_pattern` vs actual log format |
| Email not sent | Credentials missing | Verify `.env` has O365 variables |
| Analyzer not running | Wrong schedule | Use `--run-once` to test immediately |
| Empty log file list | Wrong path pattern | Check `log_path_pattern` and `rotation_period` |

## Existing Analyzers

### Barcode Not Found (`barcode_not_found`)

Tracks failed UPC/barcode lookups from price checker logs, with timestamp-based filtering for accurate daily/weekly aggregation.

**Query Definition:** `barcode_not_found.json`

**Expected Log Format:**
```
1764614071 [warning] [application] priceChecker.py:24 [acme-co]: get_price - Price check failed: Product not found for UPC=081702034609, device=d2f00c044f86d416
1764614100 [info] [application] priceChecker.py:24 [acme-co]: get_price - Price check: UPC=123456789012
```

**Features:**
- Counts both failed lookups and successful scans
- Calculates success rate percentage
- Filters events by timestamp (honors `analysis_period_days`)
- UPC codes sorted by scan count (descending)
- Simple 2-column table: UPC Code, Count
- Timezone-aware date ranges in report

**Configuration Example:**
```json
{
  "acme-barcode-not-found": {
    "enabled": true,
    "type": "barcode_not_found",
    "client": "acme-co",
    "query_definition": "barcode_not_found.json",
    "log_path_pattern": "/data/logs/file/acme-co/{date}.log",
    "rotation_period": "weekly",
    "analysis_period_days": 7,
    "schedule": {
      "frequency": "weekly",
      "day_of_week": "monday",
      "time": "09:00"
    },
    "email_recipients": ["admin@example.com"],
    "email_subject": "Weekly Barcode Lookup Failure Report"
  }
}
```

## Best Practices

### Scheduling

- **Weekly reports**: Monday morning (e.g., 09:00) for previous week
- **Daily reports**: Early morning (06:00-08:00) for previous day
- **Monthly reports**: 1st of month, off-peak hours

### Performance

- Limit `analysis_period_days` to 7-30 days
- Use weekly log rotation for long-term efficiency
- Keep email tables under 100 rows

### Email Design

- Use inline CSS only (external stylesheets don't work)
- Keep total email size under 100KB
- Test across email clients (Outlook, Gmail, Apple Mail)
- Use table-based layouts for compatibility

## Related Documentation

- **[CLAUDE.md](../CLAUDE.md)** - Project structure and development patterns
- **[DATABASE_INTEGRATION.md](DATABASE_INTEGRATION.md)** - Database-backed analytics
- **[TEMPLATES_GUIDE.md](TEMPLATES_GUIDE.md)** - Email template development
