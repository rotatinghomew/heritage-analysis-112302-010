import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class InputTests(unittest.TestCase):
    def test_synthetic_csv_files(self):
        files = sorted((ROOT / "fixtures").glob("*.csv"))
        self.assertGreaterEqual(len(files), 2)
        for path in files:
            with self.subTest(path=path.name), path.open(encoding="utf-8", newline="") as stream:
                rows = csv.reader(stream)
                header = next(rows)
                self.assertTrue(header)
                self.assertEqual(len(header), len(set(header)))
                self.assertTrue(list(rows))

if __name__ == "__main__":
    unittest.main()
