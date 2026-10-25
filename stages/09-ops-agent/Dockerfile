# Build from the repository root. Includes the shared wire/write/review contracts.
FROM python:3.12-slim
WORKDIR /app
COPY stages/09-ops-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stages/09-ops-agent/ops_agent ops_agent
COPY stages/09-ops-agent/ops_contract.py stages/09-ops-agent/ops_write_contract.py ./
COPY stages/09-ops-agent/v4_joint.py ./
COPY stages/10-security-agent/review_contract.py ./
COPY stages/08-agent-rag/rag_agent/business_quality.py ./
COPY stages/09-ops-agent/deploy deploy
RUN groupadd --gid 10003 ops && useradd --uid 10003 --gid 10003 --create-home ops
USER ops
EXPOSE 8094
CMD ["uvicorn", "ops_agent.main:app", "--host", "0.0.0.0", "--port", "8094", "--workers", "1"]
