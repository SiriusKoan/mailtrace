#!/bin/bash

# Traffic generation script for mailtrace testing
# Intermittently calls send_bulk_emails.sh to generate SMTP traffic
# Usage: ./gen_traffic.sh [--sleep MIN MAX] [--emails MIN MAX] [-t DURATION]
# Example: ./gen_traffic.sh --sleep 0.1 0.5 --emails 1 5 -t 10

# Default values
SLEEP_MIN=0.1
SLEEP_MAX=0.5
EMAILS_MIN=1
EMAILS_MAX=5
DURATION=10

# Function to display usage
usage() {
    cat << EOF
Usage: $0 [OPTIONS]

OPTIONS:
    --sleep MIN MAX      Sleep time range in seconds (default: 0.1 0.5)
    --emails MIN MAX     Number of emails to send per batch (default: 1 5)
    -t DURATION          Total duration to run in seconds (default: 10)
    -h, --help           Display this help message

EXAMPLE:
    ./gen_traffic.sh --sleep 0.1 0.5 --emails 1 5 -t 10

This will:
    - Sleep randomly between 0.1 and 0.5 seconds
    - Send a random number of emails between 1 and 5
    - Repeat for 10 seconds total
EOF
    exit 1
}

# Parse arguments manually to handle multi-argument options
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sleep)
            if [[ -z "$2" ]] || [[ -z "$3" ]]; then
                echo "Error: --sleep requires two arguments (MIN MAX)"
                usage
            fi
            SLEEP_MIN="$2"
            SLEEP_MAX="$3"
            shift 3
            ;;
        --emails)
            if [[ -z "$2" ]] || [[ -z "$3" ]]; then
                echo "Error: --emails requires two arguments (MIN MAX)"
                usage
            fi
            EMAILS_MIN="$2"
            EMAILS_MAX="$3"
            shift 3
            ;;
        -t)
            if [[ -z "$2" ]]; then
                echo "Error: -t requires an argument (DURATION)"
                usage
            fi
            DURATION="$2"
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

# Validate parameters
if (( $(echo "$SLEEP_MIN > $SLEEP_MAX" | bc -l 2>/dev/null) )); then
    echo "Error: SLEEP_MIN must be less than or equal to SLEEP_MAX"
    exit 1
fi

if [ "$EMAILS_MIN" -gt "$EMAILS_MAX" ]; then
    echo "Error: EMAILS_MIN must be less than or equal to EMAILS_MAX"
    exit 1
fi

if [ "$DURATION" -le 0 ] 2>/dev/null; then
    echo "Error: Duration must be greater than 0"
    exit 1
fi

# Validate numeric values
for val in "$SLEEP_MIN" "$SLEEP_MAX" "$EMAILS_MIN" "$EMAILS_MAX" "$DURATION"; do
    if ! [[ "$val" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "Error: All parameters must be positive numbers"
        exit 1
    fi
done

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

echo "Starting traffic generation..."
echo "  Duration: $DURATION seconds"
echo "  Sleep range: $SLEEP_MIN - $SLEEP_MAX seconds"
echo "  Emails per batch: $EMAILS_MIN - $EMAILS_MAX"
echo ""

START_TIME=$(date +%s.%N)
BATCH_COUNT=0

while true; do
    CURRENT_TIME=$(date +%s.%N)
    ELAPSED=$(echo "$CURRENT_TIME - $START_TIME" | bc)

    # Check if we've exceeded the duration
    if (( $(echo "$ELAPSED >= $DURATION" | bc -l) )); then
        echo ""
        echo "================================"
        echo "Traffic generation complete!"
        echo "  Total batches sent: $BATCH_COUNT"
        echo "  Total elapsed time: $(printf "%.2f" $ELAPSED) seconds"
        echo "================================"
        break
    fi

    # Generate random sleep time
    RANDOM_SLEEP=$(awk -v min="$SLEEP_MIN" -v max="$SLEEP_MAX" 'BEGIN {
        srand();
        sleep_time = min + rand() * (max - min);
        printf "%.2f", sleep_time
    }')

    # Generate random email count
    EMAILS_RANGE=$((EMAILS_MAX - EMAILS_MIN + 1))
    RANDOM_EMAILS=$((EMAILS_MIN + RANDOM % EMAILS_RANGE))

    # Log the action
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TIMESTAMP] Sleeping for $RANDOM_SLEEP seconds..."
    sleep "$RANDOM_SLEEP"

    # Check again if we should exit
    CURRENT_TIME=$(date +%s.%N)
    ELAPSED=$(echo "$CURRENT_TIME - $START_TIME" | bc)
    if (( $(echo "$ELAPSED >= $DURATION" | bc -l) )); then
        echo ""
        echo "================================"
        echo "Traffic generation complete!"
        echo "  Total batches sent: $BATCH_COUNT"
        echo "  Total elapsed time: $(printf "%.2f" $ELAPSED) seconds"
        echo "================================"
        break
    fi

    # Send emails
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TIMESTAMP] Sending $RANDOM_EMAILS emails..."
    "$SCRIPT_DIR/send_bulk_emails.sh" "$RANDOM_EMAILS"
    ((BATCH_COUNT++))

    echo ""
done

exit 0
