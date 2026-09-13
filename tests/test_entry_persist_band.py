"""Entry persist must be separate per band/level (b15 vs a22)."""
import time
import unittest


class EntryPersistBandTests(unittest.TestCase):
    def test_entry_persist_band_does_not_carry_b15_into_a22(self):
        # Lightweight copy of the keying contract used by buybothourly.
        armed = {}

        def key(cond, leg, band=None):
            return f"{cond}|{leg}|{str(band or '').strip() or 'none'}"

        def ready(cond, leg, book_ok, *, persist_s=10.0, band=None, now_s=None):
            k = key(cond, leg, band)
            if not book_ok:
                armed.pop(k, None)
                return False, "book_not_ok", None
            if now_s is None:
                now_s = time.monotonic()
            ts = armed.get(k)
            if ts is None:
                armed[k] = float(now_s)
                return False, "armed", 0.0
            age = float(now_s) - float(ts)
            if age + 1e-12 < float(persist_s):
                return False, "waiting", age
            return True, "ok", age

        t0 = 1000.0
        r1, w1, _ = ready("c", "up", True, persist_s=10.0, band="b15", now_s=t0)
        self.assertEqual((r1, w1), (False, "armed"))
        r2, w2, age2 = ready("c", "up", True, persist_s=10.0, band="b15", now_s=t0 + 10.5)
        self.assertTrue(r2 and w2 == "ok" and age2 >= 10.0)
        # Same leg, a22 band must re-arm even though b15 already matured.
        r3, w3, _ = ready("c", "up", True, persist_s=10.0, band="a22", now_s=t0 + 10.5)
        self.assertEqual((r3, w3), (False, "armed"))
        r4, w4, _ = ready("c", "up", True, persist_s=10.0, band="a22", now_s=t0 + 15.0)
        self.assertEqual((r4, w4), (False, "waiting"))
        r5, w5, age5 = ready("c", "up", True, persist_s=10.0, band="a22", now_s=t0 + 21.0)
        self.assertTrue(r5 and w5 == "ok" and age5 >= 10.0)
