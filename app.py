"""
Aplicativo Flask do Tio Patinhas (Super leve, bonito e pronto para Supabase ou SQLite).
Rotas:
- GET  /                     -> Dashboard Web + Simulador WhatsApp
- GET  /api/summary          -> Dados dos gráficos, metas e fechamento do dia 30
- POST /api/chat             -> Conversa com a IA do Tio Patinhas
- POST /api/balance          -> Ajuste manual de saldo
- POST /api/goals            -> Criar/atualizar meta (Moto, Casa, etc.)
- GET/POST /api/whatsapp/webhook -> Webhook Oficial do WhatsApp (Meta for Developers)
"""

import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response

from database import (
    init_db,
    get_dashboard_summary,
    add_chat_message,
    set_balance,
    upsert_goal,
    resolve_user_by_phone,
    reset_user_data,
)
from ai_agent import process_user_message

BASE_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="")

# Inicializa banco e puxa os dados do Supabase também quando rodado via Gunicorn na nuvem (Render / Railway)
init_db()
try:
    from supabase_sync import pull_from_supabase_if_local_empty
    pull_from_supabase_if_local_empty()
except Exception:
    pass

PORT = int(os.environ.get("PORT", "8080"))

# Credenciais do WhatsApp Oficial (Meta for Developers)
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "tio_patinhas_secret_token")
WHATSAPP_ACCESS_TOKEN = os.environ.get(
    "WHATSAPP_ACCESS_TOKEN",
    "EAAPfWstF1UABSxgnEwb4L2xZApzasByiV7fZBd9ZCi6D3HN4FtgdT1ntGkZBf9wvfjpWrg62CorzFKGWWm6dCZBhN7RwiueN40BItDq4L4G7UQqm6dhEJXG1ZATbdTVniUB0ctcv1muWP6gXwmI1wLEOIoPD1qYGAgQZBH5J8ZABr9LEbEjJKCsVaJtqJEFZCYG2ztHV5YAKHmJT1aahZAHmHzav6L5LscHihn5IpWuIeh0l4OBatIqGmENz37vfWwIHpde4ETDLJIUFpB58fnEHIkS4j7"
)
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "1494557383732843")
WHATSAPP_WABA_ID = os.environ.get("WHATSAPP_WABA_ID", "1874495946882117")
WHATSAPP_ALLOWED_NUMBERS = [
    n.strip()
    for n in os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "").split(",")
    if n.strip()
]


def send_whatsapp_cloud_message(to_number: str, text_body: str) -> None:
    """Envia mensagem de resposta pela API Oficial Gratuita da Meta (WhatsApp Cloud API)."""
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return
    # Normaliza números brasileiros com 9 extra caso a Meta exija o formato de 12 dígitos na whitelist
    candidates = [to_number]
    if len(to_number) == 13 and to_number.startswith("55") and to_number[4] == "9":
        without_nine = to_number[:4] + to_number[5:]
        if without_nine not in candidates:
            candidates.insert(0, without_nine)

    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    for target in candidates:
        payload = json.dumps({
            "messaging_product": "whatsapp",
            "to": target,
            "type": "text",
            "text": {"body": text_body.replace("**", "*")}
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                out = resp.read().decode("utf-8", errors="ignore")
                with open(BASE_DIR / "wa_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"SUCCESS [{target}]: {out}\n")
                return
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            with open(BASE_DIR / "wa_debug.log", "a", encoding="utf-8") as f:
                f.write(f"HTTP_ERR [{target}] {e.code}: {err_body}\n")
        except Exception as e:
            with open(BASE_DIR / "wa_debug.log", "a", encoding="utf-8") as f:
                f.write(f"ERR [{target}]: {e}\n")


@app.route("/")
def index():
    if (BASE_DIR / "static" / "index.html").exists():
        return send_from_directory(str(BASE_DIR / "static"), "index.html")
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/avatar.jpg")
def serve_avatar():
    if (BASE_DIR / "static" / "avatar.jpg").exists():
        return send_from_directory(str(BASE_DIR / "static"), "avatar.jpg")
    return send_from_directory(str(BASE_DIR), "avatar.jpg")


@app.route("/manifest.json")
def serve_manifest():
    if (BASE_DIR / "static" / "manifest.json").exists():
        return send_from_directory(str(BASE_DIR / "static"), "manifest.json")
    return send_from_directory(str(BASE_DIR), "manifest.json")


@app.route("/api/summary", methods=["GET"])
def api_summary():
    user_id = request.args.get("user_id", "voce")
    from supabase_sync import check_supabase_tables_ready, pull_from_supabase_if_local_empty
    pull_from_supabase_if_local_empty(force=True)
    summary = get_dashboard_summary(user_id)
    supa_ok = check_supabase_tables_ready()
    summary["ai_mode"] = f"Gemini AI Ativo • {'Supabase Cloud OK ☁️' if supa_ok else 'SQLite Local (Pronto p/ SQL Supabase)'}"
    summary["whatsapp_locked"] = bool(WHATSAPP_ALLOWED_NUMBERS)
    return jsonify(summary)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.get_json(silent=True) or {}
    user_id = (body.get("user_id") or "voce").strip()
    user_message = (body.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Mensagem vazia"}), 400

    add_chat_message("user", user_message, user_id)
    result = process_user_message(user_message, user_id)
    add_chat_message("bot", result["reply"], user_id)

    return jsonify({
        "reply": result["reply"],
        "intent": result["intent"],
        "summary": get_dashboard_summary(user_id)
    })


@app.route("/api/balance", methods=["POST"])
def api_balance():
    body = request.get_json(silent=True) or {}
    user_id = (body.get("user_id") or "voce").strip()
    new_balance = float(body.get("balance") or 0.0)
    set_balance(new_balance, user_id)
    return jsonify({"ok": True, "summary": get_dashboard_summary(user_id)})


@app.route("/api/goals", methods=["POST"])
def api_goals():
    body = request.get_json(silent=True) or {}
    user_id = (body.get("user_id") or "voce").strip()
    name = (body.get("name") or "Nova Meta").strip()
    target = float(body.get("target_amount") or 0.0)
    saved = float(body.get("saved_amount") or 0.0)
    months = int(body.get("deadline_months") or 24)
    icon = body.get("icon") or ("🏍️" if "moto" in name.lower() else ("🏠" if "casa" in name.lower() else "🎯"))
    upsert_goal(name, target, saved, months, icon, user_id)
    return jsonify({"ok": True, "summary": get_dashboard_summary(user_id)})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    body = request.get_json(silent=True) or {}
    user_id = (body.get("user_id") or "voce").strip()
    reset_user_data(user_id)
    return jsonify({"ok": True, "summary": get_dashboard_summary(user_id)})


@app.route("/api/whatsapp/webhook", methods=["GET", "POST"])
def whatsapp_webhook():
    # 1. Validação do Webhook pela Meta (GET)
    if request.method == "GET":
        mode = request.args.get("hub.mode", "")
        token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            return Response(challenge, status=200, mimetype="text/plain")
        return jsonify({"error": "Token de verificação inválido"}), 403

    # 2. Recebimento de mensagens reais do WhatsApp (POST)
    body = request.get_json(silent=True) or {}
    with open(BASE_DIR / "wa_debug.log", "a", encoding="utf-8") as f:
        f.write(f"WEBHOOK_IN: {json.dumps(body, ensure_ascii=False)}\n")
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                for msg in val.get("messages", []):
                    sender_phone = msg.get("from", "")
                    if WHATSAPP_ALLOWED_NUMBERS and sender_phone not in WHATSAPP_ALLOWED_NUMBERS:
                        print(f"[Segurança] Número não autorizado ignorado: {sender_phone}")
                        continue

                    wa_user_id = resolve_user_by_phone(sender_phone)
                    if msg.get("type") == "text":
                        text_body = msg.get("text", {}).get("body", "")
                        if text_body:
                            add_chat_message("user", f"[WhatsApp] {text_body}", wa_user_id)
                            result = process_user_message(text_body, wa_user_id)
                            add_chat_message("bot", result["reply"], wa_user_id)
                            send_whatsapp_cloud_message(sender_phone, result["reply"])
    except Exception as e:
        print(f"[Erro Webhook WhatsApp]: {e}")

    return jsonify({"status": "received"}), 200


if __name__ == "__main__":
    init_db()
    from supabase_sync import pull_from_supabase_if_local_empty
    pull_from_supabase_if_local_empty()
    print(f"[Tio Patinhas Flask] Rodando em http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
