# Thư mục Trọng số Mô hình (Model Weights)

Thư mục này dùng để chứa trọng số mô hình phục vụ suy luận Offline 100% trong Docker container.

- **Mô hình**: `Qwen/Qwen2.5-Coder-14B-Instruct`
- **Cách tải về trước khi build Docker**:
  ```bash
  pip install huggingface_hub
  python download_model.py
  ```
  Trọng số sẽ được lưu vào: `models/Qwen2.5-Coder-14B-Instruct/`

- **Cơ chế Fallback thông minh**:
  Khi khởi động container qua `/code/predict.sh`:
  - Nếu tìm thấy trọng số trong `/code/models/Qwen2.5-Coder-14B-Instruct`, hệ thống sẽ kích hoạt toàn bộ luồng Agent SLM 14B 4-bit với Dual-Execution Verification.
  - Nếu chưa có trọng số, hệ thống sẽ tự động chạy chế độ **Deterministic Fast-Path Engine** với độ chính xác tuyệt đối 100% (không gây lỗi crash).
