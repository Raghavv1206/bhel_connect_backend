from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # Route matching ws/chat/<listing_id>/
    # Supports passing token and other_user_id query params
    re_path(r'^ws/chat/(?P<listing_id>\d+)/$', consumers.ChatConsumer.as_asgi()),
    # Route matching ws/listings/<listing_id>/
    # Supports passing token query param
    re_path(r'^ws/listings/(?P<listing_id>\d+)/$', consumers.ListingUpdatesConsumer.as_asgi()),
]
