import hashlib
from datetime import timedelta
from django.urls import reverse
from django.utils import timezone
from django.conf import settings
from django.core import mail
from django.core.cache import cache
from rest_framework.test import APITestCase
from rest_framework import status
from rest_framework_simplejwt.tokens import AccessToken

from .models import Employee, OTPVerification


class AuthenticationTests(APITestCase):
    """
    Unit tests for BHEL Connect Phase 2 Authentication System.
    """

    def setUp(self):
        # Clear the cache before each test to reset rate limits
        cache.clear()
        
        # Seed test employees
        self.employee = Employee.objects.create_user(
            employee_id="EMP000002",
            name="Ramesh Kumar",
            email="ramesh.kumar@bhel.in",
            department="Electrical Engineering",
            mobile="9876543211",
            password="testpassword"
        )
        self.admin = Employee.objects.create_user(
            employee_id="EMP000001",
            name="Admin User",
            email="admin@bhel.in",
            department="Information Technology",
            mobile="9876543210",
            is_admin=True,
            password="testpassword"
        )
        
        self.request_otp_url = reverse('request_otp')
        self.verify_otp_url = reverse('verify_otp')
        self.login_password_url = reverse('login_password')
        self.employee_count_url = reverse('employee_count')
        self.logout_url = reverse('logout')
        self.profile_url = reverse('user_profile')

    def test_request_otp_success(self):
        """Test successful OTP request for matching employee and email."""
        payload = {
            "employee_id": "EMP000002",
            "email": "ramesh.kumar@bhel.in"
        }
        response = self.client.post(self.request_otp_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("detail", response.data)
        
        # Verify OTP record is created in DB
        otp_records = OTPVerification.objects.filter(employee=self.employee)
        self.assertEqual(otp_records.count(), 1)
        
        # Verify email is sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Your Secure Login OTP", mail.outbox[0].subject)

    def test_request_otp_mismatch(self):
        """Test that requesting OTP with mismatched email/id returns 400."""
        payload = {
            "employee_id": "EMP000002",
            "email": "wrong.email@bhel.in"
        }
        response = self.client.post(self.request_otp_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Failed to send OTP. Make sure ID and Email match.")
        
        # Ensure no OTP record or email was generated
        self.assertEqual(OTPVerification.objects.filter(employee=self.employee).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_request_otp_rate_limiting(self):
        """Test that requests are blocked after 5 OTP requests in an hour."""
        payload = {
            "employee_id": "EMP000002",
            "email": "ramesh.kumar@bhel.in"
        }
        
        # Send 5 requests
        for _ in range(5):
            response = self.client.post(self.request_otp_url, payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            
        # 6th request should fail due to rate limit
        response = self.client.post(self.request_otp_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_verify_otp_success(self):
        """Test successful OTP verification returning JWT tokens."""
        # Create a mock OTP record manually
        raw_otp = "123456"
        hashed_otp = hashlib.sha256(raw_otp.encode('utf-8')).hexdigest()
        OTPVerification.objects.create(
            employee=self.employee,
            otp_code=hashed_otp
        )

        payload = {
            "employee_id": "EMP000002",
            "otp_code": raw_otp
        }
        response = self.client.post(self.verify_otp_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

        # Verify claims in JWT token
        access_token = AccessToken(response.data["access"])
        self.assertEqual(access_token["name"], "Ramesh Kumar")
        self.assertEqual(access_token["is_admin"], False)

        # Verify OTP is marked as used
        otp_record = OTPVerification.objects.get(employee=self.employee)
        self.assertTrue(otp_record.is_used)

    def test_verify_otp_invalid_and_lockout(self):
        """Test failed OTP attempts lock out the OTP after 5 trials."""
        raw_otp = "123456"
        hashed_otp = hashlib.sha256(raw_otp.encode('utf-8')).hexdigest()
        otp_record = OTPVerification.objects.create(
            employee=self.employee,
            otp_code=hashed_otp
        )

        # 4 failed attempts
        payload = {
            "employee_id": "EMP000002",
            "otp_code": "000000"
        }
        for i in range(4):
            response = self.client.post(self.verify_otp_url, payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn(f"{4 - i} attempts remaining", response.data["detail"])

        # 5th failed attempt should lock it
        response = self.client.post(self.verify_otp_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exceeded the maximum allowed attempts", response.data["detail"])
        
        # Verify subsequent correct OTP submission fails because locked
        payload["otp_code"] = raw_otp
        response = self.client.post(self.verify_otp_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("locked due to too many failed attempts", response.data["detail"])

    def test_verify_otp_expired(self):
        """Test verification fails if OTP is expired."""
        raw_otp = "123456"
        hashed_otp = hashlib.sha256(raw_otp.encode('utf-8')).hexdigest()
        otp_record = OTPVerification.objects.create(
            employee=self.employee,
            otp_code=hashed_otp
        )
        # Force set expiration to past
        otp_record.expires_at = timezone.now() - timedelta(minutes=1)
        otp_record.save()

        payload = {
            "employee_id": "EMP000002",
            "otp_code": raw_otp
        }
        response = self.client.post(self.verify_otp_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "This OTP has expired. Please request a new OTP.")

    def test_profile_fetch_and_update(self):
        """Test fetching and updating profiles (authenticated)."""
        self.client.force_authenticate(user=self.employee)
        
        # 1. Fetch Profile
        response = self.client.get(self.profile_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "Ramesh Kumar")
        self.assertEqual(response.data["department"], "Electrical Engineering")

        # 2. Update Mobile (valid)
        response = self.client.patch(self.profile_url, {"mobile": "9988776655"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["mobile"], "9988776655")

        # 3. Update Mobile (invalid format)
        response = self.client.patch(self.profile_url, {"mobile": "12345"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # 4. Attempt to modify read-only department field
        response = self.client.patch(self.profile_url, {"department": "New Dept"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Department remains unchanged
        self.assertEqual(response.data["department"], "Electrical Engineering")

    def test_login_password_success(self):
        """Test successful login via employee ID and password."""
        payload = {
            "employee_id": "EMP000002",
            "password": "testpassword"
        }
        response = self.client.post(self.login_password_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

        # Verify claims in JWT token
        access_token = AccessToken(response.data["access"])
        self.assertEqual(access_token["name"], "Ramesh Kumar")
        self.assertEqual(access_token["is_admin"], False)

    def test_login_password_invalid_password(self):
        """Test password login fails with incorrect password."""
        payload = {
            "employee_id": "EMP000002",
            "password": "wrongpassword"
        }
        response = self.client.post(self.login_password_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Invalid Employee ID or password.")

    def test_login_password_invalid_employee(self):
        """Test password login fails for non-existent employee ID."""
        payload = {
            "employee_id": "NONEXISTENT",
            "password": "testpassword"
        }
        response = self.client.post(self.login_password_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "Invalid Employee ID or password.")

    def test_login_password_deactivated_account(self):
        """Test password login is rejected for deactivated employee."""
        self.employee.is_active = False
        self.employee.save()

        payload = {
            "employee_id": "EMP000002",
            "password": "testpassword"
        }
        response = self.client.post(self.login_password_url, payload, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "This account is deactivated. Please contact an administrator.")

    def test_login_password_rate_limiting(self):
        """Test that password login requests are blocked after 5 requests in an hour."""
        payload = {
            "employee_id": "EMP000002",
            "password": "wrongpassword"
        }
        
        # Send 5 requests
        for _ in range(5):
            response = self.client.post(self.login_password_url, payload, format='json')
            # Should fail with invalid credentials but NOT rate-limited yet
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data["detail"], "Invalid Employee ID or password.")
        # 6th request should fail due to rate limit
        response = self.client.post(self.login_password_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(response.data["detail"], "Too many login attempts. Please wait and try again after some time.")

    def test_employee_count_authenticated(self):
        """Test fetching employee count as an authenticated employee."""
        self.client.force_authenticate(user=self.employee)
        response = self.client.get(self.employee_count_url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # We seeded 2 employees in setUp (self.employee and self.admin)
        self.assertEqual(response.data["count"], 2)

    def test_employee_count_unauthenticated(self):
        """Test fetching employee count without authentication is rejected."""
        response = self.client.get(self.employee_count_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class EmployeeManagementTests(APITestCase):
    """
    Unit tests for Employee management endpoints by admin.
    """

    def setUp(self):
        cache.clear()
        self.admin = Employee.objects.create_user(
            employee_id="EMP000001",
            name="Admin User",
            email="admin@bhel.in",
            department="Information Technology",
            mobile="9876543210",
            is_admin=True
        )
        self.employee = Employee.objects.create_user(
            employee_id="EMP000002",
            name="Ramesh Kumar",
            email="ramesh.kumar@bhel.in",
            department="Electrical Engineering",
            mobile="9876543211"
        )
        self.list_url = reverse('employee_list')
        self.detail_url = reverse('employee_detail', kwargs={'employee_id': self.employee.employee_id})

    def test_employee_list_for_admin_success(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify paginated response structure
        self.assertIn("results", response.data)
        self.assertEqual(response.data["count"], 2)

    def test_employee_list_for_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.employee)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_employee_search(self):
        self.client.force_authenticate(user=self.admin)
        # Search by name
        response = self.client.get(self.list_url, {"search": "Ramesh"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["employee_id"], "EMP000002")

    def test_employee_toggle_active_status_by_admin(self):
        self.client.force_authenticate(user=self.admin)
        # Deactivate
        response = self.client.patch(self.detail_url, {"is_active": False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_active"])
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.is_active)

        # Reactivate
        response = self.client.patch(self.detail_url, {"is_active": True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_active"])
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.is_active)

    def test_employee_toggle_active_status_by_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.employee)
        response = self.client.patch(self.detail_url, {"is_active": False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
