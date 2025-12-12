#!/bin/sh
set -e

mkdir -p /var/spool/postfix/dev
ln -sf /dev/log /var/spool/postfix/dev/log
cp /etc/resolv.conf /var/spool/postfix/etc/resolv.conf

rsyslogd
/usr/sbin/sshd

postfix start
dovecot

tail -F /var/log/mail.log
