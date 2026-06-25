from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch
from .models import Category, MarketplaceListing, ListingImage, VehicleListing, PropertyListing, ChatMessage

User = get_user_model()

class MarketplaceTestCase(APITestCase):
    """
    Test suite for BHEL Connect Marketplace (Phase 6).
    """

    def setUp(self):
        # Create normal employees
        self.seller = User.objects.create_user(
            employee_id="E12345",
            name="Seller Employee",
            email="seller@bhel.in",
            department="IT",
            password="password123"
        )
        self.buyer = User.objects.create_user(
            employee_id="E67890",
            name="Buyer Employee",
            email="buyer@bhel.in",
            department="HR",
            password="password123"
        )
        
        # Create admin employee
        self.admin = User.objects.create_user(
            employee_id="E00001",
            name="Admin User",
            email="admin@bhel.in",
            department="Management",
            password="password123",
            is_admin=True
        )

        # Create Category hierarchy
        self.vehicles_cat = Category.objects.create(
            name="Vehicles",
            slug="vehicles",
            display_order=1
        )
        self.cars_cat = Category.objects.create(
            name="Cars",
            slug="cars",
            parent=self.vehicles_cat,
            display_order=1
        )
        self.electronics_cat = Category.objects.create(
            name="Electronics",
            slug="electronics",
            display_order=2
        )

        # Create some listings
        self.active_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="Sleek Sedan Car",
            description="Excellent sedan in pristine condition.",
            price=350000.00,
            condition="like_new",
            category=self.cars_cat,
            status="available"
        )
        self.vehicle_details = VehicleListing.objects.create(
            listing=self.active_listing,
            brand="Honda",
            model="City",
            year=2019,
            km_driven=45000,
            fuel_type="petrol",
            transmission="manual"
        )

        self.pending_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="iPhone 14 Pro",
            description="Pending approval by admin.",
            price=60000.00,
            condition="good",
            category=self.electronics_cat,
            status="pending"
        )

    def test_category_listing(self):
        """
        Verify categories can be fetched successfully by authenticated users.
        """
        self.client.force_authenticate(user=self.buyer)
        url = reverse('category-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return both parent and children categories
        self.assertTrue(len(response.data) >= 3)

    def test_listing_list_excludes_pending_for_buyer(self):
        """
        Buyers should only see available listings.
        """
        self.client.force_authenticate(user=self.buyer)
        url = reverse('listing-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Buyer should see the active listing but NOT the pending one
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertIn(self.active_listing.id, listing_ids)
        self.assertNotIn(self.pending_listing.id, listing_ids)

    def test_listing_list_excludes_own_pending_listings(self):
        """
        Sellers should NOT see their own pending listings in the main browse list.
        """
        self.client.force_authenticate(user=self.seller)
        url = reverse('listing-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Seller should see the active listing but NOT their own pending listing
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertIn(self.active_listing.id, listing_ids)
        self.assertNotIn(self.pending_listing.id, listing_ids)

    def test_listing_detail_increments_views(self):
        """
        Retrieving a listing detail should increment the view count atomically
        only when viewed by a new unique user profile (reloads do not increment it).
        """
        self.client.force_authenticate(user=self.buyer)
        url = reverse('listing-detail', kwargs={'pk': self.active_listing.id})
        
        # Fetch it first time as buyer
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['views'], 1)
        
        # Fetch it second time as buyer (reload) - should NOT increment
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['views'], 1)

        # Fetch it as seller - should NOT increment (seller is excluded)
        self.client.force_authenticate(user=self.seller)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['views'], 1)

        # Fetch it as a different user (admin) - should increment to 2
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['views'], 2)

    def test_create_listing_with_flat_keys_mapping_to_vehicle(self):
        """
        Verify flat form-data parameters translate into a nested VehicleListing.
        """
        self.client.force_authenticate(user=self.seller)
        url = reverse('listing-list')
        
        data = {
            'title': 'Maruti Swift LXi',
            'description': 'Family hatchback for sale.',
            'price': '280000.00',
            'condition': 'good',
            'category_id': self.cars_cat.id,
            'vehicle_brand': 'Maruti Suzuki',
            'vehicle_model': 'Swift',
            'vehicle_year': '2018',
            'vehicle_km_driven': '52000',
            'vehicle_fuel_type': 'petrol',
            'vehicle_transmission': 'manual'
        }
        
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Check that Listing got created with status 'pending'
        created_listing_id = response.data['id']
        listing = MarketplaceListing.objects.get(id=created_listing_id)
        self.assertEqual(listing.status, 'pending')
        self.assertEqual(listing.title, 'Maruti Swift LXi')
        
        # Check that VehicleListing got created and linked
        self.assertIsNotNone(listing.vehicle_details)
        self.assertEqual(listing.vehicle_details.brand, 'Maruti Suzuki')
        self.assertEqual(listing.vehicle_details.year, 2018)

    def test_image_uploads_limit_and_size(self):
        """
        Verify the serializer enforces image count limits and formats.
        """
        self.client.force_authenticate(user=self.seller)
        url = reverse('listing-list')

        # Try to upload too many images (6 instead of 5)
        small_image = SimpleUploadedFile("item.jpg", b"dummy_content", content_type="image/jpeg")
        data = {
            'title': 'Test Item with Images',
            'description': 'A brief description.',
            'price': '500.00',
            'condition': 'new',
            'category_id': self.electronics_cat.id,
            'images': [small_image] * 6
        }
        
        response = self.client.post(url, data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('images', response.data)

    def test_owner_status_transitions(self):
        """
        Listing owners can transition between available -> reserved -> sold.
        """
        self.client.force_authenticate(user=self.seller)
        url_active = reverse('listing-update-status', kwargs={'pk': self.active_listing.id})
        url_pending = reverse('listing-update-status', kwargs={'pk': self.pending_listing.id})
        
        # 1. Owner tries to transition pending listing to available (should fail/400)
        response = self.client.patch(url_pending, {'status': 'available'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # 1b. Owner transitions pending listing to sold (should succeed/200)
        response = self.client.patch(url_pending, {'status': 'sold'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'sold')
        
        # 2. Transition active listing to reserved
        response = self.client.patch(url_active, {'status': 'reserved'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'reserved')
        
        # 3. Transition to sold
        response = self.client.patch(url_active, {'status': 'sold'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'sold')
        
        # 4. Owner tries to change status of already sold listing (should fail/400)
        response = self.client.patch(url_active, {'status': 'available'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        
        # 5. Non-owner tries to transition status (should fail/403)
        self.client.force_authenticate(user=self.buyer)
        response = self.client.patch(url_active, {'status': 'available'})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_moderation_flow(self):
        """
        Admins can view pending queue, approve, or reject listings.
        """
        # Admin gets pending listings queue
        self.client.force_authenticate(user=self.admin)
        url_pending = reverse('listing-pending-listings')
        response = self.client.get(url_pending)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)
        
        # Approve pending listing
        url_approve = reverse('listing-approve-listing', kwargs={'pk': self.pending_listing.id})
        response = self.client.post(url_approve)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.pending_listing.refresh_from_db()
        self.assertEqual(self.pending_listing.status, 'available')
        
        # Reject listing (re-flagging active one to pending then rejecting)
        self.active_listing.status = 'pending'
        self.active_listing.save()
        
        url_reject = reverse('listing-reject-listing', kwargs={'pk': self.active_listing.id})
        response = self.client.post(url_reject, {'rejection_reason': 'Invalid price details'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.active_listing.refresh_from_db()
        self.assertEqual(self.active_listing.status, 'rejected')
        self.assertEqual(self.active_listing.rejection_reason, 'Invalid price details')

    def test_update_listing_reverts_to_pending(self):
        """
        Editing a listing by a non-admin owner should revert its status to 'pending' review.
        """
        self.client.force_authenticate(user=self.seller)
        url = reverse('listing-detail', kwargs={'pk': self.active_listing.id})
        
        # Verify it starts as available
        self.assertEqual(self.active_listing.status, 'available')
        
        data = {
            'title': 'Sleek Sedan Car (Updated)',
            'description': 'Updated description.',
            'price': 360000.00,
            'condition': 'like_new',
            'category_id': self.cars_cat.id
        }
        
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'pending')
        
        # Verify in DB
        self.active_listing.refresh_from_db()
        self.assertEqual(self.active_listing.status, 'pending')

    def test_chat_endpoints_list_and_history(self):
        """
        Verify chat inbox conversations list and message history pagination.
        """
        # Create some messages
        ChatMessage.objects.create(
            listing=self.active_listing,
            sender=self.buyer,
            receiver=self.seller,
            message="Hi, is this car still available?"
        )
        ChatMessage.objects.create(
            listing=self.active_listing,
            sender=self.seller,
            receiver=self.buyer,
            message="Yes, it is available."
        )

        # 1. Fetch conversations list for buyer
        self.client.force_authenticate(user=self.buyer)
        response = self.client.get(reverse('chat-conversations'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['other_user']['employee_id'], self.seller.employee_id)
        self.assertEqual(response.data[0]['last_message']['message'], "Yes, it is available.")

        # 2. Fetch messages history between buyer and seller for the listing
        history_url = reverse('chat-message-history', kwargs={'listing_id': self.active_listing.id})
        response = self.client.get(history_url, {'other_user_id': self.seller.employee_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual(len(response.data['results']), 2)
        self.assertEqual(response.data['results'][0]['message'], "Hi, is this car still available?")

    def test_chat_blocked_and_status_in_history(self):
        """
        Verify that marking a listing as sold blocks further chat messages
        and returns the correct listing status in the history REST API.
        """
        # 1. Verify history response contains listing status
        self.client.force_authenticate(user=self.buyer)
        history_url = reverse('chat-message-history', kwargs={'listing_id': self.active_listing.id})
        response = self.client.get(history_url, {'other_user_id': self.seller.employee_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['listing_status'], 'available')

        # 2. Mark listing as sold
        self.active_listing.status = 'sold'
        self.active_listing.save()

        # 3. Verify history response now contains listing_status 'sold'
        response = self.client.get(history_url, {'other_user_id': self.seller.employee_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['listing_status'], 'sold')

        # 4. Verify that trying to save a message raises ValueError on backend (consumer level)
        from .consumers import save_chat_message

        with self.assertRaises(ValueError):
            save_chat_message.__wrapped__(
                self.active_listing.id,
                self.buyer,
                self.seller,
                "Is it sold?"
            )

    def test_delete_old_sold_listings_command(self):
        """
        Verify that the delete_old_sold_listings command correctly purges
        listings that have been sold for more than 6 days and calls Cloudinary destroy.
        """
        from django.core.management import call_command
        from django.utils import timezone
        from datetime import timedelta

        # Create a listing marked as sold 7 days ago
        old_sold_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="Old Sold Item",
            description="Sold long ago.",
            price=100.00,
            condition="good",
            category=self.electronics_cat,
            status="sold"
        )
        # Manually update updated_at back in time
        MarketplaceListing.objects.filter(id=old_sold_listing.id).update(
            updated_at=timezone.now() - timedelta(days=7)
        )

        # Create a listing marked as sold 3 days ago (should NOT be deleted)
        recent_sold_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="Recent Sold Item",
            description="Sold recently.",
            price=150.00,
            condition="good",
            category=self.electronics_cat,
            status="sold"
        )
        MarketplaceListing.objects.filter(id=recent_sold_listing.id).update(
            updated_at=timezone.now() - timedelta(days=3)
        )

        # Mock image
        image_mock = ListingImage.objects.create(
            listing=old_sold_listing,
            image="uploads/dummy.jpg"
        )

        with patch('cloudinary.uploader.destroy') as mock_destroy, \
             patch('django.core.files.storage.default_storage.exists', return_value=True) as mock_exists, \
             patch('django.core.files.storage.default_storage.delete') as mock_delete:

            # Run command
            call_command('delete_old_sold_listings')

            # Check DB
            self.assertFalse(MarketplaceListing.objects.filter(id=old_sold_listing.id).exists())
            self.assertTrue(MarketplaceListing.objects.filter(id=recent_sold_listing.id).exists())

            # Since mock image is a LocalMediaResource (in local environment/fallback), mock_delete should be called
            mock_delete.assert_called_once()

    def test_sold_listings_visibility_in_feed(self):
        """
        Verify that sold listings are hidden from the public feed for both
        the owner and other buyers, but still queryable if explicitly requested by owner.
        """
        # Create a sold listing
        sold_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="My Sold Listing",
            description="Sold item.",
            price=500.00,
            condition="good",
            category=self.electronics_cat,
            status="sold"
        )

        # 1. Other buyer requests the list feed - should NOT see the sold listing
        self.client.force_authenticate(user=self.buyer)
        url = reverse('listing-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertNotIn(sold_listing.id, listing_ids)

        # 2. Owner requests the list feed - should NOT see the sold listing by default
        self.client.force_authenticate(user=self.seller)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertNotIn(sold_listing.id, listing_ids)

        # 3. Owner queries explicitly for status=sold - should see it
        response = self.client.get(url, {'status': 'sold'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertIn(sold_listing.id, listing_ids)

        # 4. Other buyer queries explicitly for status=sold - should NOT see it (restricted)
        self.client.force_authenticate(user=self.buyer)
        response = self.client.get(url, {'status': 'sold'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertNotIn(sold_listing.id, listing_ids)

    def test_listing_expiration_model_and_approval_logic(self):
        """
        Verify expires_at is set on creation and resets on admin approval,
        and is_expired property behaves correctly.
        """
        from django.utils import timezone
        from datetime import timedelta

        # 1. Test creation sets expires_at to roughly 30 days
        new_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="Test Expiring Item",
            description="Testing expiration.",
            price=200.00,
            condition="new",
            category=self.electronics_cat,
            status="pending"
        )
        self.assertIsNotNone(new_listing.expires_at)
        time_diff = new_listing.expires_at - timezone.now()
        self.assertTrue(timedelta(days=29) < time_diff < timedelta(days=31))
        self.assertFalse(new_listing.is_expired)

        # 2. Test admin approval resets expires_at
        # Set expires_at back in time
        old_time = timezone.now() - timedelta(days=10)
        new_listing.expires_at = old_time
        new_listing.save()

        # Approve it
        self.client.force_authenticate(user=self.admin)
        approve_url = reverse('listing-approve-listing', kwargs={'pk': new_listing.id})
        response = self.client.post(approve_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        new_listing.refresh_from_db()
        self.assertEqual(new_listing.status, 'available')
        time_diff = new_listing.expires_at - timezone.now()
        self.assertTrue(timedelta(days=29) < time_diff < timedelta(days=31))
        self.assertFalse(new_listing.is_expired)

    def test_expired_listings_visibility_and_restrictions(self):
        """
        Verify that expired listings are filtered out for general buyers,
        locked from updates, and block chat messages.
        """
        from django.utils import timezone
        from datetime import timedelta
        from .consumers import save_chat_message

        # 1. Create an expired listing (dynamic expiration: status available, expires_at in the past)
        expired_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="Expired Item",
            description="No longer available.",
            price=150.00,
            condition="good",
            category=self.electronics_cat,
            status="available"
        )
        # Force expires_at to be in the past
        MarketplaceListing.objects.filter(id=expired_listing.id).update(
            expires_at=timezone.now() - timedelta(days=1)
        )
        expired_listing.refresh_from_db()
        self.assertTrue(expired_listing.is_expired)

        # 2. Buyer requests list feed - should NOT see the expired listing
        self.client.force_authenticate(user=self.buyer)
        url = reverse('listing-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertNotIn(expired_listing.id, listing_ids)

        # 3. Buyer queries status=available explicitly - should NOT see it
        response = self.client.get(url, {'status': 'available'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertNotIn(expired_listing.id, listing_ids)

        # 4. Buyer attempts to retrieve details - should return 404
        detail_url = reverse('listing-detail', kwargs={'pk': expired_listing.id})
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # 5. Seller requests feed - should NOT see their own expired listing in the main browse feed
        self.client.force_authenticate(user=self.seller)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data['results'] if isinstance(response.data, dict) and 'results' in response.data else response.data
        listing_ids = [item['id'] for item in results]
        self.assertNotIn(expired_listing.id, listing_ids)

        # 6. Seller retrieves details - should succeed
        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 7. Seller attempts to edit the expired listing - should be blocked
        response = self.client.put(detail_url, {
            'title': 'Updated Expired Item',
            'description': 'Attempting update.',
            'price': 140.00,
            'condition': 'good',
            'category_id': self.electronics_cat.id
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cannot modify details of a listing that has already been marked as sold or is expired.", response.data['detail'])

        # 8. Seller attempts to change status via status action - should be blocked
        status_url = reverse('listing-update-status', kwargs={'pk': expired_listing.id})
        response = self.client.patch(status_url, {'status': 'available'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cannot modify status of a listing that has already been marked as sold or is expired.", response.data['detail'])

        # 9. Verify WebSocket chat is blocked for expired listings
        with self.assertRaises(ValueError):
            save_chat_message.__wrapped__(
                expired_listing.id,
                self.buyer,
                self.seller,
                "Is it still available?"
            )

    def test_expire_old_listings_command(self):
        """
        Verify the expire_old_listings custom management command correctly
        transitions expired listings to status='expired' and notifies the seller.
        """
        from django.core.management import call_command
        from django.utils import timezone
        from datetime import timedelta
        from notifications.models import Notification

        # Create a listing that expired
        to_expire_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="Soon to Expire",
            description="Active but old.",
            price=300.00,
            condition="good",
            category=self.electronics_cat,
            status="available"
        )
        MarketplaceListing.objects.filter(id=to_expire_listing.id).update(
            expires_at=timezone.now() - timedelta(days=1)
        )

        # Create a listing that is NOT expired
        active_listing = MarketplaceListing.objects.create(
            seller=self.seller,
            title="Still Active",
            description="Active and new.",
            price=400.00,
            condition="good",
            category=self.electronics_cat,
            status="available"
        )

        # Count notifications before
        noti_count_before = Notification.objects.filter(recipient=self.seller).count()

        # Run the command, executing deferred on_commit callbacks
        with self.captureOnCommitCallbacks(execute=True):
            call_command('expire_old_listings')

        to_expire_listing.refresh_from_db()
        active_listing.refresh_from_db()

        # Check status updates
        self.assertEqual(to_expire_listing.status, 'expired')
        self.assertEqual(active_listing.status, 'available')

        # Check notification was created
        noti_count_after = Notification.objects.filter(recipient=self.seller).count()
        self.assertEqual(noti_count_after, noti_count_before + 1)
        latest_noti = Notification.objects.filter(recipient=self.seller).order_by('-created_at').first()
        self.assertIn("expired", latest_noti.title.lower())
        self.assertIn("Soon to Expire", latest_noti.message)
