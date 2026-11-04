"""
Conector Direto do WhatsApp para o Tio Patinhas (via Neonize / WhatsApp Multi-Device).
Usa QR Code puro (sem PairPhone para não travar a sessão do QR) e responde
direto no seu WhatsApp em segundos!
"""

import shutil
import importlib
from pathlib import Path
import segno
from neonize.client import NewClient
from neonize.events import ConnectedEv, MessageEv, PairStatusEv
import database
import financial_extractor
import ai_agent

BASE_DIR = Path(__file__).parent
QR_PATH = BASE_DIR / "static" / "wa_qr.png"
ARTIFACT_QR_PATH = Path(r"C:\Users\carva\.gemini\antigravity\brain\da74968b-4bba-4f56-9259-a393ca0beff4\wa_qr_live.png")
STATUS_PATH = BASE_DIR / "wa_direct_status.txt"

database.init_db()
client = NewClient(str(BASE_DIR / "tio_patinhas_qr_session.sqlite3"))


@client.event.qr
def on_qr(cli: NewClient, data_qr: bytes):
    qr_str = data_qr.decode("utf-8")
    qr_img = segno.make(qr_str)
    qr_img.save(str(QR_PATH), scale=10, border=3)
    try:
        shutil.copyfile(str(QR_PATH), str(ARTIFACT_QR_PATH))
    except Exception:
        pass
    STATUS_PATH.write_text("WAITING_QR", encoding="utf-8")
    print(f"[WhatsApp Direto] Novo QR Code gerado em {QR_PATH}", flush=True)


@client.event(ConnectedEv)
def on_connected(cli: NewClient, ev: ConnectedEv):
    STATUS_PATH.write_text("CONNECTED", encoding="utf-8")
    print("[WhatsApp Direto] CONECTADO COM SUCESSO AO WHATSAPP!", flush=True)


@client.event(PairStatusEv)
def on_pair_status(cli: NewClient, ev: PairStatusEv):
    STATUS_PATH.write_text("PAIRED", encoding="utf-8")
    print(f"[WhatsApp Direto] Aparelho pareado com sucesso: {ev}", flush=True)


@client.event(MessageEv)
def on_message(cli: NewClient, message: MessageEv):
    try:
        info = message.Info
        chat_jid = info.MessageSource.Chat
        sender_user = info.MessageSource.Sender.User or ""
        chat_user = chat_jid.User or ""
        is_from_me = info.MessageSource.IsFromMe

        # =====================================================================
        # BLINDAGEM ABSOLUTA DE PRIVACIDADE (HARD LOCK):
        # 1. Bloqueia qualquer Grupo (@g.us), Canal (@newsletter), Broadcast ou Status.
        # 2. Bloqueia qualquer mensagem onde o remetente não seja você mesmo (is_from_me).
        # 3. Bloqueia qualquer conversa com terceiros (chat_user OBRIGATORIAMENTE tem que
        #    ser o seu próprio número/LID "Você mesmo", e NUNCA outro número).
        # =====================================================================
        chat_server = (chat_jid.Server or "").lower()
        sender_server = (info.MessageSource.Sender.Server or "").lower()
        if chat_jid.IsGroup or "g.us" in chat_server or "newsletter" in chat_server or "broadcast" in chat_server or "status" in chat_user:
            return
        if not is_from_me:
            return

        ALLOWED_SELF_CHATS = {"557996474365", "7073945411585"}
        if chat_user not in ALLOWED_SELF_CHATS or sender_user not in ALLOWED_SELF_CHATS:
            return
        is_target_chat = True

        msg = message.Message

        # 1. Suporte a Áudio de Voz (PTT) ou Foto de Comprovante / Nota Fiscal!
        has_audio = msg.HasField("audioMessage")
        has_image = msg.HasField("imageMessage")
        if is_target_chat and (has_audio or has_image):
            importlib.reload(database)
            importlib.reload(financial_extractor)
            importlib.reload(ai_agent)

            media_bytes = cli.download_any(msg)
            if media_bytes:
                mime_type = msg.audioMessage.mimetype if has_audio else msg.imageMessage.mimetype
                caption = msg.imageMessage.caption if has_image else ""
                print(f"[WhatsApp Mídia Recebida] ({mime_type}) de {chat_user}", flush=True)
                database.add_chat_message("user", f"[WhatsApp {'Áudio' if has_audio else 'Foto'}] {caption}", "voce")
                result = ai_agent.process_multimodal_media(media_bytes, mime_type or "audio/ogg", caption, "voce")
                reply_text = result["reply"].replace("**", "*")
                database.add_chat_message("bot", result["reply"], "voce")
                cli.send_message(chat_jid, reply_text)
                return

        text = (
            msg.conversation
            or (msg.extendedTextMessage.text if msg.HasField("extendedTextMessage") else "")
        ).strip()
        if not text:
            return

        # Ignora mensagens geradas pelo próprio Tio Patinhas para não dar loop
        bot_prefixes = (
            "🪙", "🎩", "🍔", "💰", "🎯", "📅", "🤑", "🎙️", "🧾", "🏦", "🔄", "💳", "↩️", "✏️", "📒", "⚠️",
            "Ai, minhas moedinhas", "Anotei no livro-caixa", "Olá, sobrinho", "Olá! Eu sou"
        )
        if any(text.startswith(p) for p in bot_prefixes):
            return

        clean_text = text
        print(f"[WhatsApp Direto Recebido] ({chat_user}): {clean_text}", flush=True)

        # Sempre recarrega os módulos mais recentes sem precisar reiniciar o WhatsApp!
        importlib.reload(database)
        importlib.reload(financial_extractor)
        importlib.reload(ai_agent)

        database.add_chat_message("user", f"[WhatsApp] {clean_text}", "voce")
        result = ai_agent.process_user_message(clean_text, "voce")
        reply_text = result["reply"].replace("**", "*")
        database.add_chat_message("bot", result["reply"], "voce")

        # TRAVA FINAL NO ENVIO: Verifica uma segunda vez que o destino é estritamente 'Você mesmo'
        if chat_jid.IsGroup or chat_user not in ALLOWED_SELF_CHATS:
            return
        cli.send_message(chat_jid, reply_text)
        print(f"[WhatsApp Direto Respondido] -> {chat_user}", flush=True)
    except Exception as e:
        print(f"[Erro no on_message WhatsApp Direto]: {e}", flush=True)


if __name__ == "__main__":
    print("[WhatsApp Direto] Iniciando conector QR...", flush=True)
    client.connect()
