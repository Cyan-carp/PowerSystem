FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY review_contract.py .
COPY security_agent security_agent
COPY policy policy
RUN groupadd --gid 10004 security && useradd --uid 10004 --gid 10004 --create-home security
USER security
EXPOSE 8100
CMD ["uvicorn", "security_agent.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", "--no-access-log"]
