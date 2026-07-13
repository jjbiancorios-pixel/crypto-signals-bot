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
def guardar_senal(r: dict) -> int:
    """Guarda una señal recién generada por el bot. Devuelve el id de la fila."""
    import json
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    razones_json = json.dumps(r.get("razones", []), ensure_ascii=False)
    cur.execute("""
        INSERT INTO senales (
            par, fecha, hora_alerta, direccion, score, preset_sugerido,
            precio_entrada, apal_calculado, rango_bajo_calc, rango_alto_calc,
            rango_pct_calc, grillas_calc, horas_1pct_calc, ganancia_8h_calc, razones
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r["par"], ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
        r["direccion"], r["score"], r["preset"],
        r["precio"], r["apal"], r["rango_bajo"], r["rango_alto"],
        r["rango_pct"], r["grillas"], r["horas_1pct"], r["ganancia_8h"], razones_json,
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
def _calcular_resumen(rows: list) -> dict:
    """Calcula métricas comunes dado una lista de filas de senales."""
    if not rows:
        return {"n": 0, "n_pos": 0, "n_neg": 0, "n_abiertas": 0,
                "gan_total": 0, "gan_prom": 0, "win_rate": 0,
                "gan_total_sin": 0, "gan_prom_sin": 0, "win_rate_sin": 0}

    cerradas = [r for r in rows if r["cerrado"] == 1 and r["resultado_pct"] is not None]
    abiertas = [r for r in rows if r["cerrado"] == 0]
    positivas = [r for r in cerradas if r["resultado_pct"] > 0]
    negativas = [r for r in cerradas if r["resultado_pct"] <= 0]

    # Con estancadas (todas las cerradas)
    gan_total = sum(r["resultado_pct"] for r in cerradas)
    gan_prom = gan_total / len(cerradas) if cerradas else 0
    win_rate = len(positivas) / len(cerradas) * 100 if cerradas else 0

    # Sin estancadas (solo las que cerraron en <= 12 horas, el umbral confirmado)
    rapidas = [r for r in cerradas if r["tiempo_real_min"] is not None and r["tiempo_real_min"] <= 720]
    gan_total_sin = sum(r["resultado_pct"] for r in rapidas)
    gan_prom_sin = gan_total_sin / len(rapidas) if rapidas else 0
    win_rate_sin = sum(1 for r in rapidas if r["resultado_pct"] > 0) / len(rapidas) * 100 if rapidas else 0

    return {
        "n": len(cerradas),
        "n_pos": len(positivas),
        "n_neg": len(negativas),
        "n_abiertas": len(abiertas),
        "n_rapidas": len(rapidas),
        "gan_total": round(gan_total, 2),
        "gan_prom": round(gan_prom, 2),
        "win_rate": round(win_rate, 1),
        "gan_total_sin": round(gan_total_sin, 2),
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
    resultado = _calcular_resumen(rows)
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
    resultado = _calcular_resumen(rows)
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
    resultado = _calcular_resumen(rows)
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


def cerrar_senal_automatica(senal_id: int, resultado_pct: float):
    """
    Marca una señal como cerrada cuando la automatización DETECTA (vía API)
    que Pionex ya cerró la grilla — libera el capital comprometido para que
    vuelva a estar disponible para nuevas operaciones. Sin esto, las
    operaciones cerradas quedaban marcadas como abiertas para siempre,
    bloqueando capital fantasma (bug encontrado y corregido 12/07).
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales SET cerrado = 1, resultado_pct = ?
        WHERE id = ?
    """, (resultado_pct, senal_id))
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
