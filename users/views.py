import hashlib
import secrets
import logging
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Employee, OTPVerification
from .serializers import EmployeeSerializer, OTPRequestSerializer, OTPVerifySerializer, PasswordLoginSerializer

logger = logging.getLogger(__name__)


def employee_id_key(group, request):
    """
    Custom rate limit key generator that extracts the employee_id from 
    JSON request payloads, query params, or standard POST data.
    """
    employee_id = None
    try:
        if hasattr(request, 'data') and isinstance(request.data, dict):
            employee_id = request.data.get('employee_id')
    except Exception:
        pass
        
    if not employee_id:
        employee_id = request.POST.get('employee_id') or request.GET.get('employee_id')
        
    return str(employee_id).strip() if employee_id else 'anonymous'


class RequestOTPView(APIView):
    """
    POST: api/auth/request-otp/
    Requests a 6-digit OTP code for login.
    Validates that the provided Employee ID and official Email match an active record.
    Hashes the OTP code before storing it in the database and dispatches the raw code via email.
    
    Rate Limiting: Max 5 requests per hour per employee ID.
    """
    permission_classes = []  # Public endpoint

    @method_decorator(ratelimit(key=employee_id_key, rate='5/h', block=False))
    def post(self, request):
        # Check if rate limit was exceeded
        was_limited = getattr(request, 'limited', False)
        if was_limited:
            logger.warning(f"OTP request rate-limited for key: {employee_id_key(None, request)}")
            return Response(
                {"detail": "Too many OTP requests. Please wait and try again after some time."},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        # Deserialize and validate payload
        serializer = OTPRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        employee_id = serializer.validated_data['employee_id']
        email = serializer.validated_data['email']

        # Find matching employee in local DB (Oracle fallback logic can be added in later phases)
        try:
            employee = Employee.objects.get(employee_id=employee_id, email=email)
        except Employee.DoesNotExist:
            # For security reasons, don't disclose whether the Employee ID or Email was invalid.
            # Use the exact message expected by the frontend.
            return Response(
                {"detail": "Failed to send OTP. Make sure ID and Email match."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not employee.is_active:
            return Response(
                {"detail": "This employee account has been deactivated. Contact IT Admin."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Generate a secure 6-digit OTP code
        otp_code = f"{secrets.randbelow(1000000):06d}"
        hashed_otp = hashlib.sha256(otp_code.encode('utf-8')).hexdigest()

        # Create OTPVerification record (expires_at is automatically handled in save())
        # Invalidate all previous unused OTPs for this employee to prevent multiple valid OTPs
        OTPVerification.objects.filter(
            employee=employee,
            is_used=False
        ).update(is_used=True)

        otp_record = OTPVerification.objects.create(
            employee=employee,
            otp_code=hashed_otp
        )

        # Dispatch OTP via official BHEL Email
        try:
            send_mail(
                subject="BHEL Connect - Your Secure Login OTP",
                message=(
                    f"Hello {employee.name},\n\n"
                    f"Your OTP for logging into BHEL Connect is: {otp_code}\n\n"
                    f"This OTP is valid for 10 minutes and can only be used once.\n\n"
                    f"If you did not request this OTP, please contact the security team.\n\n"
                    f"Regards,\n"
                    f"BHEL Connect Team"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[employee.email],
                fail_silently=False,
            )
        except Exception as e:
            logger.error(f"Failed to send OTP email to {employee.email}: {str(e)}")
            # If in debug mode, fail silently so developers see OTP in console
            if not settings.DEBUG:
                # In production, clean up record and raise an error
                otp_record.delete()
                return Response(
                    {"detail": "Email service error. Failed to dispatch OTP. Please try again later."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        return Response(
            {"detail": "OTP sent successfully to your email"},
            status=status.HTTP_200_OK
        )


class VerifyOTPView(APIView):
    """
    POST: api/auth/verify-otp/
    Verifies the OTP code submitted by the employee.
    If valid, marks the OTP as used and returns JWT access + refresh tokens.
    Hashed claims like 'name' and 'is_admin' are embedded in the JWT payload.
    
    Enforces maximum of 5 failed attempts per OTP record to prevent brute-forcing.
    """
    permission_classes = []  # Public endpoint

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        employee_id = serializer.validated_data['employee_id']
        otp_code = serializer.validated_data['otp_code']

        # Look up latest unused OTP for employee
        otp_record = OTPVerification.objects.filter(
            employee__employee_id=employee_id,
            is_used=False
        ).order_by('-created_at').first()

        if not otp_record:
            return Response(
                {"detail": "No active OTP request found for this employee ID."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check for lockout
        if otp_record.is_locked_out:
            return Response(
                {"detail": "This OTP has been locked due to too many failed attempts. Please request a new OTP."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check for expiration
        if otp_record.is_expired:
            return Response(
                {"detail": "This OTP has expired. Please request a new OTP."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Hashing comparison
        submitted_hash = hashlib.sha256(otp_code.encode('utf-8')).hexdigest()
        if otp_record.otp_code != submitted_hash:
            # Increment attempt count
            otp_record.attempt_count += 1
            otp_record.save()
            
            if otp_record.is_locked_out:
                return Response(
                    {"detail": "Invalid OTP. You have exceeded the maximum allowed attempts. This OTP is now locked."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            remaining_attempts = 5 - otp_record.attempt_count
            return Response(
                {"detail": f"Invalid OTP. {remaining_attempts} attempts remaining."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Successful verification
        otp_record.is_used = True
        otp_record.save()

        # Generate JWT tokens with custom payload claims (for frontend decoding)
        employee = otp_record.employee
        refresh = RefreshToken.for_user(employee)
        
        # Inject custom claims
        refresh['name'] = employee.name
        refresh['is_admin'] = employee.is_admin

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh)
        }, status=status.HTTP_200_OK)


class LoginWithPasswordView(APIView):
    """
    POST: api/auth/login-password/
    Logs in the employee via their Employee ID and password.
    If credentials are valid and account is active, returns JWT access + refresh tokens.
    Rate Limiting: Max 5 requests per hour per employee ID.
    """
    permission_classes = []  # Public endpoint

    @method_decorator(ratelimit(key=employee_id_key, rate='5/h', block=False))
    def post(self, request):
        # Check if rate limit was exceeded
        was_limited = getattr(request, 'limited', False)
        if was_limited:
            logger.warning(f"Password login rate-limited for key: {employee_id_key(None, request)}")
            return Response(
                {"detail": "Too many login attempts. Please wait and try again after some time."},
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        serializer = PasswordLoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        employee_id = serializer.validated_data['employee_id']
        password = serializer.validated_data['password']

        try:
            employee = Employee.objects.get(employee_id=employee_id)
        except Employee.DoesNotExist:
            employee = None

        if employee:
            password_correct = employee.check_password(password)
        else:
            # Prevent timing attacks (user enumeration) by running a dummy password hashing check
            dummy = Employee()
            dummy.set_password(password)
            password_correct = False

        if not employee or not password_correct:
            return Response(
                {"detail": "Invalid Employee ID or password."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not employee.is_active:
            return Response(
                {"detail": "This account is deactivated. Please contact an administrator."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Generate JWT tokens with custom payload claims (for frontend decoding)
        refresh = RefreshToken.for_user(employee)
        
        # Inject custom claims
        refresh['name'] = employee.name
        refresh['is_admin'] = employee.is_admin

        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh)
        }, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """
    POST: api/auth/logout/
    Blacklists the provided JWT refresh token.
    Requires authentication.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data.get('refresh')
            if not refresh_token:
                return Response(
                    {"detail": "Refresh token is required to log out."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            token = RefreshToken(refresh_token)
            token.blacklist()
            return Response(
                {"detail": "Logged out successfully."},
                status=status.HTTP_200_OK
            )
        except Exception as e:
            return Response(
                {"detail": "Invalid or already blacklisted refresh token."},
                status=status.HTTP_400_BAD_REQUEST
            )


class EmployeeCountView(APIView):
    """
    GET: api/users/count/
    Returns the total count of registered employees.
    Requires authentication.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = Employee.objects.count()
        return Response({"count": count}, status=status.HTTP_200_OK)


class ProfileView(APIView):
    """
    GET: api/users/profile/
    Returns the logged-in employee's profile data.
    
    PATCH: api/users/profile/
    Allows updating mobile number and profile picture using multipart/form-data.
    Cloudinary Storage automatically processes image uploads to CDN on instance save.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = EmployeeSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = EmployeeSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MyListingsView(APIView):
    """
    GET: api/users/my-listings/
    Returns all marketplace listings created by the logged-in user.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from marketplace.models import MarketplaceListing
        from marketplace.serializers import MarketplaceListingSerializer
        listings = MarketplaceListing.objects.select_related(
            'seller', 'category', 'vehicle_details', 'property_details'
        ).prefetch_related(
            'images'
        ).filter(seller=request.user).order_by('-created_at')
        serializer = MarketplaceListingSerializer(listings, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class MyPurchasesView(APIView):
    """
    GET: api/users/my-purchases/
    Returns all campaign registrations (purchases) made by the logged-in employee.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from smartbuy.models import CampaignRegistration
        from smartbuy.serializers import CampaignRegistrationSerializer
        registrations = CampaignRegistration.objects.select_related(
            'campaign', 'employee'
        ).filter(employee=request.user).order_by('-reservation_date')
        serializer = CampaignRegistrationSerializer(registrations, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class SavedProductsView(APIView):
    """
    GET: api/users/saved-products/
    Returns the user's saved/wishlisted marketplace listings.

    POST: api/users/saved-products/
    Saves a marketplace listing for the user.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import SavedProduct
        from .serializers import SavedProductSerializer
        saved = SavedProduct.objects.select_related(
            'marketplace_listing',
            'marketplace_listing__seller',
            'marketplace_listing__category',
            'marketplace_listing__vehicle_details',
            'marketplace_listing__property_details'
        ).prefetch_related(
            'marketplace_listing__images'
        ).filter(employee=request.user).order_by('-saved_at')
        serializer = SavedProductSerializer(saved, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        from .models import SavedProduct
        from .serializers import SavedProductSerializer
        from marketplace.models import MarketplaceListing

        listing_id = request.data.get('marketplace_listing_id')
        if not listing_id:
            return Response({"detail": "marketplace_listing_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            listing = MarketplaceListing.objects.get(id=listing_id)
        except MarketplaceListing.DoesNotExist:
            return Response({"detail": "Listing not found."}, status=status.HTTP_404_NOT_FOUND)

        saved_product, created = SavedProduct.objects.get_or_create(
            employee=request.user,
            marketplace_listing=listing
        )
        if not created:
            return Response({"detail": "Product is already saved."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = SavedProductSerializer(saved_product, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class SavedProductDetailView(APIView):
    """
    DELETE: api/users/saved-products/<int:listing_id>/
    Removes a listing from the user's saved/wishlisted products list.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, listing_id):
        from .models import SavedProduct
        try:
            saved_product = SavedProduct.objects.get(
                employee=request.user, 
                marketplace_listing_id=listing_id
            )
            saved_product.delete()
            return Response({"detail": "Product removed from saved list."}, status=status.HTTP_200_OK)
        except SavedProduct.DoesNotExist:
            return Response({"detail": "Saved product not found."}, status=status.HTTP_404_NOT_FOUND)


class TokenHistoryView(APIView):
    """
    GET: api/users/token-history/
    Returns all token payments submitted by the employee.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from smartbuy.models import TokenPayment
        from smartbuy.serializers import TokenPaymentSerializer
        payments = (
            TokenPayment.objects
            .filter(registration__employee=request.user)
            .select_related('registration', 'registration__campaign', 'registration__employee')
            .order_by('-submitted_at')
        )
        serializer = TokenPaymentSerializer(payments, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


from rest_framework.pagination import PageNumberPagination
from users.permissions import IsAdminEmployee
from django.db.models import Q
from django.shortcuts import get_object_or_404

class EmployeePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class EmployeeListView(APIView):
    """
    GET: api/users/employees/
    Lists all employees, with pagination and search functionality.
    Access restricted to Admin Employees only.
    """
    permission_classes = [IsAdminEmployee]

    def get(self, request):
        queryset = Employee.objects.all().order_by('employee_id')
        
        search = request.query_params.get('search', '').strip()
        if search:
            queryset = queryset.filter(
                Q(
                    Q(employee_id__icontains=search),
                    Q(name__icontains=search),
                    Q(email__icontains=search),
                    Q(department__icontains=search),
                    _connector=Q.OR
                )
            )
            
        paginator = EmployeePagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        if page is not None:
            serializer = EmployeeSerializer(page, many=True, context={'request': request})
            return paginator.get_paginated_response(serializer.data)
            
        serializer = EmployeeSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class EmployeeDetailView(APIView):
    """
    PATCH: api/users/employees/<str:employee_id>/
    Updates an employee record (e.g. toggles is_active status).
    Access restricted to Admin Employees only.
    """
    permission_classes = [IsAdminEmployee]

    def patch(self, request, employee_id):
        employee = get_object_or_404(Employee, employee_id=employee_id)
        
        # Security: Only allow toggling is_active via direct field update.
        # All other fields must go through the serializer (which enforces read_only_fields).
        if 'is_active' in request.data:
            # Prevent admin from deactivating themselves
            if employee == request.user:
                return Response(
                    {"detail": "You cannot deactivate your own account."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Only process is_active — ignore any other fields in this code path
            employee.is_active = bool(request.data['is_active'])
            employee.save(update_fields=['is_active'])
            serializer = EmployeeSerializer(employee, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        serializer = EmployeeSerializer(employee, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

