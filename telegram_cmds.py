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
    # Mejora 1: el objetivo diario ahora se alimenta del % REAL de Pionex,
    # no de la estimación del bot (ganancia_8h_calc).
    db.registrar_ganancia_dia_real(par, resultado)

    nota_pionex = "" if senal["registrado_pionex"] else "\n💡 Tip: la próxima vez usá /registrar antes de /cerrar para poder comparar contra Pionex."

    # Mejora 3: si las últimas 2 operaciones de este par fueron negativas,
    # sugerir pausa de 24h (no se pausa automático, queda a tu criterio)
    ultimos = db.ultimos_resultados_par(par, 2)
    nota_racha = ""
    if len(ultimos) >= 2 and all(r < 0 for r in ultimos):
        nota_racha = (
            f"\n⚠️ {par} lleva {len(ultimos)} resultados negativos seguidos "
            f"({', '.join(f'{r:+.2f}%' for r in ultimos)}).\n"
            f"Usá /pausar {args[0]} para excluirlo 24h del análisis si querés."
        )

    return (f"✅ Cerrado {par} (señal #{senal['id']}) con resultado {resultado:+.2f}%"
            f"{nota_pionex}{nota_racha}")


def _cmd_pausar(args: list) -> str:
    if len(args) < 1:
        return "⚠️ Formato: /pausar PAR\nEj: /pausar MANA"
    par = _quitar_simbolo(args[0])
    db.pausar_par(par, motivo="manual", horas=24)
    return f"⏸️ {par} pausado por 24h. No recibirás nuevas señales de este par hasta entonces.\nUsá /reanudar {args[0]} para levantar la pausa antes."


def _cmd_reanudar(args: list) -> str:
    if len(args) < 1:
        return "⚠️ Formato: /reanudar PAR\nEj: /reanudar MANA"
    par = _quitar_simbolo(args[0])
    db.despausar_par(par)
    return f"▶️ {par} reanudado. Volverá a analizarse en el próximo ciclo."


def _cmd_objetivo() -> str:
    obj = db.obj_diario_db(3.0)
    estado = "✅ CUBIERTO" if obj["ok"] else f"faltan {obj['faltan']}%"
    return (
        f"📅 <b>Objetivo diario (sobre resultado REAL de Pionex)</b>\n"
        f"Operaciones cerradas hoy: {obj['n']}\n"
        f"Acumulado real: {obj['total']:+.2f}%\n"
        f"Estado: {estado}\n\n"
        f"💡 Este cálculo usa lo que registraste con /cerrar (% real de Pionex, "
        f"ya neto de reserva), no la estimación teórica del bot."
    )


def _cmd_estancadas() -> str:
    estancadas = db.operaciones_estancadas(horas_limite=6.0)
    if not estancadas:
        return "✅ No hay operaciones abiertas hace más de 6 horas."

    lineas = ["⏳ <b>Operaciones estancadas (+6hs abiertas)</b>\n"]
    for r in estancadas:
        lineas.append(
            f"#{r['id']} {r['par']} {r['direccion']} | {r['horas_abierta']}hs abierta\n"
            f"   Entrada: {r['precio_entrada']} | Rango Pionex: {r['rango_bajo_pionex']}–{r['rango_alto_pionex']}"
        )
    lineas.append(
        "\n💡 Si sigue dentro de rango pero en pérdida de tendencia hace mucho, "
        "podés considerar abrir la dirección CONTRARIA con capital nuevo "
        "(no es obligatorio). Esto diversifica exposición, no es 'doblar la apuesta' "
        "sobre la misma posición."
    )
    return "\n".join(lineas)


def _cmd_pausados() -> str:
    activos = db.pares_pausados_activos()
    if not activos:
        return "✅ No hay pares pausados actualmente."
    lineas = ["⏸️ <b>Pares pausados</b>\n"]
    for r in activos:
        lineas.append(f"{r['par']} — motivo: {r['motivo']} | hasta: {r['hasta'][:16]}")
    return "\n".join(lineas)


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
    elif cmd == "/pausar":
        return _cmd_pausar(args)
    elif cmd == "/reanudar":
        return _cmd_reanudar(args)
    elif cmd == "/objetivo":
        return _cmd_objetivo()
    elif cmd == "/estancadas":
        return _cmd_estancadas()
    elif cmd == "/pausados":
        return _cmd_pausados()
    elif cmd in ("/ayuda", "/help", "/start"):
        return (
            "🤖 <b>Comandos disponibles</b>\n\n"
            "/registrar PAR APAL RANGO_BAJO RANGO_ALTO GRILLAS\n"
            "  Anotá lo que Pionex te ofreció al crear el bot.\n"
            "  Ej: /registrar ALGO 10 0.395 0.410 120\n\n"
            "/cerrar PAR RESULTADO_PCT\n"
            "  Anotá el resultado final (% real de Pionex) al cerrar.\n"
            "  Ej: /cerrar ALGO -11.95\n\n"
            "/comparar\n"
            "  Ve cómo le fue al cálculo del bot vs. el preset Balanceada.\n\n"
            "/pendientes\n"
            "  Lista señales abiertas sin registrar o cerrar.\n\n"
            "/objetivo\n"
            "  Progreso del día hacia el 3%, sobre resultado REAL (no estimado).\n\n"
            "/estancadas\n"
            "  Operaciones abiertas hace +6hs. Sugiere considerar hedge.\n\n"
            "/pausar PAR  /reanudar PAR\n"
            "  Excluye o reincluye un par del análisis automático.\n\n"
            "/pausados\n"
            "  Lista pares pausados actualmente."
        )
    return None  # No es un comando reconocido


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
