#!/bin/bash
# SCIM 2.0 endpoint test script
# Usage: ./test_scim.sh [BASE_URL] [TOKEN]
#
# First run:
#   1. docker compose up -d
#   2. Open http://localhost:8069, finish setup wizard
#   3. Settings > Integrations > enable SCIM 2.0 Provisioning > Save
#   4. Click "SCIM Tokens", create a token, copy it
#   5. Run: ./test_scim.sh http://localhost:8069 YOUR_TOKEN

set -euo pipefail

BASE="${1:-http://localhost:8069}/scim/v2"
TOKEN="${2:?Usage: $0 BASE_URL TOKEN}"
PASS=0
FAIL=0
USER_ID=""

check() {
    local name="$1" expected_status="$2" actual_status="$3" body="$4"
    if [ "$actual_status" -eq "$expected_status" ]; then
        echo "  PASS  $name (HTTP $actual_status)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $name (expected $expected_status, got $actual_status)"
        echo "        Response: $body"
        FAIL=$((FAIL + 1))
    fi
}

call() {
    local method="$1" url="$2" data="${3:-}"
    if [ -n "$data" ]; then
        curl -s -w "\n%{http_code}" -X "$method" \
            -H "Authorization: Bearer $TOKEN" \
            -H "Content-Type: application/scim+json" \
            -d "$data" "$url"
    else
        curl -s -w "\n%{http_code}" -X "$method" \
            -H "Authorization: Bearer $TOKEN" "$url"
    fi
}

parse_response() {
    local response="$1"
    BODY=$(echo "$response" | head -n -1)
    STATUS=$(echo "$response" | tail -n 1)
}

echo "=== SCIM 2.0 Endpoint Tests ==="
echo "Base URL: $BASE"
echo ""

# --- Discovery ---
echo "-- Discovery Endpoints --"

parse_response "$(call GET "$BASE/ServiceProviderConfig")"
check "ServiceProviderConfig" 200 "$STATUS" "$BODY"

parse_response "$(call GET "$BASE/Schemas")"
check "Schemas" 200 "$STATUS" "$BODY"

parse_response "$(call GET "$BASE/ResourceTypes")"
check "ResourceTypes" 200 "$STATUS" "$BODY"

# --- Auth ---
echo ""
echo "-- Authentication --"

RESP=$(curl -s -w "\n%{http_code}" "$BASE/Users")
parse_response "$RESP"
check "No token → 401" 401 "$STATUS" "$BODY"

RESP=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer invalid-token" "$BASE/Users")
parse_response "$RESP"
check "Bad token → 401" 401 "$STATUS" "$BODY"

# --- Users CRUD ---
echo ""
echo "-- User Operations --"

# List users (empty filter)
parse_response "$(call GET "$BASE/Users")"
check "List users" 200 "$STATUS" "$BODY"

# Search non-existent user (Entra connection test)
parse_response "$(call GET "$BASE/Users?filter=userName%20eq%20%22nonexistent%40test.com%22")"
check "Filter non-existent user → empty list" 200 "$STATUS" "$BODY"
TOTAL=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('totalResults','-'))" 2>/dev/null || echo "?")
echo "        totalResults: $TOTAL"

# Create user
parse_response "$(call POST "$BASE/Users" '{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "userName": "scimtest@example.com",
    "name": {"givenName": "SCIM", "familyName": "Test"},
    "displayName": "SCIM Test",
    "emails": [{"value": "scimtest@example.com", "type": "work", "primary": true}],
    "externalId": "entra-test-001",
    "active": true
}')"
check "Create user" 201 "$STATUS" "$BODY"
USER_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
echo "        Created user ID: $USER_ID"

# Create duplicate → 409
if [ -n "$USER_ID" ]; then
    parse_response "$(call POST "$BASE/Users" '{
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": "scimtest@example.com",
        "externalId": "entra-test-001",
        "active": true
    }')"
    check "Duplicate user → 409" 409 "$STATUS" "$BODY"
fi

# Get single user
if [ -n "$USER_ID" ]; then
    parse_response "$(call GET "$BASE/Users/$USER_ID")"
    check "Get user by ID" 200 "$STATUS" "$BODY"
    USERNAME=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('userName',''))" 2>/dev/null || echo "?")
    echo "        userName: $USERNAME"
fi

# Filter by userName (Entra-style)
parse_response "$(call GET "$BASE/Users?filter=userName%20eq%20%22scimtest%40example.com%22")"
check "Filter by userName" 200 "$STATUS" "$BODY"
TOTAL=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('totalResults','-'))" 2>/dev/null || echo "?")
echo "        totalResults: $TOTAL"

# Filter by externalId
parse_response "$(call GET "$BASE/Users?filter=externalId%20eq%20%22entra-test-001%22")"
check "Filter by externalId" 200 "$STATUS" "$BODY"

# PATCH user — Entra style (capital Replace, string boolean)
if [ -n "$USER_ID" ]; then
    parse_response "$(call PATCH "$BASE/Users/$USER_ID" '{
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "Replace", "path": "displayName", "value": "SCIM Updated"}
        ]
    }')"
    check "PATCH displayName" 200 "$STATUS" "$BODY"
    DISPLAY=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('displayName',''))" 2>/dev/null || echo "?")
    echo "        displayName: $DISPLAY"
fi

# PATCH deactivate — Entra style (string "False")
if [ -n "$USER_ID" ]; then
    parse_response "$(call PATCH "$BASE/Users/$USER_ID" '{
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "Replace", "path": "active", "value": "False"}
        ]
    }')"
    check "PATCH deactivate (Entra-style)" 200 "$STATUS" "$BODY"
    ACTIVE=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('active',''))" 2>/dev/null || echo "?")
    echo "        active: $ACTIVE"
fi

# PATCH reactivate
if [ -n "$USER_ID" ]; then
    parse_response "$(call PATCH "$BASE/Users/$USER_ID" '{
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [
            {"op": "Replace", "path": "active", "value": "True"}
        ]
    }')"
    check "PATCH reactivate" 200 "$STATUS" "$BODY"
fi

# PUT full replace
if [ -n "$USER_ID" ]; then
    parse_response "$(call PUT "$BASE/Users/$USER_ID" '{
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": "scimtest@example.com",
        "name": {"givenName": "SCIM", "familyName": "Replaced"},
        "displayName": "SCIM Replaced",
        "emails": [{"value": "scimtest@example.com", "type": "work", "primary": true}],
        "active": true
    }')"
    check "PUT full replace" 200 "$STATUS" "$BODY"
fi

# DELETE (deactivate)
if [ -n "$USER_ID" ]; then
    parse_response "$(call DELETE "$BASE/Users/$USER_ID")"
    check "DELETE user → 204" 204 "$STATUS" "$BODY"
fi

# GET non-existent user
parse_response "$(call GET "$BASE/Users/999999")"
check "Get non-existent user → 404" 404 "$STATUS" "$BODY"

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
