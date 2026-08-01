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
from datetime import datetime, timezone, timedelta

TZ_ARG = timezone(timedelta(hours=-3))

CAPITAL_TOTAL_USD = 782  # 28/07: actualizado con el capital real post-pérdidas de v15 confirmado por Juanjo

# ── v16 (27-28/07): cambio de filosofía "diversificado" a "pocas y grandes" ──
# 35% de capital x 2 posiciones simultáneas + 30% de reserva líquida
# inmovilizada. El 30% de reserva REEMPLAZA al 15% de la actualización
# anterior (20-21/07) — no se suman, es la única reserva ahora.
PCT_OPERATIVO = 0.70  # v16: 2 posiciones x 35% = 70% operativo máximo, 30% reserva
PCT_CAPITAL_POR_OPERACION = 0.35  # v16: 6% -> 35% (pocas y grandes, alta convicción)
MAX_POSICIONES_SIMULTANEAS = 2  # v16: tope duro nuevo — antes no existía (~14 con el esquema 6%)
# v16: recalibrado 6 -> 1. El 6 (de la actualización 20-21/07) quedaba
# matemáticamente imposible de alcanzar con el tope nuevo de 2 posiciones
# simultáneas (nunca puede haber 6 atascadas si el máximo son 2). Se
# recalibra manteniendo la misma proporción aproximada del diseño original
# (6 de ~14 posiciones ≈ 43% -> redondea a 1 de 2).
MAX_ATASCADAS_RIESGO = 1
MAX_APERTURAS_POR_CICLO = 1  # v16: nuevo — máx. 1 apertura nueva por ciclo de 15 min
STOP_LOSS_PCT = -20  # 28/07: NUEVO — cierre real si una operación llega a este % de pérdida sobre su capital
# asignado, sin importar cuántos días lleve. Calibrado con expectancy real sobre 542 operaciones
# históricas limpias (98% win rate, +1.66% promedio ganador): -20% da expectancy de +1.25%/operación,
# vs. dejar correr sin límite (que en 8 casos reales promedió -103%, incluido EGLD -215.68%, SEI -130%,
# RUNE -75%, MASK -31%). Reemplaza parcialmente "nunca cerrar en pérdida nominal" SOLO para pérdidas que
# superan este piso. Recalibrar cuando haya datos de MAE real (v16 todavía no los captura).
UMBRAL_GRID_DINAMICO_PCT = 10  # v16 (modo sombra): distancia al borde del rango que "hubiera" disparado un ajuste
BETA_REBOTE_DGT_PCT = 0.3  # v16 (modo sombra, 28/07): % de rebote/pullback confirmado que exige el paper DGT antes de ajustar — valor propio, sin dato histórico detrás todavía, a calibrar con el informe semanal
OBJETIVO_DIARIO_PCT = 3

# Umbral de intervención por duración (cierre a las 10hs si cubre costos)
HORAS_CIERRE_AUTOMATICO = 10
RESULTADO_MINIMO_CIERRE_10HS = 0.2  # % — YA NO se usa para cerrar (28/07: PASO 2 pasó a ser solo informativo). Queda de referencia histórica.

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
RATIO_MARGEN_ORIGEN = 0.3  # Actualización 20-21/07: 50% -> 30% (70% inversión / 30% margen)


def verificar_seguridad_apertura(capital_total: float = CAPITAL_TOTAL_USD,
                                  aperturas_este_ciclo: int = 0,
                                  es_reapertura: bool = False) -> dict:
    """
    Corre el checklist completo ANTES de llamar a pionex_api.crear_grilla_futuros().
    Devuelve {"permitido": bool, "motivo": str, "capital_operacion": float,
    "inversion_real": float, "margen_origen": float}.

    Reglas v16 (filosofía "pocas y grandes"):
    0. Tope duro de posiciones simultáneas (MAX_POSICIONES_SIMULTANEAS=2).
    1. Máx. 1 apertura nueva por ciclo de 15 min (MAX_APERTURAS_POR_CICLO),
       salvo que sea una reapertura del sistema <5min (es_reapertura=True),
       que tiene su propia vara más exigente (VWAP+EMA de régimen) y no
       compite por este cupo — ver telegram/main para el detalle.
    2. Modo restrictivo: si hay >=1 operación en zona amarilla/roja
       (MAX_ATASCADAS_RIESGO=1, recalibrado para el tope de 2 posiciones),
       solo se permite abrir si TODAVÍA no se llegó al 3% del capital
       DISPONIBLE ese día (no del total).
    3. Debe haber capital operativo suficiente (70% del total: 35% x2
       posiciones, 30% restante es reserva líquida inmovilizada).

    El 35% de capital por operación se reparte 70/30 entre inversión real
    y margen de origen (RATIO_MARGEN_ORIGEN), igual que hace Pionex con
    el preset "Recomendada".
    """
    capital_operacion = capital_total * PCT_CAPITAL_POR_OPERACION
    margen_origen = round(capital_operacion * RATIO_MARGEN_ORIGEN, 2)
    inversion_real = round(capital_operacion - margen_origen, 2)

    posiciones_abiertas = len(db.operaciones_abiertas_con_bu_order())
    if posiciones_abiertas >= MAX_POSICIONES_SIMULTANEAS:
        return {
            "permitido": False,
            "motivo": f"Tope de posiciones simultáneas alcanzado "
                      f"({posiciones_abiertas}/{MAX_POSICIONES_SIMULTANEAS}).",
            "capital_operacion": capital_operacion,
        }

    if not es_reapertura and aperturas_este_ciclo >= MAX_APERTURAS_POR_CICLO:
        return {
            "permitido": False,
            "motivo": f"Ya se abrió {aperturas_este_ciclo} posición nueva en este ciclo "
                      f"(máx. {MAX_APERTURAS_POR_CICLO}/ciclo).",
            "capital_operacion": capital_operacion,
        }

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


def monitorear_zonas_riesgo(capital_total: float = CAPITAL_TOTAL_USD) -> dict:
    """
    Recorre las operaciones abiertas con bu_order_id, consulta su zona de
    riesgo real en Pionex, actualiza la DB, y si cae a zona roja llama a
    reforzar_margen() usando el 5% ya apartado. Pensada para correr en el
    mismo ciclo de 30 min que el análisis técnico.

    v16: además detecta cierres en MENOS DE 5 MINUTOS desde la apertura, y
    los devuelve en "candidatos_reapertura" para que main.py dispare el
    análisis de reapertura de esa moneda puntual (esta función no puede
    llamar a main.analizar_par directamente sin crear un import circular).

    Devuelve {"acciones": [...], "candidatos_reapertura": [...]}.
    """
    acciones = []
    candidatos_reapertura = []
    abiertas = db.operaciones_abiertas_con_bu_order()

    for op in abiertas:
        bu_order_id = op["bu_order_id"]
        senal_id = op["id"]
        par = op["par"]
        precio_entrada = op["precio_entrada"]

        try:
            apertura = datetime.strptime(f"{op['fecha']} {op['hora_alerta']}", "%Y%m%d %H:%M").replace(tzinfo=TZ_ARG)
            minutos_abierta = (datetime.now(TZ_ARG) - apertura).total_seconds() / 60
        except Exception:
            minutos_abierta = None

        # PASO 1: ¿ya cerró en Pionex? Si sí, liberar capital y no seguir
        # chequeando zona de riesgo sobre una operación que ya no existe.
        try:
            estado_cierre = pionex_api.esta_cerrada(bu_order_id)
        except Exception as e:
            acciones.append(f"⚠️ {par}: error consultando cierre ({e})")
            continue

        if estado_cierre.get("cerrada"):
            # FIX 28/07: no confiar en una sola lectura — Pionex puede
            # mostrar un status "cerrado-like" transitorio durante una
            # edición manual de la grilla (caso real: MOVE, 27/07 — el
            # bu_order_id nunca cambió y seguía 'running', el bot tomó una
            # foto momentánea como cierre definitivo). Se exige confirmar
            # en el chequeo SIGUIENTE antes de dar el cierre por real (y
            # antes de evaluar la reapertura, que solo debe dispararse
            # sobre un cierre YA confirmado).
            if not op.get("cierre_pendiente_desde"):
                db.marcar_cierre_pendiente(senal_id)
                acciones.append(f"🔎 {par}: posible cierre detectado, confirmando en el próximo chequeo...")
                continue

            resultado_pct = estado_cierre.get("resultado_pct")
            if resultado_pct is not None:
                # 29/07: Pionex casi nunca reporta reasonBy, así que se
                # infiere el motivo por el resultado — >=1.0% es casi
                # seguro el TP de 1.35% (con margen por fees/slippage).
                motivo_final = "tp" if resultado_pct >= 1.0 else (estado_cierre.get("motivo") or "desconocido")
                db.cerrar_senal_automatica(senal_id, resultado_pct, motivo=motivo_final)
                emoji = "✅" if resultado_pct >= 0 else "🔴"
                acciones.append(f"{emoji} {par}: cerrada en Pionex ({resultado_pct:+.2f}% real, confirmado), capital liberado.")
            else:
                # Cerró pero no pudimos calcular el resultado exacto (faltan
                # datos de marginBalance/investment en la respuesta). Se
                # marca cerrada igual para liberar el capital fantasma, pero
                # con resultado 0% hasta que se confirme manual con /cerrar.
                db.cerrar_senal_automatica(senal_id, 0.0, motivo=estado_cierre.get("motivo") or "desconocido")
                acciones.append(
                    f"⚠️ {par}: cerrada en Pionex (motivo: {estado_cierre.get('motivo')}, confirmado), "
                    f"capital liberado, pero VERIFICÁ el resultado real y corregilo con /cerrar."
                )

            # v16: sistema de reapertura — solo si cerró en <5min y todavía
            # no se alcanzó el máximo de 2 reaperturas para esta cadena.
            # (minutos_abierta se calculó al detectar por primera vez el
            # posible cierre, en el chequeo anterior — sigue siendo válido)
            num_reapertura_actual = op.get("num_reapertura") or 0
            if minutos_abierta is not None and minutos_abierta < 5 and num_reapertura_actual < 2:
                acciones.append(f"🔁 {par}: cerró en {minutos_abierta:.1f} min — evaluando reapertura...")
                candidatos_reapertura.append({
                    "par": par,
                    "senal_id_original": senal_id,
                    "num_reapertura_actual": num_reapertura_actual,
                    "direccion_original": op.get("direccion"),
                })
            continue
        elif op.get("cierre_pendiente_desde"):
            # Estaba pendiente de confirmar, pero este chequeo dio que
            # SIGUE corriendo -> falsa alarma, se descarta sin tocar nada.
            db.limpiar_cierre_pendiente(senal_id)
            acciones.append(f"↩️ {par}: falsa alarma de cierre descartada (sigue corriendo en Pionex).")

        # PASO 1.5 (28/07, NUEVO) — stop-loss real por % de pérdida. Corre
        # TODOS los ciclos (no depende de cuántas horas/días lleve abierta),
        # a diferencia del aviso de 10hs de abajo. Es un cierre REAL
        # (cerrar_grilla_futuros), no solo informativo — decisión de Juanjo
        # tras el caso real de EGLD (-215.68%, cerró en solo 6hs, algo que
        # ninguna regla basada en tiempo hubiera prevenido).
        capital_real_op = op.get("capital_asignado") or (capital_total * PCT_CAPITAL_POR_OPERACION)
        try:
            desglose = pionex_api.calcular_resultado_desglosado(bu_order_id, capital_total_real=capital_real_op)
        except Exception:
            desglose = None
        resultado_actual_sl = desglose["total_pct"] if desglose else None

        # 29/07 (modo sombra) — guardar el desglose rejilla vs. tendencia,
        # para comparar más adelante si el grid aporta valor real por
        # sobre una posición direccional simple.
        if desglose:
            db.actualizar_desglose_resultado(senal_id, desglose["rejilla_pct"], desglose["tendencia_pct"])

        # 28/07 (modo sombra) — registrar el peor resultado alcanzado hasta
        # ahora, para poder calcular MAE real más adelante con datos
        # propios. No cambia ningún comportamiento, solo guarda el dato.
        if resultado_actual_sl is not None:
            db.actualizar_peor_resultado(senal_id, resultado_actual_sl)
            # 29/07 (modo sombra) — mismo dato, pero el MEJOR alcanzado
            # (MFE) — para el análisis de si el TP de 1.35% deja plata
            # en la mesa, o si el precio se acerca y revierte sin llegar.
            db.actualizar_mejor_resultado(senal_id, resultado_actual_sl)

        if resultado_actual_sl is not None and resultado_actual_sl <= STOP_LOSS_PCT:
            try:
                pionex_api.cerrar_grilla_futuros(bu_order_id, nota=f"Stop-loss automático {STOP_LOSS_PCT}%")
                db.cerrar_senal_automatica(senal_id, resultado_actual_sl, motivo="stop_loss")
                acciones.append(
                    f"🛑 {par}: STOP-LOSS ejecutado a {resultado_actual_sl:+.2f}% "
                    f"(umbral {STOP_LOSS_PCT}%), capital liberado."
                )
                continue
            except Exception as e:
                acciones.append(
                    f"⚠️ {par}: tocó el stop-loss ({resultado_actual_sl:+.2f}%) pero falló el cierre real ({e}) — REVISAR YA."
                )

        # PASO 2 (28/07: CAMBIADO a solo informativo, ya NO cierra nada —
        # decisión confirmada: el único cierre real sigue siendo el TP de
        # 1.35% que Pionex ejecuta solo). Antes acá se cerraba
        # automáticamente a las 10hs si superaba +0.2% — se sacó esa acción
        # por pedido de Juanjo. Se avisa UNA sola vez por operación (no en
        # cada chequeo, y en v16 el chequeo corre cada 1 min) para no
        # spamear Telegram.
        horas_abierta = minutos_abierta / 60 if minutos_abierta is not None else None

        if horas_abierta is not None and horas_abierta >= HORAS_CIERRE_AUTOMATICO and not op.get("aviso_10hs_enviado"):
            capital_real_op = op.get("capital_asignado") or (capital_total * PCT_CAPITAL_POR_OPERACION)
            resultado_actual = pionex_api.calcular_resultado_actual(bu_order_id, capital_total_real=capital_real_op)
            db.marcar_aviso_10hs_enviado(senal_id)
            if resultado_actual is not None:
                acciones.append(
                    f"⏱️ {par}: lleva {horas_abierta:.1f}hs abierta, resultado actual {resultado_actual:+.2f}% "
                    f"(solo informativo, no se cierra automático)."
                )
            else:
                acciones.append(f"⏱️ {par}: lleva {horas_abierta:.1f}hs abierta (no se pudo calcular el % actual).")

        try:
            resultado = pionex_api.calcular_zona_riesgo_combinada(
                bu_order_id, op.get("capital_asignado") or (capital_total * PCT_CAPITAL_POR_OPERACION),
                RATIO_MARGEN_ORIGEN, par
            )
        except Exception as e:
            acciones.append(f"⚠️ {par}: error consultando Pionex ({e})")
            continue

        zona = resultado.get("zona", "desconocida")
        zona_anterior = op.get("zona_riesgo", "verde")

        # v16: log en modo sombra del grid dinámico — reusa el precio_actual
        # ya consultado arriba (para la zona combinada), no pide uno nuevo.
        # 28/07: actualizado a la regla REAL del paper DGT (arXiv 2506.11921)
        # — no basta con estar cerca del borde, hace falta un REBOTE
        # CONFIRMADO (beta%) desde el mínimo/máximo ya alcanzado, recién ahí
        # "hubiera ajustado". Antes era un placeholder de solo distancia.
        precio_actual = resultado.get("precio_actual")
        rango_bajo_op = op.get("rango_bajo_calc")
        rango_alto_op = op.get("rango_alto_calc")
        if precio_actual and rango_bajo_op and rango_alto_op and precio_actual > 0:
            dist_top_pct = (rango_alto_op - precio_actual) / precio_actual * 100
            dist_bottom_pct = (precio_actual - rango_bajo_op) / precio_actual * 100
            if dist_top_pct <= dist_bottom_pct:
                lado, distancia_borde_pct = "top", dist_top_pct
            else:
                lado, distancia_borde_pct = "bottom", dist_bottom_pct

            minimo_hist, maximo_hist = db.extremos_precio_grid_dinamico(senal_id)
            rebote_confirmado = False
            if lado == "bottom" and minimo_hist and minimo_hist > 0:
                rebote_pct = (precio_actual - minimo_hist) / minimo_hist * 100
                rebote_confirmado = rebote_pct >= BETA_REBOTE_DGT_PCT
            elif lado == "top" and maximo_hist and maximo_hist > 0:
                pullback_pct = (maximo_hist - precio_actual) / maximo_hist * 100
                rebote_confirmado = pullback_pct >= BETA_REBOTE_DGT_PCT

            hubiera_ajustado = distancia_borde_pct <= UMBRAL_GRID_DINAMICO_PCT and rebote_confirmado
            db.guardar_log_grid_dinamico(
                senal_id, par, precio_actual, rango_bajo_op, rango_alto_op,
                lado, round(distancia_borde_pct, 2), rebote_confirmado, hubiera_ajustado
            )

        # Monto de refuerzo: igual al margen de origen que YA tiene esta
        # operación puntual (decisión confirmada) — no un % fijo del
        # capital total desconectado de la operación real.
        capital_asignado_op = op.get("capital_asignado") or (capital_total * PCT_CAPITAL_POR_OPERACION)
        margen_de_esta_operacion = round(capital_asignado_op * RATIO_MARGEN_ORIGEN, 2)

        metodo = resultado.get("metodo_decisivo", "margen")
        detalle_metodo = (
            f"{resultado.get('pct_restante')}% del colchón (margen)" if metodo == "margen"
            else f"{resultado.get('distancia_pct')}% de distancia a liquidación"
        )

        if zona == "verde":
            if zona_anterior != "verde":
                db.actualizar_zona_riesgo(senal_id, "verde", capital_apartado=0)
                acciones.append(f"🟢 {par}: volvió a zona segura ({detalle_metodo}), capital liberado.")

        elif zona == "amarilla":
            if zona_anterior != "amarilla":
                db.actualizar_zona_riesgo(senal_id, "amarilla", capital_apartado=margen_de_esta_operacion)
                acciones.append(
                    f"🟡 {par}: zona amarilla ({detalle_metodo}), "
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
                        f"🔴 {par}: zona roja ({detalle_metodo}), "
                        f"se reforzó margen con USD {monto_refuerzo:.2f}."
                    )
                except Exception as e:
                    acciones.append(f"⚠️ {par}: zona roja pero falló refuerzo de margen ({e})")

    return {"acciones": acciones, "candidatos_reapertura": candidatos_reapertura}
