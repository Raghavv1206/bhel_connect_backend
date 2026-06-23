# ASGI configuration for BHEL Connect project.
# Exposes ASGI callable as a module-level variable named 'application'.
# Maps standard HTTP requests to Django and WebSocket requests to Channels routing.

import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'bhel_connect_backend.settings')

# Initialize Django ASGI application early to ensure AppRegistry is populated before importing consumers
django_asgi_app = get_asgi_application()

import marketplace.routing

application = ProtocolTypeRouter({
    # Handle standard HTTP requests
    "http": django_asgi_app,
    
    # Handle WebSocket connections (Chat, Notifications)
    "websocket": AuthMiddlewareStack(
        URLRouter(
            marketplace.routing.websocket_urlpatterns
        )
    ),
})
