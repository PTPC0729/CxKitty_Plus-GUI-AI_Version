FROM python:3.12-slim
WORKDIR /app
COPY requirements-cli.txt /app/
RUN pip install --no-cache-dir -r requirements-cli.txt
COPY . /app
ENV CXKITTY_DATA_DIR=/data
ENTRYPOINT ["python", "main.py", "--cli"]
