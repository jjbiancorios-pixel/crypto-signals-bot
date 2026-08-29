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

APALANCAMIENTO = {"bajo": 5, "medio": 10, "alto": 15}  # 18/08: "bajo" (5x) RESTAURADO, pero solo para C (ver COMBOS_ACTIVAS) — es la única combinación de "bajo" con evidencia real a favor (C_bajo_TP1 +1.16%, C_bajo_TP2 +1.81%, ambas n=7). ⚠️ 15x sin confirmar si Pionex lo permite real en PAXG/BTC — solo simulación.
# 18/08 — Reemplaza el viejo cruce APALANCAMIENTO×TP_POR_SENAL por
# combinaciones EXPLÍCITAS (señal, riesgo) -> lista de TP, porque ahora
# necesitamos que C_bajo tenga un TP distinto (solo TP1/TP2) que
# C_medio/C_alto (TP1/2/3/5) — algo que el cruce genérico no permitía
# expresar. Señal A queda AFUERA por completo (retirada 14/08, expectancy
# ponderada ~0% en sus 8 combinaciones — no hay evidencia real a favor).
COMBOS_ACTIVAS = {
    ("B", "medio"): [1.0, 2.0],
    ("B", "alto"): [1.0, 2.0],
    ("C", "medio"): [1.0, 2.0, 3.0, 5.0],
    ("C", "alto"): [1.0, 2.0, 3.0, 5.0],
    ("C", "bajo"): [1.0, 2.0],  # 18/08: reactivado, único "bajo" activo
}
SENALES_ACTIVAS = {"B", "C"}

# 23/08 (Juanjo) — TRADING REAL, primera vez que este cinturón arriesga
# BTC de verdad (hasta acá, 100% modo sombra). Se promovieron las
# combinaciones con mejor evidencia (n=27, muestra grande, no ruido):
# B_medio_TP1/TP2 y B_alto_TP1/TP2, TODAS con sl16 (confirmado: sl16 le
# ganó a sl10 y sl13 en las 4 comparaciones directas, sin excepción).
# Con el apalancamiento ahora unificado a 15x (Pionex confirmó que lo
# acepta real en PAXG/BTC — antes solo "alto" usaba 15x, "medio" 10x),
# B_medio y B_alto quedan CON LOS MISMOS PARÁMETROS — abrir ambos
# duplicaría la misma apuesta, no diversifica. Quedan 2 combos reales
# distintos: TP1 y TP2, ambos a 15x/sl16.
#
# Lado ganancia: trailing ESCALONADO de v18 (mismo esquema que el
# cinturón principal: activa a partir de PAXG_UMBRAL_TRAILING%,
# retrocede 50%/30%/20% según el pico — ver gestion_riesgo.
# _calcular_piso_trailing_escalonado, se reusa tal cual).
# Lado pérdida: SL PROPIO de PAXG (-16%, sl16), NO el cálculo por ATR
# del cinturón principal — decisión explícita: los sl10/13/16 de PAXG
# ya están validados con datos reales propios de este activo, mientras
# que el ATR fue diseñado para altcoins, dinámica de volatilidad
# distinta a un ratio oro/BTC.
PAXG_TRADING_REAL_ACTIVO = True  # interruptor simple: False = solo modo sombra, como hasta ahora
PAXG_APALANCAMIENTO_REAL = 15
PAXG_SL_REAL_PCT = -16.0
PAXG_COMBOS_REALES = [1.0, 2.0]  # TP1 y TP2, ambos con los parámetros de arriba
PAXG_UMBRAL_TRAILING = 4.0  # mismo umbral que el cinturón principal — a partir de acá se considera tendencia real, no ruido
# 23/08 — capital en BTC: PLACEHOLDER hasta que Juanjo confirme el monto
# real tras comprar el BTC. NO OPERAR CON ESTE VALOR SIN ANTES
# ACTUALIZARLO — ver /paxg_capital_btc para fijarlo real antes de activar.
PAXG_CAPITAL_BTC_INICIAL = None  # se carga desde db.obtener_capital_paxg_btc(), no se usa este valor directo
PAXG_PCT_CAPITAL_POR_OPERACION = 0.05  # 5% del capital BTC total por operación, "por ahora" (Juanjo: más adelante subirá el %)

STOP_LOSS_PCT = -20.0  # SL "original" — sigue siendo el que usa C_bajo (5x), no participa del experimento de abajo
# 18/08 (Opción 2, 3 variantes) — SL más ajustado, corriendo en PARALELO
# al original, aplicado SOLO a las combinaciones de riesgo medio/alto
# (10x/15x) — C_bajo (5x) no participa de este experimento, sigue con
# el STOP_LOSS_PCT de siempre. Motivo: el SL de -20% es el que más pesa
# individualmente en el resultado negativo actual (-20.72% promedio
# cuando dispara, ver análisis del 18/08).
VARIANTES_SL_MEDIO_ALTO = {"sl10": -10.0, "sl13": -13.0, "sl16": -16.0}
MAX_DURACION_HORAS = 20  # "intradía" — se fuerza el cierre si no cerró antes por TP/SL/trailing
# 18/08 — Opción 1 confirmada por Juanjo: en vez de esperar ciegamente a
# las 20hs, se evalúa en un checkpoint intermedio (10hs) si vale la pena
# seguir esperando o cortar antes — mismos 4 factores técnicos que ya
# usamos en v17 para el lado de pérdida de v16 (RSI, ADX, Bollinger,
# estado de BTC). Motivo: el cierre forzado por intradía es el 59% de
# todos los cierres post-fix, arrastrando el resultado neto a negativo.
CHECKPOINT_INTERMEDIO_HORAS = 10
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
    for f in (_velas_binance, _velas_bybit):
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

    # B) Tendencia: cruce EMA9/EMA21 + ADX>15, SIN exigir que el DI
    # confirme la dirección exacta.
    # 26/08 (Juanjo) — mismo criterio (Opción B) ya aplicado al cinturón
    # principal el mismo día: ADX>25 fijo bloqueaba casi toda oportunidad
    # de la señal B (la única con trading real en PAXG) — el candado
    # compartido con la simulación que arreglamos el 26/08 no servía de
    # nada si la señal casi nunca llegaba a dispararse. Bajado a >15,
    # igual que el principal, y sacada la exigencia de que el DI
    # confirme la dirección exacta (el cruce de EMA9/21 ya da la
    # dirección, el DI+/DI- pedía lo mismo dos veces).
    if datos_paxg["ema9"] > datos_paxg["ema21"] and datos_paxg["adx"] > 15:
        senales["B"] = "LARGO"
    elif datos_paxg["ema9"] < datos_paxg["ema21"] and datos_paxg["adx"] > 15:
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
    18/08: usa COMBOS_ACTIVAS (señal,riesgo)->TPs, explícito. Para
    riesgo medio/alto, cada combinación se abre 3 veces (una por cada
    variante de SL de VARIANTES_SL_MEDIO_ALTO) — riesgo bajo (solo
    C_bajo) usa el STOP_LOSS_PCT original, sin variantes.

    23/08 (Juanjo) — además de la simulación (que sigue corriendo igual,
    sin cambios), si la señal es "B" y PAXG_TRADING_REAL_ACTIVO, intenta
    abrir posiciones REALES para los 2 combos confirmados (TP1/TP2,
    ambos 15x/sl16). No reemplaza la simulación — corren en paralelo,
    la simulación sigue siendo la referencia de las 66 combinaciones
    completas.
    """
    for (senal, riesgo), tps in COMBOS_ACTIVAS.items():
        if senal != senal_tipo:
            continue
        apal = APALANCAMIENTO[riesgo]
        for tp in tps:
            if riesgo == "bajo":
                combinacion = f"{senal_tipo}_{riesgo}_TP{tp:g}"
                db.abrir_paxg_simulacion(combinacion, senal_tipo, riesgo, apal, tp, direccion, precio_entrada, sl_pct=STOP_LOSS_PCT)
            else:
                for etiqueta_sl, valor_sl in VARIANTES_SL_MEDIO_ALTO.items():
                    combinacion = f"{senal_tipo}_{riesgo}_TP{tp:g}_{etiqueta_sl}"
                    db.abrir_paxg_simulacion(combinacion, senal_tipo, riesgo, apal, tp, direccion, precio_entrada, sl_pct=valor_sl)


def _avisar_telegram_paxg(msg: str):
    """
    28/08 (Juanjo) — caso real: PAXG nunca mandaba NADA a Telegram, ni
    siquiera los fallos de apertura con plata real — todo quedaba
    únicamente en los logs de Railway, invisible en el chat. Import
    diferido (adentro de la función, no arriba del archivo) para evitar
    el ciclo: main.py importa paxg_bot, así que paxg_bot no puede
    importar main a nivel de módulo sin crear una dependencia circular
    — a esta altura (la función ya se está EJECUTANDO, no cargando)
    main.py ya terminó de importar todo, así que funciona bien.
    """
    try:
        import main
        main.enviar_telegram(msg)
    except Exception as e:
        print(f"⚠️ No se pudo avisar por Telegram (PAXG): {e}")


# 29/08 (Juanjo) — caso real: con la apertura de PAXG fallando seguido
# (mismo problema de conectividad con Pionex que afecta al resto del
# sistema), el aviso por Telegram del punto anterior generó "cantidad
# de mensajes" — cada intento manda hasta 2 avisos (TP1 y TP2), cada 15
# min que la señal B dispare. Mismo criterio ya usado para "error
# consultando cierre" y el recálculo de capital: avisa la 1ra vez,
# después cada 15 intentos — sigue reintentando la apertura real cada
# vez igual, solo se calla el aviso repetido.
_fallos_apertura_paxg = 0


def _avisar_apertura_paxg_con_throttle(msg: str):
    global _fallos_apertura_paxg
    _fallos_apertura_paxg += 1
    if _fallos_apertura_paxg == 1 or _fallos_apertura_paxg % 15 == 0:
        _avisar_telegram_paxg(f"{msg}\n(van {_fallos_apertura_paxg} intento(s) de apertura fallando seguido)")


def _abrir_paxg_real(direccion: str, precio_entrada: float):
    """
    23/08 (Juanjo) — Apertura REAL de PAXG/BTC. Por cada TP en
    PAXG_COMBOS_REALES (1.0, 2.0), abre una grilla real independiente,
    con quote="BTC" (fix del 23/08 — antes hardcodeado a USDT, rompía
    la validación del rango para este par), 15x, SL nativo -16%.

    Capital: exige que ya se haya cargado PAXG_CAPITAL_BTC vía
    /paxg_capital_btc — si no, NO abre nada (mejor no operar que operar
    con un placeholder equivocado, es plata real).

    26/08 (Juanjo) — 2 cambios importantes:
    (1) Candado PROPIO: antes dependía de que no hubiera combos
    SIMULADOS abiertos — con 24 combinaciones de sombra siempre
    corriendo, esto bloqueaba la apertura real casi todo el tiempo sin
    que nadie lo notara. Ahora chequea SUS PROPIAS posiciones reales
    abiertas (paxg_senales_reales), totalmente independiente de la
    simulación.
    (2) Máximo 1 apertura real por ronda: aunque la señal B calificara
    para TP1 y TP2 a la vez, solo se abre una — se corta con el primer
    éxito, no se intentan los 2 combos en la misma pasada.
    """
    ya_abiertas = db.operaciones_paxg_reales_abiertas()
    if len(ya_abiertas) > 0:
        print(f"ℹ️ PAXG real: ya hay {len(ya_abiertas)} posición(es) real(es) abierta(s) — no se abre otra esta ronda.")
        return

    capital_btc_total = db.obtener_capital_paxg_btc()
    if capital_btc_total is None:
        print("⚠️ PAXG real: no se abrió nada — falta cargar el capital en BTC con /paxg_capital_btc")
        return

    capital_btc_operacion = round(capital_btc_total * PAXG_PCT_CAPITAL_POR_OPERACION, 8)
    trend = "long" if direccion == "LARGO" else "short"

    for tp in PAXG_COMBOS_REALES:
        top = round(precio_entrada * 1.03, 6)
        bottom = round(precio_entrada * 0.97, 6)
        try:
            resp = pionex_api.crear_grilla_futuros(
                par="PAXG", top=top, bottom=bottom, row=67,
                capital_usdt=capital_btc_operacion,  # nombre del parámetro genérico, acá representa BTC (quote=BTC)
                leverage=PAXG_APALANCAMIENTO_REAL, trend=trend, extra_margin_usdt=0,
                sl_pct=PAXG_SL_REAL_PCT, quote="BTC",
            )
            bu_order_id = resp.get("data", {}).get("buOrderId")
            if bu_order_id:
                combinacion = f"B_real_TP{tp:g}_sl16_15x"
                db.guardar_senal_paxg_real(combinacion, "B", PAXG_APALANCAMIENTO_REAL, tp, direccion,
                                            precio_entrada, PAXG_SL_REAL_PCT, bu_order_id, capital_btc_operacion)
                msg = f"✅ PAXG real abierta: {combinacion} — {capital_btc_operacion} BTC, bu_order_id={bu_order_id}"
                print(msg)
                _avisar_telegram_paxg(msg)
                global _fallos_apertura_paxg
                _fallos_apertura_paxg = 0  # se recuperó, resetear
                return  # 26/08: máximo 1 por ronda — corta apenas abre una, no intenta el otro TP
            else:
                msg = f"⚠️ PAXG real: Pionex no devolvió buOrderId para TP{tp:g} — {resp}"
                print(msg)
                _avisar_apertura_paxg_con_throttle(msg)
        except Exception as e:
            msg = f"⚠️ PAXG real: falló la apertura para TP{tp:g} — {e}"
            print(msg)
            _avisar_apertura_paxg_con_throttle(msg)


def _evaluar_factores_tecnicos_paxg(direccion: str) -> dict:
    """
    18/08 — Mismos 4 factores técnicos que v17 (v16 principal), aplicados
    acá al checkpoint intermedio (10hs) de PAXG: RSI en sobreventa/
    sobrecompra a favor de reversión, ADX bajando, precio tocó banda de
    Bollinger contraria, BTC no refuerza el movimiento en contra.
    OJO: PAXG/BTC se mueve INVERSO a BTC (rotación risk-on/risk-off) —
    por eso el factor 4 está invertido respecto de v16/v17: BTC ALCISTA
    (risk-on) es lo que perjudica a una posición LARGA en PAXG/BTC, no
    BAJISTA como en un par cripto normal.
    """
    try:
        df15 = get_velas("PAXGBTC", "15m", n=40)
        if df15 is None or len(df15) < 30:
            return {"factores_cumplidos": 0, "error": "sin velas suficientes"}
        df_btc = get_velas("BTCUSDT", "15m", n=20)

        rsi = calc_rsi(df15["close"])
        bb = calc_bb(df15["close"])
        precio_actual = float(df15["close"].iloc[-1])
        ancho_banda = bb["upper"] - bb["lower"]
        pos_banda = (precio_actual - bb["lower"]) / ancho_banda if ancho_banda > 0 else 0.5
        adx_reciente = calc_adx(df15.tail(20))["adx"]
        adx_anterior = calc_adx(df15.iloc[-28:-8])["adx"]
        adx_bajando = adx_reciente < adx_anterior
        estado_btc = calc_estado_btc_simple(df_btc) if df_btc is not None else "SIN_DATO"

        es_largo = direccion == "LARGO"
        if es_largo:
            f1 = rsi <= 32
            f3 = pos_banda <= 0.10
            f4 = estado_btc != "ALCISTA"  # BTC alcista (risk-on) perjudica al LARGO en PAXG/BTC (inverso)
        else:
            f1 = rsi >= 68
            f3 = pos_banda >= 0.90
            f4 = estado_btc != "BAJISTA"
        f2 = adx_bajando

        return {
            "factores_cumplidos": sum([f1, f2, f3, f4]), "rsi": rsi, "adx": adx_reciente,
            "adx_bajando": adx_bajando, "estado_btc": estado_btc, "toco_banda_contraria": f3,
        }
    except Exception as e:
        return {"factores_cumplidos": 0, "error": str(e)}


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
        if tipo not in SENALES_ACTIVAS:
            continue
        if direccion and not db.paxg_hay_combos_abiertas_de(tipo):
            abrir_lote(tipo, direccion, precio_paxg_actual)

    # 26/08 (Juanjo) — lo que pasa en modo SOMBRA no debe afectar el
    # trading REAL: antes, _abrir_paxg_real vivía DENTRO de abrir_lote,
    # que solo se llamaba si NO había combos SIMULADOS abiertos de ese
    # tipo — con 24 combinaciones simuladas siempre corriendo, "B" casi
    # siempre tenía algo abierto, bloqueando sin querer la apertura real
    # (nunca llegó a intentarse en la práctica). Ahora es independiente:
    # se llama siempre que la señal B dispara, sin importar el estado de
    # la simulación.
    if senales.get("B") and PAXG_TRADING_REAL_ACTIVO:
        _abrir_paxg_real(senales["B"], precio_paxg_actual)

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
        sl_aplicable = combo.get("sl_pct")
        if sl_aplicable is None:
            sl_aplicable = STOP_LOSS_PCT  # compatibilidad con combinaciones viejas sin sl_pct guardado
        if resultado <= sl_aplicable:
            db.cerrar_paxg_simulacion(combo["id"], resultado, motivo="stop_loss")
            continue
        try:
            apertura = datetime.strptime(f"{combo['fecha']} {combo['hora_apertura']}", "%Y%m%d %H:%M").replace(tzinfo=db.TZ_ARG)
            horas_abierta = (datetime.now(db.TZ_ARG) - apertura).total_seconds() / 3600

            # 18/08 — checkpoint intermedio (10hs): antes de esperar
            # ciegamente a las 20hs, evaluar si los factores técnicos
            # favorecen cortar antes. Se evalúa UNA sola vez por
            # combinación (no en cada ciclo de 15 min).
            if (horas_abierta >= CHECKPOINT_INTERMEDIO_HORAS
                    and not combo.get("checkpoint_intermedio_evaluado")):
                analisis = _evaluar_factores_tecnicos_paxg(combo["direccion"])
                factores = analisis.get("factores_cumplidos", 0)
                db.marcar_checkpoint_intermedio_paxg(combo["id"])
                if factores < 3:
                    db.cerrar_paxg_simulacion(combo["id"], resultado, motivo="checkpoint_intermedio")
                    continue

            if horas_abierta >= MAX_DURACION_HORAS:
                db.cerrar_paxg_simulacion(combo["id"], resultado, motivo="cierre_intradia_forzado")
        except Exception:
            pass


def monitorear_paxg_real():
    """
    23/08 (Juanjo) — Monitoreo de las posiciones REALES de PAXG.
    Lado ganancia: trailing ESCALONADO de v18 (reusa gestion_riesgo.
    _calcular_piso_trailing_escalonado tal cual, mismo esquema que el
    cinturón principal — activa a partir de PAXG_UMBRAL_TRAILING%,
    retrocede 50%/30%/20% según el pico).
    Lado pérdida: NO se toca — el SL nativo (-16%, sl16) protege solo,
    fijado en Pionex desde la apertura, decisión explícita de Juanjo de
    no aplicar el cálculo por ATR acá.
    """
    import gestion_riesgo
    abiertas = db.operaciones_paxg_reales_abiertas()
    for op in abiertas:
        try:
            desglose = pionex_api.calcular_resultado_desglosado(
                op["bu_order_id"], par="PAXGBTC", capital_total_real=op["capital_btc_operacion"]
            )
            resultado_actual = desglose["total_pct"] if desglose else None
        except Exception as e:
            print(f"⚠️ PAXG real {op['combinacion']}: falló la consulta de resultado — {e}")
            continue
        if resultado_actual is None:
            continue

        mejor = op.get("mejor_resultado_pct")
        if mejor is None or resultado_actual > mejor:
            db.actualizar_mejor_resultado_paxg_real(op["id"], resultado_actual)
            mejor = max(resultado_actual, mejor) if mejor is not None else resultado_actual

        if mejor >= PAXG_UMBRAL_TRAILING:
            piso = gestion_riesgo._calcular_piso_trailing_escalonado(mejor)
            if resultado_actual <= piso:
                try:
                    pionex_api.cerrar_grilla_futuros(op["bu_order_id"], nota=f"PAXG real: trailing — pico {mejor:+.2f}%, piso {piso:+.2f}%")
                    db.cerrar_senal_paxg_real(op["id"], resultado_actual, motivo="trailing_escalonado")
                    msg = f"✅ PAXG real cerrada por trailing: {op['combinacion']} — pico {mejor:+.2f}%, cerrada en {resultado_actual:+.2f}%"
                    print(msg)
                    _avisar_telegram_paxg(msg)
                except Exception as e:
                    msg = f"🚨 PAXG real {op['combinacion']}: tocó el trailing pero falló el cierre — {e} — REVISAR YA"
                    print(msg)
                    _avisar_telegram_paxg(msg)
