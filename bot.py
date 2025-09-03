# bot.py
import os, re, requests, time
from fastapi import FastAPI, Request

HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")        # pat-xxxxx
BASE_HS = "https://api.hubapi.com"
HDRS = {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}

# ======== memoria de sesión (usa Redis en prod) ========
SESS = {}  # { phone: {"step": "...", "contact_id": "...", "deal_id": "...", "lang": "ES"} }

app = FastAPI()

# ---------- utilidades HubSpot ----------
def hs_find_owner_id(owner_email:str):
    r = requests.get(f"{BASE_HS}/crm/v3/owners", headers=HDRS, params={"email": owner_email, "archived":"false"})
    r.raise_for_status()
    res = r.json().get("results", [])
    return res[0]["id"] if res else None

def hs_upsert_contact(phone:str, name=None, email=None, lang=None):
    # Busca por teléfono o email
    cid = None
    if email:
        s = requests.post(f"{BASE_HS}/crm/v3/objects/contacts/search", headers=HDRS,
                          json={"filterGroups":[{"filters":[{"propertyName":"email","operator":"EQ","value":email}]}]})
        s.raise_for_status()
        r = s.json().get("results", [])
        if r: cid = r[0]["id"]
    if not cid:
        s = requests.post(f"{BASE_HS}/crm/v3/objects/contacts/search", headers=HDRS,
                          json={"filterGroups":[{"filters":[{"propertyName":"phone","operator":"EQ","value":phone}]}]})
        s.raise_for_status(); r=s.json().get("results", [])
        if r: cid = r[0]["id"]

    props = {}
    if email: props["email"]=email
    if phone: props["phone"]=phone
    if name:
        parts = name.strip().split()
        props["firstname"]=parts[0]
        props["lastname"]=" ".join(parts[1:]) if len(parts)>1 else ""
    if lang: props["language"]=lang

    if cid:
        requests.patch(f"{BASE_HS}/crm/v3/objects/contacts/{cid}", headers=HDRS, json={"properties":props}).raise_for_status()
        return cid
    c = requests.post(f"{BASE_HS}/crm/v3/objects/contacts", headers=HDRS, json={"properties":props})
    c.raise_for_status()
    return c.json()["id"]

def hs_create_or_update_deal(contact_id:str, owner_email:str, service_type:str,
                             city=None, start=None, end=None, pax=None, deal_id=None):
    owner_id = hs_find_owner_id(owner_email)
    props = {
        "dealname": f"{service_type} – {int(time.time())}",
        "pipeline": "default",
        "dealstage": "appointmentscheduled",
        "hubspot_owner_id": owner_id,
        "service_type": service_type,
    }
    if city: props["city"]=city
    if start: props["start_date"]=start
    if end: props["end_date"]=end
    if pax: props["pax"]=str(pax)

    if deal_id:
        requests.patch(f"{BASE_HS}/crm/v3/objects/deals/{deal_id}", headers=HDRS, json={"properties":props}).raise_for_status()
        return deal_id

    d = requests.post(f"{BASE_HS}/crm/v3/objects/deals", headers=HDRS, json={"properties":props})
    d.raise_for_status()
    deal_id = d.json()["id"]
    # asociar contacto
    requests.put(f"{BASE_HS}/crm/v3/objects/deals/{deal_id}/associations/contacts/{contact_id}/deal_to_contact",
                 headers=HDRS).raise_for_status()
    return deal_id

# ---------- WhatsApp (simulado) ----------
def send_wpp(to:str, text:str):
    # Aquí luego llamas a la API real de WhatsApp (Meta/360/Twilio)
    print(f"[WPP → {to}] {text}")

EMAIL_REGEX = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"

def next_step(phone, text):
    s = SESS.setdefault(phone, {"step":"lang", "lang":None})

    # Paso: idioma
    if s["step"]=="lang":
        if text.strip().lower().startswith("en"):
            s["lang"]="EN"; send_wpp(phone, "Hi! What's your full name?")
        else:
            s["lang"]="ES"; send_wpp(phone, "¡Hola! ¿Tu nombre completo?")
        s["step"]="name"
        return

    # Nombre
    if s["step"]=="name":
        if len(text.strip().split())<2:
            send_wpp(phone, "¿Me confirmas nombre y apellido?") if s["lang"]=="ES" else send_wpp(phone,"Could you share name and last name?")
            return
        s["name"]=text.strip()
        # contacto parcial
        cid = hs_upsert_contact(phone=phone, name=s["name"])
        s["contact_id"]=cid
        send_wpp(phone, "¿Cuál es tu correo?") if s["lang"]=="ES" else send_wpp(phone,"What's your email?")
        s["step"]="email"
        return

    # Email
    if s["step"]=="email":
        if not re.match(EMAIL_REGEX, text.strip(), re.I):
            send_wpp(phone, "Ese correo no parece válido, ¿puedes revisarlo?") if s["lang"]=="ES" else send_wpp(phone,"That email looks invalid, mind checking it?")
            return
        s["email"]=text.strip().lower()
        # update contacto
        hs_upsert_contact(phone=phone, name=s.get("name"), email=s["email"], lang=s["lang"])
        # menú
        menu_es = "¿Qué necesitas hoy?\n1) Villas\n2) Boats\n3) Weddings\n4) Concierge\n5) Hablar con ventas"
        menu_en = "What do you need today?\n1) Villas\n2) Boats\n3) Weddings\n4) Concierge\n5) Talk to sales"
        send_wpp(phone, menu_es if s["lang"]=="ES" else menu_en)
        s["step"]="menu"
        return

    # Menú → crea Deal y asigna dueño
    if s["step"]=="menu":
        opts = {"1":"Villas & Homes","2":"Boats & Yachts","3":"Weddings & Events","4":"Concierge","5":"Sales"}
        choice = opts.get(text.strip())
        if not choice:
            send_wpp(phone, "Elige 1–5, por favor.") if s["lang"]=="ES" else send_wpp(phone,"Choose 1–5, please.")
            return
        s["service_type"]=choice
        # crea deal ya mismo (dueño: cambia por tu regla/routeo)
        owner_email = os.getenv("DEFAULT_OWNER_EMAIL","ventas@tutravel.com")
        s["deal_id"]=hs_create_or_update_deal(
            contact_id=s["contact_id"],
            owner_email=owner_email,
            service_type=s["service_type"]
        )
        # sigue preguntas por servicio (ejemplo boats)
        if choice=="Boats & Yachts":
            send_wpp(phone, "Ciudad/puerto de salida (ej: Cartagena)?"); s["step"]="boats_city"; return
        # … agrega ramas para Villas/Weddings/Concierge …
        send_wpp(phone, "Cuéntame la ciudad"); s["step"]="generic_city"; return

    # Ejemplo rama Boats
    if s["step"]=="boats_city":
        s["city"]=text.strip()
        hs_create_or_update_deal(s["contact_id"], os.getenv("DEFAULT_OWNER_EMAIL","ventas@tutravel.com"),
                                 s["service_type"], city=s["city"], deal_id=s["deal_id"])
        send_wpp(phone, "Fecha del paseo (YYYY-MM-DD)?"); s["step"]="boats_date"; return

    if s["step"]=="boats_date":
        s["start_date"]=text.strip()
        hs_create_or_update_deal(s["contact_id"], os.getenv("DEFAULT_OWNER_EMAIL","ventas@tutravel.com"),
                                 s["service_type"], start=s["start_date"], deal_id=s["deal_id"])
        send_wpp(phone, "¿Número de pasajeros?"); s["step"]="boats_pax"; return

    if s["step"]=="boats_pax":
        try: s["pax"]=int(text.strip())
        except: send_wpp(phone,"Número, por favor."); return
        hs_create_or_update_deal(s["contact_id"], os.getenv("DEFAULT_OWNER_EMAIL","ventas@tutravel.com"),
                                 s["service_type"], pax=s["pax"], deal_id=s["deal_id"])
        send_wpp(phone, "Listo 🙌 ¿Te conecto con ventas para confirmar disponibilidad y cotización final?")
        s["step"]="handoff"; return

    # Handoff
    if s["step"]=="handoff":
        send_wpp(phone, "Te conecto con el equipo. Gracias!") if s["lang"]=="ES" else send_wpp(phone,"Connecting you with sales, thanks!")
        # aquí opcional: notificación interna / Slack / tarea en HS
        return

@app.post("/whatsapp/webhook")
async def wpp_webhook(req: Request):
    body = await req.json()
    # normaliza entrada (simulada): {"from":"+573001112233","text":"hola"}
    phone = body.get("from")
    text  = body.get("text","")
    if not phone or not text:
        return {"ok": False}
    next_step(phone, text)
    return {"ok": True}
