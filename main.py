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

# Pares a monitorear (los más líquidos de Pionex Futuros)
PARES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "DOTUSDT"
]

INTERVALO   = "15m"   # Velas de 15 minutos
LIMITE      = 100     # Cantidad de velas a analizar
MIN_SCORE   = 3       # Mínimo de señales para alertar (sobre 5)

alertas_enviadas = {}  # Evita repetir alertas del mismo par


# ── Telegram ───────────────────────────────────────────────
def enviar_telegram(mensaje: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error Telegram: {e}")


# ── Datos de Binance (gratis, sin API key) ─────────────────
def obtener_velas(par: str) -> pd.DataFrame | None:
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={par}&interval={INTERVALO}&limit={LIMITE}"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 50:
            return None
        df = pd.DataFrame(data, columns=[
            "timestamp","open","high","low","close","volume",
            "ct","qav","trades","tbbav","tbqav","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"Error velas {par}: {e}")
        return None


# ── Indicadores ────────────────────────────────────────────
def calcular_rsi(serie: pd.Series, periodo: int = 14) -> pd.Series:
    delta = serie.diff()
    ganancia = delta.clip(lower=0).rolling(periodo).mean()
    perdida  = (-delta.clip(upper=0)).rolling(periodo).mean()
    rs = ganancia / perdida.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calcular_atr(df: pd.DataFrame, periodo: int = 14) -> pd.Series:
    hl  = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift()).abs()
    lcp = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hcp, lcp], axis=1).max(axis=1)
    return tr.rolling(periodo).mean()

def calcular_bollinger(serie: pd.Series, periodo: int = 20) -> tuple:
    media  = serie.rolling(periodo).mean()
    std    = serie.rolling(periodo).std()
    upper  = media + 2 * std
    lower  = media - 2 * std
    ancho  = (upper - lower) / media * 100   # % del precio
    return upper, lower, ancho

def calcular_macd(serie: pd.Series) -> tuple:
    ema12  = serie.ewm(span=12).mean()
    ema26  = serie.ewm(span=26).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    return macd, signal


# ── Análisis principal ─────────────────────────────────────
def analizar_par(par: str) -> dict | None:
    df = obtener_velas(par)
    if df is None:
        return None

    precio = df["close"].iloc[-1]
    rsi    = calcular_rsi(df["close"]).iloc[-1]
    atr    = calcular_atr(df).iloc[-1]
    bb_up, bb_low, bb_ancho = calcular_bollinger(df["close"])
    macd, signal             = calcular_macd(df["close"])

    atr_pct    = (atr / precio) * 100
    macd_val   = macd.iloc[-1]
    signal_val = signal.iloc[-1]
    bb_ancho_actual = bb_ancho.iloc[-1]

    # Volumen relativo (actual vs promedio últimas 20 velas)
    vol_actual  = df["volume"].iloc[-1]
    vol_promedio = df["volume"].iloc[-21:-1].mean()
    vol_ratio   = vol_actual / vol_promedio if vol_promedio > 0 else 1

    score = 0
    razones = []

    # 1. Volatilidad ATR suficiente para cubrir 1% con apalancamiento
    if atr_pct >= 0.3:
        score += 1
        razones.append(f"✅ Volatilidad ATR: {atr_pct:.2f}%")
    else:
        razones.append(f"❌ Volatilidad baja ATR: {atr_pct:.2f}%")

    # 2. Bollinger Bands con buen ancho (mercado activo)
    if bb_ancho_actual >= 1.5:
        score += 1
        razones.append(f"✅ Bandas Bollinger activas: {bb_ancho_actual:.2f}%")
    else:
        razones.append(f"❌ Bollinger comprimido: {bb_ancho_actual:.2f}%")

    # 3. RSI en zona neutral-activa (no extremos peligrosos)
    if 35 <= rsi <= 65:
        score += 1
        razones.append(f"✅ RSI zona activa: {rsi:.1f}")
    elif rsi < 35:
        score += 1
        razones.append(f"⚡ RSI sobreventa: {rsi:.1f} (rebote posible)")
    elif rsi > 65:
        score += 1
        razones.append(f"⚡ RSI sobrecompra: {rsi:.1f} (corrección posible)")
    else:
        razones.append(f"❌ RSI extremo: {rsi:.1f}")

    # 4. MACD con momentum
    if abs(macd_val - signal_val) > 0:
        score += 1
        razones.append(f"✅ MACD con momentum")
    else:
        razones.append(f"❌ MACD sin señal clara")

    # 5. Volumen elevado (confirma movimiento real)
    if vol_ratio >= 1.2:
        score += 1
        razones.append(f"✅ Volumen elevado: {vol_ratio:.1f}x")
    else:
        razones.append(f"❌ Volumen bajo: {vol_ratio:.1f}x")

    # Dirección sugerida
    if rsi < 45 and macd_val > signal_val:
        direccion = "📈 LARGO"
    elif rsi > 55 and macd_val < signal_val:
        direccion = "📉 CORTO"
    else:
        direccion = "↔️ NEUTRAL"

    # Rango sugerido para el Grid (±2x ATR)
    rango_bajo  = round(precio - 2 * atr, 4)
    rango_alto  = round(precio + 2 * atr, 4)

    # Apalancamiento óptimo según volatilidad
    if atr_pct >= 1.0:
        apalancamiento = 3
        apal_razon = "Volatilidad alta → apalancamiento conservador"
    elif atr_pct >= 0.5:
        apalancamiento = 5
        apal_razon = "Volatilidad media → apalancamiento moderado"
    else:
        apalancamiento = 10
        apal_razon = "Volatilidad baja → apalancamiento alto seguro"

    # Tiempo estimado para 1% de profit
    # Basado en cuántas veces el precio recorre el ATR por hora
    # En 100 velas de 15min = 25 horas de datos
    oscilaciones_por_hora = (atr_pct * 4) / 100  # veces que se mueve el ATR por hora
    profit_por_hora = oscilaciones_por_hora * (atr_pct / 2) * apalancamiento
    if profit_por_hora > 0:
        horas_estimadas = 1 / profit_por_hora
        if horas_estimadas < 1:
            tiempo_estimado = f"{int(horas_estimadas * 60)} minutos"
        elif horas_estimadas < 24:
            tiempo_estimado = f"{horas_estimadas:.1f} horas"
        else:
            tiempo_estimado = f"{horas_estimadas/24:.1f} días"
    else:
        tiempo_estimado = "Indeterminado"

    return {
        "par":            par,
        "precio":         precio,
        "score":          score,
        "direccion":      direccion,
        "rango_bajo":     rango_bajo,
        "rango_alto":     rango_alto,
        "razones":        razones,
        "atr_pct":        atr_pct,
        "apalancamiento": apalancamiento,
        "apal_razon":     apal_razon,
        "tiempo_estimado": tiempo_estimado,
    }


# ── Generador de alertas ───────────────────────────────────
def generar_alertas():
    ahora = datetime.now().strftime("%H:%M")
    print(f"\n[{ahora}] Analizando mercado...")

    mejores = []

    for par in PARES:
        resultado = analizar_par(par)
        if resultado and resultado["score"] >= MIN_SCORE:
            mejores.append(resultado)
        time.sleep(0.3)   # Respeta límites de Binance

    # Ordenar por score
    mejores.sort(key=lambda x: x["score"], reverse=True)

    if not mejores:
        print(f"[{ahora}] Sin señales fuertes ahora.")
        return

    for r in mejores[:3]:   # Máximo 3 alertas por ciclo
        clave = f"{r['par']}_{datetime.now().strftime('%Y%m%d_%H')}"
        if clave in alertas_enviadas:
            continue
        alertas_enviadas[clave] = True

        estrellas = "⭐" * r["score"]
        msg = (
            f"🚨 <b>SEÑAL DE ENTRADA DETECTADA</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 Par: <b>{r['par']}</b>\n"
            f"💰 Precio actual: <b>{r['precio']:.4f} USDT</b>\n"
            f"🎯 Dirección: <b>{r['direccion']}</b>\n"
            f"📊 Confianza: {estrellas} ({r['score']}/5)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Configuración Grid sugerida:</b>\n"
            f"   Precio bajo:  {r['rango_bajo']} USDT\n"
            f"   Precio alto:  {r['rango_alto']} USDT\n"
            f"   Apalancamiento: <b>{r['apalancamiento']}x</b>\n"
            f"   ({r['apal_razon']})\n"
            f"   Take profit grilla: 1%\n"
            f"⏱ Tiempo estimado al 1%: <b>{r['tiempo_estimado']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Señales detectadas:</b>\n"
            + "\n".join(f"   {s}" for s in r["razones"]) +
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 Sin margen dinámico → usá apalancamiento sugerido\n"
            f"🕐 {ahora}"
        )
        enviar_telegram(msg)
        print(f"  ✅ Alerta enviada: {r['par']} (score {r['score']}/5)")


# ── Scheduler ──────────────────────────────────────────────
def main():
    print("🤖 Bot de señales iniciado")
    enviar_telegram(
        "🤖 <b>Bot de Señales Crypto iniciado</b>\n"
        "Monitoreando mercado cada 30 minutos.\n"
        "Te avisaré cuando detecte buenas entradas para el Grid de Futuros. 📊"
    )

    # Analiza cada 30 minutos
    schedule.every(30).minutes.do(generar_alertas)

    # Primera ejecución inmediata
    generar_alertas()

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
