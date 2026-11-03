"""The Ubuntu host profile reuses the Rocky Linux 9 nginx configuration (issue #6).

deploy/ubuntu2604/nginx.conf is the deploy/rocky9 file with only the worker user and the
dynamic-modules path changed for Ubuntu's package, so a change to one must reach the other.
"""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

DEPLOY = Path(settings.BASE_DIR) / "deploy"
# The only directives that differ: Rocky's value, then Ubuntu's.
UBUNTU_CHANGES = {
    "user nginx;": "user www-data;",
    "include /usr/share/nginx/modules/*.conf;": "include /etc/nginx/modules-enabled/*.conf;",
}


def directives(path):
    """The configuration lines without comments, as Ubuntu would receive them."""
    lines = []
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(UBUNTU_CHANGES.get(line, line))
    return lines


class UbuntuNginxProfileTests(SimpleTestCase):
    def test_ubuntu_nginx_conf_is_the_rocky_file_with_only_the_ubuntu_lines_changed(self):
        rocky = directives(DEPLOY / "rocky9" / "nginx.conf")
        ubuntu = directives(DEPLOY / "ubuntu2604" / "nginx.conf")
        self.assertEqual(ubuntu, rocky)

    def test_each_ubuntu_change_applies_to_exactly_one_rocky_line(self):
        rocky = (DEPLOY / "rocky9" / "nginx.conf").read_text().splitlines()
        ubuntu = (DEPLOY / "ubuntu2604" / "nginx.conf").read_text().splitlines()
        for old, new in UBUNTU_CHANGES.items():
            with self.subTest(old):
                self.assertEqual(rocky.count(old), 1)
                self.assertEqual(ubuntu.count(new), 1)
                self.assertNotIn(old, ubuntu)
