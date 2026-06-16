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
    "TRXUSDT","AAVEUSDT","ALGOUSDT","ICPUSDT","AXSUSDT",
    "SANDUSDT","MANAUSDT","GALAUSDT","FTMUSDT","NEARUSDT",
    "EGLDUSDT","CHZUSDT","CRVUSDT","RUNEUSDT","HBARUSDT",
    "OPUSDT","ARBUSDT","INJUSDT","SUIUSDT","WLDUSDT",
    "TIAUSOUT","STXUSDT","LDOUSDT","SEIUSDT","FETUSDT",
    "GRTUSDT","1000SHIBUSDT","1000PEPEUSDT","WIFUSDT","FLOKIUSDT",
    "ENAUSDT","TIAUSDT","NOTUSDT","TAOUSDT","MEMEUSDT",
    "ORDIUSDT","SATSUSDT","1000BONKUSDT","ACEUSDT","ALTUSDT",
]

MIN_SCORE    = 3
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
def get_velas(par: str, tf: str, n: int = 100) -> pd.DataFrame | None:
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={par}&interval={tf}&limit={n}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 20:
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

def get_precio(par: str) -> float | None:
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={par}", timeout=5)
        return float(r.json()["price"])
    except:
        return None


# ── Indicadores ────────────────────────────────────────────
def calc_rsi(s: pd.Series, p=14) -> float:
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    rs = g / l.replace(0, np.nan)
    return float((100 - 100/(1+rs)).iloc[-1])

def calc_atr(df: pd.DataFrame, p=14) -> float:
    hl  = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift()).abs()
    lcp = (df["low"]  - df["close"].shift()).abs()
    return float(pd.concat([hl,hcp,lcp],axis=1).max(axis=1).rolling(p).mean().iloc[-1])

def calc_bb(s: pd.Series, p=20) -> dict:
    m  = s.rolling(p).mean()
    st = s.rolling(p).std()
    up = (m + 2*st).iloc[-1]
    dn = (m - 2*st).iloc[-1]
    mid = m.iloc[-1]
    ancho = (up - dn) / mid * 100 if mid > 0 else 0
    pos   = (s.iloc[-1] - dn) / (up - dn) if (up - dn) > 0 else 0.5
    return {"upper": up, "lower": dn, "mid": mid, "ancho": ancho, "pos": pos}

def calc_macd(s: pd.Series) -> dict:
    m  = s.ewm(span=12).mean() - s.ewm(span=26).mean()
    sg = m.ewm(span=9).mean()
    return {
        "macd": float(m.iloc[-1]),
        "signal": float(sg.iloc[-1]),
        "hist": float((m-sg).iloc[-1]),
        "cruce_alc": bool(m.iloc[-1] > sg.iloc[-1] and m.iloc[-2] <= sg.iloc[-2]),
        "cruce_baj": bool(m.iloc[-1] < sg.iloc[-1] and m.iloc[-2] >= sg.iloc[-2]),
    }

def calc_ema(s: pd.Series, p: int) -> float:
    return float(s.ewm(span=p).mean().iloc[-1])

def calc_stoch_rsi(s: pd.Series, p=14) -> float:
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    rsi = 100 - 100/(1 + g/l.replace(0,np.nan))
    mn = rsi.rolling(p).min()
    mx = rsi.rolling(p).max()
    stoch = (rsi - mn) / (mx - mn + 1e-10) * 100
    return float(stoch.iloc[-1])

def patron_vela(df: pd.DataFrame) -> str:
    c, o = df["close"].iloc[-1], df["open"].iloc[-1]
    h, l = df["high"].iloc[-1], df["low"].iloc[-1]
    c1, o1 = df["close"].iloc[-2], df["open"].iloc[-2]
    rng = h - l
    if rng == 0: return "NEUTRO"
    cuerpo = abs(c - o)
    mecha_inf = min(c,o) - l
    mecha_sup = h - max(c,o)
    if cuerpo/rng < 0.1: return "DOJI"
    if mecha_inf > 2*cuerpo and c > o and c1 < o1: return "MARTILLO_ALC"
    if mecha_sup > 2*cuerpo and c < o and c1 > o1: return "SHOOTING_BAJ"
    if c > o and c > o1 and o < c1 and c1 < o1: return "ENGULFING_ALC"
    if c < o and c < o1 and o > c1 and c1 > o1: return "ENGULFING_BAJ"
    if c > o and cuerpo/rng > 0.6: return "VELA_ALC"
    if c < o and cuerpo/rng > 0.6: return "VELA_BAJ"
    return "NEUTRO"


# ── Análisis BTC: tendencia + estado actual ────────────────
def analizar_btc() -> dict:
    precio_btc = get_precio("BTCUSDT") or 0
    fuerza = 0
    detalle = []
    estado_btc = "LATERAL"  # SUBIO_RANGEA, BAJO_RANGEA, LATERAL
    movimiento_pct = 0.0

    df1d = get_velas("BTCUSDT", "1d", 50)
    df4h = get_velas("BTCUSDT", "4h", 100)
    df1h = get_velas("BTCUSDT", "1h", 100)

    # ── Tendencia macro (diario) ──
    if df1d is not None and len(df1d) >= 50:
        p = df1d["close"].iloc[-1]
        e20 = calc_ema(df1d["close"], 20)
        e50 = calc_ema(df1d["close"], 50)
        r1d = calc_rsi(df1d["close"])
        if p > e20 > e50:
            fuerza += 2; detalle.append(f"📈 Diario alcista (RSI:{r1d:.0f})")
        elif p < e20 < e50:
            fuerza -= 2; detalle.append(f"📉 Diario bajista (RSI:{r1d:.0f})")
        else:
            detalle.append(f"↔️ Diario lateral (RSI:{r1d:.0f})")

    # ── Tendencia 4h ──
    if df4h is not None and len(df4h) >= 20:
        p4 = df4h["close"].iloc[-1]
        e20_4h = calc_ema(df4h["close"], 20)
        r4h = calc_rsi(df4h["close"])
        if p4 > e20_4h:
            fuerza += 1; detalle.append(f"📈 4h alcista (RSI:{r4h:.0f})")
        else:
            fuerza -= 1; detalle.append(f"📉 4h bajista (RSI:{r4h:.0f})")

    # ── Estado actual 1h: detectar post-movimiento ──
    if df1h is not None and len(df1h) >= 16:
        # Precio hace 8 horas vs ahora
        precio_8h_atras = df1h["close"].iloc[-9]
        precio_ahora    = df1h["close"].iloc[-1]
        movimiento_pct  = (precio_ahora - precio_8h_atras) / precio_8h_atras * 100

        # ATR últimas 4 velas vs ATR anterior (¿está bajando la volatilidad?)
        atr_reciente = calc_atr(df1h.tail(8))
        atr_anterior = calc_atr(df1h.iloc[-16:-8])
        atr_bajando  = atr_reciente < atr_anterior * 0.85

        # Volumen últimas 4h vs promedio
        vol_reciente = df1h["vol"].iloc[-4:].mean()
        vol_promedio = df1h["vol"].iloc[-20:-4].mean()
        vol_alto     = vol_reciente > vol_promedio * 1.3

        if movimiento_pct >= 1.0 and atr_bajando:
            estado_btc = "SUBIO_RANGEA"
            fuerza += 2
            detalle.append(f"🚀 BTC subió {movimiento_pct:.1f}% y ahora RANGEA → LARGO en altcoins")
        elif movimiento_pct <= -1.0 and atr_bajando:
            estado_btc = "BAJO_RANGEA"
            fuerza -= 2
            detalle.append(f"💥 BTC bajó {movimiento_pct:.1f}% y ahora RANGEA → CORTO en altcoins")
        elif abs(movimiento_pct) < 1.0:
            estado_btc = "LATERAL"
            detalle.append(f"↔️ BTC lateral ({movimiento_pct:.1f}%) → Grid NEUTRAL válido")
        else:
            estado_btc = "EN_MOVIMIENTO"
            detalle.append(f"⚠️ BTC en movimiento activo ({movimiento_pct:.1f}%) → esperar")

        p1 = df1h["close"].iloc[-1]
        e20_1h = calc_ema(df1h["close"], 20)
        r1h = calc_rsi(df1h["close"])
        if p1 > e20_1h:
            fuerza += 1; detalle.append(f"📈 1h sobre EMA20 (RSI:{r1h:.0f})")
        else:
            fuerza -= 1; detalle.append(f"📉 1h bajo EMA20 (RSI:{r1h:.0f})")

    if fuerza >= 3:   emoji, resumen = "🚀", "ALCISTA FUERTE"
    elif fuerza >= 1: emoji, resumen = "📈", "ALCISTA"
    elif fuerza <= -3: emoji, resumen = "💥", "BAJISTA FUERTE"
    elif fuerza <= -1: emoji, resumen = "📉", "BAJISTA"
    else:             emoji, resumen = "↔️", "LATERAL"

    return {
        "emoji": emoji, "resumen": resumen, "fuerza": fuerza,
        "precio": precio_btc, "detalle": detalle,
        "estado": estado_btc, "mov_pct": movimiento_pct,
    }


# ── Grid óptimo ────────────────────────────────────────────
def calcular_grid(precio: float, atr_pct: float, atr_val: float,
                  score: int, direccion: str) -> dict:
    rango_pct  = atr_pct * 3
    rango_bajo = round(precio * (1 - rango_pct/100), 6)
    rango_alto = round(precio * (1 + rango_pct/100), 6)

    pct_grilla_obj = 0.33
    grillas = max(10, min(150, int(rango_pct / pct_grilla_obj)))
    pct_grilla = round(rango_pct / grillas, 3)

    max_apal = max(2, min(20, int(precio / max(precio - rango_bajo * 0.80, 0.0001))))
    if atr_pct >= 1.5:   apal = min(3, max_apal)
    elif atr_pct >= 0.8: apal = min(5, max_apal)
    elif atr_pct >= 0.4: apal = min(7, max_apal)
    else:                apal = min(10, max_apal)

    liq_largo = round(precio * (1 - 1/apal), 6)
    liq_corto = round(precio * (1 + 1/apal), 6)
    dist_liq  = round((rango_bajo - liq_largo) / precio * 100, 2)

    cruces_hora = (atr_pct * 4) / pct_grilla if pct_grilla > 0 else 0.1
    horas_1pct  = 3 / cruces_hora if cruces_hora > 0 else 99
    if horas_1pct < 1:    tiempo_1pct = f"{int(horas_1pct*60)} min"
    elif horas_1pct < 8:  tiempo_1pct = f"{horas_1pct:.1f} hs"
    else:                 tiempo_1pct = "+8 hs"

    # ── Potencial de ganancia mayor al 1% ──
    ganancia_potencial_pct = round(cruces_hora * 8 * pct_grilla / apal, 2)  # en 8h operativas
    supera_1pct = ganancia_potencial_pct > 1.5

    # Stop loss: 10% más allá del rango (antes de liquidación)
    sl_largo = round(rango_bajo * 0.97, 6)
    sl_corto = round(rango_alto * 1.03, 6)

    # Trailing profit: cerrar el bot cuando la ganancia acumulada supere este %
    trailing = round(min(ganancia_potencial_pct * 0.7, 5.0), 2)

    if score >= 10:   preset, p_razon = "🟢 AGRESIVA", "Alta confianza"
    elif score >= 6:  preset, p_razon = "🟡 BALANCEADA", "Confianza media"
    else:             preset, p_razon = "🔴 CONSERVADORA", "Confianza básica"

    return {
        "rango_bajo": rango_bajo, "rango_alto": rango_alto,
        "rango_pct": round(rango_pct,2), "grillas": grillas,
        "pct_grilla": pct_grilla, "apal": apal,
        "liq_largo": liq_largo, "liq_corto": liq_corto,
        "dist_liq": dist_liq, "tiempo_1pct": tiempo_1pct,
        "horas_1pct": horas_1pct, "apto": horas_1pct <= 8,
        "ganancia_pot": ganancia_potencial_pct,
        "supera_1pct": supera_1pct,
        "sl_largo": sl_largo, "sl_corto": sl_corto,
        "trailing": trailing,
        "preset": preset, "p_razon": p_razon,
    }


# ── Análisis de par ────────────────────────────────────────
def analizar_par(par: str, btc: dict) -> dict | None:
    # Saltar si BTC está en movimiento activo sin rangear
    if btc["estado"] == "EN_MOVIMIENTO":
        return None

    df15 = get_velas(par, "15m", 100)
    df1h = get_velas(par, "1h",  100)

    if df15 is None or len(df15) < 30:
        return None

    precio = float(df15["close"].iloc[-1])
    if precio <= 0:
        return None

    atr15   = calc_atr(df15)
    atr_pct = (atr15 / precio) * 100
    bb15    = calc_bb(df15["close"])
    rsi15   = calc_rsi(df15["close"])
    sr15    = calc_stoch_rsi(df15["close"])
    mc15    = calc_macd(df15["close"])
    e20_15  = calc_ema(df15["close"], 20)
    e50_15  = calc_ema(df15["close"], 50)
    pat     = patron_vela(df15)
    vol_r   = float(df15["vol"].iloc[-1]) / max(float(df15["vol"].iloc[-21:-1].mean()), 0.0001)

    score   = 0
    razones = []

    # 1. Volatilidad ATR (0-2)
    if atr_pct >= 0.8:
        score += 2; razones.append(f"✅ Volatilidad alta: {atr_pct:.2f}%")
    elif atr_pct >= 0.2:
        score += 1; razones.append(f"⚡ Volatilidad media: {atr_pct:.2f}%")
    else:
        razones.append(f"❌ Volatilidad baja: {atr_pct:.2f}%")

    # 2. Bollinger (0-2)
    if bb15["ancho"] >= 3.0:
        score += 2; razones.append(f"✅ Bollinger muy activo: {bb15['ancho']:.1f}%")
    elif bb15["ancho"] >= 1.0:
        score += 1; razones.append(f"⚡ Bollinger activo: {bb15['ancho']:.1f}%")
    else:
        razones.append(f"❌ Bollinger comprimido: {bb15['ancho']:.1f}%")

    # 3. Posición Bollinger ideal para grid (0-1)
    if 0.15 <= bb15["pos"] <= 0.85:
        score += 1; razones.append(f"✅ Precio en zona grid")

    # 4. RSI (0-1)
    if 30 <= rsi15 <= 70:
        score += 1; razones.append(f"✅ RSI neutro: {rsi15:.1f}")
    else:
        score += 1; razones.append(f"⚡ RSI extremo: {rsi15:.1f} (oportunidad)")

    # 5. StochRSI (0-1)
    if 20 <= sr15 <= 80:
        score += 1; razones.append(f"✅ StochRSI neutro: {sr15:.1f}")

    # 6. MACD (0-2)
    if mc15["cruce_alc"] or mc15["cruce_baj"]:
        score += 2
        tipo = "alcista 🟢" if mc15["cruce_alc"] else "bajista 🔴"
        razones.append(f"✅ Cruce MACD {tipo}")
    elif abs(mc15["hist"]) > 0:
        score += 1; razones.append(f"⚡ MACD con momentum")

    # 7. Alineación con estado BTC (0-2) — CLAVE
    if btc["estado"] == "SUBIO_RANGEA":
        if precio > e20_15:
            score += 2; razones.append(f"✅ Alineado: BTC rangeando post-suba → LARGO")
        else:
            score += 1; razones.append(f"⚡ BTC rangeando post-suba (precio bajo EMA20)")
    elif btc["estado"] == "BAJO_RANGEA":
        if precio < e20_15:
            score += 2; razones.append(f"✅ Alineado: BTC rangeando post-baja → CORTO")
        else:
            score += 1; razones.append(f"⚡ BTC rangeando post-baja (precio sobre EMA20)")
    elif btc["estado"] == "LATERAL":
        score += 1; razones.append(f"✅ BTC lateral → Grid neutral válido")

    # 8. Patrón de vela (0-2)
    if pat in ["MARTILLO_ALC","ENGULFING_ALC","VELA_ALC"] and btc["fuerza"] >= 0:
        score += 2; razones.append(f"✅ Patrón alcista: {pat}")
    elif pat in ["SHOOTING_BAJ","ENGULFING_BAJ","VELA_BAJ"] and btc["fuerza"] <= 0:
        score += 2; razones.append(f"✅ Patrón bajista: {pat}")
    elif pat == "DOJI":
        score += 1; razones.append(f"⚡ Doji — zona de reversión")

    # 9. Confirmación 1h (0-1)
    if df1h is not None and len(df1h) >= 20:
        e20_1h = calc_ema(df1h["close"], 20)
        r1h    = calc_rsi(df1h["close"])
        if (precio > e20_1h and btc["fuerza"] >= 0) or (precio < e20_1h and btc["fuerza"] <= 0):
            score += 1; razones.append(f"✅ 1h confirma (RSI:{r1h:.0f})")

    # 10. Volumen (0-1)
    if vol_r >= 1.2:
        score += 1; razones.append(f"✅ Volumen elevado: {vol_r:.1f}x")
    elif vol_r >= 0.7:
        score += 1; razones.append(f"⚡ Volumen normal: {vol_r:.1f}x")

    # Probabilidad
    pct = score / 15 * 100
    if pct >= 65:   prob, prob_n = "🟢 ALTA", 3
    elif pct >= 40: prob, prob_n = "🟡 MEDIA", 2
    else:           prob, prob_n = "🔴 BÁSICA", 1

    # Dirección según estado BTC
    if btc["estado"] == "SUBIO_RANGEA":
        direccion = "📈 LARGO"
    elif btc["estado"] == "BAJO_RANGEA":
        direccion = "📉 CORTO"
    else:
        if rsi15 <= 45 and mc15["macd"] > mc15["signal"]:
            direccion = "📈 LARGO"
        elif rsi15 >= 55 and mc15["macd"] < mc15["signal"]:
            direccion = "📉 CORTO"
        else:
            direccion = "↔️ NEUTRAL (largo+corto)"

    if score < MIN_SCORE:
        return None

    grid = calcular_grid(precio, atr_pct, atr15, score, direccion)

    if not grid["apto"]:
        return None

    return {
        "par": par, "precio": precio,
        "score": score, "score_max": 15, "pct": pct,
        "prob": prob, "prob_n": prob_n,
        "direccion": direccion, "razones": razones,
        "atr_pct": atr_pct, **grid,
    }


# ── Generar alertas ────────────────────────────────────────
def generar_alertas():
    ahora = datetime.now().strftime("%H:%M")
    print(f"\n[{ahora}] Analizando {len(PARES)} pares...")

    btc = analizar_btc()
    print(f"  BTC: {btc['resumen']} {btc['emoji']} ${btc['precio']:,.0f} estado={btc['estado']}")

    if btc["estado"] == "EN_MOVIMIENTO":
        enviar_telegram(
            f"⚠️ <b>BTC en movimiento activo {ahora}</b>\n"
            f"Movimiento: {btc['mov_pct']:.1f}% en últimas 8h\n"
            f"Esperando que BTC rangee antes de operar grid.\n"
            f"Próximo análisis en 30 min."
        )
        return

    resultados = []
    for par in PARES:
        try:
            r = analizar_par(par, btc)
            if r:
                resultados.append(r)
        except Exception as e:
            print(f"  Error {par}: {e}")
        time.sleep(0.1)

    resultados.sort(key=lambda x: (-x["prob_n"], -x["score"], x["horas_1pct"]))

    if not resultados:
        enviar_telegram(
            f"📊 <b>Análisis {ahora}</b>\n"
            f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
            f"Estado: {btc['estado']}\n"
            f"Sin señales con score suficiente. Próximo en 30 min."
        )
        return

    enviadas = 0
    for r in resultados[:MAX_ALERTAS]:
        clave = f"{r['par']}_{datetime.now().strftime('%Y%m%d_%H')}"
        if clave in alertas_enviadas:
            continue
        alertas_enviadas[clave] = True

        # Bloque de potencial extendido
        extra = ""
        if r["supera_1pct"]:
            extra = (
                f"\n🔥 <b>POTENCIAL MAYOR AL 1%</b>\n"
                f"   Ganancia estimada en 8h: <b>{r['ganancia_pot']}%</b>\n"
                f"   Stop Loss Largo:  {r['sl_largo']} USDT\n"
                f"   Stop Loss Corto:  {r['sl_corto']} USDT\n"
                f"   Trailing Profit:  cerrar bot al <b>{r['trailing']}%</b> de ganancia\n"
            )

        msg = (
            f"🚨 <b>SEÑAL GRID INTRADAY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Par: <b>{r['par']}</b>\n"
            f"💰 Precio: <b>{r['precio']:.6g} USDT</b>\n"
            f"🎯 Dirección: <b>{r['direccion']}</b>\n"
            f"📊 Score: {r['score']}/{r['score_max']} ({r['pct']:.0f}%)\n"
            f"🎰 Probabilidad: <b>{r['prob']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
            f"   Estado: <b>{btc['estado']}</b> | Mov: {btc['mov_pct']:.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Configuración Grid Pionex:</b>\n"
            f"   Preset base: <b>{r['preset']}</b> — {r['p_razon']}\n\n"
            f"   Rango bajo:  <b>{r['rango_bajo']}</b> USDT\n"
            f"   Rango alto:  <b>{r['rango_alto']}</b> USDT\n"
            f"   Amplitud:    {r['rango_pct']}%\n"
            f"   Grillas:     <b>{r['grillas']}</b> (~{r['pct_grilla']}% c/u)\n"
            f"   Apalancamiento: <b>{r['apal']}x</b>\n\n"
            f"   Liq. Largo: {r['liq_largo']} USDT\n"
            f"   Liq. Corto: {r['liq_corto']} USDT\n"
            f"   Margen seg.: {r['dist_liq']}% bajo rango\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Tiempo est. al 1%: <b>{r['tiempo_1pct']}</b>\n"
            f"   (3 cruces × {r['pct_grilla']}% por grilla)"
            + extra +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Señales técnicas:</b>\n"
            + "\n".join(f"   {s}" for s in r["razones"][:8]) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {ahora}"
        )
        enviar_telegram(msg)
        enviadas += 1
        print(f"  ✅ {r['par']} score={r['score']} prob={r['prob']} pot={r['ganancia_pot']}%")

    print(f"[{ahora}] {enviadas} alertas de {len(resultados)} candidatos.")


# ── Resumen matutino ───────────────────────────────────────
def resumen_matutino():
    hoy = datetime.now().strftime("%Y%m%d")
    if resumen_enviado.get(hoy):
        return
    resumen_enviado[hoy] = True

    btc = analizar_btc()
    candidatos = []
    for par in PARES:
        try:
            r = analizar_par(par, btc)
            if r:
                candidatos.append(r)
        except:
            pass
        time.sleep(0.1)

    candidatos.sort(key=lambda x: (-x["prob_n"], -x["score"]))
    top3 = candidatos[:3]

    lineas = [
        f"☀️ <b>RESUMEN MATUTINO {datetime.now().strftime('%d/%m/%Y')}</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🌐 BTC: {btc['emoji']} <b>{btc['resumen']}</b> (${btc['precio']:,.0f})",
        f"Estado: <b>{btc['estado']}</b> | Mov 8h: {btc['mov_pct']:.1f}%",
        "\n".join(btc["detalle"][:3]),
        f"━━━━━━━━━━━━━━━━━━━━",
    ]

    if not top3:
        lineas.append("Mercado sin señales claras al inicio del día.")
    else:
        lineas.append(f"🏆 <b>Top 3 pares para hoy:</b>")
        for i, r in enumerate(top3, 1):
            pot = f" | 🔥 Pot: {r['ganancia_pot']}%" if r["supera_1pct"] else ""
            lineas.append(
                f"\n{i}. <b>{r['par']}</b> — {r['prob']}\n"
                f"   {r['direccion']} | Score: {r['score']}/{r['score_max']}{pot}\n"
                f"   Preset: {r['preset']} | {r['apal']}x\n"
                f"   Grillas: {r['grillas']} | Tiempo al 1%: {r['tiempo_1pct']}"
            )

    lineas += ["\n━━━━━━━━━━━━━━━━━━━━", "🔔 Alertas cada 30 min."]
    enviar_telegram("\n".join(lineas))


# ── Main ───────────────────────────────────────────────────
def main():
    print(f"🤖 Bot v6 iniciado — {len(PARES)} pares")
    enviar_telegram(
        f"🤖 <b>JJ Cripto Bot v6 iniciado</b>\n"
        f"📊 {len(PARES)} pares | 15m+1h+4h+1d\n"
        f"🚀 Detector BTC post-movimiento 1%+\n"
        f"💰 Alertas de potencial >1% con SL y trailing profit."
    )
    schedule.every(30).minutes.do(generar_alertas)
    schedule.every().day.at(HORA_RESUMEN).do(resumen_matutino)
    generar_alertas()
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
