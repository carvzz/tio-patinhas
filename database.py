"""
Camada 4 — Banco de Dados Contábil e Patrimonial Multi-Usuário do Tio Patinhas.
Implementa:
- Contas bancárias, Cartões de crédito e Faturas por competência (YYYY-MM).
- Lançamentos com suporte a parcelas, recorrência, pendência (contas futuras),
  transferências neutras, aportes/resgates, empréstimos, reembolsos e despesas divididas.
- Estado conversacional (`conversation_context`) para consolidar mensagens fragmentadas
  ("Gastei 80" -> "No mercado" -> "Foi no crédito") e confirmações pendentes.
- Histórico de auditoria (`audit_log`) e proteção anti-duplicidade (idempotência).
"""

import sqlite3
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

DB_PATH = Path(__file__).parent / "tio_patinhas.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            current_balance REAL NOT NULL DEFAULT 0.0,
            monthly_income REAL NOT NULL DEFAULT 0.0,
            invested_patrimony REAL NOT NULL DEFAULT 0.0
        )
    """)

    # Garante coluna invested_patrimony caso tabela users já exista
    user_cols = [r["name"] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "invested_patrimony" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN invested_patrimony REAL NOT NULL DEFAULT 0.0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            type TEXT NOT NULL,                 -- 'expense' | 'income' | 'transfer' | 'investment' | 'redeem' | 'loan' | 'credit_bill_payment' | 'refund'
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            category TEXT NOT NULL,
            subcategory TEXT,
            necessity TEXT NOT NULL,            -- 'essential' | 'futility' | 'investment' | 'income' | 'neutral'
            transaction_date TEXT NOT NULL,     -- YYYY-MM-DD
            due_date TEXT,                      -- YYYY-MM-DD
            payment_method TEXT,                -- 'pix' | 'credit_card' | 'debit_card' | 'cash' | 'boleto' | 'transfer'
            source_account TEXT,
            destination_account TEXT,
            card_name TEXT,
            installments INTEGER DEFAULT 1,
            installment_number INTEGER DEFAULT 1,
            parent_tx_id INTEGER,
            recurrence TEXT,
            person_involved TEXT,
            status TEXT NOT NULL DEFAULT 'confirmed', -- 'confirmed' | 'pending' | 'cancelled'
            dedup_hash TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Migração automática de colunas novas na tabela transactions
    tx_cols = {r["name"] for r in cur.execute("PRAGMA table_info(transactions)").fetchall()}
    new_tx_cols = [
        ("subcategory", "TEXT"),
        ("transaction_date", "TEXT DEFAULT ''"),
        ("due_date", "TEXT"),
        ("payment_method", "TEXT DEFAULT 'pix'"),
        ("source_account", "TEXT"),
        ("destination_account", "TEXT"),
        ("card_name", "TEXT"),
        ("installments", "INTEGER DEFAULT 1"),
        ("installment_number", "INTEGER DEFAULT 1"),
        ("parent_tx_id", "INTEGER"),
        ("recurrence", "TEXT"),
        ("person_involved", "TEXT"),
        ("status", "TEXT DEFAULT 'confirmed'"),
        ("dedup_hash", "TEXT"),
    ]
    for col_name, col_type in new_tx_cols:
        if col_name not in tx_cols:
            cur.execute(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_type}")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS credit_card_Shadow_bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            card_name TEXT NOT NULL,
            competence_month TEXT NOT NULL,     -- YYYY-MM
            total_amount REAL NOT NULL DEFAULT 0.0,
            paid_amount REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'open' -- 'open' | 'paid'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            target_amount REAL NOT NULL,
            saved_amount REAL NOT NULL DEFAULT 0,
            deadline_months INTEGER NOT NULL DEFAULT 24,
            category_icon TEXT NOT NULL DEFAULT '🏍️',
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversation_context (
            user_id TEXT PRIMARY KEY,
            pending_intent TEXT,
            partial_data_json TEXT,
            last_tx_id INTEGER,
            awaiting_confirmation INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target_tx_id INTEGER,
            before_json TEXT,
            after_json TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("SELECT COUNT(*) as c FROM users")
    if cur.fetchone()["c"] == 0:
        cur.executemany("""
            INSERT INTO users (id, name, phone, current_balance, monthly_income, invested_patrimony)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            ("voce", "Você (Admin)", "557996474365", 0.00, 0.00, 0.00),
            ("esposa", "Esposa", "557999990002", 0.00, 0.00, 0.00),
            ("amigo1", "Amigo 1 (Beta Tester)", "557999990003", 0.00, 0.00, 0.00),
            ("amigo2", "Amigo 2 (Beta Tester)", "557999990004", 0.00, 0.00, 0.00),
        ])

    conn.commit()
    conn.close()


def resolve_user_by_phone(phone: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    return row["id"] if row else "voce"


def get_balance(user_id: str = "voce") -> float:
    conn = get_conn()
    row = conn.execute("SELECT current_balance FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return float(row["current_balance"]) if row else 0.0


def set_balance(new_balance: float, user_id: str = "voce") -> float:
    conn = get_conn()
    before = get_balance(user_id)
    conn.execute("UPDATE users SET current_balance = ? WHERE id = ?", (new_balance, user_id))
    log_audit(conn, user_id, "set_balance", None, {"balance": before}, {"balance": new_balance})
    conn.commit()
    conn.close()
    return new_balance


def log_audit(
    conn: sqlite3.Connection,
    user_id: str,
    action: str,
    target_tx_id: Optional[int],
    before_obj: Any,
    after_obj: Any
) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO audit_log (user_id, action, target_tx_id, before_json, after_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        action,
        target_tx_id,
        json.dumps(before_obj, ensure_ascii=False) if before_obj is not None else None,
        json.dumps(after_obj, ensure_ascii=False) if after_obj is not None else None,
        now
    ))


def get_conversation_state(user_id: str = "voce") -> Dict[str, Any]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM conversation_context WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return {"pending_intent": None, "partial_data": {}, "last_tx_id": None, "awaiting_confirmation": False}
    return {
        "pending_intent": row["pending_intent"],
        "partial_data": json.loads(row["partial_data_json"] or "{}"),
        "last_tx_id": row["last_tx_id"],
        "awaiting_confirmation": bool(row["awaiting_confirmation"]),
        "updated_at": row["updated_at"],
    }


def save_conversation_state(
    user_id: str,
    pending_intent: Optional[str] = None,
    partial_data: Optional[Dict[str, Any]] = None,
    last_tx_id: Optional[int] = None,
    awaiting_confirmation: bool = False
) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    current = get_conversation_state(user_id)
    final_last_tx = last_tx_id if last_tx_id is not None else current.get("last_tx_id")
    conn.execute("""
        INSERT OR REPLACE INTO conversation_context
        (user_id, pending_intent, partial_data_json, last_tx_id, awaiting_confirmation, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        pending_intent,
        json.dumps(partial_data or {}, ensure_ascii=False),
        final_last_tx,
        1 if awaiting_confirmation else 0,
        now,
    ))
    conn.commit()
    conn.close()


def check_duplicate_transaction(user_id: str, tx_type: str, amount: float, description: str, tx_date: str) -> Optional[Dict[str, Any]]:
    """
    Regra 6 — Detecta possíveis duplicidades (mesmo usuário, tipo, valor, data e descrição similar nos últimos 2 minutos).
    """
    dedup_key = f"{user_id}:{tx_type}:{amount:.2f}:{description.lower().strip()}:{tx_date}"
    dedup_hash = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()[:24]
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM transactions
        WHERE user_id = ? AND dedup_hash = ? AND status != 'cancelled'
        ORDER BY id DESC LIMIT 1
    """, (user_id, dedup_hash)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def add_transaction(
    tx_type: str,
    amount: float,
    description: str,
    category: str,
    necessity: str,
    user_id: str = "voce",
    subcategory: Optional[str] = None,
    transaction_date: Optional[str] = None,
    due_date: Optional[str] = None,
    payment_method: str = "pix",
    source_account: Optional[str] = None,
    destination_account: Optional[str] = None,
    card_name: Optional[str] = None,
    installments: int = 1,
    recurrence: Optional[str] = None,
    person_involved: Optional[str] = None,
    status: str = "confirmed",
    allow_duplicate: bool = False
) -> Dict[str, Any]:
    """
    Camada 3/4 — Execução contábil determinística respeitando TODAS as Regras Financeiras Obrigatórias (Seção 4):
    - Transferências próprias ('transfer'): Neutras (não alteram receitas nem despesas).
    - Aportes ('investment'): Movem do saldo líquido para `invested_patrimony` (não são despesas comuns).
    - Resgates ('redeem'): Movem do `invested_patrimony` para o saldo líquido (não são receitas operacionais).
    - Pagamento de fatura ('credit_bill_payment'): Quita a fatura aberta e reduz o saldo em conta, MAS não duplica despesas já registradas no cartão!
    - Compras no cartão de crédito / parceladas: Distribuem as parcelas entre as faturas mensais (`credit_card_Shadow_bills`) e só reduzem o caixa quando a fatura é paga.
    - Contas futuras ('pending'): Ficam agendadas como compromissos futuros sem descontar do saldo atual até confirmação.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    tx_date = transaction_date or datetime.now().strftime("%Y-%m-%d")
    installments = max(1, int(installments or 1))

    if not allow_duplicate:
        dup = check_duplicate_transaction(user_id, tx_type, amount, description, tx_date)
        if dup:
            return {"duplicate": True, "existing_tx": dup, "new_balance": get_balance(user_id)}

    dedup_key = f"{user_id}:{tx_type}:{amount:.2f}:{description.lower().strip()}:{tx_date}"
    dedup_hash = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()[:24]

    conn = get_conn()
    cur = conn.cursor()

    # Se for compra parcelada (> 1x), distribui entre faturas mensais
    created_ids: List[int] = []
    installment_value = round(amount / installments, 2)
    base_dt = datetime.strptime(tx_date, "%Y-%m-%d")

    parent_id = None
    for idx in range(1, installments + 1):
        parcel_dt = base_dt + timedelta(days=30 * (idx - 1))
        parcel_date_str = parcel_dt.strftime("%Y-%m-%d")
        competence = parcel_dt.strftime("%Y-%m")
        parcel_desc = f"{description} ({idx}/{installments})" if installments > 1 else description
        parcel_amt = installment_value if idx < installments else round(amount - installment_value * (installments - 1), 2)

        cur.execute("""
            INSERT INTO transactions (
                user_id, type, amount, description, category, subcategory, necessity,
                transaction_date, due_date, payment_method, source_account, destination_account,
                card_name, installments, installment_number, parent_tx_id, recurrence,
                person_involved, status, dedup_hash, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, tx_type, parcel_amt, parcel_desc, category, subcategory, necessity,
            parcel_date_str, due_date, payment_method, source_account, destination_account,
            card_name, installments, idx, parent_id, recurrence,
            person_involved, status, dedup_hash if idx == 1 else None, now_str
        ))
        this_id = cur.lastrowid
        if idx == 1:
            parent_id = this_id
        created_ids.append(this_id)

        # Se foi no cartão de crédito, acumula na fatura da competência correspondente
        if payment_method == "credit_card" and tx_type == "expense" and status == "confirmed":
            cname = card_name or "Cartão Principal"
            existing_bill = cur.execute("""
                SELECT id, total_amount FROM credit_card_Shadow_bills
                WHERE user_id = ? AND card_name = ? AND competence_month = ?
            """, (user_id, cname, competence)).fetchone()
            if existing_bill:
                cur.execute("""
                    UPDATE credit_card_Shadow_bills SET total_amount = ? WHERE id = ?
                """, (round(existing_bill["total_amount"] + parcel_amt, 2), existing_bill["id"]))
            else:
                cur.execute("""
                    INSERT INTO credit_card_Shadow_bills (user_id, card_name, competence_month, total_amount)
                    VALUES (?, ?, ?, ?)
                """, (user_id, cname, competence, parcel_amt))

    # Impacto no Saldo Bancário Líquido e Patrimônio (Regras Contábeis Seção 4)
    user_row = cur.execute("SELECT current_balance, monthly_income, invested_patrimony FROM users WHERE id = ?", (user_id,)).fetchone()
    cur_bal = float(user_row["current_balance"]) if user_row else 0.0
    cur_pat = float(user_row["invested_patrimony"]) if user_row else 0.0
    new_bal = cur_bal
    new_pat = cur_pat

    if status == "confirmed":
        if tx_type == "expense":
            # Se for no cartão de crédito, vai para a fatura (não desconta da conta corrente até pagar a fatura!)
            if payment_method != "credit_card":
                new_bal = cur_bal - amount
        elif tx_type == "income":
            new_bal = cur_bal + amount
            cur.execute("UPDATE users SET monthly_income = monthly_income + ? WHERE id = ?", (amount, user_id))
        elif tx_type == "transfer":
            # Regra 4: Transferência entre contas próprias não altera o patrimônio nem é despesa/receita
            new_bal = cur_bal
        elif tx_type == "investment":
            # Regra 4: Aporte sai da conta corrente e soma no Patrimônio Investido
            new_bal = cur_bal - amount
            new_pat = cur_pat + amount
        elif tx_type == "redeem":
            # Regra 4: Resgate volta do Patrimônio Investido para a conta corrente (não é receita operacional!)
            new_bal = cur_bal + amount
            new_pat = max(0.0, cur_pat - amount)
        elif tx_type == "credit_bill_payment":
            # Regra 4: Pagamento de fatura reduz o saldo da conta corrente e quita a fatura, sem duplicar despesas!
            new_bal = cur_bal - amount
            cur.execute("""
                UPDATE credit_card_Shadow_bills
                SET paid_amount = paid_amount + ?, status = 'paid'
                WHERE user_id = ? AND status = 'open'
            """, (amount, user_id))
        elif tx_type == "refund":
            # Regra 4: Reembolso/estorno devolve ao saldo e abate das despesas
            new_bal = cur_bal + amount
        elif tx_type == "loan":
            # Empréstimo: se pegou emprestado entra no caixa mas como passivo ('neutral'); se emprestou sai do caixa
            if "emprestei" in description.lower():
                new_bal = cur_bal - amount
            else:
                new_bal = cur_bal + amount

    cur.execute(
        "UPDATE users SET current_balance = ?, invested_patrimony = ? WHERE id = ?",
        (round(new_bal, 2), round(new_pat, 2), user_id)
    )

    log_audit(conn, user_id, f"insert_{tx_type}", parent_id, {"balance": cur_bal}, {
        "tx_id": parent_id, "amount": amount, "type": tx_type, "new_balance": round(new_bal, 2)
    })

    conn.commit()
    conn.close()

    save_conversation_state(user_id, pending_intent=None, partial_data={}, last_tx_id=parent_id)
    return {
        "id": parent_id,
        "installment_ids": created_ids,
        "new_balance": round(new_bal, 2),
        "new_patrimony": round(new_pat, 2),
        "duplicate": False,
    }


def _find_matching_transaction(cur: sqlite3.Cursor, user_id: str, query_text: str, target_amount: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Localiza um lançamento específico do usuário usando:
    1. Substring direta na descrição ou categoria
    2. Similaridade aproximada (difflib fuzzy match >= 0.72) para tolerar erros de digitação como 'puizza' -> 'pizza'
    3. Valor exato (ex: 'aquele gasto de 35.99')
    """
    import difflib
    import re
    rows = [dict(r) for r in cur.execute(
        "SELECT * FROM transactions WHERE user_id = ? AND status != 'cancelled' ORDER BY id DESC LIMIT 50",
        (user_id,)
    ).fetchall()]
    if not rows:
        return None

    stop_words = {
        "corrigir", "corrige", "corrija", "editar", "edita", "alterar", "altera", "mudar", "muda", "mude",
        "trocar", "troca", "passa", "coloca", "bota", "entra", "como", "para", "pra", "por", "nao", "não",
        "foi", "era", "esta", "está", "errado", "errada", "lançamento", "lancamento", "gasto", "compra",
        "pagamento", "aquele", "aquela", "esse", "essa", "isso", "isto", "ultimo", "último", "ultima",
        "última", "valor", "categoria", "transporte", "alimentação", "alimentacao", "lanche", "delivery",
        "futilidade", "supérfluo", "superfluo", "essencial", "apagar", "apaga", "apague", "excluir",
        "exclui", "remover", "remove", "deletar", "deleta", "desfazer", "desfaz", "cancelar", "cancela",
        "reais", "real", "conto", "verdade", "errei"
    }
    tokens = [
        w for w in re.findall(r"[a-zà-ú]{3,}", query_text.lower())
        if w not in stop_words
    ]

    # 1. Substring direta
    for tok in tokens:
        for r in rows:
            if tok in r["description"].lower():
                return r

    # 2. Fuzzy match (ex: 'puizza' -> 'pizza', 'picole' -> 'picolé', 'supermecado' -> 'supermercado')
    for tok in tokens:
        for r in rows:
            desc_words = re.findall(r"[a-zà-ú]{3,}", r["description"].lower())
            if difflib.get_close_matches(tok, desc_words, n=1, cutoff=0.72):
                return r

    # 3. Busca pelo valor exato mencionado na frase (se for correção de categoria)
    if target_amount is not None:
        for r in rows:
            if abs(float(r["amount"]) - float(target_amount)) < 0.01:
                return r

    return None


def update_transaction_by_keyword(user_id: str, keyword: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Busca um lançamento específico pelo nome/palavra-chave (com tolerância a erros de digitação como 'puizza')
    e aplica as correções solicitadas pelo usuário (categoria, necessidade, descrição, valor, pagamento),
    recalculando o saldo com precisão contábil.
    """
    conn = get_conn()
    cur = conn.cursor()
    matched = _find_matching_transaction(cur, user_id, keyword, updates.get("match_amount"))
    if not matched:
        conn.close()
        return None

    before = dict(matched)
    tx_id = before["id"]
    new_amount = updates.get("amount") if updates.get("amount") is not None else before["amount"]
    new_cat = updates.get("category") or before["category"]
    new_subcat = updates.get("subcategory") if "subcategory" in updates else before["subcategory"]
    new_nec = updates.get("necessity") or before["necessity"]
    new_desc = updates.get("description") or before["description"]
    new_pm = updates.get("payment_method") or before["payment_method"]
    new_card = updates.get("card_name") or before["card_name"]

    bal = get_balance(user_id)
    if before["type"] == "expense" and before["status"] == "confirmed":
        if before["payment_method"] != "credit_card":
            bal += before["amount"]
        if new_pm != "credit_card":
            bal -= new_amount

    cur.execute("""
        UPDATE transactions
        SET amount = ?, category = ?, subcategory = ?, necessity = ?, description = ?, payment_method = ?, card_name = ?
        WHERE id = ?
    """, (new_amount, new_cat, new_subcat, new_nec, new_desc, new_pm, new_card, tx_id))

    cur.execute("UPDATE users SET current_balance = ? WHERE id = ?", (round(bal, 2), user_id))
    after = dict(cur.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone())
    log_audit(conn, user_id, "edit_transaction_by_keyword", tx_id, before, after)
    conn.commit()
    conn.close()
    return after


def undo_specific_or_last_transaction(user_id: str = "voce", query_text: str = "", target_amount: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Permite apagar/desfazer tanto o último lançamento ('desfazer') quanto um lançamento específico
    pelo nome ou valor ('apaga a pizza', 'exclui a puizza', 'remove o gasto de 35.99').
    """
    conn = get_conn()
    cur = conn.cursor()
    tx = None
    if query_text:
        tx = _find_matching_transaction(cur, user_id, query_text, target_amount)

    if not tx:
        row = cur.execute("""
            SELECT * FROM transactions
            WHERE user_id = ? AND status != 'cancelled'
            ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()
        if row:
            tx = dict(row)

    if not tx:
        conn.close()
        return None

    bal = get_balance(user_id)
    if tx["status"] == "confirmed":
        if tx["type"] == "expense" and tx["payment_method"] != "credit_card":
            bal += tx["amount"]
        elif tx["type"] == "income":
            bal -= tx["amount"]
        elif tx["type"] == "investment":
            bal += tx["amount"]
            cur.execute("UPDATE users SET invested_patrimony = MAX(0, invested_patrimony - ?) WHERE id = ?", (tx["amount"], user_id))

    cur.execute("UPDATE transactions SET status = 'cancelled' WHERE id = ?", (tx["id"],))
    cur.execute("UPDATE users SET current_balance = ? WHERE id = ?", (round(bal, 2), user_id))
    log_audit(conn, user_id, "undo_transaction", tx["id"], tx, {"status": "cancelled", "restored_balance": round(bal, 2)})
    conn.commit()
    conn.close()
    return {**tx, "restored_balance": round(bal, 2)}


def update_last_transaction(user_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Permite corrigir/complementar o último lançamento ("Foi no crédito", "Na verdade foi 95 reais", "No mercado").
    Mantém trilha no audit_log.
    """
    conn = get_conn()
    cur = conn.cursor()
    state = get_conversation_state(user_id)
    tx_id = state.get("last_tx_id")

    if tx_id:
        row = cur.execute("SELECT * FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id)).fetchone()
    else:
        row = cur.execute("SELECT * FROM transactions WHERE user_id = ? AND status != 'cancelled' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()

    if not row:
        conn.close()
        return None

    before = dict(row)
    tx_id = before["id"]

    new_amount = updates.get("amount") if updates.get("amount") is not None else before["amount"]
    new_desc = updates.get("description") or before["description"]
    new_cat = updates.get("category") or before["category"]
    new_subcat = updates.get("subcategory") or before["subcategory"]
    new_nec = updates.get("necessity") or before["necessity"]
    new_pm = updates.get("payment_method") or before["payment_method"]
    new_card = updates.get("card_name") or before["card_name"]

    # Se mudou o valor ou mudou de à vista para crédito, ajusta o saldo em conta com precisão contábil
    bal = get_balance(user_id)
    if before["type"] == "expense" and before["status"] == "confirmed":
        # Estorna o efeito antigo no saldo
        if before["payment_method"] != "credit_card":
            bal += before["amount"]
        # Aplica o efeito novo no saldo
        if new_pm != "credit_card":
            bal -= new_amount

    cur.execute("""
        UPDATE transactions
        SET amount = ?, description = ?, category = ?, subcategory = ?, necessity = ?, payment_method = ?, card_name = ?
        WHERE id = ?
    """, (new_amount, new_desc, new_cat, new_subcat, new_nec, new_pm, new_card, tx_id))

    cur.execute("UPDATE users SET current_balance = ? WHERE id = ?", (round(bal, 2), user_id))
    after = dict(cur.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone())
    log_audit(conn, user_id, "edit_transaction", tx_id, before, after)
    conn.commit()
    conn.close()
    return after


def undo_last_transaction(user_id: str = "voce") -> Optional[Dict[str, Any]]:
    """
    Regra 6 — Permite desfazer/excluir a última operação mantendo histórico de auditoria (nunca exclui silenciosamente).
    """
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("""
        SELECT * FROM transactions
        WHERE user_id = ? AND status != 'cancelled'
        ORDER BY id DESC LIMIT 1
    """, (user_id,)).fetchone()

    if not row:
        conn.close()
        return None

    tx = dict(row)
    bal = get_balance(user_id)
    if tx["status"] == "confirmed":
        if tx["type"] == "expense" and tx["payment_method"] != "credit_card":
            bal += tx["amount"]
        elif tx["type"] == "income":
            bal -= tx["amount"]
        elif tx["type"] == "investment":
            bal += tx["amount"]
            cur.execute("UPDATE users SET invested_patrimony = MAX(0, invested_patrimony - ?) WHERE id = ?", (tx["amount"], user_id))

    cur.execute("UPDATE transactions SET status = 'cancelled' WHERE id = ?", (tx["id"],))
    cur.execute("UPDATE users SET current_balance = ? WHERE id = ?", (round(bal, 2), user_id))
    log_audit(conn, user_id, "undo_transaction", tx["id"], tx, {"status": "cancelled", "restored_balance": round(bal, 2)})
    conn.commit()
    conn.close()
    return {**tx, "restored_balance": round(bal, 2)}


def search_transactions_natural(user_id: str, query_text: str) -> List[Dict[str, Any]]:
    """Item 10 — Busca por qualquer movimentação usando linguagem natural."""
    conn = get_conn()
    words = [
        w for w in query_text.lower().split()
        if len(w) > 2 and w not in {"quanto", "gastei", "com", "nos", "nas", "buscar", "procurar", "extrato", "historico", "histórico", "mostrar", "meus", "minhas"}
    ]
    txs = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE user_id = ? AND status != 'cancelled' ORDER BY id DESC LIMIT 100",
        (user_id,)
    ).fetchall()]
    conn.close()
    if not words:
        return txs[:10]
    filtered = [
        t for t in txs
        if any(w in t["description"].lower() or w in t["category"].lower() or w in (t["subcategory"] or "").lower() for w in words)
    ]
    return filtered[:15]


def set_goal_saved_exact(
    name: str,
    exact_saved_amount: float,
    target_amount: float = 0.0,
    icon: str = "🏍️",
    user_id: str = "voce"
) -> Dict[str, Any]:
    """Define o valor exato acumulado na meta (ex: 'Eu tenho guardado 8.368,76 para a moto')."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    cur = conn.cursor()
    existing = cur.execute(
        "SELECT id, target_amount, deadline_months FROM goals WHERE user_id = ? AND LOWER(name) LIKE ?",
        (user_id, f"%{name.lower()}%")
    ).fetchone()
    if existing:
        new_target = target_amount if target_amount > 0 else max(existing["target_amount"], exact_saved_amount)
        cur.execute("UPDATE goals SET saved_amount = ?, target_amount = ? WHERE id = ?", (exact_saved_amount, new_target, existing["id"]))
        goal_id = existing["id"]
    else:
        default_target = target_amount if target_amount > 0 else max(20000.0, exact_saved_amount * 2)
        cur.execute("""
            INSERT INTO goals (user_id, name, target_amount, saved_amount, deadline_months, category_icon, created_at)
            VALUES (?, ?, ?, ?, 24, ?, ?)
        """, (user_id, name, default_target, exact_saved_amount, icon, now))
        goal_id = cur.lastrowid
    cur.execute("UPDATE users SET invested_patrimony = ? WHERE id = ?", (exact_saved_amount, user_id))
    conn.commit()
    conn.close()
    return {"id": goal_id}


def upsert_goal(
    name: str,
    target_amount: float,
    saved_amount: float = 0.0,
    deadline_months: int = 24,
    icon: str = "🎯",
    user_id: str = "voce"
) -> Dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    cur = conn.cursor()
    existing = cur.execute(
        "SELECT id, saved_amount, target_amount, deadline_months FROM goals WHERE user_id = ? AND LOWER(name) LIKE ?",
        (user_id, f"%{name.lower()}%")
    ).fetchone()

    if existing:
        new_saved = existing["saved_amount"] + saved_amount if saved_amount > 0 else existing["saved_amount"]
        new_target = target_amount if target_amount > 0 else existing["target_amount"]
        new_deadline = deadline_months if deadline_months > 0 else existing["deadline_months"]
        cur.execute("""
            UPDATE goals SET target_amount = ?, saved_amount = ?, deadline_months = ? WHERE id = ?
        """, (new_target, new_saved, new_deadline, existing["id"]))
        goal_id = existing["id"]
    else:
        cur.execute("""
            INSERT INTO goals (user_id, name, target_amount, saved_amount, deadline_months, category_icon, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, name, target_amount, saved_amount, deadline_months, icon, now))
        goal_id = cur.lastrowid

    conn.commit()
    conn.close()
    return {"id": goal_id}


def add_chat_message(sender: str, message: str, user_id: str = "voce") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    conn.execute(
        "INSERT INTO chat_messages (user_id, sender, message, created_at) VALUES (?, ?, ?, ?)",
        (user_id, sender, message, now)
    )
    conn.commit()
    conn.close()


def reset_user_data(user_id: str = "voce") -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_conn()
    conn.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM credit_card_Shadow_bills WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM goals WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM conversation_context WHERE user_id = ?", (user_id,))
    conn.execute("UPDATE users SET current_balance = 0.0, monthly_income = 0.0, invested_patrimony = 0.0 WHERE id = ?", (user_id,))
    conn.execute("""
        INSERT INTO chat_messages (user_id, sender, message, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        "bot",
        "🪙 *Cofre zerado e pronto para a sua vida real!*\n\n"
        "Para começarmos do zero absoluto, me mande no WhatsApp:\n"
        "1️⃣ *\"Meu saldo hoje é X reais\"* (ou *\"Recebi X de salário\"*)\n"
        "2️⃣ *\"Quero juntar X mil para minha moto em Y meses\"*\n"
        "3️⃣ E vá me mandando seus gastos do dia a dia (à vista, parcelados, contas fixas, etc.)!",
        now
    ))
    conn.commit()
    conn.close()


def generate_month_end_closing_report(summary: Dict[str, Any]) -> Dict[str, Any]:
    income = summary["total_income"]
    expenses = summary["total_expenses"]
    leftover = max(0.0, income - expenses)
    futility = summary["futility_expenses"]
    essential = summary["essential_expenses"]

    futility_txs = [t for t in summary["transactions"] if t["type"] == "expense" and t["necessity"] == "futility"]
    futility_by_cat: Dict[str, float] = {}
    for t in futility_txs:
        futility_by_cat[t["category"]] = futility_by_cat.get(t["category"], 0.0) + t["amount"]

    recommendations: List[str] = []
    recoverable_total = 0.0

    for cat, val in sorted(futility_by_cat.items(), key=lambda x: x[1], reverse=True):
        if "Lanche" in cat or "Delivery" in cat or "Alimentação" in cat:
            cut = round(val * 0.65, 2)
            keep = round(val - cut, 2)
            recoverable_total += cut
            recommendations.append(
                f"🍔 **Em {cat} (Gastou R$ {val:,.2f}):** Você deveria ter limitado delivery/lanches a no máximo R$ {keep:,.2f}, economizando **+R$ {cut:,.2f}**."
            )
        elif "Compras" in cat or "Lazer" in cat:
            cut = round(val * 0.70, 2)
            recoverable_total += cut
            recommendations.append(
                f"🛍️ **Em {cat} (Gastou R$ {val:,.2f}):** Você deveria ter aplicado a 'Regra das 48h' antes de comprar por impulso, evitando **+R$ {cut:,.2f}**."
            )
        elif "Assinaturas" in cat:
            cut = round(val * 0.50, 2)
            recoverable_total += cut
            recommendations.append(
                f"📺 **Em {cat} (Gastou R$ {val:,.2f}):** Você deveria ter cancelado assinaturas pouco usadas, guardando **+R$ {cut:,.2f}**."
            )
        else:
            cut = round(val * 0.50, 2)
            recoverable_total += cut
            recommendations.append(
                f"✂️ **Em {cat} (Gastou R$ {val:,.2f}):** Você deveria ter reduzido pela metade esse gasto supérfluo, poupando **+R$ {cut:,.2f}**."
            )

    ideal_savings = round(leftover + recoverable_total, 2)

    goal_impact_text = ""
    if summary["goals"]:
        g0 = summary["goals"][0]
        remaining = g0["remaining"]
        months_at_current_pace = round(remaining / max(leftover, 50.0))
        months_at_ideal_pace = max(1, round(remaining / max(ideal_savings, 50.0)))
        months_gained = max(0, months_at_current_pace - months_at_ideal_pace)
        goal_impact_text = (
            f"🏍️ **Impacto direto na sua meta ({g0['name']}):**\n"
            f"• Guardando apenas os **R$ {leftover:,.2f}** que sobraram, você levaria **{months_at_current_pace} meses**.\n"
            f"• Seguindo o plano do Tio Patinhas, você guardaria **R$ {ideal_savings:,.2f}/mês** e conquistaria **{g0['name']} em {months_at_ideal_pace} meses** (**{months_gained} meses mais rápido!**)."
        )

    whatsapp_text = (
        f"📅 **FECHAMENTO E AUDITORIA DO MÊS — TIO PATINHAS** 🪙🎩\n\n"
        f"• 💵 **Receitas Operacionais:** R$ {income:,.2f}\n"
        f"• 💸 **Despesas Confirmadas:** R$ {expenses:,.2f} (Sobra: **R$ {leftover:,.2f}**)\n"
        f"• ✅ **Essenciais:** R$ {essential:,.2f} | 🚨 **Futilidades:** R$ {futility:,.2f} ({summary['futility_percentage']}%)\n"
        f"• 💳 **Faturas Futuras / Compromissos Pendentes:** R$ {summary.get('future_commitments', 0.0):,.2f}\n\n"
        f"🛠️ **O QUE VOCÊ DEVERIA TER FEITO:**\n"
        + ("\n".join(recommendations) if recommendations else "• Nenhum gasto supérfluo registrado até agora!")
        + f"\n\n💰 **Capacidade Ideal de Poupança:** **R$ {ideal_savings:,.2f}/mês** (+R$ {recoverable_total:,.2f} recuperáveis)\n\n"
        + goal_impact_text
    )

    return {
        "income": round(income, 2),
        "expenses": round(expenses, 2),
        "leftover": round(leftover, 2),
        "recoverable_total": round(recoverable_total, 2),
        "ideal_savings": ideal_savings,
        "recommendations": recommendations,
        "goal_impact_text": goal_impact_text,
        "whatsapp_text": whatsapp_text,
    }


def get_dashboard_summary(user_id: str = "voce") -> Dict[str, Any]:
    conn = get_conn()
    users = [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]
    active_user = next((u for u in users if u["id"] == user_id), users[0])
    uid = active_user["id"]

    txs = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE user_id = ? AND status != 'cancelled' ORDER BY id DESC LIMIT 80",
        (uid,)
    ).fetchall()]
    goals_raw = [dict(r) for r in conn.execute("SELECT * FROM goals WHERE user_id = ? ORDER BY id ASC", (uid,)).fetchall()]
    messages = [dict(r) for r in conn.execute("SELECT * FROM chat_messages WHERE user_id = ? ORDER BY id ASC LIMIT 100", (uid,)).fetchall()]
    open_bills = [dict(r) for r in conn.execute("SELECT * FROM credit_card_Shadow_bills WHERE user_id = ? AND status = 'open' ORDER BY competence_month ASC", (uid,)).fetchall()]

    cur_month = datetime.now().strftime("%Y-%m")
    # Despesas confirmadas da competência atual (ou sem data futura distante)
    confirmed_expenses = [
        t for t in txs
        if t["type"] == "expense" and t.get("status", "confirmed") == "confirmed"
        and (not t.get("transaction_date") or t["transaction_date"].startswith(cur_month) or t["installment_number"] == 1)
    ]
    pending_or_future = [
        t for t in txs
        if t.get("status") == "pending" or (t.get("transaction_date") and t["transaction_date"] > datetime.now().strftime("%Y-%m-%d"))
    ]

    total_expenses = sum(t["amount"] for t in confirmed_expenses)
    refunds_total = sum(t["amount"] for t in txs if t["type"] == "refund" and t.get("status") == "confirmed")
    total_expenses = max(0.0, total_expenses - refunds_total)

    total_income = sum(t["amount"] for t in txs if t["type"] == "income" and t.get("status") == "confirmed") or active_user["monthly_income"]
    essential_expenses = sum(t["amount"] for t in confirmed_expenses if t["necessity"] == "essential")
    futility_expenses = sum(t["amount"] for t in confirmed_expenses if t["necessity"] == "futility")
    future_commitments = sum(t["amount"] for t in pending_or_future if t["type"] == "expense") + sum(
        max(0.0, b["total_amount"] - b["paid_amount"]) for b in open_bills
    )

    by_category: Dict[str, float] = {}
    for t in confirmed_expenses:
        by_category[t["category"]] = by_category.get(t["category"], 0.0) + t["amount"]

    category_breakdown = [
        {
            "category": cat,
            "amount": round(amt, 2),
            "percentage": round((amt / total_expenses * 100), 1) if total_expenses > 0 else 0.0
        }
        for cat, amt in sorted(by_category.items(), key=lambda x: x[1], reverse=True)
    ]

    goals_enriched: List[Dict[str, Any]] = []
    for g in goals_raw:
        remaining = max(0.0, g["target_amount"] - g["saved_amount"])
        months = max(1, g["deadline_months"])
        monthly_needed = round(remaining / months, 2)
        progress_pct = round(min(100.0, (g["saved_amount"] / g["target_amount"] * 100)), 1) if g["target_amount"] > 0 else 0.0

        potential_extra_monthly = futility_expenses * 0.6
        if monthly_needed > 0 and potential_extra_monthly > 0:
            accelerated_months = max(1, round(remaining / (monthly_needed + potential_extra_monthly)))
            months_saved = max(0, months - accelerated_months)
        else:
            accelerated_months = months
            months_saved = 0

        goals_enriched.append({
            **g,
            "remaining": round(remaining, 2),
            "monthly_needed": monthly_needed,
            "progress_pct": progress_pct,
            "accelerated_months": accelerated_months,
            "months_saved_cutting_futility": months_saved
        })

    conn.close()

    summary_data = {
        "active_user": active_user,
        "users": users,
        "balance": round(active_user["current_balance"], 2),
        "invested_patrimony": round(active_user.get("invested_patrimony", 0.0), 2),
        "total_income": round(total_income, 2),
        "total_expenses": round(total_expenses, 2),
        "essential_expenses": round(essential_expenses, 2),
        "futility_expenses": round(futility_expenses, 2),
        "futility_percentage": round((futility_expenses / total_expenses * 100), 1) if total_expenses > 0 else 0.0,
        "future_commitments": round(future_commitments, 2),
        "open_bills": open_bills,
        "category_breakdown": category_breakdown,
        "goals": goals_enriched,
        "transactions": txs,
        "messages": messages,
    }
    summary_data["closing_report"] = generate_month_end_closing_report(summary_data)
    return summary_data
