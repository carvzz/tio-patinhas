"""
Camada 2 — Extração de Entidades, Datas Relativas, Valores Monetários e Estabelecimentos.
Converte qualquer texto natural em Português Brasileiro (com gírias, abreviações,
valores por extenso, datas relativas e referências contextuais) em uma estrutura
padronizada `FinancialExtraction` sem nunca inventar dados ausentes.
"""

import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Tuple

# Mapeamento inteligente de estabelecimentos/marcas conhecidas -> (Categoria, Subcategoria, Necessidade)
MERCHANT_KNOWLEDGE_BASE: Dict[str, Tuple[str, str, str]] = {
    "ifood": ("Alimentação", "Delivery", "futility"),
    "rappi": ("Alimentação", "Delivery", "futility"),
    "ze delivery": ("Alimentação", "Bebidas / Delivery", "futility"),
    "zé delivery": ("Alimentação", "Bebidas / Delivery", "futility"),
    "mcdonalds": ("Alimentação", "Fast Food", "futility"),
    "mc donalds": ("Alimentação", "Fast Food", "futility"),
    "bk": ("Alimentação", "Fast Food", "futility"),
    "burger king": ("Alimentação", "Fast Food", "futility"),
    "outback": ("Alimentação", "Restaurante", "futility"),
    "starbucks": ("Alimentação", "Café / Lanche", "futility"),
    "cacau show": ("Alimentação", "Doces", "futility"),
    "uber": ("Transporte", "App de Mobilidade", "essential"),
    "99": ("Transporte", "App de Mobilidade", "essential"),
    "99pop": ("Transporte", "App de Mobilidade", "essential"),
    "shell": ("Transporte", "Combustível", "essential"),
    "ipiranga": ("Transporte", "Combustível", "essential"),
    "petrobras": ("Transporte", "Combustível", "essential"),
    "netflix": ("Assinaturas", "Streaming Vídeo", "futility"),
    "spotify": ("Assinaturas", "Streaming Música", "futility"),
    "amazon prime": ("Assinaturas", "Streaming / Clube", "futility"),
    "disney": ("Assinaturas", "Streaming Vídeo", "futility"),
    "hbo": ("Assinaturas", "Streaming Vídeo", "futility"),
    "max": ("Assinaturas", "Streaming Vídeo", "futility"),
    "globoplay": ("Assinaturas", "Streaming Vídeo", "futility"),
    "youtube premium": ("Assinaturas", "Streaming Vídeo", "futility"),
    "smartfit": ("Saúde", "Academia", "essential"),
    "smart fit": ("Saúde", "Academia", "essential"),
    "drogasil": ("Saúde", "Farmácia", "essential"),
    "pague menos": ("Saúde", "Farmácia", "essential"),
    "raia": ("Saúde", "Farmácia", "essential"),
    "atacadao": ("Alimentação", "Supermercado", "essential"),
    "atacadão": ("Alimentação", "Supermercado", "essential"),
    "assai": ("Alimentação", "Supermercado", "essential"),
    "assaí": ("Alimentação", "Supermercado", "essential"),
    "carrefour": ("Alimentação", "Supermercado", "essential"),
    "gbarbosa": ("Alimentação", "Supermercado", "essential"),
    "ferreira costa": ("Moradia", "Casa & Construção", "essential"),
    "shopee": ("Compras / Lazer", "E-commerce", "futility"),
    "shein": ("Compras / Lazer", "Vestuário", "futility"),
    "mercado livre": ("Compras / Lazer", "E-commerce", "futility"),
    "aliexpress": ("Compras / Lazer", "E-commerce", "futility"),
    "steam": ("Compras / Lazer", "Jogos", "futility"),
    "playstation": ("Compras / Lazer", "Jogos", "futility"),
    "xbox": ("Compras / Lazer", "Jogos", "futility"),
}

EXTENSO_NUMBERS: Dict[str, float] = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "três": 3,
    "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9,
    "dez": 10, "onze": 11, "doze": 12, "quinze": 15, "vinte": 20,
    "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60,
    "setenta": 70, "oitenta": 80, "noventa": 90, "cem": 100, "cento": 100,
    "duzentos": 200, "trezentos": 300, "quatrocentos": 400, "quinhentos": 500,
}

WEEKDAYS_PT = {
    "segunda": 0, "segunda-feira": 0,
    "terca": 1, "terça": 1, "terça-feira": 1,
    "quarta": 2, "quarta-feira": 2,
    "quinta": 3, "quinta-feira": 3,
    "sexta": 4, "sexta-feira": 4,
    "sabado": 5, "sábado": 5,
    "domingo": 6,
}


@dataclass
class FinancialExtraction:
    intent: str
    amount: Optional[float] = None
    nature: Optional[str] = None                  # 'expense' | 'income' | 'transfer' | 'investment' | 'redeem' | 'loan' | 'credit_bill_payment' | 'refund'
    description: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    necessity: Optional[str] = None               # 'essential' | 'futility' | 'investment' | 'income' | 'neutral'
    transaction_date: Optional[str] = None        # YYYY-MM-DD
    due_date: Optional[str] = None                # YYYY-MM-DD
    payment_method: Optional[str] = None          # 'pix' | 'credit_card' | 'debit_card' | 'cash' | 'boleto' | 'transfer'
    source_account: Optional[str] = None
    destination_account: Optional[str] = None
    card_name: Optional[str] = None
    installments: Optional[int] = None
    recurrence: Optional[str] = None              # 'monthly' | 'weekly' | 'yearly' | None
    person_involved: Optional[str] = None
    related_tx_id: Optional[int] = None
    split_ratio: Optional[float] = None           # Ex: 0.5 quando "dividi com minha esposa / paguei metade"
    is_pending: bool = False                      # Contas futuras a pagar/receber vs confirmadas
    confidence: float = 0.90
    references_previous: bool = False             # "esse gasto", "o último lançamento", "foi no crédito"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_brl_amount(text: str) -> Optional[float]:
    """
    Extrai valores em R$ suportando:
    - 'R$ 1.250,50', '80,90', '35'
    - Abreviações: '2k', '2.5k', '1,5 mil', '25 mil', '3 conto', '50 pila', '20 mangos'
    - Valores por extenso ('cinquenta reais', 'duzentos e cinquenta')
    Ignora números que são parcelas ('em 10x', '3x') ou datas ('dia 15', '10/11').
    """
    t = text.lower().strip()

    # Remove padrões de data (dd/mm/yyyy ou dd/mm) e parcelas (10x, 12 vezes) e horas (14h) para não confundir com valor
    cleaned = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", t)
    cleaned = re.sub(r"\bdia\s+\d{1,2}\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{1,2}\s*(?:x|vezes|parcelas?)\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{1,2}h(?:\d{2})?\b", " ", cleaned)
    cleaned = re.sub(r"\b\d+\s*(?:meses|anos?|dias?|semanas?)\b", " ", cleaned)

    # 1. Abreviações com 'k' ou 'mil' (ex: 2.5k, 1,5k, 25 mil)
    k_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:k|mil)\b", cleaned)
    if k_match:
        return round(float(k_match.group(1).replace(",", ".")) * 1000.0, 2)

    # 2. Formato monetário explícito ou numérico (incluindo "8.368.76" com ponto duplo de digitação rápida)
    double_dot = re.search(r"\b(\d{1,3})\.(\d{3})\.(\d{2})\b", cleaned)
    if double_dot:
        return round(float(f"{double_dot.group(1)}{double_dot.group(2)}.{double_dot.group(3)}"), 2)

    matches = re.findall(r"(?:r\$\s*)?(\d{1,3}(?:\.\d{3})+,\d{2}|\d+(?:[.,]\d{1,2})?)", cleaned)
    if matches:
        raw = matches[0]
        if "," in raw and "." in raw:
            raw = raw.replace(".", "").replace(",", ".")
        elif "," in raw:
            raw = raw.replace(",", ".")
        try:
            val = float(raw)
            if val > 0:
                return round(val, 2)
        except ValueError:
            pass

    # 3. Valores por extenso simples (só usa se não houver algarismos numéricos e não for frase de pergunta/revisão)
    if not any(q in cleaned for q in ["quanto", "qual", "como", "onde", "quem", "revisar", "entra como", "me diga", "resumo"]):
        words = re.findall(r"[a-záéíóúâêôãõç]+", cleaned)
        # Se a única palavra for 'um' ou 'uma' (artigo indefinido, ex: "comprei uma pizza"), não conta como R$ 1,00!
        extenso_hits = [w for w in words if w in EXTENSO_NUMBERS and w not in ("um", "uma")]
        if extenso_hits:
            total_extenso = sum(EXTENSO_NUMBERS[w] for w in extenso_hits)
            if "mil" in words:
                total_extenso = (total_extenso or 1.0) * 1000.0
            if total_extenso > 0:
                return round(total_extenso, 2)

    return None


def parse_relative_date(text: str, now: Optional[datetime] = None) -> Tuple[str, Optional[str], bool]:
    """
    Reconhece datas relativas no fuso do usuário:
    - Hoje, ontem, anteontem, amanhã
    - Semana passada, próxima semana, mês que vem, mês passado
    - Dias da semana (na sexta, segunda-feira)
    - Formato brasileiro DD/MM ou DD/MM/YYYY ou 'dia 15'
    Retorna: (transaction_date YYYY-MM-DD, due_date YYYY-MM-DD | None, is_pending_future: bool)
    """
    ref = now or datetime.now()
    t = text.lower()
    is_future = False
    due_date: Optional[str] = None

    if "anteontem" in t:
        dt = ref - timedelta(days=2)
    elif "ontem" in t:
        dt = ref - timedelta(days=1)
    elif "depois de amanhã" in t or "depois de amanha" in t:
        dt = ref + timedelta(days=2)
        is_future = True
    elif "amanhã" in t or "amanha" in t:
        dt = ref + timedelta(days=1)
        is_future = True
    elif "semana passada" in t:
        dt = ref - timedelta(days=7)
    elif "próxima semana" in t or "proxima semana" in t or "semana que vem" in t:
        dt = ref + timedelta(days=7)
        is_future = True
    elif "mês passado" in t or "mes passado" in t:
        dt = ref - timedelta(days=30)
    elif "próximo mês" in t or "proximo mes" in t or "mês que vem" in t or "mes que vem" in t:
        dt = ref + timedelta(days=30)
        is_future = True
    else:
        dt = ref
        # Verifica formato DD/MM/YYYY ou DD/MM
        br_date = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
        if br_date:
            day = int(br_date.group(1))
            month = int(br_date.group(2))
            year = int(br_date.group(3)) if br_date.group(3) else ref.year
            if year < 100:
                year += 2000
            try:
                dt = datetime(year, month, day)
                if dt.date() > ref.date():
                    is_future = True
            except ValueError:
                dt = ref
        else:
            # Verifica 'dia 15', 'vence dia 20'
            day_match = re.search(r"\bdia\s+(\d{1,2})\b", t)
            if day_match:
                day = int(day_match.group(1))
                try:
                    dt = datetime(ref.year, ref.month, day)
                    if dt.date() > ref.date():
                        is_future = True
                except ValueError:
                    dt = ref
            else:
                # Verifica dias da semana ("na sexta", "terça")
                for wname, widx in WEEKDAYS_PT.items():
                    if re.search(rf"\b{wname}\b", t):
                        diff = (ref.weekday() - widx) % 7
                        dt = ref - timedelta(days=diff if diff > 0 else 0)
                        break

    if any(k in t for k in ["vence", "vencimento", "conta futura", "agendado", "pagar dia", "vou pagar"]):
        is_future = True
        due_date = dt.strftime("%Y-%m-%d")

    return dt.strftime("%Y-%m-%d"), due_date, is_future


def extract_payment_and_accounts(text: str) -> Dict[str, Any]:
    t = text.lower()
    payment_method: Optional[str] = None
    card_name: Optional[str] = None
    source_account: Optional[str] = None
    destination_account: Optional[str] = None
    installments: Optional[int] = None

    if any(k in t for k in ["crédito", "credito", "cartão", "cartao", "parcelado", "parcela"]):
        payment_method = "credit_card"
    elif "débito" in t or "debito" in t:
        payment_method = "debit_card"
    elif "pix" in t:
        payment_method = "pix"
    elif "boleto" in t:
        payment_method = "boleto"
    elif "dinheiro" in t or "espécie" in t:
        payment_method = "cash"

    # Parcelas: "em 10x", "3 vezes", "parcelado em 6"
    inst_match = re.search(r"\b(\d{1,2})\s*(?:x|vezes|parcelas?)\b", t) or re.search(r"parcelado\s+em\s+(\d{1,2})\b", t)
    if inst_match:
        installments = int(inst_match.group(1))
        payment_method = "credit_card"

    # Bancos / Contas / Cartões conhecidos
    banks = ["nubank", "itaú", "itau", "bradesco", "inter", "santander", "caixa", "bb", "banco do brasil", "c6", "picpay", "mercado pago"]
    found_banks = [b.title() for b in banks if re.search(rf"\b{b}\b", t)]

    # Transferência: "da conta X para Y" / "do nubank pro itau"
    transfer_match = re.search(r"(?:do|da)\s+([a-z0-9]+)\s+(?:para\s+o|para\s+a|pro|pra|para)\s+([a-z0-9]+)", t)
    if transfer_match and ("transferi" in t or "passei" in t or "mandei" in t):
        source_account = transfer_match.group(1).title()
        destination_account = transfer_match.group(2).title()
    elif found_banks:
        if payment_method == "credit_card":
            card_name = found_banks[0]
        else:
            source_account = found_banks[0]

    return {
        "payment_method": payment_method,
        "card_name": card_name,
        "source_account": source_account,
        "destination_account": destination_account,
        "installments": installments,
    }


def classify_merchant_and_category(text: str) -> Tuple[str, Optional[str], str]:
    """
    Retorna (category, subcategory, necessity) usando base de estabelecimentos
    COM FRONTEIRA DE PALAVRA (word boundary \\b) para que '35.99' nunca mais bata em '99' (Uber/99)!
    """
    t = text.lower()

    # 1. Prioridade para palavras explícitas de comida/lanche/futilidade ou mercado/padaria
    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in [
        "sanduíche", "sanduiche", "lanche", "hambúrguer", "hamburguer", "pizza", "pizzaria",
        "açaí", "acai", "sorvete", "picolé", "picole", "pastel", "delivery", "bar", "cerveja",
        "chopp", "petisco", "restaurante", "cafezinho", "coca", "coca-cola", "refrigerante",
        "refri", "doce", "chocolate", "bolo", "salgado", "coxinha", "esfiha", "sushi"
    ]):
        return "Lanche / Delivery", "Alimentação Fora", "futility"

    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in [
        "mercado", "supermercado", "feira", "açougue", "acougue", "padaria", "hortifruti",
        "almoço", "almoco", "janta", "jantar", "marmita", "pão", "pao", "queijo", "presunto",
        "leite", "ovos", "arroz", "feijão", "feijao", "carne", "frango", "verdura", "fruta"
    ]):
        return "Alimentação", "Mercado / Padaria", "essential"

    # 2. Estabelecimentos conhecidos com word boundary (\b) para números como '35.99' não baterem em '99'!
    for merchant, (cat, subcat, nec) in MERCHANT_KNOWLEDGE_BASE.items():
        if re.search(rf"(?<![\d.,])\b{re.escape(merchant)}\b(?![\d.,])", t):
            return cat, subcat, nec

    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in ["aluguel", "condomínio", "condominio", "luz", "energia", "água", "agua", "internet", "gás", "gas", "iptu", "faxina"]):
        return "Moradia", "Contas Residenciais", "essential"
    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in ["uber", "99pop", "táxi", "taxi", "gasolina", "combustível", "combustivel", "etanol", "ônibus", "onibus", "metrô", "metro", "estacionamento", "pedágio", "pedagio", "oficina", "mecânico", "ipva"]):
        return "Transporte", "Mobilidade", "essential"
    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in ["farmácia", "farmacia", "remédio", "remedio", "consulta", "médico", "medico", "dentista", "exame", "academia", "treino", "suplemento", "creatina", "whey", "plano de saúde"]):
        return "Saúde", "Academia & Saúde", "essential"
    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in ["assinatura", "mensalidade", "streaming", "clube"]):
        return "Assinaturas", "Serviços Recorrentes", "futility"
    if any(re.search(rf"\b{re.escape(k)}\b", t) for k in ["roupa", "camisa", "blusa", "calça", "tênis", "tenis", "sapato", "cinema", "ingresso", "show", "jogo", "presente", "shopping", "futilidade", "besteira"]):
        return "Compras / Lazer", "Estilo de Vida", "futility"

    return "Outros", None, "essential"


def extract_financial_entities(text: str, now: Optional[datetime] = None) -> FinancialExtraction:
    """
    Realiza a Classificação das 14 Intenções da Especificação + Extração Estruturada Completa.
    """
    t = text.lower().strip()
    amount = parse_brl_amount(text)
    tx_date, due_date, is_future = parse_relative_date(text, now)
    pay_info = extract_payment_and_accounts(text)
    cat, subcat, necessity = classify_merchant_and_category(text)

    # Detecção de referências ao contexto anterior ("esse gasto", "o último lançamento", "foi no crédito")
    references_previous = any(
        ref_kw in t for ref_kw in [
            "esse gasto", "essa compra", "aquele pagamento", "aquela conta",
            "último lançamento", "ultimo lancamento", "última compra", "ultima compra",
            "que acabei de", "na verdade foi", "errei o valor", "corrigir", "altera para", "muda para"
        ]
    ) or (
        amount is None and any(
            frag in t for frag in ["no mercado", "na padaria", "no crédito", "no credito", "no pix", "no débito", "no debito", "foi no", "foi na", "em 2x", "em 3x", "em 4x", "em 5x", "em 6x", "em 10x", "em 12x"]
        )
    )

    # Detecção de despesa compartilhada ("dividi com", "meio a meio", "minha parte")
    split_ratio = None
    if any(k in t for k in ["meio a meio", "dividi por 2", "dividi com", "metade pra cada"]):
        split_ratio = 0.5

    # Detecção de recorrência ("todo mês", "mensal", "toda semana", "conta fixa")
    recurrence = None
    if any(k in t for k in ["todo mês", "todo mes", "mensal", "mensalmente", "conta fixa", "recorrente"]):
        recurrence = "monthly"
    elif any(k in t for k in ["toda semana", "semanal"]):
        recurrence = "weekly"

    # Detecção de pessoa envolvida ("emprestei para João", "recebi de Maria")
    person_match = re.search(r"(?:para\s+o|para\s+a|pro|pra|do|da|com\s+o|com\s+a)\s+([A-ZÀ-Ú][a-zà-ú]+)", text)
    person_involved = person_match.group(1) if person_match else None

    # --- CLASSIFICAÇÃO DAS 14 INTENÇÕES ---
    intent = "conversational_query"
    nature = None

    # 0. Resetar / Desfazer
    if any(k in t for k in ["resetar", "reseta", "reset", "zerar", "zere", "zera", "começar do zero", "comecar do zero", "limpar tudo", "apagar tudo"]):
        intent = "reset_all"
    elif any(k in t for k in ["desfazer", "desfazerr", "desfaz", "cancelar último", "apagar último", "apagar o último", "excluir último", "excluir o último", "remover último"]):
        intent = "undo_or_delete_last"
        references_previous = True

    # 1. Edição / Correção de registro anterior
    elif any(k in t for k in ["corrigir", "editar", "na verdade foi", "errei o valor", "muda o último", "alterar último"]):
        intent = "edit_last_transaction"
        references_previous = True

    # 2. Pagamento de Fatura de Cartão (Regra 4: Não pode duplicar despesas já registradas!)
    elif any(k in t for k in ["paguei a fatura", "pagamento da fatura", "pagamento de fatura", "fatura do cartão", "fatura do nubank", "fatura do itau", "quitei a fatura"]):
        intent = "pay_credit_bill"
        nature = "credit_bill_payment"
        cat, subcat, necessity = "Fatura de Cartão", "Quitação de Fatura", "neutral"

    # 3. Estorno / Reembolso / Devolução (Regra 4: Vincula/ajusta despesa, não é receita operacional)
    elif any(k in t for k in ["reembolso", "me reembolsaram", "estorno", "estornaram", "devolução", "devolveram o dinheiro"]):
        intent = "register_refund"
        nature = "refund"
        cat, subcat, necessity = "Reembolso / Estorno", "Ajuste de Despesa", "neutral"

    # 4. Transferência entre contas próprias (Regra 4: Não é receita nem despesa)
    elif any(k in t for k in ["transferi", "transferência", "transferencia", "passei da minha conta", "entre minhas contas"]):
        intent = "register_transfer"
        nature = "transfer"
        necessity = "neutral"

    # 5. Investimentos e Resgates (Regra 4: Aporte não é despesa comum; Resgate não é receita operacional)
    elif any(k in t for k in ["resgatei", "resgate do", "tirei da poupança", "tirei do investimento", "saquei da reserva", "resgatei da reserva"]):
        intent = "register_redemption"
        nature = "redeem"
        cat, subcat, necessity = "Investimentos", "Resgate de Patrimônio", "neutral"
    elif any(k in t for k in [
        "investi", "apliquei no", "coloquei no tesouro", "aportei", "guardei na reserva",
        "guardei para", "guardei pra", "guardei ", "tenho guardado", "já tinha", "ja tinha",
        "adicione mais", "adicionar mais", "para a moto", "pra moto", "para a casa", "pra casa"
    ]) and amount is not None and "?" not in text:
        intent = "register_investment"
        nature = "investment"
        cat, subcat, necessity = "Investimentos", "Aporte Patrimonial", "investment"

    # 6. Empréstimos e Dívidas (Regra 4: Empréstimo pego não é receita operacional!)
    elif any(k in t for k in ["peguei emprestado", "fiz um empréstimo", "emprestimo no banco", "emprestei para", "emprestei pro", "me emprestou"]):
        intent = "register_loan"
        nature = "loan"
        cat, subcat, necessity = "Empréstimos & Dívidas", "Operação de Crédito", "neutral"

    # 7. Metas e Orçamentos
    elif any(k in t for k in ["criar meta", "nova meta", "quero juntar", "meta de comprar", "planejamento para comprar", "definir orçamento", "teto de gastos"]):
        intent = "manage_goal_or_budget"
        nature = "investment"

    # 8. Ajuste de Saldo Inicial / Manual
    elif any(k in t for k in ["meu saldo", "saldo hoje", "saldo na conta", "saldo atual", "tenho na conta hoje"]) and amount is not None and "?" not in text:
        intent = "set_balance"
        nature = "neutral"

    # 9. Fechamento do Mês / Comparações / Análises / Consultas de Extrato
    elif any(k in t for k in ["fechamento", "dia 30", "final do mês", "fim do mês", "relatório do mês", "balanço do mês"]):
        intent = "month_end_closing"
    elif any(k in t for k in [
        "buscar", "procurar", "quanto gastei com", "o que gastei", "que gastei como",
        "histórico de", "extrato", "últimos lançamentos", "ultimos lancamentos",
        "resumo até agora", "resumo ate agora", "resumo do que", "me diga as informações",
        "me diga as informacoes", "apresente-se"
    ]):
        intent = "search_statement" if any(x in t for x in ["extrato", "buscar", "procurar", "como transporte", "gastei com"]) else "financial_analysis_or_query"
    elif "?" in text or "/" in text or any(t.startswith(w) for w in [
        "oi", "olá", "ola", "opa", "bom dia", "boa tarde", "boa noite",
        "quanto", "qual", "como", "posso", "ainda", "me diga", "me fala", "fala tu", "resumo"
    ]):
        if not any(t.startswith(v) for v in ["gastei", "comprei", "paguei", "recebi"]):
            intent = "financial_analysis_or_query"

    # 10. Registro de Receita Operacional (Salário, Freela, Venda, Rendimento)
    if intent == "conversational_query" and any(k in t for k in ["recebi", "salário", "salario", "ganhei", "freela", "vendi", "caiu na conta", "pagamento do cliente"]) and amount is not None:
        intent = "register_income"
        nature = "income"
        cat, subcat, necessity = "Renda", "Receita Operacional", "income"

    # 11. Compras Parceladas / Contas Recorrentes / Contas Futuras / Registro de Despesa
    if intent == "conversational_query":
        has_expense_verb = any(k in t for k in [
            "gastei", "comprei", "paguei", "comi", "fui comer", "pedi", "almocei",
            "jantei", "abasteci", "conta de", "boleto de", "assinatura", "no mercado",
            "na padaria", "no uber", "no ifood", "vence dia", "vou pagar"
        ])
        if amount is not None and (has_expense_verb or len(t.split()) <= 6):
            if pay_info["installments"] and pay_info["installments"] > 1:
                intent = "register_installment_purchase"
            elif recurrence:
                intent = "register_recurring_bill"
            elif is_future:
                intent = "register_future_bill"
            else:
                intent = "register_expense"
            nature = "expense"
        elif has_expense_verb and amount is None:
            # Ex: "Gastei no mercado" (sem valor) ou continuação "No mercado" / "Foi no crédito"
            intent = "partial_or_contextual_input"
            references_previous = True

    return FinancialExtraction(
        intent=intent,
        amount=amount,
        nature=nature,
        description=text.strip(),
        category=cat,
        subcategory=subcat,
        necessity=necessity,
        transaction_date=tx_date,
        due_date=due_date,
        payment_method=pay_info["payment_method"],
        source_account=pay_info["source_account"],
        destination_account=pay_info["destination_account"],
        card_name=pay_info["card_name"],
        installments=pay_info["installments"],
        recurrence=recurrence,
        person_involved=person_involved,
        split_ratio=split_ratio,
        is_pending=is_future,
        confidence=0.95 if amount is not None else 0.75,
        references_previous=references_previous,
    )
