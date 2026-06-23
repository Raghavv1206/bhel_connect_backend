import os
import logging
from django.conf import settings
from cashfree_pg.api_client import Cashfree
from cashfree_pg.models.create_order_request import CreateOrderRequest
from cashfree_pg.models.customer_details import CustomerDetails
from cashfree_pg.models.order_meta import OrderMeta
from cashfree_pg.models.order_create_refund_request import OrderCreateRefundRequest

logger = logging.getLogger(__name__)


class CashfreeService:
    """
    Service class encapsulating interaction with the Cashfree Payment Gateway SDK (v6+).
    Encapsulates Sandbox vs Production configuration, Order creation, Verification, and Refunds.
    """

    def __init__(self):
        # Read credentials from environment variables
        app_id = os.environ.get('CASHFREE_APP_ID')
        secret_key = os.environ.get('CASHFREE_SECRET_KEY')
        env_str = os.environ.get('CASHFREE_ENV', 'TEST').upper()

        if not app_id or not secret_key:
            import sys
            # Allow tests to proceed with dummy credentials if not present in the environment
            if 'test' in sys.argv:
                app_id = app_id or 'dummy_app_id'
                secret_key = secret_key or 'dummy_secret_key'
            else:
                from django.core.exceptions import ImproperlyConfigured
                raise ImproperlyConfigured(
                    "CASHFREE_APP_ID and CASHFREE_SECRET_KEY environment variables must be configured."
                )

        # Route environment constants accordingly
        if env_str in ('PROD', 'PRODUCTION'):
            env = Cashfree.PRODUCTION
        else:
            env = Cashfree.SANDBOX

        # Initialize the global SDK client
        self.client = Cashfree(
            XEnvironment=env,
            XClientId=app_id,
            XClientSecret=secret_key
        )

    def create_cashfree_order(self, order_id, amount, customer_id, customer_phone, customer_email, customer_name, return_url, notify_url=None):
        """
        Creates a new payment order with Cashfree.
        Returns the OrderEntity response data (containing payment_session_id and status).
        """
        request_obj = CreateOrderRequest(
            order_id=str(order_id),
            order_amount=float(amount),
            order_currency="INR",
            customer_details=CustomerDetails(
                customer_id=str(customer_id),
                # Cashfree requires customer_phone to be at least 10 digits
                customer_phone=str(customer_phone) if customer_phone else "9999999999",
                customer_email=str(customer_email),
                customer_name=str(customer_name)
            ),
            order_meta=OrderMeta(
                return_url=str(return_url),
                notify_url=str(notify_url) if notify_url else None
            )
        )
        
        # PGCreateOrder positional signature: request_body, x_request_id, x_idempotency_key
        response = self.client.PGCreateOrder(request_obj, None, None)
        return response.data

    def fetch_cashfree_order(self, order_id):
        """
        Retrieves the details and status of an existing Cashfree order.
        Authoritative source of payment truth.
        """
        response = self.client.PGFetchOrder(str(order_id), None, None)
        return response.data

    def create_cashfree_refund(self, order_id, refund_amount, refund_id, refund_note="SmartBuy Reservation Refund"):
        """
        Initiates a refund for a paid order.
        """
        request_obj = OrderCreateRefundRequest(
            refund_amount=float(refund_amount),
            refund_id=str(refund_id),
            refund_note=str(refund_note),
            refund_speed="STANDARD"
        )
        response = self.client.PGOrderCreateRefund(str(order_id), request_obj, None, None)
        return response.data

    def verify_webhook_signature(self, signature, raw_body, timestamp):
        """
        Verifies the authenticity of an incoming Cashfree webhook signature (HMAC-SHA256).
        Returns True if signature is valid, False otherwise.
        """
        try:
            self.client.PGVerifyWebhookSignature(signature, raw_body, timestamp)
            return True
        except Exception as e:
            logger.error(f"Cashfree webhook signature verification failed: {e}", exc_info=True)
            return False
