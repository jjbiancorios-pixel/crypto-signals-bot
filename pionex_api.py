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


def _precio_bybit(par):
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={par}", timeout=6)
    data = r.json()
    if data.get("retCode") != 0: raise ValueError()
    return float(data["result"]["list"][0]["lastPrice"])

def _precio_okx(par):
    inst = par.replace("1000SHIB","SHIB").replace("1000PEPE","PEPE").replace("1000BONK","BONK").replace("USDT","-USDT")
    r = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={inst}", timeout=6)
    rows = r.json().get("data", [])
    if not rows: raise ValueError()
    return float(rows[0]["last"])

def _precio_binance(par):
    r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={par}", timeout=6)
    return float(r.json()["price"])

def obtener_precio_mercado(par):
    """
    v16 (28/07): precio de mercado en tiempo real, independiente de main.py
    (evita import circular) — se usa para el chequeo de zona de riesgo por
    DISTANCIA a liquidación, complementario al de marginBalance. Prueba 3
    exchanges en orden hasta conseguir un precio válido.
    """
    for f in (_precio_bybit, _precio_okx, _precio_binance):
        try:
            p = f(par)
            if p and p > 0: return p
        except Exception:
            continue
    return None


def calcular_zona_riesgo_combinada(bu_order_id: str, capital_asignado: float,
                                    ratio_margen_origen: float, par: str) -> dict:
    """
    v16 (28/07) — FIX: calcular_zona_riesgo_por_margen() sola puede fallar
    en detectar riesgo real cuando un grid de compra sigue ampliando la
    posición mientras el precio cae (ej. real: ATOM el 27/07 — marginBalance
    daba zona VERDE con 89.7% del capital, pero el precio estaba a solo
    2.2% del precio de liquidación real, porque la posición había crecido
    de 137 a 266 unidades comprando en la baja — el apalancamiento efectivo
    subió aunque el marginBalance en USDT todavía no había caído tanto).

    Se combinan DOS métodos y se usa el MÁS PESIMISTA de los dos:
    1. marginBalance vs capital (calcular_zona_riesgo_por_margen)
    2. distancia % al precio de liquidación real (calcular_zona_riesgo)

    Si un método falla o da "desconocida", se usa el otro. Si ambos fallan,
    devuelve "desconocida" (no se actúa a ciegas).
    """
    orden_severidad = {"verde": 0, "amarilla": 1, "roja": 2}

    try:
        r_margen = calcular_zona_riesgo_por_margen(bu_order_id, capital_asignado, ratio_margen_origen)
    except Exception:
        r_margen = {"zona": "desconocida"}

    precio_actual = obtener_precio_mercado(par)
    if precio_actual:
        try:
            r_distancia = calcular_zona_riesgo(bu_order_id, precio_actual)
        except Exception:
            r_distancia = {"zona": "desconocida"}
    else:
        r_distancia = {"zona": "desconocida"}

    zona_margen = r_margen.get("zona", "desconocida")
    zona_distancia = r_distancia.get("zona", "desconocida")

    sev_margen = orden_severidad.get(zona_margen, -1)
    sev_distancia = orden_severidad.get(zona_distancia, -1)

    if sev_margen >= sev_distancia:
        zona_final = zona_margen if sev_margen >= 0 else "desconocida"
        metodo_decisivo = "margen"
    else:
        zona_final = zona_distancia
        metodo_decisivo = "distancia"

    return {
        "zona": zona_final,
        "metodo_decisivo": metodo_decisivo,
        "pct_restante": r_margen.get("pct_restante"),
        "distancia_pct": r_distancia.get("distancia_pct"),
        "position_open_price": r_margen.get("position_open_price"),
        "margin_balance": r_margen.get("margin_balance"),
        "precio_actual": precio_actual,  # v16: reusado por el log de grid dinámico, evita pedirlo 2 veces
    }


def calcular_zona_riesgo_por_margen(bu_order_id: str, capital_asignado: float,
                                     ratio_margen_origen: float,
                                     ratio_perdida_trigger: float = None) -> dict:
    """
    Calcula la zona de riesgo usando 'marginBalance' (equity real restante
    de la posición) en vez de distancia de precio — más confiable, se
    confirmó con datos reales del usuario (12/07): a mayor pérdida, menor
    marginBalance, tendiendo a 0 en la liquidación.

    CORREGIDO 24/07: el umbral de refuerzo (antes fijo en 1.49 = -149%) se
    había calibrado específicamente para el reparto 50/50 (liquidación real
    en -200% de la inversión). Al pasar a 70/30 en el 20-21, la liquidación
    real ocurre antes (-142.9%), y el 1.49 fijo quedó DESACTUALIZADO — el
    aviso hubiera llegado después de la liquidación real, no antes. Bug real
    encontrado 24/07 comparando ATOM y MANA.

    CORREGIDO 28/07 (parte 2 del mismo bug): el fix del 24/07 recalculaba
    el umbral en función del ratio_margen_origen VIGENTE en el sistema al
    momento de chequear — pero eso asume que la operación se abrió con ESE
    mismo reparto. Si el reparto vuelve a cambiar en una futura actualización
    mientras quedan operaciones viejas abiertas con el reparto anterior, el
    mismo bug reaparecería para esas operaciones viejas. Ahora se lee el
    reparto REAL de cada operación puntual directo de Pionex
    (initExtraMargin/initUsdtInvestment), en vez de asumir el vigente del
    sistema — funciona sin importar cuándo se abrió ni cuántas veces haya
    cambiado el reparto desde entonces. El parámetro ratio_margen_origen
    queda solo como fallback si Pionex no devuelve esos campos.

    capital_asignado = inversión + margen (lo que ya guarda la DB).
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}

    if ratio_perdida_trigger is None:
        init_extra_margin = bod.get("initExtraMargin")
        init_usdt_investment = bod.get("initUsdtInvestment")
        try:
            if init_extra_margin is not None and init_usdt_investment and float(init_usdt_investment) > 0:
                ratio_real = float(init_extra_margin) / float(init_usdt_investment)
            else:
                ratio_real = ratio_margen_origen
        except (TypeError, ValueError):
            ratio_real = ratio_margen_origen
        # Mismo colchón relativo de siempre (~25.5% antes de la liquidación
        # real), pero recalculado según el reparto REAL de esta operación.
        liquidacion_pct = 1 / (1 - ratio_real)  # ej. 70/30 -> 1.429 (=-142.9%)
        ratio_perdida_trigger = 0.745 * liquidacion_pct

    margin_balance_str = bod.get("marginBalance")
    if margin_balance_str is None:
        return {"zona": "desconocida", "margin_balance": None, "raw": bod}

    margin_balance = float(margin_balance_str)

    # CORREGIDO 24/07: antes se calculaba inversion_real asumiendo que el
    # RATIO_MARGEN_ORIGEN actual del sistema aplicaba a esta operación —
    # pero si la operación se abrió ANTES de un cambio de ese ratio (ej.
    # 50/50 antes del 20-21, ahora 70/30), el cálculo quedaba mal y el
    # umbral podía terminar siendo negativo (inalcanzable) o mal calibrado.
    # Bug real encontrado 24/07 en una operación de EGLD abierta pre-20-21:
    # el refuerzo nunca se hubiera disparado con el ratio nuevo aplicado
    # retroactivamente. Ahora se lee el dato REAL de esa posición puntual
    # directo de Pionex (quoteInvestment/initQuoteInvestment), sin asumir
    # ningún ratio — funciona sin importar cuándo se abrió la operación.
    inversion_real_real = bod.get("quoteInvestment") or bod.get("initQuoteInvestment")
    if inversion_real_real is not None:
        inversion_real = float(inversion_real_real)
    else:
        # Fallback solo si Pionex no devuelve el dato por algún motivo
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


def cerrar_grilla_futuros(bu_order_id: str, nota: str = "Cierre automático") -> dict:
    """
    POST /futuresGrid/cancel — cierra y cancela una grilla YA ABIERTA.
    Usar para el cierre automático a las 10hs (o cualquier cierre forzado
    por lógica propia, no por TP). closeSellModel=TO_QUOTE es el default
    de Pionex (cierra la posición, no vende a USDT automáticamente).
    """
    path = "/api/v1/bot/orders/futuresGrid/cancel"
    body_dict = {
        "buOrderId": bu_order_id,
        "closeNote": nota,
        "closeSellModel": "TO_QUOTE",
        "immediate": True,
        "closeSlippage": "0.01",
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


def calcular_resultado_actual(bu_order_id: str, capital_total_real: float = None):
    """
    Calcula el % de resultado REAL en este momento para una operación
    todavía ABIERTA (misma fórmula confirmada con datos reales: 12/07):
    Ganancia% = (marginBalance - init_investment) / quoteInvestment * 100.
    Ahora solo INFORMATIVO — ya no dispara ningún cierre automático (ver
    gestion_riesgo.py, decisión 28/07: solo se cierra al 1.35% real de TP).

    FIX CRÍTICO 28/07: initUsdtInvestment es un campo que Pionex graba UNA
    VEZ al crear la operación y NUNCA actualiza si agregás capital manual
    después (caso real: MOVE, 27/07 — se agregó capital, initUsdtInvestment
    quedó congelado en 60, y la fórmula contó TODO el capital agregado como
    si fuera ganancia: dio +45.18% cuando el resultado real rondaba -3%).
    Ahora, si se pasa capital_total_real (el capital_asignado que tenemos
    en NUESTRA base, que sí se actualiza con /corregir), se usa ESE en vez
    del initUsdtInvestment de Pionex — evita el mismo bug en cualquier
    operación editada manualmente.
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}
    try:
        margin_balance = float(bod.get("marginBalance", 0) or 0)
        if capital_total_real is not None:
            init_investment = float(capital_total_real)
        else:
            init_investment = float(bod.get("initUsdtInvestment", 0) or 0)
        quote_investment = float(bod.get("quoteInvestment") or bod.get("initQuoteInvestment") or 0)
        if quote_investment <= 0:
            return None
        return round((margin_balance - init_investment) / quote_investment * 100, 4)
    except (ValueError, TypeError):
        return None


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
