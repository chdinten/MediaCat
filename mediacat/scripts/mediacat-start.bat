@echo off
rem MediaCat launcher for Windows: makes sure WSL is running (starts it if
rem not), then shows the command that starts MediaCat and the address to
rem open, and offers to run both. Double-click it or run it from cmd.
rem Edit the four values below if the distro, path or address change.
rem
rem To customize this bat file, change the "set" variables, to reflect your
rem environment and path settings accordingly.
setlocal EnableExtensions
title MediaCat launcher

set "DISTRO=Ubuntu-24.04"
set "PROJ_DIR=/mnt/c/MEGA/SoundDB/mediacat"
set "APP_URL=https://mediacat.localhost:8443/"
set "START_CMD=wsl -d %DISTRO% --cd %PROJ_DIR% -- bash scripts/mediacat-ctl.sh --run 1"

rem wsl.exe prints UTF-16 unless told otherwise; findstr needs UTF-8
set "WSL_UTF8=1"

echo.
echo  MediaCat launcher
echo  =================
echo.

where wsl >nul 2>&1
if errorlevel 1 (
    echo  [!] WSL is not installed. See MediaCat_Installation_Guide.md, section 1.
    goto :end
)

wsl -l --running -q 2>nul | findstr /i /b /c:"%DISTRO%" >nul
if not errorlevel 1 (
    echo  [OK] WSL is running ^(%DISTRO%^).
    goto :docker
)

echo  [..] WSL ^(%DISTRO%^) is not running - starting it...
rem dbus-launch leaves a small daemon behind, which keeps the distro (and
rem with it Docker and the MediaCat containers) running after this window
rem closes. A plain "wsl -e true" would let WSL shut down again when idle.
wsl -d %DISTRO% --exec dbus-launch true >nul 2>&1
if errorlevel 1 (
    echo  [!] Could not start %DISTRO%. Check "wsl -l -v" for the distro name.
    goto :end
)
echo  [OK] WSL started.

:docker
rem Docker starts with systemd; give it up to 30 seconds after a cold start
set /a TRIES=0
:wait_docker
wsl -d %DISTRO% -e systemctl is-active --quiet docker >nul 2>&1
if not errorlevel 1 goto :status
set /a TRIES+=1
if %TRIES% geq 15 (
    echo  [!] Docker is not running inside WSL. Try: wsl -d %DISTRO% -e sudo systemctl start docker
    goto :commands
)
if %TRIES% equ 1 echo  [..] Waiting for Docker...
timeout /t 2 /nobreak >nul
goto :wait_docker

:status
echo  [OK] Docker is running.
set "APP_STATUS="
for /f "delims=" %%S in ('wsl -d %DISTRO% -e docker ps --filter "name=mediacat-app-1" --format "{{.Status}}" 2^>nul') do set "APP_STATUS=%%S"
rem No ( ) block here: the status text itself contains ")" ("Up 5 min (healthy)")
if not defined APP_STATUS echo  [--] MediaCat is not running yet.
if defined APP_STATUS echo  [OK] MediaCat is already running: %APP_STATUS%

:commands
echo.
echo  Start MediaCat:   %START_CMD%
echo  Open in browser:  %APP_URL%
echo.

if defined APP_STATUS goto :ask_browser
choice /c YN /n /m " Start MediaCat now? [Y/N] "
if errorlevel 2 goto :end
echo.
%START_CMD%
if errorlevel 1 (
    echo.
    echo  [!] Starting MediaCat failed - see the output above.
    goto :end
)
echo.

:ask_browser
choice /c YN /n /m " Open MediaCat in the browser? [Y/N] "
if errorlevel 2 goto :end
start "" "%APP_URL%"

:end
echo.
pause
endlocal
