FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /

RUN apt update && \
    apt install -y \
        vim curl jq iputils-ping iproute2 openssh-server \
        rsyslog \
    && apt-get clean autoclean \
    && apt-get autoremove --yes \
    && rm -rf /var/lib/apt/lists/*

RUN sed -i '/imklog/s/^/#/' /etc/rsyslog.conf && \
    ssh-keygen -A && \
    mkdir -p /var/run/sshd && \
    chmod 755 /var/run/sshd && \
    echo 'root:root' | chpasswd && \
    echo "PermitRootLogin yes" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config

# Create directories for service-specific logs
RUN mkdir -p /var/log/mx && \
    mkdir -p /var/log/mailer && \
    mkdir -p /var/log/mailpolicy && \
    mkdir -p /var/log/mailbox

# Configure rsyslog to receive logs from remote hosts
COPY ./loghost/rsyslog.conf /etc/rsyslog.d/50-remote.conf

COPY ./loghost/entrypoint.sh ./entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 514/udp 514/tcp 22

ENTRYPOINT ["/entrypoint.sh"]
