@echo off
REM Abre la app de pronosticos MotoTrack con doble clic (Windows).
REM Ir a la carpeta donde esta este archivo, sin importar desde donde se ejecute.
cd /d "%~dp0"

REM Verificar que Python este instalado.
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: No se encontro Python. Instalalo desde https://www.python.org
    echo y marca la casilla "Add Python to PATH" durante la instalacion.
    pause
    exit /b 1
)

REM Primera vez: crear el entorno virtual.
if not exist ".venv\Scripts\activate.bat" (
    echo Preparando el entorno por primera vez, esto puede tardar unos minutos...
    python -m venv .venv
    if errorlevel 1 goto error
)

call .venv\Scripts\activate.bat

REM Instalar dependencias si aun no se instalaron correctamente.
REM La marca "instalado.ok" solo se crea si la instalacion termino bien.
if not exist ".venv\instalado.ok" (
    echo Instalando dependencias...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    if errorlevel 1 goto error
    echo ok> ".venv\instalado.ok"
)

echo Iniciando la aplicacion... (cierra esta ventana para detenerla)
streamlit run app.py
if errorlevel 1 goto error
pause
exit /b 0

:error
echo.
echo Ocurrio un error. Lee el mensaje de arriba.
pause
exit /b 1
