from dataclasses import replace
import os
import unittest
from unittest.mock import patch

from insightquest.config import get_config
from insightquest.page_helpers import render_dataset_scope


class DatasetScopeTests(unittest.TestCase):
    def test_configuration_defaults_and_validates_sample_size(self):
        with patch.dict(os.environ, {"IQ_SAMPLE_INSTALLATIONS": "1000000", "IQ_PURCHASE_DATE_IS_PROXY": "true"}):
            config = get_config()
        self.assertEqual(config.sample_installations, 1_000_000)
        self.assertTrue(config.purchase_date_is_proxy)
        with patch.dict(os.environ, {"IQ_SAMPLE_INSTALLATIONS": "-1"}):
            with self.assertRaisesRegex(ValueError, "IQ_SAMPLE_INSTALLATIONS"):
                get_config()

    def test_database_sample_displays_scope_and_proxy(self):
        config = replace(
            get_config(), demo_mode=False, sample_installations=1_000_000,
            purchase_date_is_proxy=True,
        )
        with patch("insightquest.page_helpers.st.warning") as warning, \
                patch("insightquest.page_helpers.st.info") as info:
            render_dataset_scope(config)
        self.assertIn("1,000,000", warning.call_args.args[0])
        self.assertIn("selection-biased", warning.call_args.args[0])
        self.assertIn("warranty start date", info.call_args.args[0])

    def test_demo_does_not_display_database_sample_notice(self):
        config = replace(get_config(), demo_mode=True, sample_installations=1_000_000)
        with patch("insightquest.page_helpers.st.warning") as warning:
            render_dataset_scope(config)
        warning.assert_not_called()
