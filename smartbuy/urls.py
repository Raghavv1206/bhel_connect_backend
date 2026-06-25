from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CampaignViewSet,
    VendorViewSet,
    RegisterCampaignView,
    JoinWaitlistView,
    SubmitPaymentView,
    CancelRegistrationView,
    CampaignRegistrationsListView,
    CampaignWaitlistListView,
    DownloadReceiptView
)

router = DefaultRouter()
router.register('campaigns', CampaignViewSet, basename='campaign')
router.register('vendors', VendorViewSet, basename='vendor')

urlpatterns = [
    # Router endpoints (Vendors and Campaigns CRUD/actions)
    path('', include(router.urls)),
    
    # Custom campaign registration actions
    path('campaigns/<int:id>/register/', RegisterCampaignView.as_view(), name='campaign_register'),
    path('campaigns/<int:id>/waitlist/', JoinWaitlistView.as_view(), name='campaign_waitlist'),
    path('campaigns/<int:id>/submit-payment/', SubmitPaymentView.as_view(), name='campaign_submit_payment'),
    path('campaigns/<int:id>/cancel-registration/', CancelRegistrationView.as_view(), name='campaign_cancel_registration'),
    path('campaigns/<int:id>/receipt/', DownloadReceiptView.as_view(), name='campaign_download_receipt'),
    
    # Admin reporting lists
    path('campaigns/<int:id>/registrations/', CampaignRegistrationsListView.as_view(), name='campaign_registrations_list'),
    path('campaigns/<int:id>/waitlist-list/', CampaignWaitlistListView.as_view(), name='campaign_waitlist_list'),
]
