# watch_deploy.ps1 — фоновый watcher для авто-деплоя на Windows.
#
# Мониторит data/deploy.trigger. При появлении файла — git pull + docker compose.
#
# Запуск вручную:
#   powershell -ExecutionPolicy Bypass -File "C:\tg-autopost-manager\scripts\watch_deploy.ps1"
#
# Регистрация как фоновой задачи (автозапуск):
#   powershell -ExecutionPolicy Bypass -File "C:\tg-autopost-manager\scripts\watch_deploy.ps1" -Register

param(
    [string]$ProjectDir = "C:\tg-autopost-manager",
    [switch]$Register,
    [int]$PollInterval = 3
)

$ErrorActionPreference = "Continue"

$TriggerFile = Join-Path $ProjectDir "data\deploy.trigger"
$LogFile = Join-Path $ProjectDir "data\deploy.log"

function Write-DeployLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Host $line
}

# --- Найти git.exe ---
function Find-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) { return $git.Source }
    $paths = @(
        "C:\Program Files\Git\cmd\git.exe",
        "C:\Program Files\Git\bin\git.exe",
        "C:\Program Files (x86)\Git\cmd\git.exe",
        "C:\Program Files (x86)\Git\bin\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\bin\git.exe"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) { return $p }
    }
    try {
        $reg = Get-ItemProperty -Path "HKLM:\SOFTWARE\GitForWindows" -Name "InstallPath" -ErrorAction SilentlyContinue
        if ($reg) {
            $exe = Join-Path $reg.InstallPath "bin\git.exe"
            if (Test-Path $exe) { return $exe }
        }
    } catch {}
    return $null
}

# --- Найти docker.exe ---
function Find-Docker {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($docker) { return $docker.Source }
    $paths = @(
        "C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$GitExe = Find-Git
$DockerExe = Find-Docker

# --- Режим регистрации ---
if ($Register) {
    if (-not $GitExe) {
        Write-Host "ОШИБКА: Git не найден!" -ForegroundColor Red
        exit 1
    }
    if (-not $DockerExe) {
        Write-Host "ОШИБКА: Docker не найден!" -ForegroundColor Red
        exit 1
    }
    $taskName = "TgAutoDeployWatcher"
    $scriptPath = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -ProjectDir `"$ProjectDir`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
        Write-Host "Watcher зарегистрирован как фоновая задача '$taskName'." -ForegroundColor Green
        Write-Host "Git: $GitExe" -ForegroundColor Gray
        Write-Host "Docker: $DockerExe" -ForegroundColor Gray
    } catch {
        Write-Host "Ошибка: $_" -ForegroundColor Red
        Write-Host "Запустите PowerShell от имени Администратора." -ForegroundColor Yellow
    }
    exit
}

# --- Проверка ---
if (-not $GitExe) {
    Write-Host "ОШИБКА: Git не найден! Установите Git for Windows." -ForegroundColor Red
    exit 1
}
if (-not $DockerExe) {
    Write-Host "ОШИБКА: Docker не найден! Установите Docker Desktop." -ForegroundColor Red
    exit 1
}

# --- Основной цикл ---
Write-DeployLog "=== Watcher started (poll=${PollInterval}s) ==="
Write-DeployLog "Git: $GitExe"
Write-DeployLog "Docker: $DockerExe"
Write-Host "Watcher запущен. Git: $GitExe" -ForegroundColor Green
Write-Host "Мониторю триггер каждые $PollInterval сек. Ctrl+C для остановки." -ForegroundColor Gray
Write-Host ""

while ($true) {
    try {
        if (Test-Path $TriggerFile) {
            $content = Get-Content $TriggerFile -ErrorAction SilentlyContinue
            $commit = if ($content -and $content[0]) { $content[0] } else { "unknown" }
            Remove-Item $TriggerFile -Force -ErrorAction SilentlyContinue

            Write-DeployLog "=== Deploy triggered (commit=$commit) ==="

            # 1. git fetch + reset
            Write-DeployLog "Pulling latest code..."
            Push-Location $ProjectDir
            try {
                $out = & $GitExe fetch origin main 2>&1 | Out-String
                Write-DeployLog "git fetch: $($out.Trim())"
                $out = & $GitExe reset --hard origin/main 2>&1 | Out-String
                Write-DeployLog "git reset: $($out.Trim())"
                $ver = & $GitExe rev-parse --short HEAD 2>&1 | Out-String
                Write-DeployLog "Code updated to $($ver.Trim())"
            } catch {
                Write-DeployLog "git error: $_"
            }

            # 2. docker compose down + up --build -d
            Write-DeployLog "Rebuilding containers..."
            try {
                $out = & $DockerExe compose down 2>&1 | Out-String
                Write-DeployLog "docker down: $($out.Trim())"
                $out = & $DockerExe compose up --build -d 2>&1 | Out-String
                Write-DeployLog "docker up: $($out.Trim())"
            } catch {
                Write-DeployLog "docker error: $_"
            }
            Pop-Location

            Write-DeployLog "Waiting 10s..."
            Start-Sleep -Seconds 10

            Push-Location $ProjectDir
            try {
                $status = & $DockerExe compose ps 2>&1 | Out-String
                Write-DeployLog "Status: $($status.Trim())"
            } catch {}
            Pop-Location

            Write-DeployLog "=== Deploy completed ==="
            Write-Host "Deploy завершён!" -ForegroundColor Green
        }
    } catch {
        Write-DeployLog "Watcher error: $_"
    }
    Start-Sleep -Seconds $PollInterval
}
