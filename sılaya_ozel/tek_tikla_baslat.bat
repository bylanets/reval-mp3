@echo off
title Reval MP3 & Ngrok Baslatici
cd /d "%~dp0"

echo ============================================================
echo   ? REVAL MP3 SUNUCUSU VE NGROK BAÞLATILIYOR ?
echo ============================================================

echo [1/3] Web Sunucusu Aciliyor...
start "Reval Flask Sunucu" cmd /k "cd /d ""%~dp0"" && python app.py"

timeout /t 3 /nobreak >nul

set "NGROK_EXE=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
if not exist "%NGROK_EXE%" (
    set "NGROK_EXE=ngrok"
)

echo [2/3] Ngrok Internet Tuneli Aciliyor...
start "Ngrok Tuneli" cmd /k ""%NGROK_EXE%" http 5000 --url https://affront-buccaneer-nurture.ngrok-free.dev"

timeout /t 3 /nobreak >nul

echo [3/3] Siteniz Tarayicida Aciliyor...
start https://affront-buccaneer-nurture.ngrok-free.dev

echo.
echo ============================================================
echo   BASARIYLA BASLATILDI!
echo   Acilan iki kucuk pencereyi kapatmayin (simge durumuna kucultun).
echo ============================================================
timeout /t 5 >nul