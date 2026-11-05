FROM python:3.12-alpine

WORKDIR /app
COPY app.py index.html ./

ENV PORT=8080 \
    POOF_NOTES_DATA=/data
VOLUME /data
EXPOSE 8080

CMD ["python", "-u", "app.py"]
