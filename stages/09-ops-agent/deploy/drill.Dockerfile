FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY drill/main.py /app/main.py
USER 65532:65532
HEALTHCHECK --interval=1s --timeout=1s --start-period=1s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=.5).read()"]
CMD ["python", "/app/main.py"]
