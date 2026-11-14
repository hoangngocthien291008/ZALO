# HƯỚNG DẪN DỰ ÁN DATAMATE - ZALO AI CHALLENGE 2026

Dự án này là giải pháp toàn diện, tự động cho đề tài **DataMate - Natural Language Vietnamese Question Answering over Tabular & Relational Business Data** trong khuôn khổ cuộc thi **Zalo AI Challenge 2026**.

Toàn bộ mã nguồn, cấu hình môi trường, script chạy cục bộ, script đánh giá và Dockerfile đã được thiết lập hoàn chỉnh và sẵn sàng sử dụng.

---

## 1. Cấu trúc thư mục giải pháp (`bài làm nộp thử/`)

```text
bài làm nộp thử/
├── datamate/                       # Gói mã nguồn cốt lõi DataMate
│   ├── __init__.py                 # Khởi tạo package
│   ├── calendar_resolver.py        # Xử lý âm lịch, dương lịch, khoảng thời gian Tết
│   ├── time_parser.py              # Phân tích biểu thức thời gian tương đối & ca làm việc
│   ├── data_loader.py              # Đọc tự động CSV, Excel (đa sheet, merged cell), SQLite
│   ├── business_adapter.py         # Chuẩn hóa dữ liệu cửa hàng, liên kết đơn hàng & chi tiết
│   ├── question_parser.py          # Phân loại ý định (intent) & trích xuất thực thể
│   ├── query_executors.py          # Động cơ tính toán theo đúng quy chuẩn Rulebook §1-10
│   ├── formatters.py               # Kiểm tra & chuẩn hóa định dạng kết quả nộp bài
│   └── pipeline.py                 # Bộ điều phối xử lý câu hỏi & ghi log chi tiết
├── predict.py                      # File thực thi chính chuẩn Docker BTC (/data -> /result)
├── predict.sh                      # Shell script entrypoint cho container Docker
├── evaluate.py                     # Script tính điểm Penalized Accuracy (PA) độc lập
├── run_local.py                    # Script chạy suy luận & test cục bộ trên máy tính
├── requirements.txt                # Danh sách thư viện Python cần thiết
├── Dockerfile                      # File xây dựng Docker container nộp bài
├── submission.jsonl                # File kết quả suy luận nộp bài (public_test)
└── README.md                       # Bản hướng dẫn chi tiết này
```

---

## 2. Các nguyên tắc xử lý cốt lõi (Rulebook Compliance)

Hệ thống được thiết kế bám sát từng điều khoản trong tài liệu đặc tả `rulebook.md` của Ban Tổ Chức:
1. **Quy tắc doanh thu thuần (§1, §14):**
   - Loại trừ hoàn toàn các đơn hàng bị hủy (`Đã hủy`, `huỷ`, `cancel`) và đơn hàng bị trả lại (`trả lại`, `hoàn hàng`).
   - Tự động cộng dồn doanh thu hàng hóa và phụ thu (phí phục vụ).
   - Tự động nhận diện cửa hàng ghi đơn giá theo đơn vị nghìn đồng (`unit_price < 500`) và chuẩn hóa về đồng VND chuẩn.
2. **Xử lý thời gian & lịch (§2, §3, §36):**
   - Chuyển đổi chính xác các mốc Tết âm lịch dựa trên `calendar.csv` (ví dụ: Tết 2026 từ 23 tháng Chạp đến hết mùng 10 tháng Giêng).
   - "Trước Tết": khoảng thời gian 3 tuần (21 ngày) trước 23 tháng Chạp.
   - Ca làm việc: Ca sáng (`06:00` đến `13:59`), Ca chiều (`14:00` đến `21:59`).
   - "N ngày gần nhất" / "hôm nay": tính lùi từ `as_of_date` (không bao gồm các giao dịch sau `as_of_date`).
3. **Bảng công nợ & khách hàng (§54 - §56):**
   - Lọc bỏ khách lẻ (`khách lẻ`), chỉ giữ khách hàng có mã và tên rõ ràng.
   - Loại trừ đơn hàng COD và đơn hàng qua sàn thương mại điện tử (Shopee, Lazada, TikTok).
4. **Quy chuẩn thuốc & y tế (§87 - §89):**
   - Trích xuất bảng danh mục thuốc, hoạt chất và cờ kê đơn (`Thuốc kê đơn = True`).
   - Chuẩn hóa tên thuốc và khoảng trắng (`re.sub(r'\s+', ' ', name)`).
5. **Chiến lược tối ưu điểm số Penalized Accuracy (§9):**
   - Công thức tính điểm: `PA = (Correct - Wrong) / Total`.
   - Đáp án đúng: `+1` điểm.
   - Đáp án sai: `-1` điểm.
   - Không trả lời (`abstain`): `0` điểm.
   - Hệ thống tự động chuyển sang `abstain` khi độ tin cậy thấp hoặc không tìm thấy dữ liệu mặt hàng cụ thể, giúp bảo toàn điểm số tốt hơn nhiều so với đoán mò dẫn đến bị trừ điểm.

---

## 3. Hướng dẫn chạy thử nghiệm cục bộ (Local Run)

Bạn có thể chạy thử nghiệm trực tiếp trên máy tính thông qua script `run_local.py`:

### A. Chạy suy luận trên tập `public_test` (1.000 câu hỏi):
```bash
python run_local.py --split public_test --output submission.jsonl --log_dir logs
```
- Kết quả sẽ được ghi vào file `submission.jsonl` trong thư mục hiện tại.
- Thời gian thực thi và chi tiết từng câu hỏi được lưu trong thư mục `logs/`.

### B. Chạy đánh giá trên tập `train` (có đáp án tham chiếu `answers.jsonl`):
Chạy thử nghiệm trên 10 câu đầu:
```bash
python run_local.py --split train --limit 10 --eval
```
Chạy toàn bộ tập huấn luyện:
```bash
python run_local.py --split train --eval
```

---

## 4. Hướng dẫn đóng gói và kiểm thử Docker

Theo quy định của Zalo AI Challenge, mô hình vòng chung kết sẽ chạy trong Docker container độc lập, không có kết nối internet:

### A. Build Docker Image:
Mở terminal tại thư mục `bài làm nộp thử` và chạy:
```bash
docker build -t datamate_solution:latest .
```

### B. Chạy kiểm thử Container với dữ liệu test:
Giả sử dữ liệu kiểm tra nằm tại `D:/ZALO AI CHALLENNGE/DATA MATE(which we will choose)/public_test`:
```bash
docker run --rm \
  -v "D:/ZALO AI CHALLENNGE/DATA MATE(which we will choose)/public_test":/data:ro \
  -v "D:/ZALO AI CHALLENNGE/bài làm nộp thử/docker_result":/result \
  datamate_solution:latest
```

Sau khi chạy xong:
- File kết quả nộp bài sẽ nằm tại `docker_result/submission.jsonl`.
- Logs chi tiết từng câu sẽ nằm tại `docker_result/logs/`.

---

## 5. Hướng dẫn nộp bài lên cổng Zalo AI Challenge

### Bước 1: Nộp file `submission.jsonl` (Vòng Sơ Loại / Bảng Xếp Hạng Public)
1. Truy cập vào trang web chính thức của cuộc thi: [https://challenge.zalo.ai/](https://challenge.zalo.ai/)
2. Đăng nhập tài khoản đội thi của bạn và chọn bài thi **DataMate**.
3. Chọn mục **Submit** / **Nộp bài**.
4. Tải file `submission.jsonl` (đã được sinh sẵn tại `bài làm nộp thử/submission.jsonl`) lên hệ thống.
5. Kiểm tra trạng thái chấm điểm tự động trên bảng xếp hạng (Leaderboard).

### Bước 2: Nộp mã nguồn / Docker Image (Vòng Đánh Giá Cuối Cùng)
Khi Ban Tổ Chức yêu cầu nộp container để chạy trên tập dữ liệu Private Test:
1. Xuất Docker image thành file nén:
   ```bash
   docker save datamate_solution:latest | gzip > datamate_solution.tar.gz
   ```
2. Tải file `datamate_solution.tar.gz` lên Google Drive cá nhân.
3. Cấp quyền truy cập "Anyone with the link can view" (Bất kỳ ai có liên kết đều có thể xem).
4. Điền liên kết tải Docker image và liên kết repository mã nguồn vào biểu mẫu nộp bài theo hướng dẫn của BTC.
