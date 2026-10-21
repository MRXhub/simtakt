FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/simtakt:/workspace/extensions \
    PUID=10001 \
    PGID=10001

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 simtakt \
    && useradd --uid 10001 --gid simtakt --create-home simtakt \
    && mkdir -p /workspace \
    && chown simtakt:simtakt /workspace

COPY --chown=10001:10001 control_plane /opt/simtakt/control_plane
COPY --chown=10001:10001 examples /opt/simtakt/examples
COPY docker/entrypoint.sh /usr/local/bin/simtakt-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/simtakt-entrypoint \
    && chmod 0755 /usr/local/bin/simtakt-entrypoint

WORKDIR /workspace
# Starts as root only to map the simtakt account to PUID/PGID; the entrypoint
# drops privileges before running the command.
ENTRYPOINT ["/usr/local/bin/simtakt-entrypoint"]
EXPOSE 8321

CMD ["python", "-m", "control_plane.web.status_server", "--demo", "--host", "0.0.0.0"]
