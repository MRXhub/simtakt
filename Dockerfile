FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/simtakt:/workspace/extensions

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 simtakt \
    && useradd --uid 10001 --gid simtakt --create-home simtakt \
    && mkdir -p /workspace \
    && chown simtakt:simtakt /workspace

COPY --chown=10001:10001 control_plane /opt/simtakt/control_plane
COPY --chown=10001:10001 examples /opt/simtakt/examples

WORKDIR /workspace
USER 10001:10001
EXPOSE 8321

CMD ["python", "-m", "control_plane.web.status_server", "--demo", "--host", "0.0.0.0"]
