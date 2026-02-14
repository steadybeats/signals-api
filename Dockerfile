FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Run server
CMD ["uvicorn", "signals_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
