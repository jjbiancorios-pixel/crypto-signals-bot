import requests
import pandas as pd
import numpy as np
import time
import threading
import schedule
from datetime import datetime, timezone, timedelta
import os
import db
import telegram_cmds
import gestion_riesgo
import paxg_bot
import bingx_bot
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
    "WIFUSDT","FLOKIUSDT",
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
    # 10/08: 14 pares nuevos, disponibilidad confirmada por Juanjo en la
    # app (de 15 propuestos, HIGH no está disponible como bot y se sacó)
    "FILUSDT","BCHUSDT","DASHUSDT","RENDERUSDT",
    "JUPUSDT","NEOUSDT","LPTUSDT","PENDLEUSDT","ENSUSDT",
    "YGGUSDT","MANTAUSDT","TRBUSDT","API3USDT",
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
    try:
        data = r.json()
    except Exception:
        # 19/08 — diagnóstico: antes esto tiraba "Expecting property name..."
        # sin mostrar qué mandó Bybit de verdad. Loguea el cuerpo real
        # (recortado) para poder investigar la causa real, no adivinar de
        # nuevo (ya nos pasó con el caso XMR/delisting).
        raise ValueError(f"bybit no-json (status={r.status_code}): {r.text[:150]!r}")
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

def get_velas(par, tf, n=100, verbose=False):
    """17/08: mismo agregado que get_precio() — ver ese docstring."""
    for f in (_velas_binance, _velas_okx, _velas_bybit):
        nombre_fuente = f.__name__.replace("_velas_", "")
        try:
            df = f(par, tf, n)
            if df is not None and len(df) >= 20:
                if verbose:
                    print(f"🕯️ {par} ({tf}): velas de {nombre_fuente}, última close={df['close'].iloc[-1]}")
                return df
            if verbose:
                print(f"🕯️ {par} ({tf}): {nombre_fuente} devolvió datos insuficientes")
        except Exception as e:
            if verbose:
                print(f"🕯️ {par} ({tf}): {nombre_fuente} falló — {e}")
            continue
    if verbose:
        print(f"🕯️ {par} ({tf}): las 3 fuentes fallaron, sin velas.")
    return None

def _precio_bybit(par):
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={par}", timeout=6)
    try:
        data = r.json()
    except Exception:
        raise ValueError(f"bybit no-json (status={r.status_code}): {r.text[:150]!r}")
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

def get_precio(par, verbose=False):
    """
    17/08: agregado parámetro verbose — antes cualquier fallo de cada
    fuente (Bybit/OKX/Binance) se tragaba en silencio (except: continue),
    sin dejar rastro de qué pasó ni qué valor devolvió cada una. Caso
    real: XMRUSDT abrió con un rango totalmente desconectado del precio
    real, sin forma de reconstruir después qué fuente dio el dato malo.
    Con verbose=True, loguea cada intento (fuente + resultado).
    """
    for f in (_precio_binance, _precio_okx, _precio_bybit):
        nombre_fuente = f.__name__.replace("_precio_", "")
        try:
            p = f(par)
            if p and p > 0:
                if verbose:
                    print(f"💲 {par}: precio de {nombre_fuente} = {p}")
                return p
            if verbose:
                print(f"💲 {par}: {nombre_fuente} devolvió valor inválido ({p})")
        except Exception as e:
            if verbose:
                print(f"💲 {par}: {nombre_fuente} falló — {e}")
            continue
    if verbose:
        print(f"💲 {par}: las 3 fuentes fallaron, sin precio.")
    return None


def comparar_fuentes_precio(par: str) -> dict:
    """
    19/08 — Diagnóstico: consulta las 3 fuentes de la cascada externa
    SIN cascada (todas, no para en la primera que responde) MÁS el
    precio DIRECTO de Pionex — la referencia real que importa, ya que
    es contra ESE precio que se valida el rango antes de abrir. Permite
    ver de un vistazo si el problema es nuestra cascada externa (alguna
    da un valor raro comparada con Pionex) o algo más general.
    """
    resultado = {}
    for f in (_precio_binance, _precio_okx, _precio_bybit):
        nombre = f.__name__.replace("_precio_", "")
        try:
            resultado[nombre] = f(par)
        except Exception as e:
            resultado[nombre] = f"ERROR: {e}"
    try:
        resultado["pionex_directo"] = pionex_api.obtener_precio_pionex_directo(par)
    except Exception as e:
        resultado["pionex_directo"] = f"ERROR: {e}"
    return resultado


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
    # v18 (21/08, Juanjo) — rango de grilla fijo en 20% (antes escalonado
    # 6/7.5/9% según ADX). Contexto: se detectó que operaciones de minutos
    # no daban tiempo a que el grid complete ni una ronda (caso real ACE:
    # 0 rondas en 2 min, 100% del resultado fue ruido de precio amplificado
    # por apalancamiento, no señal real) — rango más ancho + SL/TP más
    # amplios (ver STOP_LOSS_INICIAL_PIONEX/TP_FIJO_SIMPLE) buscan dar
    # tiempo real a que se distinga tendencia de ruido, apoyados en el
    # 5% de capital por operación (más margen para tolerar el rango ancho).
    RANGO_PCT_MINIMO = 20.0
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
        corr_temprana = correlacion_propia(df15c,btc["mov_pct"])
        if not corr_temprana["diverge_fuerte"]: return None
        # 19/08 — pedido explícito de Juanjo tras pérdidas del 25% en un
        # día: cuando BTC tiene tendencia fuerte, NO ir en la dirección
        # CONTRARIA a BTC, aunque la moneda diverja fuerte en ese sentido
        # opuesto (antes solo exigía divergencia en magnitud, sin
        # importar si era a favor o en contra del rumbo de BTC).
        va_contra_btc = (btc["mov_pct"] > 0 and corr_temprana["mov_propio"] < 0) or \
                        (btc["mov_pct"] < 0 and corr_temprana["mov_propio"] > 0)
        if va_contra_btc:
            return None

    df15=get_velas(par,"15m",100); df1h=get_velas(par,"1h",100); df4h=get_velas(par,"4h",50)
    if df15 is None or len(df15)<30: return None
    precio=float(df15["close"].iloc[-1])
    if precio<=0: return None

    atr15=calc_atr(df15); atr_pct=(atr15/precio)*100
    bb15=calc_bb(df15["close"]); rsi15=calc_rsi(df15["close"])
    # v18 (21/08) — modo sombra: ancho de Bollinger de hace 5 velas (75min
    # atrás en 15m) para comparar si se está EXPANDIENDO (tendencia
    # arrancando) o CONTRAYENDO (todavía comprimido/rango). Solo se loguea,
    # no bloquea — evaluar con más datos si conviene exigirlo.
    try:
        bb15_previo = calc_bb(df15["close"].iloc[:-5])
        sombra_bb_expandiendo = bb15["ancho"] > bb15_previo["ancho"]
    except Exception:
        sombra_bb_expandiendo = False
    sr15=calc_stoch_rsi(df15["close"]); mc15=calc_macd(df15["close"])
    e20_15=calc_ema(df15["close"],20); pat=patron_vela(df15)
    vol_r=float(df15["vol"].iloc[-1])/max(float(df15["vol"].iloc[-21:-1].mean()),0.0001)
    corr=correlacion_propia(df15,btc["mov_pct"])
    # v18 (21/08) — modo sombra: movimiento propio de la moneda medido en
    # múltiplos de su ATR, no en % crudo — un 0.5% puede ser enorme para
    # una moneda tranquila e insignificante para una volátil. Solo se
    # loguea, no bloquea.
    sombra_movimiento_atr = round(abs(corr["mov_propio"]) / atr_pct, 2) if atr_pct > 0 else 0.0
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

    bono_btc_lateral = False  # 18/08: default — solo True cuando de verdad se aplicó el bono del experimento
    if forzar_corto:
        score+=2; razones.append(f"✅ BTC caída brusca → CORTO forzado")
    elif forzar_largo:
        score+=2; razones.append(f"✅ BTC subida brusca → LARGO forzado")
    elif btc["estado"]=="SUBIO_RANGEA":
        score+=(2 if precio>e20_15 else 1); razones.append(f"✅ BTC post-suba rangeando → LARGO")
    elif btc["estado"]=="BAJO_RANGEA":
        score+=(2 if precio<e20_15 else 1); razones.append(f"✅ BTC post-baja rangeando → CORTO")
    elif btc["estado"]=="LATERAL":
        # 23/08 (SACADO, pedido de Juanjo) — el experimento "Opción 2"
        # (+2 en vez de +1 cuando además diverge fuerte) se midió con
        # /experimento_btc_lateral: de 53 señales que calificaron SOLO
        # por este bono, 51 ya cerradas dieron 13.7% de win rate y
        # -0.33% de resultado promedio — muy por debajo del resto del
        # sistema (~98% históricamente). Vuelve a +1 fijo, sin mirar
        # divergencia propia en este caso.
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

    score_sin_bono_lateral = score - 1 if bono_btc_lateral else score  # 18/08: para comparar el viernes vs. el escenario sin el cambio
    if score<MIN_SCORE_ALTA:
        # 18/08 — antes se descartaba en silencio, sin dejar ningún rastro
        # de cuánto le faltó ni qué criterio falló. Ahora devuelve info
        # liviana para el análisis de "por qué tan pocas señales" — el
        # caller decide qué hacer según el score (log liviano para todas,
        # seguimiento simulado completo para las que casi llegan).
        return {"no_califico": True, "par": par, "score": score, "direccion_candidata": direccion_cand,
                "razones": razones, "btc_estado": btc.get("estado"), "precio": precio,
                "bono_btc_lateral": bono_btc_lateral, "score_sin_bono_lateral": score_sin_bono_lateral,
                "movimiento_atr": sombra_movimiento_atr}

    # 17/08: la señal calificó (score>=11) — recién ACÁ vale la pena
    # loguear el detalle completo de la cascada de precio (fuente que
    # respondió + valor), para no saturar los logs con los ~90 pares que
    # no califican en cada ciclo. Consulta extra (no reemplaza el precio
    # ya usado arriba, es solo para dejar rastro diagnóstico) — caso real
    # que motivó esto: XMRUSDT abrió con un rango totalmente desconectado
    # del precio real, sin ningún rastro de qué fuente dio el dato malo.
    try:
        get_precio(par, verbose=True)
    except Exception as e:
        print(f"⚠️ {par}: falló el logging verbose de precio — {e}")

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
        # 24/08 (FIX) — antes esto bloqueaba directo (False) cuando
        # fallaba la consulta de 4h por CUALQUIER motivo — incluidos
        # errores de red que ya vimos en los logs (Bybit 403, etc.). Un
        # fallo de conexión no debería descartar una señal técnicamente
        # buena. Ahora se trata como "sin dato" (None) — no aprueba pero
        # tampoco cuenta como una falla real del par, se distingue en el
        # log de bloqueo.
        sombra_multi_tf = None
    # 24/08 (Juanjo, investigación exhaustiva) — ADX bajado de 25 a 20:
    # con 25, bloqueaba ~90% de todos los candidatos con score>=11 de
    # forma sostenida, en TODOS los estados de BTC (LATERAL, SUBIO_
    # RANGEA, BAJO_RANGEA por igual) — el estado de BTC no determina el
    # ADX de cada moneda individual, así que la falta casi total de
    # candidatas no era "BTC quieto", era el umbral en sí.
    #
    # 26/08 (Juanjo, Opción A) — bajado de 20 a 15: confirmado que ADX
    # seguía siendo el bloqueador dominante (~90%+ de los descartes) aun
    # en 20. Probar unas horas — si sigue habiendo pocas o ninguna
    # recomendación, Juanjo indicó escalar a Opción B (sacar la
    # exigencia de que el DI confirme la dirección exacta, dejar solo la
    # fuerza de tendencia) — NO implementar B todavía, solo si se
    # confirma que A no alcanzó.
    sombra_adx_gate = adx15["adx"]>15 and (
        (adx15["plus_di"]>adx15["minus_di"] and es_largo) or
        (adx15["minus_di"]>adx15["plus_di"] and not es_largo)
    )
    # v18 (21/08) — TODOS los indicadores de sombra se calculan ACÁ,
    # ANTES de los 2 filtros duros (no después) — antes, si ADX o
    # multi_tf bloqueaban, el código cortaba con return None sin llegar a
    # calcular volumen/CCI/OBV/persistencia para ese candidato, dejando
    # CIEGO cualquier intento de comparar "¿los bloqueados por ADX
    # mostraban señales buenas en los demás indicadores, o eran
    # débiles en general?" — pregunta real de Juanjo, sin poder
    # responderla por este motivo. Ahora se calcula todo primero, se
    # decide el bloqueo después, y se guarda el cuadro completo.
    sombra_volumen = vol_r>=1.5
    sombra_vwap = (precio>vwap15) if es_largo else (precio<vwap15)
    cci15 = calc_cci(df15)
    sombra_cci = (cci15 < -100) if es_largo else (cci15 > 100)
    obv_slope15 = calc_obv_slope(df15)
    sombra_obv = (obv_slope15 > 0) if es_largo else (obv_slope15 < 0)
    sombra_persistio = db.senal_persistio_ciclo_anterior(par, direccion)

    # v18 (21/08, Juanjo) — 2 chequeos PROMOVIDOS de modo sombra a FILTRO
    # DURO: antes solo se registraban para el informe, ahora bloquean la
    # señal directamente. Motivo: con operaciones más pacientes (rango
    # 20%, SL/TP amplios, trailing recién a partir de 4%), conviene exigir
    # más certeza de que hay tendencia real detrás antes de comprometer
    # capital por más tiempo — no alcanza con "parece que hay algo", como
    # sí podía alcanzar cuando las operaciones cerraban en minutos.
    detalle_sombra_completo = {
        "multi_tf": sombra_multi_tf, "adx_gate": sombra_adx_gate,
        "volumen": sombra_volumen, "vwap": sombra_vwap, "cci": sombra_cci, "obv": sombra_obv,
        "bb_expandiendo": sombra_bb_expandiendo, "movimiento_atr": sombra_movimiento_atr,
        "persistio_ciclo_anterior": sombra_persistio,
    }
    if not sombra_adx_gate:
        db.guardar_bloqueo_filtro_duro(par, "adx_gate", score, direccion, detalle_sombra_completo)
        # 21/08 (pedido de Juanjo) — seguimiento simulado (mismo mecanismo
        # que ya existía para señales bloqueadas por falta de capital):
        # sin esto, sabíamos CUÁNTAS veces bloqueaba cada filtro, pero no
        # cómo les hubiera ido de verdad a esas señales descartadas.
        db.guardar_senal_simulada(
            {"par": par, "direccion": direccion, "precio": precio, "apal": 10, "score": score, "razones": razones, "movimiento_atr": sombra_movimiento_atr},
            motivo_no_apertura="filtro_duro_adx_gate"
        )
        # 22/08 (FIX CRÍTICO) — antes devolvía None directo. El caller
        # (generar_alertas) solo loguea el score completo cuando recibe
        # un dict con "no_califico": True — con None no hacía NADA,
        # ni siquiera el log liviano. Esto dejaba a /distribucion_scores
        # CIEGO para cualquier candidato que llegaba a score>=11 y después
        # se bloqueaba acá — nunca se veía un 11+ en el reporte, aunque sí
        # existieran, porque simplemente no se registraban.
        return {"no_califico": True, "par": par, "score": score, "direccion_candidata": direccion,
                "razones": razones, "btc_estado": btc.get("estado"), "precio": precio,
                "bono_btc_lateral": bono_btc_lateral, "score_sin_bono_lateral": score_sin_bono_lateral}
    if sombra_multi_tf is False:
        db.guardar_bloqueo_filtro_duro(par, "multi_tf", score, direccion, detalle_sombra_completo)
        db.guardar_senal_simulada(
            {"par": par, "direccion": direccion, "precio": precio, "apal": 10, "score": score, "razones": razones, "movimiento_atr": sombra_movimiento_atr},
            motivo_no_apertura="filtro_duro_multi_tf"
        )
        return {"no_califico": True, "par": par, "score": score, "direccion_candidata": direccion,
                "razones": razones, "btc_estado": btc.get("estado"), "precio": precio,
                "bono_btc_lateral": bono_btc_lateral, "score_sin_bono_lateral": score_sin_bono_lateral}

    # 23/08 (Juanjo) — volumen PROMOVIDO de modo sombra a filtro duro,
    # mismo patrón que ADX/multi_tf: una "tendencia" sin volumen de
    # respaldo real es más sospechosa de ser ruido.
    if not sombra_volumen:
        db.guardar_bloqueo_filtro_duro(par, "volumen", score, direccion, detalle_sombra_completo)
        db.guardar_senal_simulada(
            {"par": par, "direccion": direccion, "precio": precio, "apal": 10, "score": score, "razones": razones, "movimiento_atr": sombra_movimiento_atr},
            motivo_no_apertura="filtro_duro_volumen"
        )
        return {"no_califico": True, "par": par, "score": score, "direccion_candidata": direccion,
                "razones": razones, "btc_estado": btc.get("estado"), "precio": precio,
                "bono_btc_lateral": bono_btc_lateral, "score_sin_bono_lateral": score_sin_bono_lateral}

    # 23/08 (Juanjo) — persistencia PROMOVIDA de modo sombra a filtro
    # duro: exige que la MISMA señal (par+dirección) haya calificado
    # también en el ciclo anterior (15 min atrás) antes de abrir — filtra
    # señales que aparecen y se revierten rápido. Como una señal
    # bloqueada acá NUNCA llega a guardarse en la tabla "senales" (se
    # descarta), sombra_persistio (que mira esa tabla) nunca podría
    # encontrar el ciclo anterior de un candidato que también fue
    # bloqueado — por eso se guarda aparte en candidato_pendiente_log,
    # exista o no una apertura real detrás. db.candidato_persistio() mide
    # CUÁNTOS candidatos que llegaron hasta acá NO tenían el ciclo
    # anterior — es el número que permite ver cuánto reduce esto la
    # cantidad de señales, tal como pidió Juanjo.
    # 24/08 (Juanjo) — persistencia REVERTIDA a modo sombra: exigir que
    # la MISMA señal sobreviva 2 ciclos seguidos, apilado sobre ADX +
    # alineación 4h + volumen ya activos, resultó demasiado restrictivo
    # en conjunto — varios días sin ninguna candidata, en distintos
    # estados de BTC (LATERAL, SUBIO_RANGEA, BAJO_RANGEA), descartando
    # la hipótesis de que fuera solo "BTC quieto". Sigue midiendo (ver
    # /filtros_duros, categoría "persistencia") para decidir más
    # adelante con más datos si conviene reactivarlo, pero ya NO bloquea.
    ya_persistio = db.candidato_persistio(par, direccion)
    db.guardar_candidato_pendiente(par, direccion)
    if not ya_persistio:
        db.guardar_bloqueo_filtro_duro(par, "persistencia", score, direccion, detalle_sombra_completo)
        db.guardar_senal_simulada(
            {"par": par, "direccion": direccion, "precio": precio, "apal": 10, "score": score, "razones": razones, "movimiento_atr": sombra_movimiento_atr},
            motivo_no_apertura="filtro_duro_persistencia"
        )
        # sin bloqueo real: ya no se descarta la señal, sigue de largo

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
                  "cci":sombra_cci,"obv":sombra_obv,
                  "bb_expandiendo":sombra_bb_expandiendo,"movimiento_atr":sombra_movimiento_atr,
                  "persistio_ciclo_anterior":sombra_persistio},
        "bono_btc_lateral": bono_btc_lateral, "score_sin_bono_lateral": score_sin_bono_lateral,  # 18/08: comparación viernes
        **grid,
    }


# ── Contador diario (persistente en SQLite) ─────────────────
def registrar_señal(par, ganancia):
    db.registrar_ganancia_dia(par, ganancia)  # se mantiene solo por compatibilidad histórica

def obj_diario():
    """
    05/08 (FIX): usaba gestion_riesgo.CAPITAL_TOTAL_USD (el fijo viejo,
    782) en vez del capital real del día que ya calcula el interés
    compuesto — con el capital real más alto, el % salía inflado
    (confirmado: mostraba 0.62% cuando el real era 0.502%). Ahora usa el
    capital real de hoy, con el mismo fallback de siempre si todavía no
    corrió el recálculo diario.
    """
    cap_diario = db.obtener_capital_diario()
    capital_total = cap_diario["capital_dia"] if cap_diario else gestion_riesgo.CAPITAL_TOTAL_USD
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
    # 17/08 (FIX DE SEGURIDAD): caso real XMRUSDT — el precio usado para
    # calcular el rango vino mal por una causa que no se pudo confirmar
    # (la hipótesis inicial de que XMR estaba delistado de Binance/OKX
    # resultó ser FALSA, Juanjo lo confirmó viendo el par listado en vivo
    # — no repetir esa explicación).
    #
    # 19/08 (FIX REAL): el mismo bug volvió a pasar CON esta verificación
    # ya activa — se descubrió por qué no lo atrapaba: usaba
    # obtener_precio_mercado(), que consulta la MISMA cascada externa
    # Bybit/OKX/Binance que calculó el rango original. Si esa cascada
    # tiene un problema de datos persistente para un par (como XMR),
    # ambas consultas devuelven el MISMO valor malo — comparando algo
    # mal contra sí mismo, nunca hay discrepancia. Corregido: ahora
    # compara contra obtener_precio_pionex_directo() — el precio de
    # PIONEX MISMO (su propio motor de trading, vía endpoint público),
    # totalmente independiente de la cascada externa.
    try:
        precio_real_actual = pionex_api.obtener_precio_pionex_directo(r["par"])
    except Exception:
        precio_real_actual = None

    # 20/08 (pedido de Juanjo) — chequeo de CERTEZA de entrada: compara el
    # precio fresco contra el precio del ANÁLISIS original (no contra el
    # rango, que ya se recalcula aparte) — si se movió más de 1% desde
    # que se armó la señal, se descarta la apertura directamente, en vez
    # de abrir igual con el rango recentrado. Motivo: el rango recentrado
    # mantiene la grilla sana mecánicamente, pero si el precio ya se movió
    # mucho, la tesis técnica original (RSI/MACD/etc.) puede haber
    # quedado desactualizada igual. Umbral 1%, no 2% — ya sabemos que la
    # sola comisión de cierre come ~0.5% (caso real XLM), dejando poco
    # margen para además tolerar mucha deriva de precio y seguir
    # cumpliendo el objetivo de no arrancar peor de -1%.
    UMBRAL_DERIVA_ENTRADA_PCT = 1.0
    if precio_real_actual is not None and r.get("precio"):
        deriva_pct = abs(precio_real_actual - r["precio"]) / r["precio"] * 100
        bloqueado = deriva_pct > UMBRAL_DERIVA_ENTRADA_PCT
        # 20/08 — registro para el informe del domingo (pedido de
        # Juanjo): guarda CADA caso que pasa por acá, bloquee o no, para
        # medir con datos reales qué proporción de señales quedan afuera.
        try:
            db.guardar_deriva_entrada(r["par"], r.get("direccion"), r["precio"], precio_real_actual, deriva_pct, bloqueado)
        except Exception as e:
            print(f"⚠️ No se pudo guardar el registro de deriva de entrada: {e}")
        if bloqueado:
            return None, (
                f"⛔ DESCARTADA — el precio se movió {deriva_pct:.2f}% desde el análisis "
                f"({r['precio']} → {precio_real_actual}), por encima del {UMBRAL_DERIVA_ENTRADA_PCT}% "
                f"aceptado. La señal pudo haber quedado desactualizada. NO se abrió la grilla."
            )

    # 17/08 (FIX DE SEGURIDAD): caso real XMRUSDT — el precio usado para
    # calcular el rango vino mal por una causa que no se pudo confirmar
    # (la hipótesis inicial de que XMR estaba delistado de Binance/OKX
    # resultó ser FALSA, Juanjo lo confirmó viendo el par listado en vivo
    # — no repetir esa explicación).
    #
    # 19/08 (FIX REAL): el mismo bug volvió a pasar CON esta verificación
    # ya activa — se descubrió por qué no lo atrapaba: usaba
    # obtener_precio_mercado(), que consulta la MISMA cascada externa
    # Bybit/OKX/Binance que calculó el rango original. Si esa cascada
    # tiene un problema de datos persistente para un par (como XMR),
    # ambas consultas devuelven el MISMO valor malo — comparando algo
    # mal contra sí mismo, nunca hay discrepancia. Corregido: ahora
    # compara contra obtener_precio_pionex_directo() — el precio de
    # PIONEX MISMO (su propio motor de trading, vía endpoint público),
    # totalmente independiente de la cascada externa.
    if precio_real_actual is not None:
        margen_tolerancia = (r["rango_alto"] - r["rango_bajo"])  # mismo ancho del rango, de colchón extra a cada lado
        piso_aceptable = r["rango_bajo"] - margen_tolerancia
        techo_aceptable = r["rango_alto"] + margen_tolerancia
        if not (piso_aceptable <= precio_real_actual <= techo_aceptable):
            return None, (
                f"🚨 ABORTADO — el precio REAL de Pionex ({precio_real_actual}) está muy lejos del "
                f"rango calculado ({r['rango_bajo']}-{r['rango_alto']}) — probable dato de precio erróneo "
                f"al armar la señal (ver caso real XMRUSDT). NO se abrió la grilla. Revisar el par manualmente."
            )
        # 19/08 (FIX REAL DE ENTRADA) — caso real: casi todas las
        # operaciones abrían ya en -1% a -2% desde el arranque. Causa:
        # r["precio"]/rango_bajo/rango_alto se calculan con el CIERRE de
        # una vela de 15min en el momento del análisis — para cuando la
        # orden real se manda (después de recorrer los 94 pares, uno por
        # uno), pueden pasar varios minutos, tiempo de sobra para que el
        # precio real se mueva 1-2%. El rango quedaba centrado en un
        # precio viejo, no en el precio real de entrada. Ahora se
        # RECALCULA el rango centrado en precio_real_actual (recién
        # consultado, igual fórmula que calcular_grid), manteniendo el
        # mismo ancho porcentual — la orden entra centrada en el precio
        # de verdad, no en uno de hace varios minutos.
        rango_pct_original = r["rango_pct"]
        rango_bajo_fresco = round(precio_real_actual * (1 - rango_pct_original / 100), 6)
        rango_alto_fresco = round(precio_real_actual * (1 + rango_pct_original / 100), 6)
    else:
        # 19/08 — si Pionex mismo no responde, no abrir a ciegas: sin
        # verificación real, es mejor abortar que arriesgar otro caso XMR.
        return None, "🚨 ABORTADO — no se pudo confirmar el precio real de Pionex antes de abrir. NO se abrió la grilla, por seguridad."

    # 17/08 — Reintento automático ante BOT_INTERNAL_ERROR de Pionex (caso
    # real: INJUSDT, señal de score 11/16 perdida por completo porque
    # Pionex devolvió un error interno puntual y transitorio del lado de
    # ELLOS, no un problema de nuestros datos). 2 reintentos más, con una
    # pausa corta entre cada uno, antes de dar la señal por perdida.
    #
    # 25/08 — Se probó subir a 5 intentos con espera creciente
    # (3-6-12-24s) tras ver el mismo patrón repetirse con INJ/NEAR/ZRX,
    # pero Juanjo pidió NO subirlo — el sistema de señales funcionó bien
    # históricamente, a confirmar primero si esto fue una situación
    # puntual antes de tocar los tiempos. Revertido a 3 intentos / 3s
    # fijos. 26/08: CONFIRMADO que este revert nunca había quedado
    # guardado en el paquete real entregado (quedó solo en un espacio de
    # trabajo temporal) — ZRX volvió a fallar con "5 intentos" pese al
    # pedido explícito de revertir. Corregido ahora, verificado que
    # quede en el archivo real.
    MAX_INTENTOS_APERTURA = 3
    for intento in range(1, MAX_INTENTOS_APERTURA + 1):
        try:
            # Antes se usaba row=67 fijo, ignorando el cálculo propio de
            # r["grillas"] (rango_pct/0.20, adaptado por volatilidad). Corregido
            # 22/07: validado con fees reales de Pionex (maker 0.02%) que 67 fijo
            # da un espaciado de apenas 1.1x la fee ida-y-vuelta en rangos
            # angostos (3%) — casi sin margen real. La fórmula propia da 5x la
            # fee siempre, sea cual sea el ancho.
            # 23/08 (Juanjo) — SL dimensionado por el ATR propio de la
            # moneda, en vez del fijo -10% para todas — más ancho para
            # las volátiles, nunca más ajustado que el piso mínimo de
            # -15%.
            sl_atr = pionex_api.calcular_sl_atr(r["atr_pct"])
            r["sl_atr_usado"] = sl_atr  # 26/08: para que quien llama pueda guardarlo en guardar_bu_order_id
            resp = pionex_api.crear_grilla_futuros(
                par=r["par"].replace("USDT", ""),
                top=rango_alto_fresco,
                bottom=rango_bajo_fresco,
                row=r["grillas"],
                capital_usdt=check["inversion_real"],
                leverage=10,  # FIJO: decisión confirmada, siempre 10x
                trend="long" if r["direccion"] == "📈 LARGO" else "short",
                extra_margin_usdt=check["margen_origen"],
                sl_pct=sl_atr,
            )
            bu_order_id = resp.get("data", {}).get("buOrderId")
            if bu_order_id:
                mensaje = (
                    f"✅ Grilla abierta automáticamente "
                    f"(USD {check['inversion_real']:.2f} inversión + "
                    f"USD {check['margen_origen']:.2f} margen, "
                    f"USD {check['capital_operacion']:.2f} total)"
                    + (f" — tras {intento} intentos" if intento > 1 else "")
                )
                # 20/08 (punto 1, pedido de Juanjo) — verificación
                # post-apertura: consulta el resultado real apenas se
                # confirma la orden, para medir con datos reales cuánto
                # se pierde de entrada (comisión de apertura + spread +
                # cualquier desfasaje residual) — puramente informativo,
                # no cambia ninguna decisión, da visibilidad para seguir
                # optimizando la certeza de entrada.
                try:
                    desglose_inicial = pionex_api.calcular_resultado_desglosado(
                        bu_order_id, par=r["par"], capital_total_real=check["capital_operacion"]
                    )
                    if desglose_inicial and desglose_inicial.get("total_pct") is not None:
                        resultado_inicial = desglose_inicial["total_pct"]
                        if resultado_inicial < -1.0:
                            mensaje += f"\n⚠️ Precisión de entrada: ya arrancó en {resultado_inicial:+.2f}% (por debajo del -1% buscado) — revisar."
                        else:
                            mensaje += f"\n📍 Precisión de entrada: {resultado_inicial:+.2f}% al confirmar."
                except Exception:
                    pass
                return bu_order_id, mensaje

            codigo_error = resp.get("code", "")
            if codigo_error == "BOT_INTERNAL_ERROR" and intento < MAX_INTENTOS_APERTURA:
                print(f"⚠️ {r['par']}: BOT_INTERNAL_ERROR de Pionex (intento {intento}/{MAX_INTENTOS_APERTURA}), reintentando en 3s...")
                time.sleep(3)
                continue
            return None, f"⚠️ Pionex no devolvió buOrderId tras {intento} intento(s): {resp}"
        except Exception as e:
            if intento < MAX_INTENTOS_APERTURA:
                print(f"⚠️ {r['par']}: error al abrir grilla (intento {intento}/{MAX_INTENTOS_APERTURA}) — {e}, reintentando en 3s...")
                time.sleep(3)
                continue
            return None, f"⚠️ Error al abrir grilla automática tras {intento} intento(s): {e}"


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

    if r is None or (isinstance(r, dict) and r.get("no_califico")):
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
        db.guardar_bu_order_id(senal_id, bu_order_id, check["capital_operacion"], sl_propio_pct=r.get("sl_atr_usado"))
        enviar_telegram(
            f"🔁 <b>REAPERTURA #{nuevo_num} — {par.replace('USDT','')}</b>\n"
            f"{r['direccion']}  |  Score: {r['score']}/{r['score_max']}\n"
            f"Precio: {r['precio']:.6g} USDT\n"
            f"{mensaje}\n"
            f"🕐 {hora_arg()} hs (ARG)"
        )
        print(f"  ✅ Reapertura #{nuevo_num} {par} {r['direccion']}")
    else:
        # 19/08 (FIX) — mismo bug que en generar_alertas(): si la
        # apertura falla acá, la señal de la reapertura (creada arriba)
        # quedaba con cerrado=0 para siempre, bloqueando el par de por
        # vida — un segundo camino al mismo problema que no se había
        # revisado. Se cierra la señal fantasma explícitamente.
        db.cerrar_senal_automatica(senal_id, 0, motivo="reapertura_fallida")
        print(f"  ⚠️ Reapertura {par}: {mensaje}")
        enviar_telegram(f"⚠️ Reapertura {par} falló y quedó descartada — {mensaje}")


def _loguear_frescura_velas():
    """
    18/08 — Mide qué tan "fresca" está la última vela de 15m al momento
    del ciclo de análisis (hoy con 3 min de margen: :03/:18/:33/:48) —
    para decidir el viernes, con datos propios, si se puede achicar el
    margen a 1-2 min sin riesgo de trabajar con datos todavía no
    asentados en alguna de las 3 fuentes de la cascada.
    """
    for nombre_fuente, funcion in (("bybit", _velas_bybit), ("okx", _velas_okx), ("binance", _velas_binance)):
        try:
            df = funcion("BTCUSDT", "15m", 5)
            ts_ultima = float(df["ts"].iloc[-1])
            ts_dt = datetime.fromtimestamp(ts_ultima / 1000 if ts_ultima > 1e12 else ts_ultima, tz=timezone.utc)
            ahora = datetime.now(timezone.utc)
            minutos_desde_ts = round((ahora - ts_dt).total_seconds() / 60, 2)
            db.guardar_frescura_velas(nombre_fuente, minutos_desde_ts)
        except Exception as e:
            print(f"⚠️ frescura velas {nombre_fuente}: {e}")


def generar_alertas(forzar_corto=False, forzar_largo=False):
    try:
        if db.esta_pausado_global():
            print(f"[{hora_arg()}] Bot pausado (/pausar_todo activo)")
            return

        if not en_horario_operativo():
            print(f"[{hora_arg()}] Fuera de horario operativo")
            return

        _loguear_frescura_velas()  # 18/08 — mide antes de arrancar el análisis en sí

        # 19/08 — Limpieza de señales fantasma AL INICIO de cada ciclo
        # (cada 15 min, no cada 30) — antes de escanear los 94 pares. Si
        # se dejaba solo en el schedule aparte de 30 min, una señal nueva
        # y válida podía perderse igual durante la ventana de espera
        # (misma "ya tenía señal registrada" que causó todo esto). Acá
        # queda garantizado que ningún par arranca el ciclo bloqueado por
        # una fantasma que ya se podría haber limpiado.
        try:
            n_fantasmas = db.limpiar_senales_fantasma()
            if n_fantasmas > 0:
                enviar_telegram(f"🧹 Limpieza automática: {n_fantasmas} señal(es) fantasma cerrada(s), pares liberados.")
        except Exception as e:
            print(f"Error limpiando fantasmas al inicio del ciclo: {e}")

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
                f"Buscando pares con movimiento propio. Próximo en 15 min."
            )

        resultados=[]
        for par in PARES:
            try:
                r=analizar_par(par,btc,forzar_corto,forzar_largo)
                if r and not r.get("no_califico"):
                    resultados.append(r)
                elif r and r.get("no_califico"):
                    # 18/08 — investigación de por qué hay pocas señales:
                    # log liviano de CUALQUIER score (todos los 94 pares,
                    # todos los ciclos), y seguimiento simulado completo
                    # (con TP/SL, igual que las que sí califican pero se
                    # quedan sin capital) para las que casi llegan (9-10),
                    # para ver si hubiera valido la pena bajar el umbral.
                    db.guardar_score_completo(par, r["score"], r.get("direccion_candidata"),
                                               r.get("btc_estado"), r.get("razones"))
                    if r["score"] in (9, 10):
                        db.guardar_senal_simulada(
                            {"par": par, "direccion": r["direccion_candidata"], "precio": r["precio"],
                             "apal": 10, "score": r["score"], "razones": r.get("razones"),
                             "movimiento_atr": r.get("movimiento_atr")},
                            motivo_no_apertura=f"score_bajo_{r['score']}"
                        )
                    # 18/08 — experimento "Opción 2" (BTC lateral + divergencia
                    # propia): si el bono se aplicó, guardar para comparar el
                    # viernes contra el escenario sin el cambio.
                    if r.get("bono_btc_lateral"):
                        db.guardar_experimento_btc_lateral(par, r["score"], r["score_sin_bono_lateral"],
                                                            r.get("direccion_candidata"))
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
                    f"Sin señales ALTA probabilidad. Próximo en 15 min."
                )
            return

        enviadas=0
        aperturas_este_ciclo=0  # v16: tope de MAX_APERTURAS_POR_CICLO (1) en gestion_riesgo
        candidatas_descartadas = []  # 19/08 — para poder decir EXACTAMENTE qué par y por qué, no un mensaje genérico
        for r in resultados[:MAX_ALERTAS]:
            # Evitar abrir una segunda grilla en un par que YA tiene una
            # operación sin cerrar — SOLO aplica con automatización activa,
            # porque ahí el bot sabe con certeza qué está abierto en Pionex
            # (bu_order_id). En modo manual, esto dependería de que el
            # usuario siempre haga /cerrar, y si se olvida un solo par,
            # ese par queda bloqueado para siempre sin querer.
            if AUTOMATIZACION_ACTIVA and db.ultima_senal_par(r["par"]) is not None:
                candidatas_descartadas.append(f"{r['par']} (ya tenía una señal/operación registrada)")
                continue

            # Clave por VELA (15 min), no por hora: permite re-alertar el
            # mismo par varias veces en la misma hora si la operación
            # anterior ya cerró (rotación rápida de capital).
            vela = (datetime.now(TZ_ARG).minute // 15) * 15
            clave=f"{r['par']}_{datetime.now(TZ_ARG).strftime('%Y%m%d_%H')}{vela:02d}"
            if db.alerta_existe(clave):
                candidatas_descartadas.append(f"{r['par']} (alerta duplicada en esta misma vela de 15 min)")
                continue
            db.marcar_alerta_enviada(clave)

            senal_id = db.guardar_senal(r)
            db.guardar_log_sombra(senal_id, r["par"], r["direccion"], **r["sombra"])

            # 18/08 — experimento "Opción 2": si esta señal (que SÍ
            # calificó) tuvo el bono de BTC lateral + divergencia propia,
            # guardar con su senal_id real — así el viernes podemos ver
            # el resultado real de las que calificaron GRACIAS al bono.
            if r.get("bono_btc_lateral"):
                db.guardar_experimento_btc_lateral(r["par"], r["score"], r["score_sin_bono_lateral"],
                                                    r.get("direccion"), senal_id)

            apertura_auto = None
            if AUTOMATIZACION_ACTIVA:
                check = gestion_riesgo.verificar_seguridad_apertura(aperturas_este_ciclo=aperturas_este_ciclo)
                if check["permitido"]:
                    bu_order_id, apertura_auto = _abrir_grilla_automatica(r, check)
                    if bu_order_id:
                        db.guardar_bu_order_id(senal_id, bu_order_id, check["capital_operacion"], sl_propio_pct=r.get("sl_atr_usado"))
                        aperturas_este_ciclo += 1
                        # v17 — si se liberó una posición para hacerle
                        # lugar a esta señal, avisar explícitamente.
                        lib = check.get("liberacion")
                        if lib and lib.get("liberado"):
                            enviar_telegram(
                                f"💰 {lib['par']}: liberada anticipadamente ({lib['resultado_pct']:+.2f}%, "
                                f"ya había superado el piso de 1.35%) para hacerle lugar a {r['par']} "
                                f"(score {r['score']})."
                            )
                    else:
                        # 19/08 (FIX CRÍTICO) — antes, si la apertura real
                        # fallaba (verificación de seguridad de precio,
                        # BOT_INTERNAL_ERROR agotando los 3 reintentos,
                        # cualquier excepción), la señal quedaba guardada
                        # con cerrado=0 PARA SIEMPRE — bloqueando ese par
                        # de por vida (db.ultima_senal_par nunca más
                        # devolvía None). Caso real: XMRUSDT y PORTALUSDT
                        # quedaron "fantasma" así. Ahora se cierra la
                        # señal con motivo claro, liberando el par.
                        db.cerrar_senal_automatica(senal_id, 0, motivo="apertura_fallida")
                        enviar_telegram(f"⚠️ {r['par']}: la apertura automática falló y la señal quedó descartada — {apertura_auto}")
                else:
                    apertura_auto = f"⛔ No se abrió automáticamente: {check['motivo']}"
                    # 03/08: como no consiguió lugar real, se guarda aparte
                    # para simular su comportamiento (MAE/MFE, TP/stop-loss)
                    # sin arriesgar capital — más datos de patrones, más rápido.
                    db.guardar_senal_simulada({**r, "movimiento_atr": r.get("sombra", {}).get("movimiento_atr")}, motivo_no_apertura=check["motivo"])
                    # 19/08 (FIX CRÍTICO) — mismo problema que el caso de
                    # apertura fallida: la señal simulada es un registro
                    # APARTE (tabla senales_simuladas) — el senal_id
                    # ORIGINAL de la tabla principal (senales) seguía
                    # quedando con cerrado=0 para siempre, bloqueando el
                    # par igual. Se cierra acá también.
                    db.cerrar_senal_automatica(senal_id, 0, motivo="bloqueada_sin_capital")

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

            estado_apertura_prominente = (
                "✅ <b>ABIERTA AUTOMÁTICAMENTE</b>" if (AUTOMATIZACION_ACTIVA and apertura_auto and "✅" in apertura_auto)
                else "⛔ <b>NO SE ABRIÓ — solo señal informativa</b>" if AUTOMATIZACION_ACTIVA
                else "📝 <b>MODO MANUAL — registrala vos con /registrar</b>"
            )
            msg=(
                f"🚨 <b>━━ SEÑAL GRID — PROB. ALTA ━━</b>\n"
                f"{estado_apertura_prominente}\n\n"
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

        # 19/08 (FIX) — antes, si había candidatos (score≥11) pero
        # ninguno terminaba en alerta nueva (todos ya tenían señal
        # registrada, o la clave de vela ya existía), NO se mandaba
        # ningún mensaje — silencio total, indistinguible de un bot
        # caído. Caso real: horas sin ningún mensaje pese a que el bot
        # sí estaba corriendo y analizando.
        if enviadas == 0 and not forzar_corto and not forzar_largo and btc["estado"]!="EN_MOVIMIENTO":
            detalle_descarte = "\n".join(f"• {d}" for d in candidatas_descartadas) if candidatas_descartadas else "(sin detalle — revisar)"
            enviar_telegram(
                f"📊 <b>Análisis {ahora} hs (ARG)</b>\n"
                f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f}) | {btc['estado']}\n"
                f"Objetivo: {obj['total']}% de {OBJETIVO_DIARIO}% | Faltan: {obj['faltan']}%\n"
                f"{len(resultados)} candidata(s) con score alto, sin alertas nuevas:\n{detalle_descarte}\n"
                f"Próximo en 15 min."
            )

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
                if r and not r.get("no_califico"): candidatos.append(r)
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
    print(f"🤖 Bot v16 iniciado — {len(PARES)} pares")
    # 16/08 (FIX CRÍTICO): descarta el backlog viejo de Telegram al
    # arrancar — antes cada reinicio hacía que los comandos nuevos
    # quedaran en cola detrás de mensajes de semanas atrás, sin ningún
    # error visible (ver telegram_cmds.inicializar_offset_telegram).
    telegram_cmds.inicializar_offset_telegram()
    # 21/08: actualizado a v18 — capital, límites, SL/TP/trailing y
    # filtros duros armados dinámico desde las variables reales, mismo
    # criterio que el fix del 16/08 (nunca más texto fijo desactualizado).
    horario_real = "24hs (0:00-23:59)" if AUTOMATIZACION_ACTIVA else "7:00-22:59 ARG"
    capital_pct = gestion_riesgo.PCT_CAPITAL_POR_OPERACION * 100
    margen_pct = gestion_riesgo.RATIO_MARGEN_ORIGEN * 100
    inversion_pct = 100 - margen_pct
    enviar_telegram(
        f"🤖 <b>JJ Cripto Bot v18 iniciado</b>\n"
        f"📊 {len(PARES)} pares | Cascada Bybit→OKX→Binance\n"
        f"⏰ {horario_real} | cada 15 min (:01, :16, :31, :46)\n"
        f"💰 Capital {capital_pct:.1f}%x{gestion_riesgo.MAX_POSICIONES_SIMULTANEAS} posiciones | sin reserva | 10x fijo\n"
        f"📐 Rango de grilla: 20% fijo\n"
        f"🛑 SL inicial {pionex_api.SL_INICIAL_SIMPLE*100:.0f}% | TP nativo {pionex_api.TP_FIJO_SIMPLE*100:.0f}%\n"
        f"📈 Breakeven-stop hasta {pionex_api.UMBRAL_TRAILING_SIMPLE}%, luego trailing con {pionex_api.RETROCESO_PROPORCIONAL_TRAILING*100:.0f}% de retroceso proporcional al pico\n"
        f"✅ Filtros duros: ADX+DI, alineación EMA 4h | 🔬 Sombra: volumen, VWAP, CCI, OBV, Bollinger, ATR, persistencia\n"
        f"💾 SQLite | 📊 /diario /semanal /mensual /historial /escanear /corregir\n"
        f"Comandos: /ayuda"
    )

    for h_arg in (range(0,24) if AUTOMATIZACION_ACTIVA else range(7,23)):
        h_utc=(h_arg+3)%24
        # Escaneo completo cada 15 min (antes cada 30 min con un chequeo
        # liviano de BTC en el medio). Captura señales que antes se perdían
        # entre ciclos, sobre todo operaciones que abren y cierran rápido
        # (mediana histórica de cierre: 6-17 min en varios pares).
        # 19/08: margen post-cierre de vela bajado de 3 a 1 minuto (pedido
        # directo de Juanjo — /frescura_velas se había armado para medir
        # y decidir con datos el viernes, pero se adelantó el cambio).
        schedule.every().day.at(f"{h_utc:02d}:01").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:16").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:31").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:46").do(generar_alertas)

    if AUTOMATIZACION_ACTIVA:
        def _monitorear():
            try:
                # 01/08: interés compuesto — se reintenta acá en cada ciclo
                # por si a las 00:01 había operaciones abiertas y se
                # pospuso (la función misma chequea si ya corresponde o no,
                # es seguro llamarla de más).
                aviso_capital = gestion_riesgo.intentar_recalculo_diario()
                if aviso_capital:
                    enviar_telegram(aviso_capital)

                resultado = gestion_riesgo.monitorear_zonas_riesgo()
                acciones = resultado["acciones"]
                if acciones:
                    enviar_telegram("🛡️ <b>Monitoreo de riesgo</b>\n" + "\n".join(acciones))
                # v16: sistema de reapertura — se dispara acá, fuera del
                # ciclo normal de 15 min de generar_alertas(), porque el
                # cierre en <5min se detecta en este monitoreo de riesgo.
                for candidato in resultado["candidatos_reapertura"]:
                    intentar_reapertura(candidato)

                # 03/08: seguimiento de señales simuladas (no arriesga
                # capital, no manda avisos por Telegram para no saturar —
                # se consulta con /simuladas cuando se quiera revisar).
                gestion_riesgo.simular_seguimiento()
            except Exception as e:
                print(f"Error monitoreando riesgo: {e}")
        # 21/08 (FIX) — sacado de schedule.every(1).minutes acá — se movió
        # a un hilo aparte más abajo, mismo motivo que el chequeo rápido
        # del trailing: este schedule vivía en el hilo único que también
        # escanea 94 pares, quedando congelado durante esa ventana (1-3
        # min cada 15 min). Afectaba checkpoints de pérdida reales,
        # reapertura Y el seguimiento simulado (ver /filtros_duros: 0
        # cierres en 2 horas, parte de la explicación era esto).

        # 04/08: cinturón separado PAXG/BTC — modo sombra, 24 combinaciones
        # simuladas, sin capital real. Cada 15 min (no necesita la misma
        # frecuencia que v16, y evita saturar la API gratuita de oro).
        def _paxg_ciclo():
            try:
                paxg_bot.analizar_y_simular()
            except Exception as e:
                print(f"Error en cinturón PAXG/BTC: {e}")
        schedule.every(15).minutes.do(_paxg_ciclo)

        # 05/08: cinturón de INVESTIGACIÓN BingX — modo sombra puro, sin
        # operar nada, sin API key. Cada 30 seg (el mínimo que da el loop
        # principal, que duerme 30 seg entre chequeos) para tener
        # suficiente densidad de datos en la ventana de 1-5 min.
        def _bingx_ciclo():
            try:
                bingx_bot.recopilar_datos()
            except Exception as e:
                print(f"Error en cinturón de investigación BingX: {e}")
            try:
                # 10/08: cinturón BingX-martingala en modo sombra (2
                # variantes A/B), mismo ciclo, sin capital real.
                bingx_bot.simular_martingala()
            except Exception as e:
                print(f"Error en cinturón BingX-martingala: {e}")
        schedule.every(30).seconds.do(_bingx_ciclo)

        # 10/08: chequeo de reconciliación — compara las grillas reales de
        # Pionex contra nuestro tracking, detecta posiciones huérfanas
        # (caso real: INJUSDT). Cada 30 min, no hace falta más frecuencia.
        def _chequeo_huerfanas():
            try:
                avisos = gestion_riesgo.chequear_huerfanas()
                for aviso in avisos:
                    enviar_telegram(aviso)
            except Exception as e:
                print(f"Error en chequeo de huérfanas: {e}")
        schedule.every(30).minutes.do(_chequeo_huerfanas)

        # 19/08 — El chequeo de señales fantasma se movió al INICIO de
        # cada ciclo de análisis (cada 15 min, en generar_alertas) — no
        # hace falta un schedule aparte acá, sería redundante y corre
        # menos seguido (cada 30 min) que el ciclo normal.

        # 01/08: intento "principal" a las 00:01 ARG (=03:01 UTC, servidor
        # corre en UTC) — normalmente alcanza con este, el de _monitorear()
        # (arriba) es solo el respaldo por si justo a esa hora había
        # operaciones abiertas.
        schedule.every().day.at("03:01").do(
            lambda: enviar_telegram(msg) if (msg := gestion_riesgo.intentar_recalculo_diario()) else None
        )

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

    # v17 — chequeo rápido del piso ascendente de ganancia, SOLO para
    # posiciones que ya superaron 1.35% (no todas) — necesita reaccionar
    # más rápido que el monitoreo normal de 1 min para no perder margen
    # entre que el precio retrocede y subimos el piso real de Pionex.
    def _chequeo_rapido_ganancia_v17():
        try:
            acciones = gestion_riesgo.chequeo_rapido_ganancia_v17()
            for accion in acciones:
                enviar_telegram(accion)
        except Exception as e:
            print(f"Error en chequeo rápido de ganancia v17: {e}")

    # 21/08 (FIX CRÍTICO) — antes esto corría vía schedule.every(2).seconds
    # DENTRO del mismo loop de un solo hilo que también hace el escaneo
    # completo de 94 pares (generar_alertas). El schedule del paquete
    # "schedule" es cooperativo: mientras generar_alertas() está
    # corriendo (puede tardar 1-3 min real, entre las esperas entre pares
    # y el tiempo de cada consulta), NADA MÁS puede correr — el chequeo
    # rápido del trailing queda completamente congelado durante ese
    # tiempo. Caso real: ACEUSDT estuvo en +1% "un rato" según Juanjo,
    # pero nuestro sistema solo registró un pico de +0.01% — coincide con
    # una ventana de escaneo bloqueando el chequeo justo en ese momento.
    # Ahora corre en un HILO APARTE con su propio loop, totalmente
    # independiente del escaneo de pares — nunca se congela, sin importar
    # cuánto tarde generar_alertas().
    def _loop_chequeo_rapido():
        while True:
            _chequeo_rapido_ganancia_v17()
            time.sleep(2)
    threading.Thread(target=_loop_chequeo_rapido, daemon=True).start()

    # 21/08 (FIX) — mismo motivo que el hilo de arriba: _monitorear
    # (checkpoints de pérdida reales, techo absoluto, reapertura <5min, y
    # simular_seguimiento) corría en schedule.every(1).minutes DENTRO del
    # hilo único que también escanea 94 pares — quedaba congelado durante
    # esa ventana. Ahora en su propio hilo, cada 60 segundos real,
    # nunca bloqueado por el escaneo.
    def _loop_monitoreo_1min():
        while True:
            _monitorear()
            # 23/08 (Juanjo) — monitoreo del trading REAL de PAXG, mismo
            # hilo que el resto (independiente del escaneo de 94 pares,
            # mismo motivo que arriba). PAXG no necesita chequeo cada
            # 2seg como el cinturón principal (Juanjo: "estas operaciones
            # duran más") — 1 minuto es un ritmo razonable.
            try:
                paxg_bot.monitorear_paxg_real()
            except Exception as e:
                print(f"Error en monitoreo real de PAXG: {e}")
            time.sleep(60)
    threading.Thread(target=_loop_monitoreo_1min, daemon=True).start()

    if en_horario_operativo():
        generar_alertas()

    while True:
        try:
            schedule.run_pending()
            telegram_cmds.revisar_updates()
        except Exception as e: print(f"Error loop: {e}")
        # v17 (FIX): antes dormía 30s, lo que impedía que un schedule de
        # 15s (el chequeo rápido del piso de ganancia) se cumpliera de
        # verdad — con schedule.run_pending() solo evaluándose cada 30s,
        # un job de 15s terminaba corriendo a la misma cadencia que todo
        # lo demás. Ahora duerme 10s — liviano, no debería afectar nada
        # más (de paso, Telegram también se revisa más seguido).
        time.sleep(1.5)

if __name__=="__main__":
    main()
