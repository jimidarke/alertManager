# Analytics Engine - Quick Start Guide

## Overview

The Analytics Engine analyzes Alert Manager logs and sends professional email reports on a scheduled basis. This guide will help you get the barcode failure analyzer up and running in minutes.

## Prerequisites

- Alert Manager with file logging enabled (`--profile logging`)
- Email credentials (O365 Graph API or SMTP)
- Docker and docker-compose

## Setup Steps

### 1. Configure Email Credentials

Add email credentials to your `.env` file or environment:

**For Microsoft O365 (Graph API) - Recommended:**
```bash
O365_TENANT_ID=your-tenant-id
O365_CLIENT_ID=your-client-id
O365_CLIENT_SECRET=your-client-secret
```

**For SMTP:**
```bash
SMTP_SERVER=smtp.office365.com
SMTP_USERNAME=alerts@yourdomain.com
SMTP_PASSWORD=your-password
```

### 2. Configure Analytics

The analytics configuration is already in `data/config/docker.json`. Update these fields:

```json
{
  "analytics": {
    "email": {
      "from_address": "alerts@yourdomain.com",  // UPDATE THIS
      "tenant_id": "",  // Set in .env as O365_TENANT_ID
      "client_id": "",  // Set in .env as O365_CLIENT_ID
      "client_secret": ""  // Set in .env as O365_CLIENT_SECRET
    },
    "analyzers": {
      "acme-barcode-not-found": {
        "enabled": true,
        "client": "acme-co",  // UPDATE if different
        "email_recipients": ["reports@example.com"]  // UPDATE THIS
      }
    }
  }
}
```

### 3. Build and Start Analytics Container

```bash
# Rebuild Docker image to include analytics code
docker-compose build --no-cache alert-router

# Start with analytics profile
docker-compose --profile analytics up -d

# Verify container is running
docker ps | grep analytics

# Check logs
docker logs -f alert-analytics-engine
```

### 4. Test the Analyzer

Run the analyzer once to test without waiting for schedule:

```bash
docker exec alert-analytics-engine python3 src/analytics_engine.py \
  --config /data/config/docker.json \
  --run-once acme-barcode-not-found
```

Check the logs:
```bash
tail -f data/logs/ops/analytics-engine.$(date +%Y-%m-%d).log
```

## Configuration Reference

### Schedule Configuration

```json
{
  "schedule": {
    "frequency": "weekly",      // Options: daily, weekly, monthly
    "day_of_week": "monday",    // For weekly: monday-sunday
    "time": "09:00"             // 24-hour format HH:MM
  }
}
```

**Examples:**
- **Daily at 8 AM**: `{"frequency": "daily", "time": "08:00"}`
- **Weekly on Friday at 5 PM**: `{"frequency": "weekly", "day_of_week": "friday", "time": "17:00"}`
- **Monthly on 1st at 6 AM**: `{"frequency": "monthly", "time": "06:00"}`

### Analyzer Configuration

```json
{
  "acme-barcode-not-found": {
    "enabled": true,                              // Enable/disable analyzer
    "type": "barcode_not_found",                  // Analyzer type
    "client": "acme-co",                         // Client name (log folder)
    "log_path_pattern": "/data/logs/file/acme-co/{date}.log",
    "rotation_period": "weekly",                  // Match your log rotation
    "analysis_period_days": 7,                    // How many days to analyze
    "title_pattern": "Price check failed",        // Regex to match in title
    "message_pattern": "Product not found for UPC=(\\d+)(?:,\\s*device=([a-f0-9]+))?",
    "schedule": { /* ... */ },
    "email_recipients": ["reports@example.com"],     // Who receives report
    "email_subject": "Weekly Barcode Lookup Failure Report - Acme Co"
  }
}
```

## Email Report Format

The report includes:

1. **Executive Summary**
   - Total failures
   - Unique barcodes count
   - Analysis period
   - Insight text

2. **Detailed Barcode Table**
   - UPC codes (sorted alphabetically)
   - Number of lookup attempts (color-coded)
   - First seen timestamp
   - Last seen timestamp
   - Device IDs

3. **Professional Styling**
   - Responsive layout
   - Color-coded metrics
   - Clean table design

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker logs alert-analytics-engine

# Common issues:
# 1. Config syntax error in docker.json
# 2. Missing email credentials
# 3. Invalid schedule configuration
```

### No Email Sent

1. **Verify email config**:
   ```bash
   docker exec alert-analytics-engine env | grep O365
   ```

2. **Test email pathway**:
   ```bash
   docker exec alert-analytics-engine python3 -c \
     "from pathways.email_pathway import EmailPathway; print('Email imports OK')"
   ```

3. **Check for errors in logs**:
   ```bash
   grep -i "error\|fail" data/logs/ops/analytics-engine.*.log
   ```

### No Log Files Found

1. **Check log path**:
   ```bash
   ls -la data/logs/file/acme-co/
   ```

2. **Verify pattern matches**:
   ```bash
   # For weekly rotation (Monday dates):
   ls data/logs/file/acme-co/2024-11-*.log
   ```

3. **Check rotation_period matches log configuration**:
   - If logs are daily, set `"rotation_period": "daily"`
   - If logs are weekly, set `"rotation_period": "weekly"`

### Analyzer Runs But No Results

1. **Check if logs contain matching data**:
   ```bash
   grep -i "price check failed" data/logs/file/acme-co/*.log
   ```

2. **Verify patterns match actual log format**:
   ```bash
   # View sample log line
   head -1 data/logs/file/acme-co/2024-11-18.log
   ```

3. **Enable debug logging**:
   ```json
   {
     "analytics": {
       "logging": {
         "level": "DEBUG"
       }
     }
   }
   ```

## Docker Commands

```bash
# Start analytics engine
docker-compose --profile analytics up -d

# Stop analytics engine
docker-compose stop analytics-engine

# Restart after config changes
docker-compose restart analytics-engine

# View logs (live)
docker logs -f alert-analytics-engine

# View operational logs (on disk)
tail -f data/logs/ops/analytics-engine.$(date +%Y-%m-%d).log

# Run analyzer manually
docker exec alert-analytics-engine python3 src/analytics_engine.py \
  --config /data/config/docker.json \
  --run-once acme-barcode-not-found

# Access container shell
docker exec -it alert-analytics-engine bash
```

## Advanced Configuration

### Multiple Email Recipients

```json
{
  "email_recipients": [
    "reports@example.com",
    "manager@example.com",
    "reports-team@example.com"
  ]
}
```

### Custom Analysis Period

```json
{
  "analysis_period_days": 14  // Analyze last 2 weeks
}
```

### Run on Startup (Testing)

```json
{
  "analytics": {
    "run_on_startup": true  // Run all analyzers when container starts
  }
}
```

## Next Steps

1. **Monitor First Run**: Watch logs to ensure analyzer runs successfully
2. **Verify Email Delivery**: Check spam folders if not received
3. **Add More Analyzers**: See `docs/ANALYTICS.md` for creating custom analyzers
4. **Tune Patterns**: Adjust regex patterns if needed for your log format

## Support

- **Full Documentation**: See `docs/ANALYTICS.md`
- **Configuration Examples**: See `examples/analytics-barcode-example.json`
- **Log Viewer**: Use HTTP gateway at `https://alert.d-t.pw/logs` to view logs (the `:8080` host port was removed in v2.0.0)

## Example: Complete docker.json Analytics Section

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
      "method": "graph",
      "from_address": "alerts@yourdomain.com",
      "tenant_id": "",
      "client_id": "",
      "client_secret": "",
      "html_format": true
    },
    "analyzers": {
      "acme-barcode-not-found": {
        "enabled": true,
        "type": "barcode_not_found",
        "client": "acme-co",
        "log_path_pattern": "/data/logs/file/acme-co/{date}.log",
        "rotation_period": "weekly",
        "analysis_period_days": 7,
        "title_pattern": "Price check failed",
        "message_pattern": "Product not found for UPC=(\\d+)(?:,\\s*device=([a-f0-9]+))?",
        "schedule": {
          "frequency": "weekly",
          "day_of_week": "monday",
          "time": "09:00"
        },
        "email_recipients": ["reports@example.com"],
        "email_subject": "Weekly Barcode Lookup Failure Report - Acme Co"
      }
    }
  }
}
```
