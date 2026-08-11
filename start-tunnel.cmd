@echo off
chcp 65001 >nul
title Туннель к терминалу

rem Открывает доступ к терминалу с телефона через Cloudflare Tunnel.
rem Терминал должен быть уже запущен: сначала start-terminal.cmd, потом этот файл.
rem
rem cloudflared.exe положите рядом с этим файлом. Скачать:
rem https://github.com/cloudflare/cloudflared/releases/latest
rem нужен cloudflared-windows-amd64.exe — переименуйте в cloudflared.exe

cd /d "%~dp0"

if not exist "cloudflared.exe" (
  echo Не найден cloudflared.exe рядом с этим файлом.
  echo Скачайте cloudflared-windows-amd64.exe со страницы
  echo   https://github.com/cloudflare/cloudflared/releases/latest
  echo переименуйте в cloudflared.exe и положите сюда.
  pause
  exit /b 1
)

echo Поднимаю туннель. Адрес вида https://...trycloudflare.com появится ниже —
echo его и открывайте на телефоне. Пока это окно открыто, адрес работает.
echo.

cloudflared.exe tunnel --url http://127.0.0.1:8000
