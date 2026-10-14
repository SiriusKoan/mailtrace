#!/bin/sh
set -e

relay_delay=${MAILTRACE_RELAY_DELAY_SECONDS:-0}
case "$relay_delay" in
    *[!0-9]*)
        echo "MAILTRACE_RELAY_DELAY_SECONDS must be a non-negative integer" >&2
        exit 1
        ;;
esac

: > /etc/exim4/relay-delay.conf
case "$relay_delay" in
    *[1-9]*)
        printf "transport_filter = /bin/sh -c 'sleep %s; exec cat'\n" \
            "$relay_delay" > /etc/exim4/relay-delay.conf
        ;;
esac

rsyslogd
/usr/sbin/sshd

exim4 -bd &
vector -c /etc/vector/vector.yaml &

tail -F /var/log/mail.log
