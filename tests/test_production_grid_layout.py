import math
import unittest


# These values are the calibrated production-card content box contract.  Keeping
# the reference optimizer independent from the browser implementation makes a
# change to the runtime algorithm visible as a deliberate regression-test update.
CONTENT_WIDTH = 394
CONTENT_HEIGHT = 326
GRID_GAP = 4
FIT_SAFETY = 0.995

CARD_COUNTS = (1, 2, 4, 6, 7, 9, 12)
EXPECTED_LAYOUTS = {
    1: (1, 1),
    2: (2, 1),
    4: (2, 2),
    6: (3, 2),
    7: (4, 2),
    9: (3, 3),
    12: (4, 3),
}


def choose_layout(width, height, count):
    """Reference implementation of optimizeProductionGrid's selection contract."""
    aspect = CONTENT_WIDTH / CONTENT_HEIGHT
    selected = None

    for columns in range(1, count + 1):
        rows = math.ceil(count / columns)
        max_cell_width = (width - ((columns - 1) * GRID_GAP)) / columns
        max_cell_height = (height - ((rows - 1) * GRID_GAP)) / rows
        cell_width = min(max_cell_width, max_cell_height * aspect)
        cell_height = cell_width / aspect
        score = cell_width * cell_height
        candidate = {
            "columns": columns,
            "rows": rows,
            "cell_width": cell_width,
            "cell_height": cell_height,
            "score": score,
            "empty_slots": (columns * rows) - count,
        }

        if (
            selected is None
            or score > selected["score"] * 1.005
            or (
                score >= selected["score"] * 0.995
                and candidate["empty_slots"] < selected["empty_slots"]
            )
        ):
            selected = candidate

    return selected


class TestProductionGridLayout(unittest.TestCase):
    def assert_layout_matrix(self, width, height):
        for count, expected_grid in EXPECTED_LAYOUTS.items():
            with self.subTest(viewport=(width, height), count=count):
                result = choose_layout(width, height, count)
                self.assertEqual(
                    (result["columns"], result["rows"]),
                    expected_grid,
                )

    def test_layout_matrix_for_1920x1080(self):
        expected_cell_sizes = {
            1: (1305.3, 1080.0),
            2: (958.0, 792.7),
            4: (650.2, 538.0),
            6: (637.3, 527.3),
            7: (477.0, 394.7),
            9: (431.9, 357.3),
            12: (431.9, 357.3),
        }

        self.assert_layout_matrix(1920, 1080)
        for count, (expected_width, expected_height) in expected_cell_sizes.items():
            with self.subTest(cell_size_for_count=count):
                result = choose_layout(1920, 1080, count)
                self.assertAlmostEqual(result["cell_width"], expected_width, delta=1.0)
                self.assertAlmostEqual(result["cell_height"], expected_height, delta=1.0)

    def test_layout_matrix_for_1536x864_css_viewport(self):
        self.assert_layout_matrix(1536, 864)
        result = choose_layout(1536, 864, 7)
        self.assertAlmostEqual(result["cell_width"], 381.0, delta=1.0)
        self.assertAlmostEqual(result["cell_height"], 315.0, delta=1.0)

    def test_layout_matrix_for_1366x768(self):
        self.assert_layout_matrix(1366, 768)
        result = choose_layout(1366, 768, 7)
        self.assertAlmostEqual(result["cell_width"], 338.5, delta=1.0)
        self.assertAlmostEqual(result["cell_height"], 280.1, delta=1.0)

    def test_layout_matrix_for_2560x1440(self):
        self.assert_layout_matrix(2560, 1440)
        result = choose_layout(2560, 1440, 7)
        self.assertAlmostEqual(result["cell_width"], 637.0, delta=1.0)
        self.assertAlmostEqual(result["cell_height"], 527.0, delta=1.0)

    def test_seven_cards_prefers_four_by_two(self):
        result = choose_layout(1920, 1080, 7)
        self.assertEqual((result["columns"], result["rows"]), (4, 2))
        self.assertGreater(result["cell_width"], 470)
        self.assertGreater(result["cell_height"], 390)

    def test_cells_preserve_production_card_aspect_ratio(self):
        expected_aspect = CONTENT_WIDTH / CONTENT_HEIGHT
        for count in CARD_COUNTS:
            with self.subTest(count=count):
                result = choose_layout(1920, 1080, count)
                actual_aspect = result["cell_width"] / result["cell_height"]
                self.assertAlmostEqual(actual_aspect, expected_aspect, places=5)

    def test_content_uses_at_least_98_percent_of_cell(self):
        for count in CARD_COUNTS:
            with self.subTest(count=count):
                result = choose_layout(1920, 1080, count)
                scale = min(
                    result["cell_width"] / CONTENT_WIDTH,
                    result["cell_height"] / CONTENT_HEIGHT,
                ) * FIT_SAFETY
                rendered_width = CONTENT_WIDTH * scale
                rendered_height = CONTENT_HEIGHT * scale

                self.assertLessEqual(rendered_width, result["cell_width"] + 0.01)
                self.assertLessEqual(rendered_height, result["cell_height"] + 0.01)
                self.assertGreaterEqual(rendered_width / result["cell_width"], 0.98)
                self.assertGreaterEqual(rendered_height / result["cell_height"], 0.98)


if __name__ == "__main__":
    unittest.main()
