FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPERATIONS_KPI_HOST=0.0.0.0 \
    OPERATIONS_KPI_PORT=8054

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

COPY ["operations_kpi_dashboard.py", "operations_kpi_data.py", "operations_kpi_insights.py", "Operations KPI.html", "/app/"]

EXPOSE 8054

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys, urllib.request; urllib.request.urlopen('http://127.0.0.1:8054/healthz', timeout=3); sys.exit(0)"

USER app

CMD ["python", "operations_kpi_dashboard.py"]
