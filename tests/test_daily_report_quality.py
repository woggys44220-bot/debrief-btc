import tempfile
import unittest
from pathlib import Path

from debrief_btc import (
    Trade,
    analyze_stop_after_gain,
    classify_day_quality,
    detect_trade_outcome,
    parse_console,
)


class DailyReportQualityTest(unittest.TestCase):
    def test_may_8_trade_remains_sl_profit_not_true_tp(self):
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
        self.assertNotEqual(outcome.outcome_detected, "TP")

        metrics = {
            "profit_net": 2.86,
            "tp_count": 0,
            "sl_count": 0,
            "be_count": 0,
            "sl_profit_count": 1,
        }
        self.assertEqual(classify_day_quality(metrics), "JOURNÉE_PROTÉGÉE")

    def test_stop_after_gain_separates_tp_from_sl_profit(self):
        sl_profit_trade = Trade(
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

        analysis = analyze_stop_after_gain([sl_profit_trade])

        self.assertEqual(analysis["premier_trade_result"], "SL_PROFIT")
        self.assertIsNone(analysis["profit_si_stop_apres_premier_tp"])
        self.assertEqual(analysis["profit_si_stop_apres_premier_trade_gagnant"], 2.86)
        self.assertEqual(analysis["conclusion"], "PAS_ASSEZ_DE_DONNÉES")

    def test_stop_after_gain_uses_mt5_profit_column_not_recomputed_net(self):
        trade = Trade(
            index=1,
            open_time=None,
            close_time=None,
            symbol="BTCUSD",
            side="BUY",
            lot=0.05,
            entry_price=81380.93,
            exit_price=80971.61,
            profit=-20.47,
            commission=-3.39,
            swap=None,
            comment="",
            sl=80971.61,
            tp=82000.00,
        )

        analysis = analyze_stop_after_gain([trade])

        self.assertEqual(analysis["premier_trade_profit"], -20.47)


    def test_console_does_not_count_sl_profit_as_sl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "console.txt"
            path.write_text(
                "2026-05-08 10:00:00 CLOSE_DETECTED_SL_PROFIT ticket=1\n"
                "2026-05-08 11:00:00 CLOSE_DETECTED_TP ticket=2\n"
                "2026-05-08 12:00:00 CLOSE_DETECTED_BE ticket=3\n"
                "2026-05-08 13:00:00 CLOSE_DETECTED_SL ticket=4\n",
                encoding="utf-8",
            )

            counts = parse_console(path)["event_counts"]

        self.assertEqual(counts.get("CLOSE_DETECTED_SL_PROFIT"), 1)
        self.assertEqual(counts.get("CLOSE_DETECTED_TP"), 1)
        self.assertEqual(counts.get("CLOSE_DETECTED_BE"), 1)
        self.assertEqual(counts.get("CLOSE_DETECTED_SL"), 1)


if __name__ == "__main__":
    unittest.main()
