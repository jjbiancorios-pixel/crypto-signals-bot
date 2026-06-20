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

    conn.commit()
    conn.close()


# ── Señales (histórico completo) ────────────────────────────
def guardar_senal(r: dict) -> int:
    """Guarda una señal recién generada por el bot. Devuelve el id de la fila."""
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO senales (
            par, fecha, hora_alerta, direccion, score, preset_sugerido,
            precio_entrada, apal_calculado, rango_bajo_calc, rango_alto_calc,
            rango_pct_calc, grillas_calc, horas_1pct_calc, ganancia_8h_calc
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r["par"], ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
        r["direccion"], r["score"], r["preset"],
        r["precio"], r["apal"], r["rango_bajo"], r["rango_alto"],
        r["rango_pct"], r["grillas"], r["horas_1pct"], r["ganancia_8h"],
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
