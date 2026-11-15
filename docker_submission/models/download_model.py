#!/usr/bin/env python3
"""
Tải trước toàn bộ trọng số mô hình Qwen/Qwen2.5-Coder-14B-Instruct
để đóng gói vào Docker Image phục vụ suy luận Offline 100% (Rule 3).
"""
import os
import sys

MODEL_ID = "Qwen/Qwen2.5-Coder-14B-Instruct"
TARGET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Qwen2.5-Coder-14B-Instruct")

def main():
    print(f"============================================================")
    print(f"[DataMate] Bắt đầu tải mô hình {MODEL_ID}")
    print(f"  Thư mục lưu trữ: {TARGET_DIR}")
    print(f"============================================================")
    
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("Lỗi: Cần cài đặt huggingface_hub: pip install huggingface_hub")
        sys.exit(1)

    os.makedirs(TARGET_DIR, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        local_dir=TARGET_DIR,
        local_dir_use_symlinks=False,
        resume_download=True,
    )
    print(f"[DataMate] Tải mô hình thành công vào {TARGET_DIR}!")

if __name__ == "__main__":
    main()
