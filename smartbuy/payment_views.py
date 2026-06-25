import json
import logging
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .models import CampaignRegistration, TokenPayment
from .cashfree_service import CashfreeService
from notifications.utils import create_notification

logger = logging.getLogger(__name__)


class CashfreeVerifyView(APIView):
    """
    GET: api/payments/cashfree-verify/<str:order_id>/
    Verifies payment status of a Cashfree order by checking directly with the gateway.
    Fulfills the reservation if status is PAID.
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        # Bypass DRF format content negotiation to avoid 404 for format query params
        renderers = self.get_renderers()
        return (renderers[0], renderers[0].media_type)

    def get(self, request, order_id):
        # Enforce that the verification is wrapped in a transaction and locks the payment row
        with transaction.atomic():  # type: ignore
            # Lock the TokenPayment row to prevent concurrent manual verification / webhook updates
            token_payment = (
                TokenPayment.objects
                .select_for_update()
                .select_related('registration', 'registration__campaign', 'registration__employee')
                .filter(cashfree_order_id=order_id)
                .first()
            )

            if not token_payment:
                return Response(
                    {"detail": "Transaction not found."},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Enforce that only the recipient employee or admin can verify their own payment
            if token_payment.registration.employee != request.user and not request.user.is_admin:
                return Response(
                    {"detail": "You do not have permission to verify this transaction."},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Skip API call if already approved
            if token_payment.status == 'approved':
                return Response({
                    "status": "PAID",
                    "detail": "Payment already verified and approved.",
                    "campaign_id": token_payment.registration.campaign.id
                }, status=status.HTTP_200_OK)

            # Skip API call and return status if already rejected/cancelled
            if token_payment.status == 'rejected' or token_payment.registration.payment_status == 'cancelled':
                return Response({
                    "status": "FAILED",
                    "detail": "This transaction has been cancelled or rejected.",
                    "campaign_id": token_payment.registration.campaign.id
                }, status=status.HTTP_200_OK)

            cf = CashfreeService()
            try:
                cf_order = cf.fetch_cashfree_order(order_id)
                cf_status = cf_order.order_status  # PAID, ACTIVE, EXPIRED, etc.
                
                if cf_status == "PAID":
                    token_payment.status = 'approved'
                    # Retrieve the actual transaction payment ID from Cashfree order details
                    payments_data = cf.client.PGOrderFetchPayments(order_id, None, None)
                    if payments_data.data and len(payments_data.data) > 0:
                        token_payment.cashfree_payment_id = payments_data.data[0].cf_payment_id
                    token_payment.save()

                    reg = token_payment.registration
                    reg.payment_status = 'approved'
                    reg.save()

                    # Notify user and email only AFTER successful transaction commit
                    transaction.on_commit(lambda: create_notification(
                        recipient=reg.employee,
                        title="Payment Confirmed",
                        message=f"Payment confirmed for campaign '{reg.campaign.title}'. Slot reserved!",
                        notification_type="payment",
                        link=f"/smartbuy/{reg.campaign.id}"
                    ))

                    from .emails import send_payment_confirmed_email
                    transaction.on_commit(lambda: send_payment_confirmed_email(reg))

                    return Response({
                        "status": "PAID",
                        "detail": "Payment verified and approved successfully.",
                        "campaign_id": reg.campaign.id
                    }, status=status.HTTP_200_OK)
                else:
                    return Response({
                        "status": cf_status,
                        "detail": f"Payment is in status: {cf_status}",
                        "campaign_id": token_payment.registration.campaign.id
                    }, status=status.HTTP_200_OK)

            except Exception as e:
                logger.error(f"Failed to fetch order status from Cashfree for {order_id}: {e}", exc_info=True)
                return Response(
                    {"detail": f"Error communicating with Cashfree Payment Gateway: {str(e)}", "campaign_id": token_payment.registration.campaign.id},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )


class CashfreeWebhookView(APIView):
    """
    POST: api/payments/cashfree-webhook/
    Listen to Cashfree real-time webhook events.
    Verifies x-webhook-signature and x-webhook-timestamp before processing.
    """
    permission_classes = []  # Public endpoint

    def post(self, request):
        # Extract webhook signature headers
        signature = request.headers.get('x-webhook-signature')
        timestamp = request.headers.get('x-webhook-timestamp')
        
        if not signature or not timestamp:
            logger.warning("Rejecting unsigned Cashfree webhook request")
            return Response({"detail": "Missing signature headers"}, status=status.HTTP_403_FORBIDDEN)

        # Get raw payload bytes
        raw_body = request.body.decode('utf-8')

        # Verify Signature
        cf = CashfreeService()
        if not cf.verify_webhook_signature(signature, raw_body, timestamp):
            logger.warning("Rejecting Cashfree webhook request due to invalid signature")
            return Response({"detail": "Invalid signature"}, status=status.HTTP_403_FORBIDDEN)

        # Parse payload
        try:
            payload = json.loads(raw_body)
        except Exception as e:
            return Response({"detail": "Malformed JSON payload"}, status=status.HTTP_400_BAD_REQUEST)

        event_type = payload.get('type')
        event_data = payload.get('data', {})
        
        # Extract order details
        order_info = event_data.get('order', {})
        order_id = order_info.get('order_id')
        
        if not order_id:
            return Response({"detail": "Missing order_id in webhook data"}, status=status.HTTP_400_BAD_REQUEST)

        # Implement Webhook Idempotency: Locate payment record
        try:
            token_payment = TokenPayment.objects.select_related('registration', 'registration__campaign', 'registration__employee').get(cashfree_order_id=order_id)
        except TokenPayment.DoesNotExist:
            logger.warning(f"Cashfree Webhook received for unregistered order ID: {order_id}")
            return Response({"detail": "Order not found"}, status=status.HTTP_200_OK)  # Acknowledge 200 to stop retries

        # Skip processing if already verified and processed (approved or rejected) or if registration is cancelled
        if token_payment.status in ['approved', 'rejected'] or token_payment.registration.payment_status == 'cancelled':
            return Response({"detail": "Webhook ignored, payment already processed or cancelled"}, status=status.HTTP_200_OK)

        # Process payment events
        payment_info = event_data.get('payment', {})
        payment_status = payment_info.get('payment_status')
        cf_payment_id = payment_info.get('cf_payment_id')

        if event_type == 'PAYMENT_SUCCESS_WEBHOOK':
            # Perform authoritative double-verification from backend server
            try:
                cf_order = cf.fetch_cashfree_order(order_id)
                if cf_order.order_status == 'PAID':
                    with transaction.atomic():  # type: ignore
                        # Lock payment and registration rows to prevent concurrent webhook processing
                        token_payment = TokenPayment.objects.select_for_update().get(id=token_payment.id)
                        # Re-check idempotency inside the lock
                        if token_payment.status in ['approved', 'rejected']:
                            return Response({"detail": "Payment already processed"}, status=status.HTTP_200_OK)

                        # Update payment status
                        token_payment.status = 'approved'
                        token_payment.cashfree_payment_id = cf_payment_id
                        token_payment.save()

                        # Update registration
                        reg = CampaignRegistration.objects.select_for_update().get(id=token_payment.registration_id)
                        reg.payment_status = 'approved'
                        reg.save()

                    # Notify employee
                    create_notification(
                        recipient=reg.employee,
                        title="Token Payment Confirmed",
                        message=f"Payment confirmed for campaign '{reg.campaign.title}'. Slot reserved!",
                        notification_type="payment",
                        link=f"/smartbuy/{reg.campaign.id}"
                    )

                    from .emails import send_payment_confirmed_email
                    send_payment_confirmed_email(reg)
            except Exception as e:
                logger.error(f"Error executing authoritative fetch during success webhook: {e}", exc_info=True)
                return Response({"detail": "Fulfillment verification failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        elif event_type == 'PAYMENT_FAILED_WEBHOOK':
            # Handle payment failure with proper row locking
            with transaction.atomic():  # type: ignore
                token_payment = TokenPayment.objects.select_for_update().get(id=token_payment.id)
                # Re-check idempotency inside the lock
                if token_payment.status in ['approved', 'rejected']:
                    return Response({"detail": "Payment already processed"}, status=status.HTTP_200_OK)

                token_payment.status = 'rejected'
                token_payment.cashfree_payment_id = cf_payment_id
                token_payment.save()

                reg = CampaignRegistration.objects.select_for_update().get(id=token_payment.registration_id)
                reg.payment_status = 'cancelled'
                reg.save()

                # Reclaim campaign inventory slot with select_for_update to prevent race conditions
                from smartbuy.models import Campaign
                campaign = Campaign.objects.select_for_update().get(id=reg.campaign_id)
                campaign.available_quantity += 1
                campaign.save()

            # Notify employee about failure (outside transaction)
            create_notification(
                recipient=reg.employee,
                title="Payment Transaction Failed",
                message=f"Token payment for campaign '{campaign.title}' failed. Your reservation slot has been released.",
                notification_type="payment",
                link=f"/smartbuy/{campaign.id}"
            )

            from .emails import send_payment_failed_email
            send_payment_failed_email(reg)

        return Response({"status": "SUCCESS"}, status=status.HTTP_200_OK)
