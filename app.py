# =============================================================================
# Motor de Pronósticos MotoTrack
# -----------------------------------------------------------------------------
# App de Streamlit que:
#   1. Lee el histórico de demanda (hoja "Demand" del Excel de MotoTrack).
#   2. Compara varios modelos de pronóstico con backtesting (validación cruzada).
#   3. Elige el mejor modelo por serie (menor Score%) y pronostica h periodos.
#   4. Muestra una tabla resumen, una gráfica y permite descargar el resumen.
#
# Para correrla:  streamlit run app.py
# =============================================================================

import io
import re

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
    return fig


def resumen_a_excel(resumen):
    """Convierte la tabla resumen en un archivo Excel en memoria (bytes)."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        resumen.to_excel(writer, sheet_name="Resumen", index=False)
    return buffer.getvalue()


# -----------------------------------------------------------------------------
# 4. Interfaz de Streamlit
# -----------------------------------------------------------------------------

st.set_page_config(page_title="Pronósticos MotoTrack", layout="wide")
st.title("Motor de Pronósticos MotoTrack")
st.write(
    "Sube el Excel de MotoTrack, ajusta los parámetros y presiona **Generar pronóstico**. "
    "La app compara 6 modelos con backtesting y elige el mejor para cada serie."
)

archivo = st.file_uploader("Archivo de demanda (.xlsx)", type=["xlsx"])

# Si se sube un archivo distinto, borramos los resultados anteriores
# para no mostrar un pronóstico que no corresponde a los datos actuales.
if archivo is not None:
    firma_archivo = (archivo.name, archivo.size)
    if st.session_state.get("firma_archivo") != firma_archivo:
        st.session_state["firma_archivo"] = firma_archivo
        st.session_state.pop("resultados", None)

col1, col2 = st.columns(2)
n_windows = col1.number_input(
    "n_windows (ventanas de backtesting)", min_value=1, max_value=52, value=6, step=1,
)
h = col2.number_input(
    "h (periodos a pronosticar)", min_value=1, max_value=52, value=6, step=1,
)

if st.button("Generar pronóstico", type="primary", disabled=archivo is None):
    # --- Lectura del Excel: si algo falla, mensaje claro y nos detenemos ---
    try:
        df = leer_demanda(archivo)
        df = agregar_regionales_calculadas(df)
    except ValueError as error:
        # ValueError incluye el caso "no existe la hoja Demand".
        st.error(f"No se pudo leer el Excel: {error}")
        st.stop()
    except Exception as error:
        st.error(f"El archivo no parece un Excel válido de MotoTrack. Detalle: {error}")
        st.stop()

    # --- Backtesting, métricas y pronóstico ---
    df_ts = preparar_para_statsforecast(df)
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
        "metricas": metricas,
        "resumen": resumen,
        "fc_mejor": fc_mejor,
        "fallos": fallos,
        "h": int(h),
    }

# --- Mostrar resultados (si ya existen) ---
res = st.session_state.get("resultados")
if res is not None:
    st.subheader("Resumen: mejor modelo por serie")
    st.dataframe(res["resumen"], hide_index=True, use_container_width=True)

    st.download_button(
        "Descargar resumen en Excel",
        data=resumen_a_excel(res["resumen"]),
        file_name="resumen_pronostico_mototrack.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.subheader("Demanda real y pronóstico")
    st.plotly_chart(crear_grafica(res["df"], res["fc_mejor"], res["h"]), use_container_width=True)

    # Detalle opcional: todas las métricas de todos los modelos.
    with st.expander("Ver comparación de todos los modelos"):
        detalle = res["metricas"].copy()
        detalle[["REGIONAL", "PRODUCTO"]] = detalle["unique_id"].str.split("|", expand=True)
        for c in ["MAE_PORC", "SESGO_PORC", "SCORE_PORC"]:
            detalle[c] = (detalle[c] * 100).round(1)
        detalle["RMSE"] = detalle["RMSE"].round(2)
        st.dataframe(
            detalle[["REGIONAL", "PRODUCTO", "MODELO", "MAE_PORC", "SESGO_PORC", "SCORE_PORC", "RMSE"]],
            hide_index=True, use_container_width=True,
        )

    if res["fallos"]:
        with st.expander(f"⚠️ {len(res['fallos'])} combinaciones serie/modelo fallaron y se omitieron"):
            st.dataframe(pd.DataFrame(res["fallos"]), hide_index=True, use_container_width=True)
elif archivo is None:
    st.info("Sube el archivo .xlsx para comenzar.")
