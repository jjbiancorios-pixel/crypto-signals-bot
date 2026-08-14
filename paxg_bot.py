"""
paxg_bot.py — Cinturón separado PAXG/BTC (04/08/2026)

Fondeado directo en BTC, separado del capital de v16. Corre en modo
SOMBRA por 30 días: 3 tipos de señal (A/B/C) x 2 niveles de riesgo
(bajo/medio) x 4 objetivos de TP = 24 combinaciones simuladas en
paralelo, sobre el mismo precio real de PAXG/BTC — sin arriesgar BTC
todavía. Al cabo de los 30 días, se elige la mejor combinación con
db.resumen_paxg_simulaciones().

Autocontenido a propósito (no importa main.py) para evitar import
circular, igual que pionex_api.py — duplica algunas funciones chicas
de indicadores/velas en vez de reusar las de main.py.
"""
import requests
import numpy as np
import pandas as pd
from datetime import datetime

import db
import pionex_api

PAR_PAXG = "PAXGBTC"
PAR_BTC = "BTCUSDT"

APALANCAMIENTO = {"medio": 10}  # 14/08 (Opción 3 aplicada): se sacó "bajo" (5x) — generaba más del doble de cierres forzados por intradía que "medio" (n=38 vs n=17), sin mejor resultado promedio que lo compensara.
# 14/08 (Opción 2 aplicada): TP de activación de trailing específico por
# señal, no uno único para las 3. Encontrado con /paxg_intradia: la señal
# C resuelve rápido en cualquier TP (pocos cierres forzados, chicos); A y
# B se atascan mucho en TP3/TP5 (69% de los cierres forzados eran TP3+TP5,
# y B concentraba la mayoría). Se limita A/B a TP1/TP2, se deja C con el
# rango completo.
TP_POR_SENAL = {
    "A": [1.0, 2.0],
    "B": [1.0, 2.0],
    "C": [1.0, 2.0, 3.0, 5.0],
}
STOP_LOSS_PCT = -20.0  # mismo criterio que v16 (calibrado con datos propios más adelante)
MAX_DURACION_HORAS = 20  # "intradía" — se fuerza el cierre si no cerró antes por TP/SL/trailing
# 11/08 — El TP_OBJETIVOS ya no cierra directo: pasa a ser el punto donde
# se ACTIVA el trailing. Una vez activado, cierra si el resultado
# retrocede este % del máximo alcanzado (proporcional, no puntos fijos —
# se adapta igual de bien al TP de 1% que al de 5%).
RETROCESO_TRAILING_PCT = 0.20
# 11/08 — Comisión real de Pionex Futuros (Maker 0.02% / Taker 0.05%,
# confirmado). Estimación conservadora ida+vuelta de 0.10% (mismo criterio
# que en BingX). A nivel de módulo (no local a la función) a propósito,
# para poder verificarla desde afuera con /paxg_version — el 12/08 hubo
# un caso real donde el servidor corría código viejo sin que se notara
# hasta ver "tp" cerrando en fechas posteriores al fix.
COMISION_IDA_VUELTA_PCT = 0.10

BYBIT_TF = {"1h": "60", "4h": "240"}
OKX_TF = {"1h": "1H", "4h": "4H"}
BINANCE_TF = {"1h": "1h", "4h": "4h"}


# ── Velas y precio (autocontenido, mismo patrón que main.py) ──────
def _velas_bybit(par, tf, n):
    url = f"https://api.bybit.com/v5/market/kline?category=spot&symbol={par}&interval={BYBIT_TF.get(tf,'60')}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if data.get("retCode") != 0: raise ValueError("bybit fail")
    rows = data["result"]["list"]
    if not rows or len(rows) < 20: raise ValueError("bybit empty")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
    for c in ["open", "high", "low", "close", "vol"]: df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)


def _velas_binance(par, tf, n):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={par}&interval={BINANCE_TF.get(tf,'1h')}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if not isinstance(data, list) or len(data) < 20: raise ValueError("binance empty")
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "ct", "qav", "trades", "tbbav", "tbqav", "ignore"])
    for c in ["open", "high", "low", "close", "vol"]: df[c] = df[c].astype(float)
    return df


def get_velas(par, tf, n=100):
    for f in (_velas_bybit, _velas_binance):
        try:
            df = f(par, tf, n)
            if df is not None and len(df) >= 20: return df
        except Exception:
            continue
    return None


def get_precio(par):
    try:
        r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={par}", timeout=6)
        data = r.json()
        if data.get("retCode") == 0:
            return float(data["result"]["list"][0]["lastPrice"])
    except Exception:
        pass
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={par}", timeout=6)
        return float(r.json()["price"])
    except Exception:
        return None


# ── Indicadores (mismas fórmulas que main.py, copiadas para no importar) ──
def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return float((100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1])


def calc_bb(s, p=20):
    m = s.rolling(p).mean(); st = s.rolling(p).std()
    up = (m + 2 * st).iloc[-1]; dn = (m - 2 * st).iloc[-1]; mid = m.iloc[-1]
    return {"upper": up, "lower": dn, "mid": mid}


def calc_ema(s, p):
    return float(s.ewm(span=p).mean().iloc[-1])


def calc_adx(df, p=14):
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1 / p, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / p, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / p, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / p, adjust=False).mean()
    return {"adx": float(adx.iloc[-1]), "plus_di": float(plus_di.iloc[-1]), "minus_di": float(minus_di.iloc[-1])}


def calc_estado_btc_simple(df_btc):
    """Versión simplificada (EMA9 vs EMA21) del estado de BTC — sin importar main.py."""
    ema9 = calc_ema(df_btc["close"], 9)
    ema21 = calc_ema(df_btc["close"], 21)
    if ema9 > ema21 * 1.002:
        return "ALCISTA"
    elif ema9 < ema21 * 0.998:
        return "BAJISTA"
    return "LATERAL"


def _tendencia_oro(precio_oro_actual):
    """
    Tendencia de oro propia: no hay velas históricas gratis de XAU/USD
    fáciles de conseguir, así que se arma con nuestra propia serie
    (guardada en paxg_mercado_log en cada ciclo). Compara el spot actual
    contra el promedio de las últimas 20 lecturas guardadas.
    """
    historicos = db.ultimos_precios_oro(20)
    if len(historicos) < 10 or precio_oro_actual is None:
        return "SIN_DATO"  # todavía no hay suficiente historia propia
    promedio = sum(historicos) / len(historicos)
    if precio_oro_actual > promedio * 1.003:
        return "ALCISTA"
    elif precio_oro_actual < promedio * 0.997:
        return "BAJISTA"
    return "LATERAL"


def evaluar_senales(datos_paxg, datos_btc_estado, tendencia_oro):
    """
    Evalúa las 3 señales (A/B/C) y devuelve un dict {tipo: direccion_o_None}.
    """
    senales = {}

    # A) Reversión a la media: Bollinger + RSI + ADX<25
    precio = datos_paxg["precio"]
    if precio <= datos_paxg["bb_lower"] and datos_paxg["rsi"] < 30 and datos_paxg["adx"] < 25:
        senales["A"] = "LARGO"
    elif precio >= datos_paxg["bb_upper"] and datos_paxg["rsi"] > 70 and datos_paxg["adx"] < 25:
        senales["A"] = "CORTO"
    else:
        senales["A"] = None

    # B) Tendencia: cruce EMA9/EMA21 + ADX>25, confirmado por DI+/DI-
    if (datos_paxg["ema9"] > datos_paxg["ema21"] and datos_paxg["adx"] > 25
            and datos_paxg["plus_di"] > datos_paxg["minus_di"]):
        senales["B"] = "LARGO"
    elif (datos_paxg["ema9"] < datos_paxg["ema21"] and datos_paxg["adx"] > 25
            and datos_paxg["minus_di"] > datos_paxg["plus_di"]):
        senales["B"] = "CORTO"
    else:
        senales["B"] = None

    # C) Macro-inversa con BTC + análisis propio de oro (rotación risk-on/risk-off)
    if datos_btc_estado == "BAJISTA" and tendencia_oro == "ALCISTA":
        senales["C"] = "LARGO"   # BTC cae + oro sube -> favorece PAXG/BTC arriba
    elif datos_btc_estado == "ALCISTA" and tendencia_oro == "BAJISTA":
        senales["C"] = "CORTO"
    else:
        senales["C"] = None

    return senales


def abrir_lote(senal_tipo, direccion, precio_entrada):
    """
    14/08: abre las combinaciones de un tipo de señal — ahora 2 para A/B
    (solo TP1/TP2, riesgo medio) y 4 para C (TP1/2/3/5, riesgo medio),
    tras aplicar las Opciones 2 y 3 del análisis de cierres forzados.
    """
    tps = TP_POR_SENAL.get(senal_tipo, [1.0, 2.0])
    for riesgo, apal in APALANCAMIENTO.items():
        for tp in tps:
            combinacion = f"{senal_tipo}_{riesgo}_TP{tp:g}"
            db.abrir_paxg_simulacion(combinacion, senal_tipo, riesgo, apal, tp, direccion, precio_entrada)


def analizar_y_simular():
    """
    Función principal — se llama periódicamente desde main.py. Loguea el
    mercado SIEMPRE, evalúa las 3 señales, abre lotes nuevos si corresponde,
    y sigue el precio de las combinaciones abiertas (cierre por TP, stop-loss,
    o cierre forzado por intradía).
    """
    df_paxg = get_velas(PAR_PAXG, "1h", 100)
    df_btc = get_velas(PAR_BTC, "4h", 100)
    precio_oro = pionex_api.obtener_precio_oro()
    precio_paxg_actual = get_precio(PAR_PAXG)
    precio_btc_actual = get_precio(PAR_BTC)

    if df_paxg is None or precio_paxg_actual is None:
        return  # sin datos de PAXG/BTC no se puede hacer nada este ciclo

    rsi = calc_rsi(df_paxg["close"])
    bb = calc_bb(df_paxg["close"])
    adx_info = calc_adx(df_paxg)
    ema9 = calc_ema(df_paxg["close"], 9)
    ema21 = calc_ema(df_paxg["close"], 21)

    estado_btc = calc_estado_btc_simple(df_btc) if df_btc is not None else "SIN_DATO"
    tendencia_oro = _tendencia_oro(precio_oro)

    # Log de mercado — SIEMPRE, es la base de datos para buscar parámetros
    db.guardar_paxg_mercado_log({
        "precio_paxgbtc": precio_paxg_actual, "precio_btc_usdt": precio_btc_actual, "precio_oro_usd": precio_oro,
        "rsi_paxgbtc": rsi, "adx_paxgbtc": adx_info["adx"], "plus_di": adx_info["plus_di"], "minus_di": adx_info["minus_di"],
        "bb_upper": bb["upper"], "bb_lower": bb["lower"], "bb_mid": bb["mid"],
        "ema9_paxgbtc": ema9, "ema21_paxgbtc": ema21,
        "estado_btc": estado_btc, "rsi_oro": None, "tendencia_oro": tendencia_oro,
    })

    datos_paxg = {"precio": precio_paxg_actual, "rsi": rsi, "bb_lower": bb["lower"], "bb_upper": bb["upper"],
                  "adx": adx_info["adx"], "plus_di": adx_info["plus_di"], "minus_di": adx_info["minus_di"],
                  "ema9": ema9, "ema21": ema21}
    senales = evaluar_senales(datos_paxg, estado_btc, tendencia_oro)

    for tipo, direccion in senales.items():
        if direccion and not db.paxg_hay_combos_abiertas_de(tipo):
            abrir_lote(tipo, direccion, precio_paxg_actual)

    # 11/08 — Comisión real de Pionex Futuros (Maker 0.02% / Taker 0.05%,
    # confirmado). Se usa una estimación conservadora ida+vuelta de 0.10%
    # (mismo criterio que en BingX), aplicada sobre el NOCIONAL — por eso
    # se multiplica por el apalancamiento antes de restarla, igual que el
    # resultado bruto (ambos escalan igual con el apalancamiento).

    # Seguimiento de las combinaciones abiertas
    abiertas = db.paxg_simulaciones_abiertas()
    for combo in abiertas:
        cambio_pct = (precio_paxg_actual - combo["precio_entrada"]) / combo["precio_entrada"] * 100
        es_largo = combo["direccion"] == "LARGO"
        resultado_bruto = cambio_pct * combo["apalancamiento"] * (1 if es_largo else -1)
        comision_aplicada = COMISION_IDA_VUELTA_PCT * combo["apalancamiento"]
        resultado = resultado_bruto - comision_aplicada

        db.actualizar_paxg_simulacion(combo["id"], resultado)

        # 11/08 — Trailing: el TP fijo ya no cierra directo, ahora ACTIVA
        # el trailing. "pico" usa el mejor_resultado_pct ya guardado (de
        # antes de este ciclo) combinado con el resultado de AHORA, para
        # no depender de una segunda consulta a la base.
        pico_actual = resultado
        if combo["mejor_resultado_pct"] is not None:
            pico_actual = max(resultado, combo["mejor_resultado_pct"])

        if pico_actual >= combo["tp_objetivo_pct"]:
            umbral_cierre = pico_actual * (1 - RETROCESO_TRAILING_PCT)
            if resultado <= umbral_cierre:
                db.cerrar_paxg_simulacion(combo["id"], resultado, motivo="trailing")
                continue
        if resultado <= STOP_LOSS_PCT:
            db.cerrar_paxg_simulacion(combo["id"], resultado, motivo="stop_loss")
            continue
        try:
            apertura = datetime.strptime(f"{combo['fecha']} {combo['hora_apertura']}", "%Y%m%d %H:%M").replace(tzinfo=db.TZ_ARG)
            horas_abierta = (datetime.now(db.TZ_ARG) - apertura).total_seconds() / 3600
            if horas_abierta >= MAX_DURACION_HORAS:
                db.cerrar_paxg_simulacion(combo["id"], resultado, motivo="cierre_intradia_forzado")
        except Exception:
            pass
