# Clipper – uruchamianie na Windows (bez praw administratora)
# Pierwsze uruchomienie: pobiera uv (Python), ffmpeg, deno (dla yt-dlp) i biblioteki do folderu Clipper\tools i Clipper\.venv
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest jest bardzo wolny z paskiem postępu
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$App   = $PSScriptRoot                                  # ...\Clipper\app
$Root  = Split-Path -Parent $App                        # ...\Clipper
$Tools = Join-Path $Root 'tools'
$Venv  = Join-Path $Root '.venv'
$Py    = Join-Path $Venv 'Scripts\python.exe'
$Log   = Join-Path $Root 'data\start.log'
New-Item -ItemType Directory -Force -Path $Tools, (Join-Path $Root 'data'), (Join-Path $Root 'Klipy') | Out-Null

function Say($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "`n  BŁĄD: $m" -ForegroundColor Red; Write-Host "  Szczegóły: $Log"; Read-Host "`n  Naciśnij Enter, aby zamknąć"; exit 1 }
function Get-File($url, $dest) {
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing; return } catch { Start-Sleep -Seconds 3 }
    }
    Fail "Nie udało się pobrać $url. Sprawdź internet i uruchom ponownie."
}
function Get-Zip($url, $name, $dest) {
    $zip = Join-Path $env:TEMP "clipper_$name.zip"
    Say "Pobieram $name..."; Get-File $url $zip
    $tmp = Join-Path $env:TEMP "clipper_$name"; if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    return $tmp
}

Start-Transcript -Path $Log -Append | Out-Null
Write-Host "`n  === Clipper ===`n" -ForegroundColor Magenta
if ($Root.Length -gt 80) { Write-Host "  Uwaga: folder ma bardzo długą ścieżkę ($Root). Jeśli coś nie działa, przenieś go np. do C:\Clipper" -ForegroundColor Yellow }
if ($Root -match 'OneDrive') { Write-Host "  Uwaga: folder jest w OneDrive – lepiej przenieść go np. do C:\Clipper" -ForegroundColor Yellow }
# tylko nasze skrypty (wcześniej skan całego folderu z .venv/tools = tysiące plików = 100% CPU przy starcie)
Get-ChildItem -Path (Join-Path $Root '*'), (Join-Path $App '*') -File -Include *.ps1,*.bat -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

# --- uv (instaluje Pythona bez admina) ---
$Uv = Join-Path $Tools 'uv\uv.exe'
if (-not (Test-Path $Uv)) {
    $t = Get-Zip 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' 'uv' (Join-Path $Tools 'uv')
    Copy-Item -Force (Get-ChildItem $t -Recurse -Filter 'uv*.exe' | Select-Object -ExpandProperty FullName) (Join-Path $Tools 'uv')
}
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
$env:UV_CACHE_DIR = Join-Path $Tools 'uv-cache'
$env:UV_LINK_MODE = 'copy'

# --- ffmpeg (z libass do napisów) ---
$FfBin = Join-Path $Tools 'ffmpeg\bin'
if (-not (Test-Path (Join-Path $FfBin 'ffmpeg.exe'))) {
    $t = Get-Zip 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' 'ffmpeg' $FfBin
    Get-ChildItem $t -Recurse -Include 'ffmpeg.exe','ffprobe.exe' | Copy-Item -Destination $FfBin -Force
}
# --- deno (silnik JavaScript wymagany przez yt-dlp dla YouTube) ---
$DenoDir = Join-Path $Tools 'deno'
if (-not (Test-Path (Join-Path $DenoDir 'deno.exe'))) {
    $t = Get-Zip 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' 'deno' $DenoDir
    Get-ChildItem $t -Recurse -Filter 'deno.exe' | Copy-Item -Destination $DenoDir -Force
}
$env:PATH = "$FfBin;$DenoDir;$(Join-Path $Tools 'uv');$env:PATH"

# --- Python + biblioteki ---
$Stamp = Join-Path $Venv 'clipper_installed.txt'
$ReqHash = (Get-FileHash (Join-Path $App 'requirements.txt')).Hash
if (-not (Test-Path $Py) -or -not (Test-Path $Stamp) -or ((Get-Content $Stamp -Raw).Trim() -ne $ReqHash)) {
    Say "Instaluję Pythona i biblioteki (pierwszy raz ok. 5-10 min)..."
    if (-not (Test-Path $Py)) { & $Uv venv $Venv --python 3.12 --seed; if ($LASTEXITCODE) { Fail "Nie udało się utworzyć środowiska Python." } }
    & $Uv pip install --python $Py -r (Join-Path $App 'requirements.txt'); if ($LASTEXITCODE) { Fail "Instalacja bibliotek nie powiodła się." }
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        Say "Wykryto kartę NVIDIA - instaluję biblioteki CUDA (ok. 1 GB, opcjonalne)..."
        & $Uv pip install --python $Py nvidia-cublas-cu12 'nvidia-cudnn-cu12==9.*'
        if ($LASTEXITCODE) { Write-Host "  CUDA nie zainstalowana - będzie używany procesor." -ForegroundColor Yellow }
    }
    Set-Content -Path $Stamp -Value $ReqHash
} else {
    # yt-dlp: aktualizacja najwyżej raz na dobę, w tle i z niskim priorytetem (nie blokuje startu)
    $YtStamp = Join-Path $Root 'data\ytdlp_update.txt'
    if (-not (Test-Path $YtStamp) -or ((Get-Date) - (Get-Item $YtStamp).LastWriteTime).TotalHours -gt 24) {
        Say "Aktualizuję yt-dlp w tle..."
        Set-Content -Path $YtStamp -Value (Get-Date)
        $p = Start-Process -FilePath $Uv -ArgumentList @('pip','install','--python',$Py,'-U','yt-dlp[default]') -WindowStyle Hidden -PassThru
        try { $p.PriorityClass = 'BelowNormal' } catch {}
    }
}
# --- start aplikacji ---
$env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'
$env:CLIPPER_HOME = $Root
$Url = 'http://localhost:8765'
try { $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2; Say "Clipper już działa - otwieram przeglądarkę."; Start-Process $Url; Stop-Transcript | Out-Null; exit 0 } catch {}
Say "Uruchamiam Clipper na $Url (nie zamykaj tego okna; zamknięcie = wyłączenie Clippera)"
$job = Start-Job -ScriptBlock { param($u) for ($i=0; $i -lt 60; $i++) { Start-Sleep 1; try { $null = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 2; Start-Process $u; break } catch {} } } -ArgumentList $Url
Stop-Transcript | Out-Null
& $Py -m uvicorn app:app --host 127.0.0.1 --port 8765 --app-dir $App --log-level warning
Write-Host "`n  Clipper został zatrzymany." -ForegroundColor Yellow
Read-Host "  Naciśnij Enter, aby zamknąć"
