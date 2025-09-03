from fastapi import FastAPI, Request
import requests, os

app = FastAPI()

# === CONFIG ===
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")
DEFAULT_OWNER_EMAIL = os.getenv("DEFAULT_OWNER_EMAIL", "ray@two.travel")

HUBSPOT_BASE = "https://api.hubapi.com"


# === HELPERS ===
def get_owner_id(email: str):
    """Busca el ID del dueño en HubSpot por email"""
    url = f"{HUBSPOT_BASE}/crm/v3/owners/"
    headers = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        for o in r.json().get("results", []):
            if o.get("email") == email:
                return o.get("id")
    return None


def create_contact(name, email, phone):
    """Crea contacto en HubSpot"""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "properties": {
            "firstname": name.split()[0],
            "lastname": " ".join(name.split()[1:]) if len(name.split()) > 1 else "",
            "email": email,
            "phone": phone,
        }
    }
    r = requests.post(url, headers=headers, json=payload)
    return r.json()


def create_deal(service_type, city, start, end, pax, language, owner_email):
    """Crea deal en HubSpot"""
    owner_id = get_owner_id(owner_email) or get_owner_id(DEFAULT_OWNER_EMAIL)
    url = f"{HUBSPOT_BASE}/crm/v3/objects/deals"
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "properties": {
            "dealname": f"{service_type} - {city}",
            "pipeline": "default",
            "dealstage": "appointmentscheduled",  # <- cambia según tu pipeline
            "service_type": service_type,
            "city": city,
            "start_date": start,
            "end_date": end,
            "pax": pax,
            "language": language,
            "hubspot_owner_id": owner_id,
        }
    }
    r = requests.post(url, headers=headers, json=payload)
    return r.json()


# === ESTADO EN MEMORIA (simulación de flujo) ===
user_sessions = {}


# === FLUJO DEL BOT ===
@app.post("/whatsapp/webhook")
async def whatsapp_webhook(req: Request):
    body = await req.json()
    msg = body.get("text", "").strip()
    phone = body.get("from", "desconocido")

    state = user_sessions.get(phone, {"step": "lang"})
    step = state["step"]

    if step == "lang":
        user_sessions[phone] = {"step": "name"}
        return {"reply": "¡Hola! Soy tu concierge virtual 🛎️✨. ¿Cuál es tu nombre completo?"}

    elif step == "name":
        state["name"] = msg
        state["step"] = "email"
        return {"reply": "Perfecto. Ahora dime tu correo electrónico 📧"}

    elif step == "email":
        state["email"] = msg
        state["step"] = "service"
        return {
            "reply": "Genial. ¿Qué servicio necesitas hoy?\n1️⃣ Villas 🏠\n2️⃣ Botes 🚤\n3️⃣ Bodas 💍\n4️⃣ Concierge ✨"
        }

    elif step == "service":
        choice = msg.lower()
        mapping = {
            "1": "Villas & Homes",
            "2": "Boats & Yachts",
            "3": "Weddings & Events",
            "4": "Concierge",
        }
        state["service"] = mapping.get(choice, "Otro")
        state["step"] = "city"
        return {"reply": "¿En qué ciudad necesitas el servicio?"}

    elif step == "city":
        state["city"] = msg
        state["step"] = "dates"
        return {"reply": "¿Cuáles son las fechas? (ejemplo 2025-09-10 a 2025-09-15)"}

    elif step == "dates":
        if "a" in msg:
            fechas = msg.split("a")
            state["start"] = fechas[0].strip()
            state["end"] = fechas[1].strip()
        else:
            state["start"] = msg.strip()
            state["end"] = msg.strip()
        state["step"] = "pax"
        return {"reply": "¿Para cuántas personas es el servicio?"}

    elif step == "pax":
        state["pax"] = msg
        state["step"] = "done"

        # === CREAR CONTACTO Y DEAL EN HUBSPOT ===
        create_contact(state["name"], state["email"], phone)
        create_deal(
            service_type=state["service"],
            city=state["city"],
            start=state["start"],
            end=state["end"],
            pax=state["pax"],
            language="ES",
            owner_email="rey@twotravel.com",  # 👈 Ajusta aquí la regla
        )

        user_sessions.pop(phone, None)  # limpiar sesión
        return {"reply": "✅ Gracias. Te conecto ahora con un asesor para confirmar disponibilidad."}

    return {"reply": "No entendí, ¿puedes repetir?"}
