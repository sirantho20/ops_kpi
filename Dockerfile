FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPERATIONS_KPI_HOST=0.0.0.0

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY ["operations_kpi_dashboard.py", "operations_kpi_data.py", "operations_kpi_insights.py", "operations_kpi_logging.py", "Operations KPI.html", "/app/"]

EXPOSE 8054

# Liveness: /healthz (no DB). Readiness / traffic gate: /readyz (DB + warm cache when pre-warm on).
# Reverse-proxy: use deploy/nginx-proxy-timeouts.conf — default 60s proxy_read_timeout causes 504 on /.
# Auth: set OPERATIONS_KPI_BASIC_AUTH_USERNAME and OPERATIONS_KPI_BASIC_AUTH_PASSWORD (e.g. in
# .env passed via --env-file) to require HTTP Basic Auth on the dashboard and its /api/* routes.
# /healthz and /readyz stay open so health checks and load balancers don't need credentials.
# Without both vars set, the dashboard is served with no login (a warning is logged at startup).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, sys, urllib.request; port = os.environ.get('OPERATIONS_KPI_PORT') or os.environ.get('PORT', '8054'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3); sys.exit(0)"

USER app

# Force bind to all interfaces inside the container so published ports work from
# every host address (LAN, VPN, etc.). Overrides OPERATIONS_KPI_HOST from .env.
#
# Production run (survive crashes and host reboot; omit restart if you want manual-only).
# Publish on all host addresses (LAN, VPN, etc.): IPv4 0.0.0.0 and IPv6 [::].
#   docker run -d --name operations-kpi-dashboard --restart unless-stopped \
#     -p 0.0.0.0:18054:8054 -p [::]:18054:8054 --env-file .env operations-kpi-dashboard:prod
CMD ["python", "operations_kpi_dashboard.py", "--host", "0.0.0.0"]
