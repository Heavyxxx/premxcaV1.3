import csv
import io
import json
import os
import re
import uuid
from datetime import datetime

from bson.objectid import ObjectId
from flask import Flask, Response, jsonify, request, send_from_directory
from pymongo import MongoClient, ReturnDocument

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "oaxaca-travel-web")
BACKUP_FILE = os.path.join(BASE_DIR, "data", "leads_backup.json")

app = Flask(__name__, static_folder=None)
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = os.environ.get("MONGO_DB", "crm_premska")
MONGO_COLLECTION_NAME = "leads"
use_mongo = False
collection = None

try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
    client.admin.command("ping")
    collection = client[MONGO_DB_NAME][MONGO_COLLECTION_NAME]
    use_mongo = True
    print(f"[CRM Premska] MongoDB conectado: {MONGO_DB_NAME}.{MONGO_COLLECTION_NAME}")
except Exception as error:
    print(f"[CRM Premska] MongoDB no disponible ({error}). Usando respaldo local JSON en {BACKUP_FILE}")

STAGES = [
    "Cotización Web",
    "Promo 15% Enviada",
    "En Negociación",
    "Apartado",
    "Boleto Confirmado",
]
STAGE_PROBABILITIES = {
    "Cotización Web": 15,
    "Promo 15% Enviada": 30,
    "En Negociación": 55,
    "Apartado": 80,
    "Boleto Confirmado": 100,
}
DEPARTURE_MONTHS = ("Febrero", "Abril", "Junio", "Septiembre", "Noviembre")
DEPARTURES = tuple(f"Salida {index} • {month}" for index, month in enumerate(DEPARTURE_MONTHS, start=1))
DEPARTURE_BY_MONTH = {month: DEPARTURES[index] for index, month in enumerate(DEPARTURE_MONTHS)}
TASK_TYPES = {"Llamada", "WhatsApp", "Enviar itinerario", "Seguimiento promo 15%"}
TICKET_PRICE = 120000
SECOND_TICKET_DISCOUNT = 0.15


def load_json_leads():
    if not os.path.exists(BACKUP_FILE):
        return []
    with open(BACKUP_FILE, "r", encoding="utf-8") as file:
        leads = json.load(file)
    if not isinstance(leads, list):
        raise ValueError("El respaldo local de leads debe contener una lista.")
    normalized = [normalize_lead_data(lead) for lead in leads]
    if normalized != leads:
        save_json_leads(normalized)
    return normalized


def save_json_leads(leads):
    os.makedirs(os.path.dirname(BACKUP_FILE), exist_ok=True)
    temp_file = f"{BACKUP_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(leads, file, ensure_ascii=False, indent=2)
    os.replace(temp_file, BACKUP_FILE)


def calculate_quote(tickets):
    subtotal = tickets * TICKET_PRICE
    discounted_tickets = tickets // 2
    promo_applied = discounted_tickets > 0
    discount_amount = int(round(discounted_tickets * TICKET_PRICE * SECOND_TICKET_DISCOUNT))
    return {
        "tickets": tickets,
        "unit_price": TICKET_PRICE,
        "subtotal": subtotal,
        "promo_second_ticket_15": promo_applied,
        "discounted_tickets": discounted_tickets,
        "discount_amount": discount_amount,
        "total": subtotal - discount_amount,
    }


def parse_integer(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, float) and not value.is_integer():
        raise ValueError
    if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value.strip()):
        raise ValueError
    return int(value)


def normalize_departure(value, strict=False):
    text = str(value or "").strip()
    match = re.match(r"^Salida\s*([1-5])\b", text, re.IGNORECASE)
    if match:
        return DEPARTURES[int(match.group(1)) - 1]
    month = next((name for name in DEPARTURE_MONTHS if text.casefold() == name.casefold()), None)
    if month:
        return DEPARTURE_BY_MONTH[month]
    if strict:
        raise ValueError("Selecciona una de las cinco salidas disponibles.")
    return DEPARTURES[0]


def normalize_lead_data(doc):
    item = dict(doc)
    item["departure"] = normalize_departure(item.get("departure"))
    item["trip_edition_or_month"] = item["departure"].split(" • ", 1)[1]
    item.setdefault("stage", STAGES[0])
    item.setdefault("probability", STAGE_PROBABILITIES.get(item["stage"], 15))
    item.setdefault("tasks", [])
    item.setdefault("notes_log", [])
    item.setdefault("source", "Web")
    try:
        tickets = parse_integer(item.get("tickets", 1))
        if not 1 <= tickets <= 20:
            tickets = 1
    except (TypeError, ValueError):
        tickets = 1
    item.update(calculate_quote(tickets))
    return item


def serialize_doc(doc):
    item = dict(doc)
    if "_id" in item:
        item["id"] = str(item.pop("_id"))
    return normalize_lead_data(item)


def migrate_legacy_mongodb_records():
    if not use_mongo:
        return
    legacy_collection = client["premxca_crm"][MONGO_COLLECTION_NAME]
    if legacy_collection.count_documents({}):
        existing_ids = {record["_id"] for record in collection.find({}, {"_id": 1})}
        legacy_records = [
            normalize_lead_data(record)
            for record in legacy_collection.find()
            if record["_id"] not in existing_ids
        ]
        if legacy_records:
            collection.insert_many(legacy_records)
            print(
                f"[CRM Premska] Migrados {len(legacy_records)} registros de "
                f"premxca_crm.{MONGO_COLLECTION_NAME}; la colección anterior se conserva."
            )
    normalized_count = 0
    for record in collection.find():
        normalized = normalize_lead_data(record)
        updates = {
            key: value for key, value in normalized.items()
            if key != "_id" and record.get(key) != value
        }
        if updates:
            collection.update_one({"_id": record["_id"]}, {"$set": updates})
            normalized_count += 1
    if normalized_count:
        print(f"[CRM Premska] Normalizados cotización y salida en {normalized_count} registros MongoDB.")


migrate_legacy_mongodb_records()


def validate_quote_payload(data, pricing):
    expected_values = {
        "subtotal": pricing["subtotal"],
        "subtotal_mxn": pricing["subtotal"],
        "discount_amount": pricing["discount_amount"],
        "savings_mxn": pricing["discount_amount"],
        "estimated_total_mxn": pricing["total"],
        "total": pricing["total"],
        "discounted_tickets": pricing["discounted_tickets"],
    }
    for field, expected in expected_values.items():
        if field not in data:
            continue
        try:
            submitted = parse_integer(data[field])
        except (TypeError, ValueError):
            return f"El campo {field} no coincide con la cotización calculada."
        if submitted != expected:
            return f"El campo {field} no coincide con la cotización calculada."
    if "discount_applied_second_ticket" in data:
        try:
            submitted_rate = float(data["discount_applied_second_ticket"])
        except (TypeError, ValueError):
            return "La tasa de descuento no coincide con la cotización calculada."
        if submitted_rate != SECOND_TICKET_DISCOUNT:
            return "La tasa de descuento no coincide con la cotización calculada."
    return None


def read_all_leads():
    if use_mongo:
        return [serialize_doc(item) for item in collection.find().sort("created_at", -1)]
    return [serialize_doc(item) for item in reversed(load_json_leads())]


def find_lead(lead_id):
    if use_mongo:
        try:
            doc = collection.find_one({"_id": ObjectId(lead_id)})
        except Exception:
            return None, "invalid"
        return (serialize_doc(doc), None) if doc else (None, "missing")
    lead = next((item for item in load_json_leads() if str(item.get("id")) == str(lead_id)), None)
    return (serialize_doc(lead), None) if lead else (None, "missing")


def valid_contact(data):
    name = str(data.get("name") or "").strip()
    email = str(data.get("email") or "").strip()
    phone = str(data.get("phone") or "").strip()
    if not name or (not email and not phone):
        return None, (jsonify({"error": "Nombre y al menos un medio de contacto (correo o teléfono) son obligatorios."}), 400)
    return (name, email, phone), None


def error_for_lead(lead_id, error_type):
    if error_type == "invalid":
        return jsonify({"error": "ID inválido"}), 400
    return jsonify({"error": "Lead no encontrado"}), 404


@app.route("/")
def serve_index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/crm")
def serve_crm():
    return send_from_directory(BASE_DIR, "crm.html")


@app.route("/<path:filename>")
def serve_static(filename):
    web_path = os.path.join(WEB_DIR, filename)
    if os.path.isfile(web_path):
        return send_from_directory(WEB_DIR, filename)
    root_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(root_path):
        return send_from_directory(BASE_DIR, filename)
    return jsonify({"error": "Archivo no encontrado"}), 404


@app.route("/api/leads", methods=["GET"])
def get_leads():
    stage_filter = request.args.get("stage", "").strip()
    departure_filter = request.args.get("departure", "").strip()
    search = request.args.get("q", "").strip().lower()
    promo_filter = request.args.get("promo", "").strip().lower()
    pending_only = request.args.get("pending_tasks", "").lower() in {"1", "true", "yes"}
    filtered = []
    for lead in read_all_leads():
        if stage_filter and stage_filter != "ALL" and lead.get("stage") != stage_filter:
            continue
        if departure_filter and departure_filter != "ALL" and lead.get("departure") != departure_filter:
            continue
        if promo_filter in {"yes", "no"} and bool(lead.get("promo_second_ticket_15")) != (promo_filter == "yes"):
            continue
        if pending_only and not any(not task.get("completed", False) for task in lead.get("tasks", [])):
            continue
        if search:
            haystack = " ".join(
                str(lead.get(field, ""))
                for field in ("name", "email", "phone", "departure", "source")
            ).lower()
            if search not in haystack:
                continue
        filtered.append(lead)
    return jsonify({"leads": filtered, "storage": "mongodb" if use_mongo else "json_backup"})


@app.route("/api/leads", methods=["POST"])
def create_lead():
    data = request.get_json(silent=True) or {}
    contact, error = valid_contact(data)
    if error:
        return error
    name, email, phone = contact
    try:
        tickets = parse_integer(data.get("tickets", 1))
    except (ValueError, TypeError):
        return jsonify({"error": "La cantidad de boletos debe ser un número entero."}), 400
    if tickets < 1 or tickets > 20:
        return jsonify({"error": "La cantidad de boletos debe estar entre 1 y 20."}), 400
    try:
        departure = normalize_departure(data.get("departure") or DEPARTURES[0], strict=True)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    trip_edition = str(data.get("trip_edition_or_month") or "").strip()
    if trip_edition:
        expected_departure = DEPARTURE_BY_MONTH.get(trip_edition)
        if not expected_departure or expected_departure != departure:
            return jsonify({"error": "La ventana seleccionada debe coincidir con una de las cinco salidas."}), 400
    stage = str(data.get("stage") or STAGES[0]).strip()
    if stage not in STAGES:
        return jsonify({"error": "Etapa inválida."}), 400
    try:
        probability = parse_integer(data.get("probability", STAGE_PROBABILITIES[stage]))
    except (ValueError, TypeError):
        return jsonify({"error": "La probabilidad debe ser un número entero entre 0 y 100."}), 400
    if not 0 <= probability <= 100:
        return jsonify({"error": "La probabilidad debe estar entre 0 y 100."}), 400

    pricing = calculate_quote(tickets)
    quote_error = validate_quote_payload(data, pricing)
    if quote_error:
        return jsonify({"error": quote_error}), 400
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    notes_log = []
    initial_note = str(data.get("notes") or "").strip()
    if initial_note:
        notes_log.append({"text": initial_note, "created_at": now})
    lead_doc = {
        "name": name,
        "email": email,
        "phone": phone,
        "route": "La Mixteca → Huazolotitlán → Puerto Ángel",
        "departure": departure,
        "trip_edition_or_month": trip_edition or departure.split(" • ", 1)[1],
        "source": str(data.get("source") or "CRM").strip(),
        **pricing,
        "stage": stage,
        "probability": probability,
        "tasks": [],
        "notes_log": notes_log,
        "created_at": now,
        "updated_at": now,
    }
    if use_mongo:
        result = collection.insert_one(dict(lead_doc))
        lead_doc["id"] = str(result.inserted_id)
        print(f"[CRM Premska] Lead insertado en MongoDB ({MONGO_DB_NAME}.{MONGO_COLLECTION_NAME}): {result.inserted_id}")
    else:
        lead_doc["id"] = f"local-{uuid.uuid4().hex}"
        leads = load_json_leads()
        leads.append(lead_doc)
        save_json_leads(leads)
    return jsonify({"message": "Cotización registrada en CRM", "lead": lead_doc}), 201


@app.route("/api/leads/<lead_id>", methods=["GET"])
def get_lead(lead_id):
    lead, error_type = find_lead(lead_id)
    if error_type:
        return error_for_lead(lead_id, error_type)
    return jsonify({"lead": lead})


@app.route("/api/leads/<lead_id>", methods=["PATCH"])
def update_lead(lead_id):
    data = request.get_json(silent=True) or {}
    lead, error_type = find_lead(lead_id)
    if error_type:
        return error_for_lead(lead_id, error_type)
    updates = {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if "stage" in data:
        stage = str(data["stage"] or "").strip()
        if stage not in STAGES:
            return jsonify({"error": "Etapa inválida."}), 400
        updates["stage"] = stage
        if "probability" not in data:
            updates["probability"] = STAGE_PROBABILITIES[stage]
    for field in ("name", "email", "phone", "source"):
        if field in data:
            value = str(data[field] or "").strip()
            if field == "name" and not value:
                return jsonify({"error": "El nombre no puede estar vacío."}), 400
            updates[field] = value
    if "departure" in data:
        try:
            updates["departure"] = normalize_departure(data["departure"], strict=True)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        updates["trip_edition_or_month"] = updates["departure"].split(" • ", 1)[1]
    updated_name = updates.get("name", lead.get("name", ""))
    updated_email = updates.get("email", lead.get("email", ""))
    updated_phone = updates.get("phone", lead.get("phone", ""))
    if not updated_name or (not updated_email and not updated_phone):
        return jsonify({"error": "El prospecto debe conservar nombre y al menos un medio de contacto."}), 400
    if "probability" in data:
        try:
            probability = parse_integer(data["probability"])
        except (ValueError, TypeError):
            return jsonify({"error": "La probabilidad debe ser un número entero entre 0 y 100."}), 400
        if not 0 <= probability <= 100:
            return jsonify({"error": "La probabilidad debe estar entre 0 y 100."}), 400
        updates["probability"] = probability
    if "tickets" in data:
        try:
            tickets = parse_integer(data["tickets"])
        except (ValueError, TypeError):
            return jsonify({"error": "La cantidad de boletos debe ser un número entero."}), 400
        if tickets < 1 or tickets > 20:
            return jsonify({"error": "La cantidad de boletos debe estar entre 1 y 20."}), 400
        updates.update(calculate_quote(tickets))

    new_note = str(data.get("new_note") or "").strip()
    if use_mongo:
        update_ops = {"$set": updates}
        if new_note:
            update_ops["$push"] = {"notes_log": {"text": new_note, "created_at": updates["updated_at"]}}
        updated = collection.find_one_and_update(
            {"_id": ObjectId(lead_id)}, update_ops, return_document=ReturnDocument.AFTER
        )
        if not updated:
            return jsonify({"error": "Lead no encontrado"}), 404
        print(f"[CRM Premska] Lead actualizado en MongoDB ({MONGO_DB_NAME}.{MONGO_COLLECTION_NAME}): {lead_id}")
        return jsonify({"lead": serialize_doc(updated)})
    leads = load_json_leads()
    for item in leads:
        if str(item.get("id")) == str(lead_id):
            item.update(updates)
            if new_note:
                item.setdefault("notes_log", []).append({"text": new_note, "created_at": updates["updated_at"]})
            save_json_leads(leads)
            return jsonify({"lead": serialize_doc(item)})
    return jsonify({"error": "Lead no encontrado"}), 404


@app.route("/api/leads/<lead_id>/tasks", methods=["POST"])
def create_task(lead_id):
    lead, error_type = find_lead(lead_id)
    if error_type:
        return error_for_lead(lead_id, error_type)
    data = request.get_json(silent=True) or {}
    task_type = str(data.get("type") or "").strip()
    if task_type not in TASK_TYPES:
        return jsonify({"error": "Tipo de tarea inválido."}), 400
    due_date = str(data.get("due_date") or "").strip()
    if due_date:
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "La fecha límite debe tener formato AAAA-MM-DD."}), 400
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    task = {"id": uuid.uuid4().hex, "type": task_type, "due_date": due_date, "completed": False, "created_at": now}
    if use_mongo:
        updated = collection.find_one_and_update(
            {"_id": ObjectId(lead_id)},
            {"$push": {"tasks": task}, "$set": {"updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            return jsonify({"error": "Lead no encontrado"}), 404
        print(f"[CRM Premska] Tarea insertada en MongoDB ({MONGO_DB_NAME}.{MONGO_COLLECTION_NAME}): {lead_id}")
        return jsonify({"task": task, "lead": serialize_doc(updated)}), 201
    leads = load_json_leads()
    for item in leads:
        if str(item.get("id")) == str(lead_id):
            item.setdefault("tasks", []).append(task)
            item["updated_at"] = now
            save_json_leads(leads)
            return jsonify({"task": task, "lead": serialize_doc(item)}), 201
    return jsonify({"error": "Lead no encontrado"}), 404


@app.route("/api/leads/<lead_id>/tasks/<task_id>", methods=["PATCH"])
def update_task(lead_id, task_id):
    lead, error_type = find_lead(lead_id)
    if error_type:
        return error_for_lead(lead_id, error_type)
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("completed"), bool):
        return jsonify({"error": "El campo completed debe ser booleano."}), 400
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if use_mongo:
        updated = collection.find_one_and_update(
            {"_id": ObjectId(lead_id), "tasks.id": task_id},
            {"$set": {"tasks.$.completed": data["completed"], "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            return jsonify({"error": "Tarea no encontrada"}), 404
        print(f"[CRM Premska] Tarea actualizada en MongoDB ({MONGO_DB_NAME}.{MONGO_COLLECTION_NAME}): {lead_id}/{task_id}")
        return jsonify({"lead": serialize_doc(updated)})
    leads = load_json_leads()
    for item in leads:
        if str(item.get("id")) != str(lead_id):
            continue
        task = next((entry for entry in item.get("tasks", []) if entry.get("id") == task_id), None)
        if not task:
            return jsonify({"error": "Tarea no encontrada"}), 404
        task["completed"] = data["completed"]
        item["updated_at"] = now
        save_json_leads(leads)
        return jsonify({"lead": serialize_doc(item)})
    return jsonify({"error": "Lead no encontrado"}), 404


@app.route("/api/leads/<lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    if use_mongo:
        try:
            object_id = ObjectId(lead_id)
        except Exception:
            return jsonify({"error": "ID inválido"}), 400
        result = collection.delete_one({"_id": object_id})
        if result.deleted_count == 0:
            return jsonify({"error": "Lead no encontrado"}), 404
        print(f"[CRM Premska] Lead eliminado de MongoDB ({MONGO_DB_NAME}.{MONGO_COLLECTION_NAME}): {lead_id}")
    else:
        leads = load_json_leads()
        remaining = [lead for lead in leads if str(lead.get("id")) != str(lead_id)]
        if len(remaining) == len(leads):
            return jsonify({"error": "Lead no encontrado"}), 404
        save_json_leads(remaining)
    return jsonify({"deleted": True, "id": lead_id})


@app.route("/api/stats", methods=["GET"])
def get_stats():
    leads = read_all_leads()
    open_leads = [lead for lead in leads if lead.get("stage") != "Boleto Confirmado"]
    confirmed_leads = [lead for lead in leads if lead.get("stage") == "Boleto Confirmado"]
    pipeline_amount = sum(int(lead.get("total", 0)) for lead in open_leads)
    weighted_pipeline = sum(
        int(lead.get("total", 0)) * int(lead.get("probability", STAGE_PROBABILITIES.get(lead.get("stage"), 15))) / 100
        for lead in open_leads
    )
    confirmed_amount = sum(int(lead.get("total", 0)) for lead in confirmed_leads)
    confirmed_tickets = sum(int(lead.get("tickets", 1)) for lead in confirmed_leads)
    pending_tasks = sum(
        1 for lead in leads for task in lead.get("tasks", []) if not task.get("completed", False)
    )
    total = len(leads)
    return jsonify({
        "total_leads": total,
        "open_leads": len(open_leads),
        "pipeline_amount": pipeline_amount,
        "weighted_pipeline_amount": round(weighted_pipeline),
        "confirmed_leads": len(confirmed_leads),
        "confirmed_amount": confirmed_amount,
        "confirmed_tickets": confirmed_tickets,
        "annual_departures_cap": 5,
        "pending_tasks": pending_tasks,
        "promo_count": sum(1 for lead in leads if lead.get("promo_second_ticket_15")),
        "conversion_rate": round((len(confirmed_leads) / total) * 100, 1) if total else 0.0,
        "by_stage": {stage: sum(1 for lead in leads if lead.get("stage") == stage) for stage in STAGES},
        "stage_probabilities": STAGE_PROBABILITIES,
        "storage": "mongodb" if use_mongo else "json_backup",
        "database": MONGO_DB_NAME if use_mongo else None,
    })


def safe_csv_cell(value):
    text = str(value if value is not None else "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


@app.route("/api/export", methods=["GET"])
def export_leads():
    export_format = request.args.get("format", "json").strip().lower()
    leads = read_all_leads()
    if export_format == "json":
        payload = json.dumps(leads, ensure_ascii=False, indent=2)
        return Response(
            payload,
            mimetype="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=premxca-leads.json"},
        )
    if export_format != "csv":
        return jsonify({"error": "Formato de exportación inválido. Usa json o csv."}), 400
    output = io.StringIO(newline="")
    columns = [
        "id", "name", "email", "phone", "source", "departure", "stage", "probability",
        "tickets", "subtotal", "discounted_tickets", "discount_amount", "total", "tasks", "notes_log",
        "created_at", "updated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = dict(lead)
        row["tasks"] = json.dumps(lead.get("tasks", []), ensure_ascii=False)
        row["notes_log"] = json.dumps(lead.get("notes_log", []), ensure_ascii=False)
        writer.writerow({key: safe_csv_cell(row.get(key, "")) for key in columns})
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=premxca-leads.csv"},
    )


import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    print(f"PREMXCA CRM corriendo en http://0.0.0.0:{port}")
    print(f"Web pública: http://0.0.0.0:{port}/")
    print(f"Panel CRM:   http://0.0.0.0:{port}/crm")
    app.run(host="0.0.0.0", port=port, debug=debug)