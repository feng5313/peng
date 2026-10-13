@echo off
setlocal EnableExtensions
REM Switch console to UTF-8 so Python/emoji output displays correctly
REM (this file itself is ASCII-only, so parsing is unaffected).
chcp 65001 >nul 2>&1
REM ============================================================
REM  Peng - One-click installer for Windows
REM
REM  Usage:
REM    install.bat                Interactive install (recommended):
REM                               - auto runs uv sync
REM                               - asks whether to install peng service
REM                               - asks whether to install nest client
REM                                 (target host + optional name)
REM                               - auto-installs PostgreSQL if missing
REM    install.bat --with-service Non-interactive: register scheduled-task
REM                               service + auto-install PostgreSQL (UAC prompt)
REM    install.bat --yes          Non-interactive, use defaults:
REM                               no service / no nest / no PostgreSQL (CI friendly)
REM    install.bat --help         Show help
REM
REM  Steps:
REM    1. Detect/install uv (official Astral installer)
REM    2. Detect/install Python 3.12 (uv python install)
REM    3. uv sync - create .venv and install all dependencies
REM    4. Verify installation (import peng + fly --help)
REM    5. Check LLM API key configuration and print guidance
REM    6. Interactive setup:
REM       - peng system service? [Y/n]
REM       - nest client (connect to a remote peng service)? [y/N]
REM         * target host (host or host:port, required)
REM         * nest name (optional, default: auto from hostname)
REM       - PostgreSQL: auto-install if missing and needed
REM         (skipped in pure-nest mode where the database is
REM          bridged via the remote nest)
REM       - ripgrep: auto-install if missing (optional grep accelerator,
REM         non-fatal; via peng.common.ripgrep.ensure_ripgrep: scoop/winget)
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
set "RC=0"
set "INSTALL_SERVICE="
set "INSTALL_NEST="
set "NEST_HOST="
set "NEST_NAME="
set "NEED_PG="
set "PG_SKIP_REASON="

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
echo   -y, --yes            Non-interactive, use defaults
echo                        (no service, no nest, no PostgreSQL deploy)
echo   --with-service       Non-interactive: register scheduled-task service
echo                        and auto-install PostgreSQL (UAC prompt)
echo   --no-banner          Skip banner
echo   -h, --help           Show this help
echo.
echo Interactive mode (default) asks:
echo   1. Install peng system service? [Y/n]
echo   2. Install nest client connecting to a remote peng service? [y/N]
echo      (asks for target host and optional nest name, default=hostname)
echo   PostgreSQL is auto-installed when missing, except in pure-nest mode
echo   (no local service + nest installed; database bridged via remote nest).
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

REM ---------- Step 1/6: uv ----------
echo.
echo == Step 1/6: Check uv package manager ==

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

REM ---------- Step 2/6: Python 3.12 ----------
:py_check
echo.
echo == Step 2/6: Check Python 3.12 ==

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

REM ---------- Step 3/6: dependencies ----------
:deps
echo.
echo == Step 3/6: Install dependencies (uv sync) ==

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

REM ---------- Step 4/6: verify ----------
echo.
echo == Step 4/6: Verify installation ==

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

REM ---------- Step 5/6: API keys ----------
echo.
echo == Step 5/6: Check LLM API keys ==

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

REM ---------- Step 6/6: interactive service & nest setup ----------
echo.
echo == Step 6/6: Service and nest setup ==

REM Non-interactive mode: --yes (env only) or --with-service (service + PG)
if "%WITH_SERVICE%"=="1" goto :nonint_service
if "%ASSUME_YES%"=="1" goto :nonint_yes
goto :ask_service
:nonint_yes
set "INSTALL_SERVICE=0"
set "INSTALL_NEST=0"
set "NEED_PG=0"
set "PG_SKIP_REASON=--yes mode installs the environment only"
echo [INFO] Non-interactive mode (--yes): env only, no service/nest/PostgreSQL
goto :pg_decide
:nonint_service
set "INSTALL_SERVICE=1"
set "INSTALL_NEST=0"
echo [INFO] Non-interactive mode (--with-service): installing service + PostgreSQL
goto :pg_decide

:ask_service
set "SVC_ANSWER="
set /p SVC_ANSWER=Install peng system service (auto-start on logon, incl. PostgreSQL)? [Y/n]: 
if not defined SVC_ANSWER set "SVC_ANSWER=Y"
if /i "%SVC_ANSWER%"=="y" (set "INSTALL_SERVICE=1" & goto :ask_nest)
if /i "%SVC_ANSWER%"=="n" (set "INSTALL_SERVICE=0" & goto :ask_nest)
echo [WARN] Please answer y or n
goto :ask_service

:ask_nest
set "NEST_ANSWER="
set /p NEST_ANSWER=Also install nest client (connect this machine to a remote peng service)? [y/N]: 
if not defined NEST_ANSWER set "NEST_ANSWER=N"
if /i "%NEST_ANSWER%"=="y" (set "INSTALL_NEST=1" & goto :nest_ask_host)
if /i "%NEST_ANSWER%"=="n" (set "INSTALL_NEST=0" & goto :pg_decide)
echo [WARN] Please answer y or n
goto :ask_nest

:nest_ask_host
set "NEST_HOST="
set /p NEST_HOST=Remote peng service address (host or host:port, required):
if defined NEST_HOST goto :nest_ask_name
echo [WARN] Host is required
goto :nest_ask_host

:nest_ask_name
set "NEST_NAME="
set /p NEST_NAME=Nest instance name (press Enter = auto from hostname):

REM ---------- PostgreSQL decision ----------
:pg_decide
REM Pure-nest mode (no local service + nest installed): the database is
REM bridged via the remote nest, local PostgreSQL is not required.
if defined NEED_PG goto :pg_got
set "NEED_PG=1"
if "%INSTALL_SERVICE%"=="0" if "%INSTALL_NEST%"=="1" (
    set "NEED_PG=0"
    set "PG_SKIP_REASON=pure-nest mode bridges the database via the remote nest"
)
:pg_got
if "%NEED_PG%"=="1" goto :pg_check
echo [INFO] Local PostgreSQL skipped: %PG_SKIP_REASON%
goto :rg_check

:pg_check
echo.
echo == Check PostgreSQL ==
echo [INFO] Checking local PostgreSQL...
"%VENV_PYTHON%" -X utf8 -c "import sys; from peng.db.postgres_auto_deploy import check_postgresql_available; sys.exit(0 if check_postgresql_available() else 1)" >nul 2>&1
if not errorlevel 1 (
    echo [ OK ] PostgreSQL is ready
    goto :svc_install
)
echo [INFO] PostgreSQL not available, auto-installing via scoop/winget...
echo        ^(this downloads PostgreSQL, may take several minutes; no admin rights needed^)
"%VENV_PYTHON%" -X utf8 -c "import sys; from peng.db.postgres_auto_deploy import ensure_postgresql_fly; sys.exit(0 if ensure_postgresql_fly() else 1)"
if not errorlevel 1 (
    echo [ OK ] PostgreSQL installed and ready
) else (
    echo [WARN] PostgreSQL auto-install failed. Install it manually or re-run install.bat
    set "RC=1"
)

REM ---------- Check ripgrep ----------
REM Optional search accelerator for the grep tool (auto-install, non-fatal).
:rg_check
echo.
echo == Check ripgrep ==
echo [INFO] Checking ripgrep (optional, speeds up the grep tool)...
"%VENV_PYTHON%" -X utf8 -c "import sys; from peng.common.ripgrep import ensure_ripgrep; sys.exit(0 if ensure_ripgrep() else 1)"
if not errorlevel 1 (
    echo [ OK ] ripgrep is ready
) else (
    echo [WARN] ripgrep auto-install failed; grep tool falls back to built-in Python search ^(non-fatal^)
)

REM ---------- Install peng system service ----------
:svc_install
if "%INSTALL_SERVICE%"=="1" goto :svc_do
goto :nest_install

:svc_do
echo.
echo == Install peng system service ==
echo [INFO] Running fly install ^(a UAC elevation prompt may appear^)...
"%VENV_PYTHON%" "%PENG_ROOT%\bin\fly" install
if errorlevel 1 (
    echo [FAIL] fly install failed, run it manually later: bin\fly install
    set "RC=1"
) else (
    echo [ OK ] Service registered. Check status with: bin\fly status
)

REM ---------- Install nest client ----------
:nest_install
if "%INSTALL_NEST%"=="1" goto :nest_do
goto :done

:nest_do
echo.
echo == Install nest service ==
if defined NEST_NAME goto :nest_do_named
echo [INFO] Running: bin\nest install --host %NEST_HOST% ^(name: auto from hostname^)
"%VENV_PYTHON%" "%PENG_ROOT%\bin\nest" install --host "%NEST_HOST%"
goto :nest_check

:nest_do_named
echo [INFO] Running: bin\nest install --host %NEST_HOST% --name %NEST_NAME%
"%VENV_PYTHON%" "%PENG_ROOT%\bin\nest" install --host "%NEST_HOST%" --name "%NEST_NAME%"

:nest_check
if errorlevel 1 (
    echo [FAIL] nest install failed, run it manually later:
    echo        bin\nest install --host %NEST_HOST%
    set "RC=1"
) else (
    echo [ OK ] nest service registered and started
)

REM ---------- Done ----------
:done
echo.
if "%RC%"=="0" (
    echo [ OK ] Peng installed successfully!
) else (
    echo [WARN] Peng install finished with warnings/errors, review the log above
)
echo.
echo Setup summary:
if "%INSTALL_SERVICE%"=="1" (
    echo   - peng service : installed ^(scheduled task, auto-start on logon^)
) else (
    echo   - peng service : not installed ^(run bin\fly install anytime^)
)
if "%INSTALL_NEST%"=="1" (
    echo   - nest client  : installed, connecting to %NEST_HOST%
) else (
    echo   - nest client  : not installed ^(run bin\nest install --host ^<host^> anytime^)
)
echo.
echo Next steps:
echo   Interactive chat : %PENG_ROOT%\bin\fly
echo   Foreground+WebUI : %PENG_ROOT%\bin\fly service --webui    (WebUI: http://localhost:6688)
echo   Service status   : %PENG_ROOT%\bin\fly status
echo   Nest status      : %PENG_ROOT%\bin\nest status
echo   Help             : %PENG_ROOT%\bin\fly --help
echo.
echo License: free for personal/non-commercial use; commercial use requires
echo a commercial license, see LICENSE and README.
endlocal
exit /b %RC%
