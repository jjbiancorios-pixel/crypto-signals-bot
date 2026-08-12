"""
bingx_bot.py — Cinturón de INVESTIGACIÓN BingX (05/08/2026)

MODO SOMBRA PURO — no opera nada, no usa API key, no arriesga capital.
Solo recolecta: order book imbalance + RSI/VWAP de velas cortas (1-5 min)
+ qué hizo el precio DESPUÉS — para poder calcular, con datos reales
propios, qué umbral de desequilibrio predice mejor la dirección del
precio (Juanjo: "el umbral será el que nos dé mayores oportunidades de
acierto" — no se fija a priori).

⚠️ AVISO: los endpoints de order book/velas de BingX Futuros USDT-M están
marcados abajo con el path que encontré en la documentación pública, pero
no tuve forma de probarlos contra la API real (sandbox sin acceso a
internet). Si al desplegar no llegan datos, lo primero a revisar es que
estos paths sean los correctos — no hace falta API key para estos
endpoints públicos, así que un fallo acá es de path/formato, no de
credenciales.

Autocontenido (no importa main.py ni otros módulos de v16) para evitar
import circular, mismo patrón que paxg_bot.py y pionex_api.py.
"""
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

import db

BASE_URL = "https://open-api.bingx.com"
SYMBOL = "BTC-USDT"

# ⚠️ Paths a verificar contra la documentación real al desplegar —
# encontrados en búsqueda, no confirmados contra la API en vivo.
PATH_DEPTH = "/openApi/swap/v2/quote/depth"
PATH_KLINES = "/openApi/swap/v3/quote/klines"


def obtener_order_book(symbol: str = SYMBOL, limit: int = 20):
    """
    Endpoint público (sin API key). Devuelve {'bids': [[precio, cantidad], ...], 'asks': [...]}
    o None si falla.
    """
    try:
        url = f"{BASE_URL}{PATH_DEPTH}"
        r = requests.get(url, params={"symbol": symbol, "limit": limit}, timeout=10)
        data = r.json()
        d = data.get("data", {})
        if not d.get("bids") or not d.get("asks"):
            print(f"⚠️ BingX order book: respuesta sin bids/asks (status {r.status_code}): {str(data)[:200]}")
            return None
        return d
    except Exception as e:
        print(f"⚠️ BingX order book: error de conexión/parseo — {e}")
        return None


def calcular_imbalance(order_book: dict) -> float:
    """
    Desequilibrio del libro de órdenes: (compras - ventas) / (compras + ventas),
    entre -1 (todo venta) y +1 (todo compra). Usa la cantidad (no el precio)
    de cada nivel del book.
    """
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    vol_bids = sum(float(b[1]) for b in bids)
    vol_asks = sum(float(a[1]) for a in asks)
    total = vol_bids + vol_asks
    if total <= 0:
        return 0.0
    return round((vol_bids - vol_asks) / total, 4)


def get_velas(symbol: str = SYMBOL, interval: str = "1m", limit: int = 50):
    """Velas OHLC — endpoint público. Devuelve DataFrame o None."""
    try:
        url = f"{BASE_URL}{PATH_KLINES}"
        r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        data = r.json().get("data", [])
        if not data or len(data) < 15:
            return None
        df = pd.DataFrame(data)
        # Nombres de columna a confirmar contra la respuesta real — uso los
        # más comunes en exchanges compatibles con Binance-style klines.
        cols_map = {0: "open_time", 1: "open", 2: "high", 3: "low", 4: "close", 5: "volume"}
        if isinstance(data[0], list):
            df = df.rename(columns=cols_map)
        for c in ["open", "high", "low", "close", "volume"]:
            if c in df.columns:
                df[c] = df[c].astype(float)
        return df
    except Exception as e:
        print(f"⚠️ BingX velas ({interval}): error de conexión/parseo — {e}")
        return None


def get_precio(symbol: str = SYMBOL):
    """Último precio — a partir del order book (mid price), evita otro endpoint más."""
    ob = obtener_order_book(symbol, limit=5)
    if not ob:
        return None
    try:
        mejor_bid = float(ob["bids"][0][0])
        mejor_ask = float(ob["asks"][0][0])
        return (mejor_bid + mejor_ask) / 2
    except Exception:
        return None


def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    val = 100 - 100 / (1 + g / l.replace(0, np.nan))
    return float(val.iloc[-1]) if not val.empty else None


def calc_vwap(df):
    if "volume" not in df.columns:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (tp * df["volume"]).cumsum() / df["volume"].cumsum()
    return float(vwap.iloc[-1])


def recopilar_datos():
    """
    Función principal — se llama periódicamente desde main.py. Recolecta
    UN snapshot (imbalance + RSI 1m/5m + VWAP + precio), lo guarda, y de
    paso completa los resultados "1min/5min después" de snapshots viejos.
    NO abre ninguna operación — es investigación pura.
    """
    ob = obtener_order_book()
    if ob is None:
        # 05/08 (FIX): antes esto se perdía en silencio (mismo error que
        # ya corregimos hoy en el stop-loss) — ahora se loggea, para poder
        # diagnosticar si PATH_DEPTH está mal en vez de quedarnos sin
        # saber por qué /bingx no muestra datos.
        print("⚠️ BingX: no se pudo obtener el order book — revisar PATH_DEPTH en bingx_bot.py.")
        return

    precio_actual = get_precio()
    if precio_actual is None:
        print("⚠️ BingX: no se pudo calcular el precio actual a partir del order book.")
        return

    imbalance = calcular_imbalance(ob)

    df_1m = get_velas(interval="1m", limit=30)
    df_5m = get_velas(interval="5m", limit=30)
    if df_1m is None or df_5m is None:
        print("⚠️ BingX: no se pudieron obtener las velas (1m/5m) — revisar PATH_KLINES en bingx_bot.py. Se guarda igual el imbalance, sin RSI.")
    rsi_1m = calc_rsi(df_1m["close"]) if df_1m is not None else None
    rsi_5m = calc_rsi(df_5m["close"]) if df_5m is not None else None
    vwap_1m = calc_vwap(df_1m) if df_1m is not None else None

    db.guardar_bingx_dato({
        "symbol": SYMBOL, "precio": precio_actual, "imbalance": imbalance,
        "rsi_1m": rsi_1m, "rsi_5m": rsi_5m, "vwap_1m": vwap_1m,
    })

    # Completar "qué pasó después" de snapshots de hace ~1 y ~5 minutos
    db.completar_resultados_bingx(precio_actual, symbol=SYMBOL)


# ══════════════════════════════════════════════════════════════════
# 10/08 — Cinturón BingX-martingala (modo sombra puro, sin capital real)
# ══════════════════════════════════════════════════════════════════
# Variante A: la dirección de CADA trade (incluida la operación 1) se
# define con el order book imbalance fresco, umbral 0.6.
# Variante B: la dirección de la operación 1 también viene del imbalance
# (mismo umbral) — pero de ahí en más, las operaciones 2-6 siguen el
# guion FIJO del video (según si la operación 1 fue LONG o SHORT), sin
# volver a consultar el order book.
UMBRAL_MARTINGALA = 0.6
APUESTA_INICIAL = 5.0
PROFUNDIDAD_MAXIMA = 6
GUION_LONG = ["LONG", "LONG", "SHORT", "LONG", "SHORT", "LONG"]
GUION_SHORT = ["SHORT", "SHORT", "LONG", "SHORT", "LONG", "SHORT"]


def _direccion_por_imbalance(imbalance: float):
    """Devuelve 'LONG'/'SHORT' si supera el umbral, o None si no hay señal clara."""
    if imbalance >= UMBRAL_MARTINGALA:
        return "LONG"
    elif imbalance <= -UMBRAL_MARTINGALA:
        return "SHORT"
    return None


def _evaluar_trade(direccion: str, precio_entrada: float, precio_actual: float) -> bool:
    """True si el precio se movió a favor de la dirección predicha."""
    if direccion == "LONG":
        return precio_actual > precio_entrada
    return precio_actual < precio_entrada


def _intentar_abrir_secuencia(variante: str, imbalance: float, precio_actual: float):
    """Si no hay secuencia abierta de esta variante y hay señal, abre una nueva."""
    if db.secuencias_martingala_abiertas(variante):
        return
    direccion = _direccion_por_imbalance(imbalance)
    if direccion:
        db.abrir_secuencia_martingala(variante, direccion, precio_actual)


def _actualizar_capital_track(variante: str, motivo: str, resultado_usd: float):
    """
    11/08 — Actualiza los 2 tracks de capital de una variante sobre el
    MISMO resultado real (no simula secuencias nuevas, es bookkeeping):
    - "{variante}_500": capital corrido puro, sin red de contención — si
      una ruina lo deja muy bajo, queda así, sin reponerse.
    - "{variante}_1000": 500 activos + 500 de reserva — tras CUALQUIER
      ruina, si queda reserva, repone el capital activo a $500 exacto,
      descontando lo que haga falta de la reserva (pedido explícito de
      Juanjo: reponer automático tras cada ruina, no solo si se agota).
    """
    for modo in ("500", "1000"):
        track = f"{variante}_{modo}"
        estado = db.obtener_capital_track(track)
        nuevo_capital = estado["capital_activo"] + resultado_usd
        nueva_reserva = estado["reserva_disponible"]
        veces_repuesto = estado.get("veces_repuesto", 0)

        if motivo == "ruina" and modo == "1000" and nueva_reserva > 0:
            faltante = 500.0 - nuevo_capital
            if faltante > 0:
                usado = min(faltante, nueva_reserva)
                nuevo_capital += usado
                nueva_reserva -= usado
                veces_repuesto += 1

        db.guardar_capital_track(track, round(nuevo_capital, 2), round(nueva_reserva, 2), veces_repuesto)


def _procesar_secuencia(sec: dict, precio_actual: float, imbalance: float):
    """Evalúa si el trade actual de una secuencia abierta ya cumplió su minuto de espera."""
    hora_entrada = datetime.strptime(
        f"{sec['fecha']} {sec['hora_entrada_trade']}", "%Y%m%d %H:%M:%S"
    ).replace(tzinfo=db.TZ_ARG)
    segundos_transcurridos = (datetime.now(db.TZ_ARG) - hora_entrada).total_seconds()
    if segundos_transcurridos < 60:
        return  # todavía no pasó el minuto de evaluación

    gano = _evaluar_trade(sec["direccion_trade_actual"], sec["precio_entrada_trade"], precio_actual)

    if gano:
        resultado_neto = sec["apuesta_actual"] - sec["perdido_acumulado"]
        db.cerrar_secuencia_martingala(sec["id"], round(resultado_neto, 2), motivo="ganada")
        _actualizar_capital_track(sec["variante"], "ganada", round(resultado_neto, 2))
        return

    # Perdió este trade
    if sec["trade_actual"] >= PROFUNDIDAD_MAXIMA:
        perdida_total = -(sec["perdido_acumulado"] + sec["apuesta_actual"])
        db.cerrar_secuencia_martingala(sec["id"], round(perdida_total, 2), motivo="ruina")
        _actualizar_capital_track(sec["variante"], "ruina", round(perdida_total, 2))
        return

    # Avanza al siguiente trade — dirección según la variante
    nueva_apuesta = sec["apuesta_actual"] * 2
    proximo_indice = sec["trade_actual"]  # 0-indexed para el guion (trade_actual pasará a +1)
    if sec["variante"] == "A":
        direccion_nueva = _direccion_por_imbalance(imbalance) or sec["direccion_trade_actual"]
    else:
        guion = GUION_LONG if sec["direccion_op1"] == "LONG" else GUION_SHORT
        direccion_nueva = guion[proximo_indice] if proximo_indice < len(guion) else sec["direccion_trade_actual"]

    db.avanzar_trade_martingala(sec["id"], nueva_apuesta, precio_actual, direccion_nueva)


def simular_martingala():
    """
    Función principal de este cinturón — se llama junto con recopilar_datos()
    en el mismo ciclo de 30 seg. NO abre ninguna operación real en BingX,
    todo es simulado con el precio de mercado real.
    """
    ob = obtener_order_book()
    if ob is None:
        return
    precio_actual = get_precio()
    if precio_actual is None:
        return
    imbalance = calcular_imbalance(ob)

    for variante in ("A", "B"):
        abiertas = db.secuencias_martingala_abiertas(variante)
        if abiertas:
            for sec in abiertas:
                _procesar_secuencia(sec, precio_actual, imbalance)
        else:
            _intentar_abrir_secuencia(variante, imbalance, precio_actual)
