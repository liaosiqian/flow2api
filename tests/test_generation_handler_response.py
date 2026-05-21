import json
import unittest

from src.services.generation_handler import GenerationHandler


class GenerationHandlerResponseTests(unittest.TestCase):
    def test_markdown_media_response_is_not_wrapped_as_single_image(self):
        markdown = "![Generated Image 1](https://example.com/1.png)\n![Generated Image 2](https://example.com/2.png)"
        response = GenerationHandler._create_completion_response(
            self=None,
            content=markdown,
            media_type="markdown",
        )
        payload = json.loads(response)
        content = payload["choices"][0]["message"]["content"]

        self.assertEqual(content, markdown)
        self.assertEqual(content.count("![Generated Image"), 2)


if __name__ == "__main__":
    unittest.main()
