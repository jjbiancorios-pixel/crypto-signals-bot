"""
telegram_cmds.py — Comandos manuales para JJ Cripto Bot
──────────────────────────────────────────────────────
Agrega "escucha" de Telegram (polling de getUpdates) para que el usuario
pueda registrar datos reales de Pionex y resultados de cierre.

Comandos:
  /registrar PAR APAL RANGO_BAJO RANGO_ALTO GRILLAS
      Ej: /registrar ALGO 10 0.395 0.410 120

  /cerrar PAR RESULTADO_PCT
      Ej: /cerrar ALGO -11.95
      Ej: /cerrar ALGO +1.2

  /comparar
      Muestra estadísticas acumuladas: ¿el rango calculado por el bot
      hubiera dado mejor resultado que el preset Balanceada de Pionex?

  /pendientes
      Lista las señales abiertas a las que les falta /registrar o /cerrar

No modifica generar_alertas(), analizar_par() ni calcular_grid().
"""
import requests
import os
import db
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

_ultimo_update_id = 0


def _api(method: str, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = requests.post(url, json=params, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return {}


def enviar(msg: str):
    _api("sendMessage", chat_id=CHAT_ID, text=msg, parse_mode="HTML")


def _quitar_simbolo(par_in: str) -> str:
    """Permite que el usuario escriba 'ALGO' y lo matchee contra 'ALGOUSDT'."""
    p = par_in.upper().strip()
    if not p.endswith("USDT"):
        p += "USDT"
    return p


def _parse_float(s: str):
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _cmd_registrar(args: list) -> str:
    # /registrar PAR APAL RANGO_BAJO RANGO_ALTO GRILLAS
    if len(args) < 5:
        return ("⚠️ Formato: /registrar PAR APAL RANGO_BAJO RANGO_ALTO GRILLAS\n"
                "Ej: /registrar ALGO 10 0.395 0.410 120")
    par = _quitar_simbolo(args[0])
    apal = None
    try:
        apal = int(args[1].lower().replace("x", ""))
    except ValueError:
        return "⚠️ Apalancamiento inválido. Usá un número, ej: 10"
    rango_bajo = _parse_float(args[2])
    rango_alto = _parse_float(args[3])
    try:
        grillas = int(args[4])
    except ValueError:
        return "⚠️ Número de grillas inválido."

    if rango_bajo is None or rango_alto is None:
        return "⚠️ Rango inválido. Usá números, ej: 0.395 0.410"

    senal = db.ultima_senal_par(par)
    if not senal:
        return (f"⚠️ No encontré una señal abierta reciente de {par}.\n"
                f"¿Seguro que el bot mandó una alerta de este par? Usá /pendientes para ver las abiertas.")

    db.registrar_datos_pionex(senal["id"], apal, rango_bajo, rango_alto, grillas)
    return (f"✅ Registrado {par} (señal #{senal['id']})\n"
            f"Pionex Balanceada → {apal}x | Rango: {rango_bajo}–{rango_alto} | Grillas: {grillas}\n"
            f"Bot había calculado → {senal['apal_calculado']}x | "
            f"Rango: {senal['rango_bajo_calc']}–{senal['rango_alto_calc']} | "
            f"Grillas: {senal['grillas_calc']}\n\n"
            f"Cuando cierres el bot en Pionex, usá /cerrar {args[0]} +X.X o /cerrar {args[0]} -X.X")


def _cmd_cerrar(args: list) -> str:
    # /cerrar PAR RESULTADO_PCT
    if len(args) < 2:
        return "⚠️ Formato: /cerrar PAR RESULTADO_PCT\nEj: /cerrar ALGO -11.95"
    par = _quitar_simbolo(args[0])
    resultado = _parse_float(args[1])
    if resultado is None:
        return "⚠️ Resultado inválido. Usá un número, ej: +1.2 o -11.95"

    senal = db.ultima_senal_par(par)
    if not senal:
        return f"⚠️ No encontré una señal abierta reciente de {par}."

    db.cerrar_senal(senal["id"], resultado)
    nota_pionex = "" if senal["registrado_pionex"] else "\n💡 Tip: la próxima vez usá /registrar antes de /cerrar para poder comparar contra Pionex."
    return f"✅ Cerrado {par} (señal #{senal['id']}) con resultado {resultado:+.2f}%{nota_pionex}"


def _cmd_comparar() -> str:
    s = db.stats_comparacion()
    if s["total"] == 0:
        return ("📊 Todavía no hay suficientes datos comparables.\n"
                "Usá /registrar al abrir cada bot y /cerrar al cerrarlo, y volvé a consultar /comparar en unos días.")

    def _linea(label, d):
        if d["n"] == 0:
            return f"• {label}: sin casos aún"
        return f"• {label}: {d['n']} casos | resultado prom: {d['prom']:+.2f}%"

    return (
        f"📊 <b>Comparación bot vs. preset Pionex Balanceada</b>\n"
        f"Total de señales comparadas: {s['total']}\n\n"
        f"{_linea('Bot sugería rango MÁS ANGOSTO que Pionex', s['bot_mas_angosto_que_pionex'])}\n"
        f"{_linea('Bot sugería rango MÁS ANCHO que Pionex', s['bot_mas_ancho_que_pionex'])}\n"
        f"{_linea('Rangos similares (±10%)', s['similar'])}\n\n"
        f"⚠️ Esto es informativo, no es asesoramiento financiero. "
        f"Con pocos casos, el promedio no es estadísticamente confiable — "
        f"conviene esperar a tener varias decenas de señales por categoría."
    )


def _cmd_pendientes() -> str:
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM senales WHERE cerrado = 0 ORDER BY id DESC LIMIT 15")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not rows:
        return "✅ No hay señales pendientes de registrar/cerrar."

    lineas = ["📋 <b>Señales abiertas</b> (más reciente primero):"]
    for r in rows:
        estado_pionex = "✅ Pionex registrado" if r["registrado_pionex"] else "⏳ falta /registrar"
        lineas.append(f"#{r['id']} {r['par']} {r['direccion']} | {r['hora_alerta']} | {estado_pionex}")
    lineas.append("\nUsá /registrar o /cerrar con el nombre del par (sin USDT), ej: /cerrar ALGO -11.95")
    return "\n".join(lineas)


def _fmt_resumen(r: dict, titulo: str) -> str:
    """Formatea un resumen (diario/semanal/mensual) para Telegram."""
    if r["n"] == 0 and r["n_abiertas"] == 0:
        return f"📊 <b>{titulo}</b>\nSin operaciones registradas aún."

    lineas = [f"📊 <b>{titulo}</b>", "━━━━━━━━━━━━━━━━━━━━"]

    # Con estancadas
    lineas.append(f"<b>Todas las operaciones cerradas:</b>")
    lineas.append(f"  Cerradas: {r['n']} | ✅ {r['n_pos']} ganadoras | ❌ {r['n_neg']} perdedoras")
    lineas.append(f"  Win rate: {r['win_rate']}%")
    lineas.append(f"  Ganancia total: <b>{r['gan_total']:+.2f}%</b>")
    lineas.append(f"  Ganancia promedio: {r['gan_prom']:+.2f}%")
    if r['n'] > 0:
        lineas.append(f"  Mejor: {r['mejor']:+.2f}% | Peor: {r['peor']:+.2f}%")

    # Sin estancadas (≤12hs)
    lineas.append("")
    lineas.append(f"<b>Sin estancadas (≤12hs de duración):</b>")
    lineas.append(f"  Operaciones: {r['n_rapidas']}")
    lineas.append(f"  Win rate: {r['win_rate_sin']}%")
    lineas.append(f"  Ganancia total: <b>{r['gan_total_sin']:+.2f}%</b>")
    lineas.append(f"  Ganancia promedio: {r['gan_prom_sin']:+.2f}%")

    # Abiertas
    if r["n_abiertas"] > 0:
        lineas.append("")
        lineas.append(f"⏳ Abiertas (sin cerrar aún): {r['n_abiertas']}")

    return "\n".join(lineas)


def _cmd_diario(args: list) -> str:
    from datetime import datetime, timezone, timedelta
    TZ_ARG = timezone(timedelta(hours=-3))
    if args:
        fecha = args[0].replace("/", "").replace("-", "")
    else:
        fecha = datetime.now(TZ_ARG).strftime("%Y%m%d")
    fecha_fmt = f"{fecha[6:8]}/{fecha[4:6]}/{fecha[0:4]}"
    r = db.resumen_diario(fecha)
    resumen = _fmt_resumen(r, f"Resumen del {fecha_fmt}")

    # El objetivo diario (ponderado por capital real) solo aplica a HOY —
    # no tiene sentido para /diario de una fecha pasada.
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    if fecha == hoy:
        import gestion_riesgo
        obj = db.obj_diario_real_db(gestion_riesgo.OBJETIVO_DIARIO_PCT, gestion_riesgo.CAPITAL_TOTAL_USD)
        resumen += (
            f"\n\n🎯 <b>Objetivo diario:</b> {obj['total']}% de "
            f"{gestion_riesgo.OBJETIVO_DIARIO_PCT}% | Faltan: {obj['faltan']}%"
        )
    return resumen


def _cmd_semanal() -> str:
    r = db.resumen_semanal()
    return _fmt_resumen(r, f"Resumen semanal ({r.get('periodo','')})")


def _cmd_mensual() -> str:
    r = db.resumen_mensual()
    return _fmt_resumen(r, f"Resumen mensual ({r.get('periodo','')})")


def _cmd_historial() -> str:
    dias = db.resumen_por_dia_detalle()
    if not dias:
        return "📅 Sin historial de operaciones aún."
    lineas = ["📅 <b>Historial por día</b> (últimos 30 días)\n━━━━━━━━━━━━━━━━━━━━"]
    for d in dias:
        fecha_fmt = f"{d['fecha'][6:8]}/{d['fecha'][4:6]}"
        signo = "✅" if (d['gan_total'] or 0) >= 0 else "❌"
        lineas.append(
            f"{fecha_fmt}: {signo} {(d['gan_total'] or 0):+.2f}% | "
            f"C:{d['positivas']}✅ {d['negativas']}❌ | Abiertas:{d['abiertas']}"
        )
    return "\n".join(lineas)


def _cmd_debug_orden(args: list) -> str:
    """
    Diagnóstico: muestra los datos CRUDOS que devuelve Pionex para la
    operación abierta de un par (sin adivinar campos). Sirve para
    confirmar contra la app cuál es el campo real de "Ganancia total"
    antes de confiar en un cálculo automático con capital real.
    Uso: /debug_orden PAR
    Ej:  /debug_orden CRV
    """
    if len(args) < 1:
        return "Uso: /debug_orden PAR\nEj: /debug_orden CRV"
    par_completo = args[0].upper().strip()
    if not par_completo.endswith("USDT"):
        par_completo += "USDT"

    senal = db.ultima_senal_par(par_completo)
    if not senal or not senal.get("bu_order_id"):
        return f"⚠️ No encontré una operación automática abierta de {par_completo} (sin bu_order_id)."

    try:
        import pionex_api
        resultado = pionex_api.consultar_orden(senal["bu_order_id"])
        bod = resultado.get("data", {}).get("buOrderData", {})
        return (
            f"🔍 <b>Debug — {par_completo}</b>\n"
            f"Inversión guardada: USD {senal.get('capital_asignado')}\n\n"
            f"<code>{bod}</code>"
        )
    except Exception as e:
        return f"⚠️ Error: {e}"


def _cmd_probar_pionex(args: list) -> str:
    """
    Prueba la conexión con la API de Pionex SIN crear ninguna orden real.
    Llama a checkParams (solo valida y estima), para confirmar que la
    firma HMAC y las keys cargadas en Railway funcionan bien.
    Uso: /probar_pionex PAR PRECIO_ACTUAL [LEVERAGE] [CAPITAL_USD] [MARGEN_USD]
    Ej:  /probar_pionex ALGO 0.20
    Ej:  /probar_pionex BTC 64000 5 100
    Ej:  /probar_pionex BTC 64000 10 90 45   (con margen de origen)
    """
    if len(args) < 2:
        return "Uso: /probar_pionex PAR PRECIO_ACTUAL [LEVERAGE] [CAPITAL_USD] [MARGEN_USD]\nEj: /probar_pionex ALGO 0.20"
    par = args[0].upper().strip().replace("USDT", "")
    precio = _parse_float(args[1])
    if precio is None:
        return "⚠️ El precio tiene que ser un número. Ej: /probar_pionex ALGO 0.20"

    leverage = int(_parse_float(args[2])) if len(args) > 2 and _parse_float(args[2]) else 10
    capital = _parse_float(args[3]) if len(args) > 3 and _parse_float(args[3]) else 50
    margen = _parse_float(args[4]) if len(args) > 4 and _parse_float(args[4]) else 0

    top = round(precio * 1.03, 6)
    bottom = round(precio * 0.97, 6)

    try:
        import pionex_api
        resultado = pionex_api.validar_parametros_grilla(
            par=par, top=top, bottom=bottom, row=67,
            capital_usdt=capital, leverage=leverage, extra_margin_usdt=margen
        )
        return (
            f"🧪 <b>Prueba Pionex — {par}</b> (sin crear orden real)\n"
            f"Rango: {bottom}–{top} | 67 grillas | {leverage}x | "
            f"USD {capital} inversión"
            + (f" + USD {margen} margen" if margen else "") + "\n\n"
            f"<code>{resultado}</code>"
        )
    except Exception as e:
        return f"⚠️ Error al conectar con Pionex: {e}"


def _cmd_pausar_todo(args: list) -> str:
    motivo = " ".join(args) if args else "sin motivo especificado"
    db.pausar_todo(motivo)
    return (
        f"🛑 <b>Bot PAUSADO</b>\n"
        f"Motivo: {motivo}\n\n"
        f"No se van a enviar alertas ni abrir grillas nuevas hasta que "
        f"uses /reanudar_todo. Las operaciones ya abiertas en Pionex "
        f"siguen funcionando normalmente (esto no las cierra ni las toca)."
    )


def _cmd_reanudar_todo() -> str:
    db.reanudar_todo()
    return "✅ <b>Bot reanudado</b>. Vuelve a analizar y alertar normalmente."


def _cmd_exportar() -> str:
    """
    Exporta TODO el historial de señales (tabla senales completa) a un CSV
    y lo manda directo como archivo por Telegram — mismo tipo de datos que
    tenía el Excel original de 177 operaciones, pero generado solo.
    """
    import csv
    import io

    conn = db._conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM senales ORDER BY id")
    rows = cur.fetchall()
    columnas = [d[0] for d in cur.description]
    conn.close()

    if not rows:
        return "No hay señales registradas todavía para exportar."

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columnas)
    for row in rows:
        writer.writerow([row[c] for c in columnas])

    # utf-8-sig: para que Excel abra bien los acentos (ñ, á, etc.)
    csv_bytes = output.getvalue().encode("utf-8-sig")

    fecha = datetime.now().strftime("%Y%m%d_%H%M")
    nombre_archivo = f"jj_cripto_bot_historial_{fecha}.csv"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    files = {"document": (nombre_archivo, csv_bytes, "text/csv")}
    data = {"chat_id": CHAT_ID, "caption": f"📊 Historial completo — {len(rows)} señales registradas"}
    try:
        requests.post(url, data=data, files=files, timeout=30)
        return None  # el archivo ya se mandó directo, no hace falta texto extra
    except Exception as e:
        return f"⚠️ Error al exportar: {e}"


def procesar_comando(texto: str) -> str:
    partes = texto.strip().split()
    if not partes:
        return ""
    cmd = partes[0].lower()
    args = partes[1:]

    if cmd == "/registrar":
        return _cmd_registrar(args)
    elif cmd == "/cerrar":
        return _cmd_cerrar(args)
    elif cmd == "/comparar":
        return _cmd_comparar()
    elif cmd == "/pendientes":
        return _cmd_pendientes()
    elif cmd == "/diario":
        return _cmd_diario(args)
    elif cmd == "/semanal":
        return _cmd_semanal()
    elif cmd == "/mensual":
        return _cmd_mensual()
    elif cmd == "/historial":
        return _cmd_historial()
    elif cmd == "/probar_pionex":
        return _cmd_probar_pionex(args)
    elif cmd == "/pausar_todo":
        return _cmd_pausar_todo(args)
    elif cmd == "/reanudar_todo":
        return _cmd_reanudar_todo()
    elif cmd == "/exportar":
        return _cmd_exportar()
    elif cmd == "/debug_orden":
        return _cmd_debug_orden(args)
    elif cmd in ("/ayuda", "/help", "/start"):
        return (
            "🤖 <b>Comandos disponibles</b>\n\n"
            "/registrar PAR APAL RANGO_BAJO RANGO_ALTO GRILLAS\n"
            "  Anotá lo que Pionex te ofreció al crear el bot.\n"
            "  Ej: /registrar ALGO 10 0.395 0.410 120\n\n"
            "/cerrar PAR RESULTADO_PCT\n"
            "  Anotá el resultado final cuando cerrás el bot en Pionex.\n"
            "  Ej: /cerrar ALGO -11.95\n\n"
            "/comparar\n"
            "  Ve cómo le fue al cálculo del bot vs. el preset Balanceada.\n\n"
            "/pendientes\n"
            "  Lista señales abiertas sin registrar o cerrar.\n\n"
            "/diario [FECHA]\n"
            "  Resumen del día (con y sin estancadas).\n"
            "  Ej: /diario  o  /diario 20260630\n\n"
            "/semanal\n"
            "  Resumen de los últimos 7 días.\n\n"
            "/mensual\n"
            "  Resumen de los últimos 30 días.\n\n"
            "/historial\n"
            "  Ganancia/pérdida por día, últimos 30 días.\n\n"
            "/probar_pionex PAR PRECIO_ACTUAL\n"
            "  Prueba la conexión con Pionex (sin crear orden real).\n"
            "  Ej: /probar_pionex BTC 63000\n\n"
            "/pausar_todo [motivo]\n"
            "  🛑 Frena TODO el bot (alertas y aperturas automáticas).\n"
            "  No afecta operaciones ya abiertas en Pionex.\n\n"
            "/reanudar_todo\n"
            "  ✅ Reactiva el bot después de /pausar_todo.\n\n"
            "/exportar\n"
            "  📊 Manda un CSV con TODO el historial (abrí con Excel)."
        )
    return None


def revisar_updates():
    """
    Hace polling de getUpdates (long-poll corto) y procesa comandos nuevos.
    Llamar periódicamente desde el loop principal (ej. cada 30s, junto con schedule.run_pending()).
    """
    global _ultimo_update_id
    data = _api("getUpdates", offset=_ultimo_update_id + 1, timeout=5)
    if not data.get("ok"):
        return
    for update in data.get("result", []):
        _ultimo_update_id = max(_ultimo_update_id, update["update_id"])
        msg = update.get("message", {})
        texto = msg.get("text", "")
        chat_id_msg = str(msg.get("chat", {}).get("id", ""))
        if not texto.startswith("/"):
            continue
        if CHAT_ID and chat_id_msg != str(CHAT_ID):
            continue  # Ignorar comandos de otros chats (seguridad básica)
        respuesta = procesar_comando(texto)
        if respuesta:
            enviar(respuesta)
