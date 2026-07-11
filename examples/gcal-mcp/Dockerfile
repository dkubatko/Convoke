FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
# GOOGLE_SERVICE_ACCOUNT_FILE is supplied at run time. Mount the service-account
# key read-only, e.g.
#   -v ./sa.json:/creds/sa.json:ro -e GOOGLE_SERVICE_ACCOUNT_FILE=/creds/sa.json
CMD ["python", "server.py"]
