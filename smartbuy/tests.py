from datetime import timedelta
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import patch, MagicMock

from django.core.files.uploadedfile import SimpleUploadedFile
from .models import Campaign, Vendor, CampaignRegistration, TokenPayment, PricingTier

User = get_user_model()


class SmartBuyCampaignTests(APITestCase):
    """
    Unit tests for BHEL Connect SmartBuy Campaign & Registration System.
    """
    def setUp(self):
        # Create administrative user
        self.admin_user = User.objects.create_user(
            employee_id="ADM000001",
            name="Admin User",
            email="admin@bhel.in",
            department="IT",
            password="securepassword123",
            is_admin=True
        )
        
        # Create normal employees
        self.buyer1 = User.objects.create_user(
            employee_id="EMP000001",
            name="Buyer One",
            email="buyer1@bhel.in",
            department="Finance",
            password="securepassword123"
        )
        
        self.buyer2 = User.objects.create_user(
            employee_id="EMP000002",
            name="Buyer Two",
            email="buyer2@bhel.in",
            department="HR",
            password="securepassword123"
        )

        # Create active Vendor
        self.vendor = Vendor.objects.create(
            name="Test Vendor Corp",
            contact_person="Sales Rep",
            email="sales@testvendor.com",
            phone="9998887776",
            products_provided="Office supplies and laptops",
            is_active=True
        )

        # Create Campaign
        self.campaign = Campaign.objects.create(
            title="Office Laptop Campaign",
            description="Dell Latitude 5540 with 16GB RAM",
            vendor=self.vendor,
            total_quantity=2, # Set low to test waitlisting easily
            available_quantity=2,
            duration_days=7,
            start_date=timezone.now() - timedelta(days=1), # Started yesterday
            status="active",
            created_by=self.admin_user
        )

        # Create Pricing Tiers
        self.tier1 = PricingTier.objects.create(campaign=self.campaign, min_buyers=1, max_buyers=1, price=50000)
        self.tier2 = PricingTier.objects.create(campaign=self.campaign, min_buyers=2, max_buyers=None, price=45000)

        # URLs
        self.register_url = reverse('campaign_register', kwargs={'id': self.campaign.id})
        self.waitlist_url = reverse('campaign_waitlist', kwargs={'id': self.campaign.id})
        self.cancel_reg_url = reverse('campaign_cancel_registration', kwargs={'id': self.campaign.id})

    def test_campaign_retrieve_price_calculation(self):
        """
        Verify that get_current_price updates as registrations increase.
        """
        # Initially base price is 50,000 (first milestone)
        self.assertEqual(self.campaign.get_current_price(), 50000)
        
        # Add 1 approved buyer
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer1,
            token_amount=5000,
            payment_status='approved'
        )
        # Should stay at 50,000 (tier 1: 1-1 buyer)
        self.assertEqual(self.campaign.get_current_price(), 50000)

        # Add 2nd approved buyer
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer2,
            token_amount=5000,
            payment_status='approved'
        )
        # Price should drop to 45,000 (tier 2: 2+ buyers)
        self.assertEqual(self.campaign.get_current_price(), 45000)

    @patch('smartbuy.cashfree_service.CashfreeService.create_cashfree_order')
    def test_campaign_registration_and_inventory(self, mock_create_order):
        """
        Test that registering via Cashfree order creation decreases campaign available quantity atomically.
        """
        mock_response = MagicMock()
        mock_response.payment_session_id = "mock_session_123"
        mock_create_order.return_value = mock_response

        self.client.force_authenticate(user=self.buyer1)
        url = reverse('campaign-create-order', kwargs={'pk': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.available_quantity, 1)

        # Mark the registration as approved to test duplicate booking prevention
        reg = CampaignRegistration.objects.get(campaign=self.campaign, employee=self.buyer1)
        reg.payment_status = 'approved'
        reg.save()

        # Attempt to register again (should prevent duplicates when already approved)
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_waitlist_flow(self):
        """
        Test that users are correctly added to waitlist when stock is sold out,
        and promoted once a slot opens.
        """
        # Fill campaign slots
        self.campaign.available_quantity = 0
        self.campaign.save()

        # Buyer 1 joins waitlist
        self.client.force_authenticate(user=self.buyer1)
        response = self.client.post(self.waitlist_url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['is_waitlisted'])
        self.assertEqual(response.data['waitlist_position'], 1)

        # Buyer 2 joins waitlist
        self.client.force_authenticate(user=self.buyer2)
        response = self.client.post(self.waitlist_url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['is_waitlisted'])
        self.assertEqual(response.data['waitlist_position'], 2)

    def test_backout_penalties(self):
        """
        Test that cancellation refunds calculate correctly based on campaign active/closed state.
        """
        # Case 1: Active campaign cancellation (50% penalty refund)
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer1,
            token_amount=5000,
            payment_status='approved',
            is_waitlisted=False
        )
        TokenPayment.objects.create(registration=reg, amount=5000, cashfree_order_id="TEST-1", status="approved")

        self.client.force_authenticate(user=self.buyer1)
        response = self.client.post(self.cancel_reg_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(float(response.data['refund_amount']), 2500.0) # 50% of 5000
        self.assertEqual(response.data['refund_status'], 'pending')

        # Case 2: Closed campaign cancellation (0% refund)
        self.campaign.status = 'closed'
        self.campaign.save()

        reg2 = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer2,
            token_amount=5000,
            payment_status='approved',
            is_waitlisted=False
        )
        TokenPayment.objects.create(registration=reg2, amount=5000, cashfree_order_id="TEST-2", status="approved")

        self.client.force_authenticate(user=self.buyer2)
        response = self.client.post(self.cancel_reg_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(float(response.data['refund_amount']), 0.0)
        self.assertEqual(response.data['refund_status'], 'not_applicable')

    def test_waitlist_slot_expiration(self):
        """
        Test that checking campaign details automatically expires any promoted slots
        whose 24-hour window has passed without submitting payment proof, and promotes
        the next waitlisted employee.
        """
        # Set campaign stock to 0
        self.campaign.available_quantity = 0
        self.campaign.save()

        # Buyer 1 has a promoted slot that is expired (promoted 25 hours ago, no payment)
        reg1 = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer1,
            token_amount=5000,
            payment_status='pending',
            is_waitlisted=False,
            slot_expiry_date=timezone.now() - timedelta(hours=1)
        )

        # Buyer 2 is waitlisted at position 1
        reg2 = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer2,
            token_amount=0,
            payment_status='pending',
            is_waitlisted=True,
            waitlist_position=1
        )

        # Retrieve campaign details (triggers check_and_close_campaign -> check_and_expire_slots)
        self.client.force_authenticate(user=self.buyer1)
        detail_url = reverse('campaign-detail', kwargs={'pk': self.campaign.id})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Buyer 1's registration should now be cancelled
        reg1.refresh_from_db()
        self.assertEqual(reg1.payment_status, 'cancelled')
        self.assertIsNotNone(reg1.cancellation_date)

        # Buyer 2's registration should now be promoted
        reg2.refresh_from_db()
        self.assertFalse(reg2.is_waitlisted)
        self.assertIsNotNone(reg2.slot_expiry_date)

    def test_payment_rejection_reclaims_slot_and_promotes(self):
        """
        Test that when an admin rejects a registration's payment deposit proof:
        1. The campaign's available quantity increases by 1.
        2. The next waitlisted user is automatically promoted.
        """
        # Reset campaign stock to 0 and allow capacity for registration
        self.campaign.available_quantity = 0
        self.campaign.total_quantity = 2
        self.campaign.save()

        # Buyer 1 is registered (active slot, pending payment)
        reg1 = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer1,
            token_amount=5000,
            payment_status='pending',
            is_waitlisted=False
        )

        # Buyer 2 is waitlisted
        reg2 = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer2,
            token_amount=0,
            payment_status='pending',
            is_waitlisted=True,
            waitlist_position=1
        )

        # Create TokenPayment for Buyer 1
        payment = TokenPayment.objects.create(
            registration=reg1,
            amount=5000,
            cashfree_order_id="TEST-ORDER-1",
            status="pending"
        )

        # Admin rejects payment
        self.client.force_authenticate(user=self.admin_user)
        reject_url = reverse('admin_reject_payment', kwargs={'payment_id': payment.id})
        response = self.client.post(reject_url, {'rejection_reason': 'Invalid reference ID'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify Buyer 1 is rejected
        reg1.refresh_from_db()
        self.assertEqual(reg1.payment_status, 'rejected')

        # Verify Campaign stock is still 0 (since the reclaimed slot was promoted to Buyer 2)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.available_quantity, 0)

        # Verify Buyer 2 is promoted
        reg2.refresh_from_db()
        self.assertFalse(reg2.is_waitlisted)
        self.assertIsNotNone(reg2.slot_expiry_date)


from unittest.mock import patch, MagicMock

class CashfreePaymentGatewayTests(APITestCase):
    """
    Unit tests for Cashfree Payment Gateway Integration.
    """
    def setUp(self):
        # Create admin and employee users
        self.admin_user = User.objects.create_user(
            employee_id="ADM999999",
            name="Admin User",
            email="admin_test@bhel.in",
            department="IT",
            password="securepassword123",
            is_admin=True
        )
        self.buyer = User.objects.create_user(
            employee_id="EMP999999",
            name="Buyer Test",
            email="buyer_test@bhel.in",
            department="Engineering",
            mobile="9876543210",
            password="securepassword123"
        )
        self.vendor = Vendor.objects.create(
            name="Cashfree Vendor",
            contact_person="Contact Person",
            email="vendor_test@test.com",
            phone="9998887776",
            is_active=True
        )
        self.campaign = Campaign.objects.create(
            title="Cashfree Laptop Campaign",
            description="Campaign description",
            vendor=self.vendor,
            total_quantity=5,
            available_quantity=5,
            duration_days=7,
            start_date=timezone.now() - timedelta(days=1),
            status="active",
            created_by=self.admin_user
        )
        self.tier = PricingTier.objects.create(campaign=self.campaign, min_buyers=1, max_buyers=None, price=10000)

    @patch('smartbuy.cashfree_service.CashfreeService.create_cashfree_order')
    def test_create_order_endpoint(self, mock_create_order):
        # Mock Cashfree order creation response
        mock_response = MagicMock()
        mock_response.payment_session_id = "mock_session_123"
        mock_create_order.return_value = mock_response

        self.client.force_authenticate(user=self.buyer)
        url = reverse('campaign-create-order', kwargs={'pk': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['payment_session_id'], "mock_session_123")
        self.assertTrue(CampaignRegistration.objects.filter(campaign=self.campaign, employee=self.buyer).exists())

    @patch('smartbuy.cashfree_service.CashfreeService.fetch_cashfree_order')
    @patch('cashfree_pg.api_client.Cashfree.PGOrderFetchPayments')
    def test_cashfree_verify_endpoint_success(self, mock_fetch_payments, mock_fetch_order):
        # Setup CampaignRegistration & TokenPayment
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=1000,
            payment_status='pending'
        )
        payment = TokenPayment.objects.create(
            registration=reg,
            amount=1000,
            cashfree_order_id="mock_order_id",
            status="pending"
        )

        # Mock fetch order response
        mock_order = MagicMock()
        mock_order.order_status = "PAID"
        mock_fetch_order.return_value = mock_order

        # Mock fetch payments response
        mock_payment_details = MagicMock()
        mock_payment_item = MagicMock()
        mock_payment_item.cf_payment_id = "mock_cf_payment_id"
        mock_payment_details.data = [mock_payment_item]
        mock_fetch_payments.return_value = mock_payment_details

        self.client.force_authenticate(user=self.buyer)
        url = reverse('cashfree-verify', kwargs={'order_id': 'mock_order_id'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'PAID')

        reg.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(reg.payment_status, 'approved')
        self.assertEqual(payment.status, 'approved')
        self.assertEqual(payment.cashfree_payment_id, "mock_cf_payment_id")

    @patch('smartbuy.cashfree_service.CashfreeService.verify_webhook_signature')
    @patch('smartbuy.cashfree_service.CashfreeService.fetch_cashfree_order')
    def test_cashfree_webhook_success(self, mock_fetch_order, mock_verify_signature):
        # Setup CampaignRegistration & TokenPayment
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=1000,
            payment_status='pending'
        )
        payment = TokenPayment.objects.create(
            registration=reg,
            amount=1000,
            cashfree_order_id="mock_order_id",
            status="pending"
        )

        mock_verify_signature.return_value = True

        mock_order = MagicMock()
        mock_order.order_status = "PAID"
        mock_fetch_order.return_value = mock_order

        webhook_payload = {
            "type": "PAYMENT_SUCCESS_WEBHOOK",
            "data": {
                "order": {"order_id": "mock_order_id"},
                "payment": {"cf_payment_id": "mock_cf_payment_id", "payment_status": "SUCCESS"}
            }
        }

        url = reverse('cashfree-webhook')
        response = self.client.post(
            url,
            data=webhook_payload,
            format='json',
            HTTP_X_WEBHOOK_SIGNATURE="mock_signature",
            HTTP_X_WEBHOOK_TIMESTAMP="mock_timestamp"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        reg.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(reg.payment_status, 'approved')
        self.assertEqual(payment.status, 'approved')
        self.assertEqual(payment.cashfree_payment_id, "mock_cf_payment_id")

    def test_create_order_waitlisted_user(self):
        """
        Verify waitlisted users are blocked from creating payment orders.
        """
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=0,
            payment_status='pending',
            is_waitlisted=True,
            waitlist_position=1
        )
        self.client.force_authenticate(user=self.buyer)
        url = reverse('campaign-create-order', kwargs={'pk': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("waitlist", response.data['detail'].lower())

    @patch('smartbuy.cashfree_service.CashfreeService.create_cashfree_order')
    def test_create_order_cancelled_registration(self, mock_create_order):
        """
        Verify that re-registering after cancellation deletes the old registration
        and allocates a fresh slot correctly.
        """
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=1000,
            payment_status='cancelled'
        )
        self.assertEqual(self.campaign.available_quantity, 5)

        # Mock Cashfree order creation response
        mock_response = MagicMock()
        mock_response.payment_session_id = "mock_session_456"
        mock_create_order.return_value = mock_response

        self.client.force_authenticate(user=self.buyer)
        url = reverse('campaign-create-order', kwargs={'pk': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['payment_session_id'], "mock_session_456")

        self.campaign.refresh_from_db()
        # Campaign quantity should be decremented because a new slot is allocated
        self.assertEqual(self.campaign.available_quantity, 4)

        # Confirm new registration exists with pending status
        reg = CampaignRegistration.objects.get(campaign=self.campaign, employee=self.buyer)
        self.assertEqual(reg.payment_status, 'pending')

    @patch('smartbuy.cashfree_service.CashfreeService.verify_webhook_signature')
    def test_cashfree_webhook_duplicate_ignore(self, mock_verify_signature):
        """
        Verify that duplicate webhook processing is safely ignored for rejected payments/cancelled registrations.
        """
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=1000,
            payment_status='cancelled'
        )
        payment = TokenPayment.objects.create(
            registration=reg,
            amount=1000,
            cashfree_order_id="mock_order_id_2",
            status="rejected"
        )
        self.campaign.available_quantity = 5
        self.campaign.save()

        mock_verify_signature.return_value = True

        webhook_payload = {
            "type": "PAYMENT_FAILED_WEBHOOK",
            "data": {
                "order": {"order_id": "mock_order_id_2"},
                "payment": {"cf_payment_id": "mock_cf_payment_id_2", "payment_status": "FAILED"}
            }
        }

        url = reverse('cashfree-webhook')
        response = self.client.post(
            url,
            data=webhook_payload,
            format='json',
            HTTP_X_WEBHOOK_SIGNATURE="mock_signature",
            HTTP_X_WEBHOOK_TIMESTAMP="mock_timestamp"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.campaign.refresh_from_db()
        # Inventory quantity should NOT be incremented again
        self.assertEqual(self.campaign.available_quantity, 5)


from django.core import mail

from rest_framework.test import APITransactionTestCase
from django.conf import settings

class SmartBuyEmailNotificationTests(APITransactionTestCase):
    """
    Unit tests for automated SmartBuy transaction-proof confirmation emails.
    """
    def setUp(self):
        settings.TESTING = True
        # Create users
        self.admin_user = User.objects.create_user(
            employee_id="ADM888888",
            name="Admin User",
            email="admin_mail@bhel.in",
            department="IT",
            password="securepassword123",
            is_admin=True
        )
        self.buyer = User.objects.create_user(
            employee_id="EMP888888",
            name="Buyer Mail",
            email="buyer_mail@bhel.in",
            department="Engineering",
            mobile="9876543210",
            password="securepassword123"
        )
        self.vendor = Vendor.objects.create(
            name="Mail Vendor",
            contact_person="Person",
            email="vendor_mail@test.com",
            phone="9998887776",
            is_active=True
        )
        self.campaign = Campaign.objects.create(
            title="Mail Laptop Campaign",
            description="Campaign description",
            vendor=self.vendor,
            total_quantity=2,
            available_quantity=2,
            duration_days=7,
            start_date=timezone.now() - timedelta(days=1),
            status="active",
            created_by=self.admin_user
        )
        self.tier1 = PricingTier.objects.create(campaign=self.campaign, min_buyers=1, max_buyers=1, price=50000)
        self.tier2 = PricingTier.objects.create(campaign=self.campaign, min_buyers=2, max_buyers=None, price=45000)

        # Clear outbox before each test
        mail.outbox = []

    @patch('smartbuy.cashfree_service.CashfreeService.create_cashfree_order')
    def test_checkout_initiates_email(self, mock_create_order):
        mock_response = MagicMock()
        mock_response.payment_session_id = "mock_session_456"
        mock_create_order.return_value = mock_response

        self.client.force_authenticate(user=self.buyer)
        url = reverse('campaign-create-order', kwargs={'pk': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # 1 email for order initiated
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Registration Initiated", email.subject)
        self.assertEqual(email.to, [self.buyer.email])
        self.assertIn("4500.00", email.body)  # 10% of 45000 (lowest pricing tier: 45000)

    @patch('smartbuy.cashfree_service.CashfreeService.fetch_cashfree_order')
    @patch('cashfree_pg.api_client.Cashfree.PGOrderFetchPayments')
    def test_payment_success_sends_email(self, mock_fetch_payments, mock_fetch_order):
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=4500,
            payment_status='pending'
        )
        payment = TokenPayment.objects.create(
            registration=reg,
            amount=4500,
            cashfree_order_id="mock_order_id_email",
            status="pending"
        )

        mock_order = MagicMock()
        mock_order.order_status = "PAID"
        mock_fetch_order.return_value = mock_order

        mock_payment_details = MagicMock()
        mock_payment_item = MagicMock()
        mock_payment_item.cf_payment_id = "mock_cf_payment_id_email"
        mock_payment_details.data = [mock_payment_item]
        mock_fetch_payments.return_value = mock_payment_details

        self.client.force_authenticate(user=self.buyer)
        url = reverse('cashfree-verify', kwargs={'order_id': 'mock_order_id_email'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check email
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Booking Confirmed", email.subject)
        self.assertEqual(email.to, [self.buyer.email])
        self.assertIn("mock_cf_payment_id_email", email.body)

    def test_waitlist_joins_sends_email(self):
        # Sell out campaign
        self.campaign.available_quantity = 0
        self.campaign.save()

        self.client.force_authenticate(user=self.buyer)
        url = reverse('campaign_waitlist', kwargs={'id': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check email
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Joined Waitlist", email.subject)
        self.assertEqual(email.to, [self.buyer.email])
        self.assertIn("Queue Position: #1", email.body)

    def test_cancellation_sends_email(self):
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=4500,
            payment_status='approved',
            is_waitlisted=False
        )
        TokenPayment.objects.create(registration=reg, amount=4500, cashfree_order_id="mock_order_id_cancel", status="approved")

        self.client.force_authenticate(user=self.buyer)
        url = reverse('campaign_cancel_registration', kwargs={'id': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check email
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Registration Cancelled", email.subject)
        self.assertEqual(email.to, [self.buyer.email])
        self.assertIn("2250.00", email.body) # 50% refund

    def test_promotion_from_waitlist_sends_email(self):
        # 1. Buyer is waitlisted
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=0,
            payment_status='pending',
            is_waitlisted=True,
            waitlist_position=1
        )
        self.campaign.available_quantity = 0
        self.campaign.save()

        # Promote
        from .utils import promote_from_waitlist
        promote_from_waitlist(self.campaign.id)

        # Check email
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Waitlist Promotion", email.subject)
        self.assertEqual(email.to, [self.buyer.email])
        self.assertIn("within 24 hours", email.body)

    def test_campaign_closed_sends_emails(self):
        # Register and approve buyer
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=4500,
            payment_status='approved',
            is_waitlisted=False
        )

        # Admin closes campaign early
        self.client.force_authenticate(user=self.admin_user)
        url = reverse('campaign-close-campaign', kwargs={'pk': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check email
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Closed Successfully", email.subject)
        self.assertEqual(email.to, [self.buyer.email])
        self.assertIn("50000.00", email.body) # final price

    def test_campaign_cancelled_sends_emails(self):
        # Register and approve buyer
        reg = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.buyer,
            token_amount=4500,
            payment_status='approved',
            is_waitlisted=False
        )

        # Admin cancels campaign
        self.client.force_authenticate(user=self.admin_user)
        url = reverse('campaign-cancel-campaign', kwargs={'pk': self.campaign.id})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check email
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertIn("Cancelled", email.subject)
        self.assertEqual(email.to, [self.buyer.email])
        self.assertIn("full refund", email.body)
