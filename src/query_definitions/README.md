# Query Definitions

This directory contains query configuration files that define how analyzers extract and process log data. Query definitions separate business logic (what to search for) from code (how to search), enabling easy customization for different clients without modifying code.

## Structure

Each query definition file contains:
- **Matching patterns**: Regex patterns to identify relevant log entries
- **Extraction patterns**: Patterns to extract specific data fields
- **Aggregation rules**: How to group and summarize data
- **Business rules**: Thresholds, filters, and conditions

## File Format

Query definitions are JSON files with this structure:

```json
{
  "query_name": "descriptive-name",
  "query_version": "1.0.0",
  "description": "What this query does",
  "log_pattern_matching": {
    "title_pattern": "regex pattern for title",
    "message_pattern": "regex pattern for message",
    "level_filter": ["critical", "warning"],
    "category_filter": ["application"]
  },
  "data_extraction": {
    "fields": [
      {
        "name": "field_name",
        "pattern": "regex with capture group",
        "type": "string|integer|float",
        "required": true
      }
    ]
  },
  "aggregation": {
    "group_by": ["field1", "field2"],
    "metrics": ["count", "unique_values", "first", "last"]
  },
  "business_rules": {
    "thresholds": {
      "warning": 5,
      "critical": 10
    },
    "filters": {
      "min_occurrences": 1
    }
  }
}
```

## Usage

Analyzers load query definitions using:

```python
from src.analyzers.base_analyzer import BaseAnalyzer

class MyAnalyzer(BaseAnalyzer):
    def __init__(self, name, config, logger):
        super().__init__(name, config, logger)
        self.query_def = self.load_query_definition('query_name.json')
```

## Creating New Query Definitions

1. **Copy an existing query definition** as a template
2. **Modify patterns** to match your log format
3. **Define extraction fields** for the data you need
4. **Set aggregation rules** for how to summarize
5. **Configure business rules** for thresholds and filters
6. **Reference in analyzer config** using `query_definition` field

## Example: Multiple Clients with Same Query

```json
// query_definitions/barcode_not_found.json - Base query
{
  "query_name": "barcode_not_found",
  "log_pattern_matching": {
    "title_pattern": "Price check failed"
  }
}

// Configuration for different clients:
{
  "analyzers": {
    "client1-barcodes": {
      "type": "barcode_not_found",
      "client": "client1",
      "query_definition": "barcode_not_found.json"
    },
    "client2-barcodes": {
      "type": "barcode_not_found",
      "client": "client2",
      "query_definition": "barcode_not_found.json"
    }
  }
}
```

## Benefits

- **Reusability**: One query definition, multiple clients
- **Maintainability**: Update patterns without touching code
- **Flexibility**: Override patterns per analyzer instance
- **Testability**: Test queries independently from code
- **Documentation**: Self-documenting query logic
