@echo off
setlocal

cd /d "%~dp0"

set "OUTPUT_ROOT=G:\RenderdocDiffTools"
:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="-OutputRoot" (
  if not "%~2"=="" (
    set "OUTPUT_ROOT=%~2"
    shift
  )
)
shift
goto parse_args
:args_done

set "PORTABLE_DIR=%OUTPUT_ROOT%\RenderdocDiffPortable"
set "PORTABLE_EXE=%PORTABLE_DIR%\RenderdocDiffTools.exe"

echo [RenderdocDiffTools] Building portable package...
powershell -ExecutionPolicy Bypass -File "%~dp0build_portable.ps1" %*
if errorlevel 1 (
  echo [RenderdocDiffTools] Portable build failed.
  exit /b %errorlevel%
)

if exist "%PORTABLE_EXE%" (
  echo [RenderdocDiffTools] Portable build completed.
  echo [RenderdocDiffTools] Output: "%PORTABLE_DIR%"
  start "" explorer "%PORTABLE_DIR%"
) else (
  echo [RenderdocDiffTools] Build command finished, but portable package was not found.
  echo [RenderdocDiffTools] Expected: "%PORTABLE_EXE%"
  exit /b 1
)

endlocal
