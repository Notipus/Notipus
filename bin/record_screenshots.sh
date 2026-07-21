#!/bin/bash
set -euo pipefail

SCREENSHOTS_DIR="screenshots"
OUTPUT_DIR="$SCREENSHOTS_DIR/output"

build_frontend() {
    echo "Building frontend assets..."
    bun install --frozen-lockfile
    bun run build
}

ensure_browser() {
    echo "Ensuring Playwright Chromium is installed..."
    uv run playwright install chromium
}

run_screenshot() {
    local file="$1"
    local basename
    basename=$(basename "$file" .py)
    echo "--- Capturing: $file ---"
    # addopts is cleared to drop --disable-socket: the live server and
    # the browser both need real sockets.
    uv run pytest "$file" \
        --override-ini="python_files=*.py" \
        --override-ini="python_functions=$basename" \
        --override-ini="addopts=" \
        -s
}

INPUT="${1:-all}"

# Validate input: must be "all" or a safe Python filename (not conftest.py)
if [[ "$INPUT" != "all" ]]; then
    if [[ ! "$INPUT" =~ ^[a-zA-Z0-9_-]+\.py$ ]]; then
        echo "Error: Invalid screenshot name '$INPUT'. Must be 'all' or a filename like 'dashboard.py'"
        exit 1
    fi
    if [[ "$INPUT" == "conftest.py" ]]; then
        echo "Error: 'conftest.py' is not a valid screenshot target"
        exit 1
    fi
fi

build_frontend
ensure_browser
mkdir -p "$OUTPUT_DIR"

if [ "$INPUT" = "all" ]; then
    echo "Capturing all screenshots..."
    for file in "$SCREENSHOTS_DIR"/*.py; do
        [ -f "$file" ] || continue
        [[ "$(basename "$file")" == "conftest.py" ]] && continue
        run_screenshot "$file"
    done
else
    FILE="$SCREENSHOTS_DIR/$INPUT"
    if [ ! -f "$FILE" ]; then
        echo "Error: $FILE not found"
        exit 1
    fi
    run_screenshot "$FILE"
fi

echo "Done. Screenshots are in $OUTPUT_DIR/"
