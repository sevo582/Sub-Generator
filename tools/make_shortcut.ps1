<#
.SYNOPSIS
    Слага пряк път до subs на десктопа.

.DESCRIPTION
    Търси subs-gui.exe там, където pip го е сложил — първо във виртуалната
    среда на проекта, после в Scripts на Python. Ако не го намери, казва
    какво липсва, вместо да направи пряк път, който не работи.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
#>

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot

$candidates = @(
    (Join-Path $project ".venv\Scripts\subs-gui.exe"),
    (Join-Path $project "venv\Scripts\subs-gui.exe")
)
$onPath = Get-Command subs-gui -ErrorAction SilentlyContinue
if ($onPath) { $candidates += $onPath.Source }

$target = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $target) {
    Write-Error @"
Не намирам subs-gui.exe. Първо инсталирай пакета:

    py -3.12 -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -e .

и пусни този скрипт наново.
"@
}

$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "subs.lnk"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $project
$shortcut.Description = "Анимирани субтитри за вертикално видео"
$shortcut.Save()

Write-Host "Прекият път е готов: $link"
Write-Host "Сочи към: $target"
