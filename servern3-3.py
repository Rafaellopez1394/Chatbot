from fastapi import FastAPI, Request
from pydantic import BaseModel
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import requests, random, logging, re
import ollama  # Para generar respuestas más humanas

# =====================
# Configuración / Logging
# =====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =====================
# Base de datos
# =====================
app = FastAPI()
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbot_db"]
cache_col = db["cache"]
sesiones_col = db["sesiones"]
bitacora_col = db["bitacora"]

BOT_NOMBRE = "Alex"
AGENCIA = "Volkswagen Eurocity Culiacan"

# =====================
# Modelos Pydantic
# =====================
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

# =====================
# Funciones de obtención de autos
# =====================
def obtener_autos_nuevos(force_refresh: bool = False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_nuevos"})
        if not force_refresh and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

        url = "https://vw-eurocity.com.mx/info/consultas.ashx"
        payload = {"r": "cargaAutosTodos", "x": str(random.random())}
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.post(url, data=payload, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()
        modelos = list({auto.get("modelo") for auto in data if auto.get("modelo")})
        cache_col.update_one({"_id": "autos_nuevos"}, {"$set": {"data": modelos, "ts": ahora}}, upsert=True)
        return modelos
    except Exception as e:
        logger.error(f"Error obteniendo autos nuevos: {e}")
        return []

def obtener_autos_usados(force_refresh: bool = False):
    try:
        ahora = datetime.utcnow()
        cache = cache_col.find_one({"_id": "autos_usados"})
        if not force_refresh and cache and (ahora - cache.get("ts", ahora) < timedelta(hours=3)):
            return cache.get("data", [])

        url = "https://vw-eurocity.com.mx/SeminuevosMotorV3/info/consultas.aspx"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://vw-eurocity.com.mx",
            "Referer": "https://vw-eurocity.com.mx/Seminuevos/",
        }
        payload = {"r": "CheckDist"}
        res = requests.post(url, headers=headers, data=payload, timeout=10)
        res.raise_for_status()
        data = res.json()
        autos = []
        vistos = set()
        for auto in data.get("LiAutos", []):
            modelo, anio = auto.get("Modelo"), auto.get("Anio")
            clave = f"{modelo}-{anio}"
            if modelo and anio and clave not in vistos:
                vistos.add(clave)
                autos.append({"modelo": modelo, "anio": anio})
        cache_col.update_one({"_id": "autos_usados"}, {"$set": {"data": autos, "ts": ahora}}, upsert=True)
        return autos
    except Exception as e:
        logger.error(f"Error obteniendo autos usados: {e}")
        return []

# =====================
# Funciones de sesión y bitácora
# =====================
def obtener_sesion(cliente_id):
    sesion = sesiones_col.find_one({"cliente_id": cliente_id})
    return sesion if sesion else {}

def guardar_sesion(cliente_id, sesion):
    sesion["cliente_id"] = cliente_id
    sesion["fecha"] = datetime.now()
    sesiones_col.update_one({"cliente_id": cliente_id}, {"$set": sesion}, upsert=True)

def guardar_bitacora(datos):
    datos["fecha"] = datetime.now()
    bitacora_col.insert_one(datos)

# =====================
# Ejecutivos
# =====================
EJECUTIVOS = [{"nombre": "Pedro", "whatsapp": "+5215512345678"},
              {"nombre": "Ana", "whatsapp": "+5215512345679"}]

async def asignar_ejecutivo(cliente):
    for ejecutivo in EJECUTIVOS:
        disponible = True  # Lógica real de confirmación puede ir aquí
        if disponible:
            guardar_bitacora({
                "cliente": cliente["nombre"],
                "telefono": cliente["telefono"],
                "modelo": cliente["modelo"],
                "ejecutivo": ejecutivo["nombre"],
                "asignado": True
            })
            return ejecutivo
    return None

# =====================
# Funciones de parsing
# =====================
def parsear_nombre_modelo(texto, sesion):
    palabras = texto.lower().split()
    # Nombre
    if "nombre" not in sesion:
        for palabra in palabras:
            if palabra.isalpha() and palabra not in {"hola","ok","sí","si","gracias"}:
                sesion["nombre"] = palabra.title()
                break
    # Modelo
    if "modelo" not in sesion:
        modelos = obtener_autos_nuevos() if sesion.get("tipo_auto")=="nuevo" else [a["modelo"] for a in obtener_autos_usados()]
        for modelo in modelos:
            if modelo.lower() in texto.lower():
                sesion["modelo"] = modelo
                break
    return sesion

# =====================
# Función para generar respuesta humana
# =====================
def generar_respuesta_humana(sesion, texto_usuario):
    prompt_base = f"""
    Eres un asistente de ventas humano y amable para Volkswagen Eurocity Culiacán llamado {BOT_NOMBRE}.
    Conversación actual:
    Cliente: {texto_usuario}
    Sesión: {sesion}
    Genera una respuesta natural y cálida, preguntando nombre o modelo si faltan, confirmando datos antes de enviar a un ejecutivo, y usando lenguaje cercano.
    """
    response = ollama.generate(model="llama3", prompt=prompt_base)
    return response["response"].strip()

# =====================
# Webhook principal
# =====================
@app.post("/webhook")
async def webhook(req: Mensaje):
    cliente_id = req.cliente_id
    texto = req.texto.strip()
    sesion = obtener_sesion(cliente_id)

    # Parsear nombre y modelo
    sesion = parsear_nombre_modelo(texto, sesion)
    guardar_sesion(cliente_id, sesion)

    # Generar respuesta humana
    respuesta = generar_respuesta_humana(sesion, texto)

    # Confirmación final y asignación
    if "nombre" in sesion and "modelo" in sesion and not sesion.get("confirmado"):
        sesion["confirmado"] = True
        guardar_sesion(cliente_id, sesion)
        cliente = {"nombre": sesion["nombre"], "telefono": cliente_id, "modelo": sesion["modelo"]}
        ejecutivo = await asignar_ejecutivo(cliente)
        if ejecutivo:
            respuesta += f"\n\n¡Excelente! Un ejecutivo ({ejecutivo['nombre']}) te contactará pronto vía WhatsApp."
        else:
            respuesta += "\n\nTodos nuestros ejecutivos están ocupados, te contactaremos en cuanto sea posible."

    return {"texto": respuesta}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
