"""Render the real crew seal page with synthetic configuration."""

from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class SealPageTemplateTests(SimpleTestCase):
    def test_page_renders_the_workflow_and_escapes_configuration(self):
        html = render_to_string(
            "seals/vehicle.html",
            {
                "vehicle": SimpleNamespace(
                    pk="00000000-0000-4000-8000-000000000001", display_name="Sample vehicle"
                ),
                "config": {"socket_url": "/socket", "value": "</script><script>unsafe</script>"},
            },
        )
        self.assertIn("Sealed kits and cabinets", html)
        self.assertIn('id="seal-subjects"', html)
        self.assertIn('id="seal-form"', html)
        self.assertIn('id="seal-config" type="application/json"', html)
        self.assertIn("/socket", html)
        self.assertNotIn("</script><script>unsafe</script>", html)
