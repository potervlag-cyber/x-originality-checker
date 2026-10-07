FROM python:3.12.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8787

WORKDIR /app
COPY --chown=10001:10001 webcheck.py webcheck_server.py content_review.py ./
COPY --chown=10001:10001 deployment/container_entrypoint.py deployment/container_healthcheck.py ./deployment/

USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "deployment/container_healthcheck.py"]
CMD ["python", "deployment/container_entrypoint.py"]
