FROM python:3.11-alpine

RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    'uvicorn[standard]'==0.30.6 \
    requests==2.32.3

WORKDIR /app
COPY app.py /app/

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
