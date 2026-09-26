#!/bin/bash

# Randomly send test messages across the six successful delivery routes.

if [ "$#" -ne 1 ] || ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
    echo "Usage: $0 <number_of_emails>" >&2
    exit 1
fi

if ! command -v swaks >/dev/null 2>&1; then
    echo "Error: swaks is not installed" >&2
    exit 1
fi

NUM_EMAILS=$1
PORTS=(10025 10025 10025 20025 20026 20027)
RECIPIENTS=(
    single@1.example.com
    team@2.example.com
    single@3.example.com
    team@1.example.com
    single@2.example.com
    team@3.example.com
)
NAMES=(
    mx-1-single
    mx-2-team
    mx-3-single
    mailer-1-team
    mailer-2-single
    mailer-3-team
)

SUCCESS_COUNT=0
FAIL_COUNT=0

for ((i=1; i<=NUM_EMAILS; i++)); do
    scenario=$((RANDOM % 6))
    port=${PORTS[$scenario]}
    recipient=${RECIPIENTS[$scenario]}
    name=${NAMES[$scenario]}
    printf '[%d/%d] %s -> %s ... ' "$i" "$NUM_EMAILS" "$name" "$recipient"
    if swaks \
        --to "$recipient" \
        --from "sender-${name}@sender.test" \
        --helo sender.test \
        --server 127.0.0.1 \
        --port "$port" \
        >/dev/null 2>&1; then
        echo "OK"
        ((SUCCESS_COUNT++))
    else
        echo "FAILED"
        ((FAIL_COUNT++))
    fi
    sleep 0.1
done

echo "successful=$SUCCESS_COUNT failed=$FAIL_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
