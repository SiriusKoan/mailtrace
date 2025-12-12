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

RUN sed -i '/imklog/s/^/#/' /etc/rsyslog.conf && \
    ssh-keygen -A && \
    mkdir -p /var/run/sshd && \
    chmod 755 /var/run/sshd && \
    echo 'root:root' | chpasswd && \
    echo "PermitRootLogin yes" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config

RUN useradd -m user1 && echo "user1:user1" | chpasswd && \
    useradd -m user2 && echo "user2:user2" | chpasswd

COPY ./postfix /etc/postfix

COPY ./entrypoint.sh ./entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 25 22

ENTRYPOINT ["/entrypoint.sh"]
