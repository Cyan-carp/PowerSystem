FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app DRILL_CONFIG_FILE=/private/broker.json
COPY ops_contract.py ops_write_contract.py /app/
COPY collector /app/collector
COPY drill/broker.py /app/broker.py
CMD ["python", "/app/broker.py"]
