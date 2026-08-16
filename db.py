"""
db.py — Persistencia SQLite para JJ Cripto Bot
────────────────────────────────────────────────
Guarda en disco (Railway Volume) lo que antes vivía solo en RAM:
  - alertas_enviadas      (evitar duplicados)
  - señales_del_dia       (objetivo diario)
  - operaciones_abiertas  (recordatorios de cierre)
  - señales (histórico completo: cálculo del bot + datos reales de Pionex + resultado)

No modifica la lógica de análisis/señales. Solo agrega lectura/escritura.
"""
import sqlite3
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", "/data/bot.db")  # /data = punto de montaje del Volume en Railway
TZ_ARG = timezone(timedelta(hours=-3))


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrar_columnas_riesgo(cur):
    """
    Agrega columnas nuevas para automatización (capital, zona de riesgo,
    bu_order_id de Pionex) a la tabla senales si todavía no existen.
    SQLite no soporta 'ADD COLUMN IF NOT EXISTS', así que se ignora el
    error si la columna ya está.
    """
    columnas_nuevas = [
        ("bu_order_id", "TEXT"),
        ("capital_asignado", "REAL"),
        ("zona_riesgo", "TEXT DEFAULT 'verde'"),
        ("capital_apartado", "REAL DEFAULT 0"),
        ("razones", "TEXT"),  # detalle completo de indicadores confirmados, para análisis de patrones
        ("reabierta_de_id", "INTEGER"),  # v16: id de la señal original si esto es una reapertura (<5min)
        ("num_reapertura", "INTEGER DEFAULT 0"),  # v16: 0=original, 1=primera reapertura, 2=segunda (máx.)
        ("cierre_pendiente_desde", "TEXT"),  # 28/07: debounce de falsos cierres (ver monitorear_zonas_riesgo)
        ("aviso_10hs_enviado", "INTEGER DEFAULT 0"),  # 28/07: para no repetir el aviso informativo de 10hs en cada ciclo
        ("peor_resultado_pct", "REAL"),  # 28/07 (modo sombra): peor % alcanzado durante la vida de la operación — MAE real, mismas unidades que STOP_LOSS_PCT
        ("rejilla_pct", "REAL"),  # 29/07: última lectura de ganancia por oscilación de grid (Pionex "Ganancia de rejilla")
        ("tendencia_pct", "REAL"),  # 29/07: última lectura de ganancia por movimiento direccional (Pionex "PnL tend.")
        ("mejor_resultado_pct", "REAL"),  # 29/07 (modo sombra): mejor % alcanzado durante la vida de la operación — MFE (Maximum Favorable Excursion), complemento del MAE
        ("motivo_cierre", "TEXT"),  # 29/07: 'tp' / 'stop_loss' / lo que devuelva Pionex / 'desconocido' — para separar cierres por TP real de otros tipos
    ]
    for nombre, tipo in columnas_nuevas:
        try:
            cur.execute(f"ALTER TABLE senales ADD COLUMN {nombre} {tipo}")
        except Exception:
            pass  # ya existe


def _corregir_registrado_pionex_automaticas(cur):
    """
    Corrige señales que la automatización ya abrió (tienen bu_order_id)
    pero quedaron con registrado_pionex=0 por el bug de guardar_bu_order_id
    (ya corregido). Se ejecuta una sola vez por fila, es seguro repetir.
    """
    cur.execute("""
        UPDATE senales SET registrado_pionex = 1
        WHERE bu_order_id IS NOT NULL AND registrado_pionex = 0
    """)


def init_db():
    """Crea las tablas si no existen. Llamar una vez al iniciar el bot."""
    conn = _conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS senales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora_alerta TEXT NOT NULL,
            direccion TEXT,
            score INTEGER,
            preset_sugerido TEXT,

            -- Calculado por el bot
            precio_entrada REAL,
            apal_calculado INTEGER,
            rango_bajo_calc REAL,
            rango_alto_calc REAL,
            rango_pct_calc REAL,
            grillas_calc INTEGER,
            horas_1pct_calc REAL,
            ganancia_8h_calc REAL,

            -- Datos reales que el usuario pega desde Pionex (preset Balanceada)
            apal_pionex INTEGER,
            rango_bajo_pionex REAL,
            rango_alto_pionex REAL,
            grillas_pionex INTEGER,
            registrado_pionex INTEGER DEFAULT 0,

            -- Resultado real
            resultado_pct REAL,
            tiempo_real_min INTEGER,
            cerrado INTEGER DEFAULT 0,
            hora_cierre TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alertas_enviadas (
            clave TEXT PRIMARY KEY,
            creado TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS señales_del_dia (
            fecha TEXT NOT NULL,
            par TEXT,
            ganancia REAL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS operaciones_abiertas (
            clave TEXT PRIMARY KEY,
            par TEXT,
            direccion TEXT,
            entrada REAL,
            horas REAL,
            ganancia REAL,
            tp REAL,
            apertura TEXT,
            cierre_est TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS resumen_enviado (
            fecha TEXT PRIMARY KEY
        )
    """)

    # v16: log de los 6 filtros en modo sombra (multi-timeframe, ADX, volumen,
    # VWAP, CCI, OBV) — NO gatean ninguna señal todavía, solo se registra si
    # cada uno "hubiera aprobado" para poder armar el informe semanal.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sombra_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            senal_id INTEGER,
            par TEXT,
            fecha TEXT NOT NULL,
            direccion TEXT,
            multi_tf INTEGER,
            adx_gate INTEGER,
            volumen INTEGER,
            vwap INTEGER,
            cci INTEGER,
            obv INTEGER,
            creado TEXT
        )
    """)

    # v16: log del grid dinámico en modo sombra — registra cuándo una
    # operación abierta se acerca al borde de su rango y "hubiera" disparado
    # un ajuste vía adjustParams, SIN ejecutarlo todavía. Sirve para medir
    # con cuánta frecuencia hubiera ayudado, antes de activarlo real.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS grid_dinamico_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            senal_id INTEGER,
            par TEXT,
            fecha TEXT NOT NULL,
            precio_actual REAL,
            rango_bajo REAL,
            rango_alto REAL,
            lado TEXT,
            distancia_borde_pct REAL,
            rebote_confirmado INTEGER DEFAULT 0,
            hubiera_ajustado INTEGER,
            creado TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS pares_pausados (
            par TEXT PRIMARY KEY,
            motivo TEXT,
            desde TEXT,
            hasta TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)

    # 01/08 — Interés compuesto diario + reserva de recupero post-pérdida.
    # Un registro por día: se fija UNA vez a las 00:01 (con capital real,
    # vía API, solo si no hay operaciones abiertas) y queda fijo para
    # todas las operaciones de ese día.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS capital_diario (
            fecha TEXT PRIMARY KEY,
            capital_dia REAL NOT NULL,
            tamano_objetivo REAL NOT NULL,
            reserva_inicial REAL NOT NULL,
            reserva_restante REAL NOT NULL,
            creado TEXT NOT NULL
        )
    """)

    # 03/08 — Señales que calificaron (score≥11) pero NO consiguieron lugar
    # real (tope de 2 posiciones / 1 apertura por ciclo). Se les hace
    # seguimiento igual que a una real (MAE/MFE, cierre por TP/stop-loss),
    # aproximando el resultado con precio+apalancamiento (sin el aporte
    # extra de oscilación del grid) — sin arriesgar capital, para juntar
    # patrones de comportamiento más rápido de lo que daría esperar solo
    # las 2 operaciones reales simultáneas.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS senales_simuladas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora_apertura TEXT NOT NULL,
            precio_entrada REAL NOT NULL,
            apal INTEGER DEFAULT 10,
            score INTEGER,
            razones TEXT,
            motivo_no_apertura TEXT,
            cerrada INTEGER DEFAULT 0,
            resultado_pct REAL,
            peor_resultado_pct REAL,
            mejor_resultado_pct REAL,
            motivo_cierre TEXT,
            tiempo_real_min INTEGER,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)

    # 04/08 — Cinturón separado PAXG/BTC (fondeado directo en BTC). Log de
    # mercado: una foto de precio+indicadores de PAXG/BTC, BTC/USDT y oro
    # cada ciclo, SIEMPRE (haya o no señal) — es la base de datos que pidió
    # Juanjo para buscar parámetros óptimos más adelante, no solo para las
    # operaciones simuladas.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS paxg_mercado_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            precio_paxgbtc REAL,
            precio_btc_usdt REAL,
            precio_oro_usd REAL,
            rsi_paxgbtc REAL,
            adx_paxgbtc REAL,
            plus_di REAL,
            minus_di REAL,
            bb_upper REAL,
            bb_lower REAL,
            bb_mid REAL,
            ema9_paxgbtc REAL,
            ema21_paxgbtc REAL,
            estado_btc TEXT,
            rsi_oro REAL,
            tendencia_oro TEXT
        )
    """)

    # 04/08 — Las 24 combinaciones simuladas (3 señales A/B/C x 2 niveles
    # de riesgo x 4 objetivos de TP), corriendo en paralelo sobre el mismo
    # precio real de PAXG/BTC, sin capital real, para elegir la mejor
    # combinación al cabo de los 30 días de prueba.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS paxg_simulaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            combinacion TEXT NOT NULL,
            senal_tipo TEXT NOT NULL,
            riesgo TEXT NOT NULL,
            apalancamiento INTEGER NOT NULL,
            tp_objetivo_pct REAL NOT NULL,
            direccion TEXT NOT NULL,
            precio_entrada REAL NOT NULL,
            fecha TEXT NOT NULL,
            hora_apertura TEXT NOT NULL,
            cerrada INTEGER DEFAULT 0,
            resultado_pct REAL,
            peor_resultado_pct REAL,
            mejor_resultado_pct REAL,
            motivo_cierre TEXT,
            tiempo_min INTEGER,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)

    # 05/08 — Cinturón BingX (investigación, modo sombra puro, SIN operar
    # todavía): recolecta order book imbalance + indicadores de velas
    # cortas (1-5 min) + qué hizo el precio DESPUÉS, para poder calibrar
    # con datos propios qué umbral de desequilibrio predice mejor —
    # ninguna decisión de trading todavía, solo el dataset.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bingx_datos_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            precio REAL,
            imbalance REAL,
            rsi_1m REAL,
            rsi_5m REAL,
            vwap_1m REAL,
            precio_1min_despues REAL,
            precio_5min_despues REAL
        )
    """)

    # 10/08 — Cinturón BingX-martingala en modo sombra (sin capital real).
    # 2 variantes: A (imbalance fresco en cada trade) y B (guion fijo del
    # video, elegido según la dirección de la operación 1). Cada fila es
    # UNA SECUENCIA completa (hasta 6 operaciones), no una operación suelta.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bingx_martingala_secuencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            variante TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora_inicio TEXT NOT NULL,
            direccion_op1 TEXT NOT NULL,
            trade_actual INTEGER DEFAULT 1,
            apuesta_actual REAL NOT NULL,
            precio_entrada_trade REAL NOT NULL,
            hora_entrada_trade TEXT NOT NULL,
            direccion_trade_actual TEXT NOT NULL,
            perdido_acumulado REAL DEFAULT 0,
            cerrada INTEGER DEFAULT 0,
            resultado_usd REAL,
            motivo_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)

    # 11/08 — Capital PERSISTENTE por track (4: A_500, A_1000, B_500,
    # B_1000) — 2 formas de llevar la cuenta sobre los MISMOS resultados
    # de cada variante: "500" = capital corrido sin red de contención,
    # "1000" = 500 activos + 500 de reserva que repone a $500 automático
    # tras cada ruina (mientras haya reserva disponible).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bingx_martingala_capital (
            track TEXT PRIMARY KEY,
            capital_activo REAL NOT NULL,
            reserva_disponible REAL NOT NULL,
            veces_repuesto INTEGER DEFAULT 0,
            actualizado TEXT NOT NULL
        )
    """)

    _migrar_columnas_riesgo(cur)
    _corregir_registrado_pionex_automaticas(cur)

    conn.commit()
    conn.close()


# ── Pausa global (freno de emergencia vía Telegram) ──────────
def pausar_todo(motivo: str = ""):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_global', '1')")
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_motivo', ?)", (motivo,))
    conn.commit()
    conn.close()


def reanudar_todo():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_global', '0')")
    conn.commit()
    conn.close()


def esta_pausado_global() -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT valor FROM config WHERE clave = 'pausado_global'")
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] == "1")


# ── Señales (histórico completo) ────────────────────────────
def guardar_senal(r: dict, reabierta_de_id: int = None, num_reapertura: int = 0) -> int:
    """
    Guarda una señal recién generada por el bot. Devuelve el id de la fila.

    v16: reabierta_de_id / num_reapertura permiten trackear la cadena del
    sistema de reapertura (<5min) — reabierta_de_id apunta al id de la
    señal original (o de la reapertura anterior), num_reapertura cuenta
    0=original, 1=primera reapertura, 2=segunda (máximo confirmado).
    """
    import json
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    razones_json = json.dumps(r.get("razones", []), ensure_ascii=False)
    cur.execute("""
        INSERT INTO senales (
            par, fecha, hora_alerta, direccion, score, preset_sugerido,
            precio_entrada, apal_calculado, rango_bajo_calc, rango_alto_calc,
            rango_pct_calc, grillas_calc, horas_1pct_calc, ganancia_8h_calc, razones,
            reabierta_de_id, num_reapertura
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r["par"], ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
        r["direccion"], r["score"], r["preset"],
        r["precio"], r["apal"], r["rango_bajo"], r["rango_alto"],
        r["rango_pct"], r["grillas"], r["horas_1pct"], r["ganancia_8h"], razones_json,
        reabierta_de_id, num_reapertura,
    ))
    conn.commit()
    senal_id = cur.lastrowid
    conn.close()
    return senal_id


def ultima_senal_par(par: str):
    """Devuelve la señal más reciente sin cerrar para un par (para /registrar y /cerrar)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM senales
        WHERE par = ? AND cerrado = 0
        ORDER BY id DESC LIMIT 1
    """, (par,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def ultima_senal_par_cualquiera(par: str):
    """
    28/07 — Igual que ultima_senal_par, pero SIN filtrar por cerrado=0.
    Para /corregir: casos donde el bot marcó "cerrada" una operación que en
    realidad Pionex nunca cerró de verdad.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM senales WHERE par = ? ORDER BY id DESC LIMIT 1", (par,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def marcar_cierre_pendiente(senal_id: int):
    """
    28/07 — FIX: Pionex puede devolver un status "cerrado-like" TRANSITORIO
    durante una edición manual de la grilla (caso real: MOVE, 27/07 — el
    bu_order_id nunca cambió y seguía 'running'). Primera detección de
    posible cierre -> se marca pendiente, no se cierra al toque.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE senales SET cierre_pendiente_desde = ? WHERE id = ?",
                (datetime.now(TZ_ARG).isoformat(), senal_id))
    conn.commit()
    conn.close()


def limpiar_cierre_pendiente(senal_id: int):
    """Descarta una falsa alarma de cierre (el chequeo siguiente dio que sigue abierta)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE senales SET cierre_pendiente_desde = NULL WHERE id = ?", (senal_id,))
    conn.commit()
    conn.close()


def marcar_aviso_10hs_enviado(senal_id: int):
    """28/07: evita repetir el aviso informativo de 10hs en cada ciclo de monitoreo."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE senales SET aviso_10hs_enviado = 1 WHERE id = ?", (senal_id,))
    conn.commit()
    conn.close()


def actualizar_desglose_resultado(senal_id: int, rejilla_pct: float, tendencia_pct: float):
    """29/07: guarda la última lectura de rejilla vs. tendencia (se sobreescribe cada ciclo, no es histórico)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE senales SET rejilla_pct = ?, tendencia_pct = ? WHERE id = ?",
                (rejilla_pct, tendencia_pct, senal_id))
    conn.commit()
    conn.close()


def actualizar_mejor_resultado(senal_id: int, resultado_actual: float):
    """
    29/07 (modo sombra) — Registra el MEJOR % alcanzado durante la vida de
    la operación (MFE, Maximum Favorable Excursion) — complemento del MAE.
    Sirve para ver si el TP de 1.35% está "dejando plata en la mesa" (la
    operación llegó mucho más alto y después bajó) o si, al revés, muchas
    veces el precio se acerca al TP (ej. tu observación de 1.13%) y
    revierte a pérdida ANTES de tocarlo — el dato clave para decidir si
    conviene ajustar el TP. Reusa resultado_actual, no pide nada nuevo.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales
        SET mejor_resultado_pct = CASE
            WHEN mejor_resultado_pct IS NULL OR ? > mejor_resultado_pct THEN ?
            ELSE mejor_resultado_pct
        END
        WHERE id = ?
    """, (resultado_actual, resultado_actual, senal_id))
    conn.commit()
    conn.close()


def actualizar_peor_resultado(senal_id: int, resultado_actual: float):
    """
    28/07 (modo sombra) — Registra el PEOR % alcanzado durante la vida de
    la operación, en las mismas unidades que STOP_LOSS_PCT (no precio
    crudo). Es el dato que faltaba para calcular MAE real (Maximum Adverse
    Excursion, Sweeney) — reusa el resultado_actual que ya se calcula cada
    ciclo para el chequeo de stop-loss, no pide nada nuevo. NO cambia
    ningún comportamiento — solo guarda el mínimo histórico observado.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales
        SET peor_resultado_pct = CASE
            WHEN peor_resultado_pct IS NULL OR ? < peor_resultado_pct THEN ?
            ELSE peor_resultado_pct
        END
        WHERE id = ?
    """, (resultado_actual, resultado_actual, senal_id))
    conn.commit()
    conn.close()


def resumen_mfe(desde_fecha: str = None) -> dict:
    """
    29/07 — Informe de MFE real: de las operaciones cerradas con
    mejor_resultado_pct registrado, calcula la "eficiencia de captura"
    (resultado final / MFE) — si da bajo (ej. <50%), significa que el
    precio llegaba mucho más alto/lejos del TP y el resultado final se
    quedó corto, señal de que el TP podría estar mal calibrado. También
    separa cuántas operaciones llegaron cerca del TP (>=1.0%) y terminaron
    revirtiendo a pérdida — el patrón que Juanjo observó (pico ~1.13%).
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT resultado_pct, mejor_resultado_pct FROM senales WHERE cerrado = 1 AND mejor_resultado_pct IS NOT NULL"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return {"total": 0}

    con_mfe_positivo = [f for f in filas if f["mejor_resultado_pct"] and f["mejor_resultado_pct"] > 0]
    eficiencias = []
    for f in con_mfe_positivo:
        if f["resultado_pct"] is not None:
            eficiencias.append(f["resultado_pct"] / f["mejor_resultado_pct"])

    casi_tp_pero_perdio = [
        f for f in filas
        if f["mejor_resultado_pct"] is not None and f["mejor_resultado_pct"] >= 1.0
        and f["resultado_pct"] is not None and f["resultado_pct"] <= 0
    ]

    return {
        "total_con_dato": len(filas),
        "eficiencia_captura_promedio": round(sum(eficiencias) / len(eficiencias), 3) if eficiencias else None,
        "n_casi_tp_pero_termino_perdiendo": len(casi_tp_pero_perdio),
        "confiable": len(filas) >= 30,
    }


def resumen_por_motivo_cierre(desde_fecha: str = None) -> dict:
    """
    29/07 — Agrupa las operaciones cerradas por motivo_cierre ('tp',
    'stop_loss', otros) con su tiempo promedio y resultado promedio — para
    el análisis de "tiempo de llegada a TP" real, separado de otros tipos
    de cierre.
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT motivo_cierre, resultado_pct, tiempo_real_min, par FROM senales WHERE cerrado = 1"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return {"total": 0}

    por_motivo = {}
    for f in filas:
        m = f["motivo_cierre"] or "desconocido"
        por_motivo.setdefault(m, []).append(f)

    resumen = {}
    for motivo, lst in por_motivo.items():
        tiempos = [f["tiempo_real_min"] for f in lst if f["tiempo_real_min"] is not None]
        resultados = [f["resultado_pct"] for f in lst if f["resultado_pct"] is not None]
        resumen[motivo] = {
            "n": len(lst),
            "tiempo_prom_min": round(sum(tiempos) / len(tiempos), 1) if tiempos else None,
            "resultado_prom_pct": round(sum(resultados) / len(resultados), 3) if resultados else None,
        }
    return resumen


def resumen_mae(desde_fecha: str = None) -> dict:
    """
    28/07 — Informe de MAE real: de las operaciones YA CERRADAS con
    peor_resultado_pct registrado, separa ganadoras vs. perdedoras y
    muestra hasta cuánto cayeron las GANADORAS antes de recuperarse — ese
    es el dato clave para calibrar el stop-loss con evidencia propia (no
    con el -20% actual, elegido sin datos de trayectoria). Necesita
    mínimo ~50 operaciones para ser estadísticamente confiable (Sweeney).
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT resultado_pct, peor_resultado_pct FROM senales WHERE cerrado = 1 AND peor_resultado_pct IS NOT NULL"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = cur.fetchall()
    conn.close()
    if not filas:
        return {"total": 0}

    ganadoras = [dict(f) for f in filas if f["resultado_pct"] is not None and f["resultado_pct"] > 0]
    perdedoras = [dict(f) for f in filas if f["resultado_pct"] is not None and f["resultado_pct"] <= 0]

    mae_ganadoras = [g["peor_resultado_pct"] for g in ganadoras if g["peor_resultado_pct"] is not None]
    mae_perdedoras = [p["peor_resultado_pct"] for p in perdedoras if p["peor_resultado_pct"] is not None]

    return {
        "total_con_dato": len(filas),
        "n_ganadoras": len(ganadoras),
        "n_perdedoras": len(perdedoras),
        "mae_ganadoras_peor": min(mae_ganadoras) if mae_ganadoras else None,
        "mae_ganadoras_promedio": round(sum(mae_ganadoras) / len(mae_ganadoras), 2) if mae_ganadoras else None,
        "mae_perdedoras_promedio": round(sum(mae_perdedoras) / len(mae_perdedoras), 2) if mae_perdedoras else None,
        "confiable": len(filas) >= 50,
    }


def ultima_senal_par_cualquiera(par: str):
    """
    28/07 — Igual que ultima_senal_par, pero SIN filtrar por cerrado=0.
    Para /corregir: casos donde el bot marcó "cerrada" una operación que en
    realidad Pionex nunca cerró de verdad (ej. un "Restablecer P&L" manual
    en la app, que resetea el tracking del bu_order_id sin cerrar la
    posición real).
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM senales WHERE par = ? ORDER BY id DESC LIMIT 1
    """, (par,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def corregir_senal(senal_id: int, resultado_pct: float, reabrir: bool,
                    capital_asignado: float = None, bu_order_id: str = None,
                    tiempo_real_min: int = None, peor_resultado_pct: float = None):
    """
    28/07 — Corrige una señal que quedó con datos falsos (ej. cierre
    detectado por error). Si reabrir=True, la vuelve a marcar como abierta
    (cerrado=0, zona_riesgo='verde', capital_apartado=0) para que el
    monitoreo de riesgo la retome. Si reabrir=False, solo corrige el
    resultado_pct de una señal ya cerrada.

    11/08 (FIX): antes NO tocaba tiempo_real_min ni peor_resultado_pct —
    en el caso real de INJUSDT (5d9h reales, -53% de peor punto) esos 2
    datos se hubieran perdido justo cuando más valían para el análisis de
    MAE. Ahora son parámetros opcionales — se cargan solo si se pasan.
    """
    conn = _conn()
    cur = conn.cursor()
    if reabrir:
        cur.execute("""
            UPDATE senales SET resultado_pct = ?, cerrado = 0, zona_riesgo = 'verde',
                                capital_apartado = 0, cierre_pendiente_desde = NULL
            WHERE id = ?
        """, (resultado_pct, senal_id))
    else:
        cur.execute("UPDATE senales SET resultado_pct = ?, cerrado = 1 WHERE id = ?", (resultado_pct, senal_id))
    if capital_asignado is not None:
        cur.execute("UPDATE senales SET capital_asignado = ? WHERE id = ?", (capital_asignado, senal_id))
    if bu_order_id is not None:
        cur.execute("UPDATE senales SET bu_order_id = ? WHERE id = ?", (bu_order_id, senal_id))
    if tiempo_real_min is not None:
        cur.execute("UPDATE senales SET tiempo_real_min = ? WHERE id = ?", (tiempo_real_min, senal_id))
    if peor_resultado_pct is not None:
        cur.execute("""
            UPDATE senales
            SET peor_resultado_pct = CASE WHEN peor_resultado_pct IS NULL OR ? < peor_resultado_pct THEN ? ELSE peor_resultado_pct END
            WHERE id = ?
        """, (peor_resultado_pct, peor_resultado_pct, senal_id))
    conn.commit()
    conn.close()


def registrar_datos_pionex(senal_id: int, apal: int, rango_bajo: float, rango_alto: float, grillas: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales SET
            apal_pionex = ?, rango_bajo_pionex = ?, rango_alto_pionex = ?,
            grillas_pionex = ?, registrado_pionex = 1
        WHERE id = ?
    """, (apal, rango_bajo, rango_alto, grillas, senal_id))
    conn.commit()
    conn.close()


def cerrar_senal(senal_id: int, resultado_pct: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT hora_alerta, fecha FROM senales WHERE id = ?", (senal_id,))
    row = cur.fetchone()
    tiempo_real_min = None
    if row:
        try:
            apertura = datetime.strptime(f"{row['fecha']} {row['hora_alerta']}", "%Y%m%d %H:%M").replace(tzinfo=TZ_ARG)
            tiempo_real_min = int((datetime.now(TZ_ARG) - apertura).total_seconds() / 60)
        except Exception:
            pass
    cur.execute("""
        UPDATE senales SET
            resultado_pct = ?, tiempo_real_min = ?, cerrado = 1, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, tiempo_real_min, datetime.now(TZ_ARG).strftime("%H:%M"), senal_id))
    conn.commit()
    conn.close()


def stats_comparacion():
    """
    Compara, entre las señales cerradas y con datos de Pionex registrados:
    - resultado promedio cuando rango_pct calculado es MÁS ANGOSTO que el de Pionex
    - resultado promedio cuando es MÁS ANCHO
    - resultado promedio cuando coinciden (±10%)
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT *,
            (rango_alto_calc - rango_bajo_calc) / precio_entrada * 100 AS rango_pct_calc_real,
            (rango_alto_pionex - rango_bajo_pionex) / precio_entrada * 100 AS rango_pct_pionex
        FROM senales
        WHERE cerrado = 1 AND registrado_pionex = 1
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    mas_angosto, mas_ancho, similar = [], [], []
    for r in rows:
        diff = r["rango_pct_calc_real"] - r["rango_pct_pionex"]
        if abs(diff) <= r["rango_pct_pionex"] * 0.10:
            similar.append(r)
        elif diff < 0:
            mas_angosto.append(r)
        else:
            mas_ancho.append(r)

    def _resumen(lst):
        if not lst:
            return {"n": 0, "prom": None}
        return {"n": len(lst), "prom": round(sum(x["resultado_pct"] for x in lst) / len(lst), 2)}

    return {
        "total": len(rows),
        "bot_mas_angosto_que_pionex": _resumen(mas_angosto),
        "bot_mas_ancho_que_pionex": _resumen(mas_ancho),
        "similar": _resumen(similar),
    }


# ── Alertas enviadas (anti-duplicado) ───────────────────────
def alerta_existe(clave: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM alertas_enviadas WHERE clave = ?", (clave,))
    existe = cur.fetchone() is not None
    conn.close()
    return existe


def marcar_alerta_enviada(clave: str):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO alertas_enviadas (clave, creado) VALUES (?,?)",
                (clave, datetime.now(TZ_ARG).isoformat()))
    conn.commit()
    conn.close()


# ── Señales del día (objetivo diario) ───────────────────────
def registrar_ganancia_dia(par: str, ganancia: float):
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("INSERT INTO señales_del_dia (fecha, par, ganancia) VALUES (?,?,?)", (hoy, par, ganancia))
    conn.commit()
    conn.close()


def obj_diario_db(objetivo_diario: float):
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("SELECT COUNT(*), COALESCE(SUM(ganancia),0) FROM señales_del_dia WHERE fecha = ?", (hoy,))
    n, total = cur.fetchone()
    conn.close()
    total = round(total, 2)
    return {"n": n, "total": total, "ok": total >= objetivo_diario,
            "faltan": round(max(0, objetivo_diario - total), 2)}


def obj_diario_real_db(objetivo_diario: float, capital_total: float = 1000.0) -> dict:
    """
    % de ganancia REAL del día sobre el capital total — reemplaza a
    obj_diario_db(), que sumaba 1.35% fijo por cada alerta ENVIADA sin
    importar si la operación ganó, perdió, o cuánto capital usó de verdad.

    Esta versión solo cuenta operaciones CERRADAS, con su resultado_pct
    real, ponderado por el capital que realmente usaron (capital_asignado
    si está disponible —automatizadas—, o 9% del total como estimación
    para operaciones manuales viejas que no guardaron ese dato).
    """
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("""
        SELECT resultado_pct, capital_asignado FROM senales
        WHERE fecha = ? AND cerrado = 1 AND resultado_pct IS NOT NULL
    """, (hoy,))
    rows = cur.fetchall()
    conn.close()

    ganancia_usd = 0.0
    for resultado_pct, capital_asignado in rows:
        capital_op = capital_asignado if capital_asignado else capital_total * 0.09
        ganancia_usd += (resultado_pct / 100) * capital_op

    total_pct = round((ganancia_usd / capital_total) * 100, 2) if capital_total > 0 else 0.0
    return {
        "n": len(rows),
        "total": total_pct,
        "ok": total_pct >= objetivo_diario,
        "faltan": round(max(0, objetivo_diario - total_pct), 2),
    }


# ── Operaciones abiertas (recordatorio de cierre) ───────────
def guardar_operacion_abierta(clave: str, par: str, direccion: str, entrada: float,
                                horas: float, ganancia: float, tp: float, apertura: str, cierre_est: str):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO operaciones_abiertas
        (clave, par, direccion, entrada, horas, ganancia, tp, apertura, cierre_est)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (clave, par, direccion, entrada, horas, ganancia, tp, apertura, cierre_est))
    conn.commit()
    conn.close()


def operaciones_abiertas_pendientes():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM operaciones_abiertas")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def borrar_operacion_abierta(clave: str):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM operaciones_abiertas WHERE clave = ?", (clave,))
    conn.commit()
    conn.close()


# ── Resúmenes de rendimiento (diario/semanal/mensual) ───────
def _calcular_resumen(rows: list, capital_periodo: float = None) -> dict:
    """
    Calcula métricas comunes dado una lista de filas de senales.

    05/08 (FIX): "gan_total" antes sumaba directo resultado_pct de cada
    operación — pero cada resultado_pct ya es % relativo al capital de ESA
    operación puntual, no al capital total. Sumarlos no daba un % real de
    cartera. Ahora, si se pasa capital_periodo (el capital real de inicio
    del período, mismo dato que usa /rendimiento), se calcula bien: USD
    total del período / capital real. Si no se pasa (fallback, por si
    todavía no hay capital_diario disponible), usa el cálculo viejo.
    """
    if not rows:
        return {"n": 0, "n_pos": 0, "n_neg": 0, "n_abiertas": 0,
                "gan_total": 0, "gan_prom": 0, "win_rate": 0,
                "gan_total_sin": 0, "gan_prom_sin": 0, "win_rate_sin": 0}

    cerradas = [r for r in rows if r["cerrado"] == 1 and r["resultado_pct"] is not None]
    abiertas = [r for r in rows if r["cerrado"] == 0]
    positivas = [r for r in cerradas if r["resultado_pct"] > 0]
    negativas = [r for r in cerradas if r["resultado_pct"] <= 0]

    # Con estancadas (todas las cerradas)
    gan_prom = sum(r["resultado_pct"] for r in cerradas) / len(cerradas) if cerradas else 0
    win_rate = len(positivas) / len(cerradas) * 100 if cerradas else 0

    # Sin estancadas (solo las que cerraron en <= 12 horas, el umbral confirmado)
    rapidas = [r for r in cerradas if r["tiempo_real_min"] is not None and r["tiempo_real_min"] <= 720]
    gan_prom_sin = sum(r["resultado_pct"] for r in rapidas) / len(rapidas) if rapidas else 0
    win_rate_sin = sum(1 for r in rapidas if r["resultado_pct"] > 0) / len(rapidas) * 100 if rapidas else 0

    if capital_periodo:
        gan_total_usd = sum(r["resultado_pct"] * (r.get("capital_asignado") or 0) / 100 for r in cerradas)
        gan_total = round(gan_total_usd / capital_periodo * 100, 2)
        gan_total_sin_usd = sum(r["resultado_pct"] * (r.get("capital_asignado") or 0) / 100 for r in rapidas)
        gan_total_sin = round(gan_total_sin_usd / capital_periodo * 100, 2)
    else:
        # Fallback (comportamiento viejo) — solo si no hay capital real disponible
        gan_total = round(sum(r["resultado_pct"] for r in cerradas), 2)
        gan_total_sin = round(sum(r["resultado_pct"] for r in rapidas), 2)

    return {
        "n": len(cerradas),
        "n_pos": len(positivas),
        "n_neg": len(negativas),
        "n_abiertas": len(abiertas),
        "n_rapidas": len(rapidas),
        "gan_total": gan_total,
        "gan_prom": round(gan_prom, 2),
        "win_rate": round(win_rate, 1),
        "gan_total_sin": gan_total_sin,
        "gan_prom_sin": round(gan_prom_sin, 2),
        "win_rate_sin": round(win_rate_sin, 1),
        "mejor": round(max((r["resultado_pct"] for r in cerradas), default=0), 2),
        "peor": round(min((r["resultado_pct"] for r in cerradas), default=0), 2),
    }


def resumen_diario(fecha: str = None) -> dict:
    """Resumen de operaciones de un día. Si no se pasa fecha, usa hoy ARG."""
    conn = _conn()
    cur = conn.cursor()
    if fecha is None:
        fecha = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("SELECT * FROM senales WHERE fecha = ?", (fecha,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    capital_periodo = capital_inicio_periodo(fecha)
    resultado = _calcular_resumen(rows, capital_periodo)
    resultado["fecha"] = fecha
    return resultado


def resumen_semanal() -> dict:
    """Resumen de los últimos 7 días."""
    conn = _conn()
    cur = conn.cursor()
    desde = (datetime.now(TZ_ARG) - timedelta(days=7)).strftime("%Y%m%d")
    cur.execute("SELECT * FROM senales WHERE fecha >= ?", (desde,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    capital_periodo = capital_inicio_periodo(desde)
    resultado = _calcular_resumen(rows, capital_periodo)
    resultado["periodo"] = f"{desde} → hoy"
    return resultado


def resumen_mensual() -> dict:
    """Resumen de los últimos 30 días."""
    conn = _conn()
    cur = conn.cursor()
    desde = (datetime.now(TZ_ARG) - timedelta(days=30)).strftime("%Y%m%d")
    cur.execute("SELECT * FROM senales WHERE fecha >= ?", (desde,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    capital_periodo = capital_inicio_periodo(desde)
    resultado = _calcular_resumen(rows, capital_periodo)
    resultado["periodo"] = f"{desde} → hoy"
    return resultado


def resumen_por_dia_detalle() -> list:
    """Retorna una fila por cada día con datos, útil para ver tendencia."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT fecha,
               COUNT(*) as total,
               SUM(CASE WHEN cerrado=1 AND resultado_pct > 0 THEN 1 ELSE 0 END) as positivas,
               SUM(CASE WHEN cerrado=1 AND resultado_pct <= 0 THEN 1 ELSE 0 END) as negativas,
               SUM(CASE WHEN cerrado=0 THEN 1 ELSE 0 END) as abiertas,
               ROUND(SUM(CASE WHEN cerrado=1 THEN COALESCE(resultado_pct,0) ELSE 0 END), 2) as gan_total,
               ROUND(AVG(CASE WHEN cerrado=1 THEN resultado_pct ELSE NULL END), 2) as gan_prom
        FROM senales
        GROUP BY fecha
        ORDER BY fecha DESC
        LIMIT 30
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def registrar_ganancia_dia_real(par: str, ganancia_pct_pionex: float):
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("INSERT INTO señales_del_dia (fecha, par, ganancia) VALUES (?,?,?)",
                (hoy, par, ganancia_pct_pionex))
    conn.commit()
    conn.close()


def pausar_par(par: str, motivo: str, horas: int = 24):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    hasta = ahora + timedelta(hours=horas)
    try:
        cur.execute("""
            INSERT OR REPLACE INTO pares_pausados (par, motivo, desde, hasta)
            VALUES (?,?,?,?)
        """, (par, motivo, ahora.isoformat(), hasta.isoformat()))
        conn.commit()
    except Exception:
        pass
    conn.close()


def par_esta_pausado(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT hasta FROM pares_pausados WHERE par = ?", (par,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return False
        hasta = datetime.fromisoformat(row["hasta"])
        if datetime.now(TZ_ARG) >= hasta:
            despausar_par(par)
            return False
        return True
    except Exception:
        conn.close()
        return False


def despausar_par(par: str):
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM pares_pausados WHERE par = ?", (par,))
        conn.commit()
    except Exception:
        pass
    conn.close()


def pares_pausados_activos() -> list:
    conn = _conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM pares_pausados")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return [r for r in rows if par_esta_pausado(r["par"])]
    except Exception:
        conn.close()
        return []


def ultimos_resultados_par(par: str, n: int = 2) -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT resultado_pct FROM senales
        WHERE par = ? AND cerrado = 1 AND resultado_pct IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (par, n))
    rows = [r["resultado_pct"] for r in cur.fetchall()]
    conn.close()
    return rows


def operaciones_estancadas(horas_limite: float = 12.0) -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM senales WHERE cerrado = 0 AND registrado_pionex = 1")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    ahora = datetime.now(TZ_ARG)
    estancadas = []
    for r in rows:
        try:
            apertura = datetime.strptime(
                f"{r['fecha']} {r['hora_alerta']}", "%Y%m%d %H:%M"
            ).replace(tzinfo=TZ_ARG)
            horas_abierta = (ahora - apertura).total_seconds() / 3600
            if horas_abierta >= horas_limite:
                r["horas_abierta"] = round(horas_abierta, 1)
                estancadas.append(r)
        except Exception:
            continue
    return estancadas


# ── Resumen matutino (evitar duplicado por día) ─────────────
def resumen_ya_enviado(fecha: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM resumen_enviado WHERE fecha = ?", (fecha,))
    existe = cur.fetchone() is not None
    conn.close()
    return existe


def marcar_resumen_enviado(fecha: str):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO resumen_enviado (fecha) VALUES (?)", (fecha,))
    conn.commit()
    conn.close()


# ── Capital y zona de riesgo (automatización Pionex) ────────
def guardar_bu_order_id(senal_id: int, bu_order_id: str, capital_asignado: float):
    """
    Guarda el ID del bot de Pionex y el capital comprometido (inversión +
    margen) tras crear la grilla automática real. Marca registrado_pionex=1
    porque ya tenemos el dato real de Pionex (bu_order_id) — mejor que un
    /registrar manual — así /pendientes no la marca como "falta registrar".
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales SET bu_order_id = ?, capital_asignado = ?, registrado_pionex = 1
        WHERE id = ?
    """, (bu_order_id, capital_asignado, senal_id))
    conn.commit()
    conn.close()


def actualizar_zona_riesgo(senal_id: int, zona: str, capital_apartado: float = 0):
    """
    Actualiza la zona de riesgo (verde/amarilla/roja) de una operación abierta,
    y cuánto capital extra tiene apartado (5% en amarilla, se libera en verde).
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales SET zona_riesgo = ?, capital_apartado = ?
        WHERE id = ?
    """, (zona, capital_apartado, senal_id))
    conn.commit()
    conn.close()


def operaciones_abiertas_con_bu_order() -> list:
    """Operaciones abiertas y ya registradas en Pionex, con bu_order_id para monitorear."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM senales
        WHERE cerrado = 0 AND registrado_pionex = 1 AND bu_order_id IS NOT NULL
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def cerrar_senal_automatica(senal_id: int, resultado_pct: float, motivo: str = None):
    """
    Marca una señal como cerrada cuando la automatización DETECTA (vía API)
    que Pionex ya cerró la grilla — libera el capital comprometido para que
    vuelva a estar disponible para nuevas operaciones. Sin esto, las
    operaciones cerradas quedaban marcadas como abiertas para siempre,
    bloqueando capital fantasma (bug encontrado y corregido 12/07).

    CORREGIDO 20/07: antes no calculaba tiempo_real_min ni hora_cierre
    (a diferencia de cerrar_senal, la función del /cerrar manual) — se
    perdía el dato de duración real para TODOS los cierres automáticos,
    justo lo que se usa para medir velocidad de rotación.

    29/07: agregado motivo ('tp' / 'stop_loss' / lo que reporte Pionex /
    'desconocido') — para poder separar tiempo-hasta-TP real de otros
    tipos de cierre en el análisis de selección entre señales simultáneas.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT hora_alerta, fecha FROM senales WHERE id = ?", (senal_id,))
    row = cur.fetchone()
    tiempo_real_min = None
    if row:
        try:
            apertura = datetime.strptime(f"{row['fecha']} {row['hora_alerta']}", "%Y%m%d %H:%M").replace(tzinfo=TZ_ARG)
            tiempo_real_min = int((datetime.now(TZ_ARG) - apertura).total_seconds() / 60)
        except Exception:
            pass
    cur.execute("""
        UPDATE senales SET cerrado = 1, resultado_pct = ?, tiempo_real_min = ?, hora_cierre = ?,
                            cierre_pendiente_desde = NULL, motivo_cierre = ?
        WHERE id = ?
    """, (resultado_pct, tiempo_real_min, datetime.now(TZ_ARG).strftime("%H:%M"), motivo, senal_id))
    conn.commit()
    conn.close()


def contar_atascadas_riesgo() -> int:
    """
    Cuenta operaciones abiertas en zona amarilla o roja (NO por tiempo).
    Este es el número que activa el modo restrictivo al llegar a 3.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM senales
        WHERE cerrado = 0 AND zona_riesgo IN ('amarilla', 'roja')
    """)
    count = cur.fetchone()[0]
    conn.close()
    return count


def capital_comprometido_total() -> float:
    """Suma del capital ya asignado a operaciones abiertas (9% c/u)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(capital_asignado), 0) FROM senales
        WHERE cerrado = 0
    """)
    total = cur.fetchone()[0]
    conn.close()
    return float(total)


def capital_apartado_total() -> float:
    """Suma del capital apartado (5% extra) por operaciones en zona amarilla/roja."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(capital_apartado), 0) FROM senales
        WHERE cerrado = 0 AND zona_riesgo IN ('amarilla', 'roja')
    """)
    total = cur.fetchone()[0]
    conn.close()
    return float(total)


def resultado_acumulado_usd_hoy() -> float:
    """
    01/08 — Suma en USD (no %) de todas las operaciones YA CERRADAS hoy.
    Negativo si el día viene en pérdida — se usa para decidir si corresponde
    completar con la reserva de recupero al abrir la próxima operación.
    """
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("""
        SELECT COALESCE(SUM(resultado_pct * capital_asignado / 100), 0)
        FROM senales WHERE fecha = ? AND cerrado = 1
    """, (hoy,))
    total = cur.fetchone()[0]
    conn.close()
    return round(total, 2)


def guardar_capital_diario(capital_dia: float, tamano_objetivo: float, reserva_inicial: float):
    """
    01/08 — Fija los 3 números del día (capital, tamaño objetivo por
    operación, reserva de recupero) — se llama UNA vez a las 00:01, solo
    si no hay operaciones abiertas. reserva_restante arranca igual a
    reserva_inicial y se va gastando durante el día si hace falta.
    """
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("""
        INSERT OR REPLACE INTO capital_diario
            (fecha, capital_dia, tamano_objetivo, reserva_inicial, reserva_restante, creado)
        VALUES (?,?,?,?,?,?)
    """, (hoy, capital_dia, tamano_objetivo, reserva_inicial, reserva_inicial, datetime.now(TZ_ARG).isoformat()))
    conn.commit()
    conn.close()


def obtener_capital_diario():
    """
    01/08 — Devuelve el registro de HOY (dict) o None si todavía no se
    ejecutó el recálculo diario (ej. porque había operaciones abiertas a
    las 00:01 y se pospuso). Ausencia de registro = recálculo pendiente.
    """
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("SELECT * FROM capital_diario WHERE fecha = ?", (hoy,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def descontar_reserva_diaria(monto: float):
    """01/08 — Descuenta uso de la reserva de recupero del día (nunca queda negativa)."""
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("""
        UPDATE capital_diario SET reserva_restante = MAX(0, reserva_restante - ?)
        WHERE fecha = ?
    """, (monto, hoy))
    conn.commit()
    conn.close()


def capital_inicio_periodo(fecha_desde: str):
    """
    03/08 — Capital de referencia al INICIO de un período (para calcular
    rendimiento %). Busca el registro de capital_diario de esa fecha
    exacta; si no existe (ej. el bot no estaba corriendo interés
    compuesto todavía ese día), usa el primero disponible DESPUÉS de esa
    fecha, como mejor aproximación posible.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT capital_dia FROM capital_diario WHERE fecha = ?", (fecha_desde,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row["capital_dia"]
    cur.execute("""
        SELECT capital_dia FROM capital_diario WHERE fecha >= ? ORDER BY fecha ASC LIMIT 1
    """, (fecha_desde,))
    row = cur.fetchone()
    conn.close()
    return row["capital_dia"] if row else None


def resultado_usd_desde(fecha_desde: str) -> dict:
    """
    03/08 — Resultado en USD (y conteo de ganadas/perdidas) de todas las
    operaciones cerradas desde una fecha en adelante (inclusive).
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT resultado_pct, capital_asignado FROM senales
        WHERE fecha >= ? AND cerrado = 1
    """, (fecha_desde,))
    filas = cur.fetchall()
    conn.close()
    total_usd = 0.0
    ganadas = 0
    perdidas = 0
    for f in filas:
        if f["resultado_pct"] is None or f["capital_asignado"] is None:
            continue
        usd = f["resultado_pct"] * f["capital_asignado"] / 100
        total_usd += usd
        if f["resultado_pct"] > 0:
            ganadas += 1
        elif f["resultado_pct"] < 0:
            perdidas += 1
    return {"total_usd": round(total_usd, 2), "ganadas": ganadas, "perdidas": perdidas, "total_ops": len(filas)}


def guardar_senal_simulada(r: dict, motivo_no_apertura: str = None) -> int:
    """
    03/08 — Guarda una señal que calificó (score≥11) pero no consiguió
    lugar real. Se le hace seguimiento aparte de las reales, sin
    comprometer capital.
    """
    import json
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    razones_json = json.dumps(r.get("razones", []), ensure_ascii=False)
    cur.execute("""
        INSERT INTO senales_simuladas
            (par, direccion, fecha, hora_apertura, precio_entrada, apal, score, razones, motivo_no_apertura, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        r["par"], r["direccion"], ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
        r["precio"], r.get("apal", 10), r["score"], razones_json, motivo_no_apertura, ahora.isoformat(),
    ))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def operaciones_simuladas_abiertas() -> list:
    """03/08 — Todas las señales simuladas todavía sin cerrar."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM senales_simuladas WHERE cerrada = 0")
    filas = [dict(r) for r in cur.fetchall()]
    conn.close()
    return filas


def actualizar_seguimiento_simulada(sim_id: int, resultado_actual: float):
    """03/08 — Actualiza MAE/MFE de una señal simulada (mismo patrón que las reales)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales_simuladas
        SET peor_resultado_pct = CASE WHEN peor_resultado_pct IS NULL OR ? < peor_resultado_pct THEN ? ELSE peor_resultado_pct END,
            mejor_resultado_pct = CASE WHEN mejor_resultado_pct IS NULL OR ? > mejor_resultado_pct THEN ? ELSE mejor_resultado_pct END
        WHERE id = ?
    """, (resultado_actual, resultado_actual, resultado_actual, resultado_actual, sim_id))
    conn.commit()
    conn.close()


def cerrar_senal_simulada(sim_id: int, resultado_pct: float, motivo: str):
    """03/08 — Cierra una señal simulada (por TP o stop-loss simulado)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT hora_apertura, fecha FROM senales_simuladas WHERE id = ?", (sim_id,))
    row = cur.fetchone()
    tiempo_real_min = None
    if row:
        try:
            apertura = datetime.strptime(f"{row['fecha']} {row['hora_apertura']}", "%Y%m%d %H:%M").replace(tzinfo=TZ_ARG)
            tiempo_real_min = int((datetime.now(TZ_ARG) - apertura).total_seconds() / 60)
        except Exception:
            pass
    cur.execute("""
        UPDATE senales_simuladas
        SET cerrada = 1, resultado_pct = ?, motivo_cierre = ?, tiempo_real_min = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, tiempo_real_min, datetime.now(TZ_ARG).strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


def resumen_simuladas(desde_fecha: str = None) -> dict:
    """
    03/08 — Informe agregado de las señales simuladas: cuántas, win rate,
    MAE/MFE promedio — para comparar el comportamiento de las señales que
    NO consiguieron lugar real contra las que sí (útil para ver si el
    tope de 2 posiciones nos está haciendo perder buenas señales).
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM senales_simuladas WHERE cerrada = 1"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return {"total": 0}

    ganadas = [f for f in filas if f["resultado_pct"] is not None and f["resultado_pct"] > 0]
    perdidas = [f for f in filas if f["resultado_pct"] is not None and f["resultado_pct"] <= 0]
    mae_vals = [f["peor_resultado_pct"] for f in filas if f["peor_resultado_pct"] is not None]
    mfe_vals = [f["mejor_resultado_pct"] for f in filas if f["mejor_resultado_pct"] is not None]

    return {
        "total_cerradas": len(filas),
        "ganadas": len(ganadas),
        "perdidas": len(perdidas),
        "win_rate_pct": round(len(ganadas) / len(filas) * 100, 1) if filas else None,
        "mae_promedio": round(sum(mae_vals) / len(mae_vals), 2) if mae_vals else None,
        "mfe_promedio": round(sum(mfe_vals) / len(mfe_vals), 2) if mfe_vals else None,
    }


def resumen_rendimiento() -> dict:
    """
    03/08 — Arma el resumen de rendimiento diario/semanal/mensual, cada uno
    en % sobre el capital REAL de inicio de ESE período (no el total fijo).
    Diario: desde hoy 00:00. Semanal: desde el lunes de esta semana.
    Mensual: desde el día 1 de este mes. Todas las fechas en huso ARG.
    """
    ahora = datetime.now(TZ_ARG)
    hoy = ahora.strftime("%Y%m%d")
    lunes = (ahora - timedelta(days=ahora.weekday())).strftime("%Y%m%d")
    dia1_mes = ahora.replace(day=1).strftime("%Y%m%d")

    periodos = {}
    for nombre, fecha_desde in (("diario", hoy), ("semanal", lunes), ("mensual", dia1_mes)):
        capital_inicio = capital_inicio_periodo(fecha_desde)
        resultado = resultado_usd_desde(fecha_desde)
        pct = round((resultado["total_usd"] / capital_inicio) * 100, 2) if capital_inicio else None
        periodos[nombre] = {
            "fecha_desde": fecha_desde,
            "capital_inicio": capital_inicio,
            "resultado_usd": resultado["total_usd"],
            "resultado_pct": pct,
            "ganadas": resultado["ganadas"],
            "perdidas": resultado["perdidas"],
        }
    return periodos



def ganancia_hoy_pct(capital_total: float) -> float:
    """% de ganancia ya logrado hoy sobre el capital total (para el objetivo del 3%)."""
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("""
        SELECT COALESCE(SUM(resultado_pct * capital_asignado / 100), 0)
        FROM senales WHERE fecha = ? AND cerrado = 1
    """, (hoy,))
    ganancia_usd = cur.fetchone()[0]
    conn.close()
    if capital_total <= 0:
        return 0.0
    return round((ganancia_usd / capital_total) * 100, 2)


# ── Modo sombra v16 (multi-timeframe, ADX, volumen, VWAP) ───
def guardar_log_sombra(senal_id: int, par: str, direccion: str,
                        multi_tf: bool, adx_gate: bool, volumen: bool, vwap: bool,
                        cci: bool = None, obv: bool = None):
    """
    Guarda si cada uno de los filtros en modo sombra HUBIERA aprobado esta
    señal — no bloquea ni modifica la señal en sí. Se usa para armar el
    informe semanal (resumen_sombra) y decidir cuáles conviene activar
    como filtro duro. cci/obv son opcionales (None si no se calcularon).
    """
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO sombra_log (senal_id, par, fecha, direccion, multi_tf, adx_gate, volumen, vwap, cci, obv, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        senal_id, par, ahora.strftime("%Y%m%d"), direccion,
        int(multi_tf), int(adx_gate), int(volumen), int(vwap),
        None if cci is None else int(cci), None if obv is None else int(obv),
        ahora.isoformat(),
    ))
    conn.commit()
    conn.close()


def extremos_precio_grid_dinamico(senal_id: int):
    """
    28/07 — Devuelve (mínimo, máximo) de precio_actual ya logueado para
    esta operación (histórico acumulado en grid_dinamico_log). Se usa para
    detectar "rebote confirmado" al estilo DGT (arXiv 2506.11921): no
    ajustar apenas toca el borde, esperar a que rebote/retroceda un % desde
    el extremo alcanzado antes de considerar el ajuste.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT MIN(precio_actual), MAX(precio_actual) FROM grid_dinamico_log WHERE senal_id = ?
    """, (senal_id,))
    row = cur.fetchone()
    conn.close()
    minimo = row[0] if row and row[0] is not None else None
    maximo = row[1] if row and row[1] is not None else None
    return minimo, maximo


def guardar_log_grid_dinamico(senal_id: int, par: str, precio_actual: float,
                               rango_bajo: float, rango_alto: float,
                               lado: str, distancia_borde_pct: float,
                               rebote_confirmado: bool, hubiera_ajustado: bool):
    """
    v16: log en modo sombra del grid dinámico — se llama desde
    monitorear_zonas_riesgo() reusando el precio ya consultado ahí (no pide
    uno nuevo). Registra qué tan cerca estuvo el precio del borde del rango,
    si hubo un REBOTE CONFIRMADO desde el mínimo/máximo histórico (regla
    real del paper DGT, no solo "cerca del borde"), y si con eso se HUBIERA
    disparado un ajuste vía adjustParams, sin ejecutarlo.
    """
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO grid_dinamico_log
            (senal_id, par, fecha, precio_actual, rango_bajo, rango_alto, lado,
             distancia_borde_pct, rebote_confirmado, hubiera_ajustado, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        senal_id, par, ahora.strftime("%Y%m%d"), precio_actual, rango_bajo, rango_alto,
        lado, distancia_borde_pct, int(rebote_confirmado), int(hubiera_ajustado), ahora.isoformat(),
    ))
    conn.commit()
    conn.close()


def resumen_sombra(desde_fecha: str = None) -> dict:
    """
    Informe de los filtros en modo sombra: para cada uno, cuántas señales
    aprobó/rechazó, y de las que ya cerraron (con resultado real), el
    resultado promedio separado por aprobó=Sí vs aprobó=No. Sirve para
    decidir con datos reales cuáles conviene activar como filtro duro.
    Si no se pasa desde_fecha, usa todo el historial disponible.
    """
    conn = _conn()
    cur = conn.cursor()
    if desde_fecha:
        cur.execute("SELECT * FROM sombra_log WHERE fecha >= ?", (desde_fecha,))
    else:
        cur.execute("SELECT * FROM sombra_log")
    logs = [dict(r) for r in cur.fetchall()]

    if not logs:
        conn.close()
        return {"total": 0}

    # Traer resultado real de las señales ya cerradas, para cruzar con el log
    ids = [l["senal_id"] for l in logs if l["senal_id"] is not None]
    resultados = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        cur.execute(f"""
            SELECT id, resultado_pct, cerrado FROM senales WHERE id IN ({placeholders})
        """, ids)
        for row in cur.fetchall():
            if row["cerrado"] == 1 and row["resultado_pct"] is not None:
                resultados[row["id"]] = row["resultado_pct"]
    conn.close()

    def _stat(campo):
        aprobo = [l for l in logs if l[campo] == 1]
        rechazo = [l for l in logs if l[campo] == 0]
        def _prom(lst):
            vals = [resultados[l["senal_id"]] for l in lst if l["senal_id"] in resultados]
            return {"n_total": len(lst), "n_con_resultado": len(vals),
                     "prom": round(sum(vals) / len(vals), 2) if vals else None}
        return {"aprobo": _prom(aprobo), "rechazo": _prom(rechazo)}

    return {
        "total": len(logs),
        "multi_tf": _stat("multi_tf"),
        "adx_gate": _stat("adx_gate"),
        "volumen": _stat("volumen"),
        "vwap": _stat("vwap"),
        "cci": _stat("cci"),
        "obv": _stat("obv"),
    }


def resumen_grid_dinamico(desde_fecha: str = None) -> dict:
    """
    Informe del grid dinámico en modo sombra: cuántas veces se acercó al
    borde, cuántas tuvieron REBOTE CONFIRMADO (regla real del DGT) y
    "hubiera ajustado" con esa confirmación, vs. cuántas hubieran ajustado
    con el criterio viejo (solo distancia, sin esperar rebote) — para
    comparar si confirmar el rebote realmente filtra mejor.
    """
    conn = _conn()
    cur = conn.cursor()
    if desde_fecha:
        cur.execute("SELECT * FROM grid_dinamico_log WHERE fecha >= ?", (desde_fecha,))
    else:
        cur.execute("SELECT * FROM grid_dinamico_log")
    logs = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not logs:
        return {"total": 0}
    cerca_del_borde = [l for l in logs if l["distancia_borde_pct"] is not None]
    con_rebote_confirmado = [l for l in logs if l["rebote_confirmado"] == 1]
    hubiera_ajustado = [l for l in logs if l["hubiera_ajustado"] == 1]
    return {
        "total_chequeos": len(logs),
        "cerca_del_borde_n": len(cerca_del_borde),
        "rebote_confirmado_n": len(con_rebote_confirmado),
        "hubiera_ajustado_n": len(hubiera_ajustado),
        "pares_afectados": sorted(set(l["par"] for l in hubiera_ajustado)),
    }


# ══════════════════════════════════════════════════════════════════
# 04/08 — Cinturón PAXG/BTC (separado de v16, fondeado directo en BTC)
# ══════════════════════════════════════════════════════════════════

def guardar_paxg_mercado_log(datos: dict):
    """
    04/08 — Guarda una foto del mercado (PAXG/BTC + BTC/USDT + oro +
    indicadores). Se llama SIEMPRE, haya o no señal — es la base de datos
    para buscar parámetros óptimos más adelante.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paxg_mercado_log
            (timestamp, precio_paxgbtc, precio_btc_usdt, precio_oro_usd,
             rsi_paxgbtc, adx_paxgbtc, plus_di, minus_di, bb_upper, bb_lower, bb_mid,
             ema9_paxgbtc, ema21_paxgbtc, estado_btc, rsi_oro, tendencia_oro)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(TZ_ARG).isoformat(),
        datos.get("precio_paxgbtc"), datos.get("precio_btc_usdt"), datos.get("precio_oro_usd"),
        datos.get("rsi_paxgbtc"), datos.get("adx_paxgbtc"), datos.get("plus_di"), datos.get("minus_di"),
        datos.get("bb_upper"), datos.get("bb_lower"), datos.get("bb_mid"),
        datos.get("ema9_paxgbtc"), datos.get("ema21_paxgbtc"),
        datos.get("estado_btc"), datos.get("rsi_oro"), datos.get("tendencia_oro"),
    ))
    conn.commit()
    conn.close()


def abrir_paxg_simulacion(combinacion: str, senal_tipo: str, riesgo: str,
                            apalancamiento: int, tp_objetivo_pct: float,
                            direccion: str, precio_entrada: float) -> int:
    """04/08 — Abre una de las 24 combinaciones simuladas."""
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO paxg_simulaciones
            (combinacion, senal_tipo, riesgo, apalancamiento, tp_objetivo_pct,
             direccion, precio_entrada, fecha, hora_apertura, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        combinacion, senal_tipo, riesgo, apalancamiento, tp_objetivo_pct,
        direccion, precio_entrada, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat(),
    ))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def paxg_simulaciones_abiertas() -> list:
    """04/08 — Todas las combinaciones simuladas todavía sin cerrar."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM paxg_simulaciones WHERE cerrada = 0")
    filas = [dict(r) for r in cur.fetchall()]
    conn.close()
    return filas


def actualizar_paxg_simulacion(sim_id: int, resultado_actual: float):
    """04/08 — Actualiza MAE/MFE de una combinación (mismo patrón que el resto del bot)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE paxg_simulaciones
        SET peor_resultado_pct = CASE WHEN peor_resultado_pct IS NULL OR ? < peor_resultado_pct THEN ? ELSE peor_resultado_pct END,
            mejor_resultado_pct = CASE WHEN mejor_resultado_pct IS NULL OR ? > mejor_resultado_pct THEN ? ELSE mejor_resultado_pct END
        WHERE id = ?
    """, (resultado_actual, resultado_actual, resultado_actual, resultado_actual, sim_id))
    conn.commit()
    conn.close()


def cerrar_paxg_simulacion(sim_id: int, resultado_pct: float, motivo: str):
    """04/08 — Cierra una combinación (TP, stop-loss, o cierre forzado por intradía)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT hora_apertura, fecha FROM paxg_simulaciones WHERE id = ?", (sim_id,))
    row = cur.fetchone()
    tiempo_min = None
    if row:
        try:
            apertura = datetime.strptime(f"{row['fecha']} {row['hora_apertura']}", "%Y%m%d %H:%M").replace(tzinfo=TZ_ARG)
            tiempo_min = int((datetime.now(TZ_ARG) - apertura).total_seconds() / 60)
        except Exception:
            pass
    cur.execute("""
        UPDATE paxg_simulaciones
        SET cerrada = 1, resultado_pct = ?, motivo_cierre = ?, tiempo_min = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, tiempo_min, datetime.now(TZ_ARG).strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


def paxg_hay_combos_abiertas_de(senal_tipo: str) -> bool:
    """04/08 — Para no reabrir un lote nuevo de una señal mientras el lote anterior sigue corriendo."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM paxg_simulaciones WHERE senal_tipo = ? AND cerrada = 0", (senal_tipo,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def resumen_paxg_simulaciones(desde_fecha: str = None) -> list:
    """
    04/08 — Resultado agregado por combinación (las 24), para elegir la
    mejor al cabo de los 30 días: win rate, resultado promedio, MAE/MFE.
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM paxg_simulaciones WHERE cerrada = 1"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return []

    por_combo = {}
    for f in filas:
        por_combo.setdefault(f["combinacion"], []).append(f)

    resumen = []
    for combo, lst in por_combo.items():
        resultados = [f["resultado_pct"] for f in lst if f["resultado_pct"] is not None]
        ganadas = [r for r in resultados if r > 0]
        mae = [f["peor_resultado_pct"] for f in lst if f["peor_resultado_pct"] is not None]
        resumen.append({
            "combinacion": combo,
            "n": len(lst),
            "win_rate_pct": round(len(ganadas) / len(lst) * 100, 1) if lst else None,
            "resultado_promedio_pct": round(sum(resultados) / len(resultados), 3) if resultados else None,
            "mae_promedio": round(sum(mae) / len(mae), 2) if mae else None,
        })
    resumen.sort(key=lambda x: (x["resultado_promedio_pct"] is None, -(x["resultado_promedio_pct"] or 0)))
    return resumen


def ultimos_precios_oro(n: int = 20) -> list:
    """04/08 — Últimos N precios de oro guardados (para armar tendencia propia, sin velas históricas gratis)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT precio_oro_usd FROM paxg_mercado_log
        WHERE precio_oro_usd IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (n,))
    valores = [r[0] for r in cur.fetchall()]
    conn.close()
    return valores


# ══════════════════════════════════════════════════════════════════
# 05/08 — Cinturón BingX (investigación pura, modo sombra — sin operar)
# ══════════════════════════════════════════════════════════════════

def guardar_bingx_dato(datos: dict) -> int:
    """Guarda un snapshot (imbalance + indicadores + precio). Devuelve el id."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bingx_datos_log (timestamp, symbol, precio, imbalance, rsi_1m, rsi_5m, vwap_1m)
        VALUES (?,?,?,?,?,?,?)
    """, (
        datetime.now(TZ_ARG).isoformat(), datos.get("symbol", "BTC-USDT"), datos.get("precio"),
        datos.get("imbalance"), datos.get("rsi_1m"), datos.get("rsi_5m"), datos.get("vwap_1m"),
    ))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def completar_resultados_bingx(precio_actual: float, symbol: str = "BTC-USDT"):
    """
    05/08 — Completa "qué hizo el precio después" para filas viejas que
    todavía no lo tienen. Se llama cada ciclo con el precio actual: busca
    filas de hace ~1 min sin precio_1min_despues, y de hace ~5 min sin
    precio_5min_despues, y las completa con el precio de AHORA. Usa un
    margen de tolerancia de 30 seg (no hace falta el segundo exacto).
    """
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    v_1min_desde = (ahora - timedelta(seconds=75)).isoformat()
    v_1min_hasta = (ahora - timedelta(seconds=45)).isoformat()
    v_5min_desde = (ahora - timedelta(seconds=330)).isoformat()
    v_5min_hasta = (ahora - timedelta(seconds=270)).isoformat()

    cur.execute("""
        UPDATE bingx_datos_log SET precio_1min_despues = ?
        WHERE symbol = ? AND precio_1min_despues IS NULL AND timestamp BETWEEN ? AND ?
    """, (precio_actual, symbol, v_1min_desde, v_1min_hasta))
    cur.execute("""
        UPDATE bingx_datos_log SET precio_5min_despues = ?
        WHERE symbol = ? AND precio_5min_despues IS NULL AND timestamp BETWEEN ? AND ?
    """, (precio_actual, symbol, v_5min_desde, v_5min_hasta))
    conn.commit()
    conn.close()


def resumen_umbral_imbalance(desde_fecha: str = None) -> list:
    """
    05/08 — Para distintos umbrales de |imbalance|, calcula qué % de las
    veces el precio se movió en la dirección "predicha" por el
    desequilibrio (positivo -> predice suba, negativo -> predice baja),
    a 1 y a 5 minutos. Es la base para elegir el umbral óptimo con datos
    reales — no a ciegas ni con un número fijo de entrada.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT imbalance, precio, precio_1min_despues, precio_5min_despues
        FROM bingx_datos_log WHERE imbalance IS NOT NULL
    """)
    filas = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not filas:
        return []

    umbrales = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    resumen = []
    for u in umbrales:
        aciertos_1m = total_1m = aciertos_5m = total_5m = 0
        for f in filas:
            imb = f["imbalance"]
            if imb is None or abs(imb) < u:
                continue
            prediccion = 1 if imb > 0 else -1
            if f["precio_1min_despues"] is not None:
                total_1m += 1
                mov = f["precio_1min_despues"] - f["precio"]
                if (mov > 0 and prediccion == 1) or (mov < 0 and prediccion == -1):
                    aciertos_1m += 1
            if f["precio_5min_despues"] is not None:
                total_5m += 1
                mov = f["precio_5min_despues"] - f["precio"]
                if (mov > 0 and prediccion == 1) or (mov < 0 and prediccion == -1):
                    aciertos_5m += 1
        resumen.append({
            "umbral": u,
            "n_1m": total_1m,
            "acierto_1m_pct": round(aciertos_1m / total_1m * 100, 1) if total_1m else None,
            "n_5m": total_5m,
            "acierto_5m_pct": round(aciertos_5m / total_5m * 100, 1) if total_5m else None,
        })
    return resumen


# ══════════════════════════════════════════════════════════════════
# 10/08 — Cinturón BingX-martingala (investigación pura, modo sombra)
# ══════════════════════════════════════════════════════════════════

def abrir_secuencia_martingala(variante: str, direccion_op1: str, precio_entrada: float) -> int:
    """Abre una nueva secuencia de martingala (trade 1 de hasta 6)."""
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO bingx_martingala_secuencias
            (variante, fecha, hora_inicio, direccion_op1, trade_actual, apuesta_actual,
             precio_entrada_trade, hora_entrada_trade, direccion_trade_actual, creado)
        VALUES (?,?,?,?,1,5.0,?,?,?,?)
    """, (
        variante, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M:%S"), direccion_op1,
        precio_entrada, ahora.strftime("%H:%M:%S"), direccion_op1, ahora.isoformat(),
    ))
    conn.commit()
    sec_id = cur.lastrowid
    conn.close()
    return sec_id


def secuencias_martingala_abiertas(variante: str = None) -> list:
    """Secuencias de martingala todavía sin cerrar (opcionalmente filtradas por variante)."""
    conn = _conn()
    cur = conn.cursor()
    if variante:
        cur.execute("SELECT * FROM bingx_martingala_secuencias WHERE cerrada = 0 AND variante = ?", (variante,))
    else:
        cur.execute("SELECT * FROM bingx_martingala_secuencias WHERE cerrada = 0")
    filas = [dict(r) for r in cur.fetchall()]
    conn.close()
    return filas


def avanzar_trade_martingala(sec_id: int, nueva_apuesta: float, precio_entrada: float, direccion_trade: str):
    """El trade actual perdió — avanza al siguiente (dobla apuesta, nueva dirección/precio)."""
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE bingx_martingala_secuencias
        SET trade_actual = trade_actual + 1,
            apuesta_actual = ?,
            perdido_acumulado = perdido_acumulado + apuesta_actual,
            precio_entrada_trade = ?,
            hora_entrada_trade = ?,
            direccion_trade_actual = ?
        WHERE id = ?
    """, (nueva_apuesta, precio_entrada, ahora.strftime("%H:%M:%S"), direccion_trade, sec_id))
    conn.commit()
    conn.close()


def cerrar_secuencia_martingala(sec_id: int, resultado_usd: float, motivo: str):
    """Cierra una secuencia — 'ganada' (recuperó todo + algo) o 'ruina' (llegó al trade 6 y perdió)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE bingx_martingala_secuencias
        SET cerrada = 1, resultado_usd = ?, motivo_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_usd, motivo, datetime.now(TZ_ARG).strftime("%H:%M:%S"), sec_id))
    conn.commit()
    conn.close()


def resumen_martingala(desde_fecha: str = None) -> dict:
    """Resumen por variante: cuántas secuencias ganadas/ruina, resultado neto, profundidad promedio."""
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM bingx_martingala_secuencias WHERE cerrada = 1"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return {}

    resumen = {}
    for variante in ("A", "B"):
        lst = [f for f in filas if f["variante"] == variante]
        if not lst:
            continue
        ganadas = [f for f in lst if f["motivo_cierre"] == "ganada"]
        ruinas = [f for f in lst if f["motivo_cierre"] == "ruina"]
        resumen[variante] = {
            "n": len(lst),
            "ganadas": len(ganadas),
            "ruinas": len(ruinas),
            "win_rate_pct": round(len(ganadas) / len(lst) * 100, 1),
            "resultado_neto_usd": round(sum(f["resultado_usd"] or 0 for f in lst), 2),
            "profundidad_promedio": round(sum(f["trade_actual"] for f in lst) / len(lst), 2),
        }
    return resumen


# ══════════════════════════════════════════════════════════════════
# 11/08 — Capital persistente por track (bookkeeping sobre los mismos
# resultados de A/B, en modo "500 puro" y "1000 con reserva 500+500")
# ══════════════════════════════════════════════════════════════════

def obtener_capital_track(track: str) -> dict:
    """Devuelve el estado del track, creándolo con valores iniciales si no existe."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bingx_martingala_capital WHERE track = ?", (track,))
    row = cur.fetchone()
    if row:
        conn.close()
        return dict(row)
    reserva_inicial = 500.0 if track.endswith("_1000") else 0.0
    cur.execute("""
        INSERT INTO bingx_martingala_capital (track, capital_activo, reserva_disponible, actualizado)
        VALUES (?, 500.0, ?, ?)
    """, (track, reserva_inicial, datetime.now(TZ_ARG).isoformat()))
    conn.commit()
    conn.close()
    return {"track": track, "capital_activo": 500.0, "reserva_disponible": reserva_inicial, "veces_repuesto": 0}


def guardar_capital_track(track: str, capital_activo: float, reserva_disponible: float, veces_repuesto: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE bingx_martingala_capital
        SET capital_activo = ?, reserva_disponible = ?, veces_repuesto = ?, actualizado = ?
        WHERE track = ?
    """, (capital_activo, reserva_disponible, veces_repuesto, datetime.now(TZ_ARG).isoformat(), track))
    conn.commit()
    conn.close()


def resumen_capital_tracks() -> dict:
    """Estado actual de los 4 tracks (A_500, A_1000, B_500, B_1000)."""
    resumen = {}
    for variante in ("A", "B"):
        for modo in ("500", "1000"):
            track = f"{variante}_{modo}"
            resumen[track] = obtener_capital_track(track)
    return resumen


def resumen_estrategias_imbalance(fee_pct_roundtrip: float = 0.10) -> list:
    """
    11/08 — Calcula RETROACTIVAMENTE, con los datos ya guardados en
    bingx_datos_log, el resultado de 5 combinaciones de entrada (sin
    martingala, apuesta fija, sin doblar): umbral 0.4 solo, 0.4+RSI/VWAP,
    0.6 solo, 0.6+RSI/VWAP, y combinada (0.4 mínimo para entrar, doble
    "peso" si además llega a 0.6). Resta una comisión estimada (0.10%
    ida+vuelta, maker+taker o taker+taker en BingX perpetuos) del
    movimiento de precio bruto a 1 minuto — el resultado en % de precio
    no depende del apalancamiento que se use después (se cancela
    matemáticamente), así que sirve para cualquier tamaño de posición.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT imbalance, precio, precio_1min_despues, rsi_1m, vwap_1m
        FROM bingx_datos_log
        WHERE imbalance IS NOT NULL AND precio_1min_despues IS NOT NULL
    """)
    filas = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not filas:
        return []

    def retorno_neto(f):
        direccion = 1 if f["imbalance"] > 0 else -1
        movimiento_pct = (f["precio_1min_despues"] - f["precio"]) / f["precio"] * 100 * direccion
        return movimiento_pct, movimiento_pct - fee_pct_roundtrip

    def rsi_vwap_confirma(f, direccion):
        if f["rsi_1m"] is None or f["vwap_1m"] is None:
            return False
        rsi_ok = (f["rsi_1m"] > 50) if direccion == 1 else (f["rsi_1m"] < 50)
        vwap_ok = (f["precio"] > f["vwap_1m"]) if direccion == 1 else (f["precio"] < f["vwap_1m"])
        return rsi_ok and vwap_ok

    simples = {"0.4 solo": [], "0.4 + RSI/VWAP": [], "0.6 solo": [], "0.6 + RSI/VWAP": []}
    combinada = []  # lista de (bruto, neto, peso)

    for f in filas:
        imb = f["imbalance"]
        direccion = 1 if imb > 0 else -1
        bruto, neto = retorno_neto(f)

        if abs(imb) >= 0.4:
            simples["0.4 solo"].append((bruto, neto))
            peso = 2 if abs(imb) >= 0.6 else 1
            combinada.append((bruto, neto, peso))
            if rsi_vwap_confirma(f, direccion):
                simples["0.4 + RSI/VWAP"].append((bruto, neto))
        if abs(imb) >= 0.6:
            simples["0.6 solo"].append((bruto, neto))
            if rsi_vwap_confirma(f, direccion):
                simples["0.6 + RSI/VWAP"].append((bruto, neto))

    resumen = []
    for nombre, valores in simples.items():
        if not valores:
            resumen.append({"nombre": nombre, "n": 0})
            continue
        netos = [v[1] for v in valores]
        brutos = [v[0] for v in valores]
        ganadoras = sum(1 for v in netos if v > 0)
        resumen.append({
            "nombre": nombre, "n": len(valores),
            "bruto_prom_pct": round(sum(brutos) / len(brutos), 4),
            "retorno_prom_pct": round(sum(netos) / len(netos), 4),
            "win_rate_pct": round(ganadoras / len(valores) * 100, 1),
        })

    if combinada:
        suma_ponderada_bruto = sum(b * p for b, n, p in combinada)
        suma_ponderada_neto = sum(n * p for b, n, p in combinada)
        suma_pesos = sum(p for b, n, p in combinada)
        ganadoras = sum(1 for b, n, p in combinada if n > 0)
        resumen.append({
            "nombre": "Combinada (0.4 min, doble peso si 0.6)", "n": len(combinada),
            "bruto_prom_pct": round(suma_ponderada_bruto / suma_pesos, 4),
            "retorno_prom_pct": round(suma_ponderada_neto / suma_pesos, 4),
            "win_rate_pct": round(ganadoras / len(combinada) * 100, 1),
        })
    else:
        resumen.append({"nombre": "Combinada (0.4 min, doble peso si 0.6)", "n": 0})

    return resumen


def resumen_mae_paxg(desde_fecha: str = None) -> dict:
    """
    11/08 — Análisis de MAE/MFE para el cinturón PAXG/BTC (mismo enfoque
    que resumen_mae()/resumen_mfe() del bot principal): compara cuánto
    cayeron las combinaciones GANADORAS antes de recuperarse — la base
    para saber si el SL de -20% (heredado de v16, sin recalibrar para
    PAXG) tiene sentido acá, o si el perfil más tranquilo de PAXG permite
    uno más ajustado.
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT resultado_pct, peor_resultado_pct, mejor_resultado_pct FROM paxg_simulaciones WHERE cerrada = 1"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return {"total": 0}

    ganadoras = [f for f in filas if f["resultado_pct"] is not None and f["resultado_pct"] > 0]
    perdedoras = [f for f in filas if f["resultado_pct"] is not None and f["resultado_pct"] <= 0]
    mae_ganadoras = [g["peor_resultado_pct"] for g in ganadoras if g["peor_resultado_pct"] is not None]
    mae_perdedoras = [p["peor_resultado_pct"] for p in perdedoras if p["peor_resultado_pct"] is not None]

    return {
        "total_con_dato": len(filas),
        "n_ganadoras": len(ganadoras),
        "n_perdedoras": len(perdedoras),
        "mae_ganadoras_peor": min(mae_ganadoras) if mae_ganadoras else None,
        "mae_ganadoras_promedio": round(sum(mae_ganadoras) / len(mae_ganadoras), 2) if mae_ganadoras else None,
        "mae_perdedoras_promedio": round(sum(mae_perdedoras) / len(mae_perdedoras), 2) if mae_perdedoras else None,
        "confiable": len(filas) >= 50,
    }


def resumen_por_motivo_cierre_paxg(desde_fecha: str = None) -> dict:
    """
    12/08 — Desglosa las combinaciones de PAXG cerradas por motivo (tp
    viejo / trailing / stop_loss / cierre_intradia_forzado), con fecha del
    cierre más reciente de cada motivo. Sirve para VERIFICAR si el fix de
    trailing (11/08) ya está actuando de verdad — si siguen apareciendo
    cierres "tp" con fecha POSTERIOR al 11/08, el fix no se está aplicando
    a esas combinaciones (revisar despliegue), no es solo falta de datos
    nuevos.
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT motivo_cierre, resultado_pct, fecha, hora_cierre FROM paxg_simulaciones WHERE cerrada = 1"
    params = ()
    if desde_fecha:
        query += " AND fecha >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return {}

    por_motivo = {}
    for f in filas:
        m = f["motivo_cierre"] or "desconocido"
        por_motivo.setdefault(m, []).append(f)

    resumen = {}
    for motivo, lst in por_motivo.items():
        resultados = [f["resultado_pct"] for f in lst if f["resultado_pct"] is not None]
        fecha_mas_reciente = max(f["fecha"] for f in lst)
        hora_mas_reciente = max((f["hora_cierre"] or "") for f in lst if f["fecha"] == fecha_mas_reciente)
        resumen[motivo] = {
            "n": len(lst),
            "resultado_prom_pct": round(sum(resultados) / len(resultados), 3) if resultados else None,
            "cierre_mas_reciente": f"{fecha_mas_reciente} {hora_mas_reciente}",
        }
    return resumen


def resumen_intradia_forzado_paxg() -> list:
    """
    14/08 — Desglosa los cierres por "cierre_intradia_forzado" (posiciones
    que nunca activaron el trailing ni tocaron SL, se cerraron a la fuerza
    a las 20hs) por combinación — para ver si se concentran en TP altos
    (más difícil de activar) o riesgo bajo (necesita mover más el precio
    real para el mismo % apalancado).
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT combinacion, senal_tipo, riesgo, tp_objetivo_pct, resultado_pct
        FROM paxg_simulaciones
        WHERE cerrada = 1 AND motivo_cierre = 'cierre_intradia_forzado'
    """)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return []

    por_combo = {}
    for f in filas:
        por_combo.setdefault(f["combinacion"], []).append(f["resultado_pct"])

    resumen = []
    for combo, resultados in por_combo.items():
        resumen.append({
            "combinacion": combo,
            "n": len(resultados),
            "resultado_prom_pct": round(sum(resultados) / len(resultados), 2),
        })
    resumen.sort(key=lambda x: -x["n"])
    return resumen


def resumen_intradia_forzado_paxg_por_tp_riesgo() -> dict:
    """14/08 — Mismo análisis, pero agregado por TP y por riesgo (para ver el patrón general, no combo por combo)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT senal_tipo, riesgo, tp_objetivo_pct, resultado_pct
        FROM paxg_simulaciones
        WHERE cerrada = 1 AND motivo_cierre = 'cierre_intradia_forzado'
    """)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return {}

    por_tp, por_riesgo = {}, {}
    for f in filas:
        por_tp.setdefault(f["tp_objetivo_pct"], []).append(f["resultado_pct"])
        por_riesgo.setdefault(f["riesgo"], []).append(f["resultado_pct"])

    return {
        "por_tp": {tp: {"n": len(v), "prom": round(sum(v)/len(v), 2)} for tp, v in sorted(por_tp.items())},
        "por_riesgo": {r: {"n": len(v), "prom": round(sum(v)/len(v), 2)} for r, v in por_riesgo.items()},
    }


def resumen_mae_por_profundidad() -> list:
    """
    15/08 — Agrupa las operaciones REALES (senales) cerradas por qué tan
    profundo cayó el peor punto (MAE) antes de resolverse, en franjas de
    1 punto (0-1%, 1-2%, ..., hasta 20%+). Para cada franja: cuántas
    operaciones, duración promedio, y qué % terminó ganando vs. perdiendo.
    Objetivo: confirmar con datos si rangos de caída más profunda (ej.
    6-8%) generan operaciones más largas, como viene notando Juanjo, antes
    de definir un % óptimo de corte distinto al -20% actual.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT peor_resultado_pct, tiempo_real_min, resultado_pct
        FROM senales
        WHERE cerrado = 1 AND peor_resultado_pct IS NOT NULL AND peor_resultado_pct < 0
    """)
    filas = [dict(f) for f in cur.fetchall()]
    conn.close()
    if not filas:
        return []

    def franja(mae):
        profundidad = abs(mae)
        piso = int(profundidad)  # 0,1,2,...
        if piso >= 20:
            return "20%+"
        return f"{piso}-{piso+1}%"

    por_franja = {}
    for f in filas:
        clave = franja(f["peor_resultado_pct"])
        por_franja.setdefault(clave, []).append(f)

    def orden_franja(clave):
        if clave == "20%+":
            return 20
        return int(clave.split("-")[0])

    resumen = []
    for clave in sorted(por_franja.keys(), key=orden_franja):
        filas_franja = por_franja[clave]
        n = len(filas_franja)
        con_tiempo = [f["tiempo_real_min"] for f in filas_franja if f["tiempo_real_min"] is not None]
        con_resultado = [f["resultado_pct"] for f in filas_franja if f["resultado_pct"] is not None]
        ganadoras = [r for r in con_resultado if r > 0]
        resumen.append({
            "franja": clave,
            "n": n,
            "duracion_prom_min": round(sum(con_tiempo) / len(con_tiempo), 1) if con_tiempo else None,
            "duracion_max_min": max(con_tiempo) if con_tiempo else None,
            "win_rate_pct": round(len(ganadoras) / len(con_resultado) * 100, 1) if con_resultado else None,
            "n_con_resultado": len(con_resultado),
        })
    return resumen
