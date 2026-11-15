@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.10+ и включи "Add to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск: создаю окружение и ставлю зависимости...
    python -m venv .venv
    if errorlevel 1 (
        echo Не удалось создать .venv
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo Не удалось установить зависимости. Проверь интернет и запусти снова.
    pause
    exit /b 1
)

echo Запускаю бота. Остановить: Ctrl+C
".venv\Scripts\python.exe" bot.py
echo.
echo Бот остановлен.
pause
