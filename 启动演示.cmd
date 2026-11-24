@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo Please install uv first: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv sync --locked
if errorlevel 1 (
  pause
  exit /b 1
)
echo Open http://127.0.0.1:8765 - demo password: demo-2026
uv run uvicorn app:app --host 127.0.0.1 --port 8765
pause
