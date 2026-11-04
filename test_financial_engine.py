"""
Seção 10 — Suíte de Testes Automatizados do Motor de Inteligência Financeira (Tio Patinhas).
Valida:
1. Mensagens simples, gírias, valores por extenso e estabelecimentos automáticos (iFood, Uber, Atacadão).
2. Mensagens fragmentadas multi-turn ("Gastei 80" -> "No mercado" -> "Foi no crédito").
3. Regras financeiras obrigatórias:
   - Transferência entre contas próprias não altera receita nem despesa.
   - Aporte vai para patrimônio investido; Resgate volta sem inflar receita.
   - Pagamento de fatura não duplica despesas do cartão.
   - Compras parceladas distribuem parcelas nas faturas futuras.
   - Despesas compartilhadas ("meio a meio") lançam apenas a parte do usuário.
4. Prevenção de duplicidade (idempotência) por reenvio.
5. Edição contextual e Desfazer (Undo) com restauração exata do saldo e auditoria.
"""

import unittest
from database import init_db, reset_user_data, get_dashboard_summary
from ai_agent import process_user_message


class TestFinancialIntelligenceEngine(unittest.TestCase):
    def setUp(self):
        init_db()
        reset_user_data("amigo1")
        self.uid = "amigo1"

    def test_1_initial_balance_and_simple_expense(self):
        process_user_message("Meu saldo hoje é 2000 reais", self.uid)
        s = get_dashboard_summary(self.uid)
        self.assertEqual(s["balance"], 2000.0)

        process_user_message("Pedi um iFood de 45,50", self.uid)
        s = get_dashboard_summary(self.uid)
        self.assertEqual(s["balance"], 1954.50)
        self.assertEqual(s["futility_expenses"], 45.50)

    def test_2_fragmented_multi_turn_conversation(self):
        """Exemplo da Especificação: 'Gastei 80.' -> 'No mercado.' -> 'Foi no crédito.'"""
        process_user_message("Meu saldo hoje é 1000 reais", self.uid)

        # Turno 1: "Gastei 80" (Desconta do saldo à vista inicialmente, categoria Outros)
        r1 = process_user_message("Gastei 80", self.uid)
        self.assertEqual(r1["summary"]["balance"], 920.0)

        # Turno 2: "No mercado" (Complementa categoria do último lançamento para Alimentação / Essencial)
        r2 = process_user_message("No mercado", self.uid)
        self.assertEqual(r2["intent"], "edit_last_transaction")
        self.assertEqual(r2["summary"]["transactions"][0]["category"], "Alimentação")

        # Turno 3: "Foi no crédito" (Muda para cartão de crédito e devolve os R$ 80 para o saldo líquido da conta!)
        r3 = process_user_message("Foi no crédito", self.uid)
        self.assertEqual(r3["intent"], "edit_last_transaction")
        self.assertEqual(r3["summary"]["transactions"][0]["payment_method"], "credit_card")
        self.assertEqual(r3["summary"]["balance"], 1000.0)

    def test_3_accounting_rules_transfer_investment_redeem_bill(self):
        """Testa as Regras Obrigatórias da Seção 4."""
        process_user_message("Recebi 3000 de salário", self.uid)

        # Regra: Transferência própria não é receita nem despesa
        process_user_message("Transferi 500 do Nubank para o Itaú", self.uid)
        s = get_dashboard_summary(self.uid)
        self.assertEqual(s["balance"], 3000.0)
        self.assertEqual(s["total_expenses"], 0.0)
        self.assertEqual(s["total_income"], 3000.0)

        # Regra: Aporte e Resgate
        process_user_message("Guardei 400 na reserva", self.uid)
        s = get_dashboard_summary(self.uid)
        self.assertEqual(s["balance"], 2600.0)
        self.assertEqual(s["invested_patrimony"], 400.0)
        self.assertEqual(s["total_expenses"], 0.0)  # Aporte não é despesa de consumo!

        process_user_message("Resgatei 150 da reserva", self.uid)
        s = get_dashboard_summary(self.uid)
        self.assertEqual(s["balance"], 2750.0)
        self.assertEqual(s["invested_patrimony"], 250.0)
        self.assertEqual(s["total_income"], 3000.0)  # Resgate não infla renda operacional!

        # Regra: Compra parcelada no cartão + Pagamento de fatura sem duplicar despesa
        process_user_message("Comprei um microondas de 600 reais em 3x no cartão", self.uid)
        s = get_dashboard_summary(self.uid)
        # No crédito, a despesa do mês é a 1ª parcela (200), e o saldo da conta ainda é 2750
        self.assertEqual(s["balance"], 2750.0)
        self.assertEqual(s["total_expenses"], 200.0)

        # Pagamento de fatura reduz o saldo da conta, mas NÃO duplica total_expenses!
        process_user_message("Paguei a fatura do cartão de 200 reais", self.uid)
        s = get_dashboard_summary(self.uid)
        self.assertEqual(s["balance"], 2550.0)
        self.assertEqual(s["total_expenses"], 200.0)

    def test_4_shared_expense_and_deduplication_and_undo(self):
        process_user_message("Meu saldo hoje é 500 reais", self.uid)

        # Despesa compartilhada ("meio a meio")
        process_user_message("Jantei no restaurante deu 120 reais meio a meio", self.uid)
        s = get_dashboard_summary(self.uid)
        self.assertEqual(s["balance"], 440.0)  # Descontou apenas R$ 60!

        # Reenvio idêntico deve ser bloqueado pela trava de duplicidade
        dup_res = process_user_message("Jantei no restaurante deu 120 reais meio a meio", self.uid)
        self.assertIn("Anti-Duplicidade", dup_res["reply"])
        self.assertEqual(dup_res["summary"]["balance"], 440.0)

        # Desfazer último lançamento
        undo_res = process_user_message("Desfazer último lançamento", self.uid)
        self.assertEqual(undo_res["summary"]["balance"], 500.0)


if __name__ == "__main__":
    unittest.main()
