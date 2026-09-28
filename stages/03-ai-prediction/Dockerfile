FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY prediction ./prediction
ENV STAGE3_MODEL_DIR=/model
EXPOSE 8090
CMD ["uvicorn", "prediction.app:app", "--host", "0.0.0.0", "--port", "8090"]
