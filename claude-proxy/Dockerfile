FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi uvicorn && \
    useradd -m -d /home/claude -u 10009 -g 1009 claude 2>/dev/null || \
    groupadd -g 1009 appgroup && useradd -m -d /home/claude -u 10009 -g 1009 claude

WORKDIR /proxy
COPY proxy.py .

USER 10009
EXPOSE 8081
CMD ["uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "8081"]
