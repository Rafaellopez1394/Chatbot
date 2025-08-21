from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime
import re

app = FastAPI()

# ------------------------------
# MongoDB connection
# ------------------------------
client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=5000)
try:
    client.admin.command('ping')
    print("MongoDB connected")
except Exception as e:
    print(f"MongoDB connection failed: {e}")
    raise

db = client["chatbotdb"]
historial_col = db["historial"]
estado_conversacion = db["estado_conversacion"]
asignaciones = db["asignaciones"]
asesores_col = db["asesores"]
sends = db["sends"]
memoria_col = db["memoria_clientes"]

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

def guardar_memoria(cliente_id: str, key: str, value):
    memoria_col.update_one(
        {"cliente_id": cliente_id},
        {"$set": {key: value}},
        upsert=True
    )

def obtener_memoria(cliente_id: str) -> dict:
    memoria = memoria_col.find_one({"cliente_id": cliente_id})
    return memoria if memoria else {}

def es_contacto_valido(telefono: str) -> bool:
    return bool(re.match(r'^\d{10,}$', telefono))

def asignar_asesor(cliente_id: str) -> str:
    """Asignar primer asesor disponible"""
    asesor = asesores_col.find_one({"activo": True})
    if asesor:
        asignaciones.update_one(
            {"cliente_id": cliente_id},
            {"$set": {"asesor_phone": asesor["telefono"], "fecha_asignacion": datetime.now(), "respuesta": None}},
            upsert=True
        )
        return asesor["telefono"]
    return None

def generar_respuesta(cliente_id: str, texto: str, estado: dict, memoria: dict) -> dict:
    """Devuelve respuesta y flag si se debe enviar a asesor"""
    texto_lower = texto.lower()
    
    # Guardamos la última emoción simple (positivo/negativo)
    if any(word in texto_lower for word in ["gracias", "perfecto", "excelente"]):
        actualizar_estado(cliente_id, {"emocion": "positivo"})
    elif any(word in texto_lower for word in ["malo", "error", "problema"]):
        actualizar_estado(cliente_id, {"emocion": "negativo"})

    # Flujo inteligente
    if "nombre" not in estado:
        actualizar_estado(cliente_id, {"nombre": texto.title()})
        return {"respuesta": f"Hola {texto.title()}, ¿buscas un auto nuevo o usado?", "enviar_a_asesor": False}
    
    if "tipo_auto" not in estado and texto_lower in ["nuevo", "usado"]:
        actualizar_estado(cliente_id, {"tipo_auto": texto_lower})
        return {"respuesta": f"Perfecto {estado.get('nombre','')}, ¿qué modelo te interesa?", "enviar_a_asesor": False}
    
    if "modelo" not in estado:
        actualizar_estado(cliente_id, {"modelo": texto.title()})
        return {"respuesta": f"Gracias {estado.get('nombre','')}. Confirmemos: {estado.get('tipo_auto','')} {texto.title()} ¿Es correcto? Responde 'Sí' o 'No'.", "enviar_a_asesor": False}
    
    if "confirmado" not in estado and texto_lower in ["sí", "si"]:
        actualizar_estado(cliente_id, {"confirmado": True})
        # Asignar asesor y enviar botones
        asesor_phone = asignar_asesor(cliente_id)
        if asesor_phone:
            mensaje_asesor = f"Cliente {estado.get('nombre')} confirma {estado.get('tipo_auto')} {estado.get('modelo')}. ¿Disponible para contactar?"
            sends.insert_one({
                "jid": f"{asesor_phone}@s.whatsapp.net",
                "message": {
                    "text": mensaje_asesor,
                    "buttons": [
                        {"buttonId": f"yes_{cliente_id}", "buttonText": {"displayText": "✅ Sí"}, "type": 1},
                        {"buttonId": f"no_{cliente_id}", "buttonText": {"displayText": "❌ No"}, "type": 1}
                    ]
                },
                "sent": False
            })
        return {"respuesta": f"Perfecto, {estado.get('nombre','')}. Un asesor te contactará pronto.", "enviar_a_asesor": True}
    
    return {"respuesta": f"Hola {estado.get('nombre','')}, un asesor se pondrá en contacto contigo.", "enviar_a_asesor": False}

# ------------------------------
# Webhook principal
# ------------------------------
@app.post("/webhook")
async def webhook(mensaje: Mensaje):
    try:
        cliente_id = mensaje.cliente_id
        texto = mensaje.texto.strip()
        estado = obtener_estado(cliente_id)
        memoria = obtener_memoria(cliente_id)

        guardar_mensaje(cliente_id, texto, "user")

        # Guardar teléfono si es WhatsApp
        if "telefono" not in estado and "@s.whatsapp.net" in cliente_id:
            phone = cliente_id.split("@")[0]
            if es_contacto_valido(phone):
                actualizar_estado(cliente_id, {"telefono": phone})
                estado["telefono"] = phone

        result = generar_respuesta(cliente_id, texto, estado, memoria)
        respuesta = result["respuesta"]

        # Guardamos la respuesta en memoria y en cola
        guardar_mensaje(cliente_id, respuesta, "assistant")
        memoria_col.update_one(
            {"cliente_id": cliente_id},
            {"$set": {"ultima_pregunta": texto}},
            upsert=True
        )

        sends.insert_one({
            "jid": cliente_id,
            "message": {"text": respuesta},
            "sent": False
        })

        return {"respuesta": respuesta}

    except Exception as e:
        print(f"Error in webhook: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

# ------------------------------
# Respuesta de asesor (botones)
# ------------------------------
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
            mensaje_cliente = f"Hola, {estado.get('nombre','')}. Un asesor te contactará pronto."
        elif respuesta == "no":
            mensaje_cliente = f"Hola, {estado.get('nombre','')}. Intentaremos asignar otro asesor."
            # Podrías reasignar a otro asesor aquí si quieres
            asignar_asesor(cliente_id)
        else:
            mensaje_cliente = f"Hola, {estado.get('nombre','')}. Tu solicitud está pendiente."

        sends.insert_one({
            "jid": cliente_id,
            "message": {"text": mensaje_cliente},
            "sent": False
        })

        return {"status": "success"}
    except Exception as e:
        print(f"Error in advisor_response: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

# ------------------------------
# Obtener asesores activos
# ------------------------------
@app.get("/get_asesores")
def get_asesores():
    try:
        asesores = list(asesores_col.find({"activo": True}, {"telefono": 1, "_id": 0}))
        return [a["telefono"] for a in asesores if "telefono" in a]
    except Exception as e:
        print(f"Error in /get_asesores: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5000)
