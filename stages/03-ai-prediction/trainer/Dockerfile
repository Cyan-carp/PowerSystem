# 训练镜像：与推理镜像共用依赖，入口换成 trainer.app。
# 构建上下文必须是 stages/03-ai-prediction（需要 prediction 包与 requirements.txt）：
#   docker build -f trainer/Dockerfile -t ps-v6-ai-trainer:latest .
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN grep -v '^xgboost==' requirements.txt > /tmp/requirements-cpu.txt \
    && pip install --no-cache-dir -r /tmp/requirements-cpu.txt \
    && pip install --no-cache-dir --no-deps xgboost==3.4.1
COPY prediction ./prediction
COPY trainer ./trainer
ENV STAGE3_MODEL_DIR=/model
ENV TRAINER_WORK_DIR=/work
EXPOSE 8091
CMD ["uvicorn", "trainer.app:app", "--host", "0.0.0.0", "--port", "8091"]
