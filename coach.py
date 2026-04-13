import os
import asyncio
import logging
from datetime import date, timedelta
from telegram import Bot, Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import anthropic
import garminconnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

GARMIN_EMAIL       = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD    = os.environ["GARMIN_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
CHAT_ID            = "6385304051"

SYSTEM_PROMPT = """Sos un coach de running experto, especializado en preparación para maratón.
Tu atleta se llama Agukarsa. Sus datos de base:
- Objetivo: correr la Maratón de Buenos Aires el 20/09/2026 en menos de 3:30 (ritmo objetivo: 4:58/km)
- Volumen actual: ~45 km/semana, con plan de ir aumentando progresivamente
- Días de entrenamiento: 6-7 días por semana
- Sin lesiones previas relevantes
- La carrera está a 23 semanas a partir de abril 2026

Tu estilo de comunicación:
- Directo, motivador pero realista
- Usá lenguaje en español rioplatense (vos, che, etc.)
- Mensajes concisos — máximo 200 palabras por mensaje
- Siempre terminá con UNA acción concreta para el día o mañana
- Usá emojis con moderación (1-2 por mensaje máximo)

Cuando analizás datos de entrenamiento, prestá especial atención a:
1. HRV y calidad de sueño para evaluar recuperación
2. Pace a zona 2 para ver progreso aeróbico
3. Carga de entrenamiento semanal vs semanas anteriores
4. Señales de sobreentrenamiento (FC elevada, HRV baja, rendimiento caído)

Fases del plan:
- Sem 1-6 (ahora): Base aeróbica — volumen moderado, ritmo fácil
- Sem 7-12: Desarrollo específico — tempo runs, intervalos
- Sem 13-16: Peak — rodajes largos de 28-35km, simulacros de maratón
- Sem 17-18: Tapering — reducción de volumen, frescura para la carrera"""


def get_garmin_data():
    try:
        garmin = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        garmin.login()
        logger.info("Conectado a Garmin OK")
        today     = str(date.today())
        yesterday = str(date.today() - timedelta(days=1))
        data = {}
        try:
            activities = garmin.get_activities(0, 3)
            if activities:
                data["ultima_actividad"] = {
                    "nombre":    activities[0].get("activityName", ""),
                    "tipo":      activities[0].get("activityType", {}).get("typeKey", ""),
                    "distancia": round(activities[0].get("distance", 0) / 1000, 2),
                    "duracion":  round(activities[0].get("duration", 0) / 60, 1),
                    "fc_media":  activities[0].get("averageHR", 0),
                    "fc_max":    activities[0].get("maxHR", 0),
                    "carga":     activities[0].get("activityTrainingLoad", 0),
                    "fecha":     activities[0].get("startTimeLocal", "")[:10],
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener actividad: {e}")
        try:
            hrv = garmin.get_hrv_data(today)
            if hrv:
                data["hrv"] = {
                    "hrv_nocturno": hrv.get("hrvSummary", {}).get("lastNight", 0),
                    "estado":       hrv.get("hrvSummary", {}).get("status", ""),
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener HRV: {e}")
        try:
            sleep = garmin.get_sleep_data(yesterday)
            if sleep:
                sd = sleep.get("dailySleepDTO", {})
                data["sueno"] = {
                    "duracion_hs": round(sd.get("sleepTimeSeconds", 0) / 3600, 1),
                    "score":       sd.get("sleepScores", {}).get("overall", {}).get("value", 0),
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener sueño: {e}")
        try:
            week_start = str(date.today() - timedelta(days=date.today().weekday()))
            week_acts  = garmin.get_activities_by_date(week_start, today, "running")
            if week_acts:
                data["semana_actual"] = {
                    "km_totales": round(sum(a.get("distance", 0) for a in week_acts) / 1000, 1),
                    "sesiones":   len(week_acts),
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener stats semanales: {e}")
        return data
    except Exception as e:
        logger.error(f"Error conectando a Garmin: {e}")
        return None


def llamar_claude(mensajes: list) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=mensajes
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Error con Claude: {e}")
        return "No pude procesar eso ahora. Intentá de nuevo en un momento."


def generar_mensaje_programado(datos_garmin: dict, tipo: str) -> str:
    if tipo == "manana":
        instruccion = f"""Es la mañana. Analizá estos datos de Garmin y generá el mensaje del coach para arrancar el día.
Datos: {datos_garmin}
Incluí: estado de recuperación (HRV + sueño), recomendación para el entrenamiento de hoy, y motivación."""
    else:
        instruccion = f"""Es la noche. Analizá estos datos de Garmin y generá el resumen del día.
Datos: {datos_garmin}
Incluí: análisis del entrenamiento de hoy (si hubo), progreso semanal, y consejo para mañana."""
    return llamar_claude([{"role": "user", "content": instruccion}])


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    user_text = update.message.text
    logger.info(f"Mensaje recibido: {user_text}")
    datos = get_garmin_data()
    contexto_garmin = f"\n\nDatos actuales de Garmin del atleta: {datos}" if datos else ""
    respuesta = llamar_claude([
        {"role": "user", "content": f"{user_text}{contexto_garmin}"}
    ])
    await update.message.reply_text(respuesta, parse_mode=ParseMode.MARKDOWN)


async def tarea_programada(bot: Bot, tipo: str):
    datos = get_garmin_data()
    mensaje = generar_mensaje_programado(datos, tipo) if datos else f"No pude conectarme a Garmin esta {'mañana' if tipo == 'manana' else 'noche'}."
    try:
        await bot.send_message(chat_id=CHAT_ID, text=mensaje, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Tarea {tipo} enviada OK")
    except Exception as e:
        logger.error(f"Error enviando tarea {tipo}: {e}")


async def scheduler_loop(bot: Bot):
    """Scheduler async — corre dentro del mismo event loop que el bot."""
    import datetime
    logger.info("Scheduler activo: 9:30AM y 19:00 hora Argentina")
    manana_enviada = False
    noche_enviada  = False

    while True:
        # Hora actual en Argentina (UTC-3)
        ahora_utc = datetime.datetime.utcnow()
        ahora_arg = ahora_utc - datetime.timedelta(hours=3)
        hora = ahora_arg.hour
        minuto = ahora_arg.minute
        dia = ahora_arg.date()

        # Reset diario a medianoche
        if hora == 0 and minuto == 0:
            manana_enviada = False
            noche_enviada  = False

        # 9:30 AM Argentina
        if hora == 9 and minuto >= 30 and not manana_enviada:
            logger.info("Enviando mensaje de la mañana...")
            await tarea_programada(bot, "manana")
            manana_enviada = True

        # 19:00 Argentina
        if hora == 19 and minuto == 0 and not noche_enviada:
            logger.info("Enviando mensaje de la noche...")
            await tarea_programada(bot, "noche")
            noche_enviada = True

        await asyncio.sleep(60)


async def main():
    logger.info("Coach de maratón iniciado. Objetivo: Sub 3:30 en BSAS 20/09/2026")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "✅ *Coach listo — escribime cuando quieras*\n\n"
            "Hola Agukarsa, el chat bidireccional está activo.\n\n"
            "Ejemplos:\n"
            "— _¿Cómo estoy de recuperación hoy?_\n"
            "— _¿Hago el tempo run o descanso?_\n"
            "— _¿Cómo voy en relación al plan?_\n\n"
            "Análisis automáticos: *9:30 AM* y *19:00* 💪"
        ),
        parse_mode=ParseMode.MARKDOWN
    )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message"], drop_pending_updates=True)

    logger.info("Bot escuchando mensajes entrantes...")

    # Correr scheduler en el mismo event loop
    await scheduler_loop(bot)


if __name__ == "__main__":
    asyncio.run(main())
