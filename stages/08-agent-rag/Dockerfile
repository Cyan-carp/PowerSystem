FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# CPU wheels avoid pulling CUDA libraries for the local embedding model.
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt
COPY rag_agent rag_agent
COPY deploy deploy
COPY scripts scripts
COPY knowledge knowledge
RUN groupadd --gid 10002 rag && useradd --uid 10002 --gid 10002 --create-home rag
ENV OMP_NUM_THREADS=2 HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
USER rag
EXPOSE 8093
CMD ["uvicorn", "rag_agent.main:app", "--host", "0.0.0.0", "--port", "8093"]
