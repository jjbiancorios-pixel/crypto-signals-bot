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
import io
from PIL import Image, ImageDraw, ImageFont
import db
from datetime import datetime

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

_ultimo_update_id = 0


def _api(method: str, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = requests.post(url, json=params, timeout=20)  # margen extra sobre el long-poll de getUpdates (5s)
        return r.json()
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return {}


def enviar(msg: str):
    _api("sendMessage", chat_id=CHAT_ID, text=msg, parse_mode="HTML")


def enviar_imagen(imagen_bytes: bytes, nombre_archivo: str, caption: str = ""):
    """
    03/08 — Manda una imagen como DOCUMENTO (no sendPhoto) para que Telegram
    no la comprima — así queda descargable en calidad original con solo
    tocarla/mantenerla apretada.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        files = {"document": (nombre_archivo, imagen_bytes, "image/png")}
        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        r = requests.post(url, data=data, files=files, timeout=30)
        return r.json()
    except Exception as e:
        print(f"Telegram API error (sendDocument): {e}")
        return {}


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


def _cmd_corregir(args: list) -> str:
    """
    28/07 — Corrige una señal que el bot cerró con datos falsos (ej:
    detectó un cierre porque hiciste "Restablecer P&L" manual en Pionex,
    que resetea el tracking sin cerrar la posición real).

    Uso: /corregir PAR RESULTADO_PCT [abierta|cerrada] [capital] [bu_order_id] [tiempo_min] [peor_pct]
    Ej:  /corregir MOVE -24.8 abierta 81.48
         (deja MOVE abierta de nuevo, con -24.8% y USD 81.48 de capital)
    Ej:  /corregir INJ 1.92 cerrada 375.81 - 7740 -53
         (cerrada, USD 375.81, sin bu_order_id nuevo, duró 7740 min, tocó -53% de peor punto)

    11/08: agregados tiempo_min (duración real en minutos) y peor_pct (MAE,
    el peor % que tocó) — antes se perdían al corregir, justo cuando más
    valen para el análisis de MAE (caso real: INJUSDT, 5d9h, -53%). Usá
    "-" en bu_order_id si querés saltearlo pero sí cargar los siguientes.

    Si Pionex generó un bu_order_id NUEVO al resetear (chequealo con
    /debug_orden PAR — si tira error, cambió), pasalo como 5to parámetro.
    """
    if len(args) < 2:
        return (
            "⚠️ Formato: /corregir PAR RESULTADO_PCT [abierta|cerrada] [capital] [bu_order_id] [tiempo_min] [peor_pct]\n"
            "Ej: /corregir MOVE -24.8 abierta 81.48\n"
            "Ej: /corregir INJ 1.92 cerrada 375.81 - 7740 -53"
        )
    par = _quitar_simbolo(args[0])
    resultado = _parse_float(args[1])
    if resultado is None:
        return "⚠️ Resultado inválido. Usá un número, ej: -24.8"
    estado = args[2].lower() if len(args) > 2 else "cerrada"
    if estado not in ("abierta", "cerrada"):
        return "⚠️ El 3er parámetro debe ser 'abierta' o 'cerrada'."
    capital_nuevo = _parse_float(args[3]) if len(args) > 3 else None
    bu_order_id_nuevo = args[4] if len(args) > 4 and args[4] != "-" else None
    tiempo_min_nuevo = int(_parse_float(args[5])) if len(args) > 5 and _parse_float(args[5]) is not None else None
    peor_pct_nuevo = _parse_float(args[6]) if len(args) > 6 else None

    senal = db.ultima_senal_par_cualquiera(par)
    if not senal:
        return f"⚠️ No encontré ninguna señal de {par} en la base (ni abierta ni cerrada)."

    db.corregir_senal(senal["id"], resultado, reabrir=(estado == "abierta"),
                       capital_asignado=capital_nuevo, bu_order_id=bu_order_id_nuevo,
                       tiempo_real_min=tiempo_min_nuevo, peor_resultado_pct=peor_pct_nuevo)
    return (
        f"✅ Corregido {par} (señal #{senal['id']})\n"
        f"Resultado: {resultado:+.2f}% | Estado: {estado}"
        + (f" | Capital: USD {capital_nuevo:.2f}" if capital_nuevo else "")
        + (f" | bu_order_id: {bu_order_id_nuevo}" if bu_order_id_nuevo else "")
        + (f" | Duración: {tiempo_min_nuevo} min" if tiempo_min_nuevo else "")
        + (f" | Peor punto (MAE): {peor_pct_nuevo:+.2f}%" if peor_pct_nuevo is not None else "")
    )


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
        # 07/08 (FIX): esto llamaba a db.obj_diario_real_db() directo con
        # gestion_riesgo.CAPITAL_TOTAL_USD (el fijo viejo, 782) — un camino
        # de código duplicado que quedó sin el fix que sí se aplicó en
        # main.py:obj_diario(). Ahora usa la misma lógica correcta: capital
        # real del día si ya está disponible, con el mismo fallback.
        cap_diario = db.obtener_capital_diario()
        capital_hoy = cap_diario["capital_dia"] if cap_diario else gestion_riesgo.CAPITAL_TOTAL_USD
        obj = db.obj_diario_real_db(gestion_riesgo.OBJETIVO_DIARIO_PCT, capital_hoy)
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


def _generar_imagen_rendimiento(resumen: dict) -> bytes:
    """
    03/08 — Genera una imagen (tarjetas) con el rendimiento diario/semanal/
    mensual en % sobre el capital de inicio de CADA período — no identifica
    operaciones individuales, es un resumen visual para descargar.
    """
    ahora = datetime.now(db.TZ_ARG)
    W, H = 1000, 900
    BG, CARD = (18, 20, 28), (28, 31, 42)
    VERDE, ROJO, BLANCO, GRIS = (76, 217, 123), (240, 90, 90), (235, 237, 242), (150, 155, 168)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_titulo = ImageFont.load_default(size=44)
    f_fecha = ImageFont.load_default(size=24)
    f_label = ImageFont.load_default(size=28)
    f_pct = ImageFont.load_default(size=64)
    f_usd = ImageFont.load_default(size=26)
    f_small = ImageFont.load_default(size=20)

    d.text((50, 40), "📊 JJ Cripto Bot", font=f_titulo, fill=BLANCO)
    d.text((50, 100), ahora.strftime("%A %d/%m/%Y, %H:%M") + " (ARG)", font=f_fecha, fill=GRIS)

    labels = {"diario": "DIARIO (hoy)", "semanal": "SEMANAL (desde el lunes)", "mensual": "MENSUAL (desde el día 1)"}
    y, card_h = 170, 220
    for clave in ("diario", "semanal", "mensual"):
        p = resumen[clave]
        pct = p["resultado_pct"]
        color = VERDE if (pct or 0) >= 0 else ROJO
        signo = "+" if (pct or 0) >= 0 else ""

        d.rounded_rectangle([50, y, W - 50, y + card_h - 20], radius=20, fill=CARD)
        d.text((80, y + 25), labels[clave], font=f_label, fill=GRIS)

        pct_txt = f"{signo}{pct:.2f}%" if pct is not None else "s/d"
        d.text((80, y + 65), pct_txt, font=f_pct, fill=color)

        usd = p["resultado_usd"]
        signo_usd = "+" if usd >= 0 else ""
        d.text((420, y + 95), f"{signo_usd}{usd:.2f} USD", font=f_usd, fill=BLANCO)

        cap = p["capital_inicio"]
        cap_txt = f"USD {cap:.2f}" if cap is not None else "s/d"
        d.text((80, y + 150), f"Capital de inicio: {cap_txt}", font=f_small, fill=GRIS)
        d.text((520, y + 150), f"✅ {p['ganadas']}   ❌ {p['perdidas']}", font=f_small, fill=GRIS)

        y += card_h

    d.text((50, H - 50), "JJ Cripto Bot v16", font=f_small, fill=GRIS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _cmd_rendimiento() -> str:
    """
    03/08 — Manda una imagen descargable con el rendimiento diario/semanal/
    mensual, cada uno en % sobre el capital REAL de inicio de ese período
    (no el capital total fijo). Diario: desde hoy 00:00. Semanal: desde el
    lunes. Mensual: desde el día 1. No detalla operaciones individuales.
    """
    resumen = db.resumen_rendimiento()
    try:
        imagen = _generar_imagen_rendimiento(resumen)
    except Exception as e:
        return f"⚠️ No pude generar la imagen de rendimiento ({e})."

    d_ = resumen["diario"]
    caption = (
        f"📊 <b>Rendimiento</b>\n"
        f"Diario: {d_['resultado_pct']:+.2f}%" if d_["resultado_pct"] is not None else "📊 <b>Rendimiento</b>"
    )
    resultado = enviar_imagen(imagen, "rendimiento.png", caption=caption)
    if not resultado or not resultado.get("ok"):
        return "⚠️ Falló el envío de la imagen a Telegram — revisá los logs de Railway."
    return ""  # ya se mandó la imagen, no hace falta texto adicional


def _cmd_simuladas() -> str:
    """
    03/08 — Resumen de las señales SIMULADAS (calificaron pero no
    consiguieron lugar real por el tope de 2 posiciones). Compara su
    comportamiento contra las operaciones reales, sin arriesgar capital.
    """
    abiertas = db.operaciones_simuladas_abiertas()
    resumen = db.resumen_simuladas()
    if not resumen.get("total_cerradas") and not abiertas:
        return "🧪 Sin señales simuladas todavía (recién se activaron el 03/08)."

    lineas = ["🧪 <b>Señales simuladas</b> (sin capital real)", f"Abiertas ahora: {len(abiertas)}"]
    if resumen.get("total_cerradas"):
        lineas.append(
            f"\nCerradas: {resumen['total_cerradas']} | Win rate: {resumen['win_rate_pct']}%\n"
            f"✅ {resumen['ganadas']}  ❌ {resumen['perdidas']}\n"
            f"MAE promedio: {resumen['mae_promedio']}% | MFE promedio: {resumen['mfe_promedio']}%"
        )
    else:
        lineas.append("\nTodavía ninguna cerró.")
    return "\n".join(lineas)


def _cmd_paxg() -> str:
    """
    04/08 — Resumen del cinturón separado PAXG/BTC (modo sombra, 24
    combinaciones: 3 señales x 2 riesgos x 4 TP). Sin capital real todavía.
    """
    abiertas = db.paxg_simulaciones_abiertas()
    resumen = db.resumen_paxg_simulaciones()
    if not resumen and not abiertas:
        return "🥇 Cinturón PAXG/BTC: sin datos todavía (recién se activó el 04/08)."

    lineas = [f"🥇 <b>Cinturón PAXG/BTC</b> (modo sombra, 30 días de prueba)", f"Combinaciones abiertas ahora: {len(abiertas)}"]
    if resumen:
        # 05/08: total real de ganadas/perdidas en TODAS las combinaciones,
        # no solo las que entran en el top 10 del ranking de abajo.
        total_cerradas = sum(r["n"] for r in resumen)
        total_ganadas = sum(round(r["n"] * r["win_rate_pct"] / 100) for r in resumen if r["win_rate_pct"] is not None)
        total_perdidas = total_cerradas - total_ganadas
        lineas.append(f"\nTotal cerradas (todas las combinaciones): {total_cerradas} | ✅ {total_ganadas}  ❌ {total_perdidas}")

        lineas.append("\n<b>Ranking por combinación</b> (mejor a peor, top 10):")
        for r in resumen[:10]:
            lineas.append(
                f"{r['combinacion']}: {r['resultado_promedio_pct']:+.2f}% prom | "
                f"win {r['win_rate_pct']}% | n={r['n']}"
            )
        if len(resumen) > 10:
            lineas.append(f"... y {len(resumen)-10} combinaciones más (no se muestran acá, pero SÍ están en el total de arriba).")
    else:
        lineas.append("\nTodavía ninguna combinación cerró.")
    return "\n".join(lineas)


def _cmd_bingx() -> str:
    """
    05/08 — Resumen del cinturón de INVESTIGACIÓN BingX (order book
    imbalance). Muestra el % de acierto real por umbral, calculado con
    datos propios — para elegir el umbral óptimo, no a ciegas. Modo
    sombra puro, sin operar nada.
    """
    resumen = db.resumen_umbral_imbalance()
    if not resumen or all(r["n_1m"] == 0 for r in resumen):
        return "📡 Cinturón BingX (investigación): sin datos suficientes todavía."

    lineas = ["📡 <b>Cinturón BingX — Order Book Imbalance</b> (investigación, modo sombra)",
              "Acierto real por umbral (dirección predicha vs. lo que hizo el precio):\n"]
    for r in resumen:
        if r["n_1m"] == 0:
            continue
        linea = f"Umbral {r['umbral']}: 1min {r['acierto_1m_pct']}% (n={r['n_1m']})"
        if r["n_5m"]:
            linea += f" | 5min {r['acierto_5m_pct']}% (n={r['n_5m']})"
        lineas.append(linea)
    n_minimo = min(r["n_1m"] for r in resumen if r["n_1m"] > 0)
    if n_minimo < 200:
        lineas.append(f"\n⚠️ El umbral con menos casos tiene n={n_minimo} — todavía no es confiable, ojo con sobre-interpretar.")
    else:
        lineas.append(f"\n✅ Todos los umbrales mostrados tienen n≥{n_minimo} — muestra sólida, no son casualidad.")
    return "\n".join(lineas)


def _cmd_capital() -> str:
    """
    10/08 — Muestra el capital objetivo fijado para HOY (interés
    compuesto) y cuánta reserva de recupero se usó — para diagnosticar
    casos donde 2 operaciones del mismo día muestran montos distintos.
    """
    cap = db.obtener_capital_diario()
    if not cap:
        return "💰 Capital diario: todavía no corrió el recálculo de hoy (puede estar pospuesto por operaciones abiertas a las 00:01)."

    resultado_hoy = db.resultado_acumulado_usd_hoy()
    reserva_usada = cap["reserva_inicial"] - cap["reserva_restante"]

    return (
        f"💰 <b>Capital del día</b> ({cap['fecha']})\n"
        f"Capital real (00:01): USD {cap['capital_dia']:.2f}\n"
        f"Tamaño objetivo por operación: USD {cap['tamano_objetivo']:.2f}\n"
        f"Reserva de recupero: USD {cap['reserva_inicial']:.2f} inicial | "
        f"USD {reserva_usada:.2f} usada | USD {cap['reserva_restante']:.2f} disponible\n"
        f"Resultado acumulado hoy: {resultado_hoy:+.2f} USD "
        f"({'día en negativo, puede estar usando reserva' if resultado_hoy < 0 else 'día en positivo, tamaño completo sin reserva'})"
    )


def _cmd_estrategias_imbalance() -> str:
    """
    11/08 — Calcula retroactivamente (con los datos ya guardados, sin
    esperar más días) el resultado de 5 combinaciones NO-martingala:
    apuesta fija, sin doblar, comisión real de BingX (0.10% ida+vuelta)
    ya descontada. Resultado en % de precio, independiente del
    apalancamiento que se elija.
    """
    resumen = db.resumen_estrategias_imbalance()
    if not resumen or all(r["n"] == 0 for r in resumen):
        return "📊 Estrategias de imbalance (sin martingala): sin datos suficientes todavía."

    lineas = ["📊 <b>5 estrategias direccionales</b> (sin martingala, apuesta fija)",
              "Comisión BingX ya descontada (0.10% ida+vuelta):\n"]
    for r in resumen:
        if r["n"] == 0:
            lineas.append(f"{r['nombre']}: sin datos")
            continue
        lineas.append(
            f"<b>{r['nombre']}</b>\n"
            f"n={r['n']} | bruto (sin comisión): {r.get('bruto_prom_pct', 0):+.4f}% | "
            f"neto: {r['retorno_prom_pct']:+.4f}% | win rate: {r['win_rate_pct']}%"
        )
    return "\n".join(lineas)


def _cmd_martingala() -> str:
    """
    10/08 — Resumen del cinturón BingX-martingala en modo sombra (2
    variantes: A=imbalance fresco en cada trade, B=guion fijo del video).
    Sin capital real todavía.
    """
    abiertas_a = db.secuencias_martingala_abiertas("A")
    abiertas_b = db.secuencias_martingala_abiertas("B")
    resumen = db.resumen_martingala()

    if not resumen and not abiertas_a and not abiertas_b:
        return "🎲 Cinturón BingX-martingala: sin datos todavía."

    lineas = ["🎲 <b>BingX-martingala</b> (modo sombra, sin capital real)",
              f"Abiertas ahora: A={len(abiertas_a)} | B={len(abiertas_b)}\n"]
    for variante in ("A", "B"):
        d = resumen.get(variante)
        if not d:
            lineas.append(f"<b>Variante {variante}</b>: sin secuencias cerradas todavía")
            continue
        nombre = "imbalance fresco" if variante == "A" else "guion fijo"
        lineas.append(
            f"<b>Variante {variante}</b> ({nombre}): {d['n']} secuencias | "
            f"✅ {d['ganadas']} ganadas / ❌ {d['ruinas']} ruinas ({d['win_rate_pct']}% win)\n"
            f"Resultado neto: {d['resultado_neto_usd']:+.2f} USD | Profundidad promedio: {d['profundidad_promedio']}"
        )

    # 11/08: capital persistente — 2 formas de llevar la cuenta sobre los
    # mismos resultados (500 puro vs. 1000 con reserva que repone a $500).
    tracks = db.resumen_capital_tracks()
    lineas.append("\n💰 <b>Capital persistente</b> (sobre los mismos resultados de arriba):")
    for variante in ("A", "B"):
        t500 = tracks[f"{variante}_500"]
        t1000 = tracks[f"{variante}_1000"]
        lineas.append(
            f"{variante}_500: USD {t500['capital_activo']:.2f} activos (sin red)\n"
            f"{variante}_1000: USD {t1000['capital_activo']:.2f} activos | "
            f"USD {t1000['reserva_disponible']:.2f} reserva | repuesto {t1000.get('veces_repuesto', 0)}x"
        )
    return "\n".join(lineas)


def _cmd_filtros() -> str:
    """
    07/08 — Resumen de los 6 filtros en modo sombra (multi-tf, ADX,
    volumen, VWAP, CCI, OBV): cuántas señales aprobó/rechazó cada uno, y
    el resultado promedio real de las señales que cerraron, separado por
    aprobó=Sí vs. aprobó=No.
    """
    r = db.resumen_sombra()
    if r.get("total", 0) == 0:
        return "🔬 Filtros en sombra: sin datos suficientes todavía."

    lineas = [f"🔬 <b>Filtros en sombra</b> — {r['total']} señales registradas\n"]
    for filtro in ("multi_tf", "adx_gate", "volumen", "vwap", "cci", "obv"):
        d = r.get(filtro, {})
        ap, re = d.get("aprobo", {}), d.get("rechazo", {})
        lineas.append(
            f"<b>{filtro}</b>: aprobó {ap.get('n_total',0)} (prom {ap.get('prom')}%, n con resultado={ap.get('n_con_resultado',0)}) | "
            f"rechazó {re.get('n_total',0)} (prom {re.get('prom')}%, n con resultado={re.get('n_con_resultado',0)})"
        )
    return "\n".join(lineas)


def _cmd_griddinamico() -> str:
    """07/08 — Resumen del grid dinámico en modo sombra (regla DGT, rebote confirmado)."""
    r = db.resumen_grid_dinamico()
    if r.get("total", 0) == 0:
        return "📐 Grid dinámico: sin datos suficientes todavía."
    return (
        f"📐 <b>Grid dinámico</b> (modo sombra)\n"
        f"Chequeos totales: {r['total_chequeos']}\n"
        f"Cerca del borde: {r['cerca_del_borde_n']}\n"
        f"Con rebote confirmado: {r['rebote_confirmado_n']}\n"
        f"Hubiera ajustado: {r['hubiera_ajustado_n']}\n"
        f"Pares afectados: {', '.join(r['pares_afectados']) if r['pares_afectados'] else '—'}"
    )


def _cmd_mae() -> str:
    """
    07/08 — Resumen de MAE/MFE + motivo de cierre, todo junto (para no
    tener que correr 3 comandos separados). Incluye aviso sobre el fix
    del 07/08 (datos de antes de esa fecha pueden estar subestimados).
    """
    mae = db.resumen_mae()
    mfe = db.resumen_mfe()
    motivos = db.resumen_por_motivo_cierre()

    if mae.get("total", 0) == 0 and mfe.get("total", 0) == 0:
        return "📉 MAE/MFE: sin datos suficientes todavía."

    lineas = ["📉 <b>MAE / MFE / motivo de cierre</b>\n"]
    if mae.get("total"):
        lineas.append(
            f"<b>MAE</b> (peor punto alcanzado) — {mae['total_con_dato']} operaciones con dato "
            f"({'✅ confiable' if mae['confiable'] else '⚠️ muestra chica, no confiable todavía'})\n"
            f"Ganadoras: peor caso {mae['mae_ganadoras_peor']}%, promedio {mae['mae_ganadoras_promedio']}%\n"
            f"Perdedoras: promedio {mae['mae_perdedoras_promedio']}%"
        )
    if mfe.get("total"):
        lineas.append(
            f"\n<b>MFE</b> (mejor punto alcanzado) — {mfe['total_con_dato']} operaciones "
            f"({'✅ confiable' if mfe['confiable'] else '⚠️ muestra chica'})\n"
            f"Eficiencia de captura promedio: {mfe['eficiencia_captura_promedio']}\n"
            f"Casi llegaron al TP y terminaron perdiendo: {mfe['n_casi_tp_pero_termino_perdiendo']}"
        )
    if motivos:
        lineas.append("\n<b>Por motivo de cierre:</b>")
        for motivo, d in motivos.items():
            lineas.append(f"  {motivo}: n={d['n']} | tiempo prom {d['tiempo_prom_min']}min | resultado prom {d['resultado_prom_pct']}%")

    lineas.append("\n⚠️ Datos de ANTES del 07/08 pueden estar subestimados en operaciones con posición grande acumulada (ver fix crítico de fórmula).")
    return "\n".join(lineas)


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


def _cmd_escanear(args: list) -> str:
    """
    28/07 — Escaneo manual bajo demanda: corre el MISMO análisis que usa
    el bot automático (analizar_par de main.py) sobre TODOS los pares,
    y devuelve el ranking actual — SIN abrir ni registrar nada. Pensado
    para elegir manualmente una operación de alta convicción (ej. para
    compensar pérdidas con capital aparte), no reemplaza el flujo
    automático normal.

    OJO: tarda un buen rato en correr (recorre ~80 pares uno por uno
    contra Bybit/OKX/Binance) — no te preocupes si no responde al toque.

    Uso: /escanear [cantidad] (default 5)
    """
    top_n = int(_parse_float(args[0])) if args and _parse_float(args[0]) else 5

    try:
        import main as bot_main
    except Exception as e:
        return f"⚠️ No pude cargar el módulo de análisis: {e}"

    try:
        btc = bot_main.analizar_btc()
    except Exception as e:
        return f"⚠️ Error analizando BTC: {e}"

    resultados = []
    errores = 0
    for par in bot_main.PARES:
        try:
            r = bot_main.analizar_par(par, btc)
            if r:
                resultados.append(r)
        except Exception:
            errores += 1
            continue

    if not resultados:
        return (
            f"⚠️ Ningún par calificó ahora mismo (score≥11 de 16).\n"
            f"BTC: {btc['emoji']} {btc['resumen']}\n"
            f"({errores} pares fallaron al consultar, de {len(bot_main.PARES)} totales)"
        )

    resultados.sort(key=lambda x: x["score"], reverse=True)
    top = resultados[:top_n]

    lineas = [f"🔍 <b>Escaneo manual</b> — BTC: {btc['emoji']} {btc['resumen']}", ""]
    for r in top:
        lineas.append(
            f"<b>{r['par']}</b> {r['direccion']} | Score: {r['score']}/{r['score_max']}\n"
            f"  Precio: {r['precio']:.6g} | Rango sugerido: {r['rango_bajo']}–{r['rango_alto']} "
            f"({r['rango_pct']}%, {r['grillas']} grillas)\n"
            f"  {' | '.join(r['razones'][:4])}"
        )
    lineas.append(
        f"\n({len(resultados)} de {len(bot_main.PARES)} pares calificaron con score≥11"
        + (f", {errores} fallaron al consultar" if errores else "") + ")"
    )
    lineas.append("\n⚠️ Esto NO es una garantía de resultado — es el mismo ranking que usa el bot, para que decidas vos.")
    return "\n\n".join(lineas)


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
    elif cmd == "/corregir":
        return _cmd_corregir(args)
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
    elif cmd == "/rendimiento":
        return _cmd_rendimiento()
    elif cmd == "/simuladas":
        return _cmd_simuladas()
    elif cmd == "/paxg":
        return _cmd_paxg()
    elif cmd == "/bingx":
        return _cmd_bingx()
    elif cmd == "/estrategias_imbalance":
        return _cmd_estrategias_imbalance()
    elif cmd == "/martingala":
        return _cmd_martingala()
    elif cmd == "/capital":
        return _cmd_capital()
    elif cmd == "/filtros":
        return _cmd_filtros()
    elif cmd == "/griddinamico":
        return _cmd_griddinamico()
    elif cmd == "/mae":
        return _cmd_mae()
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
    elif cmd == "/escanear":
        return _cmd_escanear(args)
    elif cmd in ("/ayuda", "/help", "/start"):
        return (
            "🤖 <b>Comandos disponibles</b>\n\n"
            "/registrar PAR APAL RANGO_BAJO RANGO_ALTO GRILLAS\n"
            "  Anotá lo que Pionex te ofreció al crear el bot.\n"
            "  Ej: /registrar ALGO 10 0.395 0.410 120\n\n"
            "/cerrar PAR RESULTADO_PCT\n"
            "  Anotá el resultado final cuando cerrás el bot en Pionex.\n"
            "  Ej: /cerrar ALGO -11.95\n\n"
            "/corregir PAR RESULTADO_PCT [abierta|cerrada] [capital] [bu_order_id]\n"
            "  🩹 Arregla una señal con datos falsos (ej: el bot la marcó\n"
            "  cerrada por error, pero en Pionex sigue abierta).\n"
            "  Ej: /corregir MOVE -24.8 abierta 81.48\n\n"
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
            "/rendimiento\n"
            "  📊 Imagen descargable: % diario/semanal/mensual sobre el\n"
            "  capital de inicio de cada período (sin detalle por operación).\n\n"
            "/simuladas\n"
            "  🧪 Resumen de señales que calificaron pero no consiguieron\n"
            "  lugar real (sin capital) — MAE/MFE/win rate simulado.\n\n"
            "/paxg\n"
            "  🥇 Cinturón PAXG/BTC en modo sombra — ranking de las 24\n"
            "  combinaciones (señal x riesgo x TP) probadas en paralelo.\n\n"
            "/bingx\n"
            "  📡 Cinturón de investigación BingX (order book imbalance) —\n"
            "  % de acierto real por umbral, sin operar nada todavía.\n\n"
            "/estrategias_imbalance\n"
            "  📊 5 estrategias direccionales (sin martingala) calculadas\n"
            "  retroactivamente con los datos ya guardados, comisión incluida.\n\n"
            "/martingala\n"
            "  🎲 Cinturón BingX-martingala en modo sombra — 2 variantes\n"
            "  (imbalance fresco vs. guion fijo), sin capital real.\n\n"
            "/capital\n"
            "  💰 Capital objetivo del día e info de la reserva de recupero\n"
            "  — para entender por qué 2 operaciones del mismo día variaron.\n\n"
            "/filtros\n"
            "  🔬 Resumen de los 6 filtros en modo sombra (multi-tf, ADX,\n"
            "  volumen, VWAP, CCI, OBV) — aprobó/rechazó y resultado real.\n\n"
            "/griddinamico\n"
            "  📐 Resumen del grid dinámico en modo sombra (regla DGT).\n\n"
            "/mae\n"
            "  📉 MAE/MFE + motivo de cierre, todo junto.\n\n"
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
            "  📊 Manda un CSV con TODO el historial (abrí con Excel).\n\n"
            "/escanear [cantidad]\n"
            "  🔍 Busca a demanda las mejores candidatas ahora mismo (no abre nada).\n"
            "  Tarda un rato (recorre ~80 pares). Ej: /escanear 5"
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
