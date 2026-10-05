FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent ./agent
COPY knowledge ./knowledge
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 --create-home agent
USER agent
EXPOSE 8092
CMD ["python", "-m", "uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8092", "--no-access-log"]
