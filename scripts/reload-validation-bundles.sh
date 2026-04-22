#!/bin/bash
# Reload all validation bundles into the system.
#
# This script re-uploads all seed bundles from seed/connectathon-bundles/
# to the validation system. Useful when measures are missing from the HAPI engine
# but expected results are still in the database.
#
# Usage:
#   ./scripts/reload-validation-bundles.sh [API_BASE_URL]
#
# Examples:
#   ./scripts/reload-validation-bundles.sh                    # Uses http://localhost:8000
#   ./scripts/reload-validation-bundles.sh https://api.example.com

set -euo pipefail

API_BASE_URL="${1:-http://localhost:8000}"
BUNDLES_DIR="seed/connectathon-bundles"

if [ ! -d "$BUNDLES_DIR" ]; then
    echo "Error: Bundles directory not found: $BUNDLES_DIR"
    exit 1
fi

echo "Reloading validation bundles from: $BUNDLES_DIR"
echo "API endpoint: $API_BASE_URL"
echo ""

# Count bundles
BUNDLE_COUNT=$(find "$BUNDLES_DIR" -maxdepth 1 -name "*.json" -not -name "manifest.json" | wc -l)
echo "Found $BUNDLE_COUNT bundles to upload..."
echo ""

UPLOADED=0
FAILED=0

for bundle_file in "$BUNDLES_DIR"/*.json; do
    if [ "$(basename "$bundle_file")" = "manifest.json" ]; then
        continue
    fi

    FILENAME=$(basename "$bundle_file")
    echo -n "Uploading $FILENAME... "

    RESPONSE=$(curl -s -X POST \
        -F "file=@$bundle_file" \
        "$API_BASE_URL/validation/upload-bundle")

    if echo "$RESPONSE" | grep -q '"id"'; then
        UPLOAD_ID=$(echo "$RESPONSE" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
        echo "✓ (ID: $UPLOAD_ID)"
        ((UPLOADED++))

        # Wait a moment for processing to start
        sleep 1
    else
        echo "✗ Failed"
        echo "  Response: $RESPONSE"
        ((FAILED++))
    fi
done

echo ""
echo "=========================================="
echo "Upload summary:"
echo "  Uploaded: $UPLOADED"
echo "  Failed: $FAILED"
echo "=========================================="
echo ""

if [ $FAILED -eq 0 ]; then
    echo "✓ All bundles uploaded successfully!"
    echo ""
    echo "Bundles are now queued for processing. Check the Validation page"
    echo "to monitor the upload status and confirm measures are loaded."
else
    echo "⚠ Some bundles failed to upload. Check the responses above."
    exit 1
fi
