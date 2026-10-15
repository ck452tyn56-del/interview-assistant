@echo off
REM Сборка установочного EXE + обновление папки windows-installer
REM Требуется: pip install pyinstaller pyaudiowpatch sounddevice google-genai numpy pynput
py -m PyInstaller --noconfirm --onefile --windowed --name FastAssistant ^
   --collect-all pyaudiowpatch --collect-all sounddevice fast_assistant.py
if errorlevel 1 (echo BUILD FAILED & exit /b 1)
copy /y dist\FastAssistant.exe windows-installer\FastAssistant.exe
powershell -NoProfile -Command "Compress-Archive -Path windows-installer\* -DestinationPath FastAssistant-Windows-1.0.zip -Force"
echo DONE: dist\FastAssistant.exe + FastAssistant-Windows-1.0.zip
