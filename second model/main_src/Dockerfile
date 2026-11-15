# ==============================================================================
# Dockerfile - Zalo AI Challenge 2026 (Task: DataMate)
# Architecture: Schema-Aware Code-Act Agent (Qwen/Qwen2.5-Coder-14B-Instruct)
# Target Hardware: 2 x NVIDIA RTX 5090 (Blackwell, CUDA >= 12.8) / Colab L4
# ==============================================================================
FROM nvidia/cuda:12.8.0-cudnn-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    CUBLAS_WORKSPACE_CONFIG=:4096:8 \
    MODEL_DIR=/models/Qwen2.5-Coder-14B-Instruct

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Cài đặt thư viện Python cố định phiên bản
COPY requirements.txt /workspace/requirements.txt
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install --no-cache-dir -r /workspace/requirements.txt

# Sao chép toàn bộ mã nguồn vào /workspace/main_src
COPY . /workspace/main_src

# Sao chép script thực thi predict.sh ra /workspace/predict.sh và /predict.sh
RUN cp /workspace/main_src/predict.sh /workspace/predict.sh && \
    cp /workspace/main_src/predict.sh /predict.sh && \
    chmod +x /workspace/predict.sh /predict.sh /workspace/main_src/predict.sh

# Tạo sẵn thư mục kết quả và thư mục tạm (vì /data được mount ở chế độ read-only)
RUN mkdir -p /result/logs /tmp/datamate /models

CMD ["/bin/bash", "/workspace/predict.sh"]
