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
from Levenshtein import distance as levenshtein_distance  # Requiere `pip install python-Levenshtein`

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
except Exception as e:
    logger.error(f"Error al conectar con MongoDB: {e}")
    raise Exception("No se pudo conectar a MongoDB")

db = client["chatbot_db"]
cache_col = db["cache"]
sesiones_col = db["sesiones"]
bitacora_col = db["bitacora"]
asesores_col = db["asesores"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"
EJECUTIVOS = ["ejecutivo1", "ejecutivo2", "ejecutivo3"]
TIEMPO_RESPUESTA_EJECUTIVO = 300
MODELOS_RESPALDO = ["Polo", "Saveiro", "Teramont", "Amarok Panamericana", "Transporter 6.1"]

# ------------------------------
# Pydantic model
# ------------------------------
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

# ------------------------------
# Funciones para autos
# ------------------------------
def obtener_autos_nuevos(force_refresh=False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_nuevos"})
        if cache and not force_refresh and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            logger.info("Usando caché para autos nuevos")
            return cache.get("data", [])
        url = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload = {"r": "cargaAutosTodos", "x": str(random.random())}
        res = requests.post(url, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        modelos = list({auto.get("modelo") for auto in data if auto.get("modelo")})
        if not modelos:
            logger.warning("No se obtuvieron modelos nuevos, usando respaldo")
            modelos = MODELOS_RESPALDO
        cache_col.update_one({"_id": "autos_nuevos"}, {"$set": {"data": modelos, "ts": ahora}}, upsert=True)
        logger.info(f"Autos nuevos obtenidos: {modelos}")
        return modelos
    except Exception as e:
        logger.error(f"Error al obtener autos nuevos: {e}")
        return MODELOS_RESPALDO

def obtener_autos_usados(force_refresh=False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_usados"})
        if cache and not force_refresh and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            logger.info("Usando caché para autos usados")
            return cache.get("data", [])
        url = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers = {"User-Agent": "Mozilla/5.0"}
        payload = {"r": "CheckDist"}
        res = requests.post(url, headers=headers, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        modelos = list({f"{auto.get('Modelo')} ({auto.get('Anio')})" for auto in data.get("LiAutos", []) if auto.get("Modelo")})
        if not modelos:
            logger.warning("No se obtuvieron modelos usados, usando respaldo")
            modelos = MODELOS_RESPALDO
        cache_col.update_one({"_id": "autos_usados"}, {"$set": {"data": modelos, "ts": ahora}}, upsert=True)
        logger.info(f"Autos usados obtenidos: {modelos}")
        return modelos
    except Exception as e:
        logger.error(f"Error al obtener autos usados: {e}")
        return MODELOS_RESPALDO

# ------------------------------
# Sesiones y bitácora
# ------------------------------
def obtener_sesion(cliente_id):
    try:
        sesion = sesiones_col.find_one({"cliente_id": cliente_id}) or {}
        logger.info(f"Sesión recuperada para {cliente_id}: {sesion}")
        if not sesion:
            logger.warning(f"No se encontró sesión para {cliente_id}, devolviendo sesión vacía")
        return sesion
    except Exception as e:
        logger.error(f"Error al obtener sesión para {cliente_id}: {e}")
        return {}

def guardar_sesion(cliente_id, sesion):
    try:
        sesion["cliente_id"] = cliente_id
        sesion["ts"] = datetime.utcnow()
        result = sesiones_col.update_one({"cliente_id": cliente_id}, {"$set": sesion}, upsert=True)
        logger.info(f"Sesión guardada para {cliente_id}: {sesion}, Resultado: {result.modified_count} documentos modificados, {result.upserted_id} upserted")
    except Exception as e:
        logger.error(f"Error al guardar sesión para {cliente_id}: {str(e)}")
        raise  # Levanta la excepción para depuración

def guardar_bitacora(registro):
    try:
        registro["fecha"] = datetime.utcnow()
        bitacora_col.insert_one(registro)
        logger.info(f"Bitácora guardada: {registro}")
    except Exception as e:
        logger.error(f"Error al guardar bitácora: {e}")

# ------------------------------
# Generación de respuesta con Ollama
# ------------------------------
def generar_respuesta_ollama(prompt, contexto_sesion=None):
    try:
        system_prompt = (
            f"Eres {BOT_NOMBRE}, un asistente de {AGENCIA}. Tu objetivo es guiar al cliente paso a paso para elegir un auto. "
            "Sigue estrictamente este flujo conversacional: "
            "1) Solicita el nombre si no está registrado. No avances hasta que el cliente proporcione un nombre válido (una palabra alfabética que no sea 'nuevo', 'usado', 'auto', 'coche', 'vehiculo', 'quiero', 'busco', 'asi', 'es', 'mi', 'nombre'). "
            "2) Pregunta si quiere un auto nuevo o usado. No listes modelos en este paso. "
            "3) Muestra modelos disponibles según el tipo de auto, usando SOLO los modelos proporcionados en el contexto. "
            "4) Confirma el modelo seleccionado. "
            "5) Asigna un ejecutivo. "
            "No generes respuestas genéricas, no listes modelos a menos que se indique explícitamente en el contexto, y no te desvíes del flujo. "
            "Responde únicamente en español, de manera amigable, profesional y concisa."
        )
        if contexto_sesion:
            system_prompt += f"\nContexto actual: {contexto_sesion}"
        full_prompt = f"{system_prompt}\n\nInstrucción al cliente: {prompt}"
        logger.info(f"Enviando prompt a Ollama: {full_prompt}")
        resp = ollama.generate(model="llama3", prompt=full_prompt)
        logger.info(f"Respuesta cruda de Ollama: {resp}")
        
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
        logger.error(f"Error al comunicarse con Ollama: {str(e)}")
        return "Disculpa, tuve un problema procesando tu mensaje. Por favor, intenta de nuevo."

# ------------------------------
# Asignación de ejecutivo con reintentos
# ----------------------
async def enviar_a_ejecutivo(cliente_id, info_cliente):
    try:
        for ejecutivo in EJECUTIVOS:
            guardar_bitacora({
                "cliente_id": cliente_id,
                "ejecutivo": ejecutivo,
                "info_cliente": info_cliente,
                "estatus": "pendiente"
            })
            await asyncio.sleep(1)
            disponible = True
            if disponible:
                guardar_bitacora({
                    "cliente_id": cliente_id,
                    "ejecutivo": ejecutivo,
                    "info_cliente": info_cliente,
                    "estatus": "asignado"
                })
                return ejecutivo
        logger.warning(f"No se encontró ejecutivo disponible para {cliente_id}")
        return None
    except Exception as e:
        logger.error(f"Error al asignar ejecutivo para {cliente_id}: {e}")
        return None

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
        if "modelo_confirmado" in sesion and sesion["modelo_confirmado"]:
            logger.info(f"Sesión ya confirmada para {cliente_id}: {sesion}")
            contexto = f"El cliente {sesion['nombre']} ya confirmó el modelo {sesion['modelo']}. Responde amigablemente y sugiere esperar al ejecutivo."
            prompt = f"{sesion['nombre']}, ya hemos registrado tu interés en el modelo {sesion['modelo']}. Un ejecutivo te contactará pronto. ¿Hay algo más en lo que pueda ayudarte?"
            respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        # Manejar saludos iniciales, pero respetar la sesión existente
        if texto in ["hola", "hi", "buenas"]:
            if "nombre" in sesion and "tipo_auto" in sesion and "modelo" in sesion:
                logger.info(f"Sesión existente con modelo seleccionado para {cliente_id}: {sesion}")
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero ya seleccionó el modelo {sesion['modelo']}. Pide confirmación."
                prompt = f"{sesion['nombre']}, confirmas que deseas el modelo {sesion['modelo']}? Puedes cambiarlo si quieres."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Sí", "Cambiar modelo"]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta
            elif "nombre" in sesion and "tipo_auto" in sesion:
                logger.info(f"Sesión existente con tipo_auto para {cliente_id}: {sesion}")
                modelos = sesion.get("modelos", obtener_autos_nuevos() if sesion["tipo_auto"] == "nuevo" else obtener_autos_usados())
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero ya seleccionó tipo_auto {sesion['tipo_auto']}. Muestra modelos: {', '.join(modelos[:5])}."
                prompt = f"{sesion['nombre']}, estos son algunos modelos disponibles: {', '.join(modelos[:5])}. ¿Cuál te interesa?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos[:5]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta
            elif "nombre" in sesion:
                logger.info(f"Sesión existente con nombre para {cliente_id}: {sesion}")
                contexto = f"El cliente {sesion['nombre']} ha enviado un saludo, pero no ha seleccionado tipo_auto. Pregunta si quiere un auto nuevo o usado."
                prompt = f"{sesion['nombre']}, ¿buscas un auto nuevo o usado?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta
            else:
                contexto = "El cliente ha iniciado la conversación con un saludo genérico. Responde amigablemente y pregunta su nombre."
                prompt = f"Hola, bienvenido(a) a {AGENCIA}! 👋 ¿Cómo te llamas?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta

        if "nombre" not in sesion:
            palabras = [p for p in texto.split() if p.isalpha() and p not in ["nuevo", "usado", "auto", "coche", "vehiculo", "quiero", "busco", "asi", "es", "mi", "nombre"]]
            if palabras:
                sesion["nombre"] = palabras[0].title()
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente ha proporcionado su nombre: {sesion['nombre']}. Pregunta si quiere un auto nuevo o usado."
                prompt = f"¡Hola {sesion['nombre']}!, ¿buscas un auto nuevo o usado?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta
            else:
                contexto = "El cliente no ha proporcionado un nombre válido. Insiste en preguntar su nombre."
                prompt = f"Hola, necesito tu nombre para ayudarte mejor. 😊 ¿Cómo te llamas?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta

        if "tipo_auto" not in sesion:
            if "nuevo" in texto:
                sesion["tipo_auto"] = "nuevo"
                guardar_sesion(cliente_id, sesion)
            elif "usado" in texto:
                sesion["tipo_auto"] = "usado"
                guardar_sesion(cliente_id, sesion)
            else:
                contexto = f"El cliente {sesion['nombre']} no ha especificado si quiere un auto nuevo o usado. Pregunta de manera clara."
                prompt = f"{sesion['nombre']}, ¿buscas un auto nuevo o usado?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta

        tipo = sesion["tipo_auto"]
        modelos = obtener_autos_nuevos() if tipo == "nuevo" else obtener_autos_usados()
        if not modelos:
            contexto = f"No se pudieron obtener modelos de autos {tipo}. Informa al cliente y sugiere reintentar o contactar a un ejecutivo."
            prompt = f"{sesion['nombre']}, parece que no tenemos la lista de modelos disponible ahora. ¿Quieres que lo intentemos de nuevo o prefieres hablar con un ejecutivo?"
            respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Reintentar", "Hablar con ejecutivo"]}
            logger.info(f"Respuesta del webhook: {respuesta}")
            return respuesta

        sesion["modelos"] = modelos
        guardar_sesion(cliente_id, sesion)

        # Priorizar confirmación si ya hay un modelo seleccionado
        if "modelo" in sesion and "modelo_confirmado" not in sesion:
            logger.info(f"Verificando confirmación: texto={texto}, sesion={sesion}")
            if texto in ["sí", "si"] or "yes" in texto.lower() or "confirm" in texto.lower() or texto == "Sí":
                info_cliente = {
                    "nombre": sesion["nombre"],
                    "tipo_auto": tipo,
                    "modelo": sesion["modelo"],
                    "whatsapp": cliente_id
                }
                ejecutivo = await enviar_a_ejecutivo(cliente_id, info_cliente)
                sesion["modelo_confirmado"] = True
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} ha confirmado el modelo {sesion['modelo']}. Informa que un ejecutivo lo contactará."
                prompt = f"{sesion['nombre']}, un ejecutivo ({ejecutivo if ejecutivo else 'pronto asignado'}) te contactará en breve para ayudarte con tu {sesion['modelo']}."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta
            elif texto in ["cambiar modelo", "cambiar"]:
                sesion.pop("modelo", None)
                sesion.pop("modelo_confirmado", None)
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} quiere cambiar de modelo. Muestra opciones de modelos {tipo}: {', '.join(modelos[:5])}."
                prompt = f"{sesion['nombre']}, estos son algunos modelos disponibles: {', '.join(modelos[:5])}. ¿Cuál te interesa?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos[:5]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta

        # Selección de modelo con tolerancia a errores tipográficos
        modelo_seleccionado = None
        texto_lower = texto.lower()
        for m in modelos:
            model_parts = m.lower().split()
            key_model_name = model_parts[-1] if model_parts[0] == "nuevo" else m.lower()
            # Coincidencia exacta o parcial
            if key_model_name in texto_lower or m.lower() in texto_lower:
                modelo_seleccionado = m
                break
            # Tolerancia a errores tipográficos (distancia de Levenshtein <= 2)
            if levenshtein_distance(key_model_name, texto_lower) <= 2:
                modelo_seleccionado = m
                break
        logger.info(f"Modelos disponibles: {modelos}, Texto: {texto}, Modelo seleccionado: {modelo_seleccionado}")

        if "modelo_confirmado" not in sesion:
            if modelo_seleccionado:
                sesion["modelo"] = modelo_seleccionado
                guardar_sesion(cliente_id, sesion)
                contexto = f"El cliente {sesion['nombre']} ha seleccionado el modelo {modelo_seleccionado}. Pide confirmación."
                prompt = f"{sesion['nombre']}, confirmas que deseas el modelo {modelo_seleccionado}? Puedes cambiarlo si quieres."
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Sí", "Cambiar modelo"]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta
            else:
                contexto = f"El cliente {sesion['nombre']} no ha seleccionado un modelo. Muestra opciones de modelos {tipo}: {', '.join(modelos[:5])}."
                prompt = f"{sesion['nombre']}, estos son algunos modelos disponibles: {', '.join(modelos[:5])}. ¿Cuál te interesa?"
                respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos[:5]}
                logger.info(f"Respuesta del webhook: {respuesta}")
                return respuesta

        # Respaldo para mensajes no reconocidos
        contexto = f"El cliente {sesion['nombre']} ha enviado un mensaje no reconocido: {texto}. Guía la conversación para elegir un modelo: {', '.join(modelos[:5])}."
        prompt = f"{sesion['nombre']}, no entendí bien tu mensaje. Estos son algunos modelos disponibles: {', '.join(modelos[:5])}. ¿Cuál te interesa?"
        respuesta = {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos[:5]}
        logger.info(f"Respuesta del webhook: {respuesta}")
        return respuesta

    except Exception as e:
        logger.error(f"Error en el endpoint /webhook: {str(e)}")
        respuesta = {"texto": "Lo siento, ocurrió un error en el servidor. Por favor, intenta de nuevo.", "botones": []}
        logger.info(f"Respuesta del webhook (error): {respuesta}")
        return respuesta

@app.get("/get_asesores")
def get_asesores():
    try:
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        return [a["telefono"] for a in asesores if "telefono" in a]
    except Exception as e:
        logger.error(f"Error en /get_asesores: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error del servidor: {str(e)}")

# ------------------------------
# Scheduler refresco cache
# ----------------------
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)), "interval", hours=3)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)