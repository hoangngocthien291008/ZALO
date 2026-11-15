# ==============================================================================
# Dockerfile - Zalo AI Challenge 2026 (Task: DataMate)
# Official Base Image: vllm/vllm-openai:v0.10.1.1 (CUDA >= 12.8, Blackwell Support)
# Target Hardware: 2 x NVIDIA RTX 5090 (64GB VRAM) / NVIDIA L4 (24GB VRAM)
# ==============================================================================
FROM vllm/vllm-openai:v0.10.1.1

ENTRYPOINT []

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PIP_NO_CACHE_DIR=1 \
    NCCL_P2P_DISABLE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    TOKENIZERS_PARALLELISM=false \
    CUBLAS_WORKSPACE_CONFIG=:4096:8

WORKDIR /code

# Cài đặt các thư viện phụ thuộc
COPY requirements.txt /code/requirements.txt
RUN pip install --no-cache-dir -r /code/requirements.txt && \
    pip install --no-cache-dir --no-deps nvidia-nccl-cu12==2.27.7

# Sao chép các thành phần theo cấu trúc quy định của Ban Tổ Chức (Section 4)
COPY models/ /code/models/
COPY training_code/ /code/training_code/
COPY predict.sh README.md /code/
COPY src/ /code/src/

# Cấp quyền thực thi cho entrypoint
RUN chmod +x /code/predict.sh && \
    mkdir -p /result/logs /tmp/datamate /code/models

CMD ["bash", "/code/predict.sh"]
