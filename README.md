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

1. Sube el archivo de demanda `moto-track.xlsx` (hoja `Demand`). Se descarga turno a turno.
2. Sube el Excel de **costos y precios por producto** (solo cuando cambie): la app lo guarda
   en la carpeta `datos_guardados/` y lo vuelve a usar en las siguientes sesiones.
   Debe tener una fila de encabezados con `Producto`, una columna de costo (p. ej.
   `Costo de fabricación ($/u)`) y una de precio (p. ej. `Precio de venta ($/u)`).
3. Ajusta `n_windows` (por defecto 6), `h` (por defecto 6) y la ventana móvil (por defecto 52 turnos = 1 año).
4. Presiona **Generar pronóstico**.
5. Revisa las pestañas:
   - **Clasificación ABC-XYZ**: los 8 SKUs (producto × tienda), con utilidad, clase ABC,
     Score%, RMSE y clase XYZ, más el Pareto y la matriz ABC-XYZ.
   - **Consolidado por producto**: MOTO, CUATRIMOTO y TRACTOR sumando las tiendas.
   - **Pronóstico**: tabla del mejor modelo por serie y gráfica de demanda vs. pronóstico.
6. Descarga el **reporte completo en Excel** (clasificación, consolidado, RMSE, pronóstico y parámetros).

Los umbrales de clasificación son editables y se aplican al instante:

| Clase | Regla (por defecto) |
|-------|---------------------|
| A / B / C | % acumulado de utilidad: A ≤ 80 %, B ≤ 95 %, C el resto |
| X / Y / Z | Score% del mejor modelo: X ≤ 25 %, Y ≤ 60 %, Z > 60 % |

Cada turno puedes volver a subir el Excel de demanda actualizado: la ventana móvil toma los
últimos 52 turnos (entra la semana más reciente y sale la más antigua) y todo se recalcula.
