import os
import asyncio
import logging
import schedule
import time
from datetime import date, timedelta
from telegram import Bot
from telegram.constants import ParseMode
import anthropic
import garminconnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Configuración ──────────────────────────────────────────────────────────────
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

# ── Funciones Garmin ───────────────────────────────────────────────────────────

def get_garmin_data():
    """Conecta a Garmin y obtiene datos del día actual y ayer."""
    try:
        garmin = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        garmin.login()
        logger.info("Conectado a Garmin OK")

        today     = str(date.today())
        yesterday = str(date.today() - timedelta(days=1))

        data = {}

        # Última actividad
        try:
            activities = garmin.get_activities(0, 3)
            if activities:
                data["ultima_actividad"] = {
                    "nombre":    activities[0].get("activityName", ""),
                    "tipo":      activities[0].get("activityType", {}).get("typeKey", ""),
                    "distancia": round(activities[0].get("distance", 0) / 1000, 2),
                    "duracion":  round(activities[0].get("duration", 0) / 60, 1),
                    "pace_medio": activities[0].get("averageSpeed", 0),
                    "fc_media":  activities[0].get("averageHR", 0),
                    "fc_max":    activities[0].get("maxHR", 0),
                    "carga":     activities[0].get("activityTrainingLoad", 0),
                    "fecha":     activities[0].get("startTimeLocal", "")[:10],
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener actividad: {e}")

        # HRV
        try:
            hrv = garmin.get_hrv_data(today)
            if hrv:
                data["hrv"] = {
                    "hrv_nocturno": hrv.get("hrvSummary", {}).get("lastNight", 0),
                    "hrv_5min":     hrv.get("hrvSummary", {}).get("lastNight5MinHigh", 0),
                    "estado":       hrv.get("hrvSummary", {}).get("status", ""),
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener HRV: {e}")

        # Sueño
        try:
            sleep = garmin.get_sleep_data(yesterday)
            if sleep:
                sd = sleep.get("dailySleepDTO", {})
                data["sueno"] = {
                    "duracion_hs":  round(sd.get("sleepTimeSeconds", 0) / 3600, 1),
                    "profundo_hs":  round(sd.get("deepSleepSeconds", 0) / 3600, 1),
                    "score":        sd.get("sleepScores", {}).get("overall", {}).get("value", 0),
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener sueño: {e}")

        # Estado de entrenamiento
        try:
            ts = garmin.get_training_status(today)
            if ts:
                data["estado_entrenamiento"] = ts
        except Exception as e:
            logger.warning(f"No se pudo obtener training status: {e}")

        # Estadísticas semanales
        try:
            week_start = str(date.today() - timedelta(days=date.today().weekday()))
            week_acts  = garmin.get_activities_by_date(week_start, today, "running")
            if week_acts:
                km_semana = sum(a.get("distance", 0) for a in week_acts) / 1000
                data["semana_actual"] = {
                    "km_totales":    round(km_semana, 1),
                    "sesiones":      len(week_acts),
                }
        except Exception as e:
            logger.warning(f"No se pudo obtener stats semanales: {e}")

        return data

    except Exception as e:
        logger.error(f"Error conectando a Garmin: {e}")
        return None


# ── Funciones Claude ───────────────────────────────────────────────────────────

def generar_mensaje_coach(datos_garmin: dict, tipo: str) -> str:
    """Llama a Claude para generar el mensaje del coach."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    if tipo == "manana":
        instruccion = f"""Es la mañana. Analizá estos datos de Garmin y generá el mensaje del coach para arrancar el día.
Datos: {datos_garmin}
El mensaje debe incluir: estado de recuperación (HRV + sueño), recomendación para el entrenamiento de hoy, y motivación."""
    else:
        instruccion = f"""Es la noche. Analizá estos datos de Garmin y generá el resumen del día.
Datos: {datos_garmin}
El mensaje debe incluir: análisis del entrenamiento de hoy (si hubo), progreso semanal, y consejo para mañana."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": instruccion}]
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Error con Claude: {e}")
        return "No pude generar el análisis hoy. Revisá los logs en Railway."


# ── Envío a Telegram ───────────────────────────────────────────────────────────

async def enviar_telegram(mensaje: str):
    """Envía un mensaje al bot de Telegram."""
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=CHAT_ID,
            text=mensaje,
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info("Mensaje enviado a Telegram OK")
    except Exception as e:
        logger.error(f"Error enviando a Telegram: {e}")


# ── Tareas programadas ─────────────────────────────────────────────────────────

def tarea_manana():
    logger.info("Ejecutando tarea de la mañana...")
    datos = get_garmin_data()
    if datos:
        mensaje = generar_mensaje_coach(datos, "manana")
    else:
        mensaje = "No pude conectarme a Garmin esta mañana. Revisá las credenciales en Railway."
    asyncio.run(enviar_telegram(mensaje))


def tarea_noche():
    logger.info("Ejecutando tarea de la noche...")
    datos = get_garmin_data()
    if datos:
        mensaje = generar_mensaje_coach(datos, "noche")
    else:
        mensaje = "No pude conectarme a Garmin esta noche. Revisá las credenciales en Railway."
    asyncio.run(enviar_telegram(mensaje))


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Coach de maratón iniciado. Objetivo: Sub 3:30 en BSAS 20/09/2026")

    # Mensaje de bienvenida al arrancar
    async def bienvenida():
        await enviar_telegram(
            "✅ *Coach activo*\n\nHola Agukarsa, tu coach de maratón está corriendo.\n"
            "Vas a recibir análisis a las *7:00 AM* y a las *21:00*.\n\n"
            "Objetivo: Sub 3:30 · Maratón BSAS · 20/09/2026 💪"
        )
    asyncio.run(bienvenida())

    # Programar las dos tareas diarias (hora UTC — Argentina es UTC-3)
    schedule.every().day.at("10:00").do(tarea_manana)   # 9:30 AM Argentina
    schedule.every().day.at("00:00").do(tarea_noche)    # 16:00 Argentina

    logger.info("Scheduler activo: 9:30AM y 19:00 hora Argentina")

    while True:
        schedule.run_pending()
        time.sleep(60)
