#!/bin/bash

# Static traffic generation script for mailtrace testing
# Sleeps for a fixed interval and sends a fixed number of emails, repeatedly
# for a fixed number of iterations.
# Usage: ./static_gen_traffic.sh --sleep SECONDS --emails COUNT --iterations COUNT
# Example: ./static_gen_traffic.sh --sleep 1 --emails 5 --iterations 30

SLEEP_TIME=""
NUM_EMAILS=""
ITERATIONS=""

# Function to display usage
usage() {
    cat << EOF
Usage: $0 --sleep SECONDS --emails COUNT --iterations COUNT

OPTIONS:
    --sleep SECONDS      Fixed sleep time between batches in seconds
    --emails COUNT       Fixed number of emails to send per batch
    --iterations COUNT   Number of iterations (sleep + send emails) to run
    -h, --help           Display this help message

EXAMPLE:
    ./static_gen_traffic.sh --sleep 1 --emails 5 --iterations 30

This will:
    - Sleep for exactly 1 second between batches
    - Send exactly 5 emails per batch
    - Repeat for exactly 30 iterations
EOF
    exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sleep)
            if [[ -z "$2" ]]; then
                echo "Error: --sleep requires an argument (SECONDS)"
                usage
            fi
            SLEEP_TIME="$2"
            shift 2
            ;;
        --emails)
            if [[ -z "$2" ]]; then
                echo "Error: --emails requires an argument (COUNT)"
                usage
            fi
            NUM_EMAILS="$2"
            shift 2
            ;;
        --iterations)
            if [[ -z "$2" ]]; then
                echo "Error: --iterations requires an argument (COUNT)"
                usage
            fi
            ITERATIONS="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

# Ensure all required arguments are provided
if [[ -z "$SLEEP_TIME" ]] || [[ -z "$NUM_EMAILS" ]] || [[ -z "$ITERATIONS" ]]; then
    echo "Error: --sleep, --emails, and --iterations are all required"
    usage
fi

# Validate numeric values
for val in "$SLEEP_TIME" "$NUM_EMAILS" "$ITERATIONS"; do
    if ! [[ "$val" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "Error: All parameters must be positive numbers"
        exit 1
    fi
done

if (( $(echo "$SLEEP_TIME <= 0" | bc -l) )); then
    echo "Error: --sleep must be greater than 0"
    exit 1
fi

if [ "$NUM_EMAILS" -le 0 ] 2>/dev/null; then
    echo "Error: --emails must be greater than 0"
    exit 1
fi

if [ "$ITERATIONS" -le 0 ] 2>/dev/null; then
    echo "Error: --iterations must be greater than 0"
    exit 1
fi

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if send_bulk_emails.sh exists
if [ ! -f "$SCRIPT_DIR/send_bulk_emails.sh" ]; then
    echo "Error: send_bulk_emails.sh not found in $SCRIPT_DIR"
    exit 1
fi

# Check if send_bulk_emails.sh is executable
if [ ! -x "$SCRIPT_DIR/send_bulk_emails.sh" ]; then
    echo "Making send_bulk_emails.sh executable..."
    chmod +x "$SCRIPT_DIR/send_bulk_emails.sh"
fi

echo "Starting static traffic generation..."
echo "  Iterations:   $ITERATIONS"
echo "  Sleep:        $SLEEP_TIME seconds"
echo "  Emails/batch: $NUM_EMAILS"
echo "  Mode:         async"
echo ""

START_TIME=$(date +%s.%N)
BATCH_COUNT=0

while [ "$BATCH_COUNT" -lt "$ITERATIONS" ]; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TIMESTAMP] Dispatching batch of $NUM_EMAILS emails (async) and sleeping for $SLEEP_TIME seconds..."

    # Fire the send and the sleep concurrently so the interval is exact
    "$SCRIPT_DIR/send_bulk_emails.sh" "$NUM_EMAILS" &
    sleep "$SLEEP_TIME" &
    SLEEP_PID=$!
    ((BATCH_COUNT++))

    # Wait only on the sleep — this gates the next iteration to exactly SLEEP_TIME
    wait "$SLEEP_PID"

    echo ""
done

# Wait for all background batches to finish before exiting
echo "Waiting for all in-flight batches to complete..."
wait

CURRENT_TIME=$(date +%s.%N)
ELAPSED=$(echo "$CURRENT_TIME - $START_TIME" | bc)

echo ""
echo "================================"
echo "Traffic generation complete!"
echo "  Total batches dispatched: $BATCH_COUNT / $ITERATIONS"
echo "  Total elapsed time: $(printf "%.2f" $ELAPSED) seconds"
echo "================================"

exit 0
