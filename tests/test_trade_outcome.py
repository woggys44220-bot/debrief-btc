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


if __name__ == "__main__":
    unittest.main()
