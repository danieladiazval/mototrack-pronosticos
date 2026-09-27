# Motor de Pronósticos MotoTrack

App en Streamlit que carga el histórico de demanda de MotoTrack, compara 6 modelos de
pronóstico con backtesting y muestra el mejor modelo por serie, su pronóstico y gráficas.

## Requisito previo

Tener **Python 3.9 o superior** instalado ([python.org](https://www.python.org)).
En Windows, durante la instalación marca la casilla **"Add Python to PATH"**.

## Forma más fácil: doble clic

| Sistema | Archivo | Cómo abrirlo |
|---------|---------|--------------|
| Windows | `iniciar_app.bat` | Doble clic |
| macOS   | `iniciar_app.command` | Doble clic |

- La **primera vez** crea el entorno virtual e instala las dependencias (tarda unos minutos).
- Las **siguientes veces** solo levanta la app, que se abre en el navegador.
- Para detener la app, cierra la ventana negra (terminal).

**Notas para macOS:**
- Si al hacer doble clic aparece *"no se puede abrir porque proviene de un desarrollador
  no identificado"*: clic derecho sobre el archivo → **Abrir** → **Abrir**. Solo se pide la primera vez.
- Si el archivo perdió el permiso de ejecución (por ejemplo, al copiarlo desde un .zip),
  ejecuta una vez en la Terminal: `chmod +x iniciar_app.command`

**No copies la carpeta `.venv` entre Windows y Mac:** cada sistema necesita la suya.
Si pasas el proyecto de un computador a otro, borra `.venv` y el lanzador la vuelve a crear.

## Forma manual (terminal)

**Windows:**
```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

**macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Uso

1. Sube el archivo `moto-track.xlsx` (hoja `Demand`).
2. Ajusta `n_windows` (ventanas de backtesting, por defecto 6) y `h` (periodos a pronosticar, por defecto 6).
3. Presiona **Generar pronóstico**.
4. Revisa la tabla resumen y la gráfica, y descarga el resumen en Excel.

Cada turno puedes volver a subir el Excel actualizado y generar el pronóstico de nuevo.
