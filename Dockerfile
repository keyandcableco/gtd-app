FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 90 s, not gunicorn's 30: a capture waiting on Haiku over a sick network
# must live long enough to fall back to the "tag with" rules (tagger.py).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "90", "app:app"]
