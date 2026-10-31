from django.urls import path

from cya.live_updates.consumer import VehicleLiveConsumer

websocket_urlpatterns = [
    path(
        "ws/v1/vehicles/<uuid:vehicle_id>/live/",
        VehicleLiveConsumer.as_asgi(),
        name="vehicle-live-updates",
    ),
]
