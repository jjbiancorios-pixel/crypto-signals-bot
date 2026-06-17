import requests
import pandas as pd
import numpy as np
import time
import schedule
from datetime import datetime, timezone, timedelta
import os

# ── Configuración ──────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8761617567:AAGbH0Vgb-13kVZppZ-fwZHT6QngI8ZkYOo")
CHAT_ID        = os.environ.get("CHAT_ID", "674187707")
TZ_ARG         = timezone(timedelta(hours=-3))

PARES = [
    "ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT",
    "ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT","MATICUSDT",
    "LTCUSDT","UNIUSDT","ATOMUSDT","ETCUSDT","XLMUSDT",
    "TRXUSDT","AAVEUSDT","ALGOUSDT","ICPUSDT","AXSUSDT",
    "SANDUSDT","MANAUSDT","GALAUSDT","FTMUSDT","NEARUSDT",
    "EGLDUSDT","CHZUSDT","CRVUSDT","RUNEUSDT","HBARUSDT",
    "OPUSDT","ARBUSDT","INJUSDT","SUIUSDT","WLDUSDT",
    "STXUSDT","LDOUSDT","SEIUSDT","FETUSDT","GRTUSDT",
    "1000SHIBUSDT","1000PEPEUSDT","WIFUSDT","FLOKIUSDT",
    "ENAUSDT","TIAUSDT","NOTUSDT","TAOUSDT","MEMEUSDT",
    "ORDIUSDT","1000BONKUSDT","ACEUSDT","ALTUSDT","PORTALUSDT",
]

MIN_SCORE       = 3
MAX_ALERTAS     = 5
HORA_RESUMEN    = "09:03"
HORA_INICIO     = 7    # No operar antes de las 7 ARG
HORA_FIN        = 24   # No operar después de las 24 ARG
OBJETIVO_DIARIO = 3    # % diario objetivo

alertas_enviadas  = {}
resumen_enviado   = {}
señales_del_dia   = {}   # {fecha: [lista de señales]}
operaciones_abiertas = {}  # {par+hora: datos para alerta de cierre}


# ── Utilidades ─────────────────────────────────────────────
def hora_arg() -> str:
    return datetime.now(TZ_ARG).strftime("%H:%M")

def fecha_arg() -> str:
    return datetime.now(TZ_ARG).strftime("%d/%m/%Y")

def hoy_arg() -> str:
    return datetime.now(TZ_ARG).strftime("%Y%m%d")

def hora_num() -> int:
    return datetime.now(TZ_ARG).hour

def en_horario_operativo() -> bool:
    h = hora_num()
    return HORA_INICIO <= h < HORA_FIN

def score_minimo_horario() -> int:
    """Más exigente en horarios de bajo volumen"""
    h = hora_num()
    if 9 <= h <= 12 or 14 <= h <= 18:
        return MIN_SCORE      # Horario prime → menos exigente
    elif 7 <= h <= 9 or 18 <= h <= 21:
        return MIN_SCORE + 1  # Horario normal
    else:
        return MIN_SCORE + 2  # Horario bajo volumen (tarde noche)


# ── Telegram ───────────────────────────────────────────────
def enviar_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


# ── Datos: cascada Bybit → OKX → Binance Vision ────────────
BYBIT_TF = {"15m":"15","1h":"60","4h":"240","1d":"D"}
OKX_TF   = {"15m":"15m","1h":"1H","4h":"4H","1d":"1Dutc"}
OKX_PAR  = lambda p: p.replace("1000SHIB","SHIB").replace("1000PEPE","PEPE").replace("1000BONK","BONK").replace("USDT","-USDT")
BINANCE_TF = {"15m":"15m","1h":"1h","4h":"4h","1d":"1d"}

def _velas_bybit(par, tf, n):
    intervalo = BYBIT_TF.get(tf, "15")
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={par}&interval={intervalo}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if data.get("retCode") != 0:
        raise ValueError("bybit fail")
    rows = data["result"]["list"]
    if not rows or len(rows) < 20:
        raise ValueError("bybit empty")
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","turnover"])
    for c in ["open","high","low","close","vol"]:
        df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)

def _velas_okx(par, tf, n):
    intervalo = OKX_TF.get(tf, "15m")
    inst = OKX_PAR(par)
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={intervalo}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    rows = data.get("data", [])
    if not rows or len(rows) < 20:
        raise ValueError("okx empty")
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","volCcy","volCcyQuote","confirm"])
    for c in ["open","high","low","close","vol"]:
        df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)

def _velas_binance(par, tf, n):
    intervalo = BINANCE_TF.get(tf, "15m")
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={par}&interval={intervalo}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if not isinstance(data, list) or len(data) < 20:
        raise ValueError("binance empty")
    df = pd.DataFrame(data, columns=[
        "ts","open","high","low","close","vol","ct","qav","trades","tbbav","tbqav","ignore"])
    for c in ["open","high","low","close","vol"]:
        df[c] = df[c].astype(float)
    return df

def get_velas(par: str, tf: str, n: int = 100) -> pd.DataFrame | None:
    for fuente in (_velas_bybit, _velas_okx, _velas_binance):
        try:
            df = fuente(par, tf, n)
            if df is not None and len(df) >= 20:
                return df
        except Exception:
            continue
    return None

def _precio_bybit(par):
    url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={par}"
    r = requests.get(url, timeout=6)
    data = r.json()
    if data.get("retCode") != 0:
        raise ValueError("bybit fail")
    return float(data["result"]["list"][0]["lastPrice"])

def _precio_okx(par):
    inst = OKX_PAR(par)
    url = f"https://www.okx.com/api/v5/market/ticker?instId={inst}"
    r = requests.get(url, timeout=6)
    data = r.json()
    rows = data.get("data", [])
    if not rows:
        raise ValueError("okx empty")
    return float(rows[0]["last"])

def _precio_binance(par):
    url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={par}"
    r = requests.get(url, timeout=6)
    return float(r.json()["price"])

def get_precio(par: str) -> float | None:
    for fuente in (_precio_bybit, _precio_okx, _precio_binance):
        try:
            p = fuente(par)
            if p and p > 0:
                return p
        except Exception:
            continue
    return None


# ── Indicadores ────────────────────────────────────────────
def calc_rsi(s: pd.Series, p=14) -> float:
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return float((100 - 100/(1 + g/l.replace(0,np.nan))).iloc[-1])

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
        "macd": float(m.iloc[-1]), "signal": float(sg.iloc[-1]),
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
    mn, mx = rsi.rolling(p).min(), rsi.rolling(p).max()
    return float(((rsi - mn) / (mx - mn + 1e-10) * 100).iloc[-1])

def patron_vela(df: pd.DataFrame) -> str:
    c, o = df["close"].iloc[-1], df["open"].iloc[-1]
    h, l = df["high"].iloc[-1], df["low"].iloc[-1]
    c1, o1 = df["close"].iloc[-2], df["open"].iloc[-2]
    rng = h - l
    if rng == 0: return "NEUTRO"
    cuerpo = abs(c - o)
    mi = min(c,o) - l
    ms = h - max(c,o)
    if cuerpo/rng < 0.1: return "DOJI"
    if mi > 2*cuerpo and c > o and c1 < o1: return "MARTILLO_ALC"
    if ms > 2*cuerpo and c < o and c1 > o1: return "SHOOTING_BAJ"
    if c > o and c > o1 and o < c1 and c1 < o1: return "ENGULFING_ALC"
    if c < o and c < o1 and o > c1 and c1 > o1: return "ENGULFING_BAJ"
    if c > o and cuerpo/rng > 0.6: return "VELA_ALC"
    if c < o and cuerpo/rng > 0.6: return "VELA_BAJ"
    return "NEUTRO"

def correlacion_propia(df15: pd.DataFrame, btc_mov: float) -> dict:
    """Detecta si el par tiene movimiento propio independiente de BTC"""
    mov_propio = (df15["close"].iloc[-1] - df15["close"].iloc[-4]) / df15["close"].iloc[-4] * 100
    diverge = abs(mov_propio - btc_mov) > 1.0
    return {"mov_propio": round(mov_propio, 2), "diverge": diverge}


# ── Análisis BTC ───────────────────────────────────────────
def analizar_btc() -> dict:
    precio_btc = get_precio("BTCUSDT") or 0
    fuerza = 0
    detalle = []
    estado = "LATERAL"
    mov_pct = 0.0

    df1d = get_velas("BTCUSDT", "1d", 50)
    df4h = get_velas("BTCUSDT", "4h", 100)
    df1h = get_velas("BTCUSDT", "1h", 100)

    if df1d is not None and len(df1d) >= 30:
        p = df1d["close"].iloc[-1]
        e20 = calc_ema(df1d["close"], 20)
        e50 = calc_ema(df1d["close"], 50)
        r1d = calc_rsi(df1d["close"])
        if p > e20 > e50:
            fuerza += 2; detalle.append(f"📈 Diario alcista RSI:{r1d:.0f}")
        elif p < e20 < e50:
            fuerza -= 2; detalle.append(f"📉 Diario bajista RSI:{r1d:.0f}")
        else:
            detalle.append(f"↔️ Diario lateral RSI:{r1d:.0f}")

    if df4h is not None and len(df4h) >= 20:
        p4 = df4h["close"].iloc[-1]
        e20_4h = calc_ema(df4h["close"], 20)
        r4h = calc_rsi(df4h["close"])
        if p4 > e20_4h:
            fuerza += 1; detalle.append(f"📈 4h alcista RSI:{r4h:.0f}")
        else:
            fuerza -= 1; detalle.append(f"📉 4h bajista RSI:{r4h:.0f}")

    if df1h is not None and len(df1h) >= 16:
        precio_8h  = df1h["close"].iloc[-9]
        precio_now = df1h["close"].iloc[-1]
        mov_pct    = (precio_now - precio_8h) / precio_8h * 100

        ultimas_3h = df1h["close"].iloc[-4:]
        rango_3h   = (ultimas_3h.max() - ultimas_3h.min()) / ultimas_3h.mean() * 100
        rangeando  = rango_3h < 0.8

        atr_rec = calc_atr(df1h.tail(8))
        atr_ant = calc_atr(df1h.iloc[-16:-8])
        atr_bajando = atr_rec < atr_ant * 0.90

        if mov_pct >= 1.0 and (rangeando or atr_bajando):
            estado = "SUBIO_RANGEA"; fuerza += 2
            detalle.append(f"🚀 Subió {mov_pct:.1f}% y RANGEA → LARGO")
        elif mov_pct <= -1.0 and (rangeando or atr_bajando):
            estado = "BAJO_RANGEA"; fuerza -= 2
            detalle.append(f"💥 Bajó {mov_pct:.1f}% y RANGEA → CORTO")
        elif abs(mov_pct) < 1.0:
            estado = "LATERAL"
            detalle.append(f"↔️ Lateral {mov_pct:.1f}%")
        else:
            estado = "EN_MOVIMIENTO"
            detalle.append(f"⚠️ En movimiento {mov_pct:.1f}% — esperando rangeo")

        p1 = df1h["close"].iloc[-1]
        e20_1h = calc_ema(df1h["close"], 20)
        r1h = calc_rsi(df1h["close"])
        if p1 > e20_1h:
            fuerza += 1; detalle.append(f"📈 1h sobre EMA20 RSI:{r1h:.0f}")
        else:
            fuerza -= 1; detalle.append(f"📉 1h bajo EMA20 RSI:{r1h:.0f}")

    if fuerza >= 3:    emoji, resumen = "🚀", "ALCISTA FUERTE"
    elif fuerza >= 1:  emoji, resumen = "📈", "ALCISTA"
    elif fuerza <= -3: emoji, resumen = "💥", "BAJISTA FUERTE"
    elif fuerza <= -1: emoji, resumen = "📉", "BAJISTA"
    else:              emoji, resumen = "↔️", "LATERAL"

    return {"emoji": emoji, "resumen": resumen, "fuerza": fuerza,
            "precio": precio_btc, "detalle": detalle,
            "estado": estado, "mov_pct": mov_pct}


# ── Grid óptimo ────────────────────────────────────────────
def calcular_grid(precio: float, atr_pct: float, score: int) -> dict:
    rango_pct  = atr_pct * 3
    rango_bajo = round(precio * (1 - rango_pct/100), 6)
    rango_alto = round(precio * (1 + rango_pct/100), 6)

    # Grillas densas: ~0.20% cada una para más cruces por hora
    pct_grilla_obj = 0.20
    grillas = max(15, min(200, int(rango_pct / pct_grilla_obj)))
    pct_grilla = round(rango_pct / grillas, 3)

    # Apalancamiento con liquidación 20% más abajo que el rango bajo
    max_apal = max(2, min(20, int(precio / max(precio - rango_bajo * 0.80, 0.0001))))
    if atr_pct >= 1.5:   apal = min(3, max_apal)
    elif atr_pct >= 0.8: apal = min(5, max_apal)
    elif atr_pct >= 0.4: apal = min(7, max_apal)
    else:                apal = min(10, max_apal)

    liq_largo = round(precio * (1 - 1/apal), 6)
    liq_corto = round(precio * (1 + 1/apal), 6)
    dist_liq  = round((rango_bajo - liq_largo) / precio * 100, 2)

    # Tiempo estimado: calibrado con resultado real (INJ tardó ~4h con cálculo optimista)
    # Aplicamos factor de corrección 1.8x para reflejar la realidad del mercado
    cruces_hora  = (atr_pct * 4) / pct_grilla if pct_grilla > 0 else 0.1
    cruces_para_1pct = max(1, int(1.0 / (pct_grilla * apal / 100) / 100))
    horas_1pct_teorico = cruces_para_1pct / cruces_hora if cruces_hora > 0 else 99
    horas_1pct   = horas_1pct_teorico * 1.8  # factor de corrección por calibración real

    if horas_1pct < 1:   t1 = f"{int(horas_1pct*60)} min"
    elif horas_1pct < 8: t1 = f"{horas_1pct:.1f} hs"
    else:                t1 = "+8 hs"

    # Potencial en 8h operativas
    ganancia_8h = round(min(cruces_hora * 8 * pct_grilla * apal / 100, 8.0), 2)
    supera_1pct = ganancia_8h > 1.5

    sl_largo = round(rango_bajo * 0.97, 6)
    sl_corto = round(rango_alto * 1.03, 6)
    trailing = round(min(ganancia_8h * 0.65, 5.0), 2)

    # Preset Pionex
    if score >= 10:
        preset = "🟢 AGRESIVA"
        usar_preset = "Usá la predeterminada AGRESIVA como base y ajustá solo el rango"
    elif score >= 6:
        preset = "🟡 BALANCEADA"
        usar_preset = "Usá la predeterminada BALANCEADA y personalizá grillas y rango"
    else:
        preset = "🔴 CONSERVADORA"
        usar_preset = "Usá la CONSERVADORA sin modificar el apalancamiento"

    return {
        "rango_bajo": rango_bajo, "rango_alto": rango_alto,
        "rango_pct": round(rango_pct,2), "grillas": grillas,
        "pct_grilla": pct_grilla, "apal": apal,
        "liq_largo": liq_largo, "liq_corto": liq_corto,
        "dist_liq": dist_liq, "tiempo_1pct": t1,
        "horas_1pct": horas_1pct, "apto": horas_1pct <= 8,
        "ganancia_8h": ganancia_8h, "supera_1pct": supera_1pct,
        "sl_largo": sl_largo, "sl_corto": sl_corto, "trailing": trailing,
        "preset": preset, "usar_preset": usar_preset,
        "cruces_hora": round(cruces_hora, 1),
    }


# ── Análisis de par ────────────────────────────────────────
def analizar_par(par: str, btc: dict, score_min: int) -> dict | None:
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
    pat     = patron_vela(df15)
    vol_r   = float(df15["vol"].iloc[-1]) / max(float(df15["vol"].iloc[-21:-1].mean()), 0.0001)
    corr    = correlacion_propia(df15, btc["mov_pct"])

    score = 0; razones = []

    # 1. Volatilidad (0-2)
    if atr_pct >= 0.8:
        score += 2; razones.append(f"✅ Volatilidad alta: {atr_pct:.2f}%")
    elif atr_pct >= 0.2:
        score += 1; razones.append(f"⚡ Volatilidad media: {atr_pct:.2f}%")
    else:
        razones.append(f"❌ Volatilidad baja: {atr_pct:.2f}%")

    # 2. Bollinger (0-2)
    if bb15["ancho"] >= 3.0:
        score += 2; razones.append(f"✅ Bollinger activo: {bb15['ancho']:.1f}%")
    elif bb15["ancho"] >= 1.0:
        score += 1; razones.append(f"⚡ Bollinger moderado: {bb15['ancho']:.1f}%")
    else:
        razones.append(f"❌ Bollinger comprimido: {bb15['ancho']:.1f}%")

    # 3. Posición BB (0-1)
    if 0.15 <= bb15["pos"] <= 0.85:
        score += 1; razones.append(f"✅ Precio en zona grid")

    # 4. RSI (0-1)
    if 30 <= rsi15 <= 70:
        score += 1; razones.append(f"✅ RSI neutro: {rsi15:.1f}")
    else:
        score += 1; razones.append(f"⚡ RSI extremo: {rsi15:.1f}")

    # 5. StochRSI (0-1)
    if 20 <= sr15 <= 80:
        score += 1; razones.append(f"✅ StochRSI: {sr15:.1f}")

    # 6. MACD (0-2)
    if mc15["cruce_alc"] or mc15["cruce_baj"]:
        score += 2
        razones.append(f"✅ Cruce MACD {'alcista 🟢' if mc15['cruce_alc'] else 'bajista 🔴'}")
    elif abs(mc15["hist"]) > 0:
        score += 1; razones.append(f"⚡ MACD momentum")

    # 7. Estado BTC (0-2)
    if btc["estado"] == "SUBIO_RANGEA":
        score += 2 if precio > e20_15 else 1
        razones.append(f"✅ BTC post-suba rangeando → LARGO")
    elif btc["estado"] == "BAJO_RANGEA":
        score += 2 if precio < e20_15 else 1
        razones.append(f"✅ BTC post-baja rangeando → CORTO")
    elif btc["estado"] == "LATERAL":
        score += 1; razones.append(f"✅ BTC lateral")

    # 8. Correlación propia (0-1)
    if corr["diverge"]:
        score += 1; razones.append(f"✅ Movimiento propio: {corr['mov_propio']}%")

    # 9. Patrón vela (0-2)
    if pat in ["MARTILLO_ALC","ENGULFING_ALC","VELA_ALC"] and btc["fuerza"] >= 0:
        score += 2; razones.append(f"✅ Patrón: {pat}")
    elif pat in ["SHOOTING_BAJ","ENGULFING_BAJ","VELA_BAJ"] and btc["fuerza"] <= 0:
        score += 2; razones.append(f"✅ Patrón: {pat}")
    elif pat == "DOJI":
        score += 1; razones.append(f"⚡ Doji")

    # 10. Confirmación 1h (0-1)
    if df1h is not None and len(df1h) >= 20:
        e20_1h = calc_ema(df1h["close"], 20)
        r1h = calc_rsi(df1h["close"])
        if (precio > e20_1h and btc["fuerza"] >= 0) or (precio < e20_1h and btc["fuerza"] <= 0):
            score += 1; razones.append(f"✅ 1h confirma RSI:{r1h:.0f}")

    # 11. Volumen (0-1)
    if vol_r >= 1.2:
        score += 1; razones.append(f"✅ Volumen: {vol_r:.1f}x")
    elif vol_r >= 0.7:
        score += 1; razones.append(f"⚡ Volumen normal: {vol_r:.1f}x")

    if score < score_min:
        return None

    pct = score / 16 * 100
    if pct >= 65:   prob, prob_n = "🟢 ALTA", 3
    elif pct >= 40: prob, prob_n = "🟡 MEDIA", 2
    else:           prob, prob_n = "🔴 BÁSICA", 1

    # Dirección — solo LARGO o CORTO
    if btc["estado"] == "SUBIO_RANGEA":
        direccion = "📈 LARGO"
    elif btc["estado"] == "BAJO_RANGEA":
        direccion = "📉 CORTO"
    elif rsi15 <= 42 and mc15["macd"] > mc15["signal"] and btc["fuerza"] >= 0:
        direccion = "📈 LARGO"
    elif rsi15 >= 58 and mc15["macd"] < mc15["signal"] and btc["fuerza"] <= 0:
        direccion = "📉 CORTO"
    else:
        return None  # Sin señal direccional clara → no reportar

    grid = calcular_grid(precio, atr_pct, score)
    if not grid["apto"]:
        return None

    return {
        "par": par, "precio": precio,
        "score": score, "score_max": 16, "pct": pct,
        "prob": prob, "prob_n": prob_n,
        "direccion": direccion, "razones": razones,
        "atr_pct": atr_pct, **grid,
    }


# ── Contador diario ────────────────────────────────────────
def registrar_señal(par: str, ganancia_est: float):
    hoy = hoy_arg()
    if hoy not in señales_del_dia:
        señales_del_dia[hoy] = []
    señales_del_dia[hoy].append({"par": par, "ganancia": ganancia_est})

def estado_objetivo_diario() -> dict:
    hoy = hoy_arg()
    señales = señales_del_dia.get(hoy, [])
    total_est = sum(s["ganancia"] for s in señales)
    objetivo_ok = total_est >= OBJETIVO_DIARIO
    return {
        "señales": len(señales),
        "total_est": round(total_est, 2),
        "objetivo_ok": objetivo_ok,
        "faltan": round(max(0, OBJETIVO_DIARIO - total_est), 2),
    }


# ── Alertas de cierre ──────────────────────────────────────
def programar_alerta_cierre(par: str, direccion: str, precio_entrada: float,
                             horas: float, ganancia_est: float):
    clave = f"{par}_{hoy_arg()}_{hora_arg()}"
    operaciones_abiertas[clave] = {
        "par": par, "dir": direccion,
        "entrada": precio_entrada, "horas": horas,
        "ganancia_est": ganancia_est,
        "hora_apertura": hora_arg(),
        "hora_cierre_est": (datetime.now(TZ_ARG) + timedelta(hours=horas)).strftime("%H:%M"),
    }

def verificar_cierres():
    ahora = datetime.now(TZ_ARG)
    for clave, op in list(operaciones_abiertas.items()):
        hora_est = datetime.strptime(op["hora_cierre_est"], "%H:%M").replace(
            year=ahora.year, month=ahora.month, day=ahora.day, tzinfo=TZ_ARG)
        if ahora >= hora_est:
            enviar_telegram(
                f"⏰ <b>REVISAR OPERACIÓN</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 Par: <b>{op['par']}</b>\n"
                f"🎯 Dirección: {op['dir']}\n"
                f"💰 Precio entrada: {op['entrada']}\n"
                f"⏱ Abierta desde: {op['hora_apertura']} hs\n"
                f"🎯 Ganancia estimada alcanzada: ~{op['ganancia_est']}%\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Considerá cerrar el bot si ya lograste el objetivo\n"
                f"🔄 O dejalo correr si el precio sigue dentro del rango\n"
                f"🕐 {hora_arg()} hs (ARG)"
            )
            del operaciones_abiertas[clave]


# ── Generar alertas ────────────────────────────────────────
def generar_alertas():
    if not en_horario_operativo():
        print(f"[{hora_arg()}] Fuera de horario operativo (7-24 ARG)")
        return

    ahora = hora_arg()
    score_min = score_minimo_horario()
    print(f"\n[{ahora}] Analizando {len(PARES)} pares (score_min={score_min})...")

    # Verificar cierres pendientes
    verificar_cierres()

    btc = analizar_btc()
    print(f"  BTC: {btc['resumen']} ${btc['precio']:,.0f} estado={btc['estado']}")

    if btc["estado"] == "EN_MOVIMIENTO":
        enviar_telegram(
            f"⚠️ <b>BTC en movimiento — {ahora} hs (ARG)</b>\n"
            f"Movimiento: <b>{btc['mov_pct']:.1f}%</b> en últimas 8h\n"
            f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
            f"Esperando que BTC rangee. Próximo análisis en 30 min."
        )
        return

    # Estado objetivo diario
    obj = estado_objetivo_diario()
    if obj["objetivo_ok"]:
        print(f"[{ahora}] Objetivo diario {OBJETIVO_DIARIO}% ya alcanzado.")
        return

    resultados = []
    for par in PARES:
        try:
            r = analizar_par(par, btc, score_min)
            if r:
                resultados.append(r)
        except Exception as e:
            print(f"  Error {par}: {e}")
        time.sleep(0.1)

    resultados.sort(key=lambda x: (-x["prob_n"], -x["score"], x["horas_1pct"]))

    if not resultados:
        enviar_telegram(
            f"📊 <b>Análisis {ahora} hs (ARG)</b>\n"
            f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
            f"Estado BTC: <b>{btc['estado']}</b>\n"
            f"Objetivo hoy: {obj['total_est']}% de {OBJETIVO_DIARIO}% | Faltan: {obj['faltan']}%\n"
            f"Sin señales suficientes. Próximo en 30 min."
        )
        return

    enviadas = 0
    for r in resultados[:MAX_ALERTAS]:
        clave = f"{r['par']}_{datetime.now(TZ_ARG).strftime('%Y%m%d_%H')}"
        if clave in alertas_enviadas:
            continue
        alertas_enviadas[clave] = True

        # Objetivo y potencial
        if r["supera_1pct"]:
            bloque_obj = (
                f"\n💰 <b>OBJETIVO: MÁS DEL 1%</b>\n"
                f"   Potencial estimado en 8h: <b>{r['ganancia_8h']}%</b>\n"
                f"   Stop Loss Largo:  {r['sl_largo']} USDT\n"
                f"   Stop Loss Corto:  {r['sl_corto']} USDT\n"
                f"   Trailing Profit:  cerrar al <b>{r['trailing']}%</b>\n"
            )
        else:
            bloque_obj = (
                f"\n💰 <b>OBJETIVO: 1%</b>\n"
                f"   Potencial estimado en 8h: {r['ganancia_8h']}%\n"
            )

        # Estado objetivo diario
        nuevo_total = round(obj["total_est"] + r["ganancia_8h"], 2)
        bloque_diario = (
            f"\n📅 <b>Objetivo diario ({OBJETIVO_DIARIO}%):</b>\n"
            f"   Señales hoy: {obj['señales']} | Acumulado est.: {obj['total_est']}%\n"
            f"   Con esta operación: ~{nuevo_total}%\n"
            f"   {'✅ OBJETIVO CUBIERTO' if nuevo_total >= OBJETIVO_DIARIO else f'Faltan: {round(OBJETIVO_DIARIO - nuevo_total, 2)}%'}\n"
        )

        msg = (
            f"🚨 <b>━━ SEÑAL GRID INTRADAY ━━</b>\n\n"
            f"📌 PAR:           <b>{r['par']}</b>\n"
            f"🎯 OPERACIÓN:     <b>{r['direccion']}</b>\n"
            f"⚡ APALANCAMIENTO: <b>{r['apal']}x</b>\n"
            f"🎰 PROBABILIDAD:  <b>{r['prob']}</b>\n"
            f"📊 Score: {r['score']}/{r['score_max']} ({r['pct']:.0f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Precio actual: {r['precio']:.6g} USDT\n"
            f"🌐 BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
            f"   Estado BTC: <b>{btc['estado']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Configuración Grid Pionex:</b>\n"
            f"   {r['usar_preset']}\n\n"
            f"   Rango bajo:  <b>{r['rango_bajo']}</b> USDT\n"
            f"   Rango alto:  <b>{r['rango_alto']}</b> USDT\n"
            f"   Amplitud:    {r['rango_pct']}%\n"
            f"   Grillas:     <b>{r['grillas']}</b> (~{r['pct_grilla']}% c/u)\n"
            f"   Cruces/hora: ~{r['cruces_hora']}\n\n"
            f"   Liq. Largo: {r['liq_largo']} USDT\n"
            f"   Liq. Corto: {r['liq_corto']} USDT\n"
            f"   Margen seg.: {r['dist_liq']}% bajo rango\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱ Tiempo est. al 1%: <b>{r['tiempo_1pct']}</b>"
            + bloque_obj
            + bloque_diario +
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Análisis técnico:</b>\n"
            + "\n".join(f"   {s}" for s in r["razones"][:8]) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {ahora} hs (ARG)"
        )
        enviar_telegram(msg)
        registrar_señal(r["par"], r["ganancia_8h"])
        programar_alerta_cierre(r["par"], r["direccion"], r["precio"],
                                r["horas_1pct"], r["ganancia_8h"])
        enviadas += 1
        print(f"  ✅ {r['par']} {r['direccion']} score={r['score']} pot={r['ganancia_8h']}%")

    print(f"[{ahora}] {enviadas} alertas de {len(resultados)} candidatos.")


# ── Resumen matutino ───────────────────────────────────────
def resumen_matutino():
    hoy = hoy_arg()
    if resumen_enviado.get(hoy):
        return
    resumen_enviado[hoy] = True
    señales_del_dia[hoy] = []  # Reset contador diario

    btc = analizar_btc()
    candidatos = []
    for par in PARES:
        try:
            r = analizar_par(par, btc, MIN_SCORE)
            if r:
                candidatos.append(r)
        except:
            pass
        time.sleep(0.1)

    candidatos.sort(key=lambda x: (-x["prob_n"], -x["score"]))
    top3 = candidatos[:3]

    lineas = [
        f"☀️ <b>RESUMEN MATUTINO {fecha_arg()}</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🌐 BTC: {btc['emoji']} <b>{btc['resumen']}</b> (${btc['precio']:,.0f})",
        f"Estado: <b>{btc['estado']}</b> | Mov 8h: {btc['mov_pct']:.1f}%",
        f"🎯 Objetivo del día: <b>{OBJETIVO_DIARIO}% de ganancia</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]

    if not top3:
        lineas.append("Mercado sin señales claras al inicio del día.")
    else:
        lineas.append("🏆 <b>Mejores pares para hoy:</b>")
        for i, r in enumerate(top3, 1):
            pot = f"🔥 Pot: {r['ganancia_8h']}%" if r["supera_1pct"] else f"Pot: {r['ganancia_8h']}%"
            lineas.append(
                f"\n{i}. <b>{r['par']}</b> — {r['prob']}\n"
                f"   {r['direccion']} | {r['apal']}x | {pot}\n"
                f"   Preset: {r['preset']}\n"
                f"   Grillas: {r['grillas']} | Tiempo al 1%: {r['tiempo_1pct']}"
            )

    lineas += [
        f"\n━━━━━━━━━━━━━━━━━━━━",
        f"🔔 Alertas a los :03 y :33 hs (7:00-24:00 ARG)",
    ]
    enviar_telegram("\n".join(lineas))


# ── Main ───────────────────────────────────────────────────
def main():
    print(f"🤖 Bot v8 iniciado — {len(PARES)} pares")
    enviar_telegram(
        f"🤖 <b>JJ Cripto Bot v8 iniciado</b>\n"
        f"📊 {len(PARES)} pares | Cascada Bybit→OKX→Binance\n"
        f"🕐 Horario ARG (UTC-3) corregido | 7:00 a 24:00\n"
        f"⏰ Análisis a los :03 y :33 de cada hora (hora ARG real)\n"
        f"🎯 Objetivo diario: {OBJETIVO_DIARIO}% | Solo LARGO y CORTO\n"
        f"✅ Tiempo estimado calibrado con datos reales"
    )

    # schedule.every().day.at() usa la hora del SISTEMA (UTC en Railway).
    # Hora ARG = UTC - 3, entonces para que algo corra a las H:MM hora ARG,
    # hay que programarlo a las (H+3):MM en UTC.
    for h_arg in range(7, 24):
        h_utc = (h_arg + 3) % 24
        schedule.every().day.at(f"{h_utc:02d}:03").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:33").do(generar_alertas)

    # Resumen a las 09:03 ARG = 12:03 UTC
    h_resumen_utc = (9 + 3) % 24
    schedule.every().day.at(f"{h_resumen_utc:02d}:03").do(resumen_matutino)

    if en_horario_operativo():
        generar_alertas()

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
