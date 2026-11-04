"""
Camada 2 & 3 — Motor de Inteligência Financeira & Regras Contábeis do Tio Patinhas.
Integra:
- Camada 2 (Interpretação IA + `financial_extractor.py`): Compreensão de linguagem natural,
  mensagens fragmentadas ("Gastei 80" -> "No mercado" -> "Foi no crédito"), datas relativas,
  referências contextuais ("esse gasto", "o último lançamento") e detecção de anomalias.
- Camada 3 (Validação & Regras Contábeis Determinísticas):
  - Transferências próprias não são receitas nem despesas.
  - Aportes não são despesas comuns; Resgates não são receitas operacionais.
  - Pagamento de fatura quita o cartão sem duplicar despesas das compras.
  - Compras parceladas são distribuídas entre as faturas mensais.
  - Despesas compartilhadas registram apenas a parte assumida pelo usuário.
  - Prevenção de duplicidade (idempotência) e suporte a Desfazer/Editar com Auditoria.
"""

import os
import re
import json
from typing import Dict, Any, Optional
from financial_extractor import (
    FinancialExtraction,
    extract_financial_entities,
    classify_merchant_and_category,
    extract_payment_and_accounts,
    parse_brl_amount,
)
from database import (
    get_dashboard_summary,
    set_balance,
    add_transaction,
    update_last_transaction,
    update_transaction_by_keyword,
    undo_last_transaction,
    undo_specific_or_last_transaction,
    search_transactions_natural,
    upsert_goal,
    set_goal_saved_exact,
    reset_user_data,
    get_conversation_state,
    save_conversation_state,
)
from supabase_sync import sync_snapshot_to_supabase_async

DEFAULT_GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    "AQ.Ab8RN6Kx9_3iM7BBcWjHrYl9gVV2DtJV0dEA8GRZDZulRKwKSw",
)


def _call_gemini_api(prompt: str, media_b64: Optional[str] = None, mime_type: Optional[str] = None) -> Optional[str]:
    """
    Chama o Gemini suportando tanto chaves Vertex AI Express (`AQ.Ab8RN6...` via endpoint aiplatform)
    quanto chaves padrão AI Studio (`AIzaSy...` via google-genai SDK).
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or DEFAULT_GEMINI_API_KEY or "").strip()
    if not api_key:
        return None

    models = ("gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash-lite")
    if api_key.startswith("AQ."):
        import httpx
        parts = []
        if media_b64 and mime_type:
            parts.append({"inlineData": {"mimeType": mime_type, "data": media_b64}})
        parts.append({"text": prompt})
        payload = {"contents": [{"role": "user", "parts": parts}]}
        for m_name in models:
            try:
                url = f"https://aiplatform.googleapis.com/v1/publishers/google/models/{m_name}:generateContent?key={api_key}"
                resp = httpx.post(url, json=payload, timeout=8.0)
                if resp.status_code == 200:
                    data = resp.json()
                    cands = data.get("candidates") or []
                    if cands:
                        txt_parts = cands[0].get("content", {}).get("parts", [])
                        out = "".join(p.get("text", "") for p in txt_parts).strip()
                        if out:
                            return out
                elif resp.status_code in (401, 403):
                    break
            except Exception:
                continue
        return None

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        for m_name in ("gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.8-flash"):
            try:
                resp = client.models.generate_content(model=m_name, contents=prompt)
                if resp.text:
                    return resp.text.strip()
            except Exception:
                continue
    except Exception:
        pass
    return None


def process_multimodal_media(media_bytes: bytes, mime_type: str, caption: str = "", user_id: str = "voce") -> Dict[str, Any]:
    """
    Suporte a Áudios de Voz (PTT) e Fotos de Comprovantes/Notas Fiscais no WhatsApp via Gemini API!
    """
    import base64
    b64_data = base64.b64encode(media_bytes).decode("utf-8")

    is_audio = "audio" in mime_type or "ogg" in mime_type
    prompt = (
        "Transcreva exatamente o que o usuário falou neste áudio em uma única frase curta em português brasileiro "
        "(ex: 'Gastei 35 reais no sanduíche no pix'). Retorne APENAS a frase transcrita."
        if is_audio else
        f"Leia este comprovante bancário, nota fiscal ou cupom ({caption}). "
        "Extraia a operação em uma única frase em português dizendo o que foi pago/transferido, o estabelecimento, "
        "o valor exato em reais e a forma de pagamento (ex: 'Paguei 84,90 no Supermercado Atacadão no crédito'). "
        "Retorne APENAS a frase."
    )

    extracted_sentence = _call_gemini_api(prompt, media_b64=b64_data, mime_type=mime_type) or caption.strip()

    if not extracted_sentence:
        return {
            "intent": "media_error",
            "reply": "⚠️ Recebi sua mídia, mas não consegui ler o valor nitidamente. Pode me digitar o valor rapidinho?",
            "summary": get_dashboard_summary(user_id),
        }

    res = process_user_message(extracted_sentence, user_id)
    prefix = f"🎙️ *Áudio entendido:* _\"{extracted_sentence}\"_\n\n" if is_audio else f"🧾 *Comprovante lido pela IA:* _\"{extracted_sentence}\"_\n\n"
    res["reply"] = prefix + res["reply"]
    return res


def _format_payment_badge(pm: Optional[str], card_name: Optional[str], installments: Optional[int]) -> str:
    labels = {
        "pix": "⚡ Pix / À vista",
        "credit_card": f"💳 Crédito ({card_name or 'Cartão'})" + (f" em {installments}x" if installments and installments > 1 else ""),
        "debit_card": "💳 Débito",
        "cash": "💵 Dinheiro",
        "boleto": "📄 Boleto",
        "transfer": "🔄 Transferência",
    }
    return labels.get(pm or "pix", "⚡ Conta Corrente")


def _detect_spending_anomaly(amount: float, category: str, summary: Dict[str, Any]) -> Optional[str]:
    """Item 10 — Detecção de gastos fora do padrão e alerta de comprometimento do saldo."""
    txs = [t for t in summary["transactions"] if t["type"] == "expense" and t["category"] == category]
    if len(txs) >= 2:
        avg_cat = sum(t["amount"] for t in txs) / len(txs)
        if amount > avg_cat * 2.0 and amount >= 80:
            return f"🚨 *Alerta Fora do Padrão:* Esse gasto de R$ {amount:,.2f} é mais que o dobro da sua média em *{category}* (R$ {avg_cat:,.2f})!"
    if summary["balance"] > 0 and amount > summary["balance"] * 0.5:
        return f"⚠️ *Alerta de Saldo:* Essa operação consome mais de 50% do saldo que você tinha disponível na conta!"
    return None


def _build_contextual_conversational_reply(user_text: str, summary: Dict[str, Any]) -> str:
    t = user_text.lower().strip()
    balance = summary["balance"]
    patrimony = summary.get("invested_patrimony", 0.0)
    income = summary["total_income"]
    expenses = summary["total_expenses"]
    futility = summary["futility_expenses"]
    future_comm = summary.get("future_commitments", 0.0)
    goals = summary["goals"]

    moto_goal = next((g for g in goals if "moto" in g["name"].lower()), goals[0] if goals else None)
    moto_monthly = moto_goal["monthly_needed"] if moto_goal else 0.0

    delivery_spent = sum(
        c["amount"] for c in summary["category_breakdown"]
        if "lanche" in c["category"].lower() or "delivery" in c["category"].lower()
    )
    recommended_delivery_cap = round(max(120.0, income * 0.08), 2) if income > 0 else 150.0
    delivery_remaining = round(max(0.0, recommended_delivery_cap - delivery_spent), 2)
    safe_to_spend = round(max(0.0, balance - moto_monthly - future_comm), 2)

    if t in ["oi", "ola", "olá", "opa", "bom dia", "boa tarde", "boa noite", "e ai", "e aí", "fala tio patinhas", "oi tio patinhas"] or len(t) <= 4:
        return (
            f"Olá, sobrinho(a)! 🪙🎩 Aqui é o *Tio Patinhas*!\n\n"
            f"• 💰 *Saldo em Conta:* R$ {balance:,.2f}\n"
            f"• 🏦 *Patrimônio Guardado:* R$ {patrimony:,.2f}\n"
            f"• 📉 *Despesas no Mês:* R$ {expenses:,.2f}\n\n"
            f"Pode falar naturalmente comigo: registre gastos à vista ou parcelados, transferências, faturas, pergunte quanto pode gastar com delivery ou peça o *fechamento do mês*!"
        )

    if any(k in t for k in ["delivery", "lanche", "ifood", "sanduíche", "sanduiche", "pizza", "futilidade", "besteira"]) and any(
        q in t for q in ["quanto", "posso", "ainda", "limite", "gastar", "impedir", "atrapalhar", "moto", "casa"]
    ):
        if delivery_remaining > 0:
            status_msg = f"Você ainda pode gastar até *R$ {delivery_remaining:,.2f}* com delivery/lanches este mês sem prejudicar suas metas!"
        else:
            exceeded = round(delivery_spent - recommended_delivery_cap, 2)
            status_msg = f"⚠️ Você já gastou *R$ {delivery_spent:,.2f}* com lanches/delivery, ultrapassando em *R$ {exceeded:,.2f}* o teto seguro (R$ {recommended_delivery_cap:,.2f})!"

        return (
            f"🍔 *Calculadora Anti-Futilidade do Tio Patinhas:*\n\n"
            f"• *Gasto atual com Delivery/Lanches:* R$ {delivery_spent:,.2f}\n"
            f"• *Teto seguro p/ sua renda:* R$ {recommended_delivery_cap:,.2f}/mês\n\n"
            f"👉 {status_msg}\n"
            + (f"\n🏍️ *Meta ({moto_goal['name']}):* Requer *R$ {moto_monthly:,.2f}/mês*." if moto_goal else "")
        )

    if any(k in t for k in ["quanto", "qual", "tenho", "posso gastar", "disponível", "disponivel", "sobrou", "sobra", "saldo", "projeção", "projecao"]):
        return (
            f"💰 *Panorama & Projeção de Caixa:*\n\n"
            f"• *Saldo Líquido Atual:* R$ {balance:,.2f}\n"
            f"• *Patrimônio / Reserva:* R$ {patrimony:,.2f}\n"
            f"• *Receitas no Mês:* R$ {income:,.2f} | *Despesas:* R$ {expenses:,.2f}\n"
            f"• *Faturas / Contas Futuras Pendentes:* R$ {future_comm:,.2f}\n\n"
            f"🎯 *Capacidade Livre Real:* Descontando compromissos futuros e a reserva da sua meta (R$ {moto_monthly:,.2f}/mês), você tem *R$ {safe_to_spend:,.2f} livres* para passar o resto do mês com folga!"
        )

    if any(k in t for k in ["moto", "casa", "meta", "objetivo", "sonho", "planejamento"]):
        if not goals:
            return (
                "🎯 Você ainda não cadastrou nenhuma meta ativa!\n"
                "Me mande por exemplo: *\"Quero juntar 20 mil para minha moto em 24 meses\"* que eu crio o planejamento na hora!"
            )
        lines = [
            f"{g['category_icon']} *{g['name']}*\n"
            f"  • Guardado: *R$ {g['saved_amount']:,.2f}* de R$ {g['target_amount']:,.2f} ({g['progress_pct']}%)\n"
            f"  • Guardar por mês: *R$ {g['monthly_needed']:,.2f}/mês* ({g['deadline_months']} meses)"
            for g in goals
        ]
        return "🎯 *Suas Metas Financeiras:*\n\n" + "\n\n".join(lines)

    return (
        f"🎩 *Tio Patinhas na escuta!*\n\n"
        f"• *Saldo Atual:* R$ {balance:,.2f} | *Gastos no Mês:* R$ {expenses:,.2f}\n"
        f"• *Futilidades:* R$ {futility:,.2f} ({summary['futility_percentage']}%)\n\n"
        f"Pode me mandar qualquer gasto (ex: *\"Gastei 80 no mercado no crédito\"*), pedir para *\"desfazer último\"*, consultar *\"extrato de alimentação\"* ou pedir o *\"fechamento do mês\"*!"
    )


def _handle_contextual_consolidation(user_text: str, user_id: str, ext: FinancialExtraction) -> Optional[Dict[str, Any]]:
    """
    Seção 3 — Contexto Conversacional Multi-Turn:
    Consolida mensagens fragmentadas:
      Usuário: "Gastei 80." (registrou R$ 80 como 'Outros' à vista)
      Usuário: "No mercado." -> Atualiza a descrição/categoria do último lançamento para Supermercado!
      Usuário: "Foi no crédito." -> Atualiza a forma de pagamento do último lançamento para Cartão de Crédito!
    Ou quando o usuário manda primeiro o local e depois o valor:
      Usuário: "Fui no mercado hoje" (sem valor -> guarda em partial_data)
      Usuário: "Deu 140 reais no pix" -> Consolida e executa!
    """
    state = get_conversation_state(user_id)
    t = user_text.lower().strip()

    # Caso A: Havia uma intenção pendente esperando valor (ex: usuário disse antes "Comprei um tênis" sem dizer quanto)
    if state.get("pending_intent") == "awaiting_amount" and ext.amount is not None:
        partial = state.get("partial_data", {})
        merged_desc = f"{partial.get('description', '')} ({user_text.strip()})".strip()
        cat, subcat, nec = classify_merchant_and_category(merged_desc)
        pay = extract_payment_and_accounts(merged_desc)
        tx_res = add_transaction(
            tx_type="expense",
            amount=ext.amount,
            description=merged_desc,
            category=cat,
            necessity=nec,
            user_id=user_id,
            subcategory=subcat,
            transaction_date=ext.transaction_date,
            payment_method=pay["payment_method"] or "pix",
            card_name=pay["card_name"],
            installments=pay["installments"] or 1,
        )
        summary = get_dashboard_summary(user_id)
        return {
            "intent": "register_expense",
            "reply": (
                f"✅ *Lançamento consolidado no Cofre!*\n\n"
                f"• *Descrição:* {merged_desc}\n"
                f"• *Valor:* -R$ {ext.amount:,.2f}\n"
                f"• *Categoria:* {cat} ({'⚠️ Futilidade' if nec == 'futility' else '✅ Essencial'})\n"
                f"• *Pagamento:* {_format_payment_badge(pay['payment_method'], pay['card_name'], pay['installments'])}\n"
                f"• *Saldo em Conta:* R$ {summary['balance']:,.2f}"
            ),
            "summary": summary,
        }

    # Caso B: Correção universal de qualquer lançamento específico ou do último ("A pizza não é transporte, é lanche", "muda a puizza pra futilidade", "corrige o valor da energia pra 250")
    is_correction_phrase = (
        ext.intent == "edit_last_transaction"
        or any(k in t for k in [
            "entra como", "não é", "nao e", "nao é", "não foi", "nao foi", "pode refazer",
            "me refiro", "corrigir", "corrige", "corrija", "mudar", "muda", "mude",
            "alterar", "altera", "altere", "trocar", "troca", "coloca como", "bota como",
            "passa pra", "passa para", "na verdade", "errei", "tá errado", "ta errado", "estava errado"
        ])
    )
    if is_correction_phrase:
        # Remove trechos negados ("não é transporte", "não foi lanche") para não classificar a categoria errada!
        positive_text = re.sub(r"n[ãa]o\s+(?:é|e|foi|entra\s+como|era)\s+[a-zà-ú/]+", " ", user_text, flags=re.I)
        cat, subcat, nec = classify_merchant_and_category(positive_text)

        t_pos = positive_text.lower()
        if any(k in t_pos for k in ["futilidade", "supérfluo", "superfluo", "besteira"]):
            if cat == "Outros":
                cat, subcat = "Lanche / Delivery", "Alimentação Fora"
            nec = "futility"
        elif "essencial" in t_pos:
            nec = "essential"

        updates: Dict[str, Any] = {}
        if cat != "Outros":
            updates["category"] = cat
            updates["subcategory"] = subcat
            updates["necessity"] = nec
        elif "futilidade" in t_pos or "essencial" in t_pos:
            updates["necessity"] = nec

        # Verifica se o usuário está corrigindo o valor ("foi 42", "valor para 42", "na verdade foi 42") vs referenciando o lançamento pelo valor ("aquele gasto de 35.99")
        if ext.amount is not None:
            if any(k in t for k in ["valor", "na verdade foi", "errei", "foi R$", "custou", "deu "]) and not any(k in t for k in ["gasto de", "compra de", "lançamento de", "lancamento de"]):
                updates["amount"] = ext.amount
            else:
                updates["match_amount"] = ext.amount

        if ext.payment_method:
            updates["payment_method"] = ext.payment_method
        if ext.card_name:
            updates["card_name"] = ext.card_name

        if updates:
            # 1º tenta localizar o lançamento específico citado na frase (com fuzzy match p/ 'puizza', etc.)
            updated = update_transaction_by_keyword(user_id, user_text, updates)
            # 2º se não citou nenhum item específico, atualiza o último lançamento
            if not updated:
                updated = update_last_transaction(user_id, updates)
            if updated:
                summary = get_dashboard_summary(user_id)
                return {
                    "intent": "edit_specific_transaction",
                    "reply": (
                        f"✏️ *Lançamento corrigido com sucesso!* (ID #{updated['id']})\n\n"
                        f"• *Item:* {updated['description']}\n"
                        f"• *Valor:* R$ {updated['amount']:,.2f}\n"
                        f"• *Categoria:* {updated['category']}" + (f" ▸ {updated.get('subcategory')}" if updated.get('subcategory') else "") + "\n"
                        f"• *Classificação:* {'⚠️ Futilidade / Supérfluo' if updated['necessity'] == 'futility' else '✅ Essencial'}\n"
                        f"• *Saldo Atualizado em Conta:* R$ {summary['balance']:,.2f}"
                    ),
                    "summary": summary,
                }

    # Caso C: Complementação contextual do último lançamento ("No mercado", "Foi no crédito")
    if ext.references_previous or ext.intent == "partial_or_contextual_input":
        updates = {}
        if ext.payment_method:
            updates["payment_method"] = ext.payment_method
        if ext.card_name:
            updates["card_name"] = ext.card_name
        if ext.category and ext.category != "Outros":
            updates["category"] = ext.category
            updates["subcategory"] = ext.subcategory
            updates["necessity"] = ext.necessity
            updates["description"] = user_text.strip()
        elif any(k in t for k in ["no ", "na ", "mercado", "padaria", "farmácia", "farmacia", "uber", "ifood", "lanchonete"]):
            cat, subcat, nec = classify_merchant_and_category(user_text)
            updates["category"] = cat
            updates["subcategory"] = subcat
            updates["necessity"] = nec
            updates["description"] = user_text.strip()

        if updates:
            updated_tx = update_last_transaction(user_id, updates)
            if updated_tx:
                summary = get_dashboard_summary(user_id)
                return {
                    "intent": "edit_last_transaction",
                    "reply": (
                        f"✏️ *Último lançamento atualizado com sucesso!* (ID #{updated_tx['id']})\n\n"
                        f"• *Item:* {updated_tx['description']}\n"
                        f"• *Valor:* R$ {updated_tx['amount']:,.2f}\n"
                        f"• *Categoria:* {updated_tx['category']}"
                        + (f" ({updated_tx['subcategory']})" if updated_tx.get("subcategory") else "") + "\n"
                        f"• *Forma de Pagamento:* {_format_payment_badge(updated_tx.get('payment_method'), updated_tx.get('card_name'), updated_tx.get('installments'))}\n"
                        f"• *Saldo Atualizado em Conta:* R$ {summary['balance']:,.2f}\n\n"
                        f"_(Se quiser reverter, basta responder \"desfazer\")_"
                    ),
                    "summary": summary,
                }

        # Se não tinha último lançamento mas é uma frase parcial sem valor ("Fui no mercado"), salva no estado esperando o valor
        if ext.amount is None:
            save_conversation_state(
                user_id,
                pending_intent="awaiting_amount",
                partial_data={"description": user_text.strip(), "category": ext.category}
            )
            return {
                "intent": "awaiting_amount",
                "reply": (
                    f"📝 Entendi que você teve uma movimentação (*\"{user_text.strip()}\"*), "
                    f"mas **qual foi o valor em reais** (e se foi no Pix, Débito ou Crédito)?"
                ),
                "summary": get_dashboard_summary(user_id),
            }

    return None


def _gemini_semantic_enrich(user_text: str, ext: FinancialExtraction) -> FinancialExtraction:
    """
    Usa o Gemini (`gemini-2.5-flash-lite` / `gemini-2.5-flash`) para enriquecer a interpretação
    semântica quando uma categoria cai em 'Outros' ou há nuances complexas/gírias.
    """
    if not (ext.category == "Outros" and ext.amount is not None):
        return ext
    try:
        prompt = (
            "Analise a mensagem financeira em português e retorne APENAS um JSON válido com:\n"
            '{"category": "Lanche / Delivery"|"Alimentação"|"Transporte"|"Moradia"|"Saúde"|"Compras / Lazer"|"Assinaturas"|"Outros", '
            '"subcategory": string|null, "necessity": "essential"|"futility"}\n'
            f"Mensagem: {user_text}"
        )
        txt = _call_gemini_api(prompt) or ""
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            data = json.loads(m.group(0))
            if data.get("category") and ext.category == "Outros":
                ext.category = data["category"]
                ext.subcategory = data.get("subcategory")
                ext.necessity = data.get("necessity", ext.necessity)
    except Exception:
        pass
    return ext


def _gemini_conversational_answer(user_text: str, summary: Dict[str, Any]) -> Optional[str]:
    """Usa o Gemini para respostas conversacionais abertas."""
    try:
        sys_inst = (
            "Você é o Tio Patinhas 🪙🎩, guardião financeiro pessoal do usuário no WhatsApp.\n"
            "Responda em português brasileiro, de forma direta, carismática, sem enrolação, usando formatação do WhatsApp (*negrito*).\n"
            "Use SEMPRE os números exatos abaixo (nunca invente valores!):\n"
            f"- Saldo Livre em Conta Corrente: R$ {summary['balance']:,.2f}\n"
            f"- Receita do Mês: R$ {summary['total_income']:,.2f}\n"
            f"- Patrimônio / Total Guardado nas Metas: R$ {summary.get('invested_patrimony', 0.0):,.2f}\n"
            f"- Despesas do Mês: R$ {summary['total_expenses']:,.2f} (Essenciais: R$ {summary['essential_expenses']:,.2f} | Futilidades: R$ {summary['futility_expenses']:,.2f})\n"
            f"- Compromissos/Faturas Futuras: R$ {summary.get('future_commitments', 0.0):,.2f}\n"
            f"- Metas Ativas: {json.dumps(summary['goals'], ensure_ascii=False)}\n"
            f"- Categorias de gasto no mês: {json.dumps(summary['category_breakdown'], ensure_ascii=False)}"
        )
        out = _call_gemini_api(f"{sys_inst}\n\nPergunta do usuário: {user_text}")
        if out:
            return out.replace("**", "*")
    except Exception:
        pass
    return None


def _finalize_and_sync(result: Dict[str, Any]) -> Dict[str, Any]:
    if "summary" in result:
        sync_snapshot_to_supabase_async(result["summary"])
    return result


def process_user_message(user_text: str, user_id: str = "voce") -> Dict[str, Any]:
    """Garante que TODA operação feita no WhatsApp sincronize imediatamente com o Supabase e com o site!"""
    return _finalize_and_sync(_process_user_message_inner(user_text, user_id))


def _process_user_message_inner(user_text: str, user_id: str = "voce") -> Dict[str, Any]:
    """
    Pipeline completo de 4 Camadas:
    1. Entrada -> 2. Extração/Contexto -> 3. Motor de Regras Contábeis -> 4. Persistência & Resposta Auditável.
    """
    summary_before = get_dashboard_summary(user_id)
    ext = _gemini_semantic_enrich(user_text, extract_financial_entities(user_text))
    intent = ext.intent
    amount = ext.amount or 0.0

    # 1. Resetar tudo
    if intent == "reset_all":
        reset_user_data(user_id)
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": "reset_all",
            "reply": (
                "🪙 *Cofre 100% zerado, sobrinho(a)!* Todos os lançamentos de teste foram limpos.\n\n"
                "Pode começar do zero mandando seu saldo, salário, metas ou gastos reais!"
            ),
            "summary": summary_after,
        }

    # 2. Desfazer / Excluir lançamento (específico pelo nome/valor ou o último)
    if intent == "undo_or_delete_last":
        undone = undo_specific_or_last_transaction(user_id, user_text, ext.amount)
        summary_after = get_dashboard_summary(user_id)
        if not undone:
            return {
                "intent": intent,
                "reply": "ℹ️ Não encontrei nenhum lançamento correspondente para apagar/desfazer no seu cofre.",
                "summary": summary_after,
            }
        return {
            "intent": intent,
            "reply": (
                f"↩️ *Lançamento removido/estornado com segurança!* (Registro #{undone['id']} arquivado na auditoria)\n\n"
                f"• *Estornado:* {undone['description']} (R$ {undone['amount']:,.2f})\n"
                f"• *Saldo Restaurado na Conta:* R$ {summary_after['balance']:,.2f}"
            ),
            "summary": summary_after,
        }

    # 2b. Verifica se é consolidação contextual de mensagens fragmentadas ("Gastei 80" -> "No mercado" -> "Foi no crédito")
    contextual_res = _handle_contextual_consolidation(user_text, user_id, ext)
    if contextual_res:
        return contextual_res

    # 3. Busca natural no extrato (Seção 10 — Busca em linguagem natural)
    if intent == "search_statement":
        found = search_transactions_natural(user_id, user_text)
        if not found:
            return {
                "intent": intent,
                "reply": f"🔍 Não encontrei lançamentos correspondentes a *\"{user_text}\"* no seu extrato.",
                "summary": summary_before,
            }
        total_found = sum(t["amount"] for t in found)
        lines = [
            f"• `{t.get('transaction_date') or t['created_at'][:10]}` — *{t['description']}*: R$ {t['amount']:,.2f} ({t['category']})"
            for t in found[:8]
        ]
        return {
            "intent": intent,
            "reply": (
                f"📒 *Extrato Inteligente ({len(found)} lançamentos encontrados):*\n\n"
                + "\n".join(lines)
                + f"\n\n💰 *Soma dos itens listados:* R$ {total_found:,.2f}"
            ),
            "summary": summary_before,
        }

    # 4. Fechamento do Mês (Dia 30)
    if intent == "month_end_closing":
        return {
            "intent": intent,
            "reply": summary_before["closing_report"]["whatsapp_text"],
            "summary": summary_before,
        }

    # 5. Definir Saldo Manual
    if intent == "set_balance":
        set_balance(amount, user_id)
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": intent,
            "reply": (
                f"🪙 *Saldo Bancário Atualizado!*\n\n"
                f"• *Saldo Disponível em Conta:* R$ {summary_after['balance']:,.2f}\n"
                f"_(Registrado com auditoria contábil. Para corrigir, basta mandar o novo valor!)_"
            ),
            "summary": summary_after,
        }

    # 6. Regra 4 — Transferência entre contas próprias (Neutra: não é despesa nem receita!)
    if intent == "register_transfer" and amount > 0:
        tx_res = add_transaction(
            tx_type="transfer",
            amount=amount,
            description=ext.description or "Transferência entre contas próprias",
            category="Transferência",
            necessity="neutral",
            user_id=user_id,
            transaction_date=ext.transaction_date,
            payment_method="transfer",
            source_account=ext.source_account or "Conta Origem",
            destination_account=ext.destination_account or "Conta Destino",
        )
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": intent,
            "reply": (
                f"🔄 *Transferência entre Contas Próprias Registrada!*\n\n"
                f"• *Valor:* R$ {amount:,.2f}\n"
                f"• *Origem ➔ Destino:* {ext.source_account or 'Conta Principal'} ➔ {ext.destination_account or 'Outra Conta'}\n"
                f"• *Regra Contábil:* Operação neutra (não infla suas receitas nem suas despesas do mês!).\n"
                f"• *Saldo Consolidado:* R$ {summary_after['balance']:,.2f}"
            ),
            "summary": summary_after,
        }

    # 7. Regra 4 — Pagamento de Fatura de Cartão (Quita fatura sem duplicar as compras já lançadas!)
    if intent == "pay_credit_bill" and amount > 0:
        tx_res = add_transaction(
            tx_type="credit_bill_payment",
            amount=amount,
            description=ext.description or "Pagamento de Fatura de Cartão",
            category="Fatura de Cartão",
            necessity="neutral",
            user_id=user_id,
            transaction_date=ext.transaction_date,
            payment_method="pix",
            card_name=ext.card_name or "Cartão Principal",
        )
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": intent,
            "reply": (
                f"💳 *Pagamento de Fatura Registrado com Precisão Contábil!*\n\n"
                f"• *Valor Pago:* -R$ {amount:,.2f} ({ext.card_name or 'Cartão'})\n"
                f"• *Regra Contábil:* O valor saiu do seu saldo em conta corrente, mas **não duplicou** suas despesas do mês (pois as compras já foram contabilizadas por competência!).\n"
                f"• *Novo Saldo em Conta:* R$ {summary_after['balance']:,.2f}"
            ),
            "summary": summary_after,
        }

    # 8. Regra 4 — Investimentos (Aportes) e Resgates
    if intent in ("register_investment", "manage_goal_or_budget") and amount > 0:
        t_low = user_text.lower()
        is_goal_creation = any(k in t_low for k in ["criar meta", "nova meta", "quero juntar", "meta de"])
        is_exact_total_saved = any(k in t_low for k in ["tenho guardado", "já contando", "ja contando", "total guardado", "não, eu tenho", "nao, eu tenho"])
        is_previous_months = any(k in t_low for k in ["meses anteriores", "outros meses", "já tinha", "ja tinha"])
        goal_name = "Comprar Minha Moto" if "moto" in t_low else ("Entrada da Casa Própria" if "casa" in t_low else "Reserva / Investimento")
        months_match = re.search(r"(\d+)\s*meses", t_low)
        years_match = re.search(r"(\d+)\s*anos?", t_low)
        deadline = int(months_match.group(1)) if months_match else (int(years_match.group(1)) * 12 if years_match else 24)
        icon = "🏍️" if "moto" in goal_name.lower() else ("🏠" if "casa" in goal_name.lower() else "🎯")

        if is_goal_creation:
            upsert_goal(goal_name, amount, 0.0, deadline, icon, user_id)
            summary_after = get_dashboard_summary(user_id)
            monthly_req = round(amount / max(1, deadline), 2)
            return {
                "intent": intent,
                "reply": (
                    f"🎯 *Nova Meta Cadastrada no Cofre!*\n\n"
                    f"• *Objetivo:* {icon} {goal_name}\n"
                    f"• *Valor Alvo:* R$ {amount:,.2f} em *{deadline} meses*\n"
                    f"• *Quanto guardar por mês:* *R$ {monthly_req:,.2f}/mês*\n"
                    f"• *Saldo Atual em Conta:* R$ {summary_after['balance']:,.2f}"
                ),
                "summary": summary_after,
            }
        elif is_exact_total_saved:
            set_goal_saved_exact(goal_name, amount, 0.0, icon, user_id)
            summary_after = get_dashboard_summary(user_id)
            return {
                "intent": intent,
                "reply": (
                    f"🎯 *Total Guardado para {goal_name} Ajustado com Precisão!*\n\n"
                    f"• *Total Acumulado na Meta:* *R$ {amount:,.2f}*\n"
                    f"• *Saldo Disponível na Conta Corrente:* R$ {summary_after['balance']:,.2f}\n\n"
                    f"_(Não descontei novamente do seu saldo do mês porque é o seu total acumulado!)_"
                ),
                "summary": summary_after,
            }
        elif is_previous_months:
            # Soma na meta e no patrimônio sem descontar do saldo do mês atual!
            upsert_goal(goal_name, 0.0, amount, deadline, icon, user_id)
            summary_after = get_dashboard_summary(user_id)
            goal_obj = next((g for g in summary_after["goals"] if g["name"] == goal_name), None)
            total_goal_now = goal_obj["saved_amount"] if goal_obj else amount
            return {
                "intent": intent,
                "reply": (
                    f"🏦 *Saldo de Meses Anteriores Adicionado à Meta ({goal_name})!*\n\n"
                    f"• *Adicionado de meses anteriores:* +R$ {amount:,.2f}\n"
                    f"• *Total Guardado Agora p/ {goal_name}:* *R$ {total_goal_now:,.2f}*\n"
                    f"• *Saldo Líquido deste Mês na Conta:* R$ {summary_after['balance']:,.2f} _(Intacto!)_"
                ),
                "summary": summary_after,
            }
        else:
            upsert_goal(goal_name, 0.0, amount, deadline, icon, user_id)
            tx_res = add_transaction(
                tx_type="investment",
                amount=amount,
                description=f"Aporte: {goal_name}",
                category="Investimentos",
                necessity="investment",
                user_id=user_id,
                transaction_date=ext.transaction_date,
            )
            summary_after = get_dashboard_summary(user_id)
            goal_obj = next((g for g in summary_after["goals"] if g["name"] == goal_name), None)
            total_goal_now = goal_obj["saved_amount"] if goal_obj else amount
            return {
                "intent": intent,
                "reply": (
                    f"🏦 *Aporte do Mês Registrado!* (ID #{tx_res['id']})\n\n"
                    f"• *Destino:* {icon} {goal_name}\n"
                    f"• *Aportado deste mês:* R$ {amount:,.2f}\n"
                    f"• *Total Acumulado na Meta:* *R$ {total_goal_now:,.2f}*\n"
                    f"• *Saldo Restante na Conta Corrente:* R$ {summary_after['balance']:,.2f}"
                ),
                "summary": summary_after,
            }

    if intent == "register_redemption" and amount > 0:
        add_transaction(
            tx_type="redeem",
            amount=amount,
            description=ext.description or "Resgate de Investimento",
            category="Investimentos",
            necessity="neutral",
            user_id=user_id,
            transaction_date=ext.transaction_date,
        )
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": intent,
            "reply": (
                f"🔄 *Resgate de Reserva/Investimento Registrado!*\n\n"
                f"• *Valor Resgatado:* +R$ {amount:,.2f}\n"
                f"• *Regra Contábil:* O dinheiro voltou para a sua conta corrente, mas **não foi contabilizado como renda operacional** para não distorcer seus ganhos do mês!\n"
                f"• *Novo Saldo em Conta:* R$ {summary_after['balance']:,.2f} | *Patrimônio Restante:* R$ {summary_after['invested_patrimony']:,.2f}"
            ),
            "summary": summary_after,
        }

    # 9. Regra 4 — Reembolsos / Estornos e Empréstimos
    if intent == "register_refund" and amount > 0:
        add_transaction(
            tx_type="refund",
            amount=amount,
            description=ext.description or "Reembolso / Estorno",
            category=ext.category or "Reembolso / Estorno",
            necessity="neutral",
            user_id=user_id,
            transaction_date=ext.transaction_date,
        )
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": intent,
            "reply": (
                f"↩️ *Reembolso / Estorno Registrado!*\n\n"
                f"• *Valor Devolvido:* +R$ {amount:,.2f}\n"
                f"• *Regra Contábil:* Abatido das suas despesas do mês e devolvido ao seu saldo em conta (sem inflar sua renda operacional).\n"
                f"• *Novo Saldo em Conta:* R$ {summary_after['balance']:,.2f}"
            ),
            "summary": summary_after,
        }

    if intent == "register_loan" and amount > 0:
        add_transaction(
            tx_type="loan",
            amount=amount,
            description=ext.description or "Operação de Empréstimo",
            category="Empréstimos & Dívidas",
            necessity="neutral",
            user_id=user_id,
            person_involved=ext.person_involved,
            transaction_date=ext.transaction_date,
        )
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": intent,
            "reply": (
                f"🤝 *Registro de Empréstimo / Dívida Contabilizado!*\n\n"
                f"• *Descrição:* {ext.description}\n"
                f"• *Valor:* R$ {amount:,.2f}" + (f" (Pessoa: {ext.person_involved})" if ext.person_involved else "") + "\n"
                f"• *Regra Contábil:* Classificado como passivo/crédito (não entra como receita salarial do mês!).\n"
                f"• *Saldo em Conta:* R$ {summary_after['balance']:,.2f}"
            ),
            "summary": summary_after,
        }

    # 10. Receita Operacional (Salário, Freela, etc.)
    if intent == "register_income" and amount > 0:
        tx_res = add_transaction(
            tx_type="income",
            amount=amount,
            description=ext.description or "Receita",
            category="Renda",
            necessity="income",
            user_id=user_id,
            transaction_date=ext.transaction_date,
        )
        if tx_res.get("duplicate"):
            return {
                "intent": intent,
                "reply": f"⚠️ *Atenção Anti-Duplicidade:* Já registrei essa entrada de *R$ {amount:,.2f}* hoje! Não lancei duas vezes para proteger seu saldo.",
                "summary": get_dashboard_summary(user_id),
            }
        summary_after = get_dashboard_summary(user_id)
        return {
            "intent": intent,
            "reply": (
                f"🤑 *Receita Operacional Registrada no Caixa Forte!* (ID #{tx_res['id']})\n\n"
                f"• *Entrada:* +R$ {amount:,.2f} (_{ext.description}_)\n"
                f"• *Data:* {ext.transaction_date}\n"
                f"• *Novo Saldo em Conta:* R$ {summary_after['balance']:,.2f}\n\n"
                f"_(Para corrigir ou desfazer, basta responder \"foi X reais\" ou \"desfazer\")_"
            ),
            "summary": summary_after,
        }

    # 11. Registro de Despesas (Simples, Parceladas, Recorrentes, Futuras ou Compartilhadas)
    if intent in ("register_expense", "register_installment_purchase", "register_recurring_bill", "register_future_bill") and amount > 0:
        effective_amount = round(amount * ext.split_ratio, 2) if ext.split_ratio else amount
        split_note = f" (Sua parte de 50% sobre R$ {amount:,.2f})" if ext.split_ratio else ""
        tx_status = "pending" if ext.is_pending else "confirmed"

        anomaly_warning = _detect_spending_anomaly(effective_amount, ext.category or "Outros", summary_before)

        tx_res = add_transaction(
            tx_type="expense",
            amount=effective_amount,
            description=(ext.description or "Despesa") + split_note,
            category=ext.category or "Outros",
            subcategory=ext.subcategory,
            necessity=ext.necessity or "essential",
            user_id=user_id,
            transaction_date=ext.transaction_date,
            due_date=ext.due_date,
            payment_method=ext.payment_method or "pix",
            source_account=ext.source_account,
            card_name=ext.card_name,
            installments=ext.installments or 1,
            recurrence=ext.recurrence,
            person_involved=ext.person_involved,
            status=tx_status,
        )

        if tx_res.get("duplicate"):
            return {
                "intent": intent,
                "reply": (
                    f"⚠️ *Trava Anti-Duplicidade do Tio Patinhas:*\n"
                    f"Esse gasto de *R$ {effective_amount:,.2f}* (_{ext.description}_) já foi registrado agora há pouco! "
                    f"Evitei o lançamento duplicado. Seu saldo continua *R$ {tx_res['new_balance']:,.2f}*."
                ),
                "summary": get_dashboard_summary(user_id),
            }

        summary_after = get_dashboard_summary(user_id)
        tag_str = "⚠️ *Futilidade / Supérfluo*" if ext.necessity == "futility" else "✅ *Essencial*"
        pm_badge = _format_payment_badge(ext.payment_method, ext.card_name, ext.installments)

        status_line = (
            f"📅 *Status:* Agendado para {ext.due_date or ext.transaction_date} (Ainda não descontado do saldo atual)\n"
            if tx_status == "pending" else ""
        )
        installment_line = (
            f"💳 *Parcelamento:* {ext.installments}x de R$ {round(effective_amount / ext.installments, 2):,.2f} distribuídas nas faturas mensais\n"
            if ext.installments and ext.installments > 1 else ""
        )

        reply = (
            f"{'Ai, minhas moedinhas! 🎩💸' if ext.necessity == 'futility' else 'Anotei no livro-caixa! 📒🪙'} (ID #{tx_res['id']})\n\n"
            f"• *Valor:* -R$ {effective_amount:,.2f}{split_note}\n"
            f"• *Descrição:* {ext.description}\n"
            f"• *Categoria:* {ext.category}" + (f" ▸ {ext.subcategory}" if ext.subcategory else "") + f" ({tag_str})\n"
            f"• *Pagamento:* {pm_badge} | *Data:* {ext.transaction_date}\n"
            + installment_line
            + status_line
            + f"• *Saldo Líquido em Conta:* R$ {summary_after['balance']:,.2f}\n"
            + (f"\n{anomaly_warning}\n" if anomaly_warning else "")
            + f"\n_(Se precisar complementar ou corrigir, mande ex: \"foi no crédito\", \"no mercado\" ou \"desfazer\")_"
        )

        return _finalize_and_sync({
            "intent": intent,
            "reply": reply,
            "summary": summary_after,
        })

    # 12. Consultas analíticas, saudações e conversação aberta (Turbinadas com Gemini API + Fallback determinístico)
    gemini_ans = _gemini_conversational_answer(user_text, summary_before)
    return _finalize_and_sync({
        "intent": "financial_analysis_or_query",
        "reply": gemini_ans or _build_contextual_conversational_reply(user_text, summary_before),
        "summary": summary_before,
    })
