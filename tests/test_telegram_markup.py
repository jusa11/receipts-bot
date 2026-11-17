import unittest

from receipt_bot.telegram import TelegramBot


class ReviewKeyboardTests(unittest.TestCase):
    def test_item_buttons_use_product_and_category_names(self):
        markup = TelegramBot._callback_markup(
            "123_45",
            [{"product": "Молоко Домик в деревне", "category": "Молоко"}],
        )
        labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
        self.assertTrue(any("Молоко Домик в деревне" in label and "Молоко" in label for label in labels))
        self.assertTrue(any("Пропустить" in label and "Молоко Домик в деревне" in label for label in labels))
        self.assertFalse(any("измен" in label.lower() for label in labels))
        self.assertFalse(any("катег" in label.lower() for label in labels))
        self.assertFalse(any(label.startswith("1:") for label in labels))

    def test_item_button_labels_stay_within_telegram_limit(self):
        markup = TelegramBot._callback_markup(
            "123_45",
            [{"product": "Очень длинное название товара " * 5, "category": "Очень длинное название категории " * 3}],
        )
        labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
        self.assertTrue(all(len(label) <= 64 for label in labels))


if __name__ == "__main__":
    unittest.main()
