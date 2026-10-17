@echo off
rem Starts EDI minimised without opening a browser tab. "EDI, start with Windows" registers this with Task Scheduler.
set JARVIS_NO_BROWSER=1
start "E.D.I." /min cmd /c ""%~dp0run.bat""
