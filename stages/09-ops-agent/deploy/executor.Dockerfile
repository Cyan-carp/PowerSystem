FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY stages/09-ops-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stages/09-ops-agent/ops_executor ops_executor
COPY stages/09-ops-agent/ops_contract.py stages/09-ops-agent/ops_write_contract.py ./
RUN groupadd --gid 10005 executor && useradd --uid 10005 --gid 10005 --create-home executor
USER 10005:10005
EXPOSE 8095
CMD ["uvicorn","ops_executor.main:app","--host","0.0.0.0","--port","8095","--workers","1","--no-access-log"]
