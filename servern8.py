from fastapi import FastAPI
from pydantic import BaseModel
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn
import logging
from datetime import datetime, timedelta
import random
import re
from unidecode import unidecode
import whisper
import ollama
from rapidfuzz import process, fuzz

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.DEBUG, filename="chatbot.log",
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# ---------------- MONGO ----------------
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbotdb"]
historial_col = db["historial"]
estado_col = db["estado_conversacion"]
asignaciones = db["asignaciones"]
asesores_col = db["asesores"]
memoria_col = db["memoria_clientes"]
inventario_col = db["inventario_vehiculos"]
sends = db["sends"]

# ---------------- MODELOS ----------------
class Mensaje(BaseModel):
    cliente_id: str
    texto: str = ""
    audio_path: str | None = None

# ---------------- FUNCIONES AUXILIARES ----------------
def guardar_mensaje(cliente_id: str, mensaje: str, role: str):
    historial_col.insert_one({
        "cliente_id": cliente_id,
        "mensaje": mensaje,
        "role": role,
        "fecha": datetime.now()
    })

def obtener_estado(cliente_id: str) -> dict:
    estado = estado_col.find_one({"_id": cliente_id})
    return estado if estado else {}

def actualizar_estado(cliente_id: str, nuevo_estado: dict):
    estado_col.update_one({"_id": cliente_id}, {"$set": nuevo_estado}, upsert=True)

def actualizar_memoria(cliente_id: str, nuevos_datos: dict):
    memoria_col.update_one({"_id": cliente_id}, {"$set": nuevos_datos}, upsert=True)

def obtener_memoria(cliente_id: str) -> dict:
    memoria = memoria_col.find_one({"_id": cliente_id})
    if not memoria:
        memoria = {"modelos_favoritos": [], "tipo_auto": None, "emociones": [], "ultima_pregunta": ""}
    return memoria

def transcribir_audio(audio_path: str) -> str:
    try:
        model = whisper.load_model("base")
        result = model.transcribe(audio_path, language="es")
        return result["text"].strip()
    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}")
        return "No pude transcribir el audio 😔"

# ---------------- PARSEO DE ENTRADA ----------------
VEHICLE_TYPES = {
    "sedán": ["sedan", "sedán", "serán", "se dan", "se-dan"],
    "suv": ["suv", "essuv", "esuv", "todo terreno", "todoterreno"],
    "compacto": ["compacto", "kompakto", "compacta"]
}

def parsear_entrada(texto: str) -> tuple:
    texto_norm = unidecode(texto.lower()).strip()
    nombre = tipo_auto = tipo_vehiculo = None
    ignored_inputs = ["hola", "ok", "si", "sí", "hey", "busco", "carro", "vehículo", "auto"]

    palabras = texto_norm.split()
    for palabra in palabras:
        if palabra not in ignored_inputs and not any(p in palabra for p in ["nuevo", "usado"] + sum(VEHICLE_TYPES.values(), [])):
            nombre = palabra.title()
            break

    if "nuevo" in texto_norm:
        tipo_auto = "nuevo"
    elif "usado" in texto_norm:
        tipo_auto = "usado"

    for tipo, variantes in VEHICLE_TYPES.items():
        mejor = process.extractOne(texto_norm, variantes, scorer=fuzz.partial_ratio)
        if mejor and mejor[1] >= 75:
            tipo_vehiculo = tipo
            break

    return nombre, tipo_auto, tipo_vehiculo

# ---------------- INVENTARIO ----------------
def obtener_modelos_disponibles(tipo_vehiculo):
    modelos = inventario_col.find({"tipo": tipo_vehiculo, "disponible": True})
    return [m["modelo"] for m in modelos]

# ---------------- BOTONES ----------------
def crear_boton_whatsapp(texto, opciones):
    botones = [{"type": "reply", "reply": {"id": str(i), "title": o}} for i, o in enumerate(opciones)]
    return {"text": texto, "buttons": botones, "type": "interactive"}

# ---------------- REASIGNACION ----------------
scheduler = BackgroundScheduler()
scheduler.start()

def reasignar_pendientes():
    limite = datetime.now() - timedelta(minutes=5)
    pendientes = asignaciones.find({"respuesta": None, "fecha": {"$lt": limite}})
    for p in pendientes:
        asignar_asesor_humano(p["cliente_id"])

scheduler.add_job(reasignar_pendientes, 'interval', minutes=1)

def asignar_asesor_humano(cliente_id: str):
    estado = obtener_estado(cliente_id)
    if not all(k in estado for k in ["telefono", "nombre", "tipo_auto", "tipo_vehiculo", "modelo", "confirmado"]):
        return
    asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
    if not asesores:
        return
    asesor = asesores[0]["telefono"]
    mensaje_cliente = f"Hola {estado['nombre']} 👋, confirmamos tus datos: {estado['tipo_auto']} {estado['modelo']} ({estado['tipo_vehiculo']}). Un asesor te contactará pronto."
    sends.insert_one({"jid": cliente_id, "message": {"text": mensaje_cliente}, "sent": False})

# ---------------- RESPUESTA IA ----------------
def detectar_emocion(texto: str) -> str:
    # Simplificado: se puede reemplazar por análisis avanzado
    if any(p in texto.lower() for p in ["triste", "mal", "enojado", "frustrado"]):
        return "negativa"
    elif any(p in texto.lower() for p in ["feliz", "genial", "perfecto", "excelente"]):
        return "positiva"
    else:
        return "neutral"

def generar_respuesta_ia(cliente_id: str, mensaje: str, estado: dict):
    memoria = obtener_memoria(cliente_id)
    nombre, tipo_auto, tipo_vehiculo = parsear_entrada(mensaje)

    # Actualizar memoria y estado
    if nombre: estado["nombre"] = nombre
    if tipo_auto: estado["tipo_auto"] = tipo_auto
    if tipo_vehiculo: estado["tipo_vehiculo"] = tipo_vehiculo
    actualizar_estado(cliente_id, estado)

    emocion = detectar_emocion(mensaje)
    memoria["emociones"].append(emocion)
    actualizar_memoria(cliente_id, memoria)

    # Selección de modelo
    if estado.get("tipo_vehiculo") and not estado.get("modelo"):
        modelos = obtener_modelos_disponibles(estado["tipo_vehiculo"])
        if modelos:
            return crear_boton_whatsapp("Estos son los modelos disponibles, ¿cuál te interesa?", modelos)
        else:
            return "Lo siento 😔, actualmente no hay modelos disponibles de ese tipo."

    # Confirmación final
    if all(k in estado for k in ["nombre", "tipo_auto", "tipo_vehiculo", "modelo"]) and not estado.get("confirmado"):
        actualizar_estado(cliente_id, {"confirmado": True})
        asignar_asesor_humano(cliente_id)
        return f"Gracias {estado['nombre']} ✅. Un asesor te contactará pronto."

    # Generar respuesta con Ollama
    prompt = f"""
    Eres un asistente humano para ventas de autos. 
    Memoria del cliente: {memoria}
    Estado actual: {estado}
    Mensaje recibido: "{mensaje}"
    Emoción detectada: {emocion}
    Responde de manera natural, educada y empática, guiando al cliente para confirmar su información.
    """
    try:
        respuesta_ia = ollama.chat(model="llama2", prompt=prompt)
        return respuesta_ia["response"]
    except Exception as e:
        logger.error(f"Error IA Ollama: {e}")
        return "Lo siento, no pude generar la respuesta 😔"

# ---------------- WEBHOOK ----------------
@app.post("/webhook")
async def webhook(mensaje: Mensaje):
    cliente_id = mensaje.cliente_id
    texto = mensaje.texto
    if mensaje.audio_path:
        texto = transcribir_audio(mensaje.audio_path)

    estado = obtener_estado(cliente_id)
    respuesta = generar_respuesta_ia(cliente_id, texto, estado)

    guardar_mensaje(cliente_id, texto, "user")
    guardar_mensaje(cliente_id, respuesta, "assistant")

    sends.insert_one({"jid": cliente_id, "message": {"text": respuesta}, "sent": False})
    return {"respuesta": respuesta}

# ---------------- RUN ----------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
