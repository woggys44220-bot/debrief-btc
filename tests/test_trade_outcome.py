import unittest

from debrief_btc import Trade, classify_final_exit, detect_trade_outcome, parse_mt5_tabulated_report


class TradeOutcomeTest(unittest.TestCase):
    def test_positive_trade_far_from_tp_is_sl_profit(self):
        trade = Trade(
            index=1,
            open_time=None,
            close_time=None,
            symbol="BTCUSD",
            side="BUY",
            lot=0.05,
            entry_price=80042.50,
            exit_price=80099.77,
            profit=2.86,
            commission=None,
            swap=None,
            comment="",
            sl=80121.17,
            tp=80357.20,
        )

        outcome = detect_trade_outcome(trade)

        self.assertEqual(outcome.outcome_detected, "SL_PROFIT")
        self.assertEqual(outcome.reason, "sl_moved_in_profit")

    def test_mt5_tp_comment_overrides_sl_profit_heuristic(self):
        trade = Trade(
            index=2,
            open_time=None,
            close_time=None,
            symbol="BTCUSD",
            side="SELL",
            lot=0.05,
            entry_price=80600.37,
            exit_price=80256.51,
            profit=17.19,
            commission=None,
            swap=None,
            comment="close [tp 80309.90]",
            sl=80750.00,
            tp=80309.90,
        )

        outcome = detect_trade_outcome(trade)

        self.assertEqual(outcome.outcome_detected, "TP")
        self.assertEqual(outcome.reason, "mt5_close_comment_tp")

    def test_mt5_sl_comment_with_positive_profit_is_sl_profit(self):
        trade = Trade(
            index=3,
            open_time=None,
            close_time=None,
            symbol="BTCUSD",
            side="BUY",
            lot=0.05,
            entry_price=80100.00,
            exit_price=80180.00,
            profit=4.00,
            commission=None,
            swap=None,
            comment="close [sl 80180]",
            sl=80180.00,
            tp=80400.00,
        )

        outcome = detect_trade_outcome(trade)

        self.assertEqual(outcome.outcome_detected, "SL_PROFIT")
        self.assertEqual(outcome.reason, "mt5_close_comment_sl_positive")

    def test_final_exit_without_true_close_comment_is_uncertain_low(self):
        trade = Trade(
            index=4,
            open_time=None,
            close_time=None,
            symbol="BTCUSD",
            side="SELL",
            lot=0.05,
            entry_price=80600.37,
            exit_price=80256.51,
            profit=17.19,
            commission=None,
            swap=None,
            comment="position_id=317125312 sl=80 527,75 tp=80 309,90",
            ticket="317125312",
            sl=80527.75,
            tp=80309.90,
        )

        classification = classify_final_exit(trade)

        self.assertEqual(classification["outcome_final"], "SL_PROFIT")
        self.assertEqual(classification["decision_source"], "HEURISTIC_UNCERTAIN")
        self.assertEqual(classification["confidence_outcome"], "LOW")
        self.assertIn("close_reason_missing", classification["conflict_note"])

    def test_tabulated_report_true_tp_comment_makes_high_confidence_tp(self):
        text = """Rapport d'historique de trading
Positions
Heure	Position	Symbole	Type	Volume	Prix	S/L	T/P	Heure	Prix	Commission	Swap	Profit
2026.05.15 10:00:00	317125312	BTCUSD	sell	0.05	80600.37	80 527,75	80 309,90	2026.05.15 10:12:00	80256.51	0,00	0,00	17,19
Ordres
Heure	Ordre	Symbole	Type	Volume	Prix	S/L	T/P	Heure	État	Commentaire
2026.05.15 10:12:00	999	BTCUSD	sell	0.05	80256.51			2026.05.15 10:12:00	exécuté	[tp 80309.90]
Résultats
Nb trades	1
"""

        mt5 = parse_mt5_tabulated_report(text)
        classification = classify_final_exit(mt5["trades"][0])

        self.assertEqual(classification["outcome_final"], "TP")
        self.assertEqual(classification["decision_source"], "MT5_TP")
        self.assertEqual(classification["confidence_outcome"], "HIGH")



if __name__ == "__main__":
    unittest.main()
