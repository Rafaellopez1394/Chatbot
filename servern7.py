from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError, WriteError
from apscheduler.schedulers.background import BackgroundScheduler
import ollama
import re
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import uvicorn
import random
import logging
import os
import subprocess
import whisper
from rapidfuzz import process, fuzz
from unidecode import unidecode

# ------------------ CONFIG LOGGING ------------------
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', filename='chatbot.log')
logger = logging.getLogger(__name__)

app = FastAPI()

# ------------------ MONGO ------------------
try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logger.info("Conexión a MongoDB exitosa")
    db = client["chatbotdb"]
    collections = db.list_collection_names()
    required_collections = ["test", "estado_conversacion", "historial", "memoria_clientes", "asignaciones", "asesores", "sends"]
    for coll in required_collections:
        if coll not in collections:
            db.create_collection(coll)
            logger.info(f"Colección '{coll}' creada")
    test_collection = db["test"]
    test_collection.insert_one({"test": "prueba_conexion", "fecha": datetime.now()})
except Exception as e:
    logger.error(f"Error al conectar a MongoDB: {str(e)}")
    raise

db = client["chatbotdb"]
historial_col = db["historial"]
estado_conversacion = db["estado_conversacion"]
asignaciones = db["asignaciones"]
asesores_col = db["asesores"]
sends = db["sends"]
memoria_col = db["memoria_clientes"]

# ------------------ INICIALIZAR ASESORES ------------------
def inicializar_asesores():
    try:
        existing = list(asesores_col.find())
        if not existing or not all("telefono" in doc and "activo" in doc for doc in existing):
            asesores_col.delete_many({})
            asesores_data = [{"nombre": "Ana", "area": "Ventas", "activo": True, "telefono": "526879388889"}]
            asesores_col.insert_many(asesores_data)
    except Exception as e:
        logger.error(f"Error inicializando asesores: {str(e)}")

inicializar_asesores()

# ------------------ SCHEDULER ------------------
scheduler = BackgroundScheduler()
scheduler.start()

def reasignar_pendientes():
    try:
        limite = datetime.now() - timedelta(minutes=5)
        pendientes = asignaciones.find({"respuesta": None, "fecha": {"$lt": limite}})
        for p in pendientes:
            asignar_asesor_humano(p["cliente_id"])
    except Exception as e:
        logger.error(f"Error reasignando pendientes: {str(e)}")

scheduler.add_job(reasignar_pendientes, 'interval', minutes=1)

# ------------------ MODELOS ------------------
class Mensaje(BaseModel):
    cliente_id: str
    texto: str = ""
    audio_path: str | None = None

class AdvisorResponse(BaseModel):
    cliente_id: str
    respuesta: str
    asesor_phone: str

# ------------------ AUXILIARES ------------------
def actualizar_estado(cliente_id: str, nuevo_estado: dict):
    try:
        estado_conversacion.update_one({"_id": cliente_id}, {"$set": nuevo_estado}, upsert=True)
    except Exception as e:
        logger.error(f"Error actualizar estado: {str(e)}")

def obtener_estado(cliente_id: str) -> dict:
    try:
        estado = estado_conversacion.find_one({"_id": cliente_id})
        return estado if estado else {}
    except Exception as e:
        logger.error(f"Error obtener estado: {str(e)}")
        return {}

def guardar_mensaje(cliente_id: str, mensaje: str, role: str):
    try:
        result = historial_col.insert_one({
            "cliente_id": cliente_id,
            "mensaje": mensaje,
            "role": role,
            "fecha": datetime.now()
        })
        logger.debug(f"Mensaje guardado para {cliente_id}: {mensaje} ({role}), ID insertado: {result.inserted_id}")
    except WriteError as e:
        logger.error(f"Error de escritura al guardar mensaje para {cliente_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Error inesperado al guardar mensaje para {cliente_id}: {str(e)}")

def es_contacto_valido(telefono: str) -> bool:
    return bool(re.match(r'^\d{10,}$', telefono))

# Extracción de datos oficiales
def obtener_modelos_oficiales():
    try:
        urls = [
            "https://www.autocosmos.com.mx/vweurocity",
            "https://vw-eurocity.com.mx/"
        ]
        modelos = set()
        predefined_models = ["Jetta", "Tiguan", "Virtus", "Taos", "Teramont", "T-Cross", "Polo"]
        for url in urls:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            for tag in soup.find_all(['h2', 'h3', 'a', 'li', 'div']):
                text = tag.get_text(strip=True).lower()
                for model in predefined_models:
                    if model.lower() in text and not any(x in text for x in ['precio', 'culiacán', 'sinaloa']):
                        modelos.add(model.title())
        modelos_list = list(modelos) if modelos else predefined_models
        modelos_list = list(dict.fromkeys(modelos_list))  # Eliminar duplicados
        logger.debug(f"Modelos recuperados: {modelos_list}")
        return modelos_list
    except Exception as e:
        logger.error(f"Error al obtener modelos oficiales: {str(e)}")
        return ["Jetta", "Tiguan", "Taos"]

def obtener_detalles_modelo(modelo: str):
    try:
        response = requests.get("https://vw-eurocity.com.mx/", timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        info = {}
        secciones = soup.find_all(['h2', 'h3', 'p', 'li', 'div'])
        for sec in secciones:
            text = sec.get_text(strip=True).lower()
            if modelo.lower() in text:
                info['descripcion'] = text.title()
                break
        result = info if info else {"descripcion": f"Información de {modelo} no disponible en línea."}
        logger.debug(f"Detalles recuperados para {modelo}: {result}")
        return result
    except Exception as e:
        logger.error(f"Error al obtener detalles del modelo {modelo}: {str(e)}")
        return {"descripcion": f"Error al consultar el modelo {modelo}"}

# Memoria avanzada
def actualizar_memoria_avanzada(cliente_id: str, nuevo_dato: dict):
    try:
        result = memoria_col.update_one(
            {"_id": cliente_id},
            {"$set": nuevo_dato},
            upsert=True
        )
        logger.debug(f"Memoria actualizada para {cliente_id}: {nuevo_dato}, Resultado: matched={result.matched_count}, modified={result.modified_count}, upserted={result.upserted_id}")
    except WriteError as e:
        logger.error(f"Error de escritura al actualizar memoria para {cliente_id}: {str(e)}")
    except Exception as e:
        logger.error(f"Error inesperado al actualizar memoria para {cliente_id}: {str(e)}")

def obtener_memoria_avanzada(cliente_id: str) -> dict:
    try:
        memoria = memoria_col.find_one({"_id": cliente_id})
        if not memoria:
            memoria = {"modelos_favoritos": [], "tipo_auto_preferido": None, "emociones": [], "ultima_pregunta": "", "tipo_vehiculo": None}
        logger.debug(f"Memoria recuperada para {cliente_id}: {memoria}")
        return memoria
    except Exception as e:
        logger.error(f"Error al recuperar memoria para {cliente_id}: {str(e)}")
        return {"modelos_favoritos": [], "tipo_auto_preferido": None, "emociones": [], "ultima_pregunta": "", "tipo_vehiculo": None}

def detectar_emocion(texto: str) -> str:
    texto = texto.lower()
    if any(p in texto for p in ["emocionado", "genial", "excelente", "perfecto", "okey", "claro", "si esta bien"]):
        return "positivo"
    elif any(p in texto for p in ["no sé", "no estoy seguro", "tal vez", "quizás", "dudoso", "no tengo alguno en mente", "no tengo un modelo aun"]):
        return "indeciso"
    elif any(p in texto for p in ["triste", "no me gusta", "malo", "difícil"]):
        return "negativo"
    else:
        return "neutral"

def resumir_historial_emociones(historial, estado, memoria, max_msgs=5):
    try:
        resumen = []
        for h in historial[-max_msgs:]:
            role = "Usuario" if h["role"] == "user" else "Asistente"
            mensaje = h["mensaje"]
            resumen.append(f"{role}: {mensaje}")
        estado_str = ", ".join([f"{k}: {v}" for k, v in estado.items() if k in ["nombre", "tipo_auto", "tipo_vehiculo", "modelo", "confirmado"]])
        resumen.append(f"[Resumen de estado: {estado_str}]")
        memoria_str = ", ".join([f"{k}: {v}" for k, v in memoria.items() if k != "emociones"])
        emociones_str = ", ".join(memoria.get("emociones", []))
        resumen.append(f"[Memoria del cliente: {memoria_str}]")
        resumen.append(f"[Emociones detectadas: {emociones_str}]")
        logger.debug(f"Resumen historial para {estado.get('cliente_id','')}: {resumen}")
        return "\n".join(resumen)
    except Exception as e:
        logger.error(f"Error al resumir historial: {str(e)}")
        return ""

def es_contacto_valido(telefono: str) -> bool:
    return bool(re.match(r'^\d{10,}$', telefono))

# ------------------ PARSEAR ENTRADA MEJORADO ------------------
VEHICLE_TYPES = {
    "sedán": ["sedan", "sedán", "serán", "se dan", "se-dan"],
    "suv": ["suv", "essuv", "esuv", "todo terreno", "todoterreno"],
    "compacto": ["compacto", "kompakto", "compacta"]
}

def parsear_entrada(texto: str) -> tuple:
    texto_norm = unidecode(texto.lower()).strip()
    nombre = None
    tipo_auto = None
    tipo_vehiculo = None

    ignored_inputs = [
        "hola", "me interesa un auto", "no se", "no sé", "okey", "hey", "si", "sí",
        "si esta bien", "claro", "ok", "no tengo alguno en mente", "no tengo un modelo aun",
        "y", "busco", "carro", "vehículo", "auto"
    ]

    palabras = texto_norm.split()
    for palabra in palabras:
        if palabra not in ignored_inputs and not any(p in palabra for p in ["nuevo", "usado", "suv", "sedan", "sedán", "compacto"]):
            nombre = palabra.title()
            break

    if "nuevo" in texto_norm:
        tipo_auto = "nuevo"
    elif "usado" in texto_norm:
        tipo_auto = "usado"

    for tipo, variantes in VEHICLE_TYPES.items():
        mejor_coincidencia = process.extractOne(texto_norm, variantes, scorer=fuzz.partial_ratio)
        if mejor_coincidencia and mejor_coincidencia[1] >= 75:
            tipo_vehiculo = tipo
            break

    logger.debug(f"Parseado entrada '{texto}': nombre={nombre}, tipo_auto={tipo_auto}, tipo_vehiculo={tipo_vehiculo}")
    return nombre, tipo_auto, tipo_vehiculo

# Generar respuesta premium
def generar_respuesta_premium(mensaje: str, historial: list, estado: dict) -> dict:
    try:
        cliente_id = estado.get("cliente_id", "")
        logger.debug(f"Generando respuesta para cliente_id={cliente_id}, mensaje={mensaje}")
        memoria = obtener_memoria_avanzada(cliente_id)
        emocion_actual = detectar_emocion(mensaje)
        if emocion_actual not in memoria["emociones"]:
            memoria["emociones"].append(emocion_actual)
            actualizar_memoria_avanzada(cliente_id, {"emociones": memoria["emociones"]})

        saludos = {
            "positivo": [
                "¡Qué gusto verte, {nombre}! 😊 ¿Seguimos explorando opciones?",
                "¡Hola {nombre}, qué bueno que estás entusiasmado! 🚗 ¿En qué te ayudo hoy?"
            ],
            "indeciso": [
                "¡Hola {nombre}! No te preocupes, vamos a encontrar el auto perfecto juntos. 😊",
                "Hola {nombre}, ¿te ayudo a decidir qué vehículo te conviene más?"
            ],
            "negativo": [
                "¡Hola {nombre}! Tranquilo, estoy aquí para resolver tus dudas. 😊",
                "Hola {nombre}, vamos a buscar una solución que te encante."
            ],
            "neutral": [
                "¡Hola {nombre}! ¿Listo para encontrar tu próximo auto? 🚘",
                "Hola {nombre}, ¿en qué puedo ayudarte hoy?"
            ]
        }
        saludos_nuevo = [
            "¡Hola! Bienvenido a Volkswagen Eurocity Culiacán. Soy Alex, tu asistente de ventas. 😊 ¿Cuál es tu nombre y buscas un auto nuevo o usado?",
            "¡Hola! Soy Alex de Volkswagen Eurocity Culiacán. 🚗 ¿Cómo te llamas y qué tipo de auto buscas, nuevo o usado?"
        ]
        transiciones = {
            "positivo": ["¡Genial, vamos a ello!", "¡Perfecto, sigamos adelante!"],
            "indeciso": ["Tranquilo, vamos paso a paso.", "¡Vale, te ayudo a elegir!"],
            "negativo": ["No te preocupes, lo resolveremos.", "¡Vamos a encontrar algo que te guste!"],
            "neutral": ["¡Estupendo, continuemos!", "¡Bien, sigamos explorando!"]
        }

        # Verificar si hay una conversación reciente (menos de 24 horas)
        historial_reciente = [
            h for h in historial
            if h["role"] == "user" and (datetime.now() - h.get("fecha", datetime.min)).total_seconds() < 24 * 3600
        ]
        es_conversacion_nueva = not historial_reciente or mensaje.lower() == "hola"

        # Respuesta personalizada si hay estado previo
        if "nombre" in estado and es_conversacion_nueva and "confirmado" in estado:
            saludo = random.choice(saludos.get(emocion_actual, saludos["neutral"])).format(nombre=estado.get("nombre", ""))
            respuesta = f"{saludo} Ya tenemos registrado tu interés en un {estado.get('tipo_auto', '')} {estado.get('modelo', '')} ({estado.get('tipo_vehiculo', '')}). ¿Quieres continuar con eso o prefieres explorar otras opciones?"
            logger.debug(f"Respuesta generada para {cliente_id} (conversación previa): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}

        # Flujo estándar
        if not estado.get("nombre"):
            respuesta = random.choice(saludos_nuevo)
            logger.debug(f"Respuesta generada para {cliente_id} (sin nombre): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif "nombre" in estado and not estado.get("tipo_auto"):
            transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))
            respuesta = f"{transicion} ¿Buscas un auto nuevo o usado?"
            logger.debug(f"Respuesta generada para {cliente_id} (sin tipo_auto): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif "nombre" in estado and "tipo_auto" in estado and not estado.get("tipo_vehiculo"):
            transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))
            respuesta = f"{transicion} ¡Estupendo! ¿Qué tipo de vehículo prefieres? Por ejemplo: SUV, sedán o compacto."
            logger.debug(f"Respuesta generada para {cliente_id} (sin tipo_vehiculo): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif "nombre" in estado and "tipo_auto" in estado and "tipo_vehiculo" in estado and not estado.get("modelo"):
            transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))
            modelos_web = obtener_modelos_oficiales()
            modelos_por_tipo = {
                "suv": ["Tiguan", "Taos", "Teramont", "T-Cross"],
                "sedán": ["Jetta", "Virtus"],
                "compacto": ["Polo"]
            }
            modelos = modelos_por_tipo.get(estado["tipo_vehiculo"], modelos_web)
            respuesta = f"{transicion} Ahora, ¿qué modelo te interesa? Tenemos: {', '.join(modelos)}."
            logger.debug(f"Respuesta generada para {cliente_id} (sin modelo): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif all(k in estado for k in ["nombre", "tipo_auto", "tipo_vehiculo", "modelo"]) and not estado.get("confirmado"):
            transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))
            detalles = obtener_detalles_modelo(estado["modelo"])
            respuesta = f"{transicion} Confirmemos tus datos: {estado['tipo_auto']} {estado['modelo']} ({estado['tipo_vehiculo']}). {detalles.get('descripcion','')} ¿Es correcto? Responde 'Sí' o 'No'."
            logger.debug(f"Respuesta generada para {cliente_id} (sin confirmado): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif all(k in estado for k in ["nombre", "tipo_auto", "tipo_vehiculo", "modelo", "confirmado"]):
            transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))
            respuesta = f"{transicion} Hola {estado.get('nombre','')}, un asesor te contactará pronto para seguir con tu {estado.get('tipo_auto','')} {estado.get('modelo','')}."
            logger.debug(f"Respuesta generada para {cliente_id} (confirmado): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": True}

        # Respuesta fallback con Ollama
        historial_resumido = resumir_historial_emociones(historial, estado, memoria)
        prompt_base = (
            f"Eres Alex, asistente de ventas de Volkswagen Eurocity Culiacán. "
            f"Tu estilo es humano, cercano, profesional y amable. Usa emojis moderadamente para sonar natural. 😊 "
            f"Personaliza las respuestas según el historial y la memoria del cliente, evitando repetir preguntas innecesarias. "
            f"Si el cliente ya proporcionó información (como nombre o modelo), retómala para continuar la conversación. "
            f"Historial resumido:\n{historial_resumido}\n"
            f"Mensaje actual: {mensaje}"
        )

        try:
            response = ollama.generate(model="llama3", prompt=prompt_base)
            texto_respuesta = response["response"].strip()
        except Exception as e:
            logger.error(f"Error al llamar a ollama.generate: {str(e)}")
            texto_respuesta = "Lo siento, hubo un error generando la respuesta. 😔 Por favor, intenta de nuevo."

        # Actualizar memoria
        if "modelo" in estado:
            modelos_prev = memoria.get("modelos_favoritos", [])
            if estado["modelo"] not in modelos_prev:
                modelos_prev.append(estado["modelo"])
                actualizar_memoria_avanzada(cliente_id, {"modelos_favoritos": modelos_prev})
        if "tipo_auto" in estado:
            actualizar_memoria_avanzada(cliente_id, {"tipo_auto_preferido": estado["tipo_auto"]})
        if "tipo_vehiculo" in estado:
            actualizar_memoria_avanzada(cliente_id, {"tipo_vehiculo": estado["tipo_vehiculo"]})
        actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})

        logger.debug(f"Respuesta ollama generada para {cliente_id}: {texto_respuesta}")
        return {"respuesta": texto_respuesta, "enviar_a_asesor": False}

    except Exception as e:
        logger.error(f"Error al generar respuesta premium: {str(e)}")
        return {"respuesta": "Lo siento, hubo un error generando la respuesta. 😔 Por favor, intenta de nuevo.", "enviar_a_asesor": False}

# Asignación de asesores con mensaje humano
def asignar_asesor_humano(cliente_id: str):
    try:
        estado = obtener_estado(cliente_id)
        if not all(k in estado for k in ["telefono", "nombre", "tipo_auto", "tipo_vehiculo", "modelo", "confirmado"]):
            logger.warning(f"Datos incompletos para asignar asesor a {cliente_id}: {estado}")
            return

        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        if not asesores:
            logger.warning("No hay asesores disponibles")
            return

        asesor = asesores[0]["telefono"]
        mensaje_asesor = (
            f"Hola 👋 {asesor},\n"
            f"Tienes un nuevo cliente potencial: {estado['nombre']}. "
            f"Está interesado en un {estado['tipo_auto']} {estado['modelo']} ({estado['tipo_vehiculo']}).\n"
            f"¿Estás disponible para contactarlo ahora? Por favor responde 'Sí' o 'No'."
        )

        result = sends.insert_one({
            "jid": f"{asesor}@s.whatsapp.net",
            "message": {
                "text": mensaje_asesor,
                "buttons": [
                    {"buttonId": f"yes_{cliente_id}", "buttonText": {"displayText": "✅ Sí"}, "type": 1},
                    {"buttonId": f"no_{cliente_id}", "buttonText": {"displayText": "❌ No"}, "type": 1}
                ]
            },
            "sent": False
        })
        logger.debug(f"Mensaje a asesor guardado para {cliente_id}: ID={result.inserted_id}")

        mensaje_cliente = (
            f"Hola {estado['nombre']} 👋, "
            f"hemos confirmado tus datos: {estado['tipo_auto']} {estado['modelo']} ({estado['tipo_vehiculo']}). "
            f"Un asesor se pondrá en contacto contigo muy pronto para ayudarte."
        )
        result = sends.insert_one({
            "jid": cliente_id,
            "message": {"text": mensaje_cliente},
            "sent": False
        })
        logger.debug(f"Mensaje a cliente guardado para {cliente_id}: ID={result.inserted_id}")

        logger.info(f"Asignado asesor {asesor} a {cliente_id} con mensaje humano.")

    except Exception as e:
        logger.error(f"Error al asignar asesor humano: {str(e)}")

# ------------------ WHISPER ------------------
def transcribir_audio(audio_path: str) -> str:
    try:
        model = whisper.load_model("base")
        result = model.transcribe(audio_path, language="es")
        return result["text"].strip()
    except Exception as e:
        logger.error(f"Error transcribiendo audio: {e}")
        return "No pude transcribir el audio 😔"

# ------------------ WEBHOOK ------------------
@app.post("/webhook")
async def webhook(mensaje: Mensaje):
    try:
        cliente_id = mensaje.cliente_id
        texto = mensaje.texto.strip().lower()

        # Si es audio
        if mensaje.audio_path:
            texto = transcribir_audio(mensaje.audio_path).lower()

        logger.debug(f"Webhook recibido: cliente_id={cliente_id}, texto={texto}")

        # Validar cliente_id
        if not cliente_id or "@s.whatsapp.net" not in cliente_id:
            logger.error(f"cliente_id inválido: {cliente_id}")
            return {"respuesta": "Error: Identificador de cliente inválido. 😔 Por favor, intenta de nuevo."}

        estado = obtener_estado(cliente_id)
        historial = list(historial_col.find({"cliente_id": cliente_id}))

        # Evitar resetear si hay una conversación reciente confirmada
        if texto == "hola" and not estado.get("confirmado"):
            try:
                estado_conversacion.delete_one({"_id": cliente_id})
                memoria_col.delete_one({"_id": cliente_id})
                asignaciones.delete_one({"_id": cliente_id})
                sends.delete_many({"jid": cliente_id})
                logger.info(f"Estado reseteado forzosamente para {cliente_id} por 'hola' (sin confirmación previa)")
                estado = {}
            except Exception as e:
                logger.error(f"Error al resetear estado para {cliente_id}: {str(e)}")

        guardar_mensaje(cliente_id, texto, "user")

        if "telefono" not in estado and "@s.whatsapp.net" in cliente_id:
            phone = cliente_id.split("@")[0]
            if es_contacto_valido(phone):
                actualizar_estado(cliente_id, {"telefono": phone})
                estado["telefono"] = phone

        nombre, tipo_auto, tipo_vehiculo = parsear_entrada(texto)

        if "nombre" not in estado and nombre:
            actualizar_estado(cliente_id, {"nombre": nombre})
            estado["nombre"] = nombre
        if "nombre" in estado and "tipo_auto" not in estado and tipo_auto:
            actualizar_estado(cliente_id, {"tipo_auto": tipo_auto})
            estado["tipo_auto"] = tipo_auto
        if "nombre" in estado and "tipo_auto" in estado and "tipo_vehiculo" not in estado and tipo_vehiculo:
            actualizar_estado(cliente_id, {"tipo_vehiculo": tipo_vehiculo})
            estado["tipo_vehiculo"] = tipo_vehiculo

        # Detectar modelo y confirmación
        texto_norm = texto.lower()
        if "nombre" in estado and "tipo_auto" in estado and "tipo_vehiculo" in estado and "modelo" not in estado:
            for model in ['jetta', 'tiguan', 'virtus', 'taos', 'teramont', 't-cross', 'polo']:
                if model in texto_norm:
                    actualizar_estado(cliente_id, {"modelo": model.title()})
                    estado["modelo"] = model.title()
                    break
        elif all(k in estado for k in ["nombre", "tipo_auto", "tipo_vehiculo", "modelo"]) and texto_norm in ["sí", "si"]:
            actualizar_estado(cliente_id, {"confirmado": True})
            estado["confirmado"] = True

        historial_texto = [{"role": h["role"], "mensaje": h["mensaje"]} for h in historial]
        result = generar_respuesta_premium(texto, historial_texto, {**estado, "cliente_id": cliente_id})
        respuesta = result["respuesta"]
        enviar_a_asesor = result["enviar_a_asesor"]

        guardar_mensaje(cliente_id, respuesta, "assistant")
        sends.insert_one({"jid": cliente_id, "message": {"text": respuesta}, "sent": False})
        logger.debug(f"Respuesta guardada para enviar a {cliente_id}: {respuesta}, ID={result.inserted_id}")


        if enviar_a_asesor:
            asignar_asesor_humano(cliente_id)

        return {"respuesta": respuesta}

    except Exception as e:
        logger.error(f"Error en webhook: {str(e)}")
        return {"respuesta": "Lo siento, hubo un error generando la respuesta. 😔 Por favor, intenta de nuevo."}

# Respuesta de asesor
@app.post("/advisor_response")
async def advisor_response(response: AdvisorResponse):
    try:
        cliente_id = response.cliente_id
        respuesta = response.respuesta.lower()
        asesor_phone = response.asesor_phone
        result = asignaciones.update_one(
            {"cliente_id": cliente_id, "asesor_phone": asesor_phone},
            {"$set": {"respuesta": respuesta, "fecha_respuesta": datetime.now()}}
        )
        logger.debug(f"Respuesta de asesor actualizada para {cliente_id}: {respuesta}, Resultado: matched={result.matched_count}, modified={result.modified_count}")
        estado = obtener_estado(cliente_id)
        if respuesta == "yes":
            mensaje = (
                f"Contacta a {estado['nombre']} interesado en adquirir un auto ({estado['tipo_auto']}) "
                f"{estado['modelo']} ({estado['tipo_vehiculo']}) al número {estado['telefono']}"
            )
            result = sends.insert_one({
                "jid": f"{asesor_phone}@s.whatsapp.net",
                "message": {"text": mensaje},
                "sent": False
            })
            logger.debug(f"Mensaje a asesor guardado para {cliente_id}: ID={result.inserted_id}")
            result = sends.insert_one({
                "jid": cliente_id,
                "message": {"text": f"Hola, {estado['nombre']}. Un asesor te contactará pronto."},
                "sent": False
            })
            logger.debug(f"Mensaje a cliente guardado para {cliente_id}: ID={result.inserted_id}")
        elif respuesta == "no":
            asignar_asesor_humano(cliente_id)
        logger.info(f"Respuesta de asesor procesada para {cliente_id}: {respuesta}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error en advisor_response: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error del servidor: {str(e)}")

# Obtener asesores
@app.get("/get_asesores")
def get_asesores():
    try:
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        logger.debug(f"Asesores recuperados: {[a['telefono'] for a in asesores if 'telefono' in a]}")
        return [a["telefono"] for a in asesores if "telefono" in a]
    except Exception as e:
        logger.error(f"Error en /get_asesores: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error del servidor: {str(e)}")

# Ejecutar servidor
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
