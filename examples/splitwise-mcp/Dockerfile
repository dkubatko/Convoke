FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
# SPLITWISE_API_KEY is supplied at run time.
CMD ["python", "server.py"]
