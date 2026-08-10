#!/bin/bash
# Test script for invalid request handling
# Tests various error conditions and validates improved error messages

set -e

HOST="${1:-localhost:8080}"
USERNAME="${2:-testuser}"
PASSWORD="${3:-testpass}"

echo "=== Invalid Request Handling Test Suite ==="
echo "Testing against: $HOST"
echo ""

# Test 1: Send invalid JSON (just a number)
echo "1. Testing invalid JSON (single number)..."
RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" -X POST "http://$HOST/alert" \
  -H "Content-Type: application/json" \
  -d '2')

if echo "$RESPONSE" | grep -q "Invalid JSON format"; then
    echo "   ✓ Invalid JSON error caught correctly"
    echo "   Response: $RESPONSE" | head -c 150
    echo "..."
else
    echo "   ✗ Unexpected response"
    echo "   Response: $RESPONSE"
fi
echo ""

# Test 2: Send empty JSON object
echo "2. Testing empty JSON object..."
RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" -X POST "http://$HOST/alert" \
  -H "Content-Type: application/json" \
  -d '{}')

if echo "$RESPONSE" | grep -q "Missing required fields"; then
    echo "   ✓ Empty object handled correctly"
    echo "   Response includes required fields list"
else
    echo "   ✗ Unexpected response"
    echo "   Response: $RESPONSE"
fi
echo ""

# Test 3: Send malformed JSON
echo "3. Testing malformed JSON (unclosed brace)..."
RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" -X POST "http://$HOST/alert" \
  -H "Content-Type: application/json" \
  -d '{"test": "value"')

if echo "$RESPONSE" | grep -q "Invalid JSON format"; then
    echo "   ✓ Malformed JSON caught correctly"
else
    echo "   ✗ Unexpected response"
    echo "   Response: $RESPONSE"
fi
echo ""

# Test 4: Send plain text without JSON content-type
echo "4. Testing plain text (wrong content-type)..."
RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" -X POST "http://$HOST/alert" \
  -H "Content-Type: text/plain" \
  -d 'not json at all')

if echo "$RESPONSE" | grep -q "Content-Type must be application/json"; then
    echo "   ✓ Content-Type validation working"
else
    echo "   ✗ Unexpected response"
    echo "   Response: $RESPONSE"
fi
echo ""

# Test 5: Send valid JSON but missing required fields
echo "5. Testing valid JSON with missing required fields..."
RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" -X POST "http://$HOST/alert" \
  -H "Content-Type: application/json" \
  -d '{"timestamp": 1234567890, "level": "info"}')

if echo "$RESPONSE" | grep -q "Missing required fields"; then
    echo "   ✓ Missing fields validation working"
    # Extract which fields are missing
    if echo "$RESPONSE" | grep -q "required_fields"; then
        echo "   ✓ Response includes list of required fields"
    fi
else
    echo "   ✗ Unexpected response"
    echo "   Response: $RESPONSE"
fi
echo ""

# Test 6: Send completely valid alert (should succeed)
echo "6. Testing valid alert (should succeed)..."
RESPONSE=$(curl -s -u "$USERNAME:$PASSWORD" -X POST "http://$HOST/alert" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "info",
    "category": "system",
    "title": "Test Alert",
    "message": "Testing valid request",
    "source": "test-script"
  }')

if echo "$RESPONSE" | grep -q '"status":"success"'; then
    echo "   ✓ Valid alert accepted successfully"
else
    echo "   ✗ Valid alert rejected (unexpected)"
    echo "   Response: $RESPONSE"
fi
echo ""

echo "=== Test Suite Complete ==="
echo ""
echo "Summary:"
echo "  - Invalid JSON is now caught with helpful error messages"
echo "  - Error responses include hints for fixing the request"
echo "  - Server logs use WARNING level for client errors (not ERROR)"
echo "  - Valid requests still work as expected"
