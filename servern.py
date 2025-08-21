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

app = FastAPI()

# ------------------------------
# MongoDB connection
# ------------------------------
client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)
try:
    client.admin.command('ping')
    print("MongoDB connection successful")
except Exception as e:
    print(f"MongoDB connection failed: {str(e)}")
    raise

db = client["chatbotdb"]
historial_col = db["historial"]
estado_conversacion = db["estado_conversacion"]
asignaciones = db["asignaciones"]
asesores_col = db["asesores"]
sends = db["sends"]
memoria_col = db["memoria_clientes"]  # Memoria avanzada

# ------------------------------
# Initialize asesores collection
# ------------------------------
def inicializar_asesores():
    try:
        existing = list(asesores_col.find())
        if not existing or not all("telefono" in doc and "activo" in doc for doc in existing):
            asesores_col.delete_many({})
            asesores_data = [
                {"nombre": "Ana", "area": "Ventas", "activo": True, "telefono": "526879388889"},
            ]
            asesores_col.insert_many(asesores_data)
            print("Asesores initialized successfully:", asesores_data)
        else:
            print("Asesores collection already populated correctly")
    except Exception as e:
        print(f"Error initializing asesores: {str(e)}")

inicializar_asesores()

# ------------------------------
# Scheduler for reassigning pending leads
# ------------------------------
scheduler = BackgroundScheduler()
try:
    scheduler.start()
    print("Scheduler started successfully")
except Exception as e:
    print(f"Error starting scheduler: {str(e)}")
    raise

def reasignar_pendientes():
    try:
        limite = datetime.now() - timedelta(minutes=5)
        pendientes = asignaciones.find({"respuesta": None, "fecha": {"$lt": limite}})
        for p in pendientes:
            asignar_asesor_humano(p["cliente_id"])
    except Exception as e:
        print(f"Error reasignando pendientes: {str(e)}")

scheduler.add_job(reasignar_pendientes, 'interval', minutes=1)

# ------------------------------
# Models
# ------------------------------
class Mensaje(BaseModel):
    cliente_id: str
    texto: str

class AdvisorResponse(BaseModel):
    cliente_id: str
    respuesta: str
    asesor_phone: str

# ------------------------------
# Helper functions
# ------------------------------
def actualizar_estado(cliente_id: str, nuevo_estado: dict):
    estado_conversacion.update_one(
        {"_id": cliente_id}, {"$set": nuevo_estado}, upsert=True
    )

def obtener_estado(cliente_id: str) -> dict:
    estado = estado_conversacion.find_one({"_id": cliente_id})
    return estado if estado else {}

def guardar_mensaje(cliente_id: str, mensaje: str, role: str):
    historial_col.insert_one({
        "cliente_id": cliente_id,
        "mensaje": mensaje,
        "role": role,
        "fecha": datetime.now()
    })

def es_contacto_valido(telefono: str) -> bool:
    return bool(re.match(r'^\d{10,}$', telefono))

# ------------------------------
# Extracción de datos oficiales
# ------------------------------
def obtener_modelos_oficiales():
    """Obtiene modelos disponibles de VW Eurocity desde las páginas oficiales."""
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
        return list(modelos) if modelos else ["Jetta", "Tiguan", "Taos"]
    except Exception as e:
        print(f"Error obteniendo modelos oficiales: {str(e)}")
        return ["Jetta", "Tiguan", "Taos"]

def obtener_detalles_modelo(modelo: str):
    """Extrae características de un modelo específico desde la página oficial."""
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
        return info if info else {"descripcion": f"Información de {modelo} no disponible en línea."}
    except Exception as e:
        print(f"Error obteniendo detalles del modelo {modelo}: {str(e)}")
        return {"descripcion": f"Error al consultar el modelo {modelo}"}

# ------------------------------
# Memoria avanzada
# ------------------------------
def actualizar_memoria_avanzada(cliente_id: str, nuevo_dato: dict):
    memoria_col.update_one(
        {"_id": cliente_id},
        {"$set": nuevo_dato},
        upsert=True
    )

def obtener_memoria_avanzada(cliente_id: str) -> dict:
    memoria = memoria_col.find_one({"_id": cliente_id})
    if not memoria:
        memoria = {"modelos_favoritos": [], "tipo_auto_preferido": None, "emociones": []}
    return memoria

def detectar_emocion(texto: str) -> str:
    texto = texto.lower()
    if any(p in texto for p in ["emocionado", "genial", "excelente", "perfecto", "okey"]):
        return "positivo"
    elif any(p in texto for p in ["no sé", "no estoy seguro", "tal vez", "quizás", "dudoso"]):
        return "indeciso"
    elif any(p in texto for p in ["triste", "no me gusta", "malo", "difícil"]):
        return "negativo"
    else:
        return "neutral"

def resumir_historial_emociones(historial, estado, memoria, max_msgs=5):
    resumen = []
    for h in historial[-max_msgs:]:
        role = "Usuario" if h["role"] == "user" else "Asistente"
        mensaje = h["mensaje"]
        resumen.append(f"{role}: {mensaje}")
    estado_str = ", ".join([f"{k}: {v}" for k, v in estado.items() if k in ["nombre", "tipo_auto", "modelo"]])
    resumen.append(f"[Resumen de estado: {estado_str}]")
    memoria_str = ", ".join([f"{k}: {v}" for k, v in memoria.items() if k != "emociones"])
    emociones_str = ", ".join(memoria.get("emociones", []))
    resumen.append(f"[Memoria del cliente: {memoria_str}]")
    resumen.append(f"[Emociones detectadas: {emociones_str}]")
    return "\n".join(resumen)

# ------------------------------
# Generar respuesta premium
# ------------------------------
def generar_respuesta_premium(mensaje: str, historial: list, estado: dict) -> str:
    try:
        cliente_id = estado.get("cliente_id")
        memoria = obtener_memoria_avanzada(cliente_id)
        historial_resumido = resumir_historial_emociones(historial, estado, memoria)
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

        saludo = random.choice(saludos.get(emocion_actual, saludos["neutral"])).format(nombre=estado.get("nombre",""))
        transicion = random.choice(transiciones.get(emocion_actual, transiciones["neutral"]))

        prompt_base = (
            f"{saludo}\n"
            f"Eres Alex, asistente de ventas de Volkswagen Eurocity Culiacan. "
            f"Tu estilo es humano, cercano, profesional y amable. "
            f"Usa la memoria del cliente y su historial para personalizar respuestas.\n"
            f"Historial resumido:\n{historial_resumido}\n"
            f"Mensaje actual: {mensaje}"
        )

        if not estado.get("nombre"):
            prompt_base += f"\nPrimer contacto: {saludo} ¿Cuál es tu nombre y buscas un auto nuevo o usado?"
        elif "nombre" in estado and not estado.get("tipo_auto"):
            prompt_base += f"\n{transicion} ¿Buscas un auto nuevo o usado?"
        elif "nombre" in estado and "tipo_auto" in estado and not estado.get("modelo"):
            modelos_web = obtener_modelos_oficiales()
            modelos = memoria.get("modelos_favoritos", modelos_web)
            prompt_base += f"\n{transicion} Ahora, ¿qué modelo te interesa? Tenemos: {', '.join(modelos)}"
        elif all(k in estado for k in ["nombre", "tipo_auto", "modelo"]) and not estado.get("confirmado"):
            detalles = obtener_detalles_modelo(estado["modelo"])
            prompt_base += f"\n{transicion} Confirmemos tus datos: {estado['tipo_auto']} {estado['modelo']}. {detalles.get('descripcion','')} ¿Es correcto? Responde 'Sí' o 'No'."
        else:
            prompt_base += "\nResponde de manera natural y breve, indicando que un asesor contactará al cliente pronto."

        response = ollama.generate(model="llama3", prompt=prompt_base)
        texto_respuesta = response["response"].strip()

        # Actualizar memoria con modelo y tipo de auto
        if "modelo" in estado:
            modelos_prev = memoria.get("modelos_favoritos", [])
            if estado["modelo"] not in modelos_prev:
                modelos_prev.append(estado["modelo"])
                actualizar_memoria_avanzada(cliente_id, {"modelos_favoritos": modelos_prev})
        if "tipo_auto" in estado:
            actualizar_memoria_avanzada(cliente_id, {"tipo_auto_preferido": estado["tipo_auto"]})

        return texto_respuesta

    except Exception as e:
        print(f"Error generando respuesta premium: {str(e)}")
        return "Lo siento, hubo un error generando la respuesta. Por favor intenta de nuevo."

# ------------------------------
# Asignación de asesores con mensaje humano
# ------------------------------
def asignar_asesor_humano(cliente_id: str):
    try:
        estado = obtener_estado(cliente_id)
        if not all(k in estado for k in ["telefono", "nombre", "tipo_auto", "modelo"]):
            print(f"Datos incompletos para asignar asesor a {cliente_id}")
            return

        # Obtener asesores activos
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        if not asesores:
            print("No hay asesores disponibles")
            return

        # Seleccionar primer asesor disponible
        asesor = asesores[0]["telefono"]

        # Crear mensaje humano para el asesor
        mensaje_asesor = (
            f"Hola 👋 {asesor},\n"
            f"Tienes un nuevo cliente potencial: {estado['nombre']}. "
            f"Está interesado en un {estado['tipo_auto']} {estado['modelo']}.\n"
            f"¿Estás disponible para contactarlo ahora? Por favor responde 'Sí' o 'No'."
        )

        # Guardar en la colección sends
        sends.insert_one({
            "jid": f"{asesor}@s.whatsapp.net",
            "message": {"text": mensaje_asesor},
            "sent": False
        })

        # Registrar asignación pendiente
        asignaciones.insert_one({
            "cliente_id": cliente_id,
            "asesor_phone": asesor,
            "respuesta": None,
            "fecha": datetime.now()
        })

        # Enviar mensaje humano al cliente confirmando que un asesor lo contactará pronto
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

        print(f"Asignado asesor {asesor} a {cliente_id} con mensaje humano.")

    except Exception as e:
        print(f"Error asignando asesor humano: {str(e)}")

# ------------------------------
# Webhook principal
# ------------------------------
@app.post("/webhook")
async def webhook(mensaje: Mensaje):
    try:
        cliente_id = mensaje.cliente_id
        texto = mensaje.texto.strip()
        estado = obtener_estado(cliente_id)
        guardar_mensaje(cliente_id, texto, "user")

        if "telefono" not in estado and "@s.whatsapp.net" in cliente_id:
            phone = cliente_id.split("@")[0]
            if es_contacto_valido(phone):
                actualizar_estado(cliente_id, {"telefono": phone})
                estado["telefono"] = phone

        if "nombre" not in estado and texto.lower() not in ["hola", "me interesa un auto", "no se", "no sé", "okey", "hey", "si"]:
            actualizar_estado(cliente_id, {"nombre": texto.title()})
        elif "nombre" in estado and "tipo_auto" not in estado and texto.lower() in ["nuevo", "usado"]:
            actualizar_estado(cliente_id, {"tipo_auto": texto.lower()})
        elif "nombre" in estado and "tipo_auto" in estado and "modelo" not in estado:
            for model in ['jetta', 'tiguan', 'virtus', 'taos', 'teramont', 't-cross', 'polo']:
                if model in texto.lower():
                    actualizar_estado(cliente_id, {"modelo": model.title()})
                    break

        historial = list(historial_col.find({"cliente_id": cliente_id}))
        historial_texto = [{"role": h["role"], "mensaje": h["mensaje"]} for h in historial]

        if "confirmado" not in estado:
            respuesta = generar_respuesta_premium(texto, historial_texto, {**estado, "cliente_id": cliente_id})
            guardar_mensaje(cliente_id, respuesta, "assistant")
            if all(k in estado for k in ["nombre", "tipo_auto", "modelo"]):
                actualizar_estado(cliente_id, {"confirmado": True})
                asignar_asesor_humano(cliente_id)
                respuesta = f"Perfecto, {estado['nombre']}. He asignado un asesor para ayudarte con tu {estado['tipo_auto']} {estado['modelo']}. Te contactará pronto."
        else:
            respuesta = f"Hola {estado['nombre']}, un asesor te contactará pronto."

        return {"respuesta": respuesta}

    except Exception as e:
        print(f"Error in webhook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

# ------------------------------
# Respuesta de asesor (sin cambios)
# ------------------------------
@app.post("/advisor_response")
async def advisor_response(response: AdvisorResponse):
    try:
        cliente_id = response.cliente_id
        respuesta = response.respuesta
        asesor_phone = response.asesor_phone
        asignaciones.update_one(
            {"cliente_id": cliente_id, "asesor_phone": asesor_phone},
            {"$set": {"respuesta": respuesta, "fecha_respuesta": datetime.now()}}
        )
        estado = obtener_estado(cliente_id)
        if respuesta.lower() == "yes":
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
        elif respuesta.lower() == "no":
            asignar_asesor_humano(cliente_id)
        return {"status": "success"}
    except Exception as e:
        print(f"Error in advisor_response: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

# ------------------------------
# Obtener asesores
# ------------------------------
@app.get("/get_asesores")
def get_asesores():
    try:
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        return [a["telefono"] for a in asesores if "telefono" in a]
    except Exception as e:
        print(f"Error in /get_asesores: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

# ------------------------------
# Run server
# ------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
