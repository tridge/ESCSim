@echo off
rem launch the AM32 SITL GUI using the environment created by
rem "py SITL\make_gui_env.py"
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
    echo GUI environment not found, run:  py SITL\make_gui_env.py
    pause
    exit /b 1
)
start "" "venv\Scripts\pythonw.exe" "sitl_gui.py" %*
