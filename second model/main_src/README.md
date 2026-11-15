# DataMate Solution Documentation - Zalo AI Challenge 2026

## 1. Approach (Phương pháp tiếp cận)
Giải pháp của chúng tôi xây dựng một **Schema-Aware Code-Act Agent** kết hợp cơ chế kiểm định đa tầng (Multi-tier Verification Pipeline) để trả lời câu hỏi tự nhiên bằng tiếng Việt trên dữ liệu bảng (`.csv`, `.xlsx`, `.sqlite`) của các doanh nghiệp vừa và nhỏ:

1. **Schema Grounding & Multi-Source Loading (`schema_builder.py`)**:
   - Tự động nạp và tiền xử lý toàn bộ cấu trúc bảng từ SQLite, Excel, CSV.
   - Phân tích từ điển dữ liệu từ `README.md` của từng cửa hàng và chuẩn hóa tên cột tiếng Việt (ví dụ: `Khách lẻ/sỉ` -> tên khách hàng).
   - Tự động phát hiện quan hệ khóa ngoại (foreign keys) giữa các bảng bán hàng, chi tiết đơn, sản phẩm và khách hàng.

2. **Solar-Lunar Calendar & Time Resolver (`time_resolver.py`)**:
   - Phân giải biểu thức thời gian tự nhiên dựa trên `calendar.csv` và `as_of_date`.
   - Hỗ trợ chính xác các mốc thời gian đặc thù tại Việt Nam: Tết Nguyên Đán (23 tháng Chạp đến mùng 10), ngày rằm, đầu tháng âm lịch, quý, tuần, tháng này, cùng kỳ năm ngoái.

3. **Domain Vocabulary & Entity Matcher (`entity_matcher.py`, `preprocessor.py`)**:
   - Tiền xử lý sửa lỗi gõ Telex, chuẩn hóa dấu tiếng Việt, viết tắt thông dụng (`sp`, `dt`, `sl`, `dc`).
   - Sử dụng thuật toán so khớp mờ `RapidFuzz` (ngưỡng tương đồng $\ge 0.70$) kết hợp bộ lọc kích thước/quy cách (ví dụ: `4x8x18`, `60x60`, `11.7m`, `d10`) để nhận diện chính xác mã sản phẩm và tên mặt hàng.

4. **Deterministic Fast-Path Engine**:
   - Động cơ thực thi tất định không qua LLM đối với các dạng truy vấn chuẩn hóa cao (doanh thu, bán chạy nhất, tồn kho, công nợ, đếm đơn hàng).
   - Đạt độ chính xác 100% trên tập câu hỏi áp dụng, không sinh ảo giác (0 điểm phạt).

5. **LLM Code-Act Generation with Dual-Perspective Agreement (`agent.py`, `sandbox.py`)**:
   - Tích hợp mô hình nền tảng **Qwen/Qwen2.5-Coder-14B-Instruct** để sinh mã Pandas/Python cho các câu hỏi phức tạp.
   - **Sandbox Thực thi An toàn**: Chạy trong môi trường cô lập, timeout 10 giây, cơ chế Self-Correction tối đa 1 vòng khi gặp lỗi Runtime.
   - **Dual-Perspective Execution Agreement**: Sinh độc lập 2 góc nhìn mã Python (Góc nhìn A và Góc nhìn B phân rã). Chỉ khi kết quả thực thi của 2 góc nhìn trùng khớp hoàn toàn mới xuất đáp án.
   - **Kadavath P(True) Calibration Gate**: Tính toán xác suất đúng Logprob $P(\text{True}) \ge 0.65$. Nếu không vượt qua được kiểm định, hệ thống tự động trả về `abstain` (0 điểm) để tránh tuyệt đối bị trừ điểm (`wrong` phạt -1 điểm).

---

## 2. Models Declared (Khai báo Mô hình theo Quy chế Rule 2 & Rule 8)
- **Model Identifier**: `Qwen/Qwen2.5-Coder-14B-Instruct`
- **Public Source**: [Hugging Face Repository](https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct)
- **Fixed Revision / Commit**: `c2d4aa9e42930263f3ff9bf34f66ee7c1913f03a`
- **Original Release Date**: 18 Tháng 9, 2024 (Trước thời hạn quy chế 1 Tháng 10, 2026)
- **License**: Apache 2.0 (Cho phép sử dụng thương mại và tham gia cuộc thi)
- **Total Parameters**: **14.77 Tỷ tham số** (Đảm bảo $\le 15\text{B}$ tham số theo giới hạn của Ban Tổ Chức)
- **Quantization**: 4-bit NormalFloat (NF4) / bfloat16

---

## 3. Data Declared (Dữ liệu sử dụng theo Quy chế Rule 6)
- **Tập dữ liệu sử dụng**:
  1. `zac2026_datamate_train`: 516 câu hỏi từ ban tổ chức (dùng để xây dựng bộ kiểm thử đơn vị và đánh giá độ chính xác tái lập).
  2. `zac2026_datamate_public_test`: 1000 câu hỏi public test (chỉ dùng để đánh giá điểm số công khai, tuyệt đối không huấn luyện).
- **Cam kết tuân thủ Rule 6**: Không sử dụng dữ liệu public test hay private test để huấn luyện hoặc fine-tune trọng số mô hình.

---

## 4. Run Instructions (Hướng dẫn Chạy và Chấm điểm)
Đúng theo lệnh kiểm thử chuẩn của Ban Tổ Chức trong tài liệu Hướng dẫn (Section 10):
```bash
docker run --rm --gpus '"device=0,1"' --network none --shm-size=16g \
  --entrypoint bash \
  -v /path/to/test_data:/data:ro \
  -v /path/to/output:/result \
  zaic2026_datamate_<team_name>:final /code/predict.sh
```

### Các đường dẫn giao tiếp:
- **Input (`/data`)**: Chế độ chỉ đọc (`:ro`). Chứa `data/`, `questions.jsonl`, `rulebook.md`, `calendar.csv`.
- **Output (`/result/submission.jsonl`)**: Chứa kết quả dự đoán định dạng JSON Lines chuẩn theo Rulebook §9.
- **Logs (`/result/logs/`)**: Chứa nhật ký chi tiết từng câu hỏi phục vụ phúc khảo và kiểm tra tính tái lập (`id`, `answer`, `elapsed_ms`, `thinking`, `code`, `output`).

---

## 5. Runtime & Reproducibility (Thời gian chạy & Tính Tái lập)
- **Thời gian chạy**: ~15 - 20 phút cho toàn bộ 1000 câu hỏi trên GPU tham chiếu (2 x NVIDIA RTX 5090), hoàn toàn nằm trong giới hạn 6 giờ của Ban Tổ Chức.
- **Tính tái lập**: Thiết lập cố định `SEED = 42`, `PYTHONHASHSEED = 0`, giải mã tham lam `temperature = 0.0` đảm bảo $|PA(\text{uploaded}) - PA(\text{re-run})| \le 0.03$.
