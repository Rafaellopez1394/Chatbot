from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import random
import requests
import logging
import ollama
import asyncio
from ollama import GenerateResponse
from Levenshtein import distance as levenshtein_distance
import re

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
assignments_col = db["assignments"]
sends_col = db["sends"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"
TIEMPO_RESPUESTA_EJECUTIVO = 300
MODELOS_RESPALDO = ["Polo", "Saveiro", "Teramont", "Amarok Panamericana", "Transporter 6.1", "Nivus", "Taos", "T-Cross", "Virtus", "Jetta"]

# ------------------------------
# Pydantic model
# ------------------------------
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

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
        logger.info(f"Enviando solicitud a {url} con payload {payload}")
        res = requests.post(url, data=payload, headers=headers, timeout=10)
        res.raise_for_status()

        data = res.json()

        logger.info(f"Respuesta de la API (autos nuevos): {data}")
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
            "Referer": "https://vw-eurocity.com.mx/Seminuevos/",
        }
        payload = {"r": "CheckDist"}
        logger.info(f"Enviando solicitud a {url} con payload {payload}")
        res = requests.post(url, headers=headers_usados, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        logger.info(f"Respuesta de la API (autos usados): {data}")
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
                    modelos.append({
                        "modelo": modelo,
                        "anio": anio
                    })
        result = cache_col.update_one(
            {"_id": "autos_usados"},
            {"$set": {"data": modelos, "ts": ahora}},
            upsert=True
        )
        logger.info(f"Resultado de update_one para autos_usados: {result.modified_count} documentos modificados, {result.upserted_id} upserted")
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
        logger.info(f"Sesión guardada para {cliente_id}: {sesion}, Resultado: {result.modified_count} documentos modificados, {result.upserted_id} upserted")
    except Exception as e:
        logger.error(f"Error al guardar sesión para {cliente_id}: {str(e)}", exc_info=True)
        raise

def guardar_bitacora(registro):
    try:
        registro["fecha_completa"] = datetime.utcnow()
        bitacora_col.insert_one(registro)
        logger.info(f"Bitácora guardada: {registro}")
    except Exception as e:
        logger.error(f"Error al guardar bitácora: {e}", exc_info=True)

# ------------------------------
# Asignación de ejecutivos
# ------------------------------
def get_asesores():
    try:
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        return [a["telefono"] for a in asesores if "telefono" in a]
    except Exception as e:
        logger.error(f"Error al obtener asesores: {str(e)}", exc_info=True)
        return []

async def send_to_next_advisor(client_id):
    try:
        sesion = obtener_sesion(client_id)
        active_advisors = get_asesores()
        assigned_advisors = sesion.get("assigned_advisors", [])
        next_advisor = None
        for advisor in active_advisors:
            if advisor not in assigned_advisors:
                next_advisor = advisor
                break
        if next_advisor:
            assigned_advisors.append(next_advisor)
            sesion["assigned_advisors"] = assigned_advisors
            guardar_sesion(client_id, sesion)
            message = f"Cliente: {sesion['nombre']} busca {sesion['tipo_auto']} {sesion['modelo']}, contacto: {client_id}. ¿Disponible?"
            sends_col.insert_one({
                "jid": f"{next_advisor}@s.whatsapp.net",
                "message": message,
                "buttons": [
                    {"buttonId": f"yes_{client_id}_{next_advisor}", "buttonText": {"displayText": "Sí"}, "type": 1},
                    {"buttonId": f"no_{client_id}_{next_advisor}", "buttonText": {"displayText": "No"}, "type": 1}
                ],
                "sent": False
            })
            assignments_col.insert_one({
                "client_id": client_id,
                "advisor_phone": next_advisor,
                "sent_time": datetime.utcnow(),
                "status": "pending"
            })
            scheduler.add_job(check_timeout, 'date', run_date=datetime.utcnow() + timedelta(minutes=5), args=[client_id, next_advisor])
            return True
        else:
            logger.warning(f"No hay asesores disponibles para {client_id}")
            sends_col.insert_one({
                "jid": client_id,
                "message": f"{sesion['nombre']}, lo siento, no hay ejecutivos disponibles en este momento. Por favor, intenta de nuevo más tarde.",
                "sent": False
            })
            return False
    except Exception as e:
        logger.error(f"Error en send_to_next_advisor para {client_id}: {str(e)}", exc_info=True)
        sends_col.insert_one({
            "jid": client_id,
            "message": "Lo siento, ocurrió un error al asignar un ejecutivo. Por favor, intenta de nuevo.",
            "sent": False
        })
        return False

def check_timeout(client_id, advisor):
    try:
        assignment = assignments_col.find_one({"client_id": client_id, "advisor_phone": advisor, "status": "pending"})
        if assignment:
            assignments_col.update_one({"_id": assignment["_id"]}, {"$set": {"status": "timeout"}})
            logger.info(f"Timeout para {advisor} con cliente {client_id}, intentando siguiente asesor")
            asyncio.run(send_to_next_advisor(client_id))
    except Exception as e:
        logger.error(f"Error en check_timeout para {client_id}, asesor {advisor}: {str(e)}", exc_info=True)

@app.post("/advisor_response")
async def advisor_response(req: AdvisorResponse):
    try:
        cliente_id = req.cliente_id
        respuesta = req.respuesta
        asesor_phone = req.asesor_phone
        assignment = assignments_col.find_one({"client_id": cliente_id, "advisor_phone": asesor_phone, "status": "pending"})
        if not assignment:
            logger.warning(f"No se encontró asignación pendiente para cliente {cliente_id} y asesor {asesor_phone}")
            return {"texto": "Asignación no encontrada"}
        assignments_col.update_one({"_id": assignment["_id"]}, {"$set": {"status": respuesta}})
        if respuesta == "yes":
            sesion = obtener_sesion(cliente_id)
            info_cliente = {
                "nombre": sesion["nombre"],
                "modelo": sesion["modelo"],
                "contacto": cliente_id,
                "ejecutivo": asesor_phone,
                "fecha": datetime.utcnow().strftime("%Y-%m-%d"),
                "hora": datetime.utcnow().strftime("%H:%M:%S")
            }
            guardar_bitacora(info_cliente)
            client_message = f"{sesion['nombre']}, un ejecutivo te contactará pronto para ayudarte con tu {sesion['modelo']}."
            sends_col.insert_one({
                "jid": cliente_id,
                "message": client_message,
                "sent": False
            })
            summary = f"Cliente: {info_cliente['nombre']}, busca {sesion['tipo_auto']} {info_cliente['modelo']}, contacto: {info_cliente['contacto']}"
            sends_col.insert_one({
                "jid": f"{asesor_phone}@s.whatsapp.net",
                "message": summary,
                "sent": False
            })
            sesion["modelo_confirmado"] = True
            guardar_sesion(cliente_id, sesion)
        else:
            logger.info(f"Asesor {asesor_phone} no disponible, intentando siguiente asesor para {cliente_id}")
            await send_to_next_advisor(cliente_id)
        return {"texto": "Respuesta registrada"}
    except Exception as e:
        logger.error(f"Error en advisor_response: {str(e)}", exc_info=True)
        return {"texto": "Error al procesar la respuesta del asesor"}

# ------------------------------
# Generación de respuesta con Ollama
# ------------------------------
def generar_respuesta_ollama(prompt, contexto_sesion=None, es_primer_mensaje=False):
    try:
        system_prompt = (
            f"Eres {BOT_NOMBRE}, un asistente de {AGENCIA}. Tu objetivo es guiar al cliente paso a paso para elegir un auto. "
            "Sigue estrictamente este flujo conversacional: "
            "1) Solicita el nombre si no está registrado. Acepta nombres compuestos (e.g., 'Rafael Lopez') tomando el nombre completo si es razonable. No avances hasta que el cliente proporcione un nombre válido (solo letras, al menos 3 caracteres, no palabras comunes como 'que', 'rollo', 'hola', 'nuevo', 'usado', 'auto', 'coche', 'vehiculo', 'quiero', 'busco', 'asi', 'es', 'mi', 'nombre', 'buenas', 'hi'). "
            "2) Pregunta si quiere un auto nuevo o usado. Usa siempre 'usado', no 'used'. No listes modelos en este paso. "
            "3) Muestra todos los modelos disponibles según el tipo de auto, usando SOLO los modelos proporcionados en el contexto. "
            "4) Confirma el modelo seleccionado con un mensaje claro y conciso. "
            "5) Tras la confirmación del modelo (respuesta 'sí', 'si', 'yes', o 'confirm'), asigna un ejecutivo inmediatamente con el mensaje: '{nombre}, tu interés en el modelo {modelo} está registrado. Un ejecutivo te contactará pronto para ayudarte.' No hagas preguntas adicionales después de la confirmación a menos que el cliente lo solicite explícitamente (e.g., 'documentos'). "
            "Responde únicamente en español, de manera directa, profesional y concisa. "
            "Evita saludos redundantes como 'Hola', 'Me alegra', o 'Es importante para nosotros conocerte mejor' en todos los mensajes, incluyendo el primero. "
            "Si el cliente pregunta por 'documentos', 'requisitos' o 'papeles', proporciona una lista clara de documentos necesarios para la compra de un auto. "
            "Si el cliente elige 'hablar con un ejecutivo', solicita su nombre si no está registrado; una vez proporcionado, asigna un ejecutivo inmediatamente y notifica al cliente con un mensaje genérico."
        )
        if es_primer_mensaje:
            system_prompt += "\nEn el primer mensaje, usa un tono de bienvenida simple y conciso, e.g., 'Bienvenido(a) a Volkswagen Eurocity Culiacán! 😊 Por favor, dime cómo te llamas.'"
        if contexto_sesion:
            system_prompt += f"\nContexto actual: {contexto_sesion}"
        full_prompt = f"{system_prompt}\n\nInstrucción al cliente: {prompt}"
        #logger.info(f"Enviando prompt a Ollama: {full_prompt}")
        resp = ollama.generate(model="llama3", prompt=full_prompt)
        #logger.info(f"Respuesta cruda de Ollama: {resp}")
        
        if isinstance(resp, GenerateResponse):
            respuesta = str(resp.response).strip()
        elif isinstance(resp, dict) and 'response' in resp:
            respuesta = str(resp['response']).strip()
        else:
            logger.error(f"Respuesta de Ollama no es válida: tipo={type(resp)}, contenido={resp}")
            return "Disculpa, no pude generar una respuesta. Por favor, intenta de nuevo."
        
        if not respuesta:
            logger.warning("Ollama devolvió una respuesta vacía")
            return "Disculpa, no pude generar una respuesta. Por favor, intenta de nuevo."
        
        logger.info(f"Respuesta procesada de Ollama: {respuesta}")
        return respuesta
    except Exception as e:
        logger.error(f"Error al comunicarse con Ollama: {str(e)}", exc_info=True)
        return "Disculpa, tuve un problema procesando tu mensaje. Por favor, intenta de nuevo."

# ------------------------------
# Webhook operativo
# ----------------------
@app.post("/webhook")
async def webhook(req: Mensaje):
    cliente_id = req.cliente_id
    texto = req.texto.lower().strip() if req.texto and isinstance(req.texto, str) else ""
    sesion = obtener_sesion(cliente_id)

    try:
        # Verificar si la sesión debe reiniciarse (solo si han pasado más de 24 horas)
        if sesion.get("ts") and (datetime.utcnow() - sesion["ts"]) > timedelta(hours=24):
            logger.info(f"Sesión antigua detectada para {cliente_id}, reiniciando")
            sesion = {}
            guardar_sesion(cliente_id, sesion)

        # Manejar mensajes post-confirmación primero
        if sesion.get("modelo_confirmado"):
            logger.info(f"Sesión ya confirmada para {cliente_id}: {sesion}")
            if any(keyword in texto for keyword in ["documentos", "requisitos", "papeles"]):
                contexto = f"El cliente {sesion['nombre']} ha preguntado por documentos necesarios para la compra del modelo {sesion['modelo']}."
                prompt = (
                    f"{sesion['nombre']}, para la compra de tu {sesion['modelo']}, necesitarás los siguientes documentos: "
                    "1) Identificación oficial (INE o pasaporte), "
                    "2) Comprobante de domicilio (no mayor a 3 meses), "
                    "3) Comprobantes de ingresos (3 últimos recibos de nómina o estados de cuenta), "
                    "4) Solicitud de crédito (si aplica). "
                    "Un ejecutivo te confirmará los detalles. ¿Hay algo más en lo que pueda ayudarte?"
                )
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
            else:
                contexto = f"El cliente {sesion['nombre']} ya confirmó el modelo {sesion['modelo']}. Responde amigablemente y sugiere esperar al ejecutivo."
                prompt = f"{sesion['nombre']}, tu interés en el modelo {sesion['modelo']} está registrado. Un ejecutivo te contactará pronto. ¿Hay algo más en lo que pueda ayudarte?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
            #logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Manejar solicitud de hablar con un ejecutivo
        if "hablar con un ejecutivo" in texto or "ejecutivo" in texto:
            if "nombre" not in sesion:
                contexto = "El cliente ha solicitado hablar con un ejecutivo, pero no ha proporcionado su nombre."
                prompt = "Necesito tu nombre para asignarte un ejecutivo. 😊 Por favor, dime cómo te llamas."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
            else:
                sesion["tipo_auto"] = sesion.get("tipo_auto", "no especificado")
                sesion["modelo"] = sesion.get("modelo", "no especificado")
                asignado = await send_to_next_advisor(cliente_id)
                contexto = f"El cliente {sesion['nombre']} ha solicitado hablar con un ejecutivo."
                prompt = f"{sesion['nombre']}, un ejecutivo te contactará pronto para ayudarte. ¿Hay algo más en lo que pueda ayudarte?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Manejar saludos iniciales, pero respetar la sesión existente
        if texto in ["hola", "hi", "buenas"]:
            if "nombre" not in sesion:
                contexto = "El cliente ha iniciado la conversación con un saludo genérico. Responde amigablemente y pregunta su nombre."
                prompt = f"Bienvenido(a) a {AGENCIA}! 😊 Por favor, dime cómo te llamas."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto, es_primer_mensaje=True), "botones": []}
            elif "tipo_auto" not in sesion:
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero no ha seleccionado tipo_auto. Pregunta si quiere un auto nuevo o usado."
                prompt = f"{sesion['nombre']}, ¿buscas un auto nuevo o usado?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}
            elif "modelo" not in sesion:
                modelos = sesion.get("modelos", obtener_autos_nuevos() if sesion["tipo_auto"] == "nuevo" else obtener_autos_usados())
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero ya seleccionó tipo_auto {sesion['tipo_auto']}. Muestra todos los modelos: {', '.join(modelos)}."
                prompt = f"{sesion['nombre']}, estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos}
            else:
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero ya seleccionó el modelo {sesion['modelo']}. Pide confirmación."
                prompt = f"{sesion['nombre']}, confirmas que deseas el modelo {sesion['modelo']}? Puedes cambiarlo si quieres."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Sí", "Cambiar modelo"]}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Manejar nombre
        if "nombre" not in sesion:
            nombre_valido = None
            texto_words = texto.split()
            # Intentar tomar el nombre completo o la primera palabra válida
            if len(texto_words) >= 1:
                nombre_candidato = " ".join(texto_words[:2])
                if re.match(r'^[a-zA-ZáéíóúÁÉÍÓÚñÑ\s]{3,}$', nombre_candidato):
                    nombre_valido = nombre_candidato.title()
            if nombre_valido:
                sesion["nombre"] = nombre_valido
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente ha proporcionado su nombre: {sesion['nombre']}. Pregunta si quiere un auto nuevo o usado."
                prompt = f"{sesion['nombre']}, ¿buscas un auto nuevo o usado?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}
                logger.info(f"Respuesta del webhook: {respuesta}")
            else:
                contexto = "El cliente no ha proporcionado un nombre válido. Insiste en preguntar su nombre."
                prompt = f"Necesito tu nombre para ayudarte mejor. 😊 Por favor, dime cómo te llamas (por ejemplo, Juan Pérez)."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto, es_primer_mensaje=True), "botones": []}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Manejar tipo de auto
        if "tipo_auto" not in sesion:
            if texto in ["nuevo", "usado"]:
                sesion["tipo_auto"] = texto
                modelos = obtener_autos_nuevos() if texto == "nuevo" else obtener_autos_usados()
                sesion["modelos"] = modelos
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} ha seleccionado tipo_auto {texto}. Muestra todos los modelos: {', '.join(modelos)}."
                prompt = f"{sesion['nombre']}, estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos}
            else:
                contexto = f"El cliente {sesion['nombre']} no ha especificado si quiere un auto nuevo o usado. Pregunta de manera clara."
                prompt = f"{sesion['nombre']}, ¿buscas un auto nuevo o usado?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Manejar selección de modelo
        tipo = sesion["tipo_auto"]
        modelos = sesion.get("modelos", obtener_autos_nuevos() if tipo == "nuevo" else obtener_autos_usados())
        if not modelos:
            contexto = f"No se pudieron obtener modelos de autos {tipo}. Informa al cliente y sugiere reintentar o contactar a un ejecutivo."
            prompt = f"{sesion['nombre']}, parece que no tenemos la lista de modelos disponible ahora. ¿Quieres que lo intentemos de nuevo o prefieres hablar con un ejecutivo?"
            respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Reintentar", "Hablar con ejecutivo"]}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Confirmación de modelo
        if "modelo" in sesion and not sesion.get("modelo_confirmado"):
            logger.info(f"Verificando confirmación: texto={texto}, sesion={sesion}")
            if texto in ["sí", "si", "yes", "confirm"] or texto.lower() == "sí":
                asignado = await send_to_next_advisor(cliente_id)
                sesion["modelo_confirmado"] = True
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} ha confirmado el modelo {sesion['modelo']}. Informa que un ejecutivo lo contactará."
                prompt = f"{sesion['nombre']}, tu interés en el modelo {sesion['modelo']} está registrado. Un ejecutivo te contactará pronto para ayudarte."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
                logger.info(f"Respuesta del webhook: {respuesta}")
            elif texto in ["cambiar modelo", "cambiar", "otras opciones"]:
                sesion.pop("modelo", None)
                sesion.pop("modelo_confirmado", None)
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} quiere cambiar de modelo. Muestra todos los modelos {tipo}: {', '.join(modelos)}."
                prompt = f"{sesion['nombre']}, estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos}
            else:
                contexto = f"El cliente {sesion['nombre']} no ha confirmado el modelo {sesion['modelo']}. Pide confirmación."
                prompt = f"{sesion['nombre']}, confirmas que deseas el modelo {sesion['modelo']}? Puedes cambiarlo si quieres."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Sí", "Cambiar modelo"]}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Selección de modelo con tolerancia a errores tipográficos
        modelo_seleccionado = None
        texto_lower = texto.lower().replace(".", "").replace(" ", "")
        for m in modelos:
            model_parts = m.lower().split()
            key_model_name = model_parts[0] if tipo == "usado" else m.lower()
            key_model_name_normalized = key_model_name.replace(".", "").replace(" ", "")
            if key_model_name_normalized == texto_lower or m.lower().replace(".", "").replace(" ", "") == texto_lower:
                modelo_seleccionado = m
                break
            if levenshtein_distance(key_model_name_normalized, texto_lower) <= 3 and texto not in ["nuevo", "usado"]:
                modelo_seleccionado = m
                break
        logger.info(f"Modelos disponibles: {modelos}, Texto: {texto}, Modelo seleccionado: {modelo_seleccionado}")


        if modelo_seleccionado:
            sesion["modelo"] = modelo_seleccionado
            guardar_sesion(cliente_id, sesion)
            contexto = f"El cliente {sesion['nombre']} ha seleccionado el modelo {modelo_seleccionado}. Pide confirmación."
            prompt = f"{sesion['nombre']}, confirmas que deseas el modelo {modelo_seleccionado}? Puedes cambiarlo si quieres."
            respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Sí", "Cambiar modelo"]}
            logger.info(f"Respuesta del webhook: {respuesta}")
        else:
            contexto = f"El cliente {sesion['nombre']} no ha seleccionado un modelo válido. Muestra todos los modelos {tipo}: {', '.join(modelos)}."
            prompt = f"{sesion['nombre']}, estos son los modelos disponibles: {', '.join(modelos)}. ¿Cuál te interesa?"
            respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos}
        logger.info(f"Respuesta del webhook: {respuesta}")
        return respuesta

    except Exception as e:
        logger.error(f"Error en el endpoint /webhook: {str(e)}", exc_info=True)
        respuesta = {"texto": "Lo siento, ocurrió un error en el servidor. Por favor, intenta de nuevo.", "botones": []}
        logger.info(f"Respuesta del webhook (error): {respuesta}")
        return respuesta

# ------------------------------
# Scheduler refresco cache
# ----------------------
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)), "interval", hours=3)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    # Forzar actualización del caché al iniciar
    obtener_autos_nuevos(force_refresh=True)
    obtener_autos_usados(force_refresh=True)
    uvicorn.run(app, host="0.0.0.0", port=5000)