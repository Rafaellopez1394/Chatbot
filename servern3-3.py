from fastapi import FastAPI
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import random
import requests
import logging
import ollama
import asyncio

# ------------------------------
# Configuración básica
# ------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)
    client.server_info()  # Verifica la conexión a MongoDB
    logger.info("Conexión a MongoDB establecida")
except Exception as e:
    logger.error(f"Error al conectar con MongoDB: {e}")
    raise Exception("No se pudo conectar a MongoDB")

db = client["chatbot_db"]
cache_col = db["cache"]
sesiones_col = db["sesiones"]
bitacora_col = db["bitacora"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"
EJECUTIVOS = ["ejecutivo1", "ejecutivo2", "ejecutivo3"]
TIEMPO_RESPUESTA_EJECUTIVO = 300  # segundos
MODELOS_RESPALDO = ["Polo", "Saveiro", "Teramont", "Amarok Panamericana", "Transporter 6.1"]  # Respaldo en caso de fallo de API

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
        return sesion
    except Exception as e:
        logger.error(f"Error al obtener sesión para {cliente_id}: {e}")
        return {}

def guardar_sesion(cliente_id, sesion):
    try:
        sesion["cliente_id"] = cliente_id
        sesion["ts"] = datetime.utcnow()
        sesiones_col.update_one({"cliente_id": cliente_id}, {"$set": sesion}, upsert=True)
        logger.info(f"Sesión guardada para {cliente_id}: {sesion}")
    except Exception as e:
        logger.error(f"Error al guardar sesión para {cliente_id}: {e}")

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
        logger.info(f"Respuesta de Ollama: {resp['response']}")
        return resp["response"].strip()
    except Exception as e:
        logger.error(f"Error al comunicarse con Ollama: {str(e)}")
        return "Disculpa, tuve un problema procesando tu mensaje. Por favor, intenta de nuevo."

# ------------------------------
# Asignación de ejecutivo con reintentos
# ------------------------------
async def enviar_a_ejecutivo(cliente_id, info_cliente):
    try:
        for ejecutivo in EJECUTIVOS:
            guardar_bitacora({
                "cliente_id": cliente_id,
                "ejecutivo": ejecutivo,
                "info_cliente": info_cliente,
                "estatus": "pendiente"
            })
            await asyncio.sleep(1)  # simulación disponibilidad
            disponible = True  # reemplazar por lógica real
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
    texto = req.texto.lower().strip()
    sesion = obtener_sesion(cliente_id)

    # Manejar entrada vacía o genérica
    if not texto or texto in ["hola", "hi", "buenas"]:
        contexto = "El cliente ha iniciado la conversación con un saludo genérico. Responde amigablemente y pregunta su nombre."
        prompt = f"Hola, bienvenido(a) a {AGENCIA}! 👋 ¿Cómo te llamas?"
        return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}

    # 1️⃣ Solicitar nombre
    if "nombre" not in sesion:
        palabras = [p for p in texto.split() if p.isalpha() and p not in ["nuevo", "usado", "auto", "coche", "vehiculo", "quiero", "busco", "asi", "es", "mi", "nombre"]]
        if palabras:
            sesion["nombre"] = palabras[0].title()
            guardar_sesion(cliente_id, sesion)
            contexto = f"El cliente ha proporcionado su nombre: {sesion['nombre']}. Pregunta si quiere un auto nuevo o usado."
            prompt = f"¡Hola {sesion['nombre']}!, ¿buscas un auto nuevo o usado?"
            return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}
        else:
            contexto = "El cliente no ha proporcionado un nombre válido. Insiste en preguntar su nombre."
            prompt = f"Hola, necesito tu nombre para ayudarte mejor. 😊 ¿Cómo te llamas?"
            return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}

    # 2️⃣ Preguntar tipo de auto
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
            return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Nuevo", "Usado"]}

    # 3️⃣ Mostrar modelos
    tipo = sesion["tipo_auto"]
    modelos = obtener_autos_nuevos() if tipo == "nuevo" else obtener_autos_usados()
    if not modelos:
        contexto = f"No se pudieron obtener modelos de autos {tipo}. Informa al cliente y sugiere reintentar o contactar a un ejecutivo."
        prompt = f"{sesion['nombre']}, parece que no tenemos la lista de modelos disponible ahora. ¿Quieres que lo intentemos de nuevo o prefieres hablar con un ejecutivo?"
        return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Reintentar", "Hablar con ejecutivo"]}
    sesion["modelos"] = modelos
    guardar_sesion(cliente_id, sesion)

    # 4️⃣ Detectar modelo mencionado
    modelo_seleccionado = next((m for m in modelos if m.lower() in texto), None)

    # 5️⃣ Confirmación modelo
    if "modelo_confirmado" not in sesion:
        if modelo_seleccionado:
            sesion["modelo"] = modelo_seleccionado
            guardar_sesion(cliente_id, sesion)
            contexto = f"El cliente {sesion['nombre']} ha seleccionado el modelo {modelo_seleccionado}. Pide confirmación."
            prompt = f"{sesion['nombre']}, confirmas que deseas el modelo {modelo_seleccionado}? Puedes cambiarlo si quieres."
            return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": ["Sí", "Cambiar modelo"]}
        else:
            contexto = f"El cliente {sesion['nombre']} no ha seleccionado un modelo. Muestra opciones de modelos {tipo}: {', '.join(modelos[:5])}."
            prompt = f"{sesion['nombre']}, estos son algunos modelos disponibles: {', '.join(modelos[:5])}. ¿Cuál te interesa?"
            return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos[:5]}

    # 6️⃣ Confirmación final
    if texto in ["sí", "si"]:
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
        return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": []}
    elif texto in ["cambiar modelo"]:
        sesion.pop("modelo", None)
        sesion.pop("modelo_confirmado", None)
        guardar_sesion(cliente_id, sesion)
        contexto = f"El cliente {sesion['nombre']} quiere cambiar de modelo. Muestra opciones de modelos {tipo}: {', '.join(modelos[:5])}."
        prompt = f"{sesion['nombre']}, estos son algunos modelos disponibles: {', '.join(modelos[:5])}. ¿Cuál te interesa?"
        return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos[:5]}

    # 7️⃣ Manejar entradas no reconocidas
    contexto = f"El cliente {sesion['nombre']} ha enviado un mensaje no reconocido: {texto}. Guía la conversación para elegir un modelo: {', '.join(modelos[:5])}."
    prompt = f"{sesion['nombre']}, no entendí bien tu mensaje. Estos son algunos modelos disponibles: {', '.join(modelos[:5])}. ¿Cuál te interesa?"
    return {"texto": generar_respuesta_ollama(prompt, contexto), "botones": modelos[:5]}

# ------------------------------
# Scheduler refresco cache
# ----------------------
scheduler = BackgroundScheduler()
scheduler.add_job(lambda: (obtener_autos_nuevos(force_refresh=True), obtener_autos_usados(force_refresh=True)), "interval", hours=3)
scheduler.start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)