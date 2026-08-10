#!/bin/bash
# Test script for Log Viewer v1.0
# Tests the HTTP gateway log viewing endpoints

set -e

HOST="${1:-localhost:8080}"
USERNAME="${2:-testuser}"
PASSWORD="${3:-testpass}"

echo "=== Log Viewer v1.0 Test Suite ==="
echo "Testing against: $HOST"
echo ""

# Test 1: Health check (no auth)
echo "1. Testing health check endpoint (no auth required)..."
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST/health")
if [ "$HEALTH" = "200" ]; then
    echo "   ✓ Health check passed"
else
    echo "   ✗ Health check failed (HTTP $HEALTH)"
fi
echo ""

# Test 2: List log files (with auth)
echo "2. Testing log file listing endpoint (requires auth)..."
LIST_RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" "http://$HOST/api/logs/list")
if echo "$LIST_RESPONSE" | grep -q '"status":"success"'; then
    echo "   ✓ Log file listing succeeded"
    echo "   Response: $LIST_RESPONSE"
else
    echo "   ✗ Log file listing failed"
    echo "   Response: $LIST_RESPONSE"
fi
echo ""

# Test 3: Tail log file (with auth)
echo "3. Testing log tail endpoint (requires auth)..."
TAIL_RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" "http://$HOST/api/logs/tail?file=alerts.log&lines=5")
if echo "$TAIL_RESPONSE" | grep -q '"status":"success"'; then
    echo "   ✓ Log tail succeeded"
    echo "   Response preview: $(echo "$TAIL_RESPONSE" | head -c 200)..."
else
    echo "   ✗ Log tail failed"
    echo "   Response: $TAIL_RESPONSE"
fi
echo ""

# Test 4: Access log viewer page (with auth)
echo "4. Testing log viewer HTML page (requires auth)..."
VIEWER_RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" "http://$HOST/logs")
if echo "$VIEWER_RESPONSE" | grep -q "Alert Manager - Live Logs"; then
    echo "   ✓ Log viewer page loaded successfully"
    if echo "$VIEWER_RESPONSE" | grep -q "Version 1.0"; then
        echo "   ✓ Version 1.0 tag found"
    fi
    if echo "$VIEWER_RESPONSE" | grep -q "/static/css/log-viewer.css"; then
        echo "   ✓ External CSS reference found"
    fi
    if echo "$VIEWER_RESPONSE" | grep -q "/static/js/log-viewer.js"; then
        echo "   ✓ External JS reference found"
    fi
else
    echo "   ✗ Log viewer page failed to load"
fi
echo ""

# Test 5: Check static file serving
echo "5. Testing static file endpoints..."
CSS_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST/static/css/log-viewer.css")
if [ "$CSS_RESPONSE" = "200" ]; then
    echo "   ✓ CSS file accessible (HTTP $CSS_RESPONSE)"
else
    echo "   ✗ CSS file not accessible (HTTP $CSS_RESPONSE)"
fi

JS_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST/static/js/log-viewer.js")
if [ "$JS_RESPONSE" = "200" ]; then
    echo "   ✓ JS file accessible (HTTP $JS_RESPONSE)"
else
    echo "   ✗ JS file not accessible (HTTP $JS_RESPONSE)"
fi
echo ""

# Test 6: Verify CLIENTNODE.md link
echo "6. Testing documentation endpoint..."
DOCS_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -u "$USERNAME:$PASSWORD" "http://$HOST/docs")
if [ "$DOCS_RESPONSE" = "200" ]; then
    echo "   ✓ Documentation accessible (HTTP $DOCS_RESPONSE)"
else
    echo "   ✗ Documentation not accessible (HTTP $DOCS_RESPONSE)"
fi
echo ""

echo "=== Test Suite Complete ==="
echo ""
echo "To view the log viewer in a browser, navigate to:"
echo "  http://$HOST/logs"
echo ""
echo "Default credentials (if using test config):"
echo "  Username: $USERNAME"
echo "  Password: $PASSWORD"
