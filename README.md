# 🤖 Bot de Señales Crypto para Pionex Grid de Futuros

## ¿Qué hace este bot?
- Analiza 10 pares de criptomonedas cada 30 minutos
- Detecta condiciones ideales para el Grid de Futuros de Pionex
- Te avisa por Telegram con el par, dirección y rango sugerido

## Indicadores que usa
- RSI (sobrecompra/sobreventa)
- ATR (volatilidad)
- Bandas de Bollinger (amplitud del movimiento)
- MACD (momentum)
- Volumen relativo

## Cómo subir a Railway (paso a paso)

1. Creá cuenta gratis en https://railway.app (con tu cuenta de GitHub o Google)
2. Hacé clic en "New Project" → "Deploy from GitHub repo"
3. Subí estos 3 archivos a un repositorio de GitHub:
   - main.py
   - requirements.txt
   - railway.toml
4. En Railway, andá a "Variables" y agregá:
   - TELEGRAM_TOKEN = tu_token_aqui
   - CHAT_ID = tu_chat_id_aqui
5. Railway despliega automáticamente y el bot empieza a correr

## Variables de entorno necesarias
- TELEGRAM_TOKEN: El token que te dio BotFather
- CHAT_ID: Tu ID de Telegram

## Pares monitoreados
BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX, LINK, DOT (todos contra USDT)
