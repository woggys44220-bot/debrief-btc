import unittest

from debrief_btc import Trade, detect_trade_outcome


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


if __name__ == "__main__":
    unittest.main()
