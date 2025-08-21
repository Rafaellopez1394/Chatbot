from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
import ollama
import re
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import uvicorn
import random
import logging

# Configurar logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', filename='chatbot.log')
logger = logging.getLogger(__name__)

app = FastAPI()

# Conexión a MongoDB
try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)
    client.admin.command('ping')
    logger.info("Conexión a MongoDB exitosa")
except Exception as e:
    logger.error(f"Fallo en la conexión a MongoDB: {str(e)}")
    raise

db = client["chatbotdb"]
historial_col = db["historial"]
estado_conversacion = db["estado_conversacion"]
asignaciones = db["asignaciones"]
asesores_col = db["asesores"]
sends = db["sends"]
memoria_col = db["memoria_clientes"]

# Inicializar colección de asesores
def inicializar_asesores():
    try:
        existing = list(asesores_col.find())
        if not existing or not all("telefono" in doc and "activo" in doc for doc in existing):
            asesores_col.delete_many({})
            asesores_data = [
                {"nombre": "Ana", "area": "Ventas", "activo": True, "telefono": "526879388889"},
            ]
            asesores_col.insert_many(asesores_data)
            logger.info(f"Asesores inicializados correctamente: {asesores_data}")
        else:
            logger.info("La colección de asesores ya está correctamente poblada")
    except Exception as e:
        logger.error(f"Error al inicializar asesores: {str(e)}")

inicializar_asesores()

# Scheduler para reasignar leads pendientes
scheduler = BackgroundScheduler()
try:
    scheduler.start()
    logger.info("Scheduler iniciado correctamente")
except Exception as e:
    logger.error(f"Error al iniciar scheduler: {str(e)}")
    raise

def reasignar_pendientes():
    try:
        limite = datetime.now() - timedelta(minutes=5)
        pendientes = asignaciones.find({"respuesta": None, "fecha": {"$lt": limite}})
        for p in pendientes:
            asignar_asesor_humano(p["cliente_id"])
    except Exception as e:
        logger.error(f"Error al reasignar pendientes: {str(e)}")

scheduler.add_job(reasignar_pendientes, 'interval', minutes=1)

# Modelos
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

class AdvisorResponse(BaseModel):
    cliente_id: str
    respuesta: str
    asesor_phone: str

# Funciones auxiliares
def actualizar_estado(cliente_id: str, nuevo_estado: dict):
    try:
        estado_conversacion.update_one(
            {"_id": cliente_id}, {"$set": nuevo_estado}, upsert=True
        )
        logger.debug(f"Estado actualizado para {cliente_id}: {nuevo_estado}")
    except Exception as e:
        logger.error(f"Error al actualizar estado para {cliente_id}: {str(e)}")

def obtener_estado(cliente_id: str) -> dict:
    try:
        estado = estado_conversacion.find_one({"_id": cliente_id})
        logger.debug(f"Estado recuperado para {cliente_id}: {estado}")
        return estado if estado else {}
    except Exception as e:
        logger.error(f"Error al recuperar estado para {cliente_id}: {str(e)}")
        return {}

def guardar_mensaje(cliente_id: str, mensaje: str, role: str):
    try:
        historial_col.insert_one({
            "cliente_id": cliente_id,
            "mensaje": mensaje,
            "role": role,
            "fecha": datetime.now()
        })
        logger.debug(f"Mensaje guardado para {cliente_id}: {mensaje} ({role})")
    except Exception as e:
        logger.error(f"Error al guardar mensaje para {cliente_id}: {str(e)}")

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
        for url in urls:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')
            for tag in soup.find_all(['h2','h3','a','li','div']):
                text = tag.get_text(strip=True).lower()
                if any(model in text for model in ['jetta', 'tiguan', 'virtus', 'taos', 'teramont', 't-cross', 'polo']):
                    modelos.add(text.title())
        modelos_list = list(modelos) if modelos else ["Jetta", "Tiguan", "Taos"]
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
        secciones = soup.find_all(['h2','h3','p','li','div'])
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
        memoria_col.update_one(
            {"_id": cliente_id},
            {"$set": nuevo_dato},
            upsert=True
        )
        logger.debug(f"Memoria actualizada para {cliente_id}: {nuevo_dato}")
    except Exception as e:
        logger.error(f"Error al actualizar memoria para {cliente_id}: {str(e)}")

def obtener_memoria_avanzada(cliente_id: str) -> dict:
    try:
        memoria = memoria_col.find_one({"_id": cliente_id})
        if not memoria:
            memoria = {"modelos_favoritos": [], "tipo_auto_preferido": None, "emociones": [], "ultima_pregunta": ""}
        logger.debug(f"Memoria recuperada para {cliente_id}: {memoria}")
        return memoria
    except Exception as e:
        logger.error(f"Error al recuperar memoria para {cliente_id}: {str(e)}")
        return {"modelos_favoritos": [], "tipo_auto_preferido": None, "emociones": [], "ultima_pregunta": ""}

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
        estado_str = ", ".join([f"{k}: {v}" for k, v in estado.items() if k in ["nombre", "tipo_auto", "modelo", "confirmado"]])
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

# Parsear entrada para extraer nombre y tipo de auto
def parsear_entrada(texto: str) -> tuple:
    texto = texto.lower().strip()
    nombre = None
    tipo_auto = None

    # Extraer nombre (primera palabra que no esté en ignored_inputs)
    ignored_inputs = [
        "hola", "me interesa un auto", "no se", "no sé", "okey", "hey", "si", "sí",
        "si esta bien", "claro", "ok", "no tengo alguno en mente", "no tengo un modelo aun",
        "y", "busco", "carro", "vehículo", "auto"
    ]
    palabras = texto.split()
    for palabra in palabras:
        if palabra not in ignored_inputs and not any(p in palabra for p in ["nuevo", "usado"]):
            nombre = palabra.title()
            break

    # Extraer tipo de auto
    if "nuevo" in texto:
        tipo_auto = "nuevo"
    elif "usado" in texto:
        tipo_auto = "usado"

    logger.debug(f"Parseado entrada '{texto}': nombre={nombre}, tipo_auto={tipo_auto}")
    return nombre, tipo_auto

# Generar respuesta premium
def generar_respuesta_premium(mensaje: str, historial: list, estado: dict) -> dict:
    try:
        cliente_id = estado.get("cliente_id", "")
        memoria = obtener_memoria_avanzada(cliente_id)
        emocion_actual = detectar_emocion(mensaje)
        if emocion_actual not in memoria["emociones"]:
            memoria["emociones"].append(emocion_actual)
            actualizar_memoria_avanzada(cliente_id, {"emociones": memoria["emociones"]})

        saludos = {
            "positivo": ["¡Qué gusto verte, {nombre}!", "¡Hola {nombre}, me alegra que estés entusiasmado!"],
            "indeciso": ["Hola {nombre}, no te preocupes, te guiaré paso a paso.", "¡Hola {nombre}! Vamos a explorar opciones juntos."],
            "negativo": ["Hola {nombre}, estoy aquí para ayudarte y aclarar cualquier duda.", "Hola {nombre}, vamos a buscar la mejor opción para ti."],
            "neutral": ["Hola {nombre}, encantado de saludarte.", "¡Hola {nombre}! Vamos a ver algunas opciones."]
        }
        transiciones = {
            "positivo": ["¡Perfecto!", "¡Genial!"],
            "indeciso": ["Entiendo, vamos paso a paso.", "Muy bien, veamos opciones."],
            "negativo": ["No te preocupes, encontraremos la mejor opción.", "Vamos a ver alternativas que te convengan."],
            "neutral": ["Muy bien, continuemos.", "Perfecto, sigamos."]
        }

        saludo = random.choice(saludos.get(emocion_actual, saludos["neutral"])).format(nombre=estado.get("nombre", ""))
        transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))

        # Flujo simplificado para pasos iniciales
        if not estado.get("nombre"):
            respuesta = "¡Hola! Bienvenido a Volkswagen Eurocity Culiacan. Soy Alex, Para poder atenderte mejor, ¿podrías indicarme tu nombre, por favor? Además, me gustaría saber si estás buscando un vehículo nuevo o usado."
            logger.debug(f"Respuesta generada para {cliente_id} (sin nombre): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif "nombre" in estado and not estado.get("tipo_auto"):
            respuesta = f"{transicion} ¿Buscas un auto nuevo o usado?"
            logger.debug(f"Respuesta generada para {cliente_id} (sin tipo_auto): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif "nombre" in estado and "tipo_auto" in estado and not estado.get("modelo"):
            modelos_web = obtener_modelos_oficiales()
            modelos = memoria.get("modelos_favoritos", modelos_web)
            respuesta = f"{transicion} Ahora, ¿qué modelo te interesa? Tenemos: {', '.join(modelos)}"
            logger.debug(f"Respuesta generada para {cliente_id} (sin modelo): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif all(k in estado for k in ["nombre", "tipo_auto", "modelo"]) and not estado.get("confirmado"):
            detalles = obtener_detalles_modelo(estado["modelo"])
            respuesta = f"{transicion} Confirmemos tus datos: {estado['tipo_auto']} {estado['modelo']}. {detalles.get('descripcion','')} ¿Es correcto? Responde 'Sí' o 'No'."
            logger.debug(f"Respuesta generada para {cliente_id} (sin confirmado): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": False}
        elif all(k in estado for k in ["nombre", "tipo_auto", "modelo", "confirmado"]):
            respuesta = f"Hola {estado.get('nombre','')}, un asesor te contactará pronto."
            logger.debug(f"Respuesta generada para {cliente_id} (confirmado): {respuesta}")
            actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})
            return {"respuesta": respuesta, "enviar_a_asesor": True}

        # Respuesta fallback con Ollama
        historial_resumido = resumir_historial_emociones(historial, estado, memoria)
        prompt_base = (
            f"{saludo}\n"
            f"Eres Alex, asistente de ventas de Volkswagen Eurocity Culiacan. "
            f"Tu estilo es humano, cercano, profesional y amable. "
            f"Usa la memoria del cliente y su historial para personalizar respuestas.\n"
            f"Historial resumido:\n{historial_resumido}\n"
            f"Mensaje actual: {mensaje}"
        )

        try:
            response = ollama.generate(model="llama3", prompt=prompt_base)
            texto_respuesta = response["response"].strip()
        except Exception as e:
            logger.error(f"Error al llamar a ollama.generate: {str(e)}")
            texto_respuesta = "Lo siento, hubo un error generando la respuesta. Por favor intenta de nuevo."

        # Actualizar memoria
        if "modelo" in estado:
            modelos_prev = memoria.get("modelos_favoritos", [])
            if estado["modelo"] not in modelos_prev:
                modelos_prev.append(estado["modelo"])
                actualizar_memoria_avanzada(cliente_id, {"modelos_favoritos": modelos_prev})
        if "tipo_auto" in estado:
            actualizar_memoria_avanzada(cliente_id, {"tipo_auto_preferido": estado["tipo_auto"]})
        actualizar_memoria_avanzada(cliente_id, {"ultima_pregunta": mensaje})

        logger.debug(f"Respuesta ollama generada para {cliente_id}: {texto_respuesta}")
        return {"respuesta": texto_respuesta, "enviar_a_asesor": False}

    except Exception as e:
        logger.error(f"Error al generar respuesta premium: {str(e)}")
        return {"respuesta": "Lo siento, hubo un error generando la respuesta. Por favor intenta de nuevo.", "enviar_a_asesor": False}

# Asignación de asesores con mensaje humano
def asignar_asesor_humano(cliente_id: str):
    try:
        estado = obtener_estado(cliente_id)
        if not all(k in estado for k in ["telefono", "nombre", "tipo_auto", "modelo", "confirmado"]):
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
            f"Está interesado en un {estado['tipo_auto']} {estado['modelo']}.\n"
            f"¿Estás disponible para contactarlo ahora? Por favor responde 'Sí' o 'No'."
        )

        sends.insert_one({
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

        asignaciones.insert_one({
            "cliente_id": cliente_id,
            "asesor_phone": asesor,
            "respuesta": None,
            "fecha": datetime.now()
        })

        mensaje_cliente = (
            f"Hola {estado['nombre']} 👋, "
            f"hemos confirmado tus datos: {estado['tipo_auto']} {estado['modelo']}. "
            f"Un asesor se pondrá en contacto contigo muy pronto para ayudarte."
        )
        sends.insert_one({
            "jid": cliente_id,
            "message": {"text": mensaje_cliente},
            "sent": False
        })

        logger.info(f"Asignado asesor {asesor} a {cliente_id} con mensaje humano.")

    except Exception as e:
        logger.error(f"Error al asignar asesor humano: {str(e)}")

# Webhook principal
@app.post("/webhook")
async def webhook(mensaje: Mensaje):
    try:
        cliente_id = mensaje.cliente_id
        texto = mensaje.texto.strip().lower()
        logger.debug(f"Webhook recibido: cliente_id={cliente_id}, texto={texto}")

        # Forzar reset de estado para "hola"
        ignored_inputs = [
            "hola", "me interesa un auto", "no se", "no sé", "okey", "hey", "si", "sí",
            "si esta bien", "claro", "ok", "no tengo alguno en mente", "no tengo un modelo aun"
        ]
        estado = obtener_estado(cliente_id)
        if texto == "hola":
            estado_conversacion.delete_one({"_id": cliente_id})
            memoria_col.delete_one({"_id": cliente_id})
            asignaciones.delete_one({"_id": cliente_id})
            sends.delete_many({"jid": cliente_id})
            logger.info(f"Estado reseteado forzosamente para {cliente_id} por 'hola'")
            estado = {}

        guardar_mensaje(cliente_id, texto, "user")

        # Extraer número de teléfono de WhatsApp
        if "telefono" not in estado and "@s.whatsapp.net" in cliente_id:
            phone = cliente_id.split("@")[0]
            if es_contacto_valido(phone):
                actualizar_estado(cliente_id, {"telefono": phone})
                estado["telefono"] = phone
                logger.debug(f"Teléfono establecido para {cliente_id}: {phone}")

        # Parsear entrada para extraer nombre y tipo de auto
        nombre, tipo_auto = parsear_entrada(texto)
        if "nombre" not in estado and nombre:
            actualizar_estado(cliente_id, {"nombre": nombre})
            estado["nombre"] = nombre
            logger.debug(f"Nombre establecido para {cliente_id}: {nombre}")
        if "nombre" in estado and "tipo_auto" not in estado and tipo_auto:
            actualizar_estado(cliente_id, {"tipo_auto": tipo_auto})
            estado["tipo_auto"] = tipo_auto
            logger.debug(f"Tipo de auto establecido para {cliente_id}: {tipo_auto}")
        elif "nombre" in estado and "tipo_auto" in estado and "modelo" not in estado:
            for model in ['jetta', 'tiguan', 'virtus', 'taos', 'teramont', 't-cross', 'polo']:
                if model in texto:
                    actualizar_estado(cliente_id, {"modelo": model.title()})
                    estado["modelo"] = model.title()
                    logger.debug(f"Modelo establecido para {cliente_id}: {model.title()}")
                    break
        elif all(k in estado for k in ["nombre", "tipo_auto", "modelo"]) and texto in ["sí", "si"]:
            actualizar_estado(cliente_id, {"confirmado": True})
            estado["confirmado"] = True
            logger.debug(f"Confirmado establecido para {cliente_id}: True")

        historial = list(historial_col.find({"cliente_id": cliente_id}))
        historial_texto = [{"role": h["role"], "mensaje": h["mensaje"]} for h in historial]

        result = generar_respuesta_premium(texto, historial_texto, {**estado, "cliente_id": cliente_id})
        respuesta = result["respuesta"]
        enviar_a_asesor = result["enviar_a_asesor"]

        guardar_mensaje(cliente_id, respuesta, "assistant")
        sends.insert_one({
            "jid": cliente_id,
            "message": {"text": respuesta},
            "sent": False
        })

        if enviar_a_asesor:
            asignar_asesor_humano(cliente_id)

        logger.info(f"Respuesta enviada a {cliente_id}: {respuesta}")
        return {"respuesta": respuesta}

    except Exception as e:
        logger.error(f"Error en webhook: {str(e)}")
        return {"respuesta": "Lo siento, hubo un error generando la respuesta. Por favor intenta de nuevo."}

# Respuesta de asesor
@app.post("/advisor_response")
async def advisor_response(response: AdvisorResponse):
    try:
        cliente_id = response.cliente_id
        respuesta = response.respuesta.lower()
        asesor_phone = response.asesor_phone
        asignaciones.update_one(
            {"cliente_id": cliente_id, "asesor_phone": asesor_phone},
            {"$set": {"respuesta": respuesta, "fecha_respuesta": datetime.now()}}
        )
        estado = obtener_estado(cliente_id)
        if respuesta == "yes":
            mensaje = (
                f"Contacta a {estado['nombre']} interesado en adquirir un auto ({estado['tipo_auto']}) "
                f"{estado['modelo']} al número {estado['telefono']}"
            )
            sends.insert_one({
                "jid": f"{asesor_phone}@s.whatsapp.net",
                "message": {"text": mensaje},
                "sent": False
            })
            sends.insert_one({
                "jid": cliente_id,
                "message": {"text": f"Hola, {estado['nombre']}. Un asesor te contactará pronto."},
                "sent": False
            })
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