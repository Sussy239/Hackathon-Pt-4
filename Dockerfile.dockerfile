FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Clone sqlglot at the exact pinned commit required by the rules
RUN git clone https://github.com/tobymao/sqlglot.git /opt/sqlglot \
    && cd /opt/sqlglot \
    && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && rm -rf /opt/sqlglot/.git

COPY build_index.py select_tests.py /app/

# Build static import & symbol index once at image build time
RUN python3 /app/build_index.py --repo /opt/sqlglot --out /app/index.json

ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/select_tests.py", "--index", "/app/index.json"]