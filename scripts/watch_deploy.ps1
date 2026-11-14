<<<<<<< Updated upstream
# watch_deploy.ps1 вЂ” С„РѕРЅРѕРІС‹Р№ watcher РґР»СЏ Р°РІС‚Рѕ-РґРµРїР»РѕСЏ РЅР° Windows.
#
# РњРѕРЅРёС‚РѕСЂРёС‚ data/deploy.trigger. РџСЂРё РїРѕСЏРІР»РµРЅРёРё С„Р°Р№Р»Р° вЂ” git pull + docker compose.
#
# Р—Р°РїСѓСЃРє РІСЂСѓС‡РЅСѓСЋ:
#   powershell -ExecutionPolicy Bypass -File "C:\tg-autopost-manager\scripts\watch_deploy.ps1"
#
# Р РµРіРёСЃС‚СЂР°С†РёСЏ РєР°Рє С„РѕРЅРѕРІРѕР№ Р·Р°РґР°С‡Рё (Р°РІС‚РѕР·Р°РїСѓСЃРє):
#   powershell -ExecutionPolicy Bypass -File "C:\tg-autopost-manager\scripts\watch_deploy.ps1" -Register
=======
# watch_deploy.ps1 — фоновый watcher для авто-деплоя на Windows.
#
# Запускается на хосте (НЕ в контейнере). Мониторит data/deploy.trigger.
# При появлении файла — выполняет git pull + docker compose up --build -d.
#
# Установка (один раз):
#   1. Откройте PowerShell от имени Администратора
#   2. Выполните: Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
#   3. Зарегистрируйте как фоновую задачу (автозапуск):
#      powershell -ExecutionPolicy Bypass -File "C:\tg-autopost-manager\scripts\watch_deploy.ps1" -Register
#
# Или просто запускайте вручную в отдельном окне PowerShell:
#   powershell -ExecutionPolicy Bypass -File "C:\tg-autopost-manager\scripts\watch_deploy.ps1"
#
# Для остановки watcher'а: закройте окно PowerShell или выполните:
#   Stop-Process -Name powershell -ErrorAction SilentlyContinue
>>>>>>> Stashed changes

param(
    [string]$ProjectDir = "C:\tg-autopost-manager",
    [switch]$Register,
    [int]$PollInterval = 3
)

<<<<<<< Updated upstream
$ErrorActionPreference = "Continue"
=======
$ErrorActionPreference = "Stop"
>>>>>>> Stashed changes

$TriggerFile = Join-Path $ProjectDir "data\deploy.trigger"
$LogFile = Join-Path $ProjectDir "data\deploy.log"

function Write-DeployLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
<<<<<<< Updated upstream
    Add-Content -Path $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    Write-Host $line
}

# --- РќР°Р№С‚Рё git.exe ---
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

# --- РќР°Р№С‚Рё docker.exe ---
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

# --- Р РµР¶РёРј СЂРµРіРёСЃС‚СЂР°С†РёРё ---
if ($Register) {
    if (-not $GitExe) {
        Write-Host "РћРЁРР‘РљРђ: Git РЅРµ РЅР°Р№РґРµРЅ!" -ForegroundColor Red
        exit 1
    }
    if (-not $DockerExe) {
        Write-Host "РћРЁРР‘РљРђ: Docker РЅРµ РЅР°Р№РґРµРЅ!" -ForegroundColor Red
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
        Write-Host "Watcher Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅ РєР°Рє С„РѕРЅРѕРІР°СЏ Р·Р°РґР°С‡Р° '$taskName'." -ForegroundColor Green
        Write-Host "Git: $GitExe" -ForegroundColor Gray
        Write-Host "Docker: $DockerExe" -ForegroundColor Gray
    } catch {
        Write-Host "РћС€РёР±РєР°: $_" -ForegroundColor Red
        Write-Host "Р—Р°РїСѓСЃС‚РёС‚Рµ PowerShell РѕС‚ РёРјРµРЅРё РђРґРјРёРЅРёСЃС‚СЂР°С‚РѕСЂР°." -ForegroundColor Yellow
=======
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

# Режим регистрации как фоновой задачи Windows
if ($Register) {
    $taskName = "TgAutoDeployWatcher"
    $scriptPath = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -ProjectDir `"$ProjectDir`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
        Write-Host "Watcher зарегистрирован как фоновая задача '$taskName'." -ForegroundColor Green
        Write-Host "Запускается при старте Windows. Для проверки:" -ForegroundColor Cyan
        Write-Host "  Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo" -ForegroundColor Gray
        Write-Host "Для удаления:" -ForegroundColor Cyan
        Write-Host "  Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false" -ForegroundColor Gray
    } catch {
        Write-Host "Ошибка регистрации: $_" -ForegroundColor Red
        Write-Host "Запустите PowerShell от имени Администратора." -ForegroundColor Yellow
>>>>>>> Stashed changes
    }
    exit
}

<<<<<<< Updated upstream
# --- РџСЂРѕРІРµСЂРєР° ---
if (-not $GitExe) {
    Write-Host "РћРЁРР‘РљРђ: Git РЅРµ РЅР°Р№РґРµРЅ! РЈСЃС‚Р°РЅРѕРІРёС‚Рµ Git for Windows." -ForegroundColor Red
    exit 1
}
if (-not $DockerExe) {
    Write-Host "РћРЁРР‘РљРђ: Docker РЅРµ РЅР°Р№РґРµРЅ! РЈСЃС‚Р°РЅРѕРІРёС‚Рµ Docker Desktop." -ForegroundColor Red
    exit 1
}

# --- РћСЃРЅРѕРІРЅРѕР№ С†РёРєР» ---
Write-DeployLog "=== Watcher started (poll=${PollInterval}s) ==="
Write-DeployLog "Git: $GitExe"
Write-DeployLog "Docker: $DockerExe"
Write-Host "Watcher Р·Р°РїСѓС‰РµРЅ. Git: $GitExe" -ForegroundColor Green
Write-Host "РњРѕРЅРёС‚РѕСЂСЋ С‚СЂРёРіРіРµСЂ РєР°Р¶РґС‹Рµ $PollInterval СЃРµРє. Ctrl+C РґР»СЏ РѕСЃС‚Р°РЅРѕРІРєРё." -ForegroundColor Gray
=======
# Основной цикл watcher'а
Write-DeployLog "=== Watcher started (polling every ${PollInterval}s) ==="
Write-DeployLog "ProjectDir: $ProjectDir"
Write-DeployLog "TriggerFile: $TriggerFile"
Write-Host "Watcher запущен. Мониторю $TriggerFile каждые $PollInterval сек..." -ForegroundColor Green
Write-Host "Нажмите Ctrl+C для остановки." -ForegroundColor Gray
>>>>>>> Stashed changes
Write-Host ""

while ($true) {
    try {
        if (Test-Path $TriggerFile) {
<<<<<<< Updated upstream
            $content = Get-Content $TriggerFile -ErrorAction SilentlyContinue
            $commit = if ($content -and $content[0]) { $content[0] } else { "unknown" }
=======
            # Читаем commit hash из файла-триггера
            $content = Get-Content $TriggerFile -ErrorAction SilentlyContinue
            $commit = if ($content -and $content[0]) { $content[0] } else { "unknown" }
            $ref = if ($content -and $content.Count -gt 1) { $content[1] } else { "unknown" }

            # Удаляем триггер сразу (чтобы не сработал повторно)
>>>>>>> Stashed changes
            Remove-Item $TriggerFile -Force -ErrorAction SilentlyContinue

            Write-DeployLog "=== Deploy triggered (commit=$commit) ==="

<<<<<<< Updated upstream
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
=======
            # 1. git pull
            Write-DeployLog "Pulling latest code..."
            Push-Location $ProjectDir
            try {
                $gitOutput = git fetch origin main 2>&1
                Write-DeployLog "git fetch: $gitOutput"
                $gitOutput = git reset --hard origin/main 2>&1
                Write-DeployLog "git reset: $gitOutput"
                $gitVersion = git rev-parse --short HEAD 2>&1
                Write-DeployLog "Code updated to $gitVersion"
>>>>>>> Stashed changes
            } catch {
                Write-DeployLog "git error: $_"
            }

<<<<<<< Updated upstream
            # 2. docker compose down + up --build -d
            Write-DeployLog "Rebuilding containers..."
            try {
                $out = & $DockerExe compose down 2>&1 | Out-String
                Write-DeployLog "docker down: $($out.Trim())"
                $out = & $DockerExe compose up --build -d 2>&1 | Out-String
                Write-DeployLog "docker up: $($out.Trim())"
=======
            # 2. docker compose up --build -d
            Write-DeployLog "Rebuilding containers..."
            try {
                $dockerOutput = docker compose down 2>&1
                Write-DeployLog "docker down: $dockerOutput"
                $dockerOutput = docker compose up --build -d 2>&1
                Write-DeployLog "docker up: $dockerOutput"
>>>>>>> Stashed changes
            } catch {
                Write-DeployLog "docker error: $_"
            }
            Pop-Location

<<<<<<< Updated upstream
            Write-DeployLog "Waiting 10s..."
            Start-Sleep -Seconds 10

            Push-Location $ProjectDir
            try {
                $status = & $DockerExe compose ps 2>&1 | Out-String
                Write-DeployLog "Status: $($status.Trim())"
            } catch {}
            Pop-Location

            Write-DeployLog "=== Deploy completed ==="
            Write-Host "Deploy Р·Р°РІРµСЂС€С‘РЅ!" -ForegroundColor Green
=======
            # 3. Ждём запуска
            Write-DeployLog "Waiting 10s for containers..."
            Start-Sleep -Seconds 10

            # 4. Проверяем статус
            Push-Location $ProjectDir
            try {
                $status = docker compose ps 2>&1
                Write-DeployLog "Container status: $status"
            } catch {
                Write-DeployLog "docker ps error: $_"
            }
            Pop-Location

            Write-DeployLog "=== Deploy completed ==="
            Write-Host "Deploy завершён!" -ForegroundColor Green
>>>>>>> Stashed changes
        }
    } catch {
        Write-DeployLog "Watcher error: $_"
    }
<<<<<<< Updated upstream
=======

>>>>>>> Stashed changes
    Start-Sleep -Seconds $PollInterval
}
