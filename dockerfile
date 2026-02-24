FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"
COPY . .

# Later this becomes a FastAPI server.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
