#!/bin/bash

# Mass email sending script for mailtrace testing
# Sends emails using two random SMTP servers
# Usage: ./send_bulk_emails.sh <number_of_emails>

if [ $# -eq 0 ]; then
    echo "Usage: $0 <number_of_emails>"
    echo "Example: $0 100"
    exit 1
fi

NUM_EMAILS=$1

# Validate input is a number
if ! [[ "$NUM_EMAILS" =~ ^[0-9]+$ ]]; then
    echo "Error: Argument must be a positive integer"
    exit 1
fi

if [ "$NUM_EMAILS" -le 0 ]; then
    echo "Error: Number of emails must be greater than 0"
    exit 1
fi

# Check if swaks is installed
if ! command -v swaks &> /dev/null; then
    echo "Error: swaks is not installed"
    echo "Install it with: apt-get install swaks"
    exit 1
fi

echo "Sending $NUM_EMAILS emails..."
echo "Using random mix of:"
echo "  - Method 1: user2@example.com via mx (port 10025)"
echo "  - Method 2: user1@example.com via mailpolicy (port 20025)"
echo ""

SENT_COUNT=0
SUCCESS_COUNT=0
FAIL_COUNT=0

for ((i=1; i<=NUM_EMAILS; i++)); do
    # Randomly choose between two methods (0 or 1)
    METHOD=$((RANDOM % 2))

    if [ $METHOD -eq 0 ]; then
        # Method 1: Send to user2@example.com via mx
        printf "[$i/$NUM_EMAILS] Sending via mx:10025 (user1->user2)... "
        if swaks \
            --to user2@example.com \
            --from user1@example.com \
            --server 127.0.0.1 \
            --port 10025 \
            > /dev/null 2>&1; then
            echo "✓"
            ((SUCCESS_COUNT++))
        else
            echo "✗ Failed to send"
            ((FAIL_COUNT++))
        fi
    else
        # Method 2: Send to user1@example.com via mailpolicy
        printf "[$i/$NUM_EMAILS] Sending via mailpolicy:20025 (me->user1)... "
        if swaks \
            --to user1@example.com \
            --from me@siriuskoan.one \
            --helo siriuskoan.one \
            --server 127.0.0.1 \
            --port 20025 \
            > /dev/null 2>&1; then
            echo "✓"
            ((SUCCESS_COUNT++))
        else
            echo "✗ Failed to send"
            ((FAIL_COUNT++))
        fi
    fi

    ((SENT_COUNT++))

    # Small delay to avoid overwhelming the server
    sleep 0.1
done

echo ""
echo "================================"
echo "Email sending complete!"
echo "  Total sent: $SENT_COUNT"
echo "  Successful: $SUCCESS_COUNT"
echo "  Failed: $FAIL_COUNT"
echo "================================"

if [ $FAIL_COUNT -gt 0 ]; then
    exit 1
fi

exit 0
