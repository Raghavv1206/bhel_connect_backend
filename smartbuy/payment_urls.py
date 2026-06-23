from django.urls import path
from .payment_views import CashfreeVerifyView, CashfreeWebhookView

urlpatterns = [
    # GET: Verifies a Cashfree payment order status directly with Cashfree
    path("cashfree-verify/<str:order_id>/", CashfreeVerifyView.as_view(), name="cashfree-verify"),
    
    # POST: Listen to Cashfree real-time payment webhooks
    path("cashfree-webhook/", CashfreeWebhookView.as_view(), name="cashfree-webhook"),
]
