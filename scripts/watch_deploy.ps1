# watch_deploy.ps1 — фоновый watcher для авто-деплоя на Windows.
#
# Запускается на хосте (НЕ в контейнере). Мониторит data/deploy.trigger.
# При появлении файла — выполняет git pull + docker compose up --build -d.
#
# Установка (один раз):
#   1. Откройте PowerShell от имени Администратора
#   2. Выполните: Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
#   3. Зарегистрируйте как фоновую задачу (автозапуск):
#      powershell -ExecutionPolicy Bypass -File "M:\tg-autopost-manager\scripts\watch_deploy.ps1" -Register
#
# Или просто запускайте вручную в отдельном окне PowerShell:
#   powershell -ExecutionPolicy Bypass -File "M:\tg-autopost-manager\scripts\watch_deploy.ps1"
#
# Для остановки watcher'а: закройте окно PowerShell или выполните:
#   Stop-Process -Name powershell -ErrorAction SilentlyContinue

param(
    [string]$ProjectDir = "M:\tg-autopost-manager",
    [switch]$Register,
    [int]$PollInterval = 3
)

$ErrorActionPreference = "Stop"

$TriggerFile = Join-Path $ProjectDir "data\deploy.trigger"
$LogFile = Join-Path $ProjectDir "data\deploy.log"

function Write-DeployLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

# Режим регистрации как фоновой задачи Windows
if ($Register) {
    $taskName = "TgAutoDeployWatcher"
    $scriptPath = $MyInvocation.MyCommand.Path
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -ProjectDir `"$ProjectDir`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RunIndefinitely
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
    }
    exit
}

# Основной цикл watcher'а
Write-DeployLog "=== Watcher started (polling every ${PollInterval}s) ==="
Write-DeployLog "ProjectDir: $ProjectDir"
Write-DeployLog "TriggerFile: $TriggerFile"
Write-Host "Watcher запущен. Мониторю $TriggerFile каждые $PollInterval сек..." -ForegroundColor Green
Write-Host "Нажмите Ctrl+C для остановки." -ForegroundColor Gray
Write-Host ""

while ($true) {
    try {
        if (Test-Path $TriggerFile) {
            # Читаем commit hash из файла-триггера
            $content = Get-Content $TriggerFile -ErrorAction SilentlyContinue
            $commit = if ($content -and $content[0]) { $content[0] } else { "unknown" }
            $ref = if ($content -and $content.Count -gt 1) { $content[1] } else { "unknown" }

            # Удаляем триггер сразу (чтобы не сработал повторно)
            Remove-Item $TriggerFile -Force -ErrorAction SilentlyContinue

            Write-DeployLog "=== Deploy triggered (commit=$commit) ==="

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
            } catch {
                Write-DeployLog "git error: $_"
            }

            # 2. docker compose up --build -d
            Write-DeployLog "Rebuilding containers..."
            try {
                $dockerOutput = docker compose down 2>&1
                Write-DeployLog "docker down: $dockerOutput"
                $dockerOutput = docker compose up --build -d 2>&1
                Write-DeployLog "docker up: $dockerOutput"
            } catch {
                Write-DeployLog "docker error: $_"
            }
            Pop-Location

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
        }
    } catch {
        Write-DeployLog "Watcher error: $_"
    }

    Start-Sleep -Seconds $PollInterval
}
