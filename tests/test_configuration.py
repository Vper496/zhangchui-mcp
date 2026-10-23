from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zhangchui_mcp.configuration import configuration_from_environment


KEY = 'synthetic-key-never-echo-this'


class ConfigurationTests(unittest.TestCase):
    def test_shared_environment_keeps_secret_out_of_repr(self):
        config = configuration_from_environment(environ={'ZHANGCHUI_API_KEY': KEY,
            'ZHANGCHUI_API_BASE': 'https://example.invalid/api/v1/public-query',
            'ZHANGCHUI_TIMEOUT_SECONDS': '12', 'ZHANGCHUI_MIN_INTERVAL_SECONDS': '3'})
        self.assertEqual(config.api_key, KEY)
        self.assertNotIn(KEY, repr(config))
        self.assertEqual((config.timeout_seconds, config.minimum_interval_seconds), (12, 3))
        self.assertEqual(config.base_url, 'https://example.invalid/api/v1/public-query')

    def test_key_file_is_bounded_and_supports_utf8_bom(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic.key'
            path.write_text(KEY + '\n', encoding='utf-8-sig')
            config = configuration_from_environment(environ={'ZHANGCHUI_API_KEY_FILE': str(path)})
            self.assertEqual(config.api_key, KEY)
            path.write_text('x' * 5000, encoding='utf-8')
            with self.assertRaises(ValueError):
                configuration_from_environment(environ={'ZHANGCHUI_API_KEY_FILE': str(path)})

    def test_conflicting_and_invalid_configuration_errors_do_not_echo_values(self):
        bad = [{'ZHANGCHUI_API_KEY': KEY, 'ZHANGCHUI_API_KEY_FILE': KEY},
               {'ZHANGCHUI_API_KEY_FILE': KEY}, {'ZHANGCHUI_TIMEOUT_SECONDS': KEY},
               {'ZHANGCHUI_MIN_INTERVAL_SECONDS': KEY},
               {'ZHANGCHUI_API_BASE': 'https://example.invalid:' + KEY},
               {'ZHANGCHUI_API_KEY': KEY + '!'}]
        for environment in bad:
            with self.subTest(environment=tuple(environment)), self.assertRaises(ValueError) as result:
                configuration_from_environment(environ=environment)
            self.assertNotIn(KEY, str(result.exception))


if __name__ == '__main__':
    unittest.main()
