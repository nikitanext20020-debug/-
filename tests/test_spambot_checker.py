import unittest
from types import SimpleNamespace

from modules.spambot_checker import SpamBotChecker


LIMITED_RU_RESPONSE = """
Очень жаль, что Вы с этим столкнулись. К сожалению, иногда наша
антиспам-система излишне сурово реагирует на некоторые действия.
Пока действуют ограничения, Вы не сможете писать тем, кто не сохранил
Ваш номер в список контактов, а также приглашать таких пользователей
в группы или каналы.
"""


class SpamBotParserTests(unittest.TestCase):
    def test_parses_current_russian_comment_restriction(self):
        self.assertEqual(SpamBotChecker._parse_status(LIMITED_RU_RESPONSE), "limited")

    def test_parses_english_limited_response(self):
        text = (
            "While the account is limited, you will not be able to send "
            "messages to people who have not saved your number."
        )
        self.assertEqual(SpamBotChecker._parse_status(text), "limited")

    def test_parses_unrestricted_response(self):
        self.assertEqual(
            SpamBotChecker._parse_status("There are no limits on your account."),
            "ok",
        )

    def test_selects_status_message_after_greeting(self):
        messages = [
            SimpleNamespace(out=False, text="Hello! I’m very sorry that you contacted me."),
            SimpleNamespace(
                out=False,
                text=(
                    "While the limits are active, you will not be able to write "
                    "to people who have not saved your number."
                ),
            ),
        ]
        selected = SpamBotChecker._select_status_message(messages)
        self.assertIn("While the limits are active", selected)

    def test_combines_split_status_messages(self):
        messages = [
            SimpleNamespace(out=False, text="Hello!"),
            SimpleNamespace(out=False, text="While the account"),
            SimpleNamespace(
                out=False,
                text="is limited, you will not be able",
            ),
            SimpleNamespace(out=False, text="to send messages to new people."),
        ]
        selected = SpamBotChecker._select_status_message(messages)
        self.assertIn("send messages to new people", selected)


if __name__ == "__main__":
    unittest.main()
