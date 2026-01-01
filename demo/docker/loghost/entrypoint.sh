#!/bin/bash

# Start SSH service
/usr/sbin/sshd

# Start rsyslog service
/usr/sbin/rsyslogd

# Keep container running
tail -f /dev/null
