param(
    [string]$TeamName = "36_thanh_hoa"
)

# Chuẩn hóa tên đội
$cleanName = ($TeamName.ToLower() -replace '[^a-z0-9_]', '_') -replace '_+', '_' -replace '^_|_$', ''
if (-not $cleanName) { $cleanName = "36_thanh_hoa" }

$imageName = "zaic2026_datamate_${cleanName}:final"
$tarName = "zaic2026_datamate_${cleanName}.tar"
$gzName = "zaic2026_datamate_${cleanName}.tar.gz"
$md5Name = "zaic2026_datamate_${cleanName}.tar.gz.md5"

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "BUOC 1: Build Docker Image cho nen tang linux/amd64" -ForegroundColor Cyan
Write-Host "  Ten Image chuan hoa: $imageName" -ForegroundColor Yellow
Write-Host "=================================================================="

docker build --platform linux/amd64 -t $imageName .
if ($LASTEXITCODE -ne 0) {
    Write-Host "[LOI] Build Docker that bai! Hay kiem tra Docker Desktop da chay chua." -ForegroundColor Red
    exit 1
}

Write-Host "`n==================================================================" -ForegroundColor Cyan
Write-Host "BUOC 2: Kiem tra kien truc va kich thuoc Image" -ForegroundColor Cyan
Write-Host "=================================================================="
docker image inspect -f '{{.Os}}/{{.Architecture}} | Kich thuoc: {{.Size}} bytes' $imageName

Write-Host "`n==================================================================" -ForegroundColor Cyan
Write-Host "BUOC 3: Xuat Docker Image ra file .tar va nen thanh .tar.gz" -ForegroundColor Cyan
Write-Host "=================================================================="
docker save $imageName -o $tarName

Write-Host "Dang nen file $tarName thanh $gzName..."
tar -czvf $gzName $tarName
Remove-Item -Path $tarName -Force

Write-Host "`n==================================================================" -ForegroundColor Cyan
Write-Host "BUOC 4: Tao ma MD5 Checksum" -ForegroundColor Cyan
Write-Host "=================================================================="
$hash = (Get-FileHash -Path $gzName -Algorithm MD5).Hash.ToLower()
"$hash  $gzName" | Set-Content -Path $md5Name -Encoding utf8
Write-Host "Ma MD5: $hash" -ForegroundColor Green

Write-Host "`n==================================================================" -ForegroundColor Cyan
Write-Host "HOAN TAT DONG GOI!" -ForegroundColor Green
Write-Host "1. File nen : $gzName" -ForegroundColor Yellow
Write-Host "2. File MD5 : $md5Name" -ForegroundColor Yellow
Write-Host "3. Ma MD5 de dien vao form web: $hash" -ForegroundColor Green
Write-Host "=================================================================="
