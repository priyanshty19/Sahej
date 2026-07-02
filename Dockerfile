# Sahej — zero-dependency Python app. ~50MB image.
#   docker build -t sahej . && docker run -p 8000:8000 sahej
FROM python:3.12-slim
WORKDIR /app
COPY engine.py serve.py ./
COPY data/ data/
COPY web/ web/
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"
USER nobody
CMD ["python3", "serve.py"]
