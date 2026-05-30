@echo off
REM VOSBall local web UI launcher (Windows).
REM Double-click this file, or run it from a terminal, to start the eval browser.
REM It opens in your default web browser automatically.
py -m streamlit run "%~dp0webapp\app.py"
pause
