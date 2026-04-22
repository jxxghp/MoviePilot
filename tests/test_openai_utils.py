from unittest import TestCase

from app.api.openai_utils import (
    build_anthropic_messages,
    build_completion_payload,
    build_prompt,
    build_responses_input,
    build_session_id,
    extract_text_and_images,
)


class OpenAIUtilsTest(TestCase):
    def test_extract_text_and_images(self):
        text, images = extract_text_and_images(
            [
                {"type": "text", "text": "你好"},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                {"type": "text", "text": "世界"},
            ]
        )
        self.assertEqual(text, "你好\n世界")
        self.assertEqual(images, ["https://example.com/a.png"])

    def test_extract_text_and_images_with_input_image_and_base64_image(self):
        text, images = extract_text_and_images(
            [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "https://example.com/b.png"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "YWJj",
                    },
                },
            ]
        )
        self.assertEqual(text, "看图")
        self.assertEqual(
            images,
            ["https://example.com/b.png", "data:image/png;base64,YWJj"],
        )

    def test_build_prompt_without_server_session_keeps_recent_history(self):
        prompt, images = build_prompt(
            [
                {"role": "system", "content": "回答简短"},
                {"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "第一答"},
                {"role": "user", "content": "第二句"},
            ],
            use_server_session=False,
        )
        self.assertIn("系统要求：\n回答简短", prompt)
        self.assertIn("对话上下文：\nuser: 第一句\nassistant: 第一答", prompt)
        self.assertIn("当前用户消息：\n第二句", prompt)
        self.assertEqual(images, [])

    def test_build_prompt_with_server_session_ignores_history_block(self):
        prompt, _ = build_prompt(
            [
                {"role": "user", "content": "历史问题"},
                {"role": "assistant", "content": "历史回答"},
                {"role": "user", "content": "当前问题"},
            ],
            use_server_session=True,
        )
        self.assertNotIn("对话上下文：", prompt)
        self.assertIn("当前用户消息：\n当前问题", prompt)

    def test_build_prompt_accepts_image_only_user_message(self):
        prompt, images = build_prompt(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
                    ],
                }
            ],
            use_server_session=True,
        )
        self.assertIn("请结合图片内容回复", prompt)
        self.assertEqual(images, ["https://example.com/a.png"])

    def test_build_session_id_is_stable(self):
        session_id = build_session_id("user-1", "openai:")
        self.assertTrue(session_id.startswith("openai:"))
        self.assertEqual(session_id, build_session_id("user-1", "openai:"))
        self.assertNotEqual(session_id, build_session_id("user-2", "openai:"))

    def test_build_completion_payload(self):
        payload = build_completion_payload("你好", "moviepilot-agent")
        self.assertEqual(payload["model"], "moviepilot-agent")
        self.assertEqual(payload["choices"][0]["message"]["content"], "你好")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")

    def test_build_responses_input(self):
        messages = build_responses_input(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "你好"}],
                }
            ],
            instructions="你要简短回答",
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")

    def test_build_anthropic_messages(self):
        messages = build_anthropic_messages(
            system=[{"type": "text", "text": "你是助手"}],
            messages=[{"role": "user", "content": "你好"}],
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
