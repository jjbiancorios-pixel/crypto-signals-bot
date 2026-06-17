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
    # Ampliación para compensar filtro de probabilidad alta
    "APTUSDT","ARKMUSDT","BLURUSDT","CYBERUSDT","DYDXUSDT",
    "GMTUSDT","IMXUSDT","JASMYUSDT","JTOUSDT","KASUSDT",
    "MASKUSDT","MINAUSDT","ONDOUSDT","PYTHUSDT","RNDRUSDT",
    "ROSEUSDT","SSVUSDT","STRKUSDT","SUPERUSDT","TWTUSDT",
    "UMAUSDT","WUSDT","XAIUSDT","ZETAUSDT","ZRXUSDT",
]

MIN_SCORE_ALTA  = 11   # Solo probabilidad ALTA (sobre 16 puntos, ~69%)
MAX_ALERTAS     = 5
HORA_INICIO     = 7
HORA_FIN        = 24
OBJETIVO_DIARIO = 3

alertas_enviadas     = {}
resumen_enviado       = {}
señales_del_dia       = {}
operaciones_abiertas  = {}


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
def OKX_PAR(p):
    return p.replace("1000SHIB","SHIB").replace("1000PEPE","PEPE").replace("1000BONK","BONK").replace("USDT","-USDT")
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
    mov_propio = (df15["close"].iloc[-1] - df15["close"].iloc[-4]) / df15["close"].iloc[-4] * 100
    diverge_fuerte = abs(mov_propio) >= 1.5 and abs(mov_propio - btc_mov) > 1.2
    return {"mov_propio": round(mov_propio, 2), "diverge_fuerte": diverge_fuerte}


# ── Análisis BTC — detector de rangeo mejorado ─────────────
def analizar_btc() -> dict:
    precio_btc = get_precio("BTCUSDT") or 0
    fuerza = 0
    detalle = []
    estado = "LATERAL"
    mov_pct = 0.0

    df1d = get_velas("BTCUSDT", "1d", 50)
    df4h = get_velas("BTCUSDT", "4h", 100)
    df1h = get_velas("BTCUSDT", "1h", 100)
    df15 = get_velas("BTCUSDT", "15m", 50)

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

        # ── Detector de RANGEO REAL (más estricto y preciso) ──
        # Criterio 1: precio oscila en canal angosto últimas 2-3 velas de 1h
        ultimas_3h = df1h["close"].iloc[-4:]
        rango_3h   = (ultimas_3h.max() - ultimas_3h.min()) / ultimas_3h.mean() * 100
        canal_estrecho = rango_3h < 0.7

        # Criterio 2: en 15m, las últimas 8 velas (2h) oscilan sin dirección clara
        rangeando_15m = False
        if df15 is not None and len(df15) >= 8:
            ultimas_2h_15m = df15["close"].iloc[-8:]
            primera_mitad  = ultimas_2h_15m.iloc[:4].mean()
            segunda_mitad  = ultimas_2h_15m.iloc[4:].mean()
            deriva_pct = abs(segunda_mitad - primera_mitad) / primera_mitad * 100
            rangeando_15m = deriva_pct < 0.4  # sin deriva direccional clara

        # Criterio 3: ATR bajando (confirma pérdida de momentum)
        atr_rec = calc_atr(df1h.tail(8))
        atr_ant = calc_atr(df1h.iloc[-16:-8])
        atr_bajando = atr_rec < atr_ant * 0.90

        # Rangeo confirmado: necesita AL MENOS 2 de los 3 criterios
        criterios_cumplidos = sum([canal_estrecho, rangeando_15m, atr_bajando])
        rangeo_confirmado = criterios_cumplidos >= 2

        if mov_pct >= 1.0 and rangeo_confirmado:
            estado = "SUBIO_RANGEA"; fuerza += 2
            detalle.append(f"🚀 Subió {mov_pct:.1f}% y RANGEA confirmado ({criterios_cumplidos}/3) → LARGO")
        elif mov_pct <= -1.0 and rangeo_confirmado:
            estado = "BAJO_RANGEA"; fuerza -= 2
            detalle.append(f"💥 Bajó {mov_pct:.1f}% y RANGEA confirmado ({criterios_cumplidos}/3) → CORTO")
        elif abs(mov_pct) < 1.0:
            estado = "LATERAL"
            detalle.append(f"↔️ Lateral {mov_pct:.1f}%")
        else:
            estado = "EN_MOVIMIENTO"
            detalle.append(f"⚠️ Movimiento {mov_pct:.1f}% sin rangeo confirmado ({criterios_cumplidos}/3)")

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


# ── Grid óptimo con/sin reserva ─────────────────────────────
def calcular_grid(precio: float, atr_pct: float, score: int) -> dict:
    rango_pct  = atr_pct * 3
    rango_bajo = round(precio * (1 - rango_pct/100), 6)
    rango_alto = round(precio * (1 + rango_pct/100), 6)

    pct_grilla_obj = 0.20
    grillas = max(15, min(200, int(rango_pct / pct_grilla_obj)))
    pct_grilla = round(rango_pct / grillas, 3)

    # Apalancamiento CON reserva (estándar Pionex)
    max_apal = max(2, min(20, int(precio / max(precio - rango_bajo * 0.80, 0.0001))))
    if atr_pct >= 1.5:   apal = min(3, max_apal)
    elif atr_pct >= 0.8: apal = min(5, max_apal)
    elif atr_pct >= 0.4: apal = min(7, max_apal)
    else:                apal = min(10, max_apal)

    liq_largo = round(precio * (1 - 1/apal), 6)
    liq_corto = round(precio * (1 + 1/apal), 6)
    dist_liq  = round((rango_bajo - liq_largo) / precio * 100, 2)

    # ── Configuración SIN reserva (capital 100% trabajando) ──
    # Reducimos apalancamiento 2 puntos para mantener la misma distancia a liquidación
    apal_sin_reserva = max(2, apal - 2)
    liq_largo_sr = round(precio * (1 - 1/apal_sin_reserva), 6)
    liq_corto_sr = round(precio * (1 + 1/apal_sin_reserva), 6)
    dist_liq_sr  = round((rango_bajo - liq_largo_sr) / precio * 100, 2)
    # Capital de respaldo sugerido: equivalente al margen que Pionex reservaría (~46% del total)
    capital_respaldo_pct = 46

    cruces_hora  = (atr_pct * 4) / pct_grilla if pct_grilla > 0 else 0.1
    cruces_para_1pct = max(1, int(1.0 / (pct_grilla * apal / 100) / 100))
    horas_1pct_teorico = cruces_para_1pct / cruces_hora if cruces_hora > 0 else 99
    horas_1pct   = horas_1pct_teorico * 1.8

    if horas_1pct < 1:   t1 = f"{int(horas_1pct*60)} min"
    elif horas_1pct < 8: t1 = f"{horas_1pct:.1f} hs"
    else:                t1 = "+8 hs"

    ganancia_8h = round(min(cruces_hora * 8 * pct_grilla * apal / 100, 8.0), 2)
    supera_1pct = ganancia_8h > 1.5

    sl_largo = round(rango_bajo * 0.97, 6)
    sl_corto = round(rango_alto * 1.03, 6)
    trailing = round(min(ganancia_8h * 0.65, 5.0), 2)

    # Take profit exacto (objetivo principal: 1%, o ganancia_8h si supera)
    tp_objetivo = max(1.0, round(ganancia_8h * 0.6, 2))  # conservador, alcanzable

    if score >= 14:
        preset = "🟢 AGRESIVA"
        usar_preset = "Usá la predeterminada AGRESIVA, ajustá solo el Take Profit"
    elif score >= 11:
        preset = "🟡 BALANCEADA"
        usar_preset = "Usá la predeterminada BALANCEADA y el Take Profit indicado"
    else:
        preset = "🔴 CONSERVADORA"
        usar_preset = "Usá la CONSERVADORA sin modificar apalancamiento"

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
        "tp_objetivo": tp_objetivo,
        "apal_sin_reserva": apal_sin_reserva,
        "liq_largo_sr": liq_largo_sr, "liq_corto_sr": liq_corto_sr,
        "dist_liq_sr": dist_liq_sr, "capital_respaldo_pct": capital_respaldo_pct,
    }


# ── Análisis de par ────────────────────────────────────────
def analizar_par(par: str, btc: dict) -> dict | None:
    if btc["estado"] == "EN_MOVIMIENTO":
        df15_check = get_velas(par, "15m", 20)
        if df15_check is None:
            return None
        corr_check = correlacion_propia(df15_check, btc["mov_pct"])
        if not corr_check["diverge_fuerte"]:
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

    if atr_pct >= 0.8:
        score += 2; razones.append(f"✅ Volatilidad alta: {atr_pct:.2f}%")
    elif atr_pct >= 0.2:
        score += 1; razones.append(f"⚡ Volatilidad media: {atr_pct:.2f}%")
    else:
        razones.append(f"❌ Volatilidad baja: {atr_pct:.2f}%")

    if bb15["ancho"] >= 3.0:
        score += 2; razones.append(f"✅ Bollinger activo: {bb15['ancho']:.1f}%")
    elif bb15["ancho"] >= 1.0:
        score += 1; razones.append(f"⚡ Bollinger moderado: {bb15['ancho']:.1f}%")
    else:
        razones.append(f"❌ Bollinger comprimido: {bb15['ancho']:.1f}%")

    if 0.15 <= bb15["pos"] <= 0.85:
        score += 1; razones.append(f"✅ Precio en zona grid")

    if 30 <= rsi15 <= 70:
        score += 1; razones.append(f"✅ RSI neutro: {rsi15:.1f}")
    else:
        score += 1; razones.append(f"⚡ RSI extremo: {rsi15:.1f}")

    if 20 <= sr15 <= 80:
        score += 1; razones.append(f"✅ StochRSI: {sr15:.1f}")

    if mc15["cruce_alc"] or mc15["cruce_baj"]:
        score += 2
        razones.append(f"✅ Cruce MACD {'alcista 🟢' if mc15['cruce_alc'] else 'bajista 🔴'}")
    elif abs(mc15["hist"]) > 0:
        score += 1; razones.append(f"⚡ MACD momentum")

    if btc["estado"] == "SUBIO_RANGEA":
        score += 2 if precio > e20_15 else 1
        razones.append(f"✅ BTC post-suba rangeando → LARGO")
    elif btc["estado"] == "BAJO_RANGEA":
        score += 2 if precio < e20_15 else 1
        razones.append(f"✅ BTC post-baja rangeando → CORTO")
    elif btc["estado"] == "LATERAL":
        score += 1; razones.append(f"✅ BTC lateral")
    elif corr["diverge_fuerte"]:
        score += 1; razones.append(f"✅ Movimiento propio fuerte: {corr['mov_propio']}%")

    if pat in ["MARTILLO_ALC","ENGULFING_ALC","VELA_ALC"] and btc["fuerza"] >= 0:
        score += 2; razones.append(f"✅ Patrón: {pat}")
    elif pat in ["SHOOTING_BAJ","ENGULFING_BAJ","VELA_BAJ"] and btc["fuerza"] <= 0:
        score += 2; razones.append(f"✅ Patrón: {pat}")
    elif pat == "DOJI":
        score += 1; razones.append(f"⚡ Doji")

    if df1h is not None and len(df1h) >= 20:
        e20_1h = calc_ema(df1h["close"], 20)
        r1h = calc_rsi(df1h["close"])
        if (precio > e20_1h and btc["fuerza"] >= 0) or (precio < e20_1h and btc["fuerza"] <= 0):
            score += 1; razones.append(f"✅ 1h confirma RSI:{r1h:.0f}")

    if vol_r >= 1.2:
        score += 1; razones.append(f"✅ Volumen: {vol_r:.1f}x")
    elif vol_r >= 0.7:
        score += 1; razones.append(f"⚡ Volumen normal: {vol_r:.1f}x")

    # Solo probabilidad ALTA
    if score < MIN_SCORE_ALTA:
        return None

    pct = score / 16 * 100
    prob, prob_n = "🟢 ALTA", 3

    if btc["estado"] == "SUBIO_RANGEA":
        direccion = "📈 LARGO"
    elif btc["estado"] == "BAJO_RANGEA":
        direccion = "📉 CORTO"
    elif corr["diverge_fuerte"] and corr["mov_propio"] > 0:
        direccion = "📈 LARGO"
    elif corr["diverge_fuerte"] and corr["mov_propio"] < 0:
        direccion = "📉 CORTO"
    elif rsi15 <= 42 and mc15["macd"] > mc15["signal"] and btc["fuerza"] >= 0:
        direccion = "📈 LARGO"
    elif rsi15 >= 58 and mc15["macd"] < mc15["signal"] and btc["fuerza"] <= 0:
        direccion = "📉 CORTO"
    else:
        return None

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
    return {
        "señales": len(señales),
        "total_est": round(total_est, 2),
        "objetivo_ok": total_est >= OBJETIVO_DIARIO,
        "faltan": round(max(0, OBJETIVO_DIARIO - total_est), 2),
    }


# ── Alertas de cierre ──────────────────────────────────────
def programar_alerta_cierre(par, direccion, precio_entrada, horas, ganancia_est, tp):
    clave = f"{par}_{hoy_arg()}_{hora_arg()}"
    operaciones_abiertas[clave] = {
        "par": par, "dir": direccion, "entrada": precio_entrada,
        "horas": horas, "ganancia_est": ganancia_est, "tp": tp,
        "hora_apertura": hora_arg(),
        "hora_cierre_est": (datetime.now(TZ_ARG) + timedelta(hours=horas)).strftime("%H:%M"),
    }

def verificar_cierres():
    try:
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
                    f"🎯 Take Profit objetivo: {op['tp']}%\n"
                    f"⏱ Abierta desde: {op['hora_apertura']} hs\n"
                    f"📊 Ganancia estimada alcanzada: ~{op['ganancia_est']}%\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Considerá cerrar si ya alcanzaste el TP\n"
                    f"🔄 O dejalo correr si el precio sigue en rango\n"
                    f"🕐 {hora_arg()} hs (ARG)"
                )
                del operaciones_abiertas[clave]
    except Exception as e:
        print(f"Error verificar_cierres: {e}")


# ── Generar alertas ────────────────────────────────────────
def generar_alertas():
    try:
        if not en_horario_operativo():
            print(f"[{hora_arg()}] Fuera de horario operativo")
            return

        ahora = hora_arg()
        print(f"\n[{ahora}] Analizando {len(PARES)} pares (solo prob. ALTA)...")

        verificar_cierres()

        btc = analizar_btc()
        print(f"  BTC: {btc['resumen']} ${btc['precio']:,.0f} estado={btc['estado']}")

        obj = estado_objetivo_diario()
        if obj["objetivo_ok"]:
            print(f"[{ahora}] Objetivo diario {OBJETIVO_DIARIO}% ya alcanzado.")
            return

        if btc["estado"] == "EN_MOVIMIENTO":
            enviar_telegram(
                f"⚠️ <b>BTC en movimiento — {ahora} hs (ARG)</b>\n"
                f"Movimiento: <b>{btc['mov_pct']:.1f}%</b> en últimas 8h\n"
                f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
                f"Buscando pares con movimiento propio divergente...\n"
                f"Próximo análisis en 30 min."
            )

        resultados = []
        for par in PARES:
            try:
                r = analizar_par(par, btc)
                if r:
                    resultados.append(r)
            except Exception as e:
                print(f"  Error {par}: {e}")
            time.sleep(0.08)

        resultados.sort(key=lambda x: (-x["score"], x["horas_1pct"]))

        if not resultados:
            if btc["estado"] != "EN_MOVIMIENTO":
                enviar_telegram(
                    f"📊 <b>Análisis {ahora} hs (ARG)</b>\n"
                    f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
                    f"Estado BTC: <b>{btc['estado']}</b>\n"
                    f"Objetivo hoy: {obj['total_est']}% de {OBJETIVO_DIARIO}% | Faltan: {obj['faltan']}%\n"
                    f"Sin señales de probabilidad ALTA. Próximo en 30 min."
                )
            return

        enviadas = 0
        for r in resultados[:MAX_ALERTAS]:
            clave = f"{r['par']}_{datetime.now(TZ_ARG).strftime('%Y%m%d_%H')}"
            if clave in alertas_enviadas:
                continue
            alertas_enviadas[clave] = True

            if r["supera_1pct"]:
                bloque_obj = (
                    f"\n💰 <b>OBJETIVO: MÁS DEL 1%</b>\n"
                    f"   Potencial estimado en 8h: <b>{r['ganancia_8h']}%</b>\n"
                    f"   Take Profit sugerido: <b>{r['tp_objetivo']}%</b>\n"
                    f"   Stop Loss Largo:  {r['sl_largo']} USDT\n"
                    f"   Stop Loss Corto:  {r['sl_corto']} USDT\n"
                    f"   Trailing Profit:  cerrar al <b>{r['trailing']}%</b>\n"
                )
            else:
                bloque_obj = (
                    f"\n💰 <b>OBJETIVO: 1%</b>\n"
                    f"   Take Profit sugerido: <b>{r['tp_objetivo']}%</b>\n"
                )

            bloque_reserva = (
                f"\n🏦 <b>Config. CON reserva (Pionex default):</b>\n"
                f"   Apalancamiento: {r['apal']}x | Liq. Largo: {r['liq_largo']} | Liq. Corto: {r['liq_corto']}\n"
                f"\n💼 <b>Config. SIN reserva (100% capital activo):</b>\n"
                f"   Apalancamiento ajustado: <b>{r['apal_sin_reserva']}x</b>\n"
                f"   Liq. Largo: {r['liq_largo_sr']} | Liq. Corto: {r['liq_corto_sr']}\n"
                f"   Margen seg.: {r['dist_liq_sr']}%\n"
                f"   💵 Capital de respaldo sugerido: <b>~{r['capital_respaldo_pct']}%</b> aparte (líquido)\n"
            )

            nuevo_total = round(obj["total_est"] + r["ganancia_8h"], 2)
            bloque_diario = (
                f"\n📅 <b>Objetivo diario ({OBJETIVO_DIARIO}%):</b>\n"
                f"   Señales hoy: {obj['señales']} | Acumulado: {obj['total_est']}%\n"
                f"   Con esta operación: ~{nuevo_total}%\n"
                f"   {'✅ OBJETIVO CUBIERTO' if nuevo_total >= OBJETIVO_DIARIO else f'Faltan: {round(OBJETIVO_DIARIO - nuevo_total, 2)}%'}\n"
            )

            msg = (
                f"🚨 <b>━━ SEÑAL GRID — PROB. ALTA ━━</b>\n\n"
                f"📌 PAR:           <b>{r['par']}</b>\n"
                f"🎯 OPERACIÓN:     <b>{r['direccion']}</b>\n"
                f"⚡ APALANCAMIENTO: <b>{r['apal']}x</b> (con reserva)\n"
                f"🎰 PROBABILIDAD:  <b>{r['prob']}</b> ({r['score']}/{r['score_max']})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Precio actual: {r['precio']:.6g} USDT\n"
                f"🌐 BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f}) | {btc['estado']}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚙️ <b>Configuración Grid Pionex:</b>\n"
                f"   {r['usar_preset']}\n\n"
                f"   Rango bajo:  <b>{r['rango_bajo']}</b> USDT\n"
                f"   Rango alto:  <b>{r['rango_alto']}</b> USDT\n"
                f"   Amplitud:    {r['rango_pct']}%\n"
                f"   Grillas:     <b>{r['grillas']}</b> (~{r['pct_grilla']}% c/u)\n"
                + bloque_reserva +
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
                                    r["horas_1pct"], r["ganancia_8h"], r["tp_objetivo"])
            enviadas += 1
            print(f"  ✅ {r['par']} {r['direccion']} score={r['score']}")

        print(f"[{ahora}] {enviadas} alertas de {len(resultados)} candidatos ALTA.")

    except Exception as e:
        print(f"ERROR CRÍTICO en generar_alertas: {e}")
        try:
            enviar_telegram(f"⚠️ Error técnico en el análisis: {str(e)[:200]}\nReintentando en 30 min.")
        except:
            pass


# ── Resumen matutino ───────────────────────────────────────
def resumen_matutino():
    try:
        hoy = hoy_arg()
        if resumen_enviado.get(hoy):
            return
        resumen_enviado[hoy] = True
        señales_del_dia[hoy] = []

        btc = analizar_btc()
        candidatos = []
        for par in PARES:
            try:
                r = analizar_par(par, btc)
                if r:
                    candidatos.append(r)
            except:
                pass
            time.sleep(0.08)

        candidatos.sort(key=lambda x: -x["score"])
        top3 = candidatos[:3]

        lineas = [
            f"☀️ <b>RESUMEN MATUTINO {fecha_arg()}</b>",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🌐 BTC: {btc['emoji']} <b>{btc['resumen']}</b> (${btc['precio']:,.0f})",
            f"Estado: <b>{btc['estado']}</b> | Mov 8h: {btc['mov_pct']:.1f}%",
            f"🎯 Objetivo del día: <b>{OBJETIVO_DIARIO}%</b> | Solo señales ALTA probabilidad",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]

        if not top3:
            lineas.append("Mercado sin señales de alta probabilidad al inicio del día.")
        else:
            lineas.append("🏆 <b>Mejores pares para hoy:</b>")
            for i, r in enumerate(top3, 1):
                pot = f"🔥 Pot: {r['ganancia_8h']}%" if r["supera_1pct"] else f"Pot: {r['ganancia_8h']}%"
                lineas.append(
                    f"\n{i}. <b>{r['par']}</b> — {r['prob']}\n"
                    f"   {r['direccion']} | {r['apal']}x | {pot}\n"
                    f"   TP sugerido: {r['tp_objetivo']}% | Tiempo: {r['tiempo_1pct']}"
                )

        lineas += [f"\n━━━━━━━━━━━━━━━━━━━━", f"🔔 Alertas a los :03 y :33 hs (7:00-24:00 ARG)"]
        enviar_telegram("\n".join(lineas))
    except Exception as e:
        print(f"Error resumen_matutino: {e}")


# ── Main ───────────────────────────────────────────────────
def main():
    print(f"🤖 Bot v9 iniciado — {len(PARES)} pares — Solo prob. ALTA")
    enviar_telegram(
        f"🤖 <b>JJ Cripto Bot v9 iniciado</b>\n"
        f"📊 {len(PARES)} pares | Cascada Bybit→OKX→Binance\n"
        f"🎯 Solo señales de probabilidad ALTA\n"
        f"🏦 Config. con/sin reserva + capital respaldo\n"
        f"💰 Take Profit exacto incluido\n"
        f"🔍 Detector de rangeo BTC mejorado (3 criterios)\n"
        f"➕ Divergencia propia cuando BTC está en movimiento"
    )

    for h_arg in range(7, 24):
        h_utc = (h_arg + 3) % 24
        schedule.every().day.at(f"{h_utc:02d}:03").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:33").do(generar_alertas)

    h_resumen_utc = (9 + 3) % 24
    schedule.every().day.at(f"{h_resumen_utc:02d}:03").do(resumen_matutino)

    if en_horario_operativo():
        generar_alertas()

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            print(f"Error en loop principal: {e}")
        time.sleep(30)

if __name__ == "__main__":
    main()
