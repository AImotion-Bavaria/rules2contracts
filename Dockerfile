# rules2contracts — reproduction image.
# Build:  docker build -t rules2contracts .
# Run:    docker run --rm rules2contracts            # = make replay (all offline checks)
#         docker run --rm -it rules2contracts bash   # interactive shell
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends make && rm -rf /var/lib/apt/lists/*
WORKDIR /artifact
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["make", "replay"]
