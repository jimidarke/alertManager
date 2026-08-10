# Email Templates Guide

## Overview

The Analytics Engine uses **Jinja2** templates for email report generation, providing:
- **Separation of concerns**: Logic vs presentation
- **Reusability**: Shared components across reports
- **Maintainability**: Update designs without touching Python code
- **Professional emails**: Automatic inline CSS conversion via premailer

## Architecture

```
src/templates/
├── emails/
│   ├── base.html.j2                    # Base email wrapper (all reports extend this)
│   ├── barcode_not_found.html.j2       # Barcode report template
│   └── components/                     # Reusable components
│       ├── summary_card.html.j2         # Summary metrics card
│       ├── data_table.html.j2           # Data table with styling
│       └── insight_box.html.j2          # Insight/alert box
```

## Base Template

All email templates extend `emails/base.html.j2`:

```jinja2
{% extends "emails/base.html.j2" %}

{% block content %}
  <!-- Your report content here -->
{% endblock %}
```

### Base Template Variables

| Variable | Type | Description |
|----------|------|-------------|
| `report_title` | string | Email header title |
| `report_subtitle` | string | Subtitle below title (optional) |
| `client_name` | string | Client name for footer |
| `generation_time` | string | Generation timestamp |
| `support_email` | string | Support contact (optional) |

## Reusable Components

### Summary Card

Display key metrics in a styled card:

```jinja2
{% set metrics = [
    {'label': 'Total Count', 'value': 42, 'color': '#e74c3c'},
    {'label': 'Success Rate', 'value': '95%', 'color': '#27ae60'}
] %}
{% include "emails/components/summary_card.html.j2" %}
```

**Parameters:**
- `metrics` (required): List of metric dictionaries
  - `label`: Metric name
  - `value`: Metric value
  - `color`: Text color (default: '#34495e')
  - `size`: Font size (default: '16px')
  - `weight`: Font weight (default: 'normal')
- `title` (optional): Card title

### Data Table

Display tabular data with alternating row colors:

```jinja2
{% set headers = [
    {'label': 'Name', 'width': '40%'},
    {'label': 'Count', 'width': '30%'},
    {'label': 'Status', 'width': '30%'}
] %}
{% set rows = [
    {'cells': [
        {'value': 'Item 1', 'style': 'font-weight: bold;'},
        {'value': 100, 'style': 'color: #27ae60;'},
        {'value': 'Active', 'style': ''}
    ]}
] %}
{% include "emails/components/data_table.html.j2" %}
```

**Parameters:**
- `headers` (required): List of header dictionaries
  - `label`: Column header text
  - `width`: Column width (e.g., '20%', 'auto')
- `rows` (required): List of row dictionaries
  - `cells`: List of cell dictionaries
    - `value`: Cell content
    - `style`: Inline CSS styles
- `table_title` (optional): Title above table
- `table_description` (optional): Description text
- `table_footer` (optional): Footer text/HTML
- `table_font_size` (optional): Base font size (default: '13px')

### Insight Box

Highlight important insights or alerts:

```jinja2
{% set insight = {
    'title': 'Important Finding',
    'content': 'This metric exceeds the threshold by 25%.',
    'color': '#e67e22',
    'bg_color': '#fef5e7'
} %}
{% include "emails/components/insight_box.html.j2" %}
```

**Parameters:**
- `content` (required): Main insight text
- `title` (optional): Insight title
- `color` (optional): Left border color (default: '#3498db')
- `bg_color` (optional): Background color (default: '#ecf0f1')

## Creating Custom Templates

### Step 1: Create Template File

Create `src/templates/emails/my_report.html.j2`:

```jinja2
{% extends "emails/base.html.j2" %}

{% block content %}
<div style="margin-bottom: 30px;">
    <h2 style="color: #2c3e50;">My Custom Report</h2>
    <p>Analysis for {{ client_name }}</p>

    <!-- Use components -->
    {% include "emails/components/summary_card.html.j2" %}

    {% if has_data %}
    {% include "emails/components/data_table.html.j2" %}
    {% else %}
    <p style="color: #27ae60;">No issues found!</p>
    {% endif %}
</div>
{% endblock %}
```

### Step 2: Build Template Context in Analyzer

```python
def generate_email_report(self, results: Dict[str, Any]) -> str:
    context = {
        'report_title': 'My Custom Report',
        'client_name': self.config.get('client', 'Client').title(),
        'generation_time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
        'has_data': bool(results.get('items')),

        # Summary card data
        'metrics': [
            {'label': 'Total Items', 'value': len(results['items'])},
            {'label': 'Status', 'value': 'Active', 'color': '#27ae60'}
        ],

        # Table data
        'headers': [{'label': 'Item', 'width': '70%'}, {'label': 'Count', 'width': '30%'}],
        'rows': [
            {'cells': [{'value': item['name']}, {'value': item['count']}]}
            for item in results['items']
        ]
    }

    return self.render_email_template('emails/my_report.html.j2', context)
```

## Custom Filters

The template environment includes custom Jinja2 filters:

### datetime_format

Format Unix timestamp:

```jinja2
{{ timestamp|datetime_format }}
{{ timestamp|datetime_format('%Y-%m-%d') }}
```

### number_format

Format numbers with commas:

```jinja2
{{ 1234.56|number_format }}  {# Output: 1,234.56 #}
{{ 1234.56|number_format(0) }}  {# Output: 1,235 #}
```

## CSS and Styling

### Inline CSS Conversion

**Premailer** automatically converts CSS blocks to inline styles:

```jinja2
<style>
    .my-class {
        color: #ff0000;
        font-weight: bold;
    }
</style>
<p class="my-class">This text will have inline styles</p>
```

Becomes:
```html
<p style="color: #ff0000; font-weight: bold;">This text will have inline styles</p>
```

### Email-Safe Styling

**Best Practices:**
- Use table-based layouts
- Inline CSS (handled automatically)
- Email-safe fonts: Arial, Verdana, Georgia, Times New Roman
- Fixed widths in pixels (600px max for email body)
- Avoid: flexbox, grid, external stylesheets, JavaScript

### Color Scheme

Standard colors used across templates:

```python
COLOR_SCHEME = {
    # Status colors
    'success': '#27ae60',
    'warning': '#e67e22',
    'error': '#e74c3c',
    'info': '#3498db',

    # UI colors
    'primary': '#2c3e50',
    'secondary': '#34495e',
    'muted': '#7f8c8d',
    'light_bg': '#ecf0f1',
    'white': '#ffffff',

    # Metrics
    'low': '#7f8c8d',
    'medium': '#e67e22',
    'high': '#c0392b'
}
```

## Query Definition Integration

Link templates to query definitions:

**src/query_definitions/my_query.json:**
```json
{
  "query_name": "my_analysis",
  "email_template": {
    "template_name": "my_report.html.j2",
    "report_title": "My Analysis Report",
    "color_scheme": {
      "low": "#95a5a6",
      "high": "#e74c3c"
    }
  }
}
```

**Analyzer uses query definition:**
```python
def generate_email_report(self, results):
    template_name = 'emails/barcode_not_found.html.j2'  # Default

    if self.query_def:
        email_template = self.query_def.get('email_template', {})
        template_name = f"emails/{email_template.get('template_name', 'barcode_not_found.html.j2')}"
        # Use color_scheme from query def
        colors = email_template.get('color_scheme', {})

    context = self._build_context(results, colors)
    return self.render_email_template(template_name, context)
```

## Testing Templates

### 1. Render Test

Create a test script:

```python
from jinja2 import Environment, FileSystemLoader
from premailer import Premailer

env = Environment(loader=FileSystemLoader('src/templates'))
template = env.get_template('emails/barcode_not_found.html.j2')

context = {
    'report_title': 'Test Report',
    'total_failures': 42,
    'unique_barcodes': 15,
    # ... more test data
}

html = template.render(**context)
inline_html = Premailer(html).transform()

with open('test_email.html', 'w') as f:
    f.write(inline_html)
```

### 2. Email Client Testing

Test rendered emails in:
- Gmail (web and mobile)
- Outlook (desktop and web)
- Apple Mail
- Thunderbird

### 3. Validation Tools

- **[Litmus](https://litmus.com/)**: Email preview across clients
- **[Email on Acid](https://www.emailonacid.com/)**: Email testing platform
- **HTML Email Check**: Validate HTML email compatibility

## Common Patterns

### Conditional Sections

```jinja2
{% if results.critical_items %}
<div style="background-color: #ffebee; padding: 15px;">
    <h3 style="color: #c0392b;">Critical Issues</h3>
    {% for item in results.critical_items %}
    <p>{{ item.description }}</p>
    {% endfor %}
</div>
{% endif %}
```

### Looping with Colors

```jinja2
{% for item in items %}
<tr style="background-color: {% if loop.index % 2 == 0 %}#f9f9f9{% else %}#ffffff{% endif %};">
    <td>{{ item.name }}</td>
    <td>{{ item.value }}</td>
</tr>
{% endfor %}
```

### Dynamic Color Coding

```jinja2
{% set color = '#27ae60' if value > 90 else '#e67e22' if value > 70 else '#e74c3c' %}
<span style="color: {{ color }}; font-weight: bold;">{{ value }}%</span>
```

## Advanced Features

### Macros

Define reusable template functions:

```jinja2
{% macro render_metric(label, value, color='#34495e') %}
<div style="margin: 10px 0;">
    <span style="font-weight: bold;">{{ label }}:</span>
    <span style="color: {{ color }};">{{ value }}</span>
</div>
{% endmacro %}

{{ render_metric('Total', 42, '#e74c3c') }}
{{ render_metric('Active', 38, '#27ae60') }}
```

### Template Inheritance Chains

```jinja2
{# base_report.html.j2 - extends base.html.j2 #}
{% extends "emails/base.html.j2" %}

{% block content %}
<div class="report-header">
    {% block report_header %}{% endblock %}
</div>
<div class="report-body">
    {% block report_body %}{% endblock %}
</div>
{% endblock %}

{# specific_report.html.j2 - extends base_report.html.j2 #}
{% extends "emails/base_report.html.j2" %}

{% block report_header %}
<h2>Specific Report</h2>
{% endblock %}

{% block report_body %}
<p>Report content...</p>
{% endblock %}
```

## Troubleshooting

### Template Not Found

```
jinja2.exceptions.TemplateNotFound: emails/my_report.html.j2
```

**Solution:** Check template path is relative to `src/templates/`

### CSS Not Inlined

**Problem:** Styles in `<style>` blocks not converting to inline

**Solution:** Ensure premailer is installed and configured:
```python
from premailer import Premailer
inline_html = Premailer(html, strip_important=False).transform()
```

### Variables Not Rendering

```jinja2
{{ undefined_variable }}  <!-- Shows nothing -->
```

**Solution:** Pass all variables in context or use defaults:
```jinja2
{{ undefined_variable|default('Default Value') }}
```

## Resources

- **Jinja2 Documentation**: https://jinja.palletsprojects.com/
- **Premailer Documentation**: https://github.com/peterbe/premailer
- **Email Design Guide**: https://www.campaignmonitor.com/dev-resources/guides/coding/
- **HTML Email Templates**: https://templates.mailchimp.com/

## Summary

✅ **Template-based design** separates presentation from logic
✅ **Reusable components** reduce duplication
✅ **Automatic inline CSS** ensures email client compatibility
✅ **Query definition integration** links queries to templates
✅ **Custom filters** for formatting timestamps and numbers
✅ **Multi-client support** through parameterized templates
