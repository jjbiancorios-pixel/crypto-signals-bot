"""
gestion_riesgo.py
Checklist de seguridad que corre ANTES de cada apertura automática de
grilla, y la rutina de monitoreo de zona de riesgo para operaciones abiertas.

No modifica la lógica de análisis técnico (main.py) ni la persistencia
básica (db.py). Solo agrega las reglas de capital y riesgo ya definidas
en el proyecto.
"""

import db
import pionex_api

CAPITAL_TOTAL_USD = 1000  # TODO: mover a variable de entorno cuando escale
PCT_OPERATIVO = 0.82
PCT_CAPITAL_POR_OPERACION = 0.09
MAX_ATASCADAS_RIESGO = 3
OBJETIVO_DIARIO_PCT = 3

# Margen de origen: colchón reservado desde la apertura de cada grilla
# (además del monitoreo reactivo cada 30 min). Decisión confirmada por
# Juanjo: usar margen de origen + reactivo combinados, no solo reactivo,
# porque el reactivo solo no llega a tiempo ante movimientos bruscos entre
# escaneos (dato real: las 177 operaciones históricas SIEMPRE tuvieron
# margen de origen, y aun así hubo que reforzar en algunos casos).
# 0.5 = el margen es 50% de la inversión (ej: inversión 90 USD -> margen 45 USD,
# 135 USD comprometidos en total por esa operación). Ajustable acá si hace
# falta más o menos colchón con datos reales de esta semana de prueba.
RATIO_MARGEN_ORIGEN = 0.5


def verificar_seguridad_apertura(capital_total: float = CAPITAL_TOTAL_USD) -> dict:
    """
    Corre el checklist completo ANTES de llamar a pionex_api.crear_grilla_futuros().
    Devuelve {"permitido": bool, "motivo": str, "capital_operacion": float,
    "margen_origen": float, "capital_total_comprometido": float}.

    Reglas (sección 6 del proyecto, sin reabrir):
    1. Modo restrictivo: si hay >=3 operaciones en zona amarilla/roja
       simultáneas, solo se permite abrir si TODAVÍA no se llegó al 3%
       del capital DISPONIBLE ese día (no del total).
    2. Debe haber capital operativo suficiente (82% del total, menos lo
       ya comprometido, menos lo apartado por operaciones en riesgo) para
       cubrir la inversión MÁS el margen de origen — el margen no es
       capital gratis, también sale del mismo pozo disponible.
    """
    capital_operacion = capital_total * PCT_CAPITAL_POR_OPERACION
    margen_origen = round(capital_operacion * RATIO_MARGEN_ORIGEN, 2)
    capital_total_comprometido = round(capital_operacion + margen_origen, 2)

    atascadas = db.contar_atascadas_riesgo()
    comprometido = db.capital_comprometido_total()
    apartado = db.capital_apartado_total()

    capital_operativo_max = capital_total * PCT_OPERATIVO
    capital_disponible = capital_operativo_max - comprometido - apartado

    modo_restrictivo = atascadas >= MAX_ATASCADAS_RIESGO

    if modo_restrictivo:
        # En modo restrictivo, el objetivo pasa a ser el 3% del capital
        # DISPONIBLE ese día (no del total). Si ya se llegó, no se abre más.
        capital_disponible_hoy = capital_total - comprometido - apartado
        ganancia_hoy = db.ganancia_hoy_pct(capital_disponible_hoy) if capital_disponible_hoy > 0 else 0
        if ganancia_hoy >= OBJETIVO_DIARIO_PCT:
            return {
                "permitido": False,
                "motivo": f"Modo restrictivo activo ({atascadas} atascadas-de-riesgo) "
                          f"y ya se cubrió el {OBJETIVO_DIARIO_PCT}% del capital disponible hoy.",
                "capital_operacion": capital_operacion,
            }
        # Si todavía no se llegó al objetivo, se permite seguir operando
        # PERO igual respetando el límite de capital disponible más abajo.

    if capital_disponible < capital_total_comprometido:
        return {
            "permitido": False,
            "motivo": f"Capital operativo insuficiente: disponible USD {capital_disponible:.2f}, "
                      f"se necesitan USD {capital_total_comprometido:.2f} "
                      f"(USD {capital_operacion:.2f} inversión + USD {margen_origen:.2f} margen de origen).",
            "capital_operacion": capital_operacion,
        }

    return {
        "permitido": True,
        "motivo": "OK",
        "capital_operacion": round(capital_operacion, 2),
        "margen_origen": margen_origen,
        "capital_total_comprometido": capital_total_comprometido,
        "modo_restrictivo": modo_restrictivo,
        "atascadas": atascadas,
    }


def monitorear_zonas_riesgo(capital_total: float = CAPITAL_TOTAL_USD) -> list:
    """
    Recorre las operaciones abiertas con bu_order_id, consulta su zona de
    riesgo real en Pionex, actualiza la DB, y si cae a zona roja llama a
    reforzar_margen() usando el 5% ya apartado. Pensada para correr en el
    mismo ciclo de 30 min que el análisis técnico.

    Devuelve un log de acciones tomadas, para avisar por Telegram.
    """
    acciones = []
    abiertas = db.operaciones_abiertas_con_bu_order()

    for op in abiertas:
        bu_order_id = op["bu_order_id"]
        senal_id = op["id"]
        par = op["par"]
        precio_entrada = op["precio_entrada"]

        try:
            # Nota: usar precio_entrada como aproximación si no hay
            # feed de precio en vivo disponible en este contexto; lo
            # ideal es pasar el precio actual real del par.
            resultado = pionex_api.calcular_zona_riesgo(bu_order_id, precio_entrada)
        except Exception as e:
            acciones.append(f"⚠️ {par}: error consultando Pionex ({e})")
            continue

        zona = resultado.get("zona", "desconocida")
        zona_anterior = op.get("zona_riesgo", "verde")

        if zona == "verde":
            if zona_anterior != "verde":
                db.actualizar_zona_riesgo(senal_id, "verde", capital_apartado=0)
                acciones.append(f"🟢 {par}: volvió a zona segura, capital liberado.")

        elif zona == "amarilla":
            capital_apartar = capital_total * 0.05
            if zona_anterior != "amarilla":
                db.actualizar_zona_riesgo(senal_id, "amarilla", capital_apartado=capital_apartar)
                acciones.append(f"🟡 {par}: zona amarilla, se aparta 5% (USD {capital_apartar:.2f}).")

        elif zona == "roja":
            capital_apartado_previo = op.get("capital_apartado") or (capital_total * 0.05)
            if zona_anterior != "roja":
                try:
                    pionex_api.reforzar_margen(bu_order_id, capital_apartado_previo, precio_entrada)
                    db.actualizar_zona_riesgo(senal_id, "roja", capital_apartado=capital_apartado_previo)
                    acciones.append(
                        f"🔴 {par}: zona roja, se reforzó margen con USD {capital_apartado_previo:.2f}."
                    )
                except Exception as e:
                    acciones.append(f"⚠️ {par}: zona roja pero falló refuerzo de margen ({e})")

    return acciones
