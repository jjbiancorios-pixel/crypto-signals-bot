import requests
import pandas as pd
import numpy as np
import time
import schedule
from datetime import datetime, timezone, timedelta
import os
import db
import telegram_cmds

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
    "XAIUSDT","ZETAUSDT","ZRXUSDT",
    # 7 pares nuevos (reemplazan RNDR, 1000SHIB, CYBER, DYDX, MINA, 1000BONK, OP)
    # Seleccionados por liquidez y disponibilidad confirmada en Pionex
    "TONUSDT","EIGENUSDT","MOVEUSDT","VIRTUALUSDT",
    "PENGUUSDT","MOCAUSDT","SCRUSDT",
]

MIN_SCORE_ALTA  = 11
MAX_ALERTAS     = 5
HORA_INICIO     = 7
HORA_FIN        = 23   # Hasta las 23hs ARG
OBJETIVO_DIARIO = 3

# Umbral de movimiento de BTC para señal de caída brusca (cortos)
BTC_CAIDA_BRUSCA_PCT = -2.0  # BTC cayó más de 2% en 1h

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
    return HORA_INICIO <= hora_num() < HORA_FIN


# ── Telegram ───────────────────────────────────────────────
def enviar_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


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
    fuerza=0; detalle=[]; estado="LATERAL"; mov_pct=0.0; caida_brusca=False

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
            "mov_1h":mov_1h}


# ── Grid óptimo ────────────────────────────────────────────
def calcular_grid(precio, atr_pct, score):
    rango_pct=atr_pct*3
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
def analizar_par(par, btc, forzar_corto=False):
    if btc["estado"]=="EN_MOVIMIENTO" and not forzar_corto:
        df15c=get_velas(par,"15m",20)
        if df15c is None: return None
        if not correlacion_propia(df15c,btc["mov_pct"])["diverge_fuerte"]: return None

    df15=get_velas(par,"15m",100); df1h=get_velas(par,"1h",100)
    if df15 is None or len(df15)<30: return None
    precio=float(df15["close"].iloc[-1])
    if precio<=0: return None

    atr15=calc_atr(df15); atr_pct=(atr15/precio)*100
    bb15=calc_bb(df15["close"]); rsi15=calc_rsi(df15["close"])
    sr15=calc_stoch_rsi(df15["close"]); mc15=calc_macd(df15["close"])
    e20_15=calc_ema(df15["close"],20); pat=patron_vela(df15)
    vol_r=float(df15["vol"].iloc[-1])/max(float(df15["vol"].iloc[-21:-1].mean()),0.0001)
    corr=correlacion_propia(df15,btc["mov_pct"])

    # ── PASO 1: determinar dirección CANDIDATA primero ──
    if forzar_corto:
        direccion_cand = "CORTO"
    elif btc["estado"]=="SUBIO_RANGEA":
        direccion_cand = "LARGO"
    elif btc["estado"]=="BAJO_RANGEA":
        direccion_cand = "CORTO"
    elif corr["diverge_fuerte"] and corr["mov_propio"]>0:
        direccion_cand = "LARGO"
    elif corr["diverge_fuerte"] and corr["mov_propio"]<0:
        direccion_cand = "CORTO"
    elif rsi15<=42 and mc15["macd"]>mc15["signal"] and btc["fuerza"]>=0:
        direccion_cand = "LARGO"
    elif rsi15>=58 and mc15["macd"]<mc15["signal"] and btc["fuerza"]<=0:
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

    return {
        "par":par,"precio":precio,"score":score,"score_max":16,"pct":pct,
        "prob":"🟢 ALTA","prob_n":3,"direccion":direccion,"razones":razones,
        "atr_pct":atr_pct,"horas_1pct":1.0,  # estimado conservador sin grid propio
        "precio_max_largo":precio_max_largo,
        "precio_min_corto":precio_min_corto,
        "margen_entrada_pct":margen_entrada_pct,
    }


# ── Contador diario (persistente en SQLite) ─────────────────
def registrar_señal(par, ganancia):
    db.registrar_ganancia_dia(par, ganancia)

def obj_diario():
    return db.obj_diario_db(OBJETIVO_DIARIO)



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
def generar_alertas(forzar_corto=False):
    try:
        if not en_horario_operativo():
            print(f"[{hora_arg()}] Fuera de horario operativo")
            return

        ahora=hora_arg()
        print(f"\n[{ahora}] Analizando {len(PARES)} pares...")
        verificar_cierres()
        btc=analizar_btc()
        print(f"  BTC: {btc['resumen']} ${btc['precio']:,.0f} estado={btc['estado']} caida={btc['caida_brusca']}")

        obj=obj_diario()

        # Alerta especial de caída brusca de BTC
        if btc["caida_brusca"] and not forzar_corto:
            enviar_telegram(
                f"🚨 <b>CAÍDA BRUSCA BTC — {ahora} hs (ARG)</b>\n"
                f"BTC cayó <b>{btc['mov_1h']:.1f}%</b> en la última hora\n"
                f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
                f"Buscando mejores pares para CORTO ahora..."
            )
            generar_alertas(forzar_corto=True)
            return

        if btc["estado"]=="EN_MOVIMIENTO" and not forzar_corto:
            enviar_telegram(
                f"⚠️ <b>BTC en movimiento — {ahora} hs (ARG)</b>\n"
                f"Movimiento: <b>{btc['mov_pct']:.1f}%</b> en 8h | {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f})\n"
                f"Buscando pares con movimiento propio. Próximo en 30 min."
            )

        resultados=[]
        for par in PARES:
            try:
                r=analizar_par(par,btc,forzar_corto)
                if r: resultados.append(r)
            except Exception as e:
                print(f"  Error {par}: {e}")
            time.sleep(0.08)

        resultados.sort(key=lambda x:(-x["score"],x["horas_1pct"]))

        if not resultados:
            if not forzar_corto and btc["estado"]!="EN_MOVIMIENTO":
                enviar_telegram(
                    f"📊 <b>Análisis {ahora} hs (ARG)</b>\n"
                    f"BTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f}) | {btc['estado']}\n"
                    f"Objetivo: {obj['total']}% de {OBJETIVO_DIARIO}% | Faltan: {obj['faltan']}%\n"
                    f"Sin señales ALTA probabilidad. Próximo en 30 min."
                )
            return

        enviadas=0
        for r in resultados[:MAX_ALERTAS]:
            clave=f"{r['par']}_{datetime.now(TZ_ARG).strftime('%Y%m%d_%H')}"
            if db.alerta_existe(clave): continue
            db.marcar_alerta_enviada(clave)

            # Margen de entrada
            if r["direccion"]=="📈 LARGO":
                margen_txt=f"⚠️ Entrá solo si precio ≤ <b>{r['precio_max_largo']}</b> USDT"
            else:
                margen_txt=f"⚠️ Entrá solo si precio ≥ <b>{r['precio_min_corto']}</b> USDT"

            # Progreso diario
            obj=obj_diario()
            nuevo_total=round(obj["total"]+1.35,2)
            prog_txt=(f"📅 Hoy: {obj['total']}% acum. → con esta: ~{nuevo_total}% "
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
                f"{funding_txt}\n\n"
                f"── <b>ANÁLISIS TÉCNICO</b> ──\n"
                +"\n".join(f"  {s}" for s in r["razones"][:8])+
                f"\n\nBTC: {btc['emoji']} {btc['resumen']} (${btc['precio']:,.0f}) | {btc['estado']}\n"
                f"📝 /registrar {par_corto} APAL RANGO_BAJO RANGO_ALTO GRILLAS\n"
                f"🕐 {ahora} hs (ARG)"
            )
            enviar_telegram(msg)
            registrar_señal(r["par"], 1.35)
            db.guardar_senal(r)
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
        lineas+=[f"\n━━━━━━━━━━━━━━━━━━━━",f"🔔 Alertas a los :03 y :33 hs (7:00-23:00 ARG)"]
        enviar_telegram("\n".join(lineas))
    except Exception as e:
        print(f"Error resumen: {e}")


# ── Main ───────────────────────────────────────────────────
def main():
    db.init_db()
    print(f"🤖 Bot v13 iniciado — {len(PARES)} pares")
    enviar_telegram(
        f"🤖 <b>JJ Cripto Bot v13 iniciado</b>\n"
        f"📊 {len(PARES)} pares | Cascada Bybit→OKX→Binance\n"
        f"⏰ 7:00-23:00 ARG | :03 y :33 de cada hora\n"
        f"🎯 Solo ALTA prob. | TP fijo 1.35% + Grillas recomendadas Pionex\n"
        f"🔧 RSI ajustado 29/71 | Dirección-primero en scoring\n"
        f"🗑️ Eliminados: RNDR,1000SHIB,CYBER,DYDX,MINA,1000BONK,OP\n"
        f"➕ Nuevos: TON,EIGEN,MOVE,VIRTUAL,PENGU,MOCA,SCR\n"
        f"💾 SQLite | 📊 /diario /semanal /mensual /historial\n"
        f"Comandos: /ayuda"
    )

    for h_arg in range(7,23):
        h_utc=(h_arg+3)%24
        schedule.every().day.at(f"{h_utc:02d}:03").do(generar_alertas)
        schedule.every().day.at(f"{h_utc:02d}:33").do(generar_alertas)

    h_res_utc=(9+3)%24
    schedule.every().day.at(f"{h_res_utc:02d}:03").do(resumen_matutino)

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
