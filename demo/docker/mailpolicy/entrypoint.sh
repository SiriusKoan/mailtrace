#!/bin/sh
set -e

mkdir -p /var/spool/postfix/dev
ln -sf /dev/log /var/spool/postfix/dev/log
cp /etc/resolv.conf /var/spool/postfix/etc/resolv.conf

rsyslogd
/usr/sbin/sshd

postconf -e "myhostname = $(hostname)"
policy_number="$(hostname | sed -n 's/^mailpolicy\([123]\)\..*/\1/p')"
virtual_map="/etc/postfix/virtual.${policy_number}"
postconf -e "virtual_alias_maps = hash:${virtual_map}"
postmap "${virtual_map}"
postfix start
vector -c /etc/vector/vector.yaml &

tail -F /var/log/mail.log
