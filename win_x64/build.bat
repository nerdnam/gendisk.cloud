@echo off
REM ncloud-sync 를 단일 .exe 로 빌드 (win_x64\dist\ncloud-sync.exe)
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean ncloud-sync.spec

echo.
echo 빌드 완료: dist\ncloud-sync.exe
pause
