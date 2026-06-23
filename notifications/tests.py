from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.db import transaction, IntegrityError
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status

from .models import Notification
from .utils import create_notification, create_notifications_bulk

User = get_user_model()


class NotificationAPITests(APITestCase):
    """
    Test suite for notifications REST API endpoints.
    """

    def setUp(self):
        # Create test users
        self.user1 = User.objects.create_user(
            employee_id="E100001",
            email="employee1@bhel.in",
            name="John Doe",
            department="IT",
            mobile="9876543210",
            password="securepassword123"
        )
        self.user2 = User.objects.create_user(
            employee_id="E100002",
            email="employee2@bhel.in",
            name="Jane Doe",
            department="HR",
            mobile="9876543211",
            password="securepassword123"
        )

        # Create notifications for user1
        self.notif1 = Notification.objects.create(
            recipient=self.user1,
            title="Campaign Confirmed",
            message="Your reservation is confirmed.",
            notification_type="campaign",
            link="/smartbuy/1"
        )
        self.notif2 = Notification.objects.create(
            recipient=self.user1,
            title="New Chat Message",
            message="Hello from seller.",
            notification_type="chat",
            link="/marketplace/1",
            is_read=True
        )

        # Create notification for user2
        self.notif3 = Notification.objects.create(
            recipient=self.user2,
            title="System Alert",
            message="Maintenance tonight.",
            notification_type="system"
        )

    def test_notification_list_unauthenticated(self):
        """
        Ensure unauthenticated requests are rejected.
        """
        response = self.client.get(reverse("notification-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_notification_list_paginated_ordered(self):
        """
        Ensure authenticated user gets their own notifications, ordered by created_at desc.
        """
        self.client.force_authenticate(user=self.user1)
        response = self.client.get(reverse("notification-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify pagination structure
        self.assertIn("results", response.data)
        self.assertIn("count", response.data)
        self.assertEqual(response.data["count"], 2)
        
        # Verify ordering (notif2 has auto_now_add and is created after notif1)
        results = response.data["results"]
        self.assertEqual(results[0]["id"], self.notif2.id)
        self.assertEqual(results[1]["id"], self.notif1.id)

    def test_notification_unread_count(self):
        """
        Ensure unread count returns only unread notifications belonging to the user.
        """
        self.client.force_authenticate(user=self.user1)
        response = self.client.get(reverse("notification-unread-count"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["unread_count"], 1) # only notif1 is unread

        # Authenticate as user2
        self.client.force_authenticate(user=self.user2)
        response = self.client.get(reverse("notification-unread-count"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["unread_count"], 1) # notif3 is unread

    def test_mark_single_notification_read(self):
        """
        Ensure single notification can be marked read by its recipient.
        """
        self.client.force_authenticate(user=self.user1)
        url = reverse("mark-notification-read", kwargs={"pk": self.notif1.id})
        response = self.client.patch(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_read"])
        
        # Re-fetch from DB
        self.notif1.refresh_from_db()
        self.assertTrue(self.notif1.is_read)

    def test_mark_single_notification_read_forbidden(self):
        """
        Ensure a user cannot mark another user's notification as read (returns 404).
        """
        self.client.force_authenticate(user=self.user1)
        # Attempt to read user2's notification
        url = reverse("mark-notification-read", kwargs={"pk": self.notif3.id})
        response = self.client.patch(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_mark_all_notifications_read(self):
        """
        Ensure a user can mark all of their own notifications as read.
        """
        self.client.force_authenticate(user=self.user1)
        response = self.client.post(reverse("mark-all-notifications-read"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Re-verify unread count is 0
        response_count = self.client.get(reverse("notification-unread-count"))
        self.assertEqual(response_count.data["unread_count"], 0)
        
        # Verify user2's notifications were unaffected
        self.notif3.refresh_from_db()
        self.assertFalse(self.notif3.is_read)


class NotificationTransactionSafetyTests(TransactionTestCase):
    """
    Test suite verifying that notifications creation behaves safely inside transactions.
    Must inherit from TransactionTestCase to allow transactions to commit and execute on_commit hooks.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            employee_id="E200001",
            email="safetytest@bhel.in",
            name="Safety User",
            department="QA",
            mobile="9876543222",
            password="securepassword123"
        )

    def test_create_notification_inside_committed_transaction(self):
        """
        Verify that a notification created inside a transaction is only committed
        after the transaction successfully finishes.
        """
        with transaction.atomic():
            create_notification(
                recipient=self.user,
                title="Transmitted successfully",
                message="This is a test notification.",
                notification_type="system"
            )
            # Within the transaction, it shouldn't exist in the database yet
            self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 0)

        # Once transaction blocks exits (commits), it should be created
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 1)

    def test_create_notification_inside_rolled_back_transaction(self):
        """
        Verify that a notification created inside a rolled back transaction is NOT created.
        """
        try:
            with transaction.atomic():
                create_notification(
                    recipient=self.user,
                    title="Rollback test",
                    message="Should not be saved.",
                    notification_type="system"
                )
                raise IntegrityError("Force transaction failure")
        except IntegrityError:
            pass

        # Verify no notification was created
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 0)

    def test_create_notifications_bulk_inside_committed_transaction(self):
        """
        Verify bulk notification utility works correctly with transaction.on_commit.
        """
        with transaction.atomic():
            notifs = [
                Notification(
                    recipient=self.user,
                    title=f"Bulk Notif {i}",
                    message=f"Detail {i}",
                    notification_type="system"
                )
                for i in range(3)
            ]
            create_notifications_bulk(notifs)
            self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 0)

        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 3)
