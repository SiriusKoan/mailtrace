#!/bin/sh
set -e

rsyslogd
/usr/sbin/sshd

exim4 -bd &
vector -c /etc/vector/vector.yaml &

tail -F /var/log/mail.log
