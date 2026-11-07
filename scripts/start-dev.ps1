#Requires -Version 5.1
<#
.SYNOPSIS
    Menjalankan HXconvertX (FastAPI + Next.js) untuk pengembangan lokal di Windows.

.DESCRIPTION
    Memeriksa Python, Node.js, dan dependensi eksternal opsional (LibreOffice,
    Tesseract/RapidOCR). Membuat virtualenv, memasang dependensi, menyiapkan file
    .env, menjalankan migrasi database, lalu membuka backend dan frontend.

.EXAMPLE
    .\scripts\start-dev.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

function Write-Section([string]$text) {
    Write-Host ""
    Write-Host "== $text ==" -ForegroundColor Cyan
}

function Write-Ok([string]$text) { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  [WARN] $text" -ForegroundColor Yellow }
function Write-Fail([string]$text) { Write-Host "  [FAIL] $text" -ForegroundColor Red }

function Get-NodeMajor {
    $raw = (& node --version).Trim().TrimStart("v")
    return [int]($raw.Split(".")[0])
}

function Test-PortInUse([int]$port) {
    $connection = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return $null -ne $connection
}

function Find-LibreOffice {
    $command = Get-Command soffice -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base) {
            $candidate = Join-Path $base "LibreOffice\program\soffice.exe"
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Find-MicrosoftWord {
    $command = Get-Command WINWORD.EXE -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base) { continue }
        foreach ($version in @("Office16", "Office15", "Office14")) {
            foreach ($candidate in @(
                (Join-Path $base "Microsoft Office\root\$version\WINWORD.EXE"),
                (Join-Path $base "Microsoft Office\$version\WINWORD.EXE")
            )) {
                if (Test-Path $candidate) { return $candidate }
            }
        }
    }
    return $null
}

function Find-Tesseract {
    $command = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidate = Join-Path $env:ProgramFiles "Tesseract-OCR\tesseract.exe"
    if (Test-Path $candidate) { return $candidate }
    return $null
}

Write-Host "HXconvertX - local development" -ForegroundColor White
Write-Host "Project: $root"

Write-Section "Pemeriksaan dependensi utama"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Fail "Python tidak ditemukan. Pasang Python 3.11+ dan pastikan ada di PATH."
    Write-Host "        https://www.python.org/downloads/windows/"
    exit 1
}
$pythonVersion = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))").Trim()
if ([version]$pythonVersion -lt [version]"3.11") {
    Write-Fail "Python $pythonVersion terdeteksi; dibutuhkan 3.11 atau lebih baru."
    exit 1
}
Write-Ok "Python $pythonVersion"

$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Fail "Node.js tidak ditemukan. Pasang Node.js 18+ dari https://nodejs.org/"
    exit 1
}
$nodeMajor = Get-NodeMajor
if ($nodeMajor -lt 18) {
    Write-Fail "Node.js versi $nodeMajor terdeteksi; dibutuhkan 18 atau lebih baru."
    exit 1
}
Write-Ok "Node.js $(& node --version)"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Fail "npm tidak ditemukan. Instalasi Node.js biasanya menyertakannya."
    exit 1
}
Write-Ok "npm $(& npm --version)"

$backendAlreadyRunning = $false
if (Test-PortInUse 8000) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 2 | Out-Null
        $backendAlreadyRunning = $true
        Write-Ok "Backend sudah berjalan di port 8000; tidak dijalankan ulang."
    } catch {
        Write-Fail "Port 8000 dipakai proses lain yang bukan HXconvertX. Hentikan proses itu terlebih dahulu."
        exit 1
    }
}

$frontendAlreadyRunning = $false
if (Test-PortInUse 3000) {
    Write-Warn "Port 3000 sedang dipakai. Frontend tidak akan dijalankan ulang."
    $frontendAlreadyRunning = $true
}

Write-Section "Konfigurasi .env"

$backendEnv = Join-Path $backendDir ".env"
if (-not (Test-Path $backendEnv)) {
    Copy-Item (Join-Path $backendDir ".env.example") $backendEnv
    $secret = (& python -c "import secrets; print(secrets.token_urlsafe(48))").Trim()
    (Get-Content $backendEnv) -replace "change-me-to-a-long-random-value", $secret | Set-Content $backendEnv -Encoding ASCII
    Write-Ok "backend\.env dibuat dengan SECRET_KEY acak."
} else {
    Write-Ok "backend\.env sudah ada (tidak ditimpa)."
}

$frontendEnv = Join-Path $frontendDir ".env.local"
if (-not (Test-Path $frontendEnv)) {
    Copy-Item (Join-Path $frontendDir ".env.local.example") $frontendEnv
    Write-Ok "frontend\.env.local dibuat."
} else {
    Write-Ok "frontend\.env.local sudah ada (tidak ditimpa)."
}

Write-Section "Dependensi backend"

if (-not (Test-Path $venvPython)) {
    Write-Host "  Membuat virtualenv..."
    & python -m venv (Join-Path $backendDir ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Fail "Gagal membuat virtualenv."; exit 1 }
}

$requirements = Join-Path $backendDir "requirements.txt"
$marker = Join-Path $backendDir ".venv\.requirements.sha256"
$currentHash = (Get-FileHash $requirements -Algorithm SHA256).Hash
$installedHash = if (Test-Path $marker) { (Get-Content $marker -Raw).Trim() } else { "" }

if ($currentHash -ne $installedHash) {
    Write-Host "  Memasang dependensi Python (mungkin beberapa menit)..."
    & $venvPython -m pip install --upgrade pip --quiet
    & $venvPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { Write-Fail "Instalasi dependensi Python gagal."; exit 1 }
    Set-Content $marker $currentHash -Encoding ASCII
    Write-Ok "Dependensi backend terpasang."
} else {
    Write-Ok "Dependensi backend sudah terpasang."
}

Write-Section "Dependensi frontend"

if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    Write-Host "  Menjalankan npm install..."
    Push-Location $frontendDir
    try {
        & npm install --no-fund --no-audit
        if ($LASTEXITCODE -ne 0) { Write-Fail "npm install gagal."; exit 1 }
    } finally {
        Pop-Location
    }
    Write-Ok "Dependensi frontend terpasang."
} else {
    Write-Ok "node_modules sudah ada."
}

Write-Section "Dependensi eksternal (opsional)"

$winword = Find-MicrosoftWord
if ($winword) {
    Write-Ok "Microsoft Word: $winword"
    Write-Host "         Word ke PDF memakai Word Automation (tata letak paling akurat)."
} else {
    Write-Warn "Microsoft Word tidak ditemukan."
    Write-Host "         Word ke PDF memakai LibreOffice atau renderer HTML; tata letak dapat berbeda."
}

$soffice = Find-LibreOffice
if ($soffice) {
    Write-Ok "LibreOffice: $soffice"
} else {
    Write-Warn "LibreOffice tidak ditemukan."
    Write-Host "         Excel ke PDF memakai renderer HTML sederhana (tata letak terbatas)."
    Write-Host "         Konversi PowerPoint ke PDF/gambar TIDAK tersedia tanpa LibreOffice."
    Write-Host "         Unduh: https://www.libreoffice.org/download/download-libreoffice/"
}

$tesseract = Find-Tesseract
if ($tesseract) {
    Write-Ok "Tesseract OCR: $tesseract"
} else {
    Write-Host "  Mencari RapidOCR..."
    Push-Location $backendDir
    try {
        $ocrName = (& $venvPython -c "from app.services.capabilities import get_ocr_engine; e = get_ocr_engine(); print(e.name if e else '')" 2>$null).Trim()
    } finally {
        Pop-Location
    }
    if ($ocrName) {
        Write-Ok "OCR tersedia melalui $ocrName."
    } else {
        Write-Warn "Tidak ada mesin OCR. Alat OCR akan ditandai tidak tersedia."
        Write-Host "         Pasang Tesseract (https://github.com/UB-Mannheim/tesseract/wiki)"
        Write-Host "         atau jalankan: backend\.venv\Scripts\python -m pip install rapidocr-onnxruntime"
    }
}

Write-Section "Database"

Push-Location $backendDir
try {
    & $venvPython -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { Write-Fail "Migrasi database gagal. Periksa DATABASE_URL di backend\.env."; exit 1 }
    Write-Ok "Database siap."
} finally {
    Pop-Location
}

Write-Section "Menjalankan server"

if (-not $backendAlreadyRunning) {
    $backendCommand = "Set-Location -LiteralPath '$backendDir'; & '$venvPython' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
    Start-Process -FilePath "powershell" -ArgumentList @("-NoExit", "-Command", $backendCommand) | Out-Null
    Write-Ok "Backend dimulai di http://127.0.0.1:8000"
    Start-Sleep -Seconds 3
}

if (-not $frontendAlreadyRunning) {
    $frontendCommand = "Set-Location -LiteralPath '$frontendDir'; `$env:NEXT_PUBLIC_API_URL='http://127.0.0.1:8000'; npm run dev -- --port 3000"
    Start-Process -FilePath "powershell" -ArgumentList @("-NoExit", "-Command", $frontendCommand) | Out-Null
    Write-Ok "Frontend dimulai di http://127.0.0.1:3000"
}

Write-Host ""
$healthy = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 2 | Out-Null
        $healthy = $true
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}

if ($healthy) {
    Write-Host "HXconvertX siap digunakan." -ForegroundColor Green
    Write-Host "  Aplikasi : http://127.0.0.1:3000"
    Write-Host "  API docs : http://127.0.0.1:8000/docs"
    Start-Process "http://127.0.0.1:3000"
} else {
    Write-Warn "Backend belum merespons. Periksa jendela PowerShell backend untuk detail kesalahan."
}

Write-Host ""
Write-Host "Hentikan dengan menutup kedua jendela PowerShell, atau tekan Ctrl+C di masing-masing jendela."
