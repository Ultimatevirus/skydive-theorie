FROM alpine:3.22

RUN apk add --no-cache logrotate

COPY logrotate.conf /etc/logrotate.conf

CMD ["sh", "-c", "while true; do logrotate /etc/logrotate.conf; sleep 86400; done"]