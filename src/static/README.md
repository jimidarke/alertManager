# Alert Manager Static Assets

This directory contains static assets for the HTTP gateway web interface.

## Structure

```
static/
├── css/
│   └── log-viewer.css       # Styles for real-time log viewer (Theme v1.1)
├── js/
│   └── log-viewer.js        # Real-time log streaming logic
├── favicon/                 # Browser and device icons
│   ├── favicon.ico          # Multi-size ICO (16/32/48)
│   ├── favicon-16x16.png
│   ├── favicon-32x32.png
│   ├── favicon-48x48.png
│   ├── apple-touch-icon.png # 180x180
│   ├── android-chrome-192x192.png
│   └── android-chrome-512x512.png
└── logo/
    └── AlertManager-512x512.png  # Square logo for header
```

## Theme Specification

The log viewer follows the Alert Manager Theme Specification v1.0.1 (see `docs/THEME_SPECIFICATION.md`).

### Color Variables

The CSS uses CSS custom properties for maintainability:

```css
:root {
    /* Background */
    --bg-primary: #1e1e1e;
    --bg-secondary: #252526;
    --bg-surface: #3c3c3c;

    /* Text */
    --text-primary: #d4d4d4;
    --text-secondary: #858585;
    --accent-primary: #007acc;

    /* Alert Levels */
    --level-critical: #f48771;
    --level-warning: #dcdcaa;
    --level-notice: #4ec9b0;
    --level-info: #9cdcfe;
    --level-debug: #6a9955;
}
```

## Log Viewer v1.1

The real-time log viewer provides a web-based interface for monitoring alert logs:

### Features

- **Real-time streaming**: Polls log files for new content
- **Multiple file support**: View different log files via dropdown selector
- **Color-coded levels**: Visual differentiation for critical, warning, notice, info, debug
- **Level filtering**: Toggle visibility of each log level
- **Timezone selection**: Localize timestamps to North American timezones
- **Interactive controls**: Pause/resume, clear display, refresh file list
- **Auto-scroll**: Automatically scrolls to newest logs
- **User access control**: Admin/node/client permissions
- **Ops log toggle**: Admin-only access to operational logs
- **Favicon support**: Proper browser tab icons

## Usage

### In Docker Container

The static files are mounted read-only into the container:

```yaml
volumes:
  - ./src/static:/app/src/static:ro
```

### Access

Navigate to `https://alert.d-t.pw/logs` (requires authentication). As of v2.0.0 the
gateway publishes no host port -- it is reachable only on 443 via `nginx-proxy`, so
the old `:8080` URL no longer resolves from outside the Docker host.

### Development

Edit CSS or JS files locally and rebuild the HTTP gateway container:

```bash
# `build http-gateway` is a no-op -- alert-router is the only service with a
# build context; http-gateway just reuses `image: alert-router:latest`.
docker-compose build alert-router
docker-compose up -d http-gateway
```

For CSS/JS changes only, a hard refresh (Ctrl+Shift+R) may be sufficient if the files are mounted.

## API Integration

The log viewer uses these authenticated API endpoints:

- `GET /api/logs/list` - Returns array of available .log files
- `GET /api/logs/tail?file=<name>&lines=<n>&position=<pos>` - Returns log lines

### Response Format

```json
{
  "status": "success",
  "lines": ["log line 1", "log line 2"],
  "position": 12345,
  "file_size": 12345,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

## Version History

- **v1.1** (2025-12-01)
  - Added CSS variables for theme consistency
  - Added favicon and logo support
  - Added header branding with logo
  - Added responsive design for mobile
  - Updated to Theme Specification v1.0.1

- **v1.0** (Initial Release)
  - Real-time log streaming
  - Multi-file support
  - Color-coded log levels
  - Level filtering
  - Timezone selection
  - Pause/resume functionality
  - Auto-scroll detection
