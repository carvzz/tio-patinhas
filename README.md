# 🪙 Tio Patinhas — Assessor Financeiro Pessoal (WhatsApp + Gemini AI)

Sistema completo de controle financeiro pessoal, detector de futilidades e planejador de metas (Moto, Casa, etc.) com interface de chat estilo WhatsApp, Webhook oficial da Meta (WhatsApp Cloud API) com bloqueio exclusivo para o seu número e integração com o **Google Gemini (`gemini-3.8-flash`)**.

## 🚀 Como rodar no seu computador

```powershell
cd C:\Users\carva\.gemini\antigravity\scratch\tio-patinhas
python server.py
```

Depois, abra no navegador: **http://localhost:8080**

---

## 🧠 Ativando a IA do Google Gemini (Gratuita)

O sistema já funciona imediatamente com o motor local inteligente, mas para ativar o cérebro completo do **Gemini 3.8 Flash**:

1. Instale a SDK oficial:
   ```powershell
   pip install -U google-genai
   ```
2. Defina sua chave gratuita do Google AI Studio:
   ```powershell
   $env:GEMINI_API_KEY="SUA_CHAVE_AQUI"
   python server.py
   ```

---

## 📱 Como conectar ao seu WhatsApp (100% Gratuito via Meta Cloud API)

1. Acesse [developers.facebook.com](https://developers.facebook.com), crie um App do tipo **Business** e adicione o produto **WhatsApp**.
2. Na aba **API Setup**, copie o `Phone Number ID` e o `Access Token` (e adicione seu próprio número de celular como destinatário autorizado).
3. Configure as variáveis de ambiente antes de rodar o `server.py`:
   ```powershell
   $env:WHATSAPP_VERIFY_TOKEN="tio_patinhas_secret_token"
   $env:WHATSAPP_ACCESS_TOKEN="SEU_TOKEN_DA_META"
   $env:WHATSAPP_PHONE_NUMBER_ID="ID_DO_NUMERO_DA_META"
   # Trava de segurança: O bot só vai responder ao seu número!
   $env:WHATSAPP_ALLOWED_NUMBERS="5511999999999"
   ```
4. Exponha sua porta `8080` com **ngrok** (`ngrok http 8080`) ou hospede gratuitamente no Render/Cloudflare e cadastre a URL `https://sua-url/api/whatsapp/webhook` no painel da Meta.
