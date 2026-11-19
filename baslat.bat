@echo off
title Reval MP3 Donusturucu
chcp 65001 >nul
cd /d "%USERPROFILE%\Documents\sýlaya_ozel"
echo ============================================================
echo   ? REVAL MP3 DÖNÜÞTÜRÜCÜ BAÞLATILIYOR ?
echo   Tarayýcýnýz açýlýyor: http://127.0.0.1:5000
echo ============================================================
start http://127.0.0.1:5000
python app.py
pause