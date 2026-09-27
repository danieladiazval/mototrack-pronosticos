#!/bin/bash
# Abre la app de pronosticos MotoTrack con doble clic (macOS).
# Ir a la carpeta donde esta este archivo, sin importar desde donde se ejecute.
cd "$(dirname "$0")" || exit 1

# Si algo falla, dejar la ventana abierta para poder leer el mensaje.
error() {
    echo ""
    echo "Ocurrio un error. Lee el mensaje de arriba."
    read -r -p "Presiona Enter para cerrar..."
    exit 1
}

# Verificar que Python 3 este instalado.
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: No se encontro Python 3. Instalalo desde https://www.python.org"
    error
fi

# Primera vez: crear el entorno virtual.
if [ ! -f ".venv/bin/activate" ]; then
    echo "Preparando el entorno por primera vez, esto puede tardar unos minutos..."
    python3 -m venv .venv || error
fi

source .venv/bin/activate || error

# Instalar dependencias si aun no se instalaron correctamente.
# La marca "instalado.ok" solo se crea si la instalacion termino bien.
if [ ! -f ".venv/instalado.ok" ]; then
    echo "Instalando dependencias..."
    python -m pip install --upgrade pip
    pip install -r requirements.txt || error
    echo ok > ".venv/instalado.ok"
fi

echo "Iniciando la aplicacion... (cierra esta ventana o presiona Ctrl+C para detenerla)"
streamlit run app.py || error
