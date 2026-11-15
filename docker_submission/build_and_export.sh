#!/usr/bin/env bash
# ==============================================================================
# Script đóng gói Docker Image chuẩn quy chế Zalo AI Challenge 2026 (Section 11)
# Sử dụng: ./build_and_export.sh <ten_doi_thi>
# Ví dụ:   ./build_and_export.sh myteam
# ==============================================================================
set -euo pipefail

TEAM_NAME="${1:-myteam}"
# Chuyển tên đội thành chữ thường không dấu theo quy chế
TEAM_NAME=$(echo "$TEAM_NAME" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]_')

IMAGE_NAME="zaic2026_datamate_${TEAM_NAME}:final"
ARCHIVE_NAME="zaic2026_datamate_${TEAM_NAME}.tar.gz"
MD5_NAME="zaic2026_datamate_${TEAM_NAME}.tar.gz.md5"

echo "=================================================================="
echo "BƯỚC 1: Build Docker Image cho nền tảng linux/amd64"
echo "  Tên Image: ${IMAGE_NAME}"
echo "=================================================================="
docker build --platform linux/amd64 -t "${IMAGE_NAME}" .

echo ""
echo "=================================================================="
echo "BƯỚC 2: Kiểm tra kiến trúc và kích thước Image"
echo "=================================================================="
docker image inspect -f 'Hệ điều hành / Kiến trúc: {{.Os}}/{{.Architecture}} | Kích thước: {{.Size}} bytes' "${IMAGE_NAME}"

echo ""
echo "=================================================================="
echo "BƯỚC 3: Xuất và nén Docker Image thành file .tar.gz"
echo "  File xuất ra: ${ARCHIVE_NAME}"
echo "=================================================================="
set -o pipefail
if command -v pigz &> /dev/null; then
    echo "Phát hiện pigz, sử dụng pigz để nén nhanh hơn..."
    docker save "${IMAGE_NAME}" | pigz -p 8 > "${ARCHIVE_NAME}"
else
    echo "Sử dụng gzip tiêu chuẩn để nén..."
    docker save "${IMAGE_NAME}" | gzip > "${ARCHIVE_NAME}"
fi

echo ""
echo "=================================================================="
echo "BƯỚC 4: Tính dung lượng uncompressed .tar (Quy chế: tối đa 100 GB)"
echo "=================================================================="
if command -v pigz &> /dev/null; then
    UNCOMPRESSED_BYTES=$(pigz -dc "${ARCHIVE_NAME}" | wc -c)
else
    UNCOMPRESSED_BYTES=$(gzip -dc "${ARCHIVE_NAME}" | wc -c)
fi
echo "Dung lượng .tar giải nén: ${UNCOMPRESSED_BYTES} bytes"

echo ""
echo "=================================================================="
echo "BƯỚC 5: Tạo mã MD5 checksum"
echo "  File MD5: ${MD5_NAME}"
echo "=================================================================="
md5sum "${ARCHIVE_NAME}" > "${MD5_NAME}"
cat "${MD5_NAME}"

echo ""
echo "=================================================================="
echo "HOÀN TẤT ĐÓNG GÓI! BẠN ĐÃ CÓ:"
echo "1. File Image nén : ${ARCHIVE_NAME}"
echo "2. File MD5       : ${MD5_NAME}"
echo "3. Dung lượng .tar: ${UNCOMPRESSED_BYTES} bytes"
echo "=> Đưa file ${ARCHIVE_NAME} lên Google Drive (chế độ Công khai)"
echo "   và gửi link kèm file MD5 cho Ban Tổ Chức theo Section 11!"
echo "=================================================================="
