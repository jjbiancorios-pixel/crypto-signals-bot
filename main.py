import requests
import pandas as pd
import numpy as np
import time
import schedule
from datetime import datetime
import os

# ── Configuración ──────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8761617567:AAGbH0Vgb-13kVZppZ-fwZHT6QngI8ZkYOo")
CHAT_ID        = os.environ.get("CHAT_ID", "674187707")

PARES = [
    "ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT",
    "ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT",
    "LTCUSDT","UNIUSDT","ATOMUSDT","ETCUSDT","XLMUSDT",
    "TRXUSDT","AAVEUSDT","COMPUSDT","ALGOUSDT","ICPUSDT",
    "AXSUSDT","SANDUSDT","MANAUSDT","GALAUSDT","APEUSDT",
    "FTMUSDT","NEARUSDT","EGLDUSDT","ZILUSDT","CHZUSDT",
    "CRVUSDT","RUNEUSDT","KAVAUSDT","HBARUSDT","XTZUSDT",
    "OPUSDT","ARBUSDT","INJUSDT","SUIUSDT","WLDUSDT",
    "TIAUSDT","STXUSDT","CFXUSDT","LDOUSDT","SEIUSDT",
    "RENDERUSDT","FETUSDT","OCEANUSDT","GRTUSDT","CKBUSDT",
    "1000SHIBUSDT","1000PEPEUSDT","WIFUSDT","BONKUSDT","FLOKIUSDT",
]

MIN_SCORE    = 5   # sobre 16 puntos posibles
MAX_ALERTAS  = 5
HORA_RESUMEN = "09:00"

alertas_enviadas = {}
resumen_enviado  = {}


# ── Telegram ───────────────────────────────────────────────
def enviar_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


# ── Datos ──────────────────────────────────────────────────
def get_velas(par: str, tf: str, n: int = 200) -> pd.DataFrame | None:
    url = f"https://api.binance.com/api/v3/klines?symbol={par}&interval={tf}&limit={n}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 30:
            return None
        df = pd.DataFrame(data, columns=[
            "ts","open","high","low","close","vol",
            "ct","qav","trades","tbbav","tbqav","ignore"
        ])
        for c in ["open","high","low","close","vol"]:
            df[c] = df[c].astype(float)
        return df
    except:
        return None


# ── Indicadores ────────────────────────────────────────────
def rsi(s: pd.Series, p=14) -> float:
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return (100 - 100/(1 + g/l.replace(0, np.nan))).iloc[-1]

def stoch_rsi(s: pd.Series, p=14) -> float:
    r_ = 100 - 100/(1 + s.diff().clip(lower=0).rolling(p).mean() /
                    (-s.diff().clip(upper=0)).rolling(p).mean().replace(0, np.nan))
    mn = r_.rolling(p).min()
    mx = r_.rolling(p).max()
    stoch = (r_ - mn) / (mx - mn + 1e-10) * 100
    return stoch.iloc[-1]

def atr(df: pd.DataFrame, p=14) -> float:
    hl  = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift()).abs()
    lcp = (df["low"]  - df["close"].shift()).abs()
    return pd.concat([hl,hcp,lcp],axis=1).max(axis=1).rolling(p).mean().iloc[-1]

def bollinger(s: pd.Series, p=20) -> dict:
    m  = s.rolling(p).mean()
    st = s.rolling(p).std()
    up = m + 2*st
    dn = m - 2*st
    precio = s.iloc[-1]
    ancho  = ((up - dn) / m * 100).iloc[-1]
    pos    = (precio - dn.iloc[-1]) / (up.iloc[-1] - dn.iloc[-1] + 1e-10)  # 0=bajo, 1=alto
    return {"upper": up.iloc[-1], "lower": dn.iloc[-1], "ancho": ancho, "pos": pos, "mid": m.iloc[-1]}

def macd(s: pd.Series) -> dict:
    m  = s.ewm(span=12).mean() - s.ewm(span=26).mean()
    sg = m.ewm(span=9).mean()
    hist = m - sg
    return {"macd": m.iloc[-1], "signal": sg.iloc[-1], "hist": hist.iloc[-1],
            "cruce_alcista": m.iloc[-1] > sg.iloc[-1] and m.iloc[-2] <= sg.iloc[-2],
            "cruce_bajista": m.iloc[-1] < sg.iloc[-1] and m.iloc[-2] >= sg.iloc[-2]}

def emas(s: pd.Series) -> dict:
    return {
        "ema20":  s.ewm(span=20).mean().iloc[-1],
        "ema50":  s.ewm(span=50).mean().iloc[-1],
        "ema200": s.ewm(span=200).mean().iloc[-1] if len(s) >= 200 else None,
    }

def patron_velas(df: pd.DataFrame) -> str:
    """Detecta patrones de velas japonesas"""
    c  = df["close"].iloc[-1]
    o  = df["open"].iloc[-1]
    h  = df["high"].iloc[-1]
    l  = df["low"].iloc[-1]
    c1 = df["close"].iloc[-2]
    o1 = df["open"].iloc[-2]
    cuerpo  = abs(c - o)
    rango_v = h - l
    if rango_v == 0:
        return "NEUTRO"
    mecha_inf = min(c,o) - l
    mecha_sup = h - max(c,o)
    # Doji
    if cuerpo / rango_v < 0.1:
        return "DOJI"
    # Martillo (reversión alcista)
    if mecha_inf > 2*cuerpo and mecha_sup < cuerpo and c1 < o1:
        return "MARTILLO_ALCISTA"
    # Shooting star (reversión bajista)
    if mecha_sup > 2*cuerpo and mecha_inf < cuerpo and c1 > o1:
        return "SHOOTING_STAR_BAJISTA"
    # Engulfing alcista
    if c > o and c1 < o1 and c > o1 and o < c1:
        return "ENGULFING_ALCISTA"
    # Engulfing bajista
    if c < o and c1 > o1 and c < o1 and o > c1:
        return "ENGULFING_BAJISTA"
    # Vela alcista fuerte
    if c > o and cuerpo/rango_v > 0.7:
        return "VELA_ALCISTA_FUERTE"
    # Vela bajista fuerte
    if c < o and cuerpo/rango_v > 0.7:
        return "VELA_BAJISTA_FUERTE"
    return "NEUTRO"

def vol_relativo(df: pd.DataFrame) -> float:
    return df["vol"].iloc[-1] / df["vol"].iloc[-21:-1].mean()


# ── Tendencia BTC completa ─────────────────────────────────
def tendencia_btc() -> dict:
    resultado = {"emoji": "↔️", "resumen": "LATERAL", "fuerza": 0, "precio": 0, "detalle": []}

    df1d = get_velas("BTCUSDT", "1d", 50)
    df4h = get_velas("BTCUSDT", "4h", 100)
    df1h = get_velas("BTCUSDT", "1h", 100)

    if df1d is None:
        return resultado

    precio = df1d["close"].iloc[-1]
    resultado["precio"] = precio

    fuerza = 0
    detalle = []

    # Análisis diario
    e1d  = emas(df1d["close"])
    r1d  = rsi(df1d["close"])
    bb1d = bollinger(df1d["close"])
    if precio > e1d["ema20"] > e1d["ema50"]:
        fuerza += 2
        detalle.append("📈 Diario: EMA20>EMA50 alcista")
    elif precio < e1d["ema20"] < e1d["ema50"]:
        fuerza -= 2
        detalle.append("📉 Diario: EMA20<EMA50 bajista")
    else:
        detalle.append("↔️ Diario: lateral")

    if r1d > 55:
        fuerza += 1
        detalle.append(f"📈 RSI diario: {r1d:.0f}")
    elif r1d < 45:
        fuerza -= 1
        detalle.append(f"📉 RSI diario: {r1d:.0f}")

    # Análisis 4h
    if df4h is not None:
        e4h = emas(df4h["close"])
        r4h = rsi(df4h["close"])
        p4h = df4h["close"].iloc[-1]
        if p4h > e4h["ema20"]:
            fuerza += 1
            detalle.append(f"📈 4h: sobre EMA20")
        else:
            fuerza -= 1
            detalle.append(f"📉 4h: bajo EMA20")

    # Análisis 1h
    if df1h is not None:
        e1h = emas(df1h["close"])
        p1h = df1h["close"].iloc[-1]
        if p1h > e1h["ema20"]:
            fuerza += 1
            detalle.append(f"📈 1h: sobre EMA20")
        else:
            fuerza -= 1
            detalle.append(f"📉 1h: bajo EMA20")

    resultado["fuerza"]  = fuerza
    resultado["detalle"] = detalle

    if fuerza >= 3:
        resultado["emoji"]   = "🚀"
        resultado["resumen"] = "ALCISTA FUERTE"
    elif fuerza >= 1:
        resultado["emoji"]   = "📈"
        resultado["resumen"] = "ALCISTA"
    elif fuerza <= -3:
        resultado["emoji"]   = "💥"
        resultado["resumen"] = "BAJISTA FUERTE"
    elif fuerza <= -1:
        resultado["emoji"]   = "📉"
        resultado["resumen"] = "BAJISTA"
    else:
        resultado["emoji"]   = "↔️"
        resultado["resumen"] = "LATERAL"

    return resultado


# ── Cálculo de grillas y rango ─────────────────────────────
def calcular_grid(precio: float, atr_val: float, atr_pct: float, score: int, btc_fuerza: int) -> dict:
    """
    Calcula rango, grillas y apalancamiento óptimos para el Grid de Futuros.
    Objetivo: 2-3 cruces de grilla = 1% de ganancia con liquidación alejada.
    """
    # Rango base: 3x ATR (suficiente para oscilar sin romper)
    # Si BTC está en tendencia fuerte, rango más amplio para no liquidar
    factor_rango = 3.0 if abs(btc_fuerza) <= 2 else 4.0

    rango_total_pct = atr_pct * factor_rango
    rango_bajo  = round(precio * (1 - rango_total_pct/100), 4)
    rango_alto  = round(precio * (1 + rango_total_pct/100), 4)

    # Grillas: queremos que cada grilla sea ~0.3-0.5% para que 2-3 cruces = 1%
    # Ajustamos la cantidad de grillas al rango
    pct_por_grilla_objetivo = 0.35  # % por grilla
    grillas = max(10, min(150, int(rango_total_pct / pct_por_grilla_objetivo)))
    pct_real_grilla = rango_total_pct / grillas

    # Apalancamiento: que la ganancia por grilla con apalancamiento sea atractiva
    # pero el precio de liquidación quede al menos 15% fuera del rango
    # Liquidación aprox = precio * (1 - 1/apalancamiento)
    # Queremos: precio_liquidacion < rango_bajo * 0.85
    # => 1/apalancamiento > 1 - rango_bajo*0.85/precio
    # => apalancamiento < precio / (precio - rango_bajo*0.85)
    margen_seguridad = 0.85  # liquidación debe quedar 15% más abajo que el rango bajo
    max_apal = int(precio / (precio - rango_bajo * margen_seguridad))
    max_apal = max(2, min(max_apal, 20))

    # Apalancamiento recomendado según score y volatilidad
    if atr_pct >= 1.5:
        apal_base = 3
    elif atr_pct >= 0.8:
        apal_base = 5
    elif atr_pct >= 0.4:
        apal_base = 7
    else:
        apal_base = 10

    apalancamiento = min(apal_base, max_apal)

    # Precio de liquidación estimado (largo)
    liq_largo = round(precio * (1 - 1/apalancamiento), 4)
    liq_corto = round(precio * (1 + 1/apalancamiento), 4)

    # Distancia del precio de liquidación al rango
    dist_liq_pct = round((precio - liq_largo) / precio * 100 - rango_total_pct/2, 2)

    # Tiempo estimado para completar 1% (2-3 cruces)
    # Cada cruce tarda aprox atr_pct*4 veces por hora en 15m
    cruces_por_hora = (atr_pct * 4) / pct_real_grilla if pct_real_grilla > 0 else 1
    horas_para_1pct = (2.5 / cruces_por_hora) if cruces_por_hora > 0 else 99  # 2.5 cruces promedio

    if horas_para_1pct < 1:
        tiempo_1pct = f"{int(horas_para_1pct*60)} min"
    elif horas_para_1pct < 8:
        tiempo_1pct = f"{horas_para_1pct:.1f} hs"
    else:
        tiempo_1pct = "+8 hs"

    apto_intraday = horas_para_1pct <= 6

    # Preset Pionex sugerido
    if score >= 11:
        preset = "🟢 AGRESIVA"
        preset_razon = "Alta confianza — usala como base y ajustá rango y grillas"
    elif score >= 7:
        preset = "🟡 BALANCEADA"
        preset_razon = "Confianza media — modificá rango y apalancamiento según lo indicado"
    else:
        preset = "🔴 CONSERVADORA"
        preset_razon = "Confianza básica — usá la conservadora y no modifiques el apalancamiento"

    return {
        "rango_bajo":      rango_bajo,
        "rango_alto":      rango_alto,
        "rango_total_pct": round(rango_total_pct, 2),
        "grillas":         grillas,
        "pct_grilla":      round(pct_real_grilla, 3),
        "apalancamiento":  apalancamiento,
        "liq_largo":       liq_largo,
        "liq_corto":       liq_corto,
        "dist_liq_pct":    dist_liq_pct,
        "tiempo_1pct":     tiempo_1pct,
        "apto_intraday":   apto_intraday,
        "preset":          preset,
        "preset_razon":    preset_razon,
        "horas_num":       horas_para_1pct,
    }


# ── Análisis completo de un par ────────────────────────────
def analizar_par(par: str, btc: dict) -> dict | None:
    df15 = get_velas(par, "15m", 200)
    df1h = get_velas(par, "1h",  100)
    df4h = get_velas(par, "4h",   50)

    if df15 is None or len(df15) < 50:
        return None

    precio = df15["close"].iloc[-1]

    # ── Indicadores 15m ──
    r15   = rsi(df15["close"])
    sr15  = stoch_rsi(df15["close"])
    atr15 = atr(df15)
    bb15  = bollinger(df15["close"])
    mc15  = macd(df15["close"])
    em15  = emas(df15["close"])
    pat15 = patron_velas(df15)
    vol15 = vol_relativo(df15)
    atr_pct = (atr15 / precio) * 100

    # ── Indicadores 1h ──
    r1h = None; em1h = None; bb1h = None
    if df1h is not None:
        r1h  = rsi(df1h["close"])
        em1h = emas(df1h["close"])
        bb1h = bollinger(df1h["close"])

    # ── Indicadores 4h ──
    r4h = None; em4h = None
    if df4h is not None:
        r4h  = rsi(df4h["close"])
        em4h = emas(df4h["close"])

    # ── SCORING (max 16 puntos) ──
    score   = 0
    razones = []

    # 1. Volatilidad ATR 15m (0-2 pts)
    if atr_pct >= 0.8:
        score += 2
        razones.append(f"✅ ATR alta: {atr_pct:.2f}% — muchos cruces posibles")
    elif atr_pct >= 0.3:
        score += 1
        razones.append(f"⚡ ATR media: {atr_pct:.2f}%")
    else:
        razones.append(f"❌ ATR baja: {atr_pct:.2f}% — pocas oscilaciones")

    # 2. Bollinger 15m (0-2 pts)
    if bb15["ancho"] >= 3.0:
        score += 2
        razones.append(f"✅ Bollinger muy activo: {bb15['ancho']:.1f}%")
    elif bb15["ancho"] >= 1.5:
        score += 1
        razones.append(f"⚡ Bollinger activo: {bb15['ancho']:.1f}%")
    else:
        razones.append(f"❌ Bollinger comprimido: {bb15['ancho']:.1f}%")

    # Precio dentro del rango Bollinger (ideal para grid)
    if 0.2 <= bb15["pos"] <= 0.8:
        score += 1
        razones.append(f"✅ Precio en zona media Bollinger (ideal grid)")
    else:
        razones.append(f"⚠️ Precio en extremo Bollinger ({bb15['pos']:.2f})")

    # 3. RSI 15m (0-1 pt)
    if 35 <= r15 <= 65:
        score += 1
        razones.append(f"✅ RSI neutral: {r15:.1f} (zona de oscilación)")
    elif r15 < 35 or r15 > 65:
        score += 1
        razones.append(f"⚡ RSI extremo: {r15:.1f} (posible reversión)")

    # 4. Stoch RSI 15m (0-1 pt)
    if 20 <= sr15 <= 80:
        score += 1
        razones.append(f"✅ StochRSI neutral: {sr15:.1f}")
    else:
        razones.append(f"⚠️ StochRSI extremo: {sr15:.1f}")

    # 5. MACD 15m (0-2 pts)
    if mc15["cruce_alcista"] or mc15["cruce_bajista"]:
        score += 2
        tipo = "alcista 🟢" if mc15["cruce_alcista"] else "bajista 🔴"
        razones.append(f"✅ Cruce MACD {tipo} — señal fuerte")
    elif abs(mc15["hist"]) > 0:
        score += 1
        razones.append(f"⚡ MACD con momentum")

    # 6. EMAs 15m (0-1 pt)
    if precio > em15["ema20"] and btc["fuerza"] >= 0:
        score += 1
        razones.append(f"✅ Precio sobre EMA20 — alineado con BTC")
    elif precio < em15["ema20"] and btc["fuerza"] <= 0:
        score += 1
        razones.append(f"✅ Precio bajo EMA20 — alineado con BTC bajista")

    # 7. Patrón de velas (0-2 pts)
    patrones_alcistas = ["MARTILLO_ALCISTA","ENGULFING_ALCISTA","VELA_ALCISTA_FUERTE"]
    patrones_bajistas = ["SHOOTING_STAR_BAJISTA","ENGULFING_BAJISTA","VELA_BAJISTA_FUERTE"]
    if pat15 in patrones_alcistas and btc["fuerza"] >= 0:
        score += 2
        razones.append(f"✅ Patrón {pat15} confirmado")
    elif pat15 in patrones_bajistas and btc["fuerza"] <= 0:
        score += 2
        razones.append(f"✅ Patrón {pat15} confirmado")
    elif pat15 == "DOJI":
        score += 1
        razones.append(f"⚡ Doji — indecisión, esperar confirmación")

    # 8. Confirmación 1h (0-1 pt)
    if em1h and r1h:
        if (precio > em1h["ema20"] and btc["fuerza"] >= 0) or \
           (precio < em1h["ema20"] and btc["fuerza"] <= 0):
            score += 1
            razones.append(f"✅ 1h confirma tendencia (RSI: {r1h:.0f})")

    # 9. Confirmación 4h (0-1 pt)
    if em4h and r4h:
        if (precio > em4h["ema20"] and btc["fuerza"] >= 0) or \
           (precio < em4h["ema20"] and btc["fuerza"] <= 0):
            score += 1
            razones.append(f"✅ 4h confirma tendencia (RSI: {r4h:.0f})")

    # 10. Volumen (0-1 pt)
    if vol15 >= 1.3:
        score += 1
        razones.append(f"✅ Volumen elevado: {vol15:.1f}x")
    elif vol15 < 0.7:
        razones.append(f"❌ Volumen bajo: {vol15:.1f}x")

    # ── Probabilidad de éxito ──
    pct_score = score / 16 * 100
    if pct_score >= 70:
        probabilidad = "🟢 ALTA"
        prob_num = 3
    elif pct_score >= 44:
        probabilidad = "🟡 MEDIA"
        prob_num = 2
    else:
        probabilidad = "🔴 BÁSICA"
        prob_num = 1

    # ── Dirección del grid ──
    if btc["fuerza"] >= 2 and r15 < 60:
        direccion = "📈 LARGO"
    elif btc["fuerza"] <= -2 and r15 > 40:
        direccion = "📉 CORTO"
    elif 40 <= r15 <= 60:
        direccion = "↔️ NEUTRAL (largo+corto)"
    elif r15 < 40:
        direccion = "📈 LARGO"
    else:
        direccion = "📉 CORTO"

    # ── Cálculo de grillas ──
    grid = calcular_grid(precio, atr15, atr_pct, score, btc["fuerza"])

    if not grid["apto_intraday"] or score < MIN_SCORE:
        return None

    return {
        "par":         par,
        "precio":      precio,
        "score":       score,
        "score_max":   16,
        "pct_score":   pct_score,
        "probabilidad": probabilidad,
        "prob_num":    prob_num,
        "direccion":   direccion,
        "razones":     razones,
        "patron":      pat15,
        "atr_pct":     atr_pct,
        **grid,
    }


# ── Generar alertas ────────────────────────────────────────
def generar_alertas():
    ahora = datetime.now().strftime("%H:%M")
    print(f"\n[{ahora}] Analizando {len(PARES)} pares...")

    btc = tendencia_btc()
    print(f"  BTC: {btc['resumen']} {btc['emoji']} fuerza={btc['fuerza']}")

    resultados = []
    for par in PARES:
        r = analizar_par(par, btc)
        if r:
            resultados.append(r)
        time.sleep(0.15)

    resultados.sort(key=lambda x: (-x["prob_num"], -x["score"], x["horas_num"]))

    if not resultados:
        print(f"[{ahora}] Sin señales intraday.")
        enviar_telegram(
            f"📊 <b>Análisis {ahora}</b>\n"
            f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
            f"Sin señales intraday en este ciclo. Próximo en 30 min."
        )
        return

    enviadas = 0
    for r in resultados[:MAX_ALERTAS]:
        clave = f"{r['par']}_{datetime.now().strftime('%Y%m%d_%H')}"
        if clave in alertas_enviadas:
            continue
        alertas_enviadas[clave] = True

        msg = (
            f"🚨 <b>SEÑAL GRID INTRADAY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Par: <b>{r['par']}</b>\n"
            f"💰 Precio: <b>{r['precio']:.4f} USDT</b>\n"
            f"🎯 Dirección: <b>{r['direccion']}</b>\n"
            f"📊 Score: {r['score']}/{r['score_max']} ({r['pct_score']:.0f}%)\n"
            f"🎰 Probabilidad: <b>{r['probabilidad']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 BTC: {btc['emoji']} {btc['resumen']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Configuración Grid:</b>\n"
            f"   Preset base: <b>{r['preset']}</b>\n"
            f"   → {r['preset_razon']}\n\n"
            f"   Rango bajo:   <b>{r['rango_bajo']} USDT</b>\n"
            f"   Rango alto:   <b>{r['rango_alto']} USDT</b>\n"
            f"   Amplitud:     {r['rango_total_pct']}%\n"
            f"   Grillas:      <b>{r['grillas']}</b> (~{r['pct_grilla']}% c/u)\n"
            f"   Apalancamiento: <b>{r['apalancamiento']}x</b>\n\n"
            f"   Liq. estimada (largo): {r['liq_largo']} USDT\n"
            f"   Liq. estimada (corto): {r['liq_corto']} USDT\n"
            f"   Margen seguridad: {r['dist_liq_pct']:.1f}% bajo el rango\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Tiempo est. al 1%: <b>{r['tiempo_1pct']}</b>\n"
            f"   (2-3 cruces de grilla de {r['pct_grilla']}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Análisis técnico:</b>\n"
            + "\n".join(f"   {s}" for s in r["razones"][:8]) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {ahora}"
        )
        enviar_telegram(msg)
        enviadas += 1
        print(f"  ✅ {r['par']} score={r['score']} prob={r['probabilidad']}")

    print(f"[{ahora}] {enviadas} alertas de {len(resultados)} candidatos.")


# ── Resumen matutino ───────────────────────────────────────
def resumen_matutino():
    hoy = datetime.now().strftime("%Y%m%d")
    if resumen_enviado.get(hoy):
        return
    resumen_enviado[hoy] = True

    btc = tendencia_btc()
    candidatos = []
    for par in PARES:
        r = analizar_par(par, btc)
        if r:
            candidatos.append(r)
        time.sleep(0.15)

    candidatos.sort(key=lambda x: (-x["prob_num"], -x["score"]))
    top3 = candidatos[:3]

    if not top3:
        enviar_telegram(
            f"☀️ <b>Buenos días Juanjo!</b>\n"
            f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
            f"Mercado tranquilo. Te aviso cuando haya oportunidades."
        )
        return

    lineas = [
        f"☀️ <b>RESUMEN MATUTINO — {datetime.now().strftime('%d/%m/%Y')}</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🌐 BTC: {btc['emoji']} <b>{btc['resumen']}</b> (${btc['precio']:,.0f})",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🏆 <b>Mejores pares para hoy:</b>",
    ]
    for i, r in enumerate(top3, 1):
        lineas.append(
            f"\n{i}. <b>{r['par']}</b> — {r['probabilidad']}\n"
            f"   Dirección: {r['direccion']}\n"
            f"   Score: {r['score']}/{r['score_max']}\n"
            f"   Preset: {r['preset']}\n"
            f"   Grillas: {r['grillas']} | Apal: {r['apalancamiento']}x\n"
            f"   Tiempo est. al 1%: {r['tiempo_1pct']}"
        )
    lineas += [
        f"\n━━━━━━━━━━━━━━━━━━━━",
        f"🔔 Alertas cada 30 min con configuración exacta.",
    ]
    enviar_telegram("\n".join(lineas))


# ── Main ───────────────────────────────────────────────────
def main():
    print(f"🤖 Bot v5 iniciado — {len(PARES)} pares")
    enviar_telegram(
        f"🤖 <b>JJ Cripto Bot v5 iniciado</b>\n"
        f"📊 {len(PARES)} pares | 15m+1h+4h+1d\n"
        f"🎯 Grid intraday con probabilidad, preset Pionex y grillas optimizadas."
    )
    schedule.every(30).minutes.do(generar_alertas)
    schedule.every().day.at(HORA_RESUMEN).do(resumen_matutino)
    generar_alertas()
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
