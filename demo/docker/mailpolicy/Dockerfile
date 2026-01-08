FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /

RUN apt update && \
    apt install -y \
        vim curl jq python3-pip iputils-ping iproute2 openssh-server \
        postfix postfix-pcre rsyslog \
    && apt-get clean autoclean \
    && apt-get autoremove --yes \
    && rm -rf /var/lib/apt/lists/*

RUN bash -c "$(curl -L https://setup.vector.dev)" && apt-get install vector

RUN sed -i '/imklog/s/^/#/' /etc/rsyslog.conf && \
    ssh-keygen -A && \
    mkdir -p /var/run/sshd && \
    chmod 755 /var/run/sshd && \
    echo 'root:root' | chpasswd && \
    echo "PermitRootLogin yes" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config

COPY ./mailpolicy/postfix /etc/postfix
COPY ./vector/vector.yaml /etc/vector/vector.yaml
COPY ./rsyslog/forward.conf /etc/rsyslog.d/50-forward.conf

COPY ./mailpolicy/entrypoint.sh ./entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 25 22

ENTRYPOINT ["/entrypoint.sh"]
