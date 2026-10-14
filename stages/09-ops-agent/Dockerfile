FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ops_agent ops_agent
COPY deploy deploy
RUN groupadd --gid 10003 ops && useradd --uid 10003 --gid 10003 --create-home ops
USER ops
EXPOSE 8094
CMD ["uvicorn", "ops_agent.main:app", "--host", "0.0.0.0", "--port", "8094", "--workers", "1"]
