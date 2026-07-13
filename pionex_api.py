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


def obtener_precision_par(par: str) -> int:
    """
    Consulta GET /common/symbols para saber cuántos decimales acepta
    Pionex en el precio (quotePrecision) para este par específico.
    Cada par tiene su propia precisión — usar un valor fijo para todos
    causa el error 'top not match quote precision'.
    Si falla la consulta, devuelve 4 como default razonable.
    """
    base = par.upper().replace("USDT", "").replace(".PERP", "")
    symbol = f"{base}_USDT_PERP"
    path = "/api/v1/common/symbols"
    query = f"symbols={symbol}&type=PERP"
    timestamp, firma = _firmar("GET", path, query)
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma}
    url = f"{PIONEX_BASE_URL}{path}?{query}&timestamp={timestamp}"
    try:
        resp = requests.get(url, headers=headers, timeout=10).json()
        symbolsList = resp.get("data", {}).get("symbols", [])
        if symbolsList:
            return int(symbolsList[0].get("quotePrecision", 4))
    except Exception:
        pass
    return 4


def _armar_body(par: str, top: float, bottom: float, row: int,
                 capital_usdt: float, leverage: int, trend: str,
                 grid_type: str, extra_margin_usdt: float = 0) -> dict:
    base = par.upper().replace("USDT", "").replace(".PERP", "")
    precision = obtener_precision_par(base)

    bu_order_data = {
        "top": str(round(top, precision)),
        "bottom": str(round(bottom, precision)),
        "row": row,
        "grid_type": grid_type,
        "trend": trend,
        "leverage": leverage,
        "quoteInvestment": str(capital_usdt),
        "investmentFrom": "USER",
        "profitStopType": "profit_ratio",
        "profitStop": str(TAKE_PROFIT_PCT),
    }
    if extra_margin_usdt and extra_margin_usdt > 0:
        # Margen de origen (dinámico): reservado desde la apertura, baja el
        # precio de liquidación en LARGO / lo sube en CORTO. Sin esto, el
        # campo queda vacío y Pionex no reserva nada (lo confirmamos con
        # pruebas reales: estimateExtraMargin devolvía 0).
        bu_order_data["extraMargin"] = str(round(extra_margin_usdt, 2))

    return {
        "base": f"{base}.PERP",
        "quote": "USDT",
        "buOrderData": bu_order_data,
    }


def validar_parametros_grilla(par: str, top: float, bottom: float, row: int,
                               capital_usdt: float, leverage: int = 10,  # FIJO: 10x siempre, decisión confirmada por Juanjo
                               trend: str = "long",
                               grid_type: str = "arithmetic",
                               extra_margin_usdt: float = 0) -> dict:
    """
    Llama a /futuresGrid/checkParams — NO crea una orden real.
    Sirve para validar rango, capital mínimo/máximo y estimar liquidación
    ANTES de arriesgar capital real. Usar siempre primero en pruebas.
    """
    path = "/api/v1/bot/orders/futuresGrid/checkParams"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type, extra_margin_usdt)
    # checkParams usa nombres en snake_case dentro de buOrderData según doc
    bod = body_dict["buOrderData"]
    bod_snake = {
        "top": bod["top"],
        "bottom": bod["bottom"],
        "row": bod["row"],
        "grid_type": bod["grid_type"],
        "trend": bod["trend"],
        "leverage": bod["leverage"],
        "quote_investment": bod["quoteInvestment"],
    }
    if "extraMargin" in bod:
        # Confirmado en la doc oficial (schema real de checkParams):
        # son DOS campos separados, no uno solo como pensé al principio.
        bod_snake["extra_margin"] = True
        bod_snake["extra_margin_amount"] = bod["extraMargin"]
    else:
        bod_snake["extra_margin"] = False
    body_dict["buOrderData"] = bod_snake
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


def calcular_zona_riesgo_por_margen(bu_order_id: str, capital_asignado: float,
                                     ratio_margen_origen: float,
                                     ratio_perdida_trigger: float = 1.49) -> dict:
    """
    Calcula la zona de riesgo usando 'marginBalance' (equity real restante
    de la posición) en vez de distancia de precio — más confiable, se
    confirmó con datos reales del usuario (12/07): a mayor pérdida, menor
    marginBalance, tendiendo a 0 en la liquidación.

    Lógica (confirmada por el usuario): con inversión+margen partidos al
    50%, la liquidación ocurre en aprox. -200% de pérdida sobre la
    INVERSIÓN real (el margen actúa de colchón). Se quiere reforzar
    margen cuando la pérdida llega a ~-149% de la inversión (deja ~25%
    de colchón antes de la liquidación real).

    capital_asignado = inversión + margen (lo que ya guarda la DB).
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}

    margin_balance_str = bod.get("marginBalance")
    if margin_balance_str is None:
        return {"zona": "desconocida", "margin_balance": None, "raw": bod}

    margin_balance = float(margin_balance_str)
    inversion_real = capital_asignado * (1 - ratio_margen_origen)
    perdida_objetivo_usd = ratio_perdida_trigger * inversion_real
    umbral_roja = capital_asignado - perdida_objetivo_usd
    umbral_amarilla = umbral_roja + (capital_asignado * 0.15)  # colchón de aviso previo

    if margin_balance <= umbral_roja:
        zona = "roja"
    elif margin_balance <= umbral_amarilla:
        zona = "amarilla"
    else:
        zona = "verde"

    return {
        "zona": zona,
        "margin_balance": margin_balance,
        "umbral_roja": round(umbral_roja, 2),
        "umbral_amarilla": round(umbral_amarilla, 2),
        "pct_restante": round(margin_balance / capital_asignado * 100, 1) if capital_asignado else None,
        "position_open_price": bod.get("positionOpenPrice"),
    }


def calcular_zona_riesgo(bu_order_id: str, precio_actual: float) -> dict:
    """
    Consulta la orden real en Pionex y calcula la zona según distancia a
    liquidación (regla de la sección 6/7 del proyecto):
      verde   > 15%  -> esperar, nada
      amarilla 8-15% -> apartar 5% del capital para esta operación
      roja    < 8%   -> usar el 5% apartado para reforzar margen (NO cerrar)

    Usa 'liquidationPrice' si el bot ya lo calculó en tiempo real; si no está
    disponible todavía, cae a la estimación 'estimateLiquidationPriceUp/Down'.

    IMPORTANTE: estos campos vienen anidados dentro de 'buOrderData', NO al
    nivel superior de 'data' — bug corregido (antes leía del lugar
    equivocado y nunca encontraba el precio de liquidación real).
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}

    liq_price_str = (
        bod.get("liquidationPrice")
        or bod.get("estimateLiquidationPriceDown")
        or bod.get("estimateLiquidationPriceUp")
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
        "risk_status": bod.get("riskStatus"),
        "orden_status": bod.get("status"),  # "prepare"/"running"/etc — para detectar cierre
        "orden_reason": bod.get("reasonBy"),  # motivo del cierre si ya cerró
    }


def esta_cerrada(bu_order_id: str) -> dict:
    """
    Detecta si una grilla YA CERRÓ en Pionex (tocó TP, se canceló, o se
    liquidó) y calcula el resultado REAL — confirmado con un cierre real
    (CRV, 12/07): Ganancia% = (marginBalance - initUsdtInvestment) /
    quoteInvestment * 100. Antes se asumía 1.35% fijo para cualquier
    cierre por TP, lo cual no reflejaba fees/slippage reales; ahora sirve
    para CUALQUIER cierre (ganador, perdedor, o liquidación).

    Devuelve {"cerrada": bool, "motivo": str|None, "resultado_pct": float|None}.
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}
    status_top = (data.get("status") or "").lower()
    status_bod = (bod.get("status") or "").lower()
    reason = bod.get("reasonBy")

    # Confirmado con datos reales: Pionex usa 'canceled' (una L) para una
    # grilla que cerró por TP — no 'finished'/'closed' como se suponía al
    # principio. Se dejan también las otras variantes por las dudas.
    cerrada = status_top in ("finished", "closed", "cancelled", "canceled") or \
              status_bod in ("finished", "closed", "cancelled", "canceled", "stopped")

    resultado_pct = None
    if cerrada:
        try:
            margin_balance = float(bod.get("marginBalance", 0) or 0)
            init_investment = float(bod.get("initUsdtInvestment", 0) or 0)
            quote_investment = float(bod.get("quoteInvestment") or bod.get("initQuoteInvestment") or 0)
            if quote_investment > 0:
                ganancia_usd = margin_balance - init_investment
                resultado_pct = round(ganancia_usd / quote_investment * 100, 4)
        except (ValueError, TypeError):
            resultado_pct = None

    return {"cerrada": cerrada, "motivo": reason, "resultado_pct": resultado_pct}


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
                          grid_type: str = "arithmetic",
                          extra_margin_usdt: float = 0) -> dict:
    """
    Crea una grilla de futuros REAL en Pionex.

    par: ej. "BTC" (se arma automáticamente como "BTC.PERP")
    top / bottom / row: valores RECOMENDADOS por Pionex (no predeterminados)
    capital_usdt: 9% del capital total, ya calculado antes de llamar a esta función
    extra_margin_usdt: margen de origen (colchón reservado desde la apertura,
        no es capital adicional "de la nada" — se descuenta del capital
        disponible total antes de abrir, ver gestion_riesgo.py)
    """
    path = "/api/v1/bot/orders/futuresGrid/create"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type, extra_margin_usdt)
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
