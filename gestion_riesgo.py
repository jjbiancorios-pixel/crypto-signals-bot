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
#
# IMPORTANTE (corregido con datos reales del usuario): Pionex REPARTE el
# capital total entre "inversión real" y "margen", no los suma. Ejemplo
# real: 52.56 inversión + 47.44 margen = ~100 total, no 100+47. Por eso
# el 9% de capital por operación (ya fijo, no se toca) se divide acá
# ~50/50, en vez de comprometer 13.5% como en la versión anterior.
RATIO_MARGEN_ORIGEN = 0.5  # % del 9% total que va a margen (el resto es inversión real)


def verificar_seguridad_apertura(capital_total: float = CAPITAL_TOTAL_USD) -> dict:
    """
    Corre el checklist completo ANTES de llamar a pionex_api.crear_grilla_futuros().
    Devuelve {"permitido": bool, "motivo": str, "capital_operacion": float,
    "inversion_real": float, "margen_origen": float}.

    Reglas (sección 6 del proyecto, sin reabrir):
    1. Modo restrictivo: si hay >=3 operaciones en zona amarilla/roja
       simultáneas, solo se permite abrir si TODAVÍA no se llegó al 3%
       del capital DISPONIBLE ese día (no del total).
    2. Debe haber capital operativo suficiente (82% del total, menos lo
       ya comprometido, menos lo apartado por operaciones en riesgo).

    El 9% de capital por operación SIGUE SIENDO 9% en total (no sube a
    13.5%) — internamente se reparte entre inversión real y margen de
    origen, igual que hace Pionex con el preset "Recomendada".
    """
    capital_operacion = capital_total * PCT_CAPITAL_POR_OPERACION
    margen_origen = round(capital_operacion * RATIO_MARGEN_ORIGEN, 2)
    inversion_real = round(capital_operacion - margen_origen, 2)

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

    if capital_disponible < capital_operacion:
        return {
            "permitido": False,
            "motivo": f"Capital operativo insuficiente: disponible USD {capital_disponible:.2f}, "
                      f"se necesitan USD {capital_operacion:.2f}.",
            "capital_operacion": capital_operacion,
        }

    return {
        "permitido": True,
        "motivo": "OK",
        "capital_operacion": round(capital_operacion, 2),
        "inversion_real": inversion_real,
        "margen_origen": margen_origen,
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

        # PASO 1: ¿ya cerró en Pionex? Si sí, liberar capital y no seguir
        # chequeando zona de riesgo sobre una operación que ya no existe.
        try:
            estado_cierre = pionex_api.esta_cerrada(bu_order_id)
        except Exception as e:
            acciones.append(f"⚠️ {par}: error consultando cierre ({e})")
            continue

        if estado_cierre.get("cerrada"):
            resultado_pct = estado_cierre.get("profit_stop_pct")
            if resultado_pct is not None:
                db.cerrar_senal_automatica(senal_id, resultado_pct)
                acciones.append(f"✅ {par}: cerrada en Pionex (TP {resultado_pct:.2f}%), capital liberado.")
            else:
                # Cerró pero no pudimos confirmar el resultado exacto (no
                # fue por TP normal — podría ser cierre manual o
                # liquidación). Se marca cerrada igual para liberar el
                # capital fantasma, pero con resultado 0% hasta que se
                # confirme manualmente con /cerrar.
                db.cerrar_senal_automatica(senal_id, 0.0)
                acciones.append(
                    f"⚠️ {par}: cerrada en Pionex (motivo: {estado_cierre.get('motivo')}), "
                    f"capital liberado, pero VERIFICÁ el resultado real y corregilo con /cerrar."
                )
            continue

        try:
            resultado = pionex_api.calcular_zona_riesgo_por_margen(
                bu_order_id, op.get("capital_asignado") or (capital_total * PCT_CAPITAL_POR_OPERACION),
                RATIO_MARGEN_ORIGEN
            )
        except Exception as e:
            acciones.append(f"⚠️ {par}: error consultando Pionex ({e})")
            continue

        zona = resultado.get("zona", "desconocida")
        zona_anterior = op.get("zona_riesgo", "verde")

        # Monto de refuerzo: igual al margen de origen que YA tiene esta
        # operación puntual (decisión confirmada) — no un % fijo del
        # capital total desconectado de la operación real.
        capital_asignado_op = op.get("capital_asignado") or (capital_total * PCT_CAPITAL_POR_OPERACION)
        margen_de_esta_operacion = round(capital_asignado_op * RATIO_MARGEN_ORIGEN, 2)

        if zona == "verde":
            if zona_anterior != "verde":
                db.actualizar_zona_riesgo(senal_id, "verde", capital_apartado=0)
                acciones.append(f"🟢 {par}: volvió a zona segura ({resultado.get('pct_restante')}% del colchón), capital liberado.")

        elif zona == "amarilla":
            if zona_anterior != "amarilla":
                db.actualizar_zona_riesgo(senal_id, "amarilla", capital_apartado=margen_de_esta_operacion)
                acciones.append(
                    f"🟡 {par}: zona amarilla ({resultado.get('pct_restante')}% del colchón), "
                    f"se aparta USD {margen_de_esta_operacion:.2f} (= margen de origen de esta operación)."
                )

        elif zona == "roja":
            monto_refuerzo = op.get("capital_apartado") or margen_de_esta_operacion
            if zona_anterior != "roja":
                precio_ref = resultado.get("position_open_price") or precio_entrada
                try:
                    pionex_api.reforzar_margen(bu_order_id, monto_refuerzo, precio_ref)
                    db.actualizar_zona_riesgo(senal_id, "roja", capital_apartado=monto_refuerzo)
                    acciones.append(
                        f"🔴 {par}: zona roja ({resultado.get('pct_restante')}% del colchón), "
                        f"se reforzó margen con USD {monto_refuerzo:.2f}."
                    )
                except Exception as e:
                    acciones.append(f"⚠️ {par}: zona roja pero falló refuerzo de margen ({e})")

    return acciones
