from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.mongodb import MongoDBJobStore
from apscheduler.schedulers.background import BackgroundScheduler
import random
import requests
import logging
import ollama
import asyncio
from ollama import GenerateResponse
from Levenshtein import distance as levenshtein_distance
import re
from bson import ObjectId

# ------------------------------
# Configuración básica
# ------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)
    client.server_info()
    logger.info("Conexión a MongoDB establecida")
    db = client["chatbot_db"]
    test_col = db["test"]
    test_col.update_one({"_id": "test"}, {"$set": {"data": "test"}}, upsert=True)
    logger.info("Prueba de escritura en MongoDB exitosa")
except Exception as e:
    logger.error(f"Error al conectar con MongoDB o escribir en la base de datos: {e}", exc_info=True)
    raise Exception("No se pudo conectar a MongoDB o escribir en la base de datos")

db = client["chatbot_db"]
cache_col = db["cache"]
sesiones_col = db["sesiones"]
bitacora_col = db["bitacora"]
asesores_col = db["asesores"]
sends_col = db["sends"]
assignments_col = db["assignments"]

# Configuración del scheduler con MongoDBJobStore
scheduler = AsyncIOScheduler({
    'jobstores': {
        'default': MongoDBJobStore(database='chatbot_db', collection='scheduler_jobs', client=client)
    }
})

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacán"
TIEMPO_RESPUESTA_EJECUTIVO = 300  # 5 minutos
MODELOS_RESPALDO = [
    "Polo", "Saveiro", "Teramont", "Amarok Panamericana", "Transporter 6.1",
    "Nivus", "Taos", "T-Cross", "Virtus", "Jetta", "Tiguan", "Jetta GLI",
    "GTI", "Amarok Life", "Amarok Style", "Amarok Aventura", "Cross Sport",
    "Crafter", "Caddy"
]

# ------------------------------
# Pydantic models
# ------------------------------
class Mensaje(BaseModel):
    cliente_id: str
    texto: str
    audio_path: str = None

class AdvisorResponse(BaseModel):
    cliente_id: str
    respuesta: str
    asesor_phone: str

# ------------------------------
# Funciones para autos
# ------------------------------
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Content-Type": "application/x-www-form-urlencoded"
}

def normalizar_modelo(modelo):
    """Normaliza nombres de modelos eliminando prefijos y corrigiendo errores."""
    if not modelo:
        return None
    modelo = modelo.lower().strip()
    # Eliminar prefijos innecesarios
    for prefix in ["volkswagen", "nuevo", "nueva"]:
        modelo = modelo.replace(prefix, "").strip()
    # Corregir errores conocidos
    modelo = re.sub(r'\s*\(\d{4}\)', '', modelo).strip()
    correcciones = {
        "tera": "Teramont",
        "taigun": "Tiguan",
        "golf gti": "GTI",
        "jetta gli": "Jetta GLI",
        "q5": "Q5",
        "a3": "A3",
        "onix": "Onix",
        "eclipse": "Eclipse"
    }
    for error, correcto in correcciones.items():
        if error in modelo:
            modelo = modelo.replace(error, correcto.lower())
    return modelo.title()

def obtener_autos_nuevos(force_refresh=False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_nuevos"})
        logger.info(f"Cache encontrado para autos_nuevos: {cache}")
        if cache and not force_refresh and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            logger.info("Usando caché para autos nuevos")
            return cache.get("data", [])
        url = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload = {"r": "cargaAutosTodos", "x": "0.123456789"} 
        res = requests.post(url, data=payload, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()

        #logger.info(f"Respuesta de la API (autos nuevos): {data}")
        modelos_unicos = set()   # aquí evitamos duplicados
        modelos = []
        for auto in data:
            modelo = auto.get("modelo")
            if modelo and modelo not in modelos_unicos:
                modelos_unicos.add(modelo)
                modelos.append(modelo)
        # Guardamos en cache sin duplicados
        cache_col.update_one(
            {"_id": "autos_nuevos"},
            {"$set": {"data": modelos, "ts": ahora}},
            upsert=True
        )
        logger.info(f"Autos nuevos obtenidos: {modelos}")
        return modelos
    except Exception as e:
        logger.error(f"Error al obtener autos nuevos: {e}", exc_info=True)
        return MODELOS_RESPALDO

def obtener_autos_usados(force_refresh=False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_usados"})
        logger.info(f"Cache encontrado para autos_usados: {cache}")
        if cache and not force_refresh and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            logger.info("Usando caché para autos usados")
            return cache.get("data", [])
        url = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers_usados = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": headers["User-Agent"],
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://vw-eurocity.com.mx",
            "Referer": "https://vw-eurocity.com.mx/Seminuevos/"
        }
        payload = {"r": "CheckDist"}
        #logger.info(f"Enviando solicitud a {url} con payload {payload}")
        res = requests.post(url, headers=headers_usados, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        #logger.info(f"Respuesta de la API (autos usados): {data}")
        modelos = list({f"{auto.get('Modelo')} ({auto.get('Anio')})" for auto in data.get("LiAutos", []) if auto.get("Modelo")})
        if not modelos:
            logger.warning("No se obtuvieron modelos usados, usando respaldo")
            modelos = []
            vistos = set()
            for auto in data.get("LiAutos", []):
                modelo = auto.get("Modelo")
                anio = auto.get("Anio")
                # clave única: modelo+año
                clave = f"{modelo}-{anio}"
                if modelo and anio and clave not in vistos:
                    vistos.add(clave)
                    modelos.append(f"{modelo} ({anio})")
        cache_col.update_one(
            {"_id": "autos_usados"},
            {"$set": {"data": modelos, "ts": ahora}},
            upsert=True
        )
        logger.info(f"Autos usados obtenidos: {modelos}")
        return modelos
    except Exception as e:
        logger.error(f"Error al obtener autos usados: {e}", exc_info=True)
        return MODELOS_RESPALDO

# ------------------------------
# Sesiones y bitácora
# ------------------------------
def obtener_sesion(cliente_id):
    try:
        sesion = sesiones_col.find_one({"cliente_id": cliente_id}) or {}
        logger.info(f"Sesión recuperada para {cliente_id}: {sesion}")
        return sesion
    except Exception as e:
        logger.error(f"Error al obtener sesión para {cliente_id}: {e}", exc_info=True)
        return {}

def guardar_sesion(cliente_id, sesion):
    try:
        sesion["cliente_id"] = cliente_id
        sesion["ts"] = datetime.utcnow()
        result = sesiones_col.update_one({"cliente_id": cliente_id}, {"$set": sesion}, upsert=True)
        logger.info(f"Sesión guardada para {cliente_id}: {result.modified_count} modificados, {result.upserted_id} upserted")
    except Exception as e:
        logger.error(f"Error al guardar sesión para {cliente_id}: {e}", exc_info=True)
        raise

def guardar_bitacora(registro):
    try:
        registro["fecha_completa"] = datetime.utcnow()
        bitacora_col.insert_one(registro)
        logger.info(f"Bitácora guardada: {registro}")
    except Exception as e:
        logger.error(f"Error al guardar bitácora: {e}", exc_info=True)

# ------------------------------
# Limpieza de asignaciones obsoletas
# ------------------------------
async def cleanup_stale_assignments(client_id):
    try:
        now = datetime.utcnow()
        result = assignments_col.update_many(
            {
                "client_id": client_id,
                "status": "pending_availability",
                "sent_time": {"$lt": now - timedelta(minutes=TIEMPO_RESPUESTA_EJECUTIVO)}
            },
            {"$set": {"status": "timeout", "response_time": now}}
        )
        if result.modified_count > 0:
            logger.info(f"Limpieza: {result.modified_count} asignaciones obsoletas marcadas como timeout para {client_id}")
            guardar_bitacora({
                "event": "cleanup_stale_assignments",
                "client_id": client_id,
                "count": result.modified_count,
                "time": now
            })
    except Exception as e:
        logger.error(f"Error en cleanup_stale_assignments para {client_id}: {e}", exc_info=True)
        guardar_bitacora({
            "event": "error_cleanup_assignments",
            "client_id": client_id,
            "error": str(e),
            "time": datetime.utcnow()
        })

# ------------------------------
# Asignación de ejecutivos
# ----------------------
@app.get("/get_asesores")
async def get_asesores():
    try:
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "nombre": 1, "_id": 0}))
        for advisor in asesores:
            if not advisor["telefono"].startswith("521"):
                advisor["telefono"] = f"521{advisor['telefono']}"
        return [{"telefono": a["telefono"], "nombre": a.get("nombre", "Asesor Desconocido")} for a in asesores if "telefono" in a]
    except Exception as e:
        logger.error(f"Error al obtener asesores: {e}", exc_info=True)
        return []

async def send_to_next_advisor(client_id):
    try:
        # Limpiar asignaciones obsoletas antes de asignar
        await cleanup_stale_assignments(client_id)
        sesion = obtener_sesion(client_id)
        logger.info(f"Sesión para {client_id}: {sesion}")
        if "nombre" not in sesion or "tipo_auto" not in sesion or "modelo" not in sesion:
            logger.warning(f"Sesión incompleta para {client_id}: {sesion}")
            sends_col.insert_one({
                "jid": client_id,
                "message": f"{sesion.get('nombre', 'Cliente')}, por favor proporciona toda la información necesaria.",
                "sent": False,
                "sent_time": datetime.utcnow()
            })
            guardar_bitacora({
                "event": "incomplete_session",
                "client_id": client_id,
                "session": sesion,
                "time": datetime.utcnow()
            })
            return False
        active_advisors = await get_asesores()
        assigned_advisors = sesion.get("assigned_advisors", [])
        logger.info(f"Asesores asignados para {client_id}: {assigned_advisors}")
        logger.info(f"Asesores activos disponibles: {active_advisors}")
        next_advisor = next((advisor for advisor in active_advisors if advisor["telefono"] not in assigned_advisors), None)
        if next_advisor:
            assigned_advisors.append(next_advisor["telefono"])
            sesion["assigned_advisors"] = assigned_advisors
            sesion["asesor_nombre"] = next_advisor["nombre"]
            guardar_sesion(client_id, sesion)
            advisor_jid = next_advisor["telefono"]
            if not advisor_jid.startswith("521"):
                advisor_jid = f"521{advisor_jid}"
            advisor_jid = f"{advisor_jid}@s.whatsapp.net"
            logger.info(f"Generando jid para asesor: {advisor_jid}")
            # Preguntar solo por disponibilidad
            message = f"Hola {next_advisor['nombre']}, ¿estás disponible para atender a un cliente ahora?"
            sends_col.insert_one({
                "jid": advisor_jid,
                "message": message,
                "buttons": [
                    {"buttonId": f"yes_{client_id}", "buttonText": {"displayText": "Sí"}, "type": 1},
                    {"buttonId": f"no_{client_id}", "buttonText": {"displayText": "No"}, "type": 1}
                ],
                "sent": False,
                "sent_time": datetime.utcnow(),
                "client_id": client_id
            })
            assignment_id = assignments_col.insert_one({
                "client_id": client_id,
                "advisor_phone": next_advisor["telefono"],
                "advisor_name": next_advisor["nombre"],
                "sent_time": datetime.utcnow(),
                "status": "pending_availability"
            }).inserted_id
            # Guardar en bitácora la pregunta de disponibilidad
            guardar_bitacora({
                "event": "asked_availability",
                "client_id": client_id,
                "advisor_phone": next_advisor["telefono"],
                "advisor_name": next_advisor["nombre"],
                "ask_time": datetime.utcnow(),
                "message": message,
                "assignment_id": str(assignment_id)
            })
            
            logger.info(f"Programando timeout para cliente {client_id}, asesor {next_advisor['telefono']} en {TIEMPO_RESPUESTA_EJECUTIVO} segundos")
            scheduler.add_job(
                check_timeout,
                'date',
                run_date=datetime.utcnow() + timedelta(seconds=TIEMPO_RESPUESTA_EJECUTIVO),
                args=[client_id, next_advisor["telefono"], str(assignment_id)],
                id=f"timeout_{client_id}_{next_advisor['telefono']}",
                replace_existing=True
            )
            logger.info(f"Pregunta de disponibilidad enviada a {next_advisor['nombre']} ({advisor_jid}) para cliente {client_id}")
            return True
        else:
            logger.warning(f"No hay asesores disponibles para {client_id}")
            sends_col.insert_one({
                "jid": client_id,
                "message": f"{sesion['nombre']}, lo siento, no hay ejecutivos disponibles ahora. Por favor, intenta de nuevo más tarde.",
                "sent": False,
                "sent_time": datetime.utcnow()
            })
            # Guardar en bitácora que no hay asesores disponibles
            guardar_bitacora({
                "event": "no_advisors_available",
                "client_id": client_id,
                "time": datetime.utcnow()
            })
            return False
    except Exception as e:
        logger.error(f"Error en send_to_next_advisor para {client_id}: {e}", exc_info=True)
        sends_col.insert_one({
            "jid": client_id,
            "message": f"{sesion.get('nombre', 'Cliente')}, lo siento, ocurrió un error al asignar un ejecutivo. Por favor, intenta de nuevo.",
            "sent": False,
            "sent_time": datetime.utcnow()
        })
        # Guardar en bitácora el error
        guardar_bitacora({
            "event": "error_assigning_advisor",
            "client_id": client_id,
            "error": str(e),
            "time": datetime.utcnow()
        })
        return False

async def check_timeout(client_id, advisor_phone, assignment_id):
    try:
        logger.info(f"Verificando timeout para cliente {client_id}, asesor {advisor_phone}, assignment_id {assignment_id}")
        assignment = assignments_col.find_one({
            "_id": ObjectId(assignment_id),
            "client_id": client_id,
            "advisor_phone": advisor_phone,
            "status": "pending_availability"
        })
        logger.info(f"Asignación encontrada: {assignment}")
        if not assignment:
            logger.warning(f"No se encontró asignación pendiente para cliente {client_id}, asesor {advisor_phone}, assignment_id {assignment_id}")
            all_assignments = list(assignments_col.find({"client_id": client_id}))
            logger.info(f"Todas las asignaciones para {client_id}: {all_assignments}")
            guardar_bitacora({
                "event": "timeout_check_failed",
                "client_id": client_id,
                "advisor_phone": advisor_phone,
                "assignment_id": assignment_id,
                "error": "No se encontró asignación pendiente",
                "time": datetime.utcnow()
            })
            return
        now = datetime.utcnow()
        time_elapsed = (now - assignment["sent_time"]).total_seconds()
        logger.info(f"Tiempo transcurrido para {client_id} con asesor {advisor_phone}: {time_elapsed} segundos")
        if time_elapsed >= TIEMPO_RESPUESTA_EJECUTIVO:
            assignments_col.update_one(
                {"_id": ObjectId(assignment_id)},
                {"$set": {"status": "timeout", "response_time": now}}
            )
            logger.info(f"Marcando asignación {assignment_id} como timeout")
            guardar_bitacora({
                "event": "advisor_timeout",
                "client_id": client_id,
                "advisor_phone": advisor_phone,
                "advisor_name": assignment["advisor_name"],
                "timeout_time": now,
                "original_ask_time": assignment["sent_time"],
                "assignment_id": str(assignment_id)
            })
            logger.info(f"Timeout para {advisor_phone} con cliente {client_id}, intentando siguiente asesor")
            await send_to_next_advisor(client_id)
        else:
            logger.info(f"Tiempo no alcanzado para {client_id} con {advisor_phone}, tiempo restante: {TIEMPO_RESPUESTA_EJECUTIVO - time_elapsed} segundos")
            scheduler.add_job(
                check_timeout,
                'date',
                run_date=datetime.utcnow() + timedelta(seconds=TIEMPO_RESPUESTA_EJECUTIVO - time_elapsed),
                args=[client_id, advisor_phone, str(assignment_id)],
                id=f"timeout_{client_id}_{advisor_phone}",
                replace_existing=True
            )
            logger.info(f"Reprogramado timeout para cliente {client_id}, asesor {advisor_phone} en {TIEMPO_RESPUESTA_EJECUTIVO - time_elapsed} segundos")
    except Exception as e:
        logger.error(f"Error en check_timeout para {client_id}, asesor {advisor_phone}, assignment_id {assignment_id}: {e}", exc_info=True)
        guardar_bitacora({
            "event": "error_timeout_check",
            "client_id": client_id,
            "advisor_phone": advisor_phone,
            "assignment_id": assignment_id,
            "error": str(e),
            "time": datetime.utcnow()
        })

@app.post("/advisor_response")
async def advisor_response(req: AdvisorResponse):
    try:
        cliente_id = req.cliente_id
        respuesta = req.respuesta.lower()
        asesor_phone = req.asesor_phone
        assignment = assignments_col.find_one({"client_id": cliente_id, "advisor_phone": asesor_phone, "status": "pending_availability"})
        if not assignment:
            logger.warning(f"No se encontró asignación pendiente para cliente {cliente_id} y asesor {asesor_phone}")
            guardar_bitacora({
                "event": "advisor_response_failed",
                "client_id": cliente_id,
                "advisor_phone": asesor_phone,
                "error": "No se encontró asignación pendiente",
                "time": datetime.utcnow()
            })
            return {"texto": "Asignación no encontrada"}
        now = datetime.utcnow()
        # Guardar en bitácora la respuesta del asesor
        guardar_bitacora({
            "event": "advisor_availability_response",
            "client_id": cliente_id,
            "advisor_phone": asesor_phone,
            "advisor_name": assignment["advisor_name"],
            "response": respuesta,
            "response_time": now,
            "original_ask_time": assignment["sent_time"],
            "assignment_id": str(assignment["_id"])
        })
        if respuesta == "yes":
            sesion = obtener_sesion(cliente_id)
            assignments_col.update_one({"_id": assignment["_id"]}, {"$set": {"status": "accepted", "response_time": now}})
            # Enviar información del cliente al asesor
            advisor_jid = asesor_phone if asesor_phone.startswith("521") else f"521{asesor_phone}"
            advisor_jid = f"{advisor_jid}@s.whatsapp.net"
            client_summary = (
                f"Cliente: {sesion['nombre']} busca {sesion['tipo_auto']} {sesion['modelo']}, "
                f"contacto: {cliente_id}. Asesor asignado: {assignment['advisor_name']}"
            )
            sends_col.insert_one({
                "jid": advisor_jid,
                "message": client_summary,
                "sent": False,
                "sent_time": datetime.utcnow()
            })
            # Guardar en bitácora el envío de información del cliente
            guardar_bitacora({
                "event": "client_info_sent",
                "client_id": cliente_id,
                "advisor_phone": asesor_phone,
                "advisor_name": assignment["advisor_name"],
                "sent_time": now,
                "message": client_summary,
                "assignment_id": str(assignment["_id"])
            })
            # Registrar asignación final
            info_cliente = {
                "nombre": sesion["nombre"],
                "tipo_auto": sesion["tipo_auto"],
                "modelo": sesion["modelo"],
                "contacto": cliente_id,
                "ejecutivo": assignment["advisor_name"],
                "fecha": now.strftime("%Y-%m-%d"),
                "hora": now.strftime("%H:%M:%S")
            }
            guardar_bitacora({
                "event": "client_assigned",
                "client_id": cliente_id,
                "advisor_phone": asesor_phone,
                "advisor_name": assignment["advisor_name"],
                "assignment_time": now,
                "client_info": info_cliente,
                "assignment_id": str(assignment["_id"])
            })
            client_message = (
                f"{sesion['nombre']}, tu interés en el {sesion['tipo_auto']} {sesion['modelo']} está registrado. "
                f"El ejecutivo {assignment['advisor_name']} te contactará pronto."
            )
            sends_col.insert_one({
                "jid": cliente_id,
                "message": client_message,
                "sent": False,
                "sent_time": datetime.utcnow()
            })
            sesion["modelo_confirmado"] = True
            guardar_sesion(cliente_id, sesion)
        else:  # Respuesta "no" u otra
            assignments_col.update_one({"_id": assignment["_id"]}, {"$set": {"status": "declined", "response_time": now}})
            logger.info(f"Asesor {asesor_phone} no disponible, intentando siguiente asesor para {cliente_id}")
            await send_to_next_advisor(cliente_id)
        return {"texto": "Respuesta registrada"}
    except Exception as e:
        logger.error(f"Error en advisor_response: {e}", exc_info=True)
        # Guardar en bitácora el error
        guardar_bitacora({
            "event": "error_advisor_response",
            "client_id": cliente_id,
            "advisor_phone": asesor_phone,
            "error": str(e),
            "time": datetime.utcnow()
        })
        return {"texto": "Error al procesar la respuesta del asesor"}


# ------------------------------
# Generación de respuesta con Ollama
# ------------------------------
def generar_respuesta_ollama(prompt, contexto_sesion=None, es_primer_mensaje=False, expected_response=None, buttons=None):
    try:
        system_prompt = (
            f"Eres {BOT_NOMBRE}, un asistente de {AGENCIA}. Tu objetivo es guiar al cliente de manera amigable, natural y concisa para elegir un auto. "
            "Sigue estrictamente este flujo conversacional: "
            "1) Si no tienes el nombre del cliente, responde SOLO: '¡Bienvenido(a) a Volkswagen Eurocity Culiacán! 😊 ¿Me puedes proporcionar tu nombre, por favor?' "
            "   Acepta nombres compuestos (e.g., 'Rafael Lopez') si son razonables. No avances sin un nombre válido (solo letras, mínimo 3 caracteres, sin palabras comunes como 'que', 'rollo', 'hola', 'nuevo', 'usado', 'auto', 'coche', 'vehículo', 'quiero', 'busco', 'sí', 'si', 'no', 'gracias', 'teramont', 'q5', 'a3', 'onix', 'eclipse'). "
            "   Si el nombre no es válido, responde SOLO: 'Disculpa, no entendí tu nombre. ¿Me dices cómo te llamas?' "
            "2) Si ya tienes el nombre, responde SOLO: '{nombre}, ¿buscas un auto nuevo o usado?' No avances al siguiente paso sin una respuesta clara ('nuevo' o 'usado'). "
            "3) Si ya tienes el tipo de auto, muestra los modelos con: '{nombre}, estos son los modelos disponibles: {modelos}. ¿Cuál te interesa?' "
            "   Para autos nuevos, usa SOLO modelos Volkswagen. Para autos usados, incluye todos los modelos disponibles, incluso de otras marcas, según el inventario proporcionado. "
            "   No uses emojis ni exclamaciones iniciales en esta respuesta ni en las siguientes. "
            "4) Si el cliente selecciona un modelo, pide confirmación con: '{nombre}, ¿confirmas que quieres el modelo {modelo}? Si prefieres otro, dime cuál.' "
            #"5) Tras confirmar el modelo (con 'sí', 'si', 'yes', 'confirm', 'asi es', 'así es', 'okey', 'ok'), responde SOLO: '{nombre}, tu interés en el modelo {modelo} está registrado. Un ejecutivo te contactará pronto.' "
            "5) Tras confirmar el modelo (con cualquier respuesta que indique confirmación como 'sí', 'si', 'yes', 'confirmo', 'claro que sí', 'sii ese', 'ok'), responde SOLO: '{nombre}, tu interés en el modelo {modelo} está registrado. Un ejecutivo te contactará pronto.' "
            "Responde SOLO en español, de forma directa, amigable y profesional. Usa siempre el nombre completo proporcionado por el cliente (e.g., 'Rafael Lopez Gamez'). "
            "Evita CUALQUIER frase técnica, redundante o exagerada como '(esperando el nombre)', 'Recuerda que solo letras', 'proporciona un nombre válido', 'excelente elección', 'absolutamente', 'sí!', '¡no!', 'me alegra ayudarte', 'auto perfecto', o 'necesito conocerte mejor'. "
            "No uses emojis ni exclamaciones iniciales (e.g., '¡{nombre}, ...') en ninguna respuesta después del mensaje inicial. "
            "Si el cliente selecciona un modelo no disponible, responde SOLO: '{nombre}, lo siento, ese modelo no está disponible. Estos son los modelos disponibles: {modelos}. ¿Cuál te interesa?' "
            "Si el cliente dice 'no' al confirmar un modelo, responde SOLO: '{nombre}, ¿cuál modelo prefieres? Estos son los disponibles: {modelos}.' "
            "Si el cliente expresa frustración (e.g., 'ya te dije', 'ya dije', 'no me ha contactado','no me han contactado', 'nadie me ha contactado', 'no me han atendido', 'ya paso rato', '🙃', '🙄'), discúlpate y retoma el último paso: "
            "   - Si tiene nombre, tipo de auto y modelo confirmado, responde: '{nombre}, disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. ¿Algo más en lo que pueda ayudarte?' "
            "   - Si tiene nombre y tipo de auto, muestra los modelos: '{nombre}, disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. Estos son los modelos disponibles: {modelos}. ¿Cuál te interesa?' "
            "   - Si tiene solo el nombre, pregunta: '{nombre}, disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. ¿Buscas un auto nuevo o usado?' "
            "   - Si no tiene nada, pregunta: 'Disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. ¿Me puedes proporcionar tu nombre, por favor?' "
            "Si el cliente pregunta por 'documentos', 'requisitos' o 'papeles', responde SOLO: '{nombre}, para comprar tu {modelo} necesitas: 1) Identificación oficial (INE o pasaporte), 2) Comprobante de domicilio (máximo 3 meses), 3) Comprobantes de ingresos (3 últimos recibos de nómina o estados de cuenta), 4) Solicitud de crédito (si aplica). Un ejecutivo te dará más detalles. ¿Algo más en lo que pueda ayudarte?' "
            "Si el cliente pide 'hablar con un ejecutivo', verifica si tienes su nombre; if not, respond: '¡Bienvenido(a) a Volkswagen Eurocity Culiacán! 😊 ¿Me puedes proporcionar tu nombre, por favor?' Then, respond ONLY: '{nombre}, un ejecutivo te contactará pronto. ¿Algo más en lo que pueda ayudarte?' "
            "If the client says 'gracias', 'no, gracias' or similar after confirming a model, respond ONLY: 'De nada, {nombre}. Pronto uno de nuestros ejecutivos se pondrá en contacto contigo.' "
            "If the client sends greetings (e.g., 'hola', 'hi') after confirming a model, respond ONLY: 'Hola {nombre}. Tu interés en el modelo {modelo} está registrado. Un ejecutivo te contactará pronto. ¿Algo más en lo que pueda ayudarte?' "
            "Si el cliente pregunta 'cuál es el nombre del asesor?', 'cuál es el nombre del ejecutivo?', 'en qué tanto tiempo me contactarán?' o 'en cuanto tiempo?', responde SOLO: "
            "   - Para 'cuál es el nombre del asesor?' o 'ejecutivo': '{nombre}, no tengo el nombre del asesor asignado aún, ya que se determina cuando un ejecutivo esté disponible. Te informaré cuando te contacten.' "
            "   - Para 'en qué tanto tiempo me contactarán?' o 'en cuanto tiempo?': '{nombre}, te contactarán lo antes posible, generalmente dentro de unos 5 a 10 minutos una vez que un asesor se desocupe.' "
        )
        if es_primer_mensaje:
            system_prompt += "\nUsa SOLO este mensaje inicial: '¡Bienvenido(a) a Volkswagen Eurocity Culiacán! Soy {BOT_NOMBRE} 😊 ¿Me puedes proporcionar tu nombre, por favor?'"
        if contexto_sesion:
            system_prompt += f"\nContexto actual: {contexto_sesion}"
        full_prompt = f"{system_prompt}\n\nMensaje del cliente: {prompt}"
        logger.info(f"Enviando prompt a Ollama: {full_prompt}")
        resp = ollama.generate(model="llama3", prompt=full_prompt)
        if isinstance(resp, GenerateResponse):
            respuesta = str(resp.response).strip()
        elif isinstance(resp, dict) and 'response' in resp:
            respuesta = str(resp['response']).strip()
        else:
            logger.error(f"Respuesta de Ollama no válida: tipo={type(resp)}, contenido={resp}")
            return expected_response if expected_response else "Disculpa, algo salió mal. Por favor, intenta de nuevo.", buttons or []
        if not respuesta:
            logger.warning("Ollama devolvió una respuesta vacía")
            return expected_response if expected_response else "Disculpa, algo salió mal. Por favor, intenta de nuevo.", buttons or []
        if expected_response and respuesta != expected_response:
            logger.warning(f"Ollama response '{respuesta}' does not match expected '{expected_response}'")
            return expected_response, buttons or []
        logger.info(f"Respuesta procesada de Ollama: {respuesta}")
        return respuesta, buttons or []
    except Exception as e:
        logger.error(f"Error al comunicarse con Ollama: {e}", exc_info=True)
        return expected_response if expected_response else "Disculpa, algo salió mal. Por favor, intenta de nuevo.", buttons or []

# ------------------------------
# Webhook operativo
# ----------------------
@app.post("/webhook")
async def webhook(req: Mensaje):
    cliente_id = req.cliente_id
    texto = req.texto.strip() if req.texto and isinstance(req.texto, str) else ""
    sesion = obtener_sesion(cliente_id)

    try:
        # Reiniciar sesión si han pasado más de 24 horas
        if sesion.get("ts") and (datetime.utcnow() - sesion["ts"]) > timedelta(hours=24):
            logger.info(f"Sesión antigua detectada para {cliente_id}, reiniciando")
            sesion = {}
            guardar_sesion(cliente_id, sesion)

        logger.info(f"Procesando mensaje para cliente {cliente_id}: {texto}, Sesión: {sesion}")

        # Manejar mensajes post-confirmación
        if sesion.get("modelo_confirmado"):
            logger.info(f"Sesión ya confirmada para {cliente_id}: {sesion}")
            if any(keyword in texto.lower() for keyword in ["documentos", "requisitos", "papeles"]):
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} ha preguntado por documentos necesarios para la compra del modelo {sesion['modelo']}."
                expected_response = (
                    f"{sesion.get('nombre', 'Cliente')}, para comprar tu {sesion['modelo']} necesitas: "
                    "1) Identificación oficial (INE o pasaporte), "
                    "2) Comprobante de domicilio (máximo 3 meses), "
                    "3) Comprobantes de ingresos (3 últimos recibos de nómina o estados de cuenta), "
                    "4) Solicitud de crédito (si aplica). "
                    "Un ejecutivo te dará más detalles. ¿Algo más en lo que pueda ayudarte?"
                )
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif any(keyword in texto.lower() for keyword in ["no me ha contactado", "nadie me ha contactado", "no me han atendido", "no me han contactado"]):
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} expresó que no ha recibido atención después de confirmar el modelo {sesion['modelo']}."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. ¿Algo más en lo que pueda ayudarte?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif any(keyword in texto.lower() for keyword in ["gracias", "no, gracias", "ok", "de nada", "okey"]):
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} dijo '{texto}' después de confirmar el modelo {sesion['modelo']}."
                expected_response = f"De nada, {sesion.get('nombre', 'Cliente')}. Un ejecutivo te contactará pronto."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif texto.lower() in ["hola", "hi", "buenas"]:
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} envió un saludo después de confirmar el modelo {sesion['modelo']}."
                expected_response = f"Hola {sesion.get('nombre', 'Cliente')}. Tu interés en el modelo {sesion['modelo']} está registrado. Un ejecutivo te contactará pronto. ¿Algo más en lo que pueda ayudarte?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif any(keyword in texto.lower() for keyword in ["cuál es el nombre del asesor", "cuál es el nombre del ejecutivo"]):
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} preguntó por el nombre del asesor después de confirmar el modelo {sesion['modelo']}."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, no tengo el nombre del asesor asignado aún, ya que se determina cuando un ejecutivo esté disponible. Te informaré cuando te contacten."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif any(keyword in texto.lower() for keyword in ["en qué tanto tiempo me contactarán", "en cuanto tiempo"]):
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} preguntó por el tiempo de contacto después de confirmar el modelo {sesion['modelo']}."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, te contactarán lo antes posible, generalmente dentro de unos 5 a 10 minutos una vez que un asesor se desocupe."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            else:
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} ya confirmó el modelo {sesion['modelo']}. Responde amigablemente."
                expected_response = f"Hola {sesion.get('nombre', 'Cliente')}. Tu interés en el modelo {sesion['modelo']} está registrado. Un ejecutivo te contactará pronto. ¿Algo más en lo que pueda ayudarte?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}

        # Manejar solicitud de hablar con un ejecutivo
        if "hablar con un ejecutivo" in texto.lower() or "ejecutivo" in texto.lower():
            if "nombre" not in sesion:
                contexto = "El cliente pidió hablar con un ejecutivo pero no ha proporcionado un nombre. Pide el nombre de forma amigable."
                expected_response = f"¡Bienvenido(a) a {AGENCIA}! 😊 ¿Me puedes proporcionar tu nombre, por favor?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, True, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            else:
                await send_to_next_advisor(cliente_id)
                sesion["modelo_confirmado"] = True
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} pidió hablar con un ejecutivo."
                expected_response = f"{sesion['nombre']}, un ejecutivo te contactará pronto. ¿Algo más en lo que pueda ayudarte?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}

        # Manejar frustración del cliente
        if any(frase in texto.lower() for frase in ["ya te dije", "ya dije", "te dije", "no me ha contactado", "nadie me ha contactado", "no me han atendido", "🙄"]):
            if "nombre" in sesion and "tipo_auto" in sesion and "modelo" in sesion and sesion.get("modelo_confirmado"):
                contexto = f"El cliente {sesion['nombre']} expresó frustración porque no ha sido contactado después de confirmar el modelo {sesion['modelo']}."
                expected_response = f"{sesion['nombre']}, disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. ¿Algo más en lo que pueda ayudarte?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif "nombre" in sesion and "tipo_auto" in sesion:
                modelos = sesion.get("modelos", obtener_autos_nuevos() if sesion["tipo_auto"] == "nuevo" else obtener_autos_usados())
                contexto = f"El cliente {sesion['nombre']} expresó frustración y ya seleccionó tipo_auto {sesion['tipo_auto']}. Muestra los modelos disponibles."
                expected_response = f"{sesion['nombre']}, disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. Estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, modelos[:5])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif "nombre" in sesion:
                contexto = f"El cliente {sesion['nombre']} expresó frustración y ya proporcionó su nombre. Pregunta por el tipo de auto."
                expected_response = f"{sesion['nombre']}, disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. ¿Buscas un auto nuevo o usado?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Nuevo", "Usado"])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            else:
                contexto = "El cliente expresó frustración, pero no ha proporcionado su nombre. Pide el nombre de forma amigable."
                expected_response = f"Disculpa la demora. Nuestros ejecutivos se encuentran en llamada y en cuanto se desocupen te atenderán. Tu atención es prioritaria para nosotros. ¿Me puedes proporcionar tu nombre, por favor?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, True, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}

        # Manejar saludos iniciales
        if texto.lower() in ["hola", "hi", "buenas"]:
            if "nombre" not in sesion:
                contexto = "El cliente ha iniciado la conversación con un saludo. Pide su nombre de forma amigable."
                expected_response = f"¡Bienvenido(a) a {AGENCIA}! 😊 ¿Me puedes proporcionar tu nombre, por favor?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, True, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif "tipo_auto" not in sesion:
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero no ha seleccionado tipo_auto. Pregunta por el tipo de auto."
                expected_response = f"{sesion['nombre']}, ¿buscas un auto nuevo o usado?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Nuevo", "Usado"])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif "modelo" not in sesion:
                modelos = sesion.get("modelos", obtener_autos_nuevos() if sesion["tipo_auto"] == "nuevo" else obtener_autos_usados())
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero ya seleccionó tipo_auto {sesion['tipo_auto']}. Muestra los modelos disponibles."
                expected_response = f"{sesion['nombre']}, estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, modelos[:5])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            else:
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero ya seleccionó el modelo {sesion['modelo']}. Pide confirmación."
                expected_response = f"{sesion['nombre']}, ¿confirmas que quieres el modelo {sesion['modelo']}? Si prefieres otro, dime cuál."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Sí", "Cambiar modelo"])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}

        # Manejar nombre
        if "nombre" not in sesion:
            nombre_valido = None
            texto_lower = texto.lower()
            frases_comunes = [
                r"mi nombre es", r"claro que sí", r"claro que si", r"soy", r"me llamo", r"mi nombre",
                r"claro", r"ok", r"de acuerdo", r"no", r"que no"
            ]
            nombre_candidato = texto_lower
            for frase in frases_comunes:
                nombre_candidato = re.sub(frase, "", nombre_candidato, flags=re.IGNORECASE)
            nombre_candidato = " ".join(nombre_candidato.split()).strip()
            if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑ\s]{3,}$', nombre_candidato) and nombre_candidato.lower() not in [
                "nuevo", "usado", "sí", "si", "no", "gracias", "teramont", "q5", "a3", "onix", "eclipse",
                "que", "hola", "hi", "buenas"
            ]:
                palabras = nombre_candidato.split()
                if len(palabras) >= 1:
                    nombre_valido = " ".join(palabras).title()
                else:
                    match = re.search(r'(?:mi nombre es|soy|me llamo)\s+([a-zA-ZáéíóúÁÉÍÓÚñÑ\s]+)', texto_lower, re.IGNORECASE)
                    if match:
                        nombre_candidato = match.group(1).strip()
                        if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑ\s]{3,}$', nombre_candidato) and nombre_candidato.lower() not in [
                            "nuevo", "usado", "sí", "si", "no", "gracias", "teramont", "q5", "a3", "onix", "eclipse"
                        ]:
                            nombre_valido = nombre_candidato.title()
            if nombre_valido:
                sesion["nombre"] = nombre_valido
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente ha proporcionado su nombre: {sesion['nombre']}. Pregunta por el tipo de auto."
                expected_response = f"{sesion['nombre']}, ¿buscas un auto nuevo o usado?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Nuevo", "Usado"])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            else:
                # Después de un intento fallido, pasar a preguntar por el interés de compra
                sesion["nombre_intento_fallido"] = sesion.get("nombre_intento_fallido", 0) + 1
                guardar_sesion(cliente_id, sesion)
                if sesion.get("nombre_intento_fallido", 0) > 1:
                    contexto = "El cliente no proporcionó un nombre válido después de varios intentos. Pregunta por el interés de compra."
                    expected_response = "No has proporcionado un nombre válido. ¿Buscas un auto nuevo o usado? Nota que necesitarás dar tu nombre para que un asesor pueda comunicarse contigo."
                    respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Nuevo", "Usado"])
                    logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                    return {"texto": respuesta, "botones": botones}
                contexto = "El cliente no ha proporcionado un nombre válido. Pide el nombre de forma amigable."
                expected_response = f"Disculpa, no entendí tu nombre. ¿Me dices cómo te llamas?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, True, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}

        # Manejar tipo de auto
        if "tipo_auto" not in sesion:
            if texto.lower() in ["nuevo", "usado"]:
                sesion["tipo_auto"] = texto.lower()
                modelos = obtener_autos_nuevos() if sesion["tipo_auto"] == "nuevo" else obtener_autos_usados()
                if not modelos:
                    logger.error(f"No se encontraron modelos para tipo_auto {sesion['tipo_auto']}")
                    contexto = f"No se pudieron obtener modelos de autos {sesion['tipo_auto']}. Informa al cliente y sugiere reintentar."
                    expected_response = f"{sesion.get('nombre', 'Cliente')}, lo siento, no tenemos la lista de modelos disponible ahora. ¿Quieres intentar de nuevo?"
                    respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Reintentar"])
                    logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                    return {"texto": respuesta, "botones": botones}
                sesion["modelos"] = modelos
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} ha seleccionado tipo_auto {texto}. Muestra los modelos disponibles."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, modelos[:5])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            else:
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} no ha especificado si quiere un auto nuevo o usado. Pregunta de forma clara."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, ¿buscas un auto nuevo o usado?"
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Nuevo", "Usado"])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}

        # Manejar selección de modelo
        tipo = sesion["tipo_auto"]
        modelos = sesion.get("modelos", obtener_autos_nuevos() if tipo == "nuevo" else obtener_autos_usados())
        if not modelos:
            contexto = f"No se pudieron obtener modelos de autos {tipo}. Informa al cliente y sugiere reintentar o contactar a un ejecutivo."
            expected_response = f"{sesion.get('nombre', 'Cliente')}, lo siento, no tenemos la lista de modelos disponible ahora. ¿Quieres intentar de nuevo o prefieres hablar con un ejecutivo?"
            respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Reintentar", "Hablar con ejecutivo"])
            logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
            return {"texto": respuesta, "botones": botones}

        # Confirmación de modelo
        if "modelo" in sesion and not sesion.get("modelo_confirmado"):
            texto_lower = texto.lower().strip()
            confirmaciones = [
                "sí", "si", "yes", "confirmo", "claro que sí", "sii ese", "ok", "okey", "así es", "asi es",
                "si confirmo", "sí confirmo", "confirm", "vale", "está bien", "sí, ese"
            ]
            if any(conf in texto_lower or levenshtein_distance(texto_lower, conf) <= 2 for conf in confirmaciones):
                if "nombre" not in sesion:
                    contexto = "El cliente confirmó un modelo pero no proporcionó un nombre. Explica la necesidad del nombre."
                    expected_response = "Has confirmado un modelo, pero no has proporcionado tu nombre. Es necesario dar tu nombre para que un asesor pueda comunicarse contigo. ¿Me dices cómo te llamas?"
                    respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                    logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                    return {"texto": respuesta, "botones": botones}
                await send_to_next_advisor(cliente_id)
                sesion["modelo_confirmado"] = True
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} ha confirmado el modelo {sesion['modelo']}."
                expected_response = f"{sesion['nombre']}, tu interés en el modelo {sesion['modelo']} está registrado. Un ejecutivo te contactará pronto."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, [])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif texto_lower in ["no", "cambiar modelo", "cambiar", "otras opciones"]:
                sesion.pop("modelo", None)
                sesion.pop("modelo_confirmado", None)
                modelos = obtener_autos_nuevos() if tipo == "nuevo" else obtener_autos_usados()
                sesion["modelos"] = modelos
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} no confirmó el modelo y quiere elegir otro."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, ¿cuál modelo prefieres? Estos son los disponibles: {', '.join(modelos)}."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, modelos[:5])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            elif any(keyword in texto_lower for keyword in ["gracias", "no, gracias", "ok", "de nada"]):
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} dijo '{texto}' antes de confirmar el modelo {sesion['modelo']}."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, ¿confirmas que quieres el modelo {sesion['modelo']}? Si prefieres otro, dime cuál."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Sí", "Cambiar modelo"])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}
            else:
                contexto = f"El cliente {sesion.get('nombre', 'Cliente')} no ha confirmado el modelo {sesion['modelo']}."
                expected_response = f"{sesion.get('nombre', 'Cliente')}, ¿confirmas que quieres el modelo {sesion['modelo']}? Si prefieres otro, dime cuál."
                respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Sí", "Cambiar modelo"])
                logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
                return {"texto": respuesta, "botones": botones}

        # Selección de modelo
        modelo_seleccionado = None
        texto_normalized = normalizar_modelo(texto)
        for m in modelos:
            model_normalized = normalizar_modelo(m)
            if texto_normalized and model_normalized and (
                texto_normalized.lower() == model_normalized.lower() or
                texto_normalized.lower() in model_normalized.lower() or
                levenshtein_distance(texto_normalized.lower(), model_normalized.lower()) <= 2
            ):
                modelo_seleccionado = m
                break
        if modelo_seleccionado:
            sesion["modelo"] = modelo_seleccionado
            sesion["modelo_confirmado"] = False
            guardar_sesion(cliente_id, sesion)
            contexto = f"El cliente {sesion.get('nombre', 'Cliente')} ha seleccionado el modelo {modelo_seleccionado}."
            expected_response = f"{sesion.get('nombre', 'Cliente')}, ¿confirmas que quieres el modelo {modelo_seleccionado}? Si prefieres otro, dime cuál."
            respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, ["Sí", "Cambiar modelo"])
            logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
            return {"texto": respuesta, "botones": botones}
        else:
            contexto = f"El cliente {sesion.get('nombre', 'Cliente')} no ha seleccionado un modelo válido."
            expected_response = f"{sesion.get('nombre', 'Cliente')}, lo siento, ese modelo no está disponible. Estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
            respuesta, botones = generar_respuesta_ollama(texto, contexto, False, expected_response, modelos[:5])
            logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
            return {"texto": respuesta, "botones": botones}

    except Exception as e:
        logger.error(f"Error en el endpoint /webhook: {e}", exc_info=True)
        expected_response = f"{sesion.get('nombre', 'Cliente')}, disculpa, algo salió mal. Por favor, intenta de nuevo."
        respuesta, botones = generar_respuesta_ollama(texto, "Error en el procesamiento del mensaje.", False, expected_response, [])
        logger.info(f"Respuesta del webhook: texto={respuesta}, botones={botones}")
        return {"texto": respuesta, "botones": botones}


# ------------------------------
# Scheduler refresco cache
# ----------------------
@app.on_event("startup")
async def startup_event():
    scheduler.start()
    logger.info("Scheduler inicializado con MongoDBJobStore")
    # Programar refresco de cache cada 3 horas
    scheduler.add_job(
        lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)),
        "interval",
        hours=3,
        id="refresh_car_cache",
        replace_existing=True
    )
    # Refrescar cache al iniciar
    obtener_autos_nuevos(force_refresh=True)
    obtener_autos_usados(force_refresh=True)

@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()
    logger.info("Scheduler detenido correctamente")

if __name__ == "__main__":
    import uvicorn
    #obtener_autos_nuevos(force_refresh=True)
    #obtener_autos_usados(force_refresh=True)
    uvicorn.run(app, host="0.0.0.0", port=5000)