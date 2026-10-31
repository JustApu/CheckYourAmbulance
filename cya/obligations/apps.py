from django.apps import AppConfig


class ObligationsConfig(AppConfig):
    name = "cya.obligations"
    label = "cya_obligations"

    def ready(self):
        # Connects the receiver that reconciles pre-window obligations after a publication.
        from cya.obligations import signals  # noqa: F401
