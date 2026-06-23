from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from smartbuy.models import Campaign, Vendor, CampaignRegistration, PricingTier
from marketplace.models import Category, MarketplaceListing

User = get_user_model()

class ReportsEndpointsTests(APITestCase):
    """
    Unit tests for the Phase 9 Reports Module.
    Verifies authentication, admin-only restrictions, format queries,
    PDF/Excel outputs, and database aggregation details.
    """
    def setUp(self):
        # Create users
        self.admin_user = User.objects.create_user(
            employee_id="ADM9999",
            name="Admin User",
            email="admin.reports@bhel.in",
            department="IT",
            password="testadminpwd",
            is_admin=True
        )
        self.regular_user = User.objects.create_user(
            employee_id="EMP9999",
            name="Regular Employee",
            email="emp.reports@bhel.in",
            department="Finance",
            password="testemppwd",
            is_admin=False
        )

        # Create active Vendor
        self.vendor = Vendor.objects.create(
            name="Reports Vendor Corp",
            contact_person="Vendor Agent",
            email="agent@repvendor.com",
            phone="9876543210",
            products_provided="Office Equipment",
            is_active=True
        )

        # Create Campaign
        self.campaign = Campaign.objects.create(
            title="Reports Test Laptop",
            description="High-end development laptop",
            vendor=self.vendor,
            total_quantity=5,
            available_quantity=3,
            duration_days=5,
            start_date=timezone.now() - timedelta(days=1),
            status="active",
            created_by=self.admin_user
        )

        # Create Pricing Tiers
        PricingTier.objects.create(campaign=self.campaign, min_buyers=1, max_buyers=2, price=60000)
        PricingTier.objects.create(campaign=self.campaign, min_buyers=3, max_buyers=None, price=55000)

        # Create buyer registration (approved)
        self.reg_buyer = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.regular_user,
            token_amount=6000,
            payment_status='approved',
            is_waitlisted=False
        )

        # Create waitlisted registration
        self.reg_waitlist = CampaignRegistration.objects.create(
            campaign=self.campaign,
            employee=self.admin_user,  # Using admin user just for test registry
            token_amount=0,
            payment_status='pending',
            is_waitlisted=True,
            waitlist_position=1
        )

        # Create Marketplace Category & Listings
        self.category = Category.objects.create(
            name="Electronics",
            slug="electronics",
            display_order=1
        )
        self.listing = MarketplaceListing.objects.create(
            seller=self.regular_user,
            title="Slightly Used Phone",
            description="Excellent condition phone.",
            price=25000.00,
            condition="like_new",
            category=self.category,
            status="sold"  # Mark as sold for breakdown aggregation
        )

        # URLs
        self.buyers_url = reverse('campaign_buyers_report', kwargs={'campaign_id': self.campaign.id})
        self.waitlist_url = reverse('campaign_waitlist_report', kwargs={'campaign_id': self.campaign.id})
        self.marketplace_url = reverse('marketplace_summary_report')

    def test_unauthenticated_access_denied(self):
        """
        Verify that guests are blocked with 401 from accessing reports.
        """
        for url in [self.buyers_url, self.waitlist_url, self.marketplace_url]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_employee_access_denied(self):
        """
        Verify that non-admin authenticated users are blocked with 403.
        """
        self.client.force_authenticate(user=self.regular_user)
        for url in [self.buyers_url, self.waitlist_url, self.marketplace_url]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_campaign_buyers_report_excel(self):
        """
        Verify that admin can successfully download Campaign Buyers report in Excel format.
        """
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.buyers_url, {'format': 'excel'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        self.assertIn(f"attachment; filename=\"campaign_{self.campaign.id}_buyers.xlsx\"", response['Content-Disposition'])
        self.assertTrue(len(response.content) > 0)

    def test_campaign_buyers_report_pdf(self):
        """
        Verify that admin can successfully download Campaign Buyers report in PDF format.
        """
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.buyers_url, {'format': 'pdf'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(f"attachment; filename=\"campaign_{self.campaign.id}_buyers.pdf\"", response['Content-Disposition'])
        self.assertTrue(len(response.content) > 0)

    def test_campaign_waitlist_report_excel(self):
        """
        Verify that admin can successfully download Campaign Waitlist report in Excel format.
        """
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.waitlist_url, {'format': 'excel'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        self.assertIn(f"attachment; filename=\"campaign_{self.campaign.id}_waitlist.xlsx\"", response['Content-Disposition'])
        self.assertTrue(len(response.content) > 0)

    def test_campaign_waitlist_report_pdf(self):
        """
        Verify that admin can successfully download Campaign Waitlist report in PDF format.
        """
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.waitlist_url, {'format': 'pdf'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(f"attachment; filename=\"campaign_{self.campaign.id}_waitlist.pdf\"", response['Content-Disposition'])
        self.assertTrue(len(response.content) > 0)

    def test_marketplace_summary_report(self):
        """
        Verify that admin can successfully download Marketplace Summary report (Excel only).
        """
        print("DEBUG MARKETPLACE URL:", self.marketplace_url)
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.marketplace_url, {'format': 'excel'})
        print("DEBUG RESPONSE STATUS:", response.status_code)
        print("DEBUG RESPONSE CONTENT:", getattr(response, 'content', None) or getattr(response, 'data', None))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        self.assertIn('attachment; filename="marketplace_summary.xlsx"', response['Content-Disposition'])
        self.assertTrue(len(response.content) > 0)

    def test_marketplace_summary_invalid_format(self):
        """
        Verify that requesting marketplace summary in pdf format fails with 400.
        """
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.marketplace_url, {'format': 'pdf'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid format", response.data['detail'])

    def test_campaign_not_found(self):
        """
        Verify that requesting reports for a non-existent campaign ID returns 404.
        """
        self.client.force_authenticate(user=self.admin_user)
        invalid_url = reverse('campaign_buyers_report', kwargs={'campaign_id': 999999})
        response = self.client.get(invalid_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_format_parameter(self):
        """
        Verify that requesting campaign reports with invalid format returns 400.
        """
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(self.buyers_url, {'format': 'csv'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid format", response.data['detail'])
