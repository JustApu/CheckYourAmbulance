from django.apps import AppConfig
from django.db.backends.signals import connection_created


class CoreConfig(AppConfig):
    name = "cya.core"
    label = "cya_core"

    def ready(self):
        from cya.core.db import verify_session_settings

        connection_created.connect(verify_session_settings, dispatch_uid="cya_verify_session")
