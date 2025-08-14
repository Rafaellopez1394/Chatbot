import random
from flask import Flask, request, jsonify
from pymongo import MongoClient
import ollama

app = Flask(__name__)

# --- Configuración MongoDB ---
client = MongoClient("mongodb://localhost:27017/")
db = client["chatbotdb"]
historial_col = db["historial"]
estado_col = db["estado_conversacion"]

# --- Asistentes ---
asistentes = ["Marcela", "Carlos", "Sofía", "Javier", "Luisa"]

def asignar_asistente():
    return random.choice(asistentes)

# --- Frases ---
saludos_iniciales_naturales = [
    "¡Hola! Muchas gracias por comunicarte, te atiende {asistente}, asistente administrativo. ¿Con quién tengo el gusto?",
    "¡Hola! Me llamo {asistente} y estoy aquí para ayudarte. ¿Me pudieras compartir tu nombre, por favor?",
    "¡Hola que tal! Soy {asistente} y estoy aquí para ayudarte. ¿Pudieras compartirme tu nombre, por favor?",
    "¡Hola! Gracias por comunicarte, soy {asistente}. ¿Con quién tengo el gusto?",
]

disculpas_naturales = [
    "Estoy aquí para ayudarte y me gustaría saber tu nombre, ¿me lo puedes compartir, por favor?",
    "Me gustaría dirigirme a ti por tu nombre para darle más formalidad, ¿me lo puedes compartir, por favor?",
    "Quiero asegurarme de dirigirme a ti correctamente, ¿me dices tu nombre, por favor?",
    "Para ofrecerte un mejor servicio, ¿me podrías compartir tu nombre, por favor?",
    "Si no te importa, ¿me dices tu nombre para que pueda atenderte mejor?",
]

frases_pedir_area = [
    "¿Podrías decirme en qué área te gustaría que te atienda? Tenemos Ventas, Servicios y Refacciones.",
    "Para ayudarte mejor, ¿me dices si tu interés es en Ventas, Servicios o Refacciones?",
    "¿En qué área necesitas asistencia? Tenemos Ventas, Servicios y Refacciones.",
    "¿En qué área te gustaría recibir apoyo? Tenemos Ventas, Servicios y Refacciones.",
    "¿En qué área podemos ayudarte hoy? (Ventas, Servicios, Refacciones)",
    "¿En cuál de estas áreas quieres que te apoye? Ventas, Servicios o Refacciones.",
    "¿Me podrías indicar si buscas ayuda en Ventas, Servicios o Refacciones?",
]

frases_no_area = [
    "No estoy seguro de haber entendido el área. ¿Podrías repetirlo, por favor?",
    "Disculpa, ¿podrías decirme si es Ventas, Servicios o Refacciones?",
    "¿Podrías aclararme si es Ventas, Servicios o Refacciones a lo que te refieres?",
]

frases_pedir_contacto = [
    "Para que un asesor se comunique contigo, ¿me podrías compartir tu teléfono o correo electrónico?",
    "¿Me puedes proporcionar tu teléfono o correo para que un asesor te contacte?",
    "Para brindarte un mejor servicio, ¿me compartes tu teléfono o correo, por favor?",
]

# --- Funciones MongoDB ---
def guardar_mensaje(cliente_id, rol, contenido):
    historial_col.insert_one({
        "cliente_id": cliente_id,
        "rol": rol,
        "contenido": contenido
    })

def obtener_historial(cliente_id):
    return list(historial_col.find({"cliente_id": cliente_id}))

def actualizar_estado(cliente_id, nuevo_estado):
    estado_col.update_one(
        {"cliente_id": cliente_id},
        {"$set": nuevo_estado},
        upsert=True
    )

def obtener_estado(cliente_id):
    return estado_col.find_one({"cliente_id": cliente_id}) or {}

# --- Validaciones ---
def es_nombre_valido(nombre):
    nombre = nombre.strip().lower()
    palabras_no_validas = [
        "no", "nada", "salir", "precio", "me interesa",
        "informacion", "más", "mas", "hola", ""
    ]
    if len(nombre) < 3 or nombre in palabras_no_validas:
        return False
    if not any(c.isalpha() for c in nombre):
        return False
    return True

def es_contacto_valido(contacto):
    contacto = contacto.strip()
    if len(contacto) < 5:
        return False
    return True

# --- Lógica principal del chatbot ---
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    user_id = data.get("user_id")  # Identificador único del usuario/plataforma
    texto_usuario = data.get("message")

    if not user_id or not texto_usuario:
        return jsonify({"error": "Faltan datos"}), 400

    estado = obtener_estado(user_id)

    # 1. Si no hay nombre registrado, pedimos nombre
    if "nombre" not in estado:
        if es_nombre_valido(texto_usuario):
            actualizar_estado(user_id, {"nombre": texto_usuario, "intentos_nombre": 0})
            guardar_mensaje(user_id, "user", texto_usuario)
            asistente = asignar_asistente()
            respuesta = f"¡Perfecto, {texto_usuario}! Soy {asistente}, encantado de atenderte. ¿En qué área podemos ayudarte hoy? (Ventas, Servicios, Refacciones)"
        else:
            intentos = estado.get("intentos_nombre", 0) + 1
            actualizar_estado(user_id, {"intentos_nombre": intentos})
            respuesta = random.choice(disculpas_naturales)
        guardar_mensaje(user_id, "assistant", respuesta)
        return jsonify({"reply": respuesta})

    # 2. Si nombre existe pero no hay área, pedimos área
    if "area" not in estado:
        area = texto_usuario.lower()
        if any(k in area for k in ["venta", "servicio", "refacción", "refacciones"]):
            if "venta" in area:
                area_canal = "Ventas"
            elif "servicio" in area:
                area_canal = "Servicios"
            else:
                area_canal = "Refacciones"
            actualizar_estado(user_id, {"area": area_canal})
            guardar_mensaje(user_id, "user", texto_usuario)
            respuesta = f"Entendido, te canalizo al área de {area_canal}. ¿Me puedes proporcionar tu teléfono o correo para que un asesor te contacte?"
            guardar_mensaje(user_id, "assistant", respuesta)
            return jsonify({"reply": respuesta})
        else:
            guardar_mensaje(user_id, "user", texto_usuario)
            respuesta = random.choice(frases_no_area)
            guardar_mensaje(user_id, "assistant", respuesta)
            return jsonify({"reply": respuesta})

    # 3. Si área existe pero no hay contacto, pedimos contacto
    if "contacto" not in estado:
        contacto = texto_usuario.strip()
        if es_contacto_valido(contacto):
            actualizar_estado(user_id, {"contacto": contacto})
            guardar_mensaje(user_id, "user", texto_usuario)
            respuesta = f"Gracias, {estado['nombre']}. Un asesor del área de {estado['area']} se pondrá en contacto contigo pronto."
            guardar_mensaje(user_id, "assistant", respuesta)
            return jsonify({"reply": respuesta})
        else:
            guardar_mensaje(user_id, "user", texto_usuario)
            respuesta = "Disculpa, el dato que ingresaste no parece válido. ¿Me lo puedes proporcionar otra vez?"
            guardar_mensaje(user_id, "assistant", respuesta)
            return jsonify({"reply": respuesta})

    # 4. Si ya tenemos nombre, área y contacto, manejamos preguntas libres con Ollama
    guardar_mensaje(user_id, "user", texto_usuario)
    historial = obtener_historial(user_id)
    mensajes = [{"role": h["rol"], "content": h["contenido"]} for h in historial]
    respuesta_ollama = ollama.chat(model="llama3", messages=mensajes)
    texto_respuesta = respuesta_ollama['message']['content']
    guardar_mensaje(user_id, "assistant", texto_respuesta)

    return jsonify({"reply": texto_respuesta})

if __name__ == "__main__":
    print("Chatbot API corriendo en http://localhost:5000")
    app.run(host="0.0.0.0", port=5000)
