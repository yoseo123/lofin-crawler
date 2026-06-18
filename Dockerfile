FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY src/ src/
COPY templates/ templates/
COPY static/ static/
COPY crawl_lofin.py webapp.py build_policy_rag_index.py ./

EXPOSE 8787

ENV LOFIN_WEB_PORT=8787

CMD ["python", "webapp.py"]
