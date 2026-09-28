FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN grep -v '^xgboost==' requirements.txt > /tmp/requirements-cpu.txt \
    && pip install --no-cache-dir -r /tmp/requirements-cpu.txt \
    && pip install --no-cache-dir --no-deps xgboost==3.4.1
COPY prediction ./prediction
ENV STAGE3_MODEL_DIR=/model
EXPOSE 8090
CMD ["uvicorn", "prediction.app:app", "--host", "0.0.0.0", "--port", "8090"]
