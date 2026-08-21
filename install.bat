@echo off
setlocal EnableExtensions
REM ============================================================
REM  Peng - One-click installer for Windows
REM
REM  Usage:
REM    install.bat                Interactive install (recommended)
REM    install.bat --with-service Register scheduled-task service after install (UAC prompt)
REM    install.bat --yes          Non-interactive, use defaults (CI friendly)
REM    install.bat --help         Show help
REM
REM  Steps:
REM    1. Detect/install uv (official Astral installer)
REM    2. Detect/install Python 3.12 (uv python install)
REM    3. uv sync - create .venv and install all dependencies
REM    4. Verify installation (import peng + fly --help)
REM    5. Check LLM API key configuration and print guidance
REM    6. Optional: register auto-start service (fly install, incl. PostgreSQL)
REM
REM  NOTE: Keep this file ASCII-only. cmd.exe parses batch files using the
REM  active OEM code page (e.g. GBK/CP936) before chcp takes effect, so
REM  non-ASCII characters in labels/echo lines break parsing.
REM
REM  Idempotent: safe to re-run, existing components are skipped.
REM ============================================================

set "PENG_ROOT=%~dp0"
if "%PENG_ROOT:~-1%"=="\" set "PENG_ROOT=%PENG_ROOT:~0,-1%"

set "ASSUME_YES=0"
set "WITH_SERVICE=0"

REM ---------- Argument parsing ----------
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="-y"             ( set "ASSUME_YES=1" & shift & goto :parse_args )
if /i "%~1"=="--yes"          ( set "ASSUME_YES=1" & shift & goto :parse_args )
if /i "%~1"=="--with-service" ( set "WITH_SERVICE=1" & shift & goto :parse_args )
if /i "%~1"=="--no-banner"    ( shift & goto :parse_args )
if /i "%~1"=="-h"             ( goto :usage )
if /i "%~1"=="--help"         ( goto :usage )
echo [FAIL] Unknown argument: %~1
goto :usage

:usage
echo Peng one-click installer (Windows)
echo.
echo Usage: install.bat [options]
echo.
echo Options:
echo   -y, --yes            Non-interactive, use defaults (no service registration)
echo   --with-service       Register scheduled-task service and start it (UAC prompt)
echo   --no-banner          Skip banner
echo   -h, --help           Show this help
echo.
echo Environment variables:
echo   PENG_SKIP_UV_INSTALL=1   Skip uv auto-install (use system uv)
echo   PENG_SKIP_PY_INSTALL=1   Skip Python 3.12 auto-install (use system 3.12)
exit /b 0

:args_done

echo.
echo == Peng installer ==
echo [INFO] Project root: %PENG_ROOT%

REM ---------- Preflight ----------
if not exist "%PENG_ROOT%\pyproject.toml" (
    echo [FAIL] pyproject.toml not found in project root.
    echo        Clone the repository first, then run install.bat inside it:
    echo            git clone ^<repo-url^> peng ^&^& cd peng ^&^& install.bat
    exit /b 1
)

REM ---------- Step 1/5: uv ----------
echo.
echo == Step 1/5: Check uv package manager ==

set "UV_CMD="
where uv >nul 2>&1 && set "UV_CMD=uv"
if not defined UV_BIN if not defined UV_CMD (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "UV_CMD=%USERPROFILE%\.local\bin\uv.exe"
        set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    )
)
if not defined UV_CMD (
    if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" (
        set "UV_CMD=%LOCALAPPDATA%\Programs\uv\uv.exe"
        set "PATH=%LOCALAPPDATA%\Programs\uv;%PATH%"
    )
)

if defined UV_CMD (
    echo [ OK ] uv is installed
    goto :py_check
)
if "%PENG_SKIP_UV_INSTALL%"=="1" (
    echo [FAIL] PENG_SKIP_UV_INSTALL=1 but uv is not installed
    exit /b 1
)

echo [INFO] uv not found, installing via official installer...
where powershell >nul 2>&1
if errorlevel 1 (
    echo [FAIL] powershell not found, cannot auto-install uv.
    echo        Install manually: https://docs.astral.sh/uv/getting-started/installation/
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo [FAIL] uv installation failed. Install manually:
    echo        https://docs.astral.sh/uv/getting-started/installation/
    exit /b 1
)
if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "UV_CMD=%USERPROFILE%\.local\bin\uv.exe"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
) else if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" (
    set "UV_CMD=%LOCALAPPDATA%\Programs\uv\uv.exe"
    set "PATH=%LOCALAPPDATA%\Programs\uv;%PATH%"
)
if not defined UV_CMD (
    echo [FAIL] uv.exe still not found after install. Reopen a terminal and retry.
    exit /b 1
)
echo [ OK ] uv installed

REM ---------- Step 2/5: Python 3.12 ----------
:py_check
echo.
echo == Step 2/5: Check Python 3.12 ==

set "PY_OK=0"
where py >nul 2>&1 && (
    py -3.12 -c "import sys" >nul 2>&1 && set "PY_OK=1"
)
%UV_CMD% python find 3.12 >nul 2>&1 && set "PY_OK=1"

if "%PY_OK%"=="1" (
    echo [ OK ] Python 3.12 is ready
    goto :deps
)
if "%PENG_SKIP_PY_INSTALL%"=="1" (
    echo [FAIL] PENG_SKIP_PY_INSTALL=1 but Python 3.12 not found (requires ^>=3.12,^<3.13)
    exit /b 1
)

echo [INFO] Python 3.12 not found, installing via uv...
%UV_CMD% python install 3.12
if errorlevel 1 (
    echo [FAIL] Python 3.12 installation failed
    exit /b 1
)
echo [ OK ] Python 3.12 installed

REM ---------- Step 3/5: dependencies ----------
:deps
echo.
echo == Step 3/5: Install dependencies (uv sync) ==

cd /d "%PENG_ROOT%"
%UV_CMD% sync --python 3.12
if errorlevel 1 (
    echo [FAIL] Dependency installation failed, check the log above
    exit /b 1
)
echo [ OK ] Dependencies installed

set "VENV_PYTHON=%PENG_ROOT%\.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo [FAIL] Virtualenv creation failed: %VENV_PYTHON% not found
    exit /b 1
)

REM ---------- Step 4/5: verify ----------
echo.
echo == Step 4/5: Verify installation ==

"%VENV_PYTHON%" -c "import peng" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Cannot import peng package, check the install log above
    exit /b 1
)
echo [ OK ] peng package imports fine

"%VENV_PYTHON%" "%PENG_ROOT%\bin\fly" --help >nul 2>&1
if errorlevel 1 (
    echo [WARN] fly --help returned non-zero, verify manually later
) else (
    echo [ OK ] fly command works
)

REM ---------- Step 5/5: API keys ----------
echo.
echo == Step 5/5: Check LLM API keys ==

set "HAS_KEY=0"
if defined ZHIPU_AI_API_KEY  ( echo [ OK ] ZHIPU_AI_API_KEY is set   & set "HAS_KEY=1" )
if defined OPENAI_API_KEY    ( echo [ OK ] OPENAI_API_KEY is set     & set "HAS_KEY=1" )
if defined DASHSCOPE_API_KEY ( echo [ OK ] DASHSCOPE_API_KEY is set  & set "HAS_KEY=1" )
if defined DEEPSEEK_API_KEY  ( echo [ OK ] DEEPSEEK_API_KEY is set   & set "HAS_KEY=1" )

if "%HAS_KEY%"=="0" (
    echo [WARN] No LLM API key detected
    echo.
    echo   Configure one of the following:
    echo     1. User environment variable ^(recommended^): set
    echo          ZHIPU_AI_API_KEY = your-api-key     ^(Zhipu AI^)
    echo          OPENAI_API_KEY   = your-api-key     ^(OpenAI-compatible endpoint^)
    echo        Reopen the terminal after setting.
    echo     2. Config file: edit the providers section of config\peng.json,
    echo        fill api_key / base_url / model directly.
    echo.
)

REM ---------- Optional: system service ----------
if "%WITH_SERVICE%"=="1" (
    echo.
    echo == Register scheduled-task service ==
    echo [INFO] Running fly install ^(auto-deploys PostgreSQL and registers the
    echo        service; a UAC elevation prompt may appear^)...
    "%VENV_PYTHON%" "%PENG_ROOT%\bin\fly" install
    if errorlevel 1 (
        echo [FAIL] fly install failed, run it manually later: bin\fly install
        exit /b 1
    )
    echo [ OK ] Service registered. Check with: fly status
)

REM ---------- Done ----------
echo.
echo [ OK ] Peng installed successfully!
echo.
echo Next steps:
echo   Interactive chat : %PENG_ROOT%\bin\fly
echo   Foreground+WebUI : %PENG_ROOT%\bin\fly service --webui    (WebUI: http://localhost:6688)
echo   System service   : %PENG_ROOT%\bin\fly install             (scheduled task + PostgreSQL)
echo   Service status   : %PENG_ROOT%\bin\fly status
echo   Help             : %PENG_ROOT%\bin\fly --help
echo.
echo License: free for personal/non-commercial use; commercial use requires
echo a commercial license, see LICENSE and README.
endlocal
exit /b 0
