FROM python:3.11-alpine

RUN apk add --no-cache docker-cli && \
    pip install --no-cache-dir fastapi==0.110.0 'uvicorn[standard]'==0.27.0 \
                                python-multipart==0.0.9

# Synology Docker daemon caps API at 1.41 — pin client to match.
ENV DOCKER_API_VERSION=1.41

WORKDIR /app
COPY skill_server.py onboarding.py /app/

EXPOSE 8000
CMD ["uvicorn", "skill_server:app", "--host", "0.0.0.0", "--port", "8000"]
