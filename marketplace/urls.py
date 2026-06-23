from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CategoryViewSet, 
    MarketplaceListingViewSet,
    ChatConversationsView,
    ChatMessageHistoryView
)

router = DefaultRouter()
router.register('listings', MarketplaceListingViewSet, basename='listing')
router.register('categories', CategoryViewSet, basename='category')

urlpatterns = [
    path('', include(router.urls)),
    path('chats/', ChatConversationsView.as_view(), name='chat-conversations'),
    path('chats/<int:listing_id>/messages/', ChatMessageHistoryView.as_view(), name='chat-message-history'),
]
