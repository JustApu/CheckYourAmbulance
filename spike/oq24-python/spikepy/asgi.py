"""THROWAWAY spike: ASGI application with a WebSocket consumer (Django Channels).

DELIBERATELY UNAUTHENTICATED AND UNSAFE FOR PRODUCTION. This consumer exists only to prove that group
push works. It accepts any client into any vehicle group. The real system must authenticate the
connection and authorize the user for that vehicle before joining a group (SPEC section 17.7, rule 7).
"""
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from django.urls import re_path


class LiveConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group = f"vehicle-{self.scope['url_route']['kwargs']['v']}"
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        await self.channel_layer.group_discard(self.group, self.channel_name)

    async def check_recorded(self, event):
        await self.send(text_data=event["text"])


application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": URLRouter([re_path(r"^vehicles/(?P<v>\d+)/live$", LiveConsumer.as_asgi())]),
})
