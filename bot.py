from fastapi import FastAPI, Request
import requests, os, re, time

app = FastAPI()

# === CONFIG ===
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")  # pat-xxxxxx
DEFAULT_OWNER_EMAIL = os.getenv("DEFAULT_OWNER_EMAIL", "ray@two.travel")
HUBSPOT_BASE = "https://api.hubapi.com"

# ==== DUEÑOS POR SERVICIO Y CIUDAD (normalizados) ====
SERVICE_OWNER_MAP = {
    # Solo definimos los servicios que tienen dueño fijo
    "weddings & events": "sofia@two.travel",  # Bodas
    # Si quieres, puedes añadir otros servicios aquí
    # "villas & homes": "alguien@two.travel",
    # "boats & yachts": "alguien@two.travel",
    # "concierge": "alguien@two.travel",
}

CITY_OWNER_MAP = {
    "medellin":  "ross@two.travel",
    "cartagena": "sofia@two.travel",
    "mexico":    "ray@two.travel",   # México → Ray
}

DEFAULT_OWNER_EMAIL = os.getenv("DEFAULT_OWNER_EMAIL", "ray@two.travel")  # fallback


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# === HELPERS HubSpot ===
def _hdrs(json=True):
    h = {"Authorization": f"Bearer {HUBSPOT_TOKEN}"}
    if json: h["Content-Type"] = "application/json"
    return h
def resolve_owner_email(service_type: str | None, city: str | None, fallback: str | None = None) -> str:
    """
    Prioridad:
      1) Si el servicio tiene dueño fijo (p.ej. Weddings) → ese.
      2) Si no, usar dueño por ciudad (Medellín, Cartagena, México…).
      3) Si nada matchea, usar DEFAULT_OWNER_EMAIL.
    Todo se compara en minúsculas y con espacios recortados.
    """
    st = (service_type or "").strip().lower()
    ct = (city or "").strip().lower()

    # 1) Servicio manda si está definido
    if st in SERVICE_OWNER_MAP and SERVICE_OWNER_MAP[st]:
        return SERVICE_OWNER_MAP[st]

    # 2) Ciudad
    if ct in CITY_OWNER_MAP and CITY_OWNER_MAP[ct]:
        return CITY_OWNER_MAP[ct]

    # 3) Fallbacks
    if fallback:
        return fallback
    return DEFAULT_OWNER_EMAIL
    
def get_owner_id(email: str):
    """Busca el ID del dueño en HubSpot por email"""
    url = f"{HUBSPOT_BASE}/crm/v3/owners"
    r = requests.get(url, headers=_hdrs(json=False))
    r.raise_for_status()
    for o in r.json().get("results", []):
        if o.get("email") == email:
            return o.get("id")
    return None

def find_contact_by_email(email: str):
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {"filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}]}
    r = requests.post(url, headers=_hdrs(), json=payload); r.raise_for_status()
    res = r.json().get("results", [])
    return res[0]["id"] if res else None

def find_contact_by_phone(phone: str):
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {"filterGroups":[{"filters":[{"propertyName":"phone","operator":"EQ","value":phone}]}]}
    r = requests.post(url, headers=_hdrs(), json=payload); r.raise_for_status()
    res = r.json().get("results", [])
    return res[0]["id"] if res else None

def upsert_contact(name: str, email: str, phone: str, language="ES"):
    """Crea/actualiza contacto por email o phone; retorna contact_id."""
    cid = None
    if email and EMAIL_RE.match(email):  # buscar por email primero
        cid = find_contact_by_email(email)
    if not cid and phone:
        cid = find_contact_by_phone(phone)

    props = {
        "email": email,
        "phone": phone,
        "language": language,
    }
    if name:
        parts = name.strip().split()
        props["firstname"] = parts[0]
        props["lastname"] = " ".join(parts[1:]) if len(parts) > 1 else ""

    if cid:
        r = requests.patch(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{cid}", headers=_hdrs(), json={"properties": props})
        r.raise_for_status()
        return cid

    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts", headers=_hdrs(), json={"properties": props})
    r.raise_for_status()
    return r.json()["id"]


# === NUEVO: helpers para resolver IDs de pipeline y stage por nombre ===
def get_pipeline_and_stage_ids(pipeline_label: str, stage_label: str):
    """
    Devuelve (pipelineId, stageId) buscando por etiquetas (case-insensitive).
    Si no encuentra, retorna (None, None).
    """
    url = f"{HUBSPOT_BASE}/crm/v3/pipelines/deals"
    r = requests.get(url, headers=_hdrs(json=False))
    r.raise_for_status()
    data = r.json().get("results", [])

    pl = pipeline_label.strip().lower()
    st = stage_label.strip().lower()

    for p in data:
        p_label = (p.get("label") or "").strip().lower()
        if p_label == pl:
            pipeline_id = p.get("id")
            for s in p.get("stages", []):
                s_label = (s.get("label") or "").strip().lower()
                if s_label == st:
                    return pipeline_id, s.get("id")
            # pipeline ok, stage no encontrado
            return pipeline_id, None

    # pipeline no encontrado
    return None, None


def create_or_update_deal(contact_id: str, service_type: str, city: str, start: str, end: str,
                          pax: str, language: str, owner_email: str | None = None, deal_id: str | None = None):

    owner_email_final = resolve_owner_email(service_type, city, fallback=owner_email)
    owner_id = get_owner_id(owner_email_final) or get_owner_id(DEFAULT_OWNER_EMAIL)

    # ← NUEVO: resolver IDs internos por etiqueta
    # Cambia estos strings si tu pipeline o stage se llaman distinto en UI
    PIPELINE_LABEL = "B2C Sales"
    STAGE_LABEL = "Requirements Received"
    pipeline_id, stage_id = get_pipeline_and_stage_ids(PIPELINE_LABEL, STAGE_LABEL)

    # Fallbacks si no se encuentran (evita romper la creación)
    if not pipeline_id:
        pipeline_id = "default"            # o el id que ya usabas
    if not stage_id:
        stage_id = "appointmentscheduled"  # algún stage existente como respaldo

    props = {
        "dealname": f"{service_type} – {city or 'Sin ciudad'}",
        "pipeline": pipeline_id,      # ← usar ID interno del pipeline
        "dealstage": stage_id,        # ← usar ID interno del stage
        "service_type": service_type,
        "city": city,
        "start_date": start,
        "end_date": end,
        "pax": pax,
        "language": language,
        "hubspot_owner_id": owner_id,
    }

    headers = _hdrs()
    if deal_id:
        r = requests.patch(f"{HUBSPOT_BASE}/crm/v3/objects/deals/{deal_id}", headers=headers, json={"properties": props})
        r.raise_for_status()
        return deal_id

    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/deals", headers=headers, json={"properties": props})
    r.raise_for_status()
    new_deal_id = r.json()["id"]

    # asociar contacto
    assoc_url = f"{HUBSPOT_BASE}/crm/v4/objects/deals/{new_deal_id}/associations/contacts/{contact_id}"
    assoc_payload = {"types":[{"associationCategory":"HUBSPOT_DEFINED","associationTypeId":3}]}
    ra = requests.put(assoc_url, headers=headers, json=assoc_payload)
    ra.raise_for_status()

    return new_deal_id


# === ESTADO EN MEMORIA (demo; en prod usa Redis) ===
user_sessions = {}

# === FLUJO ===
@app.post("/whatsapp/webhook")
async def whatsapp_webhook(req: Request):
    body = await req.json()
    # normaliza payload de prueba: {"from":"+57300...", "text":"hola"}
    msg   = (body.get("text") or "").strip()
    phone = body.get("from") or "desconocido"

    s = user_sessions.get(phone, {"step": "lang", "lang": "ES"})
    step = s["step"]

    # 1) Idioma (simple: ES por defecto; si escribe "EN", cambiamos)
    if step == "lang":
        if msg.lower().startswith("en"):
            s["lang"] = "EN"
            reply = "Hi! What's your full name?"
        else:
            s["lang"] = "ES"
            reply = "¡Hola! Soy tu concierge virtual 🛎️✨. ¿Cuál es tu nombre completo?"
        s["step"] = "name"
        user_sessions[phone] = s
        return {"reply": reply}

    # 2) Nombre
    if step == "name":
        if len(msg.split()) < 2:
            reply = "¿Me confirmas nombre y apellido?" if s["lang"]=="ES" else "Could you share name and last name?"
            return {"reply": reply}
        s["name"] = msg
        s["step"] = "email"
        user_sessions[phone] = s
        reply = "Perfecto. Ahora dime tu correo electrónico 📧" if s["lang"]=="ES" else "Great. What's your email?"
        return {"reply": reply}

    # 3) Email (validación)
    if step == "email":
        if not EMAIL_RE.match(msg):
            reply = "Ese correo no parece válido, ¿puedes revisarlo?" if s["lang"]=="ES" else "That email looks invalid, mind checking it?"
            return {"reply": reply}
        s["email"] = msg.lower()
        # upsert contacto parcial
        s["contact_id"] = upsert_contact(name=s.get("name"), email=s["email"], phone=phone, language=s["lang"])
        s["step"] = "service"
        user_sessions[phone] = s
        if s["lang"]=="ES":
            menu = "Genial. ¿Qué servicio necesitas hoy?\n1) Villas 🏠\n2) Botes 🚤\n3) Bodas 💍\n4) Concierge ✨\n5) Hablar con ventas 👤"
        else:
            menu = "Great. What do you need today?\n1) Villas 🏠\n2) Boats 🚤\n3) Weddings 💍\n4) Concierge ✨\n5) Talk to sales 👤"
        return {"reply": menu}

    # 4) Menú → fija service_type y crea deal básico con owner según regla
    if step == "service":
        mapping = {"1":"Villas & Homes","2":"Boats & Yachts","3":"Weddings & Events","4":"Concierge","5":"Sales"}
        choice = mapping.get(msg.strip())
        if not choice:
            return {"reply": "Elige 1–5, por favor." if s["lang"]=="ES" else "Choose 1–5, please."}
        s["service"] = choice
        # Aún no creamos el deal: pedimos ciudad y fechas primero
        s["step"] = "city"
        user_sessions[phone] = s
        return {"reply": "¿En qué ciudad necesitas el servicio?" if s["lang"]=="ES" else "Which city?"}

    # 5) Ciudad
    if step == "city":
        s["city"] = msg
        s["step"] = "dates"
        user_sessions[phone] = s
        return {"reply": "¿Fechas? (YYYY-MM-DD a YYYY-MM-DD)" if s["lang"]=="ES" else "Dates? (YYYY-MM-DD to YYYY-MM-DD)"}

    # 6) Fechas
    if step == "dates":
        if "a" in msg:
            a, b = msg.split("a", 1)
            s["start"] = a.strip()
            s["end"] = b.strip()
        elif "to" in msg:
            a, b = msg.split("to", 1)
            s["start"] = a.strip()
            s["end"] = b.strip()
        else:
            s["start"] = msg.strip()
            s["end"] = msg.strip()
        s["step"] = "pax"
        user_sessions[phone] = s
        return {"reply": "¿Para cuántas personas?" if s["lang"]=="ES" else "How many guests?"}

    # 7) Pax → ahora sí creamos contacto (final) + deal + owner + asociación
    if step == "pax":
        s["pax"] = msg.strip()
        # upsert contacto con todo
        cid = upsert_contact(name=s.get("name"), email=s.get("email"), phone=phone, language=s.get("lang","ES"))
        # crear deal con asociación al contacto y owner por regla
        deal_id = create_or_update_deal(
            contact_id=cid,
            service_type=s.get("service"),
            city=s.get("city"),
            start=s.get("start"),
            end=s.get("end"),
            pax=s.get("pax"),
            language=s.get("lang","ES"),
            owner_email=DEFAULT_OWNER_EMAIL,
        )
        # limpiar sesión
        user_sessions.pop(phone, None)
        reply = ("✅ Gracias. Te conecto con el asesor para confirmar disponibilidad y cotización final."
                 if s.get("lang")=="ES"
                 else "✅ Thanks. Connecting you with sales to confirm availability and finalize.")
        return {"reply": reply}

    # fallback
    return {"reply": "No entendí, ¿puedes repetir?" if s.get("lang")=="ES" else "I didn't get that, could you repeat?"}
