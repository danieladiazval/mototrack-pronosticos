# =============================================================================
# Motor de Pronósticos MotoTrack
# -----------------------------------------------------------------------------
# App de Streamlit que:
#   1. Lee el histórico de demanda (hoja "Demand" del Excel de MotoTrack).
#   2. Compara varios modelos de pronóstico con backtesting (validación cruzada).
#   3. Elige el mejor modelo por serie (menor Score%) y pronostica h periodos.
#   4. Muestra una tabla resumen, una gráfica y permite descargar el resumen.
#   5. Clasifica los 8 SKUs (producto en cada tienda) en ABC según la utilidad
#      del último año y en XYZ según el Score% del pronóstico.
#
# Para correrla:  streamlit run app.py
# =============================================================================

import io
import re
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from statsforecast import StatsForecast
from statsforecast.models import (
    HoltWinters,
    WindowAverage,
    SimpleExponentialSmoothingOptimized,
    MSTL,
)

# -----------------------------------------------------------------------------
# Constantes generales
# -----------------------------------------------------------------------------

# Nombres de las regionales en el orden en que aparecen los bloques del Excel
# (Norte = columnas A-D, Centro = F-I, Sur = K-M).
REGIONALES_EXCEL = ["NORTE", "CENTRO", "SUR"]

# Orden en que queremos mostrar regionales y productos en tablas y gráficas.
ORDEN_REGIONALES = ["NORTE", "CENTRO", "SUR", "CEDI", "MOTOTRAK"]
ORDEN_PRODUCTOS = ["MOTO", "CUATRIMOTO", "TRACTOR"]

# Colores de cada producto en la gráfica.
COLORES = {"MOTO": "salmon", "CUATRIMOTO": "navy", "TRACTOR": "darkcyan"}

# Posición (fila, columna) de cada regional dentro de la cuadrícula 2x3.
POSICION_SUBPLOT = {
    "NORTE": (1, 1),
    "CENTRO": (1, 2),
    "SUR": (1, 3),
    "CEDI": (2, 1),
    "MOTOTRAK": (2, 2),
}

# Fecha ficticia desde la que arrancan los turnos. Es un lunes, para que
# coincida con la frecuencia semanal "W-MON" que usa StatsForecast.
FECHA_BASE = pd.Timestamp("2020-01-06")
FRECUENCIA = "W-MON"

# --- Clasificación ABC-XYZ ---
# Los SKUs son cada producto en cada tienda (NORTE, CENTRO, SUR): 3 + 3 + 2 = 8.
# CEDI y MOTOTRAK son sumas, así que NO entran en la clasificación.
REGIONALES_SKU = ["NORTE", "CENTRO", "SUR"]

# Ventana móvil: un año = 52 turnos (semanas).
VENTANA_DEFECTO = 52

# El Excel de costos y precios se guarda en esta carpeta (al lado de app.py)
# para que siga disponible la próxima vez que se abra la app.
CARPETA_GUARDADO = Path(__file__).parent / "datos_guardados"
ARCHIVO_COSTOS_GUARDADO = CARPETA_GUARDADO / "costos_precios.xlsx"

# Colores de cada clase ABC en las gráficas.
COLORES_ABC = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}


# -----------------------------------------------------------------------------
# 1. Lectura y transformación de los datos
# -----------------------------------------------------------------------------

def leer_demanda(archivo):
    """Lee la hoja 'Demand' y la devuelve en formato largo:
    Turn | REGIONAL | PRODUCTO | DEMANDA
    """
    # header=1 -> la fila 2 del Excel tiene los encabezados reales
    # (la fila 1 solo trae los nombres decorativos de las tiendas).
    crudo = pd.read_excel(archivo, sheet_name="Demand", header=1)

    # Cuando hay encabezados repetidos, pandas les agrega un sufijo:
    # "Turn", "Turn.1", "Turn.2"... Aquí se lo quitamos para saber
    # qué representa cada columna.
    nombres = [re.sub(r"\.\d+$", "", str(c)).strip().upper() for c in crudo.columns]

    # Recorremos las columnas de izquierda a derecha armando los bloques.
    # Un bloque empieza en cada columna "TURN" y termina en la siguiente
    # columna vacía ("UNNAMED: ...") o en el siguiente "TURN".
    bloques = []  # lista de listas con las posiciones de columna de cada bloque
    for i, nombre in enumerate(nombres):
        if nombre == "TURN":
            bloques.append([i])
        elif nombre.startswith("UNNAMED"):
            continue  # columna separadora vacía
        elif bloques:
            bloques[-1].append(i)

    if len(bloques) != len(REGIONALES_EXCEL):
        raise ValueError(
            f"Se esperaban {len(REGIONALES_EXCEL)} bloques (Norte, Centro, Sur) "
            f"con una columna 'Turn', pero se encontraron {len(bloques)}."
        )

    # Pasamos cada bloque a formato largo y los unimos.
    partes = []
    for regional, columnas in zip(REGIONALES_EXCEL, bloques):
        bloque = crudo.iloc[:, columnas].copy()
        # Ponemos los nombres limpios (sin sufijo) a las columnas del bloque.
        bloque.columns = [nombres[i] for i in columnas]

        # "melt" convierte las columnas de productos en filas.
        # No asumimos qué productos hay: tomamos los que existan en el bloque.
        largo = bloque.melt(id_vars="TURN", var_name="PRODUCTO", value_name="DEMANDA")
        largo["REGIONAL"] = regional
        partes.append(largo)

    df = pd.concat(partes, ignore_index=True)

    # Convertimos a número y descartamos filas vacías o con texto.
    df["TURN"] = pd.to_numeric(df["TURN"], errors="coerce")
    df["DEMANDA"] = pd.to_numeric(df["DEMANDA"], errors="coerce")
    df = df.dropna(subset=["TURN", "DEMANDA"])
    df["TURN"] = df["TURN"].astype(int)

    if df.empty:
        raise ValueError("La hoja 'Demand' no tiene datos numéricos de demanda.")

    df = df.rename(columns={"TURN": "Turn"})
    return df[["Turn", "REGIONAL", "PRODUCTO", "DEMANDA"]]


def agregar_regionales_calculadas(df):
    """Agrega CEDI (= CENTRO + SUR) y MOTOTRAK (= NORTE + CENTRO + SUR)."""
    cedi = (
        df[df["REGIONAL"].isin(["CENTRO", "SUR"])]
        .groupby(["Turn", "PRODUCTO"], as_index=False)["DEMANDA"].sum()
        .assign(REGIONAL="CEDI")
    )
    mototrak = (
        df[df["REGIONAL"].isin(["NORTE", "CENTRO", "SUR"])]
        .groupby(["Turn", "PRODUCTO"], as_index=False)["DEMANDA"].sum()
        .assign(REGIONAL="MOTOTRAK")
    )
    return pd.concat([df, cedi, mototrak], ignore_index=True)


# Conversión Turn <-> fecha ficticia. StatsForecast trabaja con fechas,
# así que cada turno se vuelve una semana a partir de FECHA_BASE.
def turn_a_fecha(turn):
    return FECHA_BASE + pd.to_timedelta((turn - 1) * 7, unit="D")


def fecha_a_turn(fecha):
    return ((fecha - FECHA_BASE).dt.days // 7 + 1).astype(int)


def preparar_para_statsforecast(df):
    """Arma el DataFrame que espera StatsForecast: unique_id | ds | y"""
    df_ts = pd.DataFrame({
        "unique_id": df["REGIONAL"] + "|" + df["PRODUCTO"],
        "ds": turn_a_fecha(df["Turn"]),
        "y": df["DEMANDA"].astype(float),
    })
    return df_ts.sort_values(["unique_id", "ds"]).reset_index(drop=True)


# -----------------------------------------------------------------------------
# 2. Modelos, backtesting y pronóstico
# -----------------------------------------------------------------------------

def crear_modelos():
    """Lista de modelos a comparar (se crea nueva cada vez que se usa)."""
    return [
        HoltWinters(season_length=13, alias="hw"),
        WindowAverage(window_size=3, alias="wa_3"),
        WindowAverage(window_size=6, alias="wa_6"),
        WindowAverage(window_size=12, alias="wa_12"),
        SimpleExponentialSmoothingOptimized(alias="ses"),
        MSTL(season_length=13, alias="mstl"),
    ]


def correr_un_modelo(modelo, df_ts, h, n_windows):
    """Corre backtesting + pronóstico de UN modelo sobre las series dadas.
    Devuelve (cv, fc) en formato largo con columnas unique_id, ds, MODELO, PRED
    (y además 'y' en el caso de cv).
    """
    # n_jobs=1: sin procesos paralelos. Es un poco más lento pero evita
    # problemas de multiprocesamiento dentro de Streamlit (sobre todo en Windows).
    sf = StatsForecast(models=[modelo], freq=FRECUENCIA, n_jobs=1)

    # Backtesting: se "esconde" el final de la serie en n_windows ventanas de
    # h periodos, se pronostica cada ventana y se compara contra lo real.
    cv = sf.cross_validation(df=df_ts, h=h, n_windows=n_windows, step_size=h)

    # Pronóstico real hacia adelante usando toda la historia.
    fc = sf.forecast(df=df_ts, h=h)

    # Pasamos la columna del modelo (su alias) a una columna genérica "PRED".
    cv = cv.rename(columns={modelo.alias: "PRED"}).assign(MODELO=modelo.alias)
    fc = fc.rename(columns={modelo.alias: "PRED"}).assign(MODELO=modelo.alias)
    return (
        cv[["unique_id", "ds", "cutoff", "y", "MODELO", "PRED"]],
        fc[["unique_id", "ds", "MODELO", "PRED"]],
    )


def backtesting_y_pronostico(df_ts, h, n_windows, barra=None):
    """Corre todos los modelos. Si un modelo falla, lo reintenta serie por
    serie para que el error solo afecte a la serie problemática.
    Devuelve (cv_total, fc_total, lista_de_fallos).
    """
    todos_cv, todos_fc, fallos = [], [], []
    modelos = crear_modelos()

    for n, modelo in enumerate(modelos, start=1):
        if barra is not None:
            barra.progress(n / len(modelos), text=f"Evaluando modelo '{modelo.alias}' ({n}/{len(modelos)})...")
        try:
            # Primer intento: todas las series juntas (más rápido).
            cv, fc = correr_un_modelo(modelo, df_ts, h, n_windows)
            todos_cv.append(cv)
            todos_fc.append(fc)
        except Exception:
            # Segundo intento: serie por serie, saltando las que fallen.
            for uid, serie in df_ts.groupby("unique_id"):
                try:
                    cv, fc = correr_un_modelo(modelo, serie, h, n_windows)
                    todos_cv.append(cv)
                    todos_fc.append(fc)
                except Exception as error:
                    fallos.append({"SERIE": uid, "MODELO": modelo.alias, "ERROR": str(error)})

    if not todos_cv:
        raise RuntimeError("Ningún modelo pudo ejecutarse. Revisa que las series tengan suficientes datos.")

    return pd.concat(todos_cv, ignore_index=True), pd.concat(todos_fc, ignore_index=True), fallos


def calcular_metricas(cv):
    """Calcula MAE%, Sesgo%, Score% y RMSE para cada serie y modelo."""
    cv = cv.dropna(subset=["PRED"]).copy()
    cv["ERROR"] = cv["y"] - cv["PRED"]

    def metricas_de_grupo(g):
        suma_y = g["y"].sum()
        # Si la demanda total es 0, los porcentajes no se pueden calcular.
        if suma_y == 0:
            mae_p = sesgo_p = np.nan
        else:
            mae_p = g["ERROR"].abs().sum() / suma_y      # MAE%   = Σ|y − pred| / Σy
            sesgo_p = g["ERROR"].sum() / suma_y          # Sesgo% = Σ(y − pred) / Σy
        return pd.Series({
            "MAE_PORC": mae_p,
            "SESGO_PORC": sesgo_p,
            "SCORE_PORC": mae_p + abs(sesgo_p),          # Score% = MAE% + |Sesgo%|
            "RMSE": np.sqrt((g["ERROR"] ** 2).mean()),   # RMSE   = sqrt(mean(error²))
        })

    metricas = (
        cv.groupby(["unique_id", "MODELO"])[["y", "ERROR"]]
        .apply(metricas_de_grupo)
        .reset_index()
    )
    return metricas


def elegir_mejor_modelo(metricas):
    """Para cada serie, se queda con el modelo de menor Score%."""
    validas = metricas.dropna(subset=["SCORE_PORC"])
    indices = validas.groupby("unique_id")["SCORE_PORC"].idxmin()
    return validas.loc[indices].reset_index(drop=True)


def armar_resumen(mejores, fc):
    """Tabla final: REGIONAL | PRODUCTO | MODELO | SCORE_PORC | RMSE | Turn_xxx..."""
    # Nos quedamos solo con el pronóstico del mejor modelo de cada serie.
    fc_mejor = fc.merge(mejores[["unique_id", "MODELO"]], on=["unique_id", "MODELO"])
    fc_mejor["Turn"] = fecha_a_turn(fc_mejor["ds"])

    # Una columna por cada turno pronosticado.
    ancho = fc_mejor.pivot(index="unique_id", columns="Turn", values="PRED")
    ancho.columns = [f"Turn_{t}" for t in ancho.columns]
    ancho = ancho.round(1).reset_index()

    resumen = mejores.merge(ancho, on="unique_id")
    resumen[["REGIONAL", "PRODUCTO"]] = resumen["unique_id"].str.split("|", expand=True)
    resumen["SCORE_PORC"] = (resumen["SCORE_PORC"] * 100).round(1)  # a porcentaje
    resumen["RMSE"] = resumen["RMSE"].round(2)

    # Ordenamos las filas como en el Excel: primero por regional, luego producto.
    resumen["REGIONAL"] = pd.Categorical(resumen["REGIONAL"], ORDEN_REGIONALES, ordered=True)
    resumen["PRODUCTO"] = pd.Categorical(resumen["PRODUCTO"], ORDEN_PRODUCTOS, ordered=True)
    resumen = resumen.sort_values(["REGIONAL", "PRODUCTO"]).reset_index(drop=True)
    resumen["REGIONAL"] = resumen["REGIONAL"].astype(str)
    resumen["PRODUCTO"] = resumen["PRODUCTO"].astype(str)

    columnas_turnos = [c for c in resumen.columns if c.startswith("Turn_")]
    return resumen[["REGIONAL", "PRODUCTO", "MODELO", "SCORE_PORC", "RMSE"] + columnas_turnos], fc_mejor


# -----------------------------------------------------------------------------
# 3. Gráfica y descarga
# -----------------------------------------------------------------------------

def fondo_blanco(fig):
    """Pone la gráfica en tono blanco: fondo blanco y rejilla gris suave
    (se aplica encima de la plantilla ggplot2, que trae fondo gris)."""
    fig.update_layout(paper_bgcolor="white", plot_bgcolor="white")
    fig.update_xaxes(gridcolor="#e6e6e6", linecolor="#bdbdbd", zerolinecolor="#e6e6e6")
    fig.update_yaxes(gridcolor="#e6e6e6", linecolor="#bdbdbd", zerolinecolor="#e6e6e6")
    return fig


def crear_grafica(df, fc_mejor, h):
    """Subplots 2x3: una regional por subplot, una línea por producto."""
    fig = make_subplots(
        rows=2, cols=3,
        subplot_titles=["NORTE", "CENTRO", "SUR", "CEDI (C+S)", "MOTOTRAK (N+C+S)", ""],
    )

    # Solo mostramos los últimos 52 + h turnos de historia.
    ultimo_turn = df["Turn"].max()
    historia = df[df["Turn"] > ultimo_turn - (52 + h)]

    productos_en_leyenda = set()  # para que cada producto salga una sola vez en la leyenda

    for regional, (fila, col) in POSICION_SUBPLOT.items():
        for producto in ORDEN_PRODUCTOS:
            real = historia[(historia["REGIONAL"] == regional) & (historia["PRODUCTO"] == producto)]
            if real.empty:
                continue  # por ejemplo, SUR no tiene TRACTOR

            mostrar_leyenda = producto not in productos_en_leyenda
            productos_en_leyenda.add(producto)
            color = COLORES.get(producto, "gray")

            # Línea sólida: demanda real.
            fig.add_trace(
                go.Scatter(
                    x=real["Turn"], y=real["DEMANDA"], mode="lines",
                    name=producto, legendgroup=producto, showlegend=mostrar_leyenda,
                    line=dict(color=color),
                ),
                row=fila, col=col,
            )

            # Línea punteada: pronóstico del mejor modelo.
            pron = fc_mejor[fc_mejor["unique_id"] == f"{regional}|{producto}"].sort_values("Turn")
            if not pron.empty:
                # Anteponemos el último punto real para que las dos líneas queden unidas.
                ultimo = real.sort_values("Turn").iloc[-1]
                x = [ultimo["Turn"]] + pron["Turn"].tolist()
                y = [ultimo["DEMANDA"]] + pron["PRED"].tolist()
                fig.add_trace(
                    go.Scatter(
                        x=x, y=y, mode="lines",
                        name=f"{producto} (pronóstico)", legendgroup=producto, showlegend=False,
                        line=dict(color=color, dash="dot"),
                    ),
                    row=fila, col=col,
                )

    fig.update_layout(template="ggplot2", height=700)
    fig.update_xaxes(title_text="Turn")
    fig.update_yaxes(title_text="Demanda")
    return fondo_blanco(fig)




def tablas_a_excel(tablas):
    """Convierte un diccionario {nombre_hoja: DataFrame} en un Excel en memoria (bytes).
    Cada DataFrame queda en su propia hoja.
    """
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for nombre_hoja, tabla in tablas.items():
            tabla.to_excel(writer, sheet_name=nombre_hoja[:31], index=False)  # Excel admite máx. 31 caracteres
    return buffer.getvalue()


# -----------------------------------------------------------------------------
# 4. Costos y precios por producto
# -----------------------------------------------------------------------------

def leer_costos_precios(archivo):
    """Lee el Excel de costos y precios y devuelve: PRODUCTO | COSTO_UNIT | PRECIO_UNIT

    No exige un formato rígido: busca en todas las hojas una fila de encabezados
    que tenga una columna "Producto", una columna con "Costo" y otra con "Precio".
    Así el usuario puede subir su propio Excel, siempre que tenga esas columnas.
    """
    libro = pd.read_excel(archivo, sheet_name=None, header=None)
    solo_faltaba_precio = False

    for hoja in libro.values():
        for fila in range(len(hoja)):
            encabezados = [str(v).strip().upper() for v in hoja.iloc[fila]]
            if "PRODUCTO" not in encabezados:
                continue

            col_producto = encabezados.index("PRODUCTO")
            cols_costo = [i for i, e in enumerate(encabezados) if "COSTO" in e]
            cols_precio = [i for i, e in enumerate(encabezados) if "PRECIO" in e]
            if not cols_costo:
                continue
            if not cols_precio:
                solo_faltaba_precio = True
                continue

            # Si hay varias columnas de costo, preferimos la del costo total
            # ("Costo de fabricación", "Costo total" o "Costo unitario").
            preferidas = [i for i in cols_costo if any(p in encabezados[i] for p in ("FABRIC", "TOTAL", "UNIT"))]
            col_costo = (preferidas or cols_costo)[0]
            col_precio = cols_precio[0]

            # Leemos las filas de abajo hasta la primera con el producto vacío.
            filas = []
            for _, registro in hoja.iloc[fila + 1:].iterrows():
                producto = registro.iloc[col_producto]
                if pd.isna(producto) or str(producto).strip() == "":
                    break
                filas.append({
                    "PRODUCTO": str(producto).strip().upper(),
                    "COSTO_UNIT": pd.to_numeric(registro.iloc[col_costo], errors="coerce"),
                    "PRECIO_UNIT": pd.to_numeric(registro.iloc[col_precio], errors="coerce"),
                })

            costos = pd.DataFrame(filas)
            if costos.empty:
                raise ValueError("La tabla de costos y precios no tiene productos debajo del encabezado.")
            incompletos = costos[costos[["COSTO_UNIT", "PRECIO_UNIT"]].isna().any(axis=1)]
            if not incompletos.empty:
                raise ValueError(
                    "Faltan costos o precios (o no son números) para: "
                    + ", ".join(incompletos["PRODUCTO"]) + "."
                )
            return costos

    if solo_faltaba_precio:
        raise ValueError(
            "Se encontró la tabla de costos, pero no tiene precio de venta. Agrega una columna "
            "llamada 'Precio de venta ($/u)' al lado del costo."
        )
    raise ValueError(
        "No se encontró una tabla con columnas 'Producto', 'Costo...' y 'Precio...'. "
        "Revisa que el Excel tenga una fila de encabezados con esas tres columnas."
    )


def cargar_costos(archivo_subido):
    """Decide qué archivo de costos usar y lo lee.
    - Si se subió uno nuevo y se lee bien, se guarda en disco (reemplaza al anterior).
    - Si no se subió nada, se usa el último guardado (si existe).
    Devuelve (costos, mensaje, es_error).
    """
    if archivo_subido is not None:
        try:
            costos = leer_costos_precios(archivo_subido)
        except Exception as error:
            return None, f"No se pudo leer el Excel de costos y precios: {error}", True
        # Solo guardamos cuando el archivo es válido, para no perder el anterior.
        CARPETA_GUARDADO.mkdir(exist_ok=True)
        ARCHIVO_COSTOS_GUARDADO.write_bytes(archivo_subido.getvalue())
        return costos, f"Usando el archivo recién subido ({archivo_subido.name}). Quedó guardado para próximas sesiones.", False

    if ARCHIVO_COSTOS_GUARDADO.exists():
        try:
            costos = leer_costos_precios(ARCHIVO_COSTOS_GUARDADO)
        except Exception as error:
            return None, f"El archivo de costos guardado no se pudo leer: {error}. Sube uno nuevo.", True
        fecha = pd.Timestamp(ARCHIVO_COSTOS_GUARDADO.stat().st_mtime, unit="s").strftime("%Y-%m-%d %H:%M")
        return costos, f"Usando el archivo de costos guardado (actualizado el {fecha}).", False

    return None, "Aún no hay archivo de costos y precios. Súbelo para poder clasificar ABC.", True


# -----------------------------------------------------------------------------
# 5. Clasificación ABC-XYZ
# -----------------------------------------------------------------------------

def clasificar_abc_xyz(df_ventana, mejores, costos, corte_a, corte_b, limite_x, limite_y):
    """Clasifica los 8 SKUs (producto en cada tienda).

    ABC: por utilidad del último año = demanda × (precio − costo).
         Se ordenan de mayor a menor utilidad y se acumula el % de la utilidad total.
         A: % acumulado <= corte_a | B: <= corte_b | C: el resto.
         (El SKU de mayor utilidad siempre es A, aunque por sí solo pase del corte.)
    XYZ: por el Score% del mejor modelo del motor de pronósticos
         (no por coeficiente de variación).
         X: Score% <= limite_x | Y: <= limite_y | Z: mayor que limite_y.
    """
    # Demanda del último año de cada SKU (solo tiendas reales, no CEDI ni MOTOTRAK).
    demanda = (
        df_ventana[df_ventana["REGIONAL"].isin(REGIONALES_SKU)]
        .groupby(["REGIONAL", "PRODUCTO"], as_index=False)["DEMANDA"].sum()
    )

    faltantes = sorted(set(demanda["PRODUCTO"]) - set(costos["PRODUCTO"]))
    if faltantes:
        raise ValueError("El Excel de costos no tiene precio/costo para: " + ", ".join(faltantes) + ".")

    tabla = demanda.merge(costos, on="PRODUCTO")
    tabla["SKU"] = tabla["PRODUCTO"] + " " + tabla["REGIONAL"]
    tabla["UTILIDAD_UNIT"] = tabla["PRECIO_UNIT"] - tabla["COSTO_UNIT"]
    tabla["VENTAS"] = tabla["DEMANDA"] * tabla["PRECIO_UNIT"]
    tabla["UTILIDAD"] = tabla["DEMANDA"] * tabla["UTILIDAD_UNIT"]

    utilidad_total = tabla["UTILIDAD"].sum()
    if utilidad_total <= 0:
        raise ValueError("La utilidad total es cero o negativa: revisa que los precios sean mayores que los costos.")

    # --- ABC ---
    tabla = tabla.sort_values("UTILIDAD", ascending=False).reset_index(drop=True)
    tabla["PORC_UTILIDAD"] = tabla["UTILIDAD"] / utilidad_total * 100
    tabla["PORC_ACUMULADO"] = tabla["PORC_UTILIDAD"].cumsum()
    tabla["ABC"] = np.select(
        [tabla["PORC_ACUMULADO"] <= corte_a, tabla["PORC_ACUMULADO"] <= corte_b],
        ["A", "B"],
        default="C",
    )
    tabla.loc[0, "ABC"] = "A"

    # --- XYZ (Score% y RMSE salen del motor de pronósticos) ---
    motor = mejores.copy()
    motor[["REGIONAL", "PRODUCTO"]] = motor["unique_id"].str.split("|", expand=True)
    motor["SCORE_PORC"] = motor["SCORE_PORC"] * 100
    tabla = tabla.merge(motor[["REGIONAL", "PRODUCTO", "MODELO", "SCORE_PORC", "RMSE"]],
                        on=["REGIONAL", "PRODUCTO"], how="left")
    tabla["XYZ"] = np.select(
        [tabla["SCORE_PORC"] <= limite_x, tabla["SCORE_PORC"] <= limite_y, tabla["SCORE_PORC"] > limite_y],
        ["X", "Y", "Z"],
        default="SIN DATO",  # la serie no tuvo ningún modelo válido
    )
    tabla["ABC_XYZ"] = tabla["ABC"] + tabla["XYZ"].where(tabla["XYZ"] != "SIN DATO", "?")

    # Redondeos para mostrar.
    for c in ["PORC_UTILIDAD", "PORC_ACUMULADO", "SCORE_PORC"]:
        tabla[c] = tabla[c].round(1)
    tabla["RMSE"] = tabla["RMSE"].round(2)

    return tabla[[
        "SKU", "REGIONAL", "PRODUCTO", "DEMANDA", "PRECIO_UNIT", "COSTO_UNIT", "UTILIDAD_UNIT",
        "VENTAS", "UTILIDAD", "PORC_UTILIDAD", "PORC_ACUMULADO", "ABC",
        "MODELO", "SCORE_PORC", "RMSE", "XYZ", "ABC_XYZ",
    ]]


def consolidar_por_producto(clasificacion, mejores, limite_x, limite_y):
    """Suma los SKUs por tipo de producto (MOTO, CUATRIMOTO, TRACTOR).
    El Score% y el RMSE salen de la serie MOTOTRAK|PRODUCTO, que es justamente
    la demanda consolidada de las tres tiendas.
    """
    consolidado = (
        clasificacion.groupby("PRODUCTO", as_index=False)
        .agg(TIENDAS=("REGIONAL", "count"), DEMANDA=("DEMANDA", "sum"),
             VENTAS=("VENTAS", "sum"), UTILIDAD=("UTILIDAD", "sum"))
    )
    consolidado["PORC_UTILIDAD"] = (consolidado["UTILIDAD"] / consolidado["UTILIDAD"].sum() * 100).round(1)
    consolidado["MARGEN_PORC"] = (consolidado["UTILIDAD"] / consolidado["VENTAS"] * 100).round(1)

    motor = mejores[mejores["unique_id"].str.startswith("MOTOTRAK|")].copy()
    motor["PRODUCTO"] = motor["unique_id"].str.split("|").str[1]
    motor["SCORE_PORC"] = (motor["SCORE_PORC"] * 100).round(1)
    motor["RMSE"] = motor["RMSE"].round(2)
    consolidado = consolidado.merge(motor[["PRODUCTO", "MODELO", "SCORE_PORC", "RMSE"]], on="PRODUCTO", how="left")
    consolidado["XYZ"] = np.select(
        [consolidado["SCORE_PORC"] <= limite_x, consolidado["SCORE_PORC"] <= limite_y, consolidado["SCORE_PORC"] > limite_y],
        ["X", "Y", "Z"],
        default="SIN DATO",
    )

    consolidado["PRODUCTO"] = pd.Categorical(consolidado["PRODUCTO"], ORDEN_PRODUCTOS, ordered=True)
    consolidado = consolidado.sort_values("PRODUCTO").reset_index(drop=True)
    consolidado["PRODUCTO"] = consolidado["PRODUCTO"].astype(str)
    return consolidado


def grafica_pareto(clasificacion):
    """Barras de utilidad por SKU (de mayor a menor), coloreadas por clase ABC.
    El % acumulado va escrito sobre cada barra (un solo eje Y)."""
    fig = go.Figure()
    for clase in ["A", "B", "C"]:
        parte = clasificacion[clasificacion["ABC"] == clase]
        if parte.empty:
            continue
        fig.add_trace(go.Bar(
            x=parte["SKU"], y=parte["UTILIDAD"] / 1e6, name=f"Clase {clase}",  # en millones de $
            marker_color=COLORES_ABC[clase],
            text=[f"{p:.0f}% acum." for p in parte["PORC_ACUMULADO"]], textposition="outside",
            customdata=parte[["PORC_UTILIDAD", "PORC_ACUMULADO"]],
            hovertemplate="%{x}<br>Utilidad: $%{y:,.0f} millones<br>% de la utilidad: %{customdata[0]:.1f}%"
                          "<br>% acumulado: %{customdata[1]:.1f}%<extra></extra>",
        ))
    fig.update_layout(
        template="ggplot2", height=450, title="Pareto de utilidad (último año)",
        xaxis=dict(title="SKU", categoryorder="array", categoryarray=clasificacion["SKU"].tolist()),
        # 15 % de espacio sobre la barra más alta para que su etiqueta no se corte.
        yaxis=dict(title="Utilidad (millones de $)", range=[0, clasificacion["UTILIDAD"].max() / 1e6 * 1.15]),
    )
    return fondo_blanco(fig)


def grafica_matriz(clasificacion):
    """Matriz ABC-XYZ de 3x3: columnas C-B-A (como en la teoría) y filas X-Y-Z.
    Cada celda lista los SKUs que caen en ella; el tono indica cuántos hay."""
    columnas, filas = ["C", "B", "A"], ["X", "Y", "Z"]
    conteo, textos = [], []
    for xyz in filas:
        fila_conteo, fila_texto = [], []
        for abc in columnas:
            skus = clasificacion.loc[(clasificacion["ABC"] == abc) & (clasificacion["XYZ"] == xyz), "SKU"].tolist()
            fila_conteo.append(len(skus))
            fila_texto.append(f"<b>{abc}{xyz}</b><br>" + "<br>".join(skus) if skus else f"{abc}{xyz}")
        conteo.append(fila_conteo)
        textos.append(fila_texto)

    fig = go.Figure(go.Heatmap(
        z=conteo, x=columnas, y=filas, text=textos, texttemplate="%{text}",
        colorscale=[[0, "#f4f4f2"], [1, "#6fa3e3"]], showscale=False,
        xgap=3, ygap=3, hovertemplate="%{text}<extra></extra>",
    ))
    fig.update_layout(
        template="ggplot2", height=450, title="Matriz ABC-XYZ",
        xaxis=dict(title="ABC: utilidad (A = mayor)"),
        yaxis=dict(title="XYZ: error de pronóstico (Z = mayor)"),
    )
    return fondo_blanco(fig)


def grafica_consolidado(consolidado):
    """Dos barras lado a lado (una por medida, sin doble eje): utilidad y demanda por producto."""
    fig = make_subplots(rows=1, cols=2, subplot_titles=["Utilidad último año (millones de $)", "Demanda último año (unidades)"])
    colores = [COLORES.get(p, "gray") for p in consolidado["PRODUCTO"]]
    fig.add_trace(go.Bar(
        x=consolidado["PRODUCTO"], y=consolidado["UTILIDAD"] / 1e6, marker_color=colores, showlegend=False,
        text=[f"{p:.0f}%" for p in consolidado["PORC_UTILIDAD"]], textposition="outside",
        hovertemplate="%{x}<br>Utilidad: $%{y:,.0f} millones<extra></extra>",
    ), row=1, col=1)
    fig.add_trace(go.Bar(
        x=consolidado["PRODUCTO"], y=consolidado["DEMANDA"], marker_color=colores, showlegend=False,
        hovertemplate="%{x}<br>Demanda: %{y:,.0f}<extra></extra>",
    ), row=1, col=2)
    # 15 % de espacio sobre la barra más alta para que su etiqueta no se corte.
    fig.update_yaxes(range=[0, consolidado["UTILIDAD"].max() / 1e6 * 1.15], row=1, col=1)
    fig.update_yaxes(range=[0, consolidado["DEMANDA"].max() * 1.15], row=1, col=2)
    fig.update_layout(template="ggplot2", height=400)
    return fondo_blanco(fig)


# -----------------------------------------------------------------------------
# 6. Interfaz de Streamlit
# -----------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Pronósticos MotoTrack", layout="wide")
    st.title("Motor de Pronósticos y Clasificación ABC-XYZ — MotoTrack")
    st.write(
        "Sube el Excel de demanda (y el de costos y precios si cambió), ajusta los parámetros "
        "y presiona **Generar pronóstico**. La app compara 6 modelos con backtesting, elige el "
        "mejor para cada serie y clasifica los 8 SKUs en ABC-XYZ."
    )

    # --- 1. Archivos de entrada ---
    st.subheader("1. Archivos")
    col_dem, col_cos = st.columns(2)
    archivo = col_dem.file_uploader("Demanda de MotoTrack (.xlsx, hoja 'Demand')", type=["xlsx"])
    archivo_costos = col_cos.file_uploader(
        "Costos y precios por producto (.xlsx) — opcional si ya se subió antes", type=["xlsx"],
    )

    costos, mensaje_costos, error_costos = cargar_costos(archivo_costos)
    if error_costos:
        col_cos.warning(mensaje_costos)
    else:
        col_cos.caption(mensaje_costos)

    # Si se sube un archivo de demanda distinto, borramos los resultados anteriores
    # para no mostrar un pronóstico que no corresponde a los datos actuales.
    if archivo is not None:
        firma_archivo = (archivo.name, archivo.size)
        if st.session_state.get("firma_archivo") != firma_archivo:
            st.session_state["firma_archivo"] = firma_archivo
            st.session_state.pop("resultados", None)

    # --- 2. Parámetros del motor ---
    st.subheader("2. Parámetros del motor de pronósticos")
    col1, col2, col3 = st.columns(3)
    n_windows = col1.number_input("n_windows (ventanas de backtesting)", min_value=1, max_value=52, value=6, step=1)
    h = col2.number_input("h (periodos a pronosticar)", min_value=1, max_value=52, value=6, step=1)
    ventana = col3.number_input(
        "Ventana móvil (turnos) para el ABC", min_value=13, max_value=260, value=VENTANA_DEFECTO, step=1,
        help="Un año = 52 turnos. Al llegar un turno nuevo entra la semana más reciente y sale la más antigua.",
    )
    solo_ventana = st.checkbox(
        "Entrenar el motor solo con la ventana móvil",
        value=False,
        help="Por defecto los modelos usan toda la historia (el backtesting igual evalúa los últimos "
             "n_windows × h turnos). Con solo 52 turnos, HoltWinters no alcanza a entrenar y se descarta.",
    )

    # --- 3. Umbrales de clasificación (se aplican al instante, sin recalcular el motor) ---
    st.subheader("3. Umbrales de clasificación")
    u1, u2, u3, u4 = st.columns(4)
    corte_a = u1.number_input("A: hasta este % acumulado de utilidad", 1.0, 99.0, 80.0, 1.0)
    corte_b = u2.number_input("B: hasta este % acumulado de utilidad", 1.0, 100.0, 95.0, 1.0)
    limite_x = u3.number_input("X: Score% menor o igual a", 1.0, 500.0, 25.0, 1.0)
    limite_y = u4.number_input("Y: Score% menor o igual a", 1.0, 500.0, 60.0, 1.0)
    umbrales_ok = corte_a < corte_b and limite_x < limite_y
    if not umbrales_ok:
        st.error("Revisa los umbrales: el corte de A debe ser menor que el de B, y el límite de X menor que el de Y.")

    if st.button("Generar pronóstico", type="primary", disabled=archivo is None):
        # --- Lectura del Excel: si algo falla, mensaje claro y nos detenemos ---
        try:
            df_tiendas = leer_demanda(archivo)
        except ValueError as error:
            # ValueError incluye el caso "no existe la hoja Demand".
            st.error(f"No se pudo leer el Excel: {error}")
            st.stop()
        except Exception as error:
            st.error(f"El archivo no parece un Excel válido de MotoTrack. Detalle: {error}")
            st.stop()

        # Ventana móvil: los últimos `ventana` turnos (entra el más nuevo, sale el más viejo).
        ultimo_turn = df_tiendas["Turn"].max()
        df_ventana = df_tiendas[df_tiendas["Turn"] > ultimo_turn - int(ventana)]

        df = agregar_regionales_calculadas(df_tiendas)
        df_motor = agregar_regionales_calculadas(df_ventana) if solo_ventana else df

        # --- Backtesting, métricas y pronóstico ---
        df_ts = preparar_para_statsforecast(df_motor)
        barra = st.progress(0.0, text="Iniciando...")
        try:
            cv, fc, fallos = backtesting_y_pronostico(df_ts, int(h), int(n_windows), barra)
        except Exception as error:
            barra.empty()
            st.error(f"No se pudo calcular el pronóstico: {error}")
            st.stop()
        barra.empty()

        metricas = calcular_metricas(cv)
        mejores = elegir_mejor_modelo(metricas)
        resumen, fc_mejor = armar_resumen(mejores, fc)

        # Guardamos todo en session_state para que no se pierda cuando
        # el usuario interactúe con la app (por ejemplo, al descargar).
        st.session_state["resultados"] = {
            "df": df,
            "df_ventana": df_ventana,
            "turnos_ventana": (int(df_ventana["Turn"].min()), int(ultimo_turn)),
            "metricas": metricas,
            "mejores": mejores,
            "resumen": resumen,
            "fc_mejor": fc_mejor,
            "fallos": fallos,
            "h": int(h),
            "n_windows": int(n_windows),
            "solo_ventana": solo_ventana,
        }

    # --- Mostrar resultados (si ya existen) ---
    res = st.session_state.get("resultados")
    if res is None:
        if archivo is None:
            st.info("Sube el archivo de demanda para comenzar.")
        return

    turno_ini, turno_fin = res["turnos_ventana"]
    st.success(
        f"Ventana móvil: turnos {turno_ini} a {turno_fin} "
        f"({turno_fin - turno_ini + 1} turnos). "
        + ("El motor se entrenó solo con esta ventana." if res["solo_ventana"] else "El motor se entrenó con toda la historia.")
    )

    # La clasificación se calcula aquí (y no al presionar el botón) para que
    # cambiar los umbrales o el archivo de costos la actualice al instante.
    clasificacion = consolidado = None
    if costos is not None and umbrales_ok:
        try:
            clasificacion = clasificar_abc_xyz(res["df_ventana"], res["mejores"], costos,
                                               corte_a, corte_b, limite_x, limite_y)
            consolidado = consolidar_por_producto(clasificacion, res["mejores"], limite_x, limite_y)
        except Exception as error:
            st.error(f"No se pudo clasificar: {error}")

    # --- Descarga de todas las salidas en un solo Excel ---
    tablas = {}
    if clasificacion is not None:
        tablas["Clasificacion ABC-XYZ"] = clasificacion
        tablas["Consolidado producto"] = consolidado
        tablas["RMSE por SKU"] = clasificacion[["SKU", "REGIONAL", "PRODUCTO", "MODELO", "RMSE"]]
    tablas["Pronostico"] = res["resumen"]
    tablas["Parametros"] = pd.DataFrame({
        "PARAMETRO": ["Turnos de la ventana", "n_windows", "h", "Motor entrenado con",
                      "Corte A (% acum.)", "Corte B (% acum.)", "Límite X (Score%)", "Límite Y (Score%)"],
        "VALOR": [f"{turno_ini} a {turno_fin}", res["n_windows"], res["h"],
                  "Solo la ventana" if res["solo_ventana"] else "Toda la historia",
                  corte_a, corte_b, limite_x, limite_y],
    }).astype(str)
    st.download_button(
        "Descargar reporte completo en Excel", type="primary",
        data=tablas_a_excel(tablas),
        file_name=f"reporte_mototrack_turno_{turno_fin}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    tab_abc, tab_cons, tab_pron = st.tabs(["Clasificación ABC-XYZ", "Consolidado por producto", "Pronóstico"])

    with tab_abc:
        if clasificacion is None:
            st.warning("Para clasificar se necesita un archivo de costos y precios válido y umbrales correctos.")
        else:
            st.dataframe(
                clasificacion, hide_index=True, width="stretch",
                column_config={
                    "PRECIO_UNIT": st.column_config.NumberColumn("PRECIO_UNIT ($)", format="localized"),
                    "COSTO_UNIT": st.column_config.NumberColumn("COSTO_UNIT ($)", format="localized"),
                    "UTILIDAD_UNIT": st.column_config.NumberColumn("UTILIDAD_UNIT ($)", format="localized"),
                    "VENTAS": st.column_config.NumberColumn("VENTAS ($)", format="localized"),
                    "UTILIDAD": st.column_config.NumberColumn("UTILIDAD ($)", format="localized"),
                },
            )
            st.caption(
                "El RMSE de cada SKU (error del mejor modelo en el backtesting) queda disponible "
                "para calcular más adelante el stock de seguridad."
            )
            g1, g2 = st.columns(2)
            g1.plotly_chart(grafica_pareto(clasificacion), theme=None)
            g2.plotly_chart(grafica_matriz(clasificacion), theme=None)

    with tab_cons:
        if consolidado is None:
            st.warning("Para el consolidado se necesita un archivo de costos y precios válido.")
        else:
            st.write("Suma de las tiendas por tipo de producto. El Score% y el RMSE son los de la serie MOTOTRAK del producto.")
            st.dataframe(
                consolidado, hide_index=True, width="stretch",
                column_config={
                    "VENTAS": st.column_config.NumberColumn("VENTAS ($)", format="localized"),
                    "UTILIDAD": st.column_config.NumberColumn("UTILIDAD ($)", format="localized"),
                },
            )
            st.plotly_chart(grafica_consolidado(consolidado), theme=None)

    with tab_pron:
        st.subheader("Resumen: mejor modelo por serie")
        st.dataframe(res["resumen"], hide_index=True, width="stretch")
        st.download_button(
            "Descargar resumen del pronóstico en Excel",
            data=tablas_a_excel({"Resumen": res["resumen"]}),
            file_name="resumen_pronostico_mototrack.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.subheader("Demanda real y pronóstico")
        st.plotly_chart(crear_grafica(res["df"], res["fc_mejor"], res["h"]), theme=None)

        # Detalle opcional: todas las métricas de todos los modelos.
        with st.expander("Ver comparación de todos los modelos"):
            detalle = res["metricas"].copy()
            detalle[["REGIONAL", "PRODUCTO"]] = detalle["unique_id"].str.split("|", expand=True)
            for c in ["MAE_PORC", "SESGO_PORC", "SCORE_PORC"]:
                detalle[c] = (detalle[c] * 100).round(1)
            detalle["RMSE"] = detalle["RMSE"].round(2)
            st.dataframe(
                detalle[["REGIONAL", "PRODUCTO", "MODELO", "MAE_PORC", "SESGO_PORC", "SCORE_PORC", "RMSE"]],
                hide_index=True, width="stretch",
            )

        if res["fallos"]:
            with st.expander(f"⚠️ {len(res['fallos'])} combinaciones serie/modelo fallaron y se omitieron"):
                st.dataframe(pd.DataFrame(res["fallos"]), hide_index=True, width="stretch")


# Streamlit ejecuta este archivo como programa principal.
if __name__ == "__main__":
    main()
