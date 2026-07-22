FROM minio/mc:RELEASE.2025-07-21T05-28-08Z@sha256:fb8f773eac8ef9d6da0486d5dec2f42f219358bcb8de579d1623d518c9ebd4cc AS minio_client
FROM rclone/rclone:1.70.3@sha256:34c729127386abec1c610b2aa024e39b4498dc2b4a72a0798ae21fbdc1b0493b AS destination_client
FROM postgres:16.9-alpine@sha256:7c688148e5e156d0e86df7ba8ae5a05a2386aaec1e2ad8e6d11bdf10504b1fb7

RUN apk add --no-cache python3=3.12.13-r0 openssl=3.5.7-r0
COPY --from=minio_client /usr/bin/mc /usr/local/bin/mc
COPY --from=destination_client /usr/local/bin/rclone /usr/local/bin/rclone
COPY *.py *.sh /opt/ux09-backup/
RUN chmod 0555 /opt/ux09-backup/*.py /opt/ux09-backup/*.sh \
    && addgroup -S -g 10001 backup \
    && adduser -S -D -H -u 10001 -G backup backup

USER 10001:10001
WORKDIR /work
ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "/opt/ux09-backup/backupctl.py"]
CMD ["--help"]
