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
# 35% de capital x 2 posiciones simultáneas. Reserva del 30% ELIMINADA
# (01/08, decisión de Juanjo): con el stop-loss real de -20% ya activo,
# el colchón de reserva pura quedó redundante en el 99.9% de los casos —
# solo protegía contra un escenario ya cubierto por el stop. El único
# riesgo residual real (falla técnica: API caída, Railway reiniciado, que
# impida ejecutar el stop-loss a tiempo) se cubre ahora con el margen de
# origen más chico (10%, ver RATIO_MARGEN_ORIGEN), no con una reserva aparte.
PCT_OPERATIVO = 1.0  # v16: sin reserva aparte — el tope real de todos modos lo pone MAX_POSICIONES_SIMULTANEAS x PCT_CAPITAL_POR_OPERACION
PCT_CAPITAL_POR_OPERACION = 0.425  # 01/08: 35% -> 42.5% (Juanjo, punto medio entre 35% y 50% que se discutieron). Con interés compuesto diario, esto es el valor de RESPALDO si todavía no corrió el recálculo de las 00:01 — el tamaño real del día lo fija db.capital_diario.tamano_objetivo
RESERVA_RECUPERO_PCT = 0.15  # 01/08: % del capital del día que se reserva para completar el tamaño objetivo si el día viene en pérdida (no es margen ni reserva "inmóvil" — se usa para ABRIR, no para reforzar)
MAX_POSICIONES_SIMULTANEAS = 2  # v16: tope duro nuevo — antes no existía (~14 con el esquema 6%)
# v16: recalibrado 6 -> 1. El 6 (de la actualización 20-21/07) quedaba
# matemáticamente imposible de alcanzar con el tope nuevo de 2 posiciones
# simultáneas (nunca puede haber 6 atascadas si el máximo son 2). Se
# recalibra manteniendo la misma proporción aproximada del diseño original
# (6 de ~14 posiciones ≈ 43% -> redondea a 1 de 2).
MAX_ATASCADAS_RIESGO = 1
MAX_APERTURAS_POR_CICLO = 2  # 12/08: subido de 1 a 2 (Juanjo) — igual al tope de posiciones simultáneas, para no perder una 2da señal buena cuando hay capital libre y aparecen 2 el mismo ciclo
STOP_LOSS_PCT = -15  # v17: techo absoluto INCONDICIONAL — antes -20%, reemplazado por el esquema de checkpoints. Este chequeo queda como RED DE SEGURIDAD (el cierre real de -15% lo protege el SL nativo de Pionex, mucho más rápido que nuestro monitoreo de 1 min — esto es un backup por si esa protección fallara).
# v17 — Checkpoints de pérdida CON análisis técnico (no solo magnitud):
# en -6/-9/-12%, se evalúan 4 factores; si se cumplen 3 de 4, se mantiene
# hasta el próximo checkpoint; si 2 o menos, se cierra ahí mismo. -15% es
# el techo absoluto, sin excepción, sin importar el análisis.
CHECKPOINTS_PERDIDA = [-6.0, -9.0, -12.0]
# v17 — Piso ascendente de ganancia (ratchet): una vez que el resultado
# toca cada nivel, se sube el SL REAL de Pionex (lossStop) al piso
# correspondiente — Pionex protege ese nivel con su motor rápido, no el
# nuestro. Más allá de 1.85%, pasa a un retroceso proporcional del pico.
RATCHET_GANANCIA = {1.35: 1.20, 1.5: 1.35, 1.6: 1.45, 1.7: 1.55, 1.85: 1.70}
RETROCESO_GANANCIA_PCT_V17 = 0.20
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
RATIO_MARGEN_ORIGEN = 0.10  # 01/08: 30% -> 10% (Juanjo: con stop-loss -20% activo, colchón de 30% quedó excesivo — 10% sigue cubriendo el escenario residual de falla técnica, ~9% de movimiento real de precio en 10x)


def intentar_liberar_slot_para_senal_nueva() -> dict:
    """
    v17 — Si el tope de posiciones está lleno pero apareció una señal
    nueva de alta probabilidad, busca entre las posiciones ABIERTAS si
    alguna ya alcanzó el primer piso de ganancia (>=1.35%, elegible para
    liberarse sin perder plata) y la cierra para hacerle lugar a la
    nueva. Si hay varias elegibles, libera la que tiene MENOR ganancia
    actual (la que menos potencial futuro sacrifica). Devuelve
    {"liberado": bool, "par": str|None, "resultado_pct": float|None}.
    """
    abiertas = db.operaciones_abiertas_con_bu_order()
    elegibles = [op for op in abiertas if (op.get("mejor_resultado_pct") or 0) >= 1.35]
    if not elegibles:
        return {"liberado": False, "par": None, "resultado_pct": None}

    elegibles.sort(key=lambda op: op.get("mejor_resultado_pct") or 0)
    candidata = elegibles[0]
    bu_order_id = candidata.get("bu_order_id")
    par = candidata.get("par")
    try:
        desglose = pionex_api.calcular_resultado_desglosado(
            bu_order_id, par=par, capital_total_real=candidata.get("capital_asignado")
        )
        resultado_actual = desglose["total_pct"] if desglose else candidata.get("mejor_resultado_pct")
        pionex_api.cerrar_grilla_futuros(bu_order_id, nota="v17: liberado para nueva señal de alta probabilidad")
        db.cerrar_senal_automatica(candidata["id"], resultado_actual, motivo="liberado_para_nueva_senal")
        return {"liberado": True, "par": par, "resultado_pct": resultado_actual}
    except Exception:
        return {"liberado": False, "par": None, "resultado_pct": None}


def verificar_seguridad_apertura(capital_total: float = CAPITAL_TOTAL_USD,
                                  aperturas_este_ciclo: int = 0,
                                  es_reapertura: bool = False) -> dict:
    """
    Corre el checklist completo ANTES de llamar a pionex_api.crear_grilla_futuros().
    Devuelve {"permitido": bool, "motivo": str, "capital_operacion": float,
    "inversion_real": float, "margen_origen": float}.

    Reglas v16 (filosofía "pocas y grandes"):
    0. Tope duro de posiciones simultáneas (MAX_POSICIONES_SIMULTANEAS=2).
    1. Máx. 2 aperturas nuevas por ciclo de 15 min (MAX_APERTURAS_POR_CICLO,
       subido de 1 a 2 el 12/08 — igual al tope de posiciones, para no
       perder una 2da señal buena el mismo ciclo si hay capital libre),
       salvo que sea una reapertura del sistema <5min (es_reapertura=True),
       que tiene su propia vara más exigente (VWAP+EMA de régimen) y no
       compite por este cupo — ver telegram/main para el detalle.
    2. Modo restrictivo: si hay >=1 operación en zona amarilla/roja
       (MAX_ATASCADAS_RIESGO=1, recalibrado para el tope de 2 posiciones),
       solo se permite abrir si TODAVÍA no se llegó al 3% del capital
       DISPONIBLE ese día (no del total).
    3. Debe haber capital operativo suficiente (70% del total: 35% x2
       posiciones, 30% restante es reserva líquida inmovilizada).

    El 42.5% de capital por operación se reparte 90/10 entre inversión real
    y margen de origen (RATIO_MARGEN_ORIGEN).

    01/08 — INTERÉS COMPUESTO + RESERVA DE RECUPERO:
    - `tamano_objetivo`: fijado una vez a las 00:01 (42.5% del capital real
      de ESE momento) — es la referencia/techo que se intenta mantener
      todo el día, no cambia operación a operación.
    - Cada operación calcula su tamaño NATURAL como 42.5% del capital real
      de HOY (capital del día + resultado acumulado de operaciones ya
      cerradas hoy) — este sí se achica solo si el día viene perdiendo.
    - Si el día está en pérdida Y el tamaño natural quedó por debajo del
      objetivo, se completa la diferencia con la reserva de recupero
      (15% fijado a las 00:01, se va gastando durante el día) — para que
      una operación perdedora no reduzca el tamaño en dólares de la
      siguiente. Se corta sola si el día vuelve a positivo o si la
      reserva ya se agotó.
    - Si todavía no corrió el recálculo diario (recién desplegado, o
      pospuesto por tener operaciones abiertas a las 00:01), cae al
      cálculo viejo (capital_total * PCT_CAPITAL_POR_OPERACION) sin
      reserva, para no bloquear el bot mientras tanto.
    """
    cap_diario = db.obtener_capital_diario()
    if cap_diario:
        tamano_objetivo = cap_diario["tamano_objetivo"]
        capital_base = cap_diario["capital_dia"]
        reserva_restante = cap_diario["reserva_restante"]
    else:
        tamano_objetivo = capital_total * PCT_CAPITAL_POR_OPERACION
        capital_base = capital_total
        reserva_restante = 0.0

    resultado_hoy_usd = db.resultado_acumulado_usd_hoy()
    capital_real_hoy = capital_base + resultado_hoy_usd
    tamano_natural_ahora = capital_real_hoy * PCT_CAPITAL_POR_OPERACION

    usando_reserva = 0.0
    if resultado_hoy_usd < 0 and tamano_natural_ahora < tamano_objetivo and reserva_restante > 0:
        faltante = tamano_objetivo - tamano_natural_ahora
        usando_reserva = min(faltante, reserva_restante)
        capital_operacion = tamano_natural_ahora + usando_reserva
    else:
        capital_operacion = tamano_objetivo if cap_diario else tamano_natural_ahora

    margen_origen = round(capital_operacion * RATIO_MARGEN_ORIGEN, 2)
    inversion_real = round(capital_operacion - margen_origen, 2)

    posiciones_abiertas = len(db.operaciones_abiertas_con_bu_order())
    liberacion = None
    if posiciones_abiertas >= MAX_POSICIONES_SIMULTANEAS:
        # v17 — antes de rechazar directo, intentar liberar una posición
        # que ya esté por encima del primer piso de ganancia (1.35%),
        # para no perderse una señal nueva de alta probabilidad solo
        # porque el capital está ocupado en algo que ya "cobró lo suyo".
        liberacion = intentar_liberar_slot_para_senal_nueva()
        if liberacion["liberado"]:
            posiciones_abiertas -= 1
        else:
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

    # Techo operativo real: se basa en capital_real_hoy (ya ajustado por
    # el resultado de hoy), no en el número fijo de las 00:01 — para que
    # el chequeo de "hay plata suficiente comprometida vs. disponible"
    # sea sobre la realidad de ahora, no sobre una foto vieja del día.
    capital_operativo_max = capital_real_hoy * PCT_OPERATIVO
    capital_disponible = capital_operativo_max - comprometido - apartado

    modo_restrictivo = atascadas >= MAX_ATASCADAS_RIESGO

    if modo_restrictivo:
        # En modo restrictivo, el objetivo pasa a ser el 3% del capital
        # DISPONIBLE ese día (no del total). Si ya se llegó, no se abre más.
        capital_disponible_hoy = capital_real_hoy - comprometido - apartado
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

    if usando_reserva > 0:
        db.descontar_reserva_diaria(usando_reserva)

    return {
        "permitido": True,
        "motivo": "OK",
        "capital_operacion": round(capital_operacion, 2),
        "inversion_real": inversion_real,
        "margen_origen": margen_origen,
        "modo_restrictivo": modo_restrictivo,
        "atascadas": atascadas,
        "usando_reserva": round(usando_reserva, 2),
        "liberacion": liberacion,
    }


def _evaluar_factores_tecnicos_perdida(par: str, es_largo: bool) -> dict:
    """
    v17 — Evalúa los 4 factores técnicos en un checkpoint de pérdida:
    1. RSI en sobreventa (LARGO) o sobrecompra (CORTO) — a favor de reversión
    2. ADX bajando — la tendencia en contra pierde fuerza (comparación
       ventana reciente vs. anterior, sin necesitar guardar estado extra)
    3. Precio tocó/cerca de la banda de Bollinger contraria
    4. BTC no refuerza el movimiento en contra
    Import de main.py DIFERIDO (dentro de la función) para evitar import
    circular — gestion_riesgo no puede importar main a nivel de módulo
    porque main ya importa gestion_riesgo.
    """
    import main
    try:
        df15 = main.get_velas(par, "15m", n=40)
        if df15 is None or len(df15) < 30:
            return {"factores_cumplidos": 0, "error": "sin velas suficientes"}

        rsi = main.calc_rsi(df15["close"])
        bb = main.calc_bb(df15["close"])
        adx_reciente = main.calc_adx(df15.tail(20))["adx"]
        adx_anterior = main.calc_adx(df15.iloc[-28:-8])["adx"]
        adx_bajando = adx_reciente < adx_anterior
        btc = main.analizar_btc()

        if es_largo:
            f1 = rsi <= 32          # sobreventa
            f3 = bb["pos"] <= 0.10  # tocó/cerca de la banda inferior
            f4 = btc["estado"] != "BAJISTA"  # BTC no refuerza la caída
        else:
            f1 = rsi >= 68          # sobrecompra
            f3 = bb["pos"] >= 0.90  # tocó/cerca de la banda superior
            f4 = btc["estado"] != "ALCISTA"  # BTC no refuerza la subida
        f2 = adx_bajando

        return {
            "factores_cumplidos": sum([f1, f2, f3, f4]), "rsi": rsi, "adx": adx_reciente,
            "adx_bajando": adx_bajando, "btc_estado": btc["estado"], "toco_banda_contraria": f3,
        }
    except Exception as e:
        return {"factores_cumplidos": 0, "error": str(e)}


def _procesar_checkpoint_perdida(op: dict, resultado_actual: float, bu_order_id: str):
    """
    v17 — Si resultado_actual cruzó un checkpoint de pérdida (-6/-9/-12)
    todavía no evaluado, decide cerrar o mantener según los 4 factores
    técnicos (3 de 4 = mantener, 2 o menos = cerrar). El techo de -15% es
    aparte (ver el chequeo de STOP_LOSS_PCT, incondicional, sin análisis).
    """
    ya_evaluado = op.get("checkpoint_perdida_evaluado")
    par = op["par"]
    senal_id = op["id"]

    for checkpoint in CHECKPOINTS_PERDIDA:
        if resultado_actual > checkpoint:
            continue
        if ya_evaluado is not None and ya_evaluado <= checkpoint:
            continue  # este checkpoint (o uno más profundo) ya se evaluó antes

        direccion = op.get("direccion", "")
        es_largo = "LARGO" in direccion
        analisis = _evaluar_factores_tecnicos_perdida(par, es_largo)
        factores = analisis.get("factores_cumplidos", 0)
        decision = "mantener" if factores >= 3 else "cerrar"

        db.guardar_checkpoint_v17(
            senal_id, par, "perdida", checkpoint, resultado_actual, decision=decision,
            rsi=analisis.get("rsi"), adx=analisis.get("adx"), adx_bajando=analisis.get("adx_bajando"),
            btc_estado=analisis.get("btc_estado"), toco_banda_contraria=analisis.get("toco_banda_contraria"),
            factores_cumplidos=factores,
        )
        db.marcar_checkpoint_perdida_evaluado(senal_id, checkpoint)

        if decision == "cerrar":
            try:
                pionex_api.cerrar_grilla_futuros(bu_order_id, nota=f"v17: checkpoint {checkpoint}%, {factores}/4 factores")
                db.cerrar_senal_automatica(senal_id, resultado_actual, motivo=f"checkpoint_{checkpoint}")
                return (f"🛑 {par}: cerrada en checkpoint {checkpoint}% ({resultado_actual:+.2f}% real) — "
                        f"solo {factores}/4 factores técnicos a favor de mantener.")
            except Exception as e:
                return f"⚠️ {par}: quiso cerrar en checkpoint {checkpoint}% pero falló ({e}) — REVISAR."
        else:
            return (f"📊 {par}: tocó checkpoint {checkpoint}% ({resultado_actual:+.2f}% real) — "
                    f"{factores}/4 factores a favor, MANTIENE hasta el próximo checkpoint.")
    return None


def _procesar_piso_ganancia(op: dict, resultado_actual: float, pico_actual: float, bu_order_id: str):
    """
    v17 — Si pico_actual alcanzó un nuevo nivel del ratchet de ganancia,
    sube el SL real de Pionex (lossStop) al piso correspondiente — el
    motor rápido de Pionex protege ese nivel, no nuestro monitoreo. Más
    allá de 1.85%, usa retroceso proporcional del 20% del pico en vez de
    niveles fijos.
    """
    piso_actual_fijado = op.get("piso_ganancia_actual")
    senal_id = op["id"]
    par = op["par"]

    if pico_actual >= 1.85:
        nuevo_piso = round(pico_actual * (1 - RETROCESO_GANANCIA_PCT_V17), 4)
        if piso_actual_fijado is not None and nuevo_piso <= piso_actual_fijado:
            return None
        try:
            pionex_api.modificar_stop_loss(bu_order_id, nuevo_piso)
            db.actualizar_piso_ganancia(senal_id, nuevo_piso)
            db.guardar_checkpoint_v17(senal_id, par, "ganancia", pico_actual, resultado_actual,
                                       decision=f"piso subido a {nuevo_piso} (retroceso 20%)")
            return f"📈 {par}: pico {pico_actual:+.2f}% — piso de Pionex subido a {nuevo_piso:+.2f}% (retroceso 20%)."
        except Exception as e:
            return f"⚠️ {par}: quiso subir el piso a {nuevo_piso:+.2f}% pero falló ({e})."

    mejor_piso_aplicable = None
    mejor_nivel_aplicable = None
    for nivel, piso_correspondiente in sorted(RATCHET_GANANCIA.items()):
        if pico_actual >= nivel:
            mejor_piso_aplicable = piso_correspondiente
            mejor_nivel_aplicable = nivel

    if mejor_piso_aplicable is None:
        return None
    if piso_actual_fijado is not None and piso_actual_fijado >= mejor_piso_aplicable:
        return None

    try:
        pionex_api.modificar_stop_loss(bu_order_id, mejor_piso_aplicable)
        db.actualizar_piso_ganancia(senal_id, mejor_piso_aplicable)
        db.guardar_checkpoint_v17(senal_id, par, "ganancia", mejor_nivel_aplicable, resultado_actual,
                                   decision=f"piso subido a {mejor_piso_aplicable}")
        return f"📈 {par}: tocó {mejor_nivel_aplicable}% — piso de Pionex subido a {mejor_piso_aplicable:+.2f}%."
    except Exception as e:
        return f"⚠️ {par}: quiso subir el piso a {mejor_piso_aplicable:+.2f}% pero falló ({e})."


def chequeo_rapido_ganancia_v17() -> list:
    """
    v17 — Chequeo MÁS FRECUENTE (cada 15seg vía main.py, no cada 1 min)
    SOLO para posiciones que ya cruzaron el primer nivel de ganancia
    (1.35%+) — no le pega a todas las posiciones, solo a las que
    necesitan que el piso ascendente reaccione rápido a un retroceso.
    """
    acciones = []
    abiertas = db.operaciones_abiertas_con_bu_order()
    for op in abiertas:
        mejor = op.get("mejor_resultado_pct")
        if mejor is None or mejor < 1.35:
            continue

        bu_order_id = op.get("bu_order_id")
        par = op["par"]
        try:
            desglose = pionex_api.calcular_resultado_desglosado(bu_order_id, par=par, capital_total_real=op.get("capital_asignado"))
            resultado_actual = desglose["total_pct"] if desglose else None
        except Exception:
            resultado_actual = None
        if resultado_actual is None:
            continue

        pico_actual = max(resultado_actual, mejor)
        if pico_actual > mejor:
            db.actualizar_mejor_resultado(op["id"], pico_actual)

        mensaje = _procesar_piso_ganancia(op, resultado_actual, pico_actual, bu_order_id)
        if mensaje:
            acciones.append(mensaje)
    return acciones


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

            # 10/08 (FIX): antes de confirmar el cierre (aunque esta_cerrada()
            # ya lo haya dicho DOS veces), cruzar contra la lista REAL de
            # grillas que Pionex tiene corriendo ahora mismo. Caso real que
            # motivó esto: INJUSDT quedó marcada como cerrada en nuestra
            # base por un falso positivo, mientras la posición real seguía
            # viva en Pionex durante días, invisible al stop-loss.
            reales = pionex_api.listar_grillas_abiertas()
            if reales is not None:
                ids_reales = {o.get("buOrderId") for o in reales if o.get("buOrderId")}
                if bu_order_id in ids_reales:
                    db.limpiar_cierre_pendiente(senal_id)
                    acciones.append(
                        f"🚨 {par}: esta_cerrada() dijo que cerró, pero SIGUE en la lista real de "
                        f"Pionex — descartado como falso cierre, no se tocó. REVISAR manualmente."
                    )
                    continue
            # Si reales es None (falló la consulta), seguimos con el cierre
            # normal — no bloquear el flujo por un fallo de esta verificación
            # extra, ya suficiente con las 2 confirmaciones de esta_cerrada().

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
        fallo_stop_loss = None
        try:
            desglose = pionex_api.calcular_resultado_desglosado(bu_order_id, par=par, capital_total_real=capital_real_op)
            if desglose is None:
                fallo_stop_loss = "Pionex respondió con datos incompletos"
        except Exception as e:
            desglose = None
            fallo_stop_loss = str(e)
        resultado_actual_sl = desglose["total_pct"] if desglose else None

        # 05/08 (FIX): antes CUALQUIER fallo acá (con o sin excepción) se
        # perdía en silencio — el stop-loss se salteaba ese ciclo sin
        # ningún rastro. Caso real: PORTALUSDT pasó -20% sin cerrar y sin
        # ningún log ni aviso. Ahora se loggea Y se avisa por Telegram.
        if fallo_stop_loss:
            print(f"⚠️ ERROR en chequeo de stop-loss de {par}: {fallo_stop_loss}")
            acciones.append(f"⚠️ {par}: falló el chequeo de stop-loss este ciclo ({fallo_stop_loss}) — REVISAR.")

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

        # v17 — Red de seguridad en -15% (techo absoluto incondicional).
        # El cierre real de este nivel lo protege el SL nativo de Pionex
        # (lossStop, fijado en -15% desde la apertura misma) — esto es un
        # BACKUP por si esa protección fallara por algún motivo (API,
        # slippage extremo, etc.), no el mecanismo principal.
        if resultado_actual_sl is not None and resultado_actual_sl <= STOP_LOSS_PCT:
            try:
                pionex_api.cerrar_grilla_futuros(bu_order_id, nota=f"Red de seguridad v17: {STOP_LOSS_PCT}%")
                db.cerrar_senal_automatica(senal_id, resultado_actual_sl, motivo="techo_absoluto_v17")
                acciones.append(
                    f"🛑 {par}: TECHO ABSOLUTO ejecutado a {resultado_actual_sl:+.2f}% "
                    f"(umbral {STOP_LOSS_PCT}%) — el SL nativo de Pionex debería haber actuado antes, REVISAR por qué no lo hizo."
                )
                continue
            except Exception as e:
                acciones.append(
                    f"⚠️ {par}: tocó el techo absoluto ({resultado_actual_sl:+.2f}%) pero falló el cierre real ({e}) — REVISAR YA."
                )

        # v17 — Checkpoints de pérdida con análisis técnico (-6/-9/-12%):
        # antes de llegar al techo de -15%, se evalúa si mantener o cerrar
        # según 4 factores técnicos, no solo la magnitud de la pérdida.
        if resultado_actual_sl is not None:
            mensaje_checkpoint = _procesar_checkpoint_perdida(op, resultado_actual_sl, bu_order_id)
            if mensaje_checkpoint:
                acciones.append(mensaje_checkpoint)
                if "cerrada en checkpoint" in mensaje_checkpoint:
                    continue

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
            resultado_actual = pionex_api.calcular_resultado_actual(bu_order_id, par=par, capital_total_real=capital_real_op)
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


def intentar_recalculo_diario() -> str:
    """
    01/08 — Interés compuesto: se llama desde main.py, tanto en un
    schedule fijo a las 00:01 ARG como en CADA ciclo de monitoreo (por si
    a las 00:01 había operaciones abiertas y se pospuso). Es seguro
    llamarla de más: si ya existe el registro de hoy, no hace nada.

    Solo recalcula si:
    1. Todavía no hay registro para el día de hoy (db.obtener_capital_diario() da None).
    2. No hay ninguna operación abierta ahora mismo.
    3. La consulta de balance real a Pionex responde con éxito (si falla,
       se reintenta en el próximo ciclo — nunca se asume $0).

    Devuelve un string para loggear/avisar por Telegram, o None si no
    correspondía hacer nada este ciclo.
    """
    if db.obtener_capital_diario() is not None:
        return None  # ya se calculó hoy

    posiciones_abiertas = len(db.operaciones_abiertas_con_bu_order())
    if posiciones_abiertas > 0:
        return None  # pospuesto — se reintenta solo en el próximo ciclo

    capital_real = pionex_api.obtener_balance_cuenta()
    if capital_real is None:
        return "⚠️ Recálculo diario de capital: falló la consulta de balance a Pionex, se reintenta en 1 min."

    tamano_objetivo = round(capital_real * PCT_CAPITAL_POR_OPERACION, 2)
    reserva_inicial = round(capital_real * RESERVA_RECUPERO_PCT, 2)
    db.guardar_capital_diario(capital_real, tamano_objetivo, reserva_inicial)

    return (
        f"💰 <b>Interés compuesto — nuevo día</b>\n"
        f"Capital real: USD {capital_real:.2f}\n"
        f"Tamaño por operación hoy: USD {tamano_objetivo:.2f} ({PCT_CAPITAL_POR_OPERACION*100:.1f}%)\n"
        f"Reserva de recupero: USD {reserva_inicial:.2f} ({RESERVA_RECUPERO_PCT*100:.0f}%)"
    )


def simular_seguimiento():
    """
    03/08 — Sigue el precio de mercado real de las señales SIMULADAS
    (calificaron score≥11 pero no consiguieron lugar real por el tope de
    2 posiciones / 1 apertura por ciclo). No arriesga capital real — usa
    una aproximación de movimiento de precio + apalancamiento (SIN el
    aporte extra de oscilación del grid, que en datos reales resultó ser
    chico frente al componente de tendencia — ver rejilla vs. tendencia).
    Se llama desde el mismo ciclo de 1 min que el resto del monitoreo.

    Cierra por TP (1.35%) o por STOP_LOSS_PCT (-20%), igual que una
    operación real — para poder comparar patrones de comportamiento en
    pérdida sin esperar semanas de datos reales (solo 2 posiciones a la
    vez limitan mucho la velocidad de aprendizaje real).
    """
    abiertas = db.operaciones_simuladas_abiertas()
    for op in abiertas:
        try:
            precio_actual = pionex_api.obtener_precio_mercado(op["par"])
        except Exception:
            precio_actual = None
        if precio_actual is None or not op["precio_entrada"]:
            continue

        cambio_pct = (precio_actual - op["precio_entrada"]) / op["precio_entrada"] * 100
        es_largo = op["direccion"] == "📈 LARGO"
        resultado_simulado = cambio_pct * (op["apal"] or 10) * (1 if es_largo else -1)

        db.actualizar_seguimiento_simulada(op["id"], resultado_simulado)

        if resultado_simulado >= 1.35:
            db.cerrar_senal_simulada(op["id"], resultado_simulado, motivo="tp_simulado")
        elif resultado_simulado <= STOP_LOSS_PCT:
            db.cerrar_senal_simulada(op["id"], resultado_simulado, motivo="stop_loss_simulado")


def chequear_huerfanas() -> list:
    """
    10/08 — Chequeo de reconciliación periódico: compara TODAS las grillas
    reales que Pionex tiene corriendo contra lo que nuestra base cree que
    está abierto (con bu_order_id). Si hay una posición real que no está
    en nuestro tracking, es una "huérfana" — probablemente un falso cierre
    viejo (antes del fix del 10/08 en el debounce) que quedó corriendo sin
    que nadie la monitoree. Se corre con poca frecuencia (no cada 1 min)
    para no sobrecargar la API — es un chequeo de seguridad, no crítico
    por segundo. Devuelve la lista de avisos generados (vacía si todo ok).
    """
    reales = pionex_api.listar_grillas_abiertas()
    if reales is None:
        return []  # no se pudo consultar, no reportar nada raro por un fallo de red

    ids_reales = {o.get("buOrderId"): o for o in reales if o.get("buOrderId")}
    ids_nuestros = {op["bu_order_id"] for op in db.operaciones_abiertas_con_bu_order()}

    huerfanas_ids = set(ids_reales.keys()) - ids_nuestros
    avisos = []
    for bu_id in huerfanas_ids:
        orden = ids_reales[bu_id]
        par = orden.get("symbol", "?")
        avisos.append(
            f"🚨 HUÉRFANA detectada: {par} (bu_order_id {bu_id}) está corriendo en Pionex "
            f"pero NO figura en nuestra base — quedó fuera de todo monitoreo (sin stop-loss). "
            f"Revisar y cerrar/registrar manualmente."
        )
    return avisos
