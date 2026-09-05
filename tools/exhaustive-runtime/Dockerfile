ARG PYTHON_BASE
FROM ${PYTHON_BASE}
RUN apt-get update && apt-get install -y --no-install-recommends git bash \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pydantic==2.13.4 PyYAML==6.0.3 PyJWT==2.12.1 \
    pytest==9.0.2 ruff==0.15.8 cryptography==46.0.6
COPY worker.py /opt/fl4write-test-worker.py
LABEL org.fl4write.test-runtime="1"
LABEL org.fl4write.model-proxy="1"
WORKDIR /work
