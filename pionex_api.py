"""
pionex_api.py
Cliente para la API de Pionex — Futures Grid Bot.

Basado en la documentación oficial:
https://www.pionex.com/docs/api-docs/bot-api/futures-grid
https://pionex-doc.gitbook.io/apidocs/restful/general/basic

IMPORTANTE — antes de usar en producción:
1. Generar API Key en Pionex con permiso de TRADE únicamente (sin retiro).
2. Configurar whitelist de IP con la IP saliente de Railway.
3. Cargar PIONEX_API_KEY y PIONEX_API_SECRET como Variables en Railway.
4. Probar SIEMPRE primero contra /futuresGrid/checkParams (no crea orden real,
   solo valida y estima) antes de llamar a /futuresGrid/create.
"""

import os
import time
import hmac
import hashlib
import json
import requests

PIONEX_BASE_URL = "https://api.pionex.com"
PIONEX_API_KEY = os.environ.get("PIONEX_API_KEY", "")
PIONEX_API_SECRET = os.environ.get("PIONEX_API_SECRET", "")

TAKE_PROFIT_PCT = 0.0135  # 1.35% fijo, según estrategia confirmada


def _firmar(method: str, path: str, query: str, body: str = "") -> tuple:
    """
    Genera timestamp (ms) y firma HMAC-SHA256 según especificación de Pionex.
    GET           -> METHOD + PATH_URL + QUERY + TIMESTAMP
    POST / DELETE -> METHOD + PATH_URL + QUERY + TIMESTAMP + BODY
    """
    if not PIONEX_API_SECRET:
        raise RuntimeError("PIONEX_API_SECRET no configurada (falta variable en Railway).")

    timestamp = str(int(time.time() * 1000))
    query_completa = f"{query}&timestamp={timestamp}" if query else f"timestamp={timestamp}"
    payload = f"{method}{path}?{query_completa}"
    if method in ("POST", "DELETE"):
        payload += body

    firma = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return timestamp, firma


def _armar_body(par: str, top: float, bottom: float, row: int,
                 capital_usdt: float, leverage: int, trend: str,
                 grid_type: str) -> dict:
    base = par.upper()
    if not base.endswith(".PERP"):
        base = f"{base}.PERP"

    return {
        "base": base,
        "quote": "USDT",
        "buOrderData": {
            "top": str(top),
            "bottom": str(bottom),
            "row": row,
            "grid_type": grid_type,
            "trend": trend,
            "leverage": leverage,
            "quoteInvestment": str(capital_usdt),
            "investmentFrom": "USER",
            "profitStopType": "profit_ratio",
            "profitStop": str(TAKE_PROFIT_PCT),
        },
    }


def validar_parametros_grilla(par: str, top: float, bottom: float, row: int,
                               capital_usdt: float, leverage: int = 10,  # FIJO: 10x siempre, decisión confirmada por Juanjo
                               trend: str = "long",
                               grid_type: str = "arithmetic") -> dict:
    """
    Llama a /futuresGrid/checkParams — NO crea una orden real.
    Sirve para validar rango, capital mínimo/máximo y estimar liquidación
    ANTES de arriesgar capital real. Usar siempre primero en pruebas.
    """
    path = "/api/v1/bot/orders/futuresGrid/checkParams"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type)
    # checkParams usa nombres en snake_case dentro de buOrderData según doc
    bod = body_dict["buOrderData"]
    body_dict["buOrderData"] = {
        "top": bod["top"],
        "bottom": bod["bottom"],
        "row": bod["row"],
        "grid_type": bod["grid_type"],
        "trend": bod["trend"],
        "leverage": bod["leverage"],
        "quote_investment": bod["quoteInvestment"],
    }
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": firma,
        "Content-Type": "application/json",
    }
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    resp = requests.post(url, headers=headers, data=body_json, timeout=15)
    return resp.json()


def consultar_orden(bu_order_id: str) -> dict:
    """
    GET /futuresGrid/order — trae el estado completo del bot de grilla:
    liquidationPrice (real), riskStatus, marginStatus, marginBalance, position, etc.
    """
    path = "/api/v1/bot/orders/futuresGrid/order"
    query = f"buOrderId={bu_order_id}"
    timestamp, firma = _firmar("GET", path, query)

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": firma,
    }
    url = f"{PIONEX_BASE_URL}{path}?{query}&timestamp={timestamp}"
    resp = requests.get(url, headers=headers, timeout=15)
    return resp.json()


def calcular_zona_riesgo(bu_order_id: str, precio_actual: float) -> dict:
    """
    Consulta la orden real en Pionex y calcula la zona según distancia a
    liquidación (regla de la sección 6/7 del proyecto):
      verde   > 15%  -> esperar, nada
      amarilla 8-15% -> apartar 5% del capital para esta operación
      roja    < 8%   -> usar el 5% apartado para reforzar margen (NO cerrar)

    Usa 'liquidationPrice' si el bot ya lo calculó en tiempo real; si no está
    disponible todavía, cae a la estimación 'estimateLiquidationPriceUp/Down'.
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}

    liq_price_str = (
        data.get("liquidationPrice")
        or data.get("estimateLiquidationPriceDown")
        or data.get("estimateLiquidationPriceUp")
    )
    if not liq_price_str or float(liq_price_str) == 0:
        return {"zona": "desconocida", "distancia_pct": None, "raw": data}

    liq_price = float(liq_price_str)
    distancia_pct = abs(precio_actual - liq_price) / precio_actual * 100

    if distancia_pct > 15:
        zona = "verde"
    elif distancia_pct >= 8:
        zona = "amarilla"
    else:
        zona = "roja"

    return {
        "zona": zona,
        "distancia_pct": round(distancia_pct, 2),
        "liquidation_price": liq_price,
        "risk_status": data.get("riskStatus"),
        "margin_status": data.get("marginStatus"),
    }


def reforzar_margen(bu_order_id: str, monto_extra_usdt: float, precio_actual: float) -> dict:
    """
    POST /futuresGrid/adjustParams (type=invest_in) — agrega margen extra a
    una grilla YA ABIERTA sin cerrarla, para alejar el precio de liquidación.

    Usar cuando calcular_zona_riesgo() devuelve zona == 'roja', con el 5%
    de capital que ya se había apartado cuando la operación entró en zona
    amarilla. Nunca cerrar la operación en pérdida por esto — solo reforzar.
    """
    path = "/api/v1/bot/orders/futuresGrid/adjustParams"
    body_dict = {
        "buOrderId": bu_order_id,
        "type": "invest_in",
        "quoteInvestment": monto_extra_usdt,
        "extraMargin": True,
        "openPrice": precio_actual,
    }
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": firma,
        "Content-Type": "application/json",
    }
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    resp = requests.post(url, headers=headers, data=body_json, timeout=15)
    return resp.json()


def crear_grilla_futuros(par: str, top: float, bottom: float, row: int,
                          capital_usdt: float, leverage: int = 10,  # FIJO: 10x siempre, decisión confirmada por Juanjo
                          trend: str = "long",
                          grid_type: str = "arithmetic") -> dict:
    """
    Crea una grilla de futuros REAL en Pionex.

    par: ej. "BTC" (se arma automáticamente como "BTC.PERP")
    top / bottom / row: valores RECOMENDADOS por Pionex (no predeterminados)
    capital_usdt: 9% del capital total, ya calculado antes de llamar a esta función
    """
    path = "/api/v1/bot/orders/futuresGrid/create"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type)
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": firma,
        "Content-Type": "application/json",
    }
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    resp = requests.post(url, headers=headers, data=body_json, timeout=15)
    return resp.json()
