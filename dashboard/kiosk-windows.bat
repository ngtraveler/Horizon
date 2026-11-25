@echo off
rem Marine Radar - open the wall in full-screen kiosk mode.
rem For a second monitor change --window-position to the right screen, e.g. 1920,0
setlocal
set "URL=http://127.0.0.1:8899/index.html"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "EDGE64=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

if exist "%CHROME%" (
    start "" "%CHROME%" --kiosk "%URL%" --window-position=0,0 --no-first-run --disable-session-crashed-bubble
    goto :eof
)
if exist "%EDGE64%" (
    start "" "%EDGE64%" --kiosk "%URL%" --window-position=0,0
    goto :eof
)
if exist "%EDGE%" (
    start "" "%EDGE%" --kiosk "%URL%" --window-position=0,0
    goto :eof
)
echo Chrome / Edge not found. Open %URL% manually with any browser.
pause
