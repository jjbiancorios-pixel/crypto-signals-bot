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
# 20/08 — comisión de cierre (taker, futuros: 0.05% confirmado en varias
# fuentes públicas — Pionex no publica una API de fees, no hay endpoint
# propio para confirmarlo). Caso real: XLMUSDT — nuestro cálculo daba
# -2.14%, el resultado real en Pionex fue -2.39% (0.25 puntos de
# diferencia) — no_realizado_usd nunca restaba la comisión del cierre,
# que con 10x de apalancamiento se amplifica al expresarla como % del
# margen. Se resta como estimación — no confirmado al centavo, revisar
# si sigue habiendo diferencia sistemática con más casos reales.
COMISION_CIERRE_TAKER_PCT = 0.0005
PIONEX_API_KEY = os.environ.get("PIONEX_API_KEY", "")
PIONEX_API_SECRET = os.environ.get("PIONEX_API_SECRET", "")

# 19/08 — Sesión HTTP persistente (keep-alive) hacia Pionex. Antes cada
# llamada usaba requests.get/post directo, que abre una conexión TLS
# nueva por request — según la recomendación de trailing agregada al
# conocimiento del proyecto, esto puede costar 100-300ms extra en el
# momento más crítico (el cierre real del trailing). Con una sesión
# persistente, la conexión queda "caliente" entre llamadas.
_session = requests.Session()

TAKE_PROFIT_PCT = 0.0135  # 1.35% — referencia INTERNA nuestra (primer checkpoint del piso ascendente de v17), YA NO se manda directo a Pionex
# v17 — El TP real que le mandamos a Pionex ahora es un techo alto (nunca
# debería alcanzarse) — el cierre real por ganancia lo maneja el piso
# ascendente vía lossStop (ver modificar_stop_loss), con el motor rápido
# de Pionex protegiendo cada nivel confirmado. El SL real arranca en
# -15% (el techo absoluto incondicional de v17) desde la apertura misma
# — antes no había ningún SL real hasta que nuestro bot lo cerraba a
# mano; ahora Pionex mismo protege ese piso desde el minuto uno.
# 19/08 (ESQUEMA SIMPLE, TEMPORAL hasta resolver el trailing) — Juanjo
# pidió volver a TP/SL fijos nativos de Pionex (rápidos, confiables, sin
# nuestra latencia) en vez del techo alto + trailing propio: TP fijo
# 1.35%, SL inicial fijo -3%, y SI la ganancia llega a 0% o más en algún
# momento, nuestro sistema vigila y cierra si retrocede a -1.5% (Pionex
# no permite modificar el SL nativo de una posición corriendo — mismo
# límite que ya confirmamos con el diseño anterior). Constantes viejas
# del techo alto quedan sin uso pero no se borran, por si se retoma el
# trailing más adelante.
PROFIT_STOP_CEILING_PIONEX = 0.50  # sin uso mientras dure el esquema simple
STOP_LOSS_INICIAL_PIONEX = -0.15  # sin uso mientras dure el esquema simple
TP_FIJO_SIMPLE = 0.35  # v18 (21/08, Juanjo): 15%->35% — techo alto nativo, el cierre real por ganancia lo maneja nuestro trailing (ver UMBRAL_TRAILING_SIMPLE/RETROCESO_PROPORCIONAL_TRAILING)
SL_INICIAL_SIMPLE = -0.10  # v18 (21/08, Juanjo): -8%->-10% — mandado directo a Pionex como lossStop al crear. Con 5% de capital por operación, -10% representa apenas 0.5% del capital total
SL_AJUSTADO_SIMPLE = -2.5  # SIN USO desde el rediseño a breakeven-stop (20/08) — gestion_riesgo ahora cierra a resultado_actual<=0 directo, no a este valor. Se deja sin borrar por si se retoma.
UMBRAL_TRAILING_SIMPLE = 4.0  # v18 (21/08, Juanjo): 1.35%->4% — recién a partir de acá se considera que hay una tendencia real, no ruido de apalancamiento (ver caso ACE: 0.22% de movimiento real, 2+ puntos de resultado por el 10x)
RETROCESO_PROPORCIONAL_TRAILING = 0.40  # v18 (21/08, Juanjo) — REEMPLAZA RETROCESO_ABSOLUTO_TRAILING: ya no es un punto fijo, es un % PROPORCIONAL al pico alcanzado (ej. pico +8% -> cierra si cae a +4.8%, un retroceso del 40% del pico. Antes: punto fijo de 0.5pts, diseñado para picos chicos de 1.35%, ya no aplica con el umbral en 4%)


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


def obtener_precision_par(par: str, quote: str = "USDT") -> int:
    """
    Consulta GET /common/symbols para saber cuántos decimales acepta
    Pionex en el precio (quotePrecision) para este par específico.
    Cada par tiene su propia precisión — usar un valor fijo para todos
    causa el error 'top not match quote precision'.
    Si falla la consulta, devuelve 4 como default razonable.

    23/08 (Juanjo, PAXG) — agregado el parámetro quote: antes armaba el
    símbolo siempre como {PAR}_USDT_PERP, sin importar la moneda real de
    cotización. Para PAXG (cotiza contra BTC, no USDT), esto consultaba
    un símbolo que no existe (PAXG_USDT_PERP), la consulta devolvía
    vacío, y caía al default de 4 decimales — insuficiente para un
    precio en el rango de 0.05-0.06, causando un rango mal redondeado.
    """
    base = par.upper().replace("USDT", "").replace(".PERP", "")
    symbol = f"{base}_{quote}_PERP"
    path = "/api/v1/common/symbols"
    query = f"symbols={symbol}&type=PERP"
    timestamp, firma = _firmar("GET", path, query)
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma}
    url = f"{PIONEX_BASE_URL}{path}?{query}&timestamp={timestamp}"
    try:
        resp = _session.get(url, headers=headers, timeout=10).json()
        symbolsList = resp.get("data", {}).get("symbols", [])
        if symbolsList:
            return int(symbolsList[0].get("quotePrecision", 4))
    except Exception:
        pass
    return 4


def _armar_body(par: str, top: float, bottom: float, row: int,
                 capital_usdt: float, leverage: int, trend: str,
                 grid_type: str, extra_margin_usdt: float = 0, sl_pct: float = None,
                 quote: str = "USDT") -> dict:
    base = par.upper().replace("USDT", "").replace(".PERP", "")
    precision = obtener_precision_par(base, quote=quote)

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
        "profitStop": str(TP_FIJO_SIMPLE),
        "lossStopType": "profit_ratio",
        # 26/08 — REVERTIDO: se había sacado este campo como prueba para
        # descartar si causaba el BOT_INTERNAL_ERROR recurrente de
        # Pionex (INJ/NEAR/ZRX/ENS). Juanjo confirmó que sacarlo NO
        # resolvió el problema — se restaura tal como estaba. Próxima
        # sospechosa a probar: la sesión HTTP persistente (_session vs.
        # requests directo, ver más abajo).
        "lossStop": str(sl_pct if sl_pct is not None else SL_INICIAL_SIMPLE),
    }
    if extra_margin_usdt and extra_margin_usdt > 0:
        # Margen de origen (dinámico): reservado desde la apertura, baja el
        # precio de liquidación en LARGO / lo sube en CORTO. Sin esto, el
        # campo queda vacío y Pionex no reserva nada (lo confirmamos con
        # pruebas reales: estimateExtraMargin devolvía 0).
        bu_order_data["extraMargin"] = str(round(extra_margin_usdt, 2))

    return {
        "base": f"{base}.PERP",
        "quote": quote,
        "buOrderData": bu_order_data,
    }


def validar_parametros_grilla(par: str, top: float, bottom: float, row: int,
                               capital_usdt: float, leverage: int = 10,  # FIJO: 10x siempre, decisión confirmada por Juanjo
                               trend: str = "long",
                               grid_type: str = "arithmetic",
                               extra_margin_usdt: float = 0, quote: str = "USDT") -> dict:
    """
    Llama a /futuresGrid/checkParams — NO crea una orden real.
    Sirve para validar rango, capital mínimo/máximo y estimar liquidación
    ANTES de arriesgar capital real. Usar siempre primero en pruebas.
    23/08 — quote="BTC" para PAXG (cotiza contra BTC, no USDT).
    """
    path = "/api/v1/bot/orders/futuresGrid/checkParams"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type, extra_margin_usdt, quote=quote)
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
    resp = _session.post(url, headers=headers, data=body_json, timeout=15)
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
    resp = _session.get(url, headers=headers, timeout=15)
    return resp.json()


def obtener_balance_cuenta() -> float:
    """
    01/08 — Consulta el balance REAL de USDT en la cuenta de trading, vía
    GET /api/v1/account/balances. Se usa para el recálculo diario de
    capital (interés compuesto).

    OJO: este endpoint EXCLUYE las cuentas de bot y de earn — solo tiene
    sentido llamarlo cuando NO hay ninguna operación de grid abierta (que
    es justo cuando se ejecuta el recálculo diario: se pospone mientras
    haya posiciones activas). Con posiciones abiertas, este número NO
    reflejaría el capital real total.

    Devuelve None si falla la consulta (no se debe usar un None como si
    fuera $0 — el llamador debe abortar el recálculo ese día y reintentar
    en el próximo ciclo, no vaciar el capital por un error de red).
    """
    path = "/api/v1/account/balances"
    timestamp, firma = _firmar("GET", path, "")
    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": firma,
    }
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    try:
        resp = _session.get(url, headers=headers, timeout=15)
        data = resp.json()
        if not data.get("result"):
            return None
        for b in data.get("data", {}).get("balances", []):
            if b.get("coin") == "USDT":
                return round(float(b.get("free", 0)) + float(b.get("frozen", 0)), 2)
        return 0.0
    except Exception:
        return None


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

def obtener_precio_pionex_directo(par: str):
    """
    19/08 — Precio directo de PIONEX (endpoint público GET
    /api/v1/market/tickers), NO de la cascada externa Bybit/OKX/Binance.
    Se usa para la verificación de seguridad de rango al abrir una
    grilla real: caso real confirmado — la cascada externa dio ~118 para
    XMR (vs. el precio real ~414) de forma CONSISTENTE en ambas consultas
    (la de calcular_grid Y la de la verificación de seguridad, que usaba
    obtener_precio_mercado — misma cascada, mismo dato malo, ninguna
    discrepancia detectada). Pionex mismo, siendo su propio motor de
    trading, siempre tiene el precio real correcto — endpoint público,
    sin necesidad de firma.
    """
    base = par.replace("USDT", "")
    symbol = f"{base}_USDT_PERP"
    url = f"{PIONEX_BASE_URL}/api/v1/market/tickers?symbol={symbol}"
    try:
        resp = _session.get(url, timeout=10)
        data = resp.json()
        if not data.get("result"):
            return None
        tickers = data.get("data", {}).get("tickers", [])
        if not tickers:
            return None
        return float(tickers[0]["close"])
    except Exception:
        return None


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


def obtener_precio_oro():
    """
    04/08 — Precio spot de oro (XAU/USD) vía xaus.com, sin API key. Se usa
    para el análisis del cinturón PAXG/BTC (señal C: macro-inversa con
    BTC + análisis propio de oro). Devuelve USD por onza troy, o None si
    falla la consulta (no asumir $0 — el llamador debe saltear ese ciclo).
    """
    try:
        r = requests.get("https://xaus.com/api/v1/spot?compact=1", timeout=10)
        data = r.json()
        precio = data.get("spot_usd_oz")
        return float(precio) if precio else None
    except Exception:
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

    # 20/08 — usa el precio DIRECTO de Pionex (antes usaba la cascada
    # externa) — por consistencia con la recomendación del documento de
    # trailing: cualquier decisión basada en precio debería usar la
    # fuente más directa posible, no solo el camino del trailing.
    precio_actual = obtener_precio_pionex_directo(par)
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

    19/08 (FIX CRÍTICO, doble):
    1) Se sacó "immediate": True — según la documentación oficial, ese
       flag es una recuperación especial SOLO válida cuando la orden ya
       está en estado close_position con un límite TP/SL trabado sin
       llenar. En cualquier posición corriendo normalmente (nuestro caso
       siempre: checkpoints, piso ascendente, liberación, red de
       seguridad), Pionex lo RECHAZA con "Forbidden: invalid status".
    2) Como antes no se chequeaba result, ese rechazo quedaba invisible
       — nuestro sistema marcaba la señal como cerrada en NUESTRA base
       (dejando de monitorearla) mientras la posición real seguía viva
       en Pionex sin nadie vigilándola. Caso real sospechoso: JTOUSDT
       llegó a +1.9% real, el piso debía cerrarla, pero terminó cayendo
       a +0.39% antes de que Juanjo la cerrara a mano — coincide
       exactamente con este patrón.
    """
    path = "/api/v1/bot/orders/futuresGrid/cancel"
    body_dict = {
        "buOrderId": bu_order_id,
        "closeNote": nota,
        "closeSellModel": "TO_QUOTE",
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
    resp = _session.post(url, headers=headers, data=body_json, timeout=15)
    data = resp.json()
    if not data.get("result"):
        raise RuntimeError(f"Pionex rechazó el cierre: {data}")
    return data


def calcular_resultado_actual(bu_order_id: str, par: str = None, capital_total_real: float = None):
    """
    Calcula el % de resultado REAL en este momento para una operación
    todavía ABIERTA. Ahora solo INFORMATIVO — ya no dispara ningún cierre
    automático (ver gestion_riesgo.py: el stop-loss usa
    calcular_resultado_desglosado, esta función queda para el aviso de 10hs).

    07/08 — FIX CRÍTICO (mismo que calcular_resultado_desglosado): a la
    fórmula le faltaba sumar la ganancia/pérdida NO REALIZADA de la
    posición ya comprada por el grid — ver docstring de
    calcular_resultado_desglosado para el caso real (INJUSDT, -8.79% vs.
    -22.05% real). Ahora recibe también `par` para poder consultar el
    precio de mercado real.
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

        no_realizado_usd = 0.0
        base_amount = float(bod.get("baseAmount", 0) or 0)
        position_open_price = float(bod.get("positionOpenPrice", 0) or 0)
        if abs(base_amount) > 0 and position_open_price > 0 and par:
            # 19/08 — usa el precio DIRECTO de Pionex (no la cascada
            # externa Bybit/OKX/Binance) — según las recomendaciones de
            # trailing agregadas al proyecto, este es el camino crítico
            # de latencia para decidir el cierre, y depender de un
            # exchange externo suma un salto de red innecesario.
            precio_actual = obtener_precio_pionex_directo(par)
            if precio_actual is not None:
                es_corto = bod.get("trend") == "short"
                if es_corto:
                    no_realizado_usd = abs(base_amount) * (position_open_price - precio_actual) - abs(base_amount) * precio_actual * COMISION_CIERRE_TAKER_PCT
                else:
                    no_realizado_usd = abs(base_amount) * (precio_actual - position_open_price) - abs(base_amount) * precio_actual * COMISION_CIERRE_TAKER_PCT

        return round(((margin_balance - init_investment) + no_realizado_usd) / quote_investment * 100, 4)
    except (ValueError, TypeError):
        return None


def calcular_resultado_desglosado(bu_order_id: str, par: str = None, capital_total_real: float = None):
    """
    Igual que calcular_resultado_actual, pero además separa cuánto del
    resultado viene de la GRILLA (oscilación, gridProfit de Pionex) vs.
    de la TENDENCIA (movimiento direccional del precio).
    Devuelve {"total_pct", "rejilla_pct", "tendencia_pct"} o None si falla.

    07/08 — FIX CRÍTICO: la fórmula anterior (marginBalance - inversión
    inicial) le faltaba sumar la ganancia/pérdida NO REALIZADA de la
    posición que el grid ya compró/vendió — cuánto vale ahora comparado
    contra el precio promedio de entrada. Caso real INJUSDT: la fórmula
    vieja daba -8.79% cuando el real (confirmado con /debug_orden y la
    app) era -22.05% — el stop-loss no tenía forma de dispararse a
    tiempo, ya había pasado -20% real sin que el bot lo detectara. Ahora
    suma baseAmount * (precio_actual - positionOpenPrice) para LARGO
    (invertido para CORTO) — requiere el precio de mercado real, por eso
    la función ahora recibe también `par`.
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}
    try:
        margin_balance = float(bod.get("marginBalance", 0) or 0)
        init_investment = float(capital_total_real) if capital_total_real is not None else float(bod.get("initUsdtInvestment", 0) or 0)
        quote_investment = float(bod.get("quoteInvestment") or bod.get("initQuoteInvestment") or 0)
        if quote_investment <= 0:
            return None

        no_realizado_usd = 0.0
        base_amount = float(bod.get("baseAmount", 0) or 0)
        position_open_price = float(bod.get("positionOpenPrice", 0) or 0)
        if abs(base_amount) > 0 and position_open_price > 0 and par:
            # 19/08 — usa el precio DIRECTO de Pionex (no la cascada
            # externa Bybit/OKX/Binance) — según las recomendaciones de
            # trailing agregadas al proyecto, este es el camino crítico
            # de latencia para decidir el cierre, y depender de un
            # exchange externo suma un salto de red innecesario.
            precio_actual = obtener_precio_pionex_directo(par)
            if precio_actual is not None:
                es_corto = bod.get("trend") == "short"
                if es_corto:
                    no_realizado_usd = abs(base_amount) * (position_open_price - precio_actual) - abs(base_amount) * precio_actual * COMISION_CIERRE_TAKER_PCT
                else:
                    no_realizado_usd = abs(base_amount) * (precio_actual - position_open_price) - abs(base_amount) * precio_actual * COMISION_CIERRE_TAKER_PCT

        total_pct = round(((margin_balance - init_investment) + no_realizado_usd) / quote_investment * 100, 4)
        grid_profit_usd = float(bod.get("gridProfit", 0) or 0)
        rejilla_pct = round(grid_profit_usd / quote_investment * 100, 4)
        tendencia_pct = round(total_pct - rejilla_pct, 4)
        return {"total_pct": total_pct, "rejilla_pct": rejilla_pct, "tendencia_pct": tendencia_pct}
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


def listar_grillas_abiertas() -> list:
    """
    10/08 — Lista TODAS las grillas de futuros que Pionex tiene actualmente
    corriendo (GET /api/v1/bot/orders?type=futures_grid), sin depender de
    nuestra base. Se usa para: (1) verificación extra antes de confirmar
    un cierre (si el bu_order_id SIGUE en esta lista real, no confiar en
    que cerró aunque esta_cerrada() lo diga dos veces), y (2) chequeo de
    reconciliación periódico (detectar posiciones "huérfanas" — reales en
    Pionex, perdidas de nuestro tracking).

    Caso real que motivó esto (INJUSDT, 10/08): una posición quedó
    corriendo en Pionex sin que nuestra base la rastreara, invisible al
    stop-loss, por varios días. Devuelve lista de dicts con al menos
    'buOrderId' y 'symbol', o None si falla la consulta.

    16/08 (FIX): el filtro `type=futures_grid` en la query causaba
    INVALID_SIGNATURE real en producción (motivo exacto no confirmado —
    la doc dice "puede pasar múltiples valores", puede que Pionex espere
    un formato de array distinto al que mandábamos). Se saca el filtro de
    la query (el ejemplo oficial de Pionex solo lleva timestamp) y se
    filtra futures_grid del lado de Python, sobre la respuesta completa.
    """
    path = "/api/v1/bot/orders"
    timestamp, firma = _firmar("GET", path, "")
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma}
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    try:
        resp = _session.get(url, headers=headers, timeout=15)
        data = resp.json()
        if not data.get("result"):
            print(f"⚠️ listar_grillas_abiertas: Pionex respondió result=false: {str(data)[:200]}")
            return None
        # 16/08: corregido según doc oficial — el campo real es
        # "results" (no "orders" como se asumía antes sin confirmar), y
        # cada orden trae "buOrderType" para filtrar futures_grid.
        ordenes = data.get("data", {}).get("results", [])
        abiertas = [
            o for o in ordenes
            if o.get("buOrderType") == "futures_grid"
            and str(o.get("status", "")).lower() in ("running", "trading")
        ]
        return abiertas
    except Exception as e:
        print(f"⚠️ listar_grillas_abiertas: error de conexión/parseo — {e}")
        return None


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
    resp = _session.post(url, headers=headers, data=body_json, timeout=15)
    return resp.json()


def modificar_stop_loss(bu_order_id: str, nuevo_lossstop_pct: float) -> dict:
    """
    v17 — Sube (o baja) el SL REAL de una grilla YA ABIERTA en Pionex, en
    modo PnL% (confirmado con captura real de la app: acepta un piso en
    zona de GANANCIA, no solo pérdida tradicional). Es el mecanismo
    central del piso ascendente de ganancia — Pionex protege en tiempo
    real lo que ya confirmamos, con su motor rápido, no el nuestro.

    nuevo_lossstop_pct: negativo para pérdida (ej. -15.0), positivo para
    un piso en zona de ganancia (ej. 1.35).

    ⚠️ Confirmado por la app que existe un modo "PnL%" (captura real,
    15/08) — pero el nombre EXACTO del valor interno de lossStopType para
    ese modo no está confirmado contra la API (asumido "profit_ratio",
    mismo patrón que profitStopType). Si falla, revisar esto primero.
    """
    path = "/api/v1/bot/orders/futuresGrid/adjustParams"
    body_dict = {
        "buOrderId": bu_order_id,
        "type": "adjust_params",
        "lossStopType": "profit_ratio",
        "lossStop": str(round(nuevo_lossstop_pct / 100, 6)),
    }
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": firma,
        "Content-Type": "application/json",
    }
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    resp = _session.post(url, headers=headers, data=body_json, timeout=15)
    data = resp.json()
    # 19/08 (FIX CRÍTICO) — antes esto devolvía data tal cual, sin
    # chequear result. Si Pionex RECHAZABA el pedido (ej. por el nombre
    # de campo lossStopType sin confirmar, o cualquier otro motivo), el
    # código que llama a esta función seguía de largo como si hubiera
    # funcionado — guardaba "piso subido" en la base y mandaba el
    # mensaje de éxito, mientras el SL real en Pionex JAMÁS cambiaba.
    # Casos reales confirmados por Juanjo viendo la app: ACEUSDT y
    # RUNEUSDT — el mensaje decía "piso subido", pero en Pionex seguía
    # el -15% original. RUNEUSDT terminó cerrando en -10.64% real por
    # esto — la ganancia de +5.32% nunca estuvo realmente protegida.
    if not data.get("result"):
        raise RuntimeError(f"Pionex rechazó el cambio de SL: {data}")
    return data


def crear_grilla_futuros(par: str, top: float, bottom: float, row: int,
                          capital_usdt: float, leverage: int = 10,  # FIJO: 10x siempre, decisión confirmada por Juanjo
                          trend: str = "long",
                          grid_type: str = "arithmetic",
                          extra_margin_usdt: float = 0, sl_pct: float = None, quote: str = "USDT") -> dict:
    """
    Crea una grilla de futuros REAL en Pionex.

    par: ej. "BTC" (se arma automáticamente como "BTC.PERP")
    top / bottom / row: valores RECOMENDADOS por Pionex (no predeterminados)
    capital_usdt: 9% del capital total, ya calculado antes de llamar a esta función
    extra_margin_usdt: margen de origen (colchón reservado desde la apertura,
        no es capital adicional "de la nada" — se descuenta del capital
        disponible total antes de abrir, ver gestion_riesgo.py)
    sl_pct: 23/08 (Juanjo) — SL calculado por ATR de la moneda (ver
        calcular_sl_atr), None cae al fijo SL_INICIAL_SIMPLE.
    quote: 23/08 (Juanjo, PAXG) — "BTC" para PAXG (cotiza contra BTC, no
        USDT) — antes estaba hardcodeado a "USDT" siempre, causando el
        error real "top must greater than bottom" al validar PAXG.
    """
    path = "/api/v1/bot/orders/futuresGrid/create"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type, extra_margin_usdt, sl_pct, quote=quote)
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)

    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": firma,
        "Content-Type": "application/json",
    }
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    # 26/08 (Juanjo, PRUEBA #2) — cambiado de _session.post (sesión HTTP
    # persistente, agregada 19-20/08) a requests.post directo (una
    # conexión nueva cada vez, como en la versión previa a v16) — SOLO
    # acá, en la creación real. Prueba #1 (sacar lossStop) NO resolvió
    # el BOT_INTERNAL_ERROR recurrente (INJ/NEAR/ZRX/ENS), se revirtió.
    # Esta es la sospechosa siguiente: aunque se agregó ANTES de que el
    # error se volviera frecuente, sigue siendo una diferencia real
    # frente a la versión que funcionaba bien. Si esto tampoco resuelve,
    # revertir de nuevo (Juanjo: "no manosear tanto el sistema") y
    # replantear el diagnóstico de fondo.
    resp = requests.post(url, headers=headers, data=body_json, timeout=15)
    return resp.json()


def calcular_sl_atr(atr_pct: float, multiplo: float = 3.0, piso_minimo: float = 15.0) -> float:
    """
    23/08 (Juanjo) — SL dimensionado por la volatilidad propia de cada
    moneda, en vez de un % fijo para todas. Fórmula: multiplo × ATR% de
    la moneda, con un PISO MÍNIMO de ancho (nunca más ajustado que
    piso_minimo, aunque la moneda sea muy tranquila) — el ATR sí puede
    agrandarlo por encima de eso para monedas volátiles.

    multiplo=3.0: elegido como punto de partida razonable (múltiplo
    común en la práctica de trading para dimensionar stops por ATR — no
    es un número confirmado con datos propios todavía, a revisar con
    /resultado_atr una vez que haya suficientes cierres reales con este
    esquema activo).

    26/08 (Juanjo, investigación BOT_INTERNAL_ERROR) — redondeado a
    ENTERO (antes 2 decimales, ej. -18.45%) — prueba para descartar si
    valores no redondos en el campo lossStop contribuían a los errores
    internos de Pionex (INJ/NEAR/ZRX/ENS). NO toca el trailing — son
    sistemas completamente separados (el trailing vive en
    gestion_riesgo._calcular_piso_trailing_escalonado, opera sobre el
    resultado en tiempo real, nunca lee este valor).

    Devuelve el SL como número NEGATIVO en % (ej. -15.0, -22.0), listo
    para pasar a crear_grilla_futuros(sl_pct=...).
    """
    ancho = max(atr_pct * multiplo, piso_minimo)
    return float(-round(ancho))
