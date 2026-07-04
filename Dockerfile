# Sahej — small Python app. Standard library only, plus the Postgres driver so
# the same image works with SQLite (no env) or Neon/Postgres (DATABASE_URL set).
#   docker build -t sahej . && docker run -p 8000:8000 sahej
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py serve.py store.py ./
COPY data/childbirth_schemes.json data/death_schemes.json data/states.json data/
COPY web/ web/
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"
USER nobody
CMD ["python3", "serve.py"]
