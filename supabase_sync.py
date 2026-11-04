"""
Sincronizador Automático com o Supabase (PostgreSQL na Nuvem).
Espelha os usuários, transações, metas e auditoria do banco local para o projeto
`syarremzrocidaoeyhac` no Supabase via REST API (PostgREST).
"""

import os
import threading
import httpx
from typing import Dict, Any, List

SUPABASE_PROJECT_REF = os.environ.get("SUPABASE_PROJECT_REF", "syarremzrocidaoeyhac")
SUPABASE_URL = os.environ.get("SUPABASE_URL", f"https://{SUPABASE_PROJECT_REF}.supabase.co")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN5YXJyZW16cm9jaWRhb2V5aGFjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM3MzQzODcsImV4cCI6MjEwOTMxMDM4N30.7yz46hEl1ciOc04ubY9td98ha2YKEiq14o0wocrnVY4",
)


def _headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def check_supabase_tables_ready() -> bool:
    """Verifica se as tabelas já foram criadas no SQL Editor do Supabase."""
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/users?select=id&limit=1",
            headers=_headers(),
            timeout=3.0,
        )
        return r.status_code == 200
    except Exception:
        return False


def pull_from_supabase_if_local_empty(force: bool = False) -> None:
    """
    Restaura e sincroniza os usuários, metas e transações diretamente do Supabase (`syarremzrocidaoeyhac`)
    para que tudo que for mandado pelo WhatsApp já apareça na hora no site (Render)!
    """
    try:
        from database import get_conn
        conn = get_conn()
        cur = conn.cursor()
        tx_count = cur.execute("SELECT COUNT(*) as c FROM transactions").fetchone()["c"]
        if tx_count > 0 and not force:
            conn.close()
            return

        r_users = httpx.get(f"{SUPABASE_URL}/rest/v1/users?select=*", headers=_headers(), timeout=5.0)
        if r_users.status_code == 200:
            for u in r_users.json():
                cur.execute("""
                    INSERT OR REPLACE INTO users (id, name, phone, current_balance, monthly_income, invested_patrimony)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (u["id"], u["name"], u["phone"], u["current_balance"], u["monthly_income"], u.get("invested_patrimony", 0.0)))

        r_goals = httpx.get(f"{SUPABASE_URL}/rest/v1/goals?select=*", headers=_headers(), timeout=5.0)
        if r_goals.status_code == 200:
            for g in r_goals.json():
                cur.execute("""
                    INSERT OR REPLACE INTO goals (id, user_id, name, target_amount, saved_amount, deadline_months, category_icon, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (g["id"], g["user_id"], g["name"], g["target_amount"], g["saved_amount"], g["deadline_months"], g["category_icon"], g["created_at"]))

        r_txs = httpx.get(f"{SUPABASE_URL}/rest/v1/transactions?select=*", headers=_headers(), timeout=5.0)
        if r_txs.status_code == 200:
            for t in r_txs.json():
                cur.execute("""
                    INSERT OR REPLACE INTO transactions (
                        id, user_id, type, amount, description, category, subcategory, necessity,
                        transaction_date, due_date, payment_method, source_account, destination_account,
                        card_name, installments, installment_number, parent_tx_id, recurrence,
                        person_involved, status, dedup_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t["id"], t["user_id"], t["type"], t["amount"], t["description"], t["category"], t.get("subcategory"),
                    t["necessity"], t.get("transaction_date") or "", t.get("due_date"), t.get("payment_method") or "pix",
                    t.get("source_account"), t.get("destination_account"), t.get("card_name"), t.get("installments") or 1,
                    t.get("installment_number") or 1, t.get("parent_tx_id"), t.get("recurrence"), t.get("person_involved"),
                    t.get("status") or "confirmed", t.get("dedup_hash"), t["created_at"]
                ))

        conn.commit()
        conn.close()
        print("[Supabase Cloud] Dados restaurados da nuvem com sucesso!")
    except Exception as e:
        print(f"[Aviso pull_from_supabase]: {e}")


def sync_snapshot_to_supabase_async(summary: Dict[str, Any]) -> None:
    """Dispara sincronização em background thread para não atrasar a resposta do WhatsApp."""
    def _worker():
        try:
            u = summary.get("active_user")
            if u:
                httpx.post(
                    f"{SUPABASE_URL}/rest/v1/users",
                    headers=_headers(),
                    json={
                        "id": u["id"],
                        "name": u["name"],
                        "phone": u["phone"],
                        "current_balance": u["current_balance"],
                        "monthly_income": u["monthly_income"],
                        "invested_patrimony": u.get("invested_patrimony", 0.0),
                    },
                    timeout=4.0,
                )

            goals: List[Dict[str, Any]] = summary.get("goals", [])
            if goals:
                goals_payload = [
                    {
                        "id": g["id"],
                        "user_id": g["user_id"],
                        "name": g["name"],
                        "target_amount": g["target_amount"],
                        "saved_amount": g["saved_amount"],
                        "deadline_months": g["deadline_months"],
                        "category_icon": g["category_icon"],
                        "created_at": g["created_at"],
                    }
                    for g in goals
                ]
                httpx.post(
                    f"{SUPABASE_URL}/rest/v1/goals",
                    headers=_headers(),
                    json=goals_payload,
                    timeout=4.0,
                )

            txs: List[Dict[str, Any]] = summary.get("transactions", [])[:20]
            if txs:
                tx_payload = [
                    {
                        "id": t["id"],
                        "user_id": t["user_id"],
                        "type": t["type"],
                        "amount": t["amount"],
                        "description": t["description"],
                        "category": t["category"],
                        "subcategory": t.get("subcategory"),
                        "necessity": t["necessity"],
                        "transaction_date": t.get("transaction_date") or "",
                        "due_date": t.get("due_date"),
                        "payment_method": t.get("payment_method") or "pix",
                        "source_account": t.get("source_account"),
                        "destination_account": t.get("destination_account"),
                        "card_name": t.get("card_name"),
                        "installments": t.get("installments") or 1,
                        "installment_number": t.get("installment_number") or 1,
                        "parent_tx_id": t.get("parent_tx_id"),
                        "recurrence": t.get("recurrence"),
                        "person_involved": t.get("person_involved"),
                        "status": t.get("status") or "confirmed",
                        "dedup_hash": t.get("dedup_hash"),
                        "created_at": t["created_at"],
                    }
                    for t in txs
                ]
                httpx.post(
                    f"{SUPABASE_URL}/rest/v1/transactions",
                    headers=_headers(),
                    json=tx_payload,
                    timeout=4.0,
                )
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()
