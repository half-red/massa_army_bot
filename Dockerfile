FROM ubuntu:jammy

RUN : \
  && apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    software-properties-common \
  && add-apt-repository ppa:deadsnakes/ppa \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

RUN : \
  && apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-dev \
    python3.12-venv \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src

ENV PATH=/venv/bin/:$PATH
COPY requirements.txt .
RUN : \
  && python3.12 -m venv /venv \
  && pip --no-cache-dir --disable-pip-version-check install -r requirements.txt
