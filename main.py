import requests
import pandas as pd
import numpy as np
import time
import schedule
from datetime import datetime, timezone, timedelta
import os
import db
import telegram_cmds
import gestion_riesgo
import pionex_api

# ── Automatización (feature flag) ───────────────────────────
# Se activa recién cuando PIONEX_API_KEY/SECRET estén cargadas y probadas.
# Mientras esté en "false", el bot sigue funcionando EXACTAMENTE igual que
# hoy (solo avisa por Telegram, sin abrir grillas ni operar 24hs).
AUTOMATIZACION_ACTIVA = os.environ.get("AUTOMATIZACION_ACTIVA", "false").lower() == "true"

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
    "ARBUSDT","INJUSDT","SUIUSDT","WLDUSDT",
    "STXUSDT","LDOUSDT","SEIUSDT","FETUSDT","GRTUSDT",
    "1000PEPEUSDT","WIFUSDT","FLOKIUSDT",
    "ENAUSDT","TIAUSDT","NOTUSDT","TAOUSDT","MEMEUSDT",
    "ORDIUSDT","ACEUSDT","ALTUSDT","PORTALUSDT",
    "APTUSDT","ARKMUSDT","BLURUSDT","GMTUSDT","IMXUSDT",
    "JASMYUSDT","JTOUSDT","KASUSDT","MASKUSDT",
    "ONDOUSDT","PYTHUSDT","ROSEUSDT","SSVUSDT",
    "STRKUSDT","SUPERUSDT","TWTUSDT","UMAUSDT","WUSDT",
    "XAIUSDT","ZETAUSDT","ZRXUSDT","OPUSDT",
    # 7 pares nuevos (reemplazan RNDR, 1000SHIB, CYBER, DYDX, MINA, 1000BONK, OP)
    # Seleccionados por liquidez y disponibilidad confirmada en Pionex
    "TONUSDT","EIGENUSDT","MOVEUSDT","VIRTUALUSDT",
    "PENGUUSDT","MOCAUSDT","SCRUSDT",
]

MIN_SCORE_ALTA  = 11
MAX_ALERTAS     = 10
HORA_INICIO     = 7
HORA_FIN        = 23   # Hasta las 23hs ARG
OBJETIVO_DIARIO = 3

# Umbral de movimiento de BTC para señal de caída brusca (cortos)
BTC_CAIDA_BRUSCA_PCT = -2.0  # BTC cayó más de 2% en 1h
BTC_SUBIDA_BRUSCA_PCT = 2.0  # BTC subió más de 2% en 1h (espejo de la caída brusca)

alertas_enviadas     = {}   # se mantiene como caché en RAM; persistencia real en db.alertas_enviadas
resumen_enviado       = {}  # idem — chequeo real contra db.resumen_ya_enviado
señales_del_dia       = {}  # ya no se usa para cálculos; queda por compatibilidad de imports
operaciones_abiertas  = {}  # idem


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
    if AUTOMATIZACION_ACTIVA:
        return True
    return HORA_INICIO <= hora_num() < HORA_FIN


# ── Telegram ───────────────────────────────────────────────
def enviar_telegram(msg: str):
    """
    Envía un mensaje a Telegram con reintentos.

    IMPORTANTE (aprendido 16/07): un timeout de lectura NO siempre significa
    que el mensaje no llegó — Telegram puede haber procesado el envío igual,
    solo que la confirmación tardó más de lo esperado. Reintentar en ese
    caso manda el mensaje DE NUEVO (duplicado), no lo recupera. Por eso acá
    se le da más margen de tiempo (25s en vez de 10s) para reducir falsos
    timeouts, y menos reintentos (2 en vez de 3) — así se prioriza esperar
    la confirmación real antes de asumir que hace falta reenviar.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for intento in range(1, 3):
        try:
            resp = requests.post(
                url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=25
            )
            if resp.status_code == 200:
                return  # éxito
            print(f"Telegram respondió error (intento {intento}/2): "
                  f"HTTP {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            print(f"Telegram error de conexión (intento {intento}/2): {e}")
        if intento < 2:
            time.sleep(3)
    print(f"Telegram: se agotaron los 2 intentos, mensaje posiblemente perdido: {msg[:80]}...")


# ── Datos: cascada Bybit → OKX → Binance Vision ────────────
BYBIT_TF   = {"15m":"15","1h":"60","4h":"240","1d":"D"}
OKX_TF     = {"15m":"15m","1h":"1H","4h":"4H","1d":"1Dutc"}
BINANCE_TF = {"15m":"15m","1h":"1h","4h":"4h","1d":"1d"}

def OKX_PAR(p):
    return p.replace("1000SHIB","SHIB").replace("1000PEPE","PEPE").replace("1000BONK","BONK").replace("USDT","-USDT")

def _velas_bybit(par, tf, n):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={par}&interval={BYBIT_TF.get(tf,'15')}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if data.get("retCode") != 0: raise ValueError("bybit fail")
    rows = data["result"]["list"]
    if not rows or len(rows) < 20: raise ValueError("bybit empty")
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","turnover"])
    for c in ["open","high","low","close","vol"]: df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)

def _velas_okx(par, tf, n):
    inst = OKX_PAR(par)
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={OKX_TF.get(tf,'15m')}&limit={n}"
    r = requests.get(url, timeout=8)
    rows = r.json().get("data", [])
    if not rows or len(rows) < 20: raise ValueError("okx empty")
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","volCcy","volCcyQuote","confirm"])
    for c in ["open","high","low","close","vol"]: df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)

def _velas_binance(par, tf, n):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={par}&interval={BINANCE_TF.get(tf,'15m')}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if not isinstance(data, list) or len(data) < 20: raise ValueError("binance empty")
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","vol","ct","qav","trades","tbbav","tbqav","ignore"])
    for c in ["open","high","low","close","vol"]: df[c] = df[c].astype(float)
    return df

def get_velas(par, tf, n=100):
    for f in (_velas_bybit, _velas_okx, _velas_binance):
        try:
            df = f(par, tf, n)
            if df is not None and len(df) >= 20: return df
        except: continue
    return None

def _precio_bybit(par):
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={par}", timeout=6)
    data = r.json()
    if data.get("retCode") != 0: raise ValueError()
    return float(data["result"]["list"][0]["lastPrice"])

def _precio_okx(par):
    r = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={OKX_PAR(par)}", timeout=6)
    rows = r.json().get("data", [])
    if not rows: raise ValueError()
    return float(rows[0]["last"])

def _precio_binance(par):
    r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={par}", timeout=6)
    return float(r.json()["price"])

def get_precio(par):
    for f in (_precio_bybit, _precio_okx, _precio_binance):
        try:
            p = f(par)
            if p and p > 0: return p
        except: continue
    return None


# ── Indicadores ────────────────────────────────────────────
def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return float((100 - 100/(1+g/l.replace(0,np.nan))).iloc[-1])

def calc_atr(df, p=14):
    hl = df["high"]-df["low"]
    hcp = (df["high"]-df["close"].shift()).abs()
    lcp = (df["low"]-df["close"].shift()).abs()
    return float(pd.concat([hl,hcp,lcp],axis=1).max(axis=1).rolling(p).mean().iloc[-1])

def calc_bb(s, p=20):
    m = s.rolling(p).mean(); st = s.rolling(p).std()
    up=(m+2*st).iloc[-1]; dn=(m-2*st).iloc[-1]; mid=m.iloc[-1]
    ancho=(up-dn)/mid*100 if mid>0 else 0
    pos=(s.iloc[-1]-dn)/(up-dn) if (up-dn)>0 else 0.5
    return {"upper":up,"lower":dn,"mid":mid,"ancho":ancho,"pos":pos}

def calc_macd(s):
    m = s.ewm(span=12).mean()-s.ewm(span=26).mean(); sg=m.ewm(span=9).mean()
    return {"macd":float(m.iloc[-1]),"signal":float(sg.iloc[-1]),"hist":float((m-sg).iloc[-1]),
            "cruce_alc":bool(m.iloc[-1]>sg.iloc[-1] and m.iloc[-2]<=sg.iloc[-2]),
            "cruce_baj":bool(m.iloc[-1]<sg.iloc[-1] and m.iloc[-2]>=sg.iloc[-2])}

def calc_ema(s, p): return float(s.ewm(span=p).mean().iloc[-1])

def calc_adx(df, p=14):
    """
    ADX (Average Directional Index) + DI+/DI- — método de Wilder.
    Mide FUERZA de tendencia (0-100), sin importar dirección:
      < 25: sin tendencia clara / lateral (bueno para grid — oscilación)
      25-35: tendencia moderada
      > 35: tendencia fuerte/sostenida (riesgo de romper el rango del grid)
    DI+ > DI-: la fuerza direccional es alcista; DI- > DI+: bajista.
    v16: usado para diferenciar el piso de ancho de grilla (ver calcular_grid)
    y en modo sombra para loggear si confirma la dirección de cada señal.
    """
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1/p, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/p, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/p, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.ewm(alpha=1/p, adjust=False).mean()
    return {
        "adx": float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0.0,
        "plus_di": float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else 0.0,
        "minus_di": float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else 0.0,
    }

def calc_vwap(df, p=96):
    """
    VWAP (Volume Weighted Average Price) sobre las últimas p velas (96 de
    15min ≈ 24hs). Precio típico (H+L+C)/3 ponderado por volumen.
    v16: usado como "régimen" — precio arriba del VWAP = sesgo alcista,
    abajo = sesgo bajista. Se usa en modo sombra y en el sistema de
    reapertura (<5min), combinado con EMA20 (ver confirma_regimen_vwap_ema).
    """
    d = df.iloc[-p:] if len(df) >= p else df
    tp = (d["high"] + d["low"] + d["close"]) / 3
    vol_total = d["vol"].sum()
    if vol_total <= 0:
        return float(d["close"].iloc[-1])
    return float((tp * d["vol"]).sum() / vol_total)

def calc_cci(df, p=20):
    """
    CCI (Commodity Channel Index) — mide desviación del precio típico
    respecto a su promedio móvil. >+100 = sobrecompra/tendencia fuerte
    alcista, <-100 = sobreventa/tendencia fuerte bajista.
    v16: candidato en modo sombra frente a RSI — un paper (arXiv 2206.06723)
    encontró que CCI estaba en las MEJORES combinaciones de indicadores
    para precisión/retorno, mientras RSI estaba en las PEORES (en acciones,
    no cripto — a validar con datos propios antes de sacar conclusiones).
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(p).mean()
    mad = tp.rolling(p).apply(lambda x: (x - x.mean()).abs().mean(), raw=False)
    cci = (tp - sma) / (0.015 * mad.replace(0, np.nan))
    val = cci.iloc[-1]
    return float(val) if pd.notna(val) else 0.0

def calc_obv_slope(df, p=14):
    """
    OBV (On-Balance Volume) — volumen acumulado que suma en velas alcistas
    y resta en bajistas. Detecta si hay presión de compra/venta real detrás
    del movimiento de precio (divergencia), a diferencia del volumen ratio
    actual que solo mide intensidad puntual, no dirección acumulada.
    Devuelve la PENDIENTE reciente (positiva = presión compradora neta).
    """
    closes = df["close"].values
    vols = df["vol"].values
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]: obv.append(obv[-1] + vols[i])
        elif closes[i] < closes[i-1]: obv.append(obv[-1] - vols[i])
        else: obv.append(obv[-1])
    obv_s = pd.Series(obv)
    recent = obv_s.iloc[-p:]
    if len(recent) < p: return 0.0
    mitad = p // 2
    return float(recent.iloc[-mitad:].mean() - recent.iloc[:mitad].mean())

def confirma_regimen_vwap_ema(precio, vwap, ema20, es_largo):
    """
    "VWAP+EMA de régimen": exige que el precio esté del lado correcto de
    AMBOS (VWAP y EMA20 de 15m) para la dirección de la señal. Usado como
    filtro adicional obligatorio en el sistema de reapertura (<5min).
    """
    if es_largo:
        return precio > vwap and precio > ema20
    return precio < vwap and precio < ema20

def calc_stoch_rsi(s, p=14):
    d=s.diff(); g=d.clip(lower=0).rolling(p).mean(); l=(-d.clip(upper=0)).rolling(p).mean()
    rsi=100-100/(1+g/l.replace(0,np.nan)); mn=rsi.rolling(p).min(); mx=rsi.rolling(p).max()
    return float(((rsi-mn)/(mx-mn+1e-10)*100).iloc[-1])

def patron_vela(df):
    c,o=df["close"].iloc[-1],df["open"].iloc[-1]
    h,l=df["high"].iloc[-1],df["low"].iloc[-1]
    c1,o1=df["close"].iloc[-2],df["open"].iloc[-2]
    rng=h-l
    if rng==0: return "NEUTRO"
    cuerpo=abs(c-o); mi=min(c,o)-l; ms=h-max(c,o)
    if cuerpo/rng<0.1: return "DOJI"
    if mi>2*cuerpo and c>o and c1<o1: return "MARTILLO_ALC"
    if ms>2*cuerpo and c<o and c1>o1: return "SHOOTING_BAJ"
    if c>o and c>o1 and o<c1 and c1<o1: return "ENGULFING_ALC"
    if c<o and c<o1 and o>c1 and c1>o1: return "ENGULFING_BAJ"
    if c>o and cuerpo/rng>0.6: return "VELA_ALC"
    if c<o and cuerpo/rng>0.6: return "VELA_BAJ"
    return "NEUTRO"

def correlacion_propia(df15, btc_mov):
    mov = (df15["close"].iloc[-1]-df15["close"].iloc[-4])/df15["close"].iloc[-4]*100
    return {"mov_propio":round(mov,2),"diverge_fuerte":abs(mov)>=1.5 and abs(mov-btc_mov)>1.2}


# ── Análisis BTC ───────────────────────────────────────────
def analizar_btc() -> dict:
    precio_btc = get_precio("BTCUSDT") or 0
    fuerza=0; detalle=[]; estado="LATERAL"; mov_pct=0.0; caida_brusca=False; subida_brusca=False

    df1d=get_velas("BTCUSDT","1d",50)
    df4h=get_velas("BTCUSDT","4h",100)
    df1h=get_velas("BTCUSDT","1h",100)
    df15=get_velas("BTCUSDT","15m",50)

    if df1d is not None and len(df1d)>=30:
        p=df1d["close"].iloc[-1]; e20=calc_ema(df1d["close"],20); e50=calc_ema(df1d["close"],50)
        r1d=calc_rsi(df1d["close"])
        if p>e20>e50: fuerza+=2; detalle.append(f"📈 Diario alcista RSI:{r1d:.0f}")
        elif p<e20<e50: fuerza-=2; detalle.append(f"📉 Diario bajista RSI:{r1d:.0f}")
        else: detalle.append(f"↔️ Diario lateral RSI:{r1d:.0f}")

    if df4h is not None and len(df4h)>=20:
        p4=df4h["close"].iloc[-1]; e20_4h=calc_ema(df4h["close"],20); r4h=calc_rsi(df4h["close"])
        if p4>e20_4h: fuerza+=1; detalle.append(f"📈 4h alcista RSI:{r4h:.0f}")
        else: fuerza-=1; detalle.append(f"📉 4h bajista RSI:{r4h:.0f}")

    # Detector de caída brusca en 1h
    mov_1h = 0.0
    if df1h is not None and len(df1h)>=4:
        mov_1h = (df1h["close"].iloc[-1]-df1h["close"].iloc[-2])/df1h["close"].iloc[-2]*100
        if mov_1h <= BTC_CAIDA_BRUSCA_PCT:
            caida_brusca = True
            detalle.append(f"💥 CAÍDA BRUSCA BTC: {mov_1h:.1f}% en 1h → buscar CORTOS")
        elif mov_1h >= BTC_SUBIDA_BRUSCA_PCT:
            subida_brusca = True
            detalle.append(f"🚀 SUBIDA BRUSCA BTC: +{mov_1h:.1f}% en 1h → buscar LARGOS")

    if df1h is not None and len(df1h)>=16:
        precio_8h=df1h["close"].iloc[-9]; precio_now=df1h["close"].iloc[-1]
        mov_pct=(precio_now-precio_8h)/precio_8h*100

        ultimas_3h=df1h["close"].iloc[-4:]
        canal_estrecho=(ultimas_3h.max()-ultimas_3h.min())/ultimas_3h.mean()*100<0.7

        rangeando_15m=False
        if df15 is not None and len(df15)>=8:
            u2h=df15["close"].iloc[-8:]
            rangeando_15m=abs(u2h.iloc[4:].mean()-u2h.iloc[:4].mean())/u2h.iloc[:4].mean()*100<0.4

        atr_rec=calc_atr(df1h.tail(8)); atr_ant=calc_atr(df1h.iloc[-16:-8])
        atr_bajando=atr_rec<atr_ant*0.90
        criterios=sum([canal_estrecho,rangeando_15m,atr_bajando])
        rangeo_ok=criterios>=2

        if mov_pct>=1.0 and rangeo_ok:
            estado="SUBIO_RANGEA"; fuerza+=2
            detalle.append(f"🚀 Subió {mov_pct:.1f}% y RANGEA ({criterios}/3) → LARGO")
        elif mov_pct<=-1.0 and rangeo_ok:
            estado="BAJO_RANGEA"; fuerza-=2
            detalle.append(f"💥 Bajó {mov_pct:.1f}% y RANGEA ({criterios}/3) → CORTO")
        elif abs(mov_pct)<1.0:
            estado="LATERAL"; detalle.append(f"↔️ Lateral {mov_pct:.1f}%")
        else:
            estado="EN_MOVIMIENTO"; detalle.append(f"⚠️ Movimiento {mov_pct:.1f}% sin rangeo ({criterios}/3)")

        p1=df1h["close"].iloc[-1]; e20_1h=calc_ema(df1h["close"],20); r1h=calc_rsi(df1h["close"])
        if p1>e20_1h: fuerza+=1; detalle.append(f"📈 1h sobre EMA20 RSI:{r1h:.0f}")
        else: fuerza-=1; detalle.append(f"📉 1h bajo EMA20 RSI:{r1h:.0f}")

    if fuerza>=3: emoji,resumen="🚀","ALCISTA FUERTE"
    elif fuerza>=1: emoji,resumen="📈","ALCISTA"
    elif fuerza<=-3: emoji,resumen="💥","BAJISTA FUERTE"
    elif fuerza<=-1: emoji,resumen="📉","BAJISTA"
    else: emoji,resumen="↔️","LATERAL"

    return {"emoji":emoji,"resumen":resumen,"fuerza":fuerza,"precio":precio_btc,
            "detalle":detalle,"estado":estado,"mov_pct":mov_pct,"caida_brusca":caida_brusca,
            "subida_brusca":subida_brusca,
            "mov_1h":mov_1h}


# ── Grid óptimo ────────────────────────────────────────────
def calcular_grid(precio, atr_pct, score, adx=None):
    # v16: piso mínimo diferenciado por ADX (fuerza de tendencia), en vez de
    # un 6% fijo para todas las monedas por igual. Mismo umbral de 25 que ya
    # se usa para el gate de modo sombra de ADX (consistencia interna).
    # Contexto: datos reales (ATOM, MANA) mostraron que el PnL direccional
    # domina sobre la ganancia de grilla — el beneficio de ensanchar es dar
    # más margen antes de romper rango en tendencia fuerte, no capturar más
    # oscilaciones. Cortes 25/35 recomendados por Claude (27/07), sin dato
    # histórico propio detrás — a revisar con más semanas de automatización.
    if adx is None or adx < 25:
        RANGO_PCT_MINIMO = 6.0    # sin tendencia clara / lateral
    elif adx < 35:
        RANGO_PCT_MINIMO = 7.5   # tendencia moderada
    else:
        RANGO_PCT_MINIMO = 9.0   # tendencia fuerte/sostenida (ej. caso ATOM)
    rango_pct=max(atr_pct*3, RANGO_PCT_MINIMO)
    rango_bajo=round(precio*(1-rango_pct/100),6)
    rango_alto=round(precio*(1+rango_pct/100),6)
    grillas=max(15,min(200,int(rango_pct/0.20)))
    pct_grilla=round(rango_pct/grillas,3)

    max_apal=max(2,min(20,int(precio/max(precio-rango_bajo*0.80,0.0001))))
    if atr_pct>=1.5: apal=min(3,max_apal)
    elif atr_pct>=0.8: apal=min(5,max_apal)
    elif atr_pct>=0.4: apal=min(7,max_apal)
    else: apal=min(10,max_apal)

    liq_largo=round(precio*(1-1/apal),6); liq_corto=round(precio*(1+1/apal),6)
    dist_liq=round((rango_bajo-liq_largo)/precio*100,2)
    apal_sr=max(2,apal-2)
    liq_largo_sr=round(precio*(1-1/apal_sr),6); liq_corto_sr=round(precio*(1+1/apal_sr),6)

    cruces_hora=(atr_pct*4)/pct_grilla if pct_grilla>0 else 0.1
    cruces_1pct=max(1,int(1.0/(pct_grilla*apal/100)/100))
    horas_1pct=cruces_1pct/cruces_hora*1.8 if cruces_hora>0 else 99
    if horas_1pct<1: t1=f"{int(horas_1pct*60)} min"
    elif horas_1pct<8: t1=f"{horas_1pct:.1f} hs"
    else: t1="+8 hs"

    ganancia_8h=round(min(cruces_hora*8*pct_grilla*apal/100,8.0),2)
    tp_obj=max(1.0,round(ganancia_8h*0.6,2))
    sl_largo=round(rango_bajo*0.97,6); sl_corto=round(rango_alto*1.03,6)
    trailing=round(min(ganancia_8h*0.65,5.0),2)

    # Margen de precio aceptable para entrar (hasta 0.5% de movimiento)
    margen_entrada_pct = 0.5
    precio_max_largo = round(precio*(1+margen_entrada_pct/100),6)
    precio_min_corto = round(precio*(1-margen_entrada_pct/100),6)

    if score>=14: preset="🟢 AGRESIVA"
    elif score>=11: preset="🟡 BALANCEADA"
    else: preset="🔴 CONSERVADORA"

    return {
        "rango_bajo":rango_bajo,"rango_alto":rango_alto,"rango_pct":round(rango_pct,2),
        "grillas":grillas,"pct_grilla":pct_grilla,"apal":apal,
        "liq_largo":liq_largo,"liq_corto":liq_corto,"dist_liq":dist_liq,
        "tiempo_1pct":t1,"horas_1pct":horas_1pct,"apto":horas_1pct<=8,
        "ganancia_8h":ganancia_8h,"supera_1pct":ganancia_8h>1.5,
        "sl_largo":sl_largo,"sl_corto":sl_corto,"trailing":trailing,
        "tp_obj":tp_obj,"preset":preset,"cruces_hora":round(cruces_hora,1),
        "apal_sr":apal_sr,"liq_largo_sr":liq_largo_sr,"liq_corto_sr":liq_corto_sr,
        "precio_max_largo":precio_max_largo,"precio_min_corto":precio_min_corto,
        "margen_entrada_pct":margen_entrada_pct,
    }


# ── Análisis de par ────────────────────────────────────────
def analizar_par(par, btc, forzar_corto=False, forzar_largo=False):
    if btc["estado"]=="EN_MOVIMIENTO" and not forzar_corto and not forzar_largo:
        df15c=get_velas(par,"15m",20)
        if df15c is None: return None
        if not correlacion_propia(df15c,btc["mov_pct"])["diverge_fuerte"]: return None

    df15=get_velas(par,"15m",100); df1h=get_velas(par,"1h",100); df4h=get_velas(par,"4h",50)
    if df15 is None or len(df15)<30: return None
    precio=float(df15["close"].iloc[-1])
    if precio<=0: return None

    atr15=calc_atr(df15); atr_pct=(atr15/precio)*100
    bb15=calc_bb(df15["close"]); rsi15=calc_rsi(df15["close"])
    sr15=calc_stoch_rsi(df15["close"]); mc15=calc_macd(df15["close"])
    e20_15=calc_ema(df15["close"],20); pat=patron_vela(df15)
    vol_r=float(df15["vol"].iloc[-1])/max(float(df15["vol"].iloc[-21:-1].mean()),0.0001)
    corr=correlacion_propia(df15,btc["mov_pct"])
    adx15=calc_adx(df15)  # v16: fuerza de tendencia, usado en calcular_grid (piso de ancho)
    vwap15=calc_vwap(df15)  # v16: régimen — usado en modo sombra y reapertura

    # ── PASO 1: determinar dirección CANDIDATA primero ──
    if forzar_corto:
        direccion_cand = "CORTO"
    elif forzar_largo:
        direccion_cand = "LARGO"
    elif btc["estado"]=="SUBIO_RANGEA":
        direccion_cand = "LARGO"
    elif btc["estado"]=="BAJO_RANGEA":
        direccion_cand = "CORTO"
    elif corr["diverge_fuerte"] and corr["mov_propio"]>0:
        direccion_cand = "LARGO"
    elif corr["diverge_fuerte"] and corr["mov_propio"]<0:
        direccion_cand = "CORTO"
    elif rsi15<=42 and mc15["macd"]>mc15["signal"] and (btc["estado"]=="LATERAL" or btc["fuerza"]>=0):
        direccion_cand = "LARGO"
    elif rsi15>=58 and mc15["macd"]<mc15["signal"] and (btc["estado"]=="LATERAL" or btc["fuerza"]<=0):
        direccion_cand = "CORTO"
    else:
        return None

    es_largo = direccion_cand == "LARGO"

    # ── PASO 2: score basado en confirmación de ESA dirección ──
    score=0; razones=[]

    if atr_pct>=0.8: score+=2; razones.append(f"✅ Volatilidad alta: {atr_pct:.2f}%")
    elif atr_pct>=0.2: score+=1; razones.append(f"⚡ Volatilidad media: {atr_pct:.2f}%")
    else: razones.append(f"❌ Volatilidad baja: {atr_pct:.2f}%")

    if bb15["ancho"]>=3.0: score+=2; razones.append(f"✅ Bollinger activo: {bb15['ancho']:.1f}%")
    elif bb15["ancho"]>=1.0: score+=1; razones.append(f"⚡ Bollinger moderado: {bb15['ancho']:.1f}%")
    else: razones.append(f"❌ Bollinger comprimido: {bb15['ancho']:.1f}%")

    if 0.15<=bb15["pos"]<=0.85: score+=1; razones.append(f"✅ Precio en zona grid")

    # RSI ajustado 29/71 para altcoins, confirma dirección específica
    if 29<=rsi15<=71:
        score+=1; razones.append(f"✅ RSI neutro: {rsi15:.1f} (zona oscilación)")
    elif rsi15<29 and es_largo:
        score+=2; razones.append(f"✅ RSI sobreventa: {rsi15:.1f} (confirma LARGO)")
    elif rsi15>71 and not es_largo:
        score+=2; razones.append(f"✅ RSI sobrecompra: {rsi15:.1f} (confirma CORTO)")
    elif rsi15>71 and es_largo:
        razones.append(f"⚠️ RSI sobrecompra: {rsi15:.1f} (CONTRADICE LARGO)")
    elif rsi15<29 and not es_largo:
        razones.append(f"⚠️ RSI sobreventa: {rsi15:.1f} (CONTRADICE CORTO)")
    else:
        razones.append(f"⚡ RSI: {rsi15:.1f}")

    if 20<=sr15<=80: score+=1; razones.append(f"✅ StochRSI: {sr15:.1f}")

    # MACD confirma dirección específica
    if (mc15["cruce_alc"] and es_largo) or (mc15["cruce_baj"] and not es_largo):
        score+=2; razones.append(f"✅ Cruce MACD {'alcista 🟢' if es_largo else 'bajista 🔴'} (confirma)")
    elif mc15["cruce_alc"] or mc15["cruce_baj"]:
        razones.append(f"⚠️ Cruce MACD en contra de la señal")
    elif abs(mc15["hist"])>0: score+=1; razones.append(f"⚡ MACD momentum")

    if forzar_corto:
        score+=2; razones.append(f"✅ BTC caída brusca → CORTO forzado")
    elif forzar_largo:
        score+=2; razones.append(f"✅ BTC subida brusca → LARGO forzado")
    elif btc["estado"]=="SUBIO_RANGEA":
        score+=(2 if precio>e20_15 else 1); razones.append(f"✅ BTC post-suba rangeando → LARGO")
    elif btc["estado"]=="BAJO_RANGEA":
        score+=(2 if precio<e20_15 else 1); razones.append(f"✅ BTC post-baja rangeando → CORTO")
    elif btc["estado"]=="LATERAL":
        score+=1; razones.append(f"✅ BTC lateral")
    elif corr["diverge_fuerte"]:
        score+=1; razones.append(f"✅ Movimiento propio: {corr['mov_propio']}%")

    # Patrones confirman dirección específica
    patrones_alc=["MARTILLO_ALC","ENGULFING_ALC","VELA_ALC"]
    patrones_baj=["SHOOTING_BAJ","ENGULFING_BAJ","VELA_BAJ"]
    if (pat in patrones_alc and es_largo) or (pat in patrones_baj and not es_largo):
        score+=2; razones.append(f"✅ Patrón confirma: {pat}")
    elif pat in patrones_alc or pat in patrones_baj:
        razones.append(f"⚠️ Patrón {pat} contradice la señal")
    elif pat=="DOJI": score+=1; razones.append(f"⚡ Doji")

    if df1h is not None and len(df1h)>=20:
        e20_1h=calc_ema(df1h["close"],20); r1h=calc_rsi(df1h["close"])
        confirma_1h=(precio>e20_1h and es_largo) or (precio<e20_1h and not es_largo)
        if confirma_1h:
            score+=1; razones.append(f"✅ 1h confirma {direccion_cand} (RSI:{r1h:.0f})")
        else:
            razones.append(f"⚠️ 1h en contra de {direccion_cand} (RSI:{r1h:.0f})")

    if vol_r>=1.2: score+=1; razones.append(f"✅ Volumen: {vol_r:.1f}x")
    elif vol_r>=0.7: score+=1; razones.append(f"⚡ Volumen normal: {vol_r:.1f}x")

    if score<MIN_SCORE_ALTA: return None

    pct=score/16*100
    direccion="📈 LARGO" if es_largo else "📉 CORTO"

    # Solo guardamos datos relevantes — sin grid propio (usamos parámetros de Pionex)
    margen_entrada_pct=0.5
    precio_max_largo=round(precio*(1+margen_entrada_pct/100),6)
    precio_min_corto=round(precio*(1-margen_entrada_pct/100),6)

    grid = calcular_grid(precio, atr_pct, score, adx=adx15["adx"])
    razones.append(f"📐 ADX: {adx15['adx']:.1f} (piso grilla: {grid['rango_pct']}%)")

    # ── v16: 4 chequeos en MODO SOMBRA — solo se LOGGEAN, no rechazan la
    # señal. Sirven para armar el informe a los 4 días (ver db.resumen_sombra).
    if df4h is not None and len(df4h)>=20:
        e20_4h=calc_ema(df4h["close"],20)
        sombra_multi_tf = (precio>e20_4h) if es_largo else (precio<e20_4h)
    else:
        sombra_multi_tf = False  # sin dato de 4h disponible -> no aprueba
    sombra_adx_gate = adx15["adx"]>25 and (
        (adx15["plus_di"]>adx15["minus_di"] and es_largo) or
        (adx15["minus_di"]>adx15["plus_di"] and not es_largo)
    )
    sombra_volumen = vol_r>=1.5
    sombra_vwap = (precio>vwap15) if es_largo else (precio<vwap15)
    cci15 = calc_cci(df15)
    sombra_cci = (cci15 < -100) if es_largo else (cci15 > 100)
    obv_slope15 = calc_obv_slope(df15)
    sombra_obv = (obv_slope15 > 0) if es_largo else (obv_slope15 < 0)

    return {
        "par":par,"precio":precio,"score":score,"score_max":16,"pct":pct,
        "prob":"🟢 ALTA","prob_n":3,"direccion":direccion,"razones":razones,
        "atr_pct":atr_pct,"horas_1pct":1.0,  # estimado conservador sin grid propio
        "precio_max_largo":precio_max_largo,
        "precio_min_corto":precio_min_corto,
        "margen_entrada_pct":margen_entrada_pct,
        "vwap":vwap15,"ema20":e20_15,  # v16: expuestos para confirma_regimen_vwap_ema (reapertura)
        "sombra":{"multi_tf":sombra_multi_tf,"adx_gate":sombra_adx_gate,
                  "volumen":sombra_volumen,"vwap":sombra_vwap,
                  "cci":sombra_cci,"obv":sombra_obv},
        **grid,
    }


# ── Contador diario (persistente en SQLite) ─────────────────
def registrar_señal(par, ganancia):
    db.registrar_ganancia_dia(par, ganancia)  # se mantiene solo por compatibilidad histórica

def obj_diario():
    capital_total = gestion_riesgo.CAPITAL_TOTAL_USD
    return db.obj_diario_real_db(OBJETIVO_DIARIO, capital_total)



# ── Alertas de cierre (RECORDATORIO, no confirmación real de TP) ──
def programar_cierre(par, dir, precio, horas, ganancia, tp):
    clave=f"{par}_{hoy_arg()}_{hora_arg()}"
    db.guardar_operacion_abierta(
        clave, par, dir, precio, horas, ganancia, tp,
        hora_arg(), (datetime.now(TZ_ARG)+timedelta(hours=horas)).strftime("%H:%M"),
    )

def verificar_cierres():
    try:
        ahora=datetime.now(TZ_ARG)
        for op in db.operaciones_abiertas_pendientes():
            hc=datetime.strptime(op["cierre_est"],"%H:%M").replace(
                year=ahora.year,month=ahora.month,day=ahora.day,tzinfo=TZ_ARG)
            if ahora>=hc:
                enviar_telegram(
                    f"⏰ <b>RECORDATORIO — {hora_arg()} hs</b>\n"
                    f"📌 {op['par']} | {op['direccion']}\n"
                    f"💰 Entrada: {op['entrada']} | TP estimado: {op['tp']}%\n"
                    f"⏱ Abierta desde: {op['apertura']} hs\n"
                    f"⚠️ Este es un recordatorio basado en tiempo estimado, NO una confirmación de que tocaste el TP.\n"
                    f"✅ Revisá el precio real en Pionex y decidí si cerrar.\n"
                    f"📝 Cuando cierres, usá /cerrar {op['par'].replace('USDT','')} +X.X (o -X.X)"
                )
                db.borrar_operacion_abierta(op["clave"])
    except Exception as e:
        print(f"Error cierres: {e}")



# ── Generar alertas ────────────────────────────────────────
def _abrir_grilla_automatica(r: dict, check: dict):
    """
    Llama a Pionex para abrir la grilla real y devuelve (bu_order_id, mensaje).
    bu_order_id es None si falló. Extraída de generar_alertas() en v16 para
    reusarla también desde intentar_reapertura() sin duplicar el código.
    """
    try:
        # Antes se usaba row=67 fijo, ignorando el cálculo propio de
        # r["grillas"] (rango_pct/0.20, adaptado por volatilidad). Corregido
        # 22/07: validado con fees reales de Pionex (maker 0.02%) que 67 fijo
        # da un espaciado de apenas 1.1x la fee ida-y-vuelta en rangos
        # angostos (3%) — casi sin margen real. La fórmula propia da 5x la
        # fee siempre, sea cual sea el ancho.
        resp = pionex_api.crear_grilla_futuros(
            par=r["par"].replace("USDT", ""),
            top=r["rango_alto"],
            bottom=r["rango_bajo"],
            row=r["grillas"],
            capital_usdt=check["inversion_real"],
            leverage=10,  # FIJO: decisión confirmada, siempre 10x
            trend="long" if r["direccion"] == "📈 LARGO" else "short",
            extra_margin_usdt=check["margen_origen"],
        )
        bu_order_id = resp.get("data", {}).get("buOrderId")
        if bu_order_id:
            mensaje = (
                f"✅ Grilla abierta automáticamente "
                f"(USD {check['inversion_real']:.2f} inversión + "
                f"USD {check['margen_origen']:.2f} margen, "
                f"USD {check['capital_operacion']:.2f} total)"
            )
            return bu_order_id, mensaje
        return None, f"⚠️ Pionex no devolvió buOrderId: {resp}"
    except Exception as e:
        return None, f"⚠️ Error al abrir grilla automática: {e}"


def intentar_reapertura(candidato: dict):
    """
    v16 — Sistema de reapertura (<5min): cuando gestion_riesgo detecta que
    una operación cerró en menos de 5 minutos desde su apertura, llama acá
    con {"par", "senal_id_original", "num_reapertura_actual",
    "direccion_original"}. Corre un análisis COMPLETO de esa única moneda
    (misma vara que una señal nueva: score≥11 + todos los indicadores) MÁS
    la exigencia adicional de VWAP+EMA de régimen. Solo reabre si la
    dirección del análisis fresco coincide con la ORIGINAL (confirmado
    28/07: no se reabre en la dirección contraria aunque el análisis la
    sugiera — es "reabrir la misma posición", no "abrir cualquier cosa en
    esta moneda"). Repetible hasta 2 veces (num_reapertura_actual llega
    como 0 en la primera reapertura, máx. 2).
    """
    par = candidato["par"]
    senal_id_original = candidato["senal_id_original"]
    num_reapertura_actual = candidato["num_reapertura_actual"]

    if db.esta_pausado_global():
        print(f"  ⏸️ {par}: no reabre — bot pausado con /pausar_todo (no se compromete capital nuevo).")
        return

    if num_reapertura_actual >= 2:
        msg = f"🔁 {par}: no se reabre — ya alcanzó el máximo de 2 reaperturas."
        print(f"  {msg}")
        enviar_telegram(msg)
        return

    try:
        btc = analizar_btc()
        r = analizar_par(par, btc)
    except Exception as e:
        msg = f"⚠️ Reapertura {par}: error al analizar ({e})"
        print(f"  {msg}")
        enviar_telegram(msg)
        return

    if r is None:
        msg = f"🔁 {par}: no se reabre — ya no cumple score≥11 con las condiciones actuales."
        print(f"  {msg}")
        enviar_telegram(msg)
        return

    direccion_original = candidato.get("direccion_original")
    if direccion_original and r["direccion"] != direccion_original:
        msg = (f"🔁 {par}: no se reabre — el análisis fresco da {r['direccion']}, distinto a la dirección "
               f"original ({direccion_original}). Solo se reabre la MISMA dirección.")
        print(f"  {msg}")
        enviar_telegram(msg)
        return

    es_largo = r["direccion"] == "📈 LARGO"
    if not confirma_regimen_vwap_ema(r["precio"], r["vwap"], r["ema20"], es_largo):
        msg = f"🔁 {par}: no se reabre — no confirma VWAP+EMA de régimen (score {r['score']}/{r['score_max']} ok, pero régimen no acompaña)."
        print(f"  {msg}")
        enviar_telegram(msg)
        return

    check = gestion_riesgo.verificar_seguridad_apertura(es_reapertura=True)
    if not check["permitido"]:
        msg = f"🔁 {par}: no se reabre — {check['motivo']}"
        print(f"  {msg}")
        enviar_telegram(msg)
        return

    nuevo_num = num_reapertura_actual + 1
    senal_id = db.guardar_senal(r, reabierta_de_id=senal_id_original, num_reapertura=nuevo_num)
    db.guardar_log_sombra(senal_id, par, r["direccion"], **r["sombra"])

    bu_order_id, mensaje = _abrir_grilla_automatica(r, check)
    if bu_order_id:
        db.guardar_bu_order_id(senal_id, bu_order_id, check["capital_operacion"])
        enviar_telegram(
            f"🔁 <b>REAPERTURA #{nuevo_num} — {par.replace('USDT','')}</b>\n"
            f"{r['direccion']}  |  Score: {r['score']}/{r['score_max']}\n"
            f"Precio: {r['precio']:.6g} USDT\n"
            f"{mensaje}\n"
            f"🕐 {hora_arg()} hs (ARG)"
        )
        print(f"  ✅ Reapertura #{nuevo_num} {par} {r['direccion']}")
    else:
        print(f"  ⚠️ Reapertura {par}: {mensaje}")


def generar_alertas(forzar_corto=False, forzar_largo=False):
    try:
        if db.esta_pausado_global():
            print(f"[{hora_arg()}] Bot pausado (/pausar_todo activo)")
            return

        if not en_horario_operativo():
            print(f"[{hora_arg()}] Fuera de horario operativo")
            return

        ahora=hora_arg()
        print(f"\n[{ahora}] Analizando {len(PARES)} pares...")
        verificar_cierres()
        btc=analizar_btc()
        print(f"  BTC: {btc['resumen']} ${btc['precio']:,.0f} estado={btc['estado']} caida={btc['caida_brusca']} subida={btc['subida_brusca']}")

        obj=obj_diario()

        # Alerta especial de caída brusca de BTC — bloquea LARGOS temporalmente
        # (el bloqueo dura lo que dura la condición: se recalcula desde cero
        # cada ciclo de 30 min a partir del precio real, no queda un flag
        # guardado — así que se levanta solo apenas BTC deja de caer fuerte)
        if btc["caida_brusca"] and not forzar_corto and not forzar_largo:
            enviar_telegram(
                f"🚨 <b>CAÍDA BRUSCA BTC — {ahora} hs (ARG)</b>\n"
                f"BTC cayó <b>{btc['mov_1h']:.1f}%</b> en la última hora\n"
                f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
                f"⛔ Buscando solo CORTOS — LARGOS bloqueados mientras dure esto."
            )
            generar_alertas(forzar_corto=True)
            return

        # Alerta especial de subida brusca de BTC — bloquea CORTOS temporalmente
        # (mismo mecanismo: se recalcula cada ciclo, se levanta solo)
        if btc["subida_brusca"] and not forzar_corto and not forzar_largo:
            enviar_telegram(
                f"🚨 <b>SUBIDA BRUSCA BTC — {ahora} hs (ARG)</b>\n"
                f"BTC subió <b>+{btc['mov_1h']:.1f}%</b> en la última hora\n"
                f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
                f"⛔ Buscando solo LARGOS — CORTOS bloqueados mientras dure esto."
            )
            generar_alertas(forzar_largo=True)
            return

        if btc["estado"]=="EN_MOVIMIENTO" and not forzar_corto and not forzar_largo:
            enviar_telegram(
                f"⚠️ <b>BTC en movimiento — {ahora} hs (ARG)</b>\n"
                f"Movimiento: <b>{btc['mov_pct']:.1f}%</b> en 8h | {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
                f"Buscando pares con movimiento propio. Próximo en 30 min."
            )

        resultados=[]
        for par in PARES:
            try:
                r=analizar_par(par,btc,forzar_corto,forzar_largo)
                if r: resultados.append(r)
            except Exception as e:
                print(f"  Error {par}: {e}")
            time.sleep(0.08)

        # Desempate por volatilidad real (atr_pct), no por la estimación
        # teórica de horas_1pct — el análisis de 327 operaciones reales
        # mostró que la volatilidad correlaciona con velocidad de cierre
        # (3.59% en 0-1h vs 2.11% en 3-12h), y el score NO predice velocidad.
        resultados.sort(key=lambda x:(-x["score"],-x["atr_pct"]))

        if not resultados:
            if not forzar_corto and not forzar_largo and btc["estado"]!="EN_MOVIMIENTO":
                enviar_telegram(
                    f"📊 <b>Análisis {ahora} hs (ARG)</b>\n"
                    f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f}) | {btc['estado']}\n"
                    f"Objetivo: {obj['total']}% de {OBJETIVO_DIARIO}% | Faltan: {obj['faltan']}%\n"
                    f"Sin señales ALTA probabilidad. Próximo en 30 min."
                )
            return

        enviadas=0
        aperturas_este_ciclo=0  # v16: tope de MAX_APERTURAS_POR_CICLO (1) en gestion_riesgo
        for r in resultados[:MAX_ALERTAS]:
            # Evitar abrir una segunda grilla en un par que YA tiene una
            # operación sin cerrar — SOLO aplica con automatización activa,
            # porque ahí el bot sabe con certeza qué está abierto en Pionex
            # (bu_order_id). En modo manual, esto dependería de que el
            # usuario siempre haga /cerrar, y si se olvida un solo par,
            # ese par queda bloqueado para siempre sin querer.
            if AUTOMATIZACION_ACTIVA and db.ultima_senal_par(r["par"]) is not None:
                continue

            # Clave por VELA (15 min), no por hora: permite re-alertar el
            # mismo par varias veces en la misma hora si la operación
            # anterior ya cerró (rotación rápida de capital).
            vela = (datetime.now(TZ_ARG).minute // 15) * 15
            clave=f"{r['par']}_{datetime.now(TZ_ARG).strftime('%Y%m%d_%H')}{vela:02d}"
            if db.alerta_existe(clave): continue
            db.marcar_alerta_enviada(clave)

            senal_id = db.guardar_senal(r)
            db.guardar_log_sombra(senal_id, r["par"], r["direccion"], **r["sombra"])

            apertura_auto = None
            if AUTOMATIZACION_ACTIVA:
                check = gestion_riesgo.verificar_seguridad_apertura(aperturas_este_ciclo=aperturas_este_ciclo)
                if check["permitido"]:
                    bu_order_id, apertura_auto = _abrir_grilla_automatica(r, check)
                    if bu_order_id:
                        db.guardar_bu_order_id(senal_id, bu_order_id, check["capital_operacion"])
                        aperturas_este_ciclo += 1
                else:
                    apertura_auto = f"⛔ No se abrió automáticamente: {check['motivo']}"

            # Margen de entrada
            if r["direccion"]=="📈 LARGO":
                margen_txt=f"⚠️ Entrá solo si precio ≤ <b>{r['precio_max_largo']}</b> USDT"
            else:
                margen_txt=f"⚠️ Entrá solo si precio ≥ <b>{r['precio_min_corto']}</b> USDT"

            # Progreso diario
            obj=obj_diario()
            contribucion_pct = round(1.35 * gestion_riesgo.PCT_CAPITAL_POR_OPERACION, 4)
            nuevo_total=round(obj["total"]+contribucion_pct,2)
            prog_txt=(f"📅 Hoy: {obj['total']}% acum. → si esta gana: ~{nuevo_total}% "
                     f"{'✅' if nuevo_total>=OBJETIVO_DIARIO else f'| Faltan: {round(OBJETIVO_DIARIO-nuevo_total,2)}%'}")

            par_corto=r["par"].replace("USDT","")

            # Funding rate informativo
            funding_txt=""
            try:
                df_fr=get_velas(r["par"],"15m",5)
                if df_fr is not None:
                    funding_txt=f"\n💹 Funding rate: verificá en Pionex antes de abrir"
            except: pass

            msg=(
                f"🚨 <b>━━ SEÑAL GRID — PROB. ALTA ━━</b>\n\n"
                f"📌 <b>{r['par']}</b>  {r['direccion']}\n"
                f"🎰 Score: {r['score']}/{r['score_max']} | {r['prob']}\n\n"
                f"── <b>ACCIÓN INMEDIATA</b> ──\n"
                f"Preset Pionex: <b>GRILLAS RECOMENDADAS</b> (o 67 grillas si no aparece)\n"
                f"Take Profit: <b>1.35%</b>\n"
                f"Precio actual: {r['precio']:.6g} USDT\n"
                f"{margen_txt}\n"
                f"{prog_txt}"
                f"{funding_txt}\n"
                + (f"\n{apertura_auto}\n" if apertura_auto else "") +
                f"\n── <b>ANÁLISIS TÉCNICO</b> ──\n"
                +"\n".join(f"  {s}" for s in r["razones"][:8])+
                f"\n\nBTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f}) | {btc['estado']}\n"
                + ("" if AUTOMATIZACION_ACTIVA and apertura_auto and "✅" in apertura_auto
                   else f"📝 /registrar {par_corto} APAL RANGO_BAJO RANGO_ALTO GRILLAS\n")
                + f"🕐 {ahora} hs (ARG)"
            )
            enviar_telegram(msg)
            registrar_señal(r["par"], 1.35)
            programar_cierre(r["par"],r["direccion"],r["precio"],1.0,1.35,1.35)
            enviadas+=1
            print(f"  ✅ {r['par']} {r['direccion']} score={r['score']}")

        print(f"[{ahora}] {enviadas} alertas enviadas.")

    except Exception as e:
        print(f"ERROR CRÍTICO: {e}")
        try: enviar_telegram(f"⚠️ Error técnico: {str(e)[:200]}")
        except: pass


# ── Resumen matutino ───────────────────────────────────────
def resumen_matutino():
    try:
        hoy=hoy_arg()
        if db.resumen_ya_enviado(hoy): return
        db.marcar_resumen_enviado(hoy)
        # No hace falta "resetear" el contador: obj_diario_db() ya filtra por fecha actual.

        btc=analizar_btc()
        candidatos=[]
        for par in PARES:
            try:
                r=analizar_par(par,btc)
                if r: candidatos.append(r)
            except: pass
            time.sleep(0.08)

        candidatos.sort(key=lambda x:-x["score"])
        top3=candidatos[:3]

        lineas=[
            f"☀️ <b>RESUMEN MATUTINO {fecha_arg()}</b>",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🌐 BTC: {btc['emoji']} <b>{btc['resumen']}</b> (${btc['precio']:,.0f})",
            f"Estado: <b>{btc['estado']}</b> | Mov 8h: {btc['mov_pct']:.1f}%",
            f"🎯 Objetivo: <b>{OBJETIVO_DIARIO}%</b> | Solo señales ALTA prob.",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]
        if not top3:
            lineas.append("Sin señales de alta probabilidad al inicio del día.")
        else:
            lineas.append("🏆 <b>Mejores pares para hoy:</b>")
            for i,r in enumerate(top3,1):
                lineas.append(
                    f"\n{i}. <b>{r['par']}</b> — {r['direccion']} | {r['apal']}x\n"
                    f"   TP: {r['tp_obj']}% | Tiempo: {r['tiempo_1pct']} | {r['preset']}"
                )
        lineas+=[f"\n━━━━━━━━━━━━━━━━━━━━",f"🔔 Alertas cada 15 min (:03, :18, :33, :48 hs)"]
        enviar_telegram("\n".join(lineas))
    except Exception as e:
        print(f"Error resumen: {e}")


# ── Main ───────────────────────────────────────────────────
# ── Chequeo liviano de BTC (detección rápida de movimiento brusco) ──
def _chequeo_btc_rapido():
    """
    Corre cada 15 min (:18 y :48), solo consulta BTC (no los 79 pares).
    Si detecta subida/caída brusca, dispara generar_alertas() completo de
    inmediato — reacciona hasta 15 min más rápido que esperar al próximo
    ciclo de :03/:33. Evita quedar con operaciones abiertas en contra de
    un cambio de tendencia que recién se hubiera detectado media hora después.
    """
    try:
        if db.esta_pausado_global():
            return
        if not en_horario_operativo():
            return
        btc = analizar_btc()
        if btc["caida_brusca"] or btc["subida_brusca"]:
            print(f"[{hora_arg()}] Chequeo rápido BTC: movimiento brusco detectado, análisis completo ahora")
            generar_alertas()
    except Exception as e:
        print(f"Error en chequeo rápido BTC: {e}")


def main():
    db.init_db()
    print(f"🤖 Bot v13 iniciado — {len(PARES)} pares")
    enviar_telegram(
        f"🤖 <b>JJ Cripto Bot v13 iniciado</b>\n"
        f"📊 {len(PARES)} pares | Cascada Bybit→OKX→Binance\n"
        f"⏰ 7:00-23:00 ARG | cada 15 min (:03, :18, :33, :48)\n"
        f"🎯 Solo ALTA prob. | TP fijo 1.35% + Grillas recomendadas Pionex\n"
        f"🔧 RSI ajustado 29/71 | Dirección-primero en scoring\n"
        f"🗑️ Eliminados: RNDR,1000SHIB,CYBER,DYDX,MINA,1000BONK\n"
        f"➕ Re-agregado: OP (confirmado disponible de nuevo en Pionex, 20/07)\n"
        f"➕ Nuevos: TON,EIGEN,MOVE,VIRTUAL,PENGU,MOCA,SCR\n"
        f"💾 SQLite | 📊 /diario /semanal /mensual /historial\n"
        f"Comandos: /ayuda"
    )

    for h_arg in (range(0,24) if AUTOMATIZACION_ACTIVA else range(7,23)):
        h_utc=(h_arg+3)%24
        # Escaneo completo cada 15 min (antes cada 30 min con un chequeo
        # liviano de BTC en el medio). Captura señales que antes se perdían
        # entre ciclos, sobre todo operaciones que abren y cierran rápido
        # (mediana histórica de cierre: 6-17 min en varios pares).
        schedule.every().day.at(f"{h_utc:02d}:03").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:18").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:33").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:48").do(generar_alertas)

    if AUTOMATIZACION_ACTIVA:
        def _monitorear():
            try:
                resultado = gestion_riesgo.monitorear_zonas_riesgo()
                acciones = resultado["acciones"]
                if acciones:
                    enviar_telegram("🛡️ <b>Monitoreo de riesgo</b>\n" + "\n".join(acciones))
                # v16: sistema de reapertura — se dispara acá, fuera del
                # ciclo normal de 15 min de generar_alertas(), porque el
                # cierre en <5min se detecta en este monitoreo de riesgo.
                for candidato in resultado["candidatos_reapertura"]:
                    intentar_reapertura(candidato)
            except Exception as e:
                print(f"Error monitoreando riesgo: {e}")
        schedule.every(1).minutes.do(_monitorear)  # v16: 30 -> 1 min, para que la reapertura <5min sea realmente inmediata

    h_res_utc=(9+3)%24
    schedule.every().day.at(f"{h_res_utc:02d}:03").do(resumen_matutino)

    # ── Cronograma de revisión (diario/semanal/mensual) ──────
    def _recordatorio_diario():
        try:
            resumen = telegram_cmds._cmd_diario([])
            enviar_telegram(
                f"📋 <b>Chequeo diario</b>\n"
                f"{resumen}\n\n"
                f"👀 Revisá: ¿corrió sin errores? ¿algún par quedó trabado "
                f"sin /cerrar? ¿hace falta /pausar_todo por algo puntual?"
            )
        except Exception as e:
            print(f"Error recordatorio diario: {e}")

    def _recordatorio_semanal():
        try:
            resumen = telegram_cmds._cmd_semanal()
            enviar_telegram(
                f"📈 <b>Revisión semanal</b>\n"
                f"{resumen}\n\n"
                f"👀 Revisá: win rate de la semana, cuántas veces se reforzó "
                f"margen (zona 🔴), y si el ratio actual de margen de origen "
                f"({round(gestion_riesgo.RATIO_MARGEN_ORIGEN*100)}%) sigue siendo el correcto."
            )
        except Exception as e:
            print(f"Error recordatorio semanal: {e}")

    def _recordatorio_mensual():
        try:
            if datetime.now(TZ_ARG).day != 1:
                return  # el schedule corre diario, pero el contenido solo sale el día 1
            resumen = telegram_cmds._cmd_mensual()
            enviar_telegram(
                f"🗓️ <b>Revisión mensual de parámetros</b>\n"
                f"{resumen}\n\n"
                f"👀 Revisá si siguen vigentes: 9% de capital por operación, "
                f"score mínimo 11, apalancamiento 10x, ratio de margen, "
                f"lista de 79 pares (¿alguno dejó de estar en Pionex?)."
            )
        except Exception as e:
            print(f"Error recordatorio mensual: {e}")

    h_diario_utc = (21+3) % 24    # 21:30 ARG
    schedule.every().day.at(f"{h_diario_utc:02d}:30").do(_recordatorio_diario)

    h_semanal_utc = (20+3) % 24   # domingos 20:00 ARG
    schedule.every().sunday.at(f"{h_semanal_utc:02d}:00").do(_recordatorio_semanal)

    h_mensual_utc = (9+3) % 24    # día 1 de cada mes, 09:00 ARG (chequea internamente la fecha)
    schedule.every().day.at(f"{h_mensual_utc:02d}:00").do(_recordatorio_mensual)

    if en_horario_operativo():
        generar_alertas()

    while True:
        try:
            schedule.run_pending()
            telegram_cmds.revisar_updates()
        except Exception as e: print(f"Error loop: {e}")
        time.sleep(30)

if __name__=="__main__":
    main()
