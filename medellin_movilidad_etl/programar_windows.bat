@echo off
REM Crea las tareas programadas del ETL (ejecutar como el usuario que correrá el ETL)
set DIR=%~dp0
set PY=%DIR%.venv\Scripts\python.exe

schtasks /Create /F /TN "ETL Movilidad Medellin - completo" /SC WEEKLY /D MON /ST 06:15 ^
  /TR "cmd /c cd /d \"%DIR%\" && \"%PY%\" -m etl run >> \"%DIR%data\etl.log\" 2>&1"

schtasks /Create /F /TN "ETL Movilidad Medellin - camaras" /SC MINUTE /MO 5 /ST 06:00 /ET 22:00 ^
  /TR "cmd /c cd /d \"%DIR%\" && \"%PY%\" -m etl camaras --rondas 1 >> \"%DIR%data\camaras.log\" 2>&1"

echo Tareas creadas. Revisa en el Programador de tareas de Windows.
