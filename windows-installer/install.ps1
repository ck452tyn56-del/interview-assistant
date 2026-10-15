$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$instDir = Join-Path $env:LOCALAPPDATA 'Programs\FastAssistant'
$dataDir = Join-Path $env:APPDATA 'FastAssistant'

New-Item -ItemType Directory -Force -Path $instDir, $dataDir | Out-Null

# 1) приложение
Copy-Item (Join-Path $src 'FastAssistant.exe') $instDir -Force
Write-Host '[1/4] FastAssistant.exe ->' $instDir

# 2) ключ API (если лежит рядом с установщиком)
$sec = Join-Path $src 'assistant_secrets.py'
if (Test-Path $sec) {
    Copy-Item $sec $dataDir -Force
    Write-Host '[2/4] API-ключ скопирован ->' $dataDir
} else {
    Write-Warning '[2/4] assistant_secrets.py не найден рядом с установщиком.'
    Write-Warning ('       После установки положи файл с ключом в: ' + $dataDir)
}

# 3) ярлыки: рабочий стол + меню Пуск
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
foreach ($base in @($desktop, $startMenu)) {
    $lnk = $ws.CreateShortcut((Join-Path $base 'Fast Assistant.lnk'))
    $lnk.TargetPath = Join-Path $instDir 'FastAssistant.exe'
    $lnk.WorkingDirectory = $instDir
    $lnk.Description = 'Голосовой IT-ассистент (пробел = говорить)'
    $lnk.Save()
}
Write-Host '[3/4] Ярлыки созданы: рабочий стол + меню Пуск'

# 4) деинсталлятор
$un = @'
@echo off
taskkill /IM FastAssistant.exe /F >nul 2>&1
del "%USERPROFILE%\Desktop\Fast Assistant.lnk" >nul 2>&1
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Fast Assistant.lnk" >nul 2>&1
rmdir /s /q "%LOCALAPPDATA%\Programs\FastAssistant"
echo Fast Assistant removed.
echo (данные и ключ остались в %%APPDATA%%\FastAssistant - можно удалить вручную)
pause
'@
Set-Content -Path (Join-Path $instDir 'uninstall.bat') -Value $un -Encoding ASCII
Write-Host '[4/4] Деинсталлятор:' (Join-Path $instDir 'uninstall.bat')

Write-Host ''
Write-Host 'Готово! Ярлык "Fast Assistant" на рабочем столе.'
Write-Host 'Первый запуск: проверь галку Прокси (нужен работающий туннель на 127.0.0.1:8081).'
