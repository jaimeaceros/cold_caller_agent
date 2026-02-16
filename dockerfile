FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Later this becomes a FastAPI server.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
