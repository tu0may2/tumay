@echo off
chcp 65001 >nul
title Казначейский терминал

rem Запуск терминала с включённым входом по паролю.
rem Слушаем только 127.0.0.1: наружу пускает туннель, а порт в локальной сети
rem открывать незачем — чем меньше открыто, тем меньше поводов волноваться.

cd /d "%~dp0backend"

if not exist ".venv" (
  echo Создаю окружение Python...
  python -m venv .venv || goto :error
)

call ".venv\Scripts\activate.bat" || goto :error

echo Проверяю зависимости...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :error

rem Вход обязателен: терминал будет доступен из интернета
set TREASURY_AUTH_ENABLED=true

rem Пароль администратора. Задайте свой и не оставляйте значение по умолчанию.
rem Если убрать строку, пароль сгенерируется и напечатается здесь один раз.
if "%TREASURY_ADMIN_PASSWORD%"=="" set TREASURY_ADMIN_PASSWORD=smenite-etot-parol

echo.
echo ============================================================
echo  Терминал: http://127.0.0.1:8000
echo  Логин: admin    Пароль: %TREASURY_ADMIN_PASSWORD%
echo  Остановить — Ctrl+C
echo ============================================================
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof

:error
echo.
echo Не удалось запустить. Проверьте, что Python установлен: python --version
pause
