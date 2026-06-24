import csv
import io
import re
import logging
from datetime import timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncMonth
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser

from users.permissions import IsAdminEmployee
from smartbuy.models import Campaign, CampaignRegistration, TokenPayment
from users.models import Employee
from marketplace.models import MarketplaceListing, Category
from smartbuy.serializers import TokenPaymentSerializer, CampaignRegistrationSerializer
from notifications.utils import create_notification
from smartbuy.utils import invalidate_campaign_price_cache
from adminpanel.models import AuditLog

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Step 8.2 — Admin Dashboard Stats
# ─────────────────────────────────────────────────────────────────────────────

class AdminDashboardView(APIView):
    """
    GET: api/admin/dashboard/
    Returns high-level statistics for the BHEL Connect Admin Dashboard.
    Uses DB-level aggregation, never Python-level counting on large querysets.

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]

    def get(self, request):
        try:
            from adminpanel.models import AuditLog
            recent_activity = list(
                AuditLog.objects.select_related('admin_user')
                .values(
                    'admin_user__name',
                    'admin_user__employee_id',
                    'action',
                    'target_model',
                    'target_id',
                    'description',
                    'timestamp',
                    'ip_address',
                )
                .order_by('-timestamp')[:10]
            )
        except Exception:
            recent_activity = []

        # Count new users registered this calendar month
        now = timezone.now()
        new_users_this_month = Employee.objects.filter(
            date_joined__year=now.year,
            date_joined__month=now.month,
        ).count()

        data = {
            "active_campaigns": Campaign.objects.filter(status='active').count(),
            "closed_campaigns": Campaign.objects.filter(status='closed').count(),
            "cancelled_campaigns": Campaign.objects.filter(status='cancelled').count(),
            "active_listings": MarketplaceListing.objects.filter(status='available').count(),
            "pending_listings": MarketplaceListing.objects.filter(status='pending').count(),
            "total_users": Employee.objects.count(),
            "pending_payment_approvals": 0,  # Manual payment approvals are disabled
            "pending_refunds": CampaignRegistration.objects.filter(refund_status='pending').count(),
            "new_users_this_month": new_users_this_month,
            "recent_activity": recent_activity,
        }
        return Response(data, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# Step 8.3 — Payment Management (Approval / Rejection / Refunds)
# ─────────────────────────────────────────────────────────────────────────────

class PendingPaymentsView(APIView):
    """
    GET: api/admin/payments/pending/
    Lists all pending token payments awaiting review.

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]

    def get(self, request):
        # Manual payment approvals are disabled; all payments are automated via Cashfree
        return Response([], status=status.HTTP_200_OK)


class ApprovePaymentView(APIView):
    """
    POST: api/admin/payments/<int:payment_id>/approve/
    Approves a token payment. Manual approvals are disabled.

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]

    def post(self, request, payment_id):
        return Response(
            {"detail": "Manual payment approvals have been disabled. All payments are automated via Cashfree."},
            status=status.HTTP_400_BAD_REQUEST
        )


class RejectPaymentView(APIView):
    """
    POST: api/admin/payments/<int:payment_id>/reject/
    Rejects a payment. Manual rejections are disabled.

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]

    def post(self, request, payment_id):
        return Response(
            {"detail": "Manual payment rejections have been disabled. All payments are automated via Cashfree."},
            status=status.HTTP_400_BAD_REQUEST
        )


class PendingRefundsView(APIView):
    """
    GET: api/admin/refunds/pending/
    Lists all campaign registrations where a refund is pending.

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]

    def get(self, request):
        refunds = (
            CampaignRegistration.objects
            .filter(refund_status='pending')
            .select_related('employee', 'campaign')
            .order_by('cancellation_date')
        )
        serializer = CampaignRegistrationSerializer(refunds, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class ProcessRefundView(APIView):
    """
    POST: api/admin/refunds/<int:refund_id>/process/
    Marks a manual/UPI refund as disbursed (processed).
    If the registration was paid via Cashfree, calls Cashfree's Refund API.

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]

    def post(self, request, refund_id):
        with transaction.atomic():  # type: ignore
            registration = get_object_or_404(
                CampaignRegistration.objects.select_for_update(), id=refund_id
            )
            if registration.refund_status != 'pending':
                return Response(
                    {"detail": "This refund has already been processed or is not applicable."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if there is an approved Cashfree payment that needs gateway refunding
            payment = getattr(registration, 'token_payment', None)
            if payment and payment.status == 'approved' and payment.cashfree_order_id and not payment.cashfree_order_id.startswith('MANUAL-'):
                from smartbuy.cashfree_service import CashfreeService
                cf = CashfreeService()
                refund_ref_id = f"REF-{registration.id}-{int(timezone.now().timestamp())}"
                try:
                    # Execute gateway refund call
                    cf.create_cashfree_refund(
                        order_id=payment.cashfree_order_id,
                        refund_amount=registration.refund_amount,
                        refund_id=refund_ref_id,
                        refund_note=f"Refund for Campaign registration cancel: {registration.campaign.title}"
                    )
                except Exception as e:
                    logger.error(f"Failed to process Cashfree refund for registration {registration.id}: {e}", exc_info=True)
                    return Response(
                        {"detail": f"Failed to initiate refund with Cashfree Gateway: {str(e)}"},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )

            registration.refund_status = 'processed'
            registration.save()

            create_notification(
                recipient=registration.employee,
                title="Refund Disbursed",
                message=(
                    f"Your refund of ₹{registration.refund_amount:.2f} for campaign "
                    f"'{registration.campaign.title}' has been processed and disbursed."
                ),
                notification_type="payment",
                link="/profile",
            )

        return Response({"detail": "Refund marked as processed successfully."}, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# Step 8.7 — Bulk CSV Employee Import
# ─────────────────────────────────────────────────────────────────────────────

# Required CSV column headers (order does not matter, checked by name)
REQUIRED_CSV_COLUMNS = {'employee_id', 'name', 'email', 'mobile', 'department', 'password'}

# Validation constraints matching the Employee model
MAX_EMPLOYEE_ID_LEN = 20
MAX_NAME_LEN = 100
MAX_DEPARTMENT_LEN = 100
MOBILE_REGEX = re.compile(r'^\d{10}$')


def _validate_csv_row(row_num: int, row: dict) -> list[str]:
    """
    Validates a single CSV row against business rules.
    Returns a list of error messages (empty list = row is valid).
    """
    errors = []

    employee_id = (row.get('employee_id') or '').strip()
    name = (row.get('name') or '').strip()
    email = (row.get('email') or '').strip().lower()
    mobile = (row.get('mobile') or '').strip()
    department = (row.get('department') or '').strip()
    password = (row.get('password') or '').strip()

    # Check required fields are non-empty
    if not employee_id:
        errors.append("employee_id is missing or empty")
    elif len(employee_id) > MAX_EMPLOYEE_ID_LEN:
        errors.append(f"employee_id exceeds {MAX_EMPLOYEE_ID_LEN} characters")
    elif not employee_id.isalnum():
        errors.append("employee_id must be alphanumeric only")

    if not name:
        errors.append("name is missing or empty")
    elif len(name) > MAX_NAME_LEN:
        errors.append(f"name exceeds {MAX_NAME_LEN} characters")

    if not email:
        errors.append("email is missing or empty")
    elif '@' not in email or '.' not in email.split('@')[-1]:
        errors.append("email is not a valid email address")

    if not mobile:
        errors.append("mobile is missing or empty")
    elif not MOBILE_REGEX.match(mobile):
        errors.append("mobile must be exactly 10 digits with no spaces")

    if not department:
        errors.append("department is missing or empty")
    elif len(department) > MAX_DEPARTMENT_LEN:
        errors.append(f"department exceeds {MAX_DEPARTMENT_LEN} characters")

    if not password:
        errors.append("password is missing or empty")
    elif len(password) < 8:
        errors.append("password must be at least 8 characters long")

    return errors


class BulkEmployeeImportView(APIView):
    """
    POST: api/admin/users/bulk-import/
    Accepts a multipart CSV file upload. Validates every row before writing.
    Creates new employees or updates existing ones (upsert by employee_id).

    Returns:
      {
        "created": N,
        "updated": N,
        "errors": [{"row": N, "employee_id": "...", "reason": "..."}]
      }

    Security notes:
    - File must be a .csv with a valid UTF-8 (or latin-1 fallback) encoding.
    - Max file size is 5 MB.
    - All rows are validated before any DB writes (all-or-nothing if any row fails header check).
    - Rows with validation errors are skipped and returned in the errors list.
    - Passwords are set to unusable (employees log in via OTP, not password).

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]
    parser_classes = [MultiPartParser]

    MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

    def post(self, request):
        csv_file = request.FILES.get('file')

        # ── File presence and type checks ──────────────────────────────────
        if not csv_file:
            return Response(
                {"detail": "No file provided. Send a CSV file in the 'file' field."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not csv_file.name.lower().endswith('.csv'):
            return Response(
                {"detail": "Only .csv files are accepted."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if csv_file.size > self.MAX_FILE_SIZE_BYTES:
            return Response(
                {"detail": f"File size exceeds the 5 MB limit ({csv_file.size} bytes received)."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Read and decode file ───────────────────────────────────────────
        try:
            raw_bytes = csv_file.read()
            try:
                csv_text = raw_bytes.decode('utf-8')
            except UnicodeDecodeError:
                csv_text = raw_bytes.decode('latin-1')
        except Exception:
            return Response(
                {"detail": "Failed to read the uploaded file. Ensure it is a valid CSV."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Parse CSV ──────────────────────────────────────────────────────
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            # DictReader.fieldnames is None when the file is completely empty
            if reader.fieldnames is None:
                return Response(
                    {"detail": "The CSV file is empty or has no header row."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Normalize header names (strip whitespace, lowercase)
            normalized_headers = {h.strip().lower() for h in reader.fieldnames}
            missing_columns = REQUIRED_CSV_COLUMNS - normalized_headers
            if missing_columns:
                return Response(
                    {
                        "detail": (
                            f"CSV is missing required column(s): {', '.join(sorted(missing_columns))}. "
                            f"Expected columns: {', '.join(sorted(REQUIRED_CSV_COLUMNS))}."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            rows = list(reader)
        except csv.Error as exc:
            return Response(
                {"detail": f"CSV parsing error: {exc}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not rows:
            return Response(
                {"detail": "The CSV file contains headers but no data rows."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Validate all rows before writing ──────────────────────────────
        validated_rows = []
        row_errors = []
        seen_employee_ids = set()  # detect duplicates within the same upload

        for row_num, row in enumerate(rows, start=2):  # start=2 because row 1 is the header
            # Normalise keys to lowercase stripped
            clean_row = {k.strip().lower(): (v or '').strip() for k, v in row.items() if k}
            employee_id = clean_row.get('employee_id', '')

            # Check for duplicate employee_id within the same file
            if employee_id in seen_employee_ids:
                row_errors.append({
                    "row": row_num,
                    "employee_id": employee_id,
                    "reason": "Duplicate employee_id within the uploaded CSV file."
                })
                continue
            seen_employee_ids.add(employee_id)

            field_errors = _validate_csv_row(row_num, clean_row)
            if field_errors:
                row_errors.append({
                    "row": row_num,
                    "employee_id": employee_id,
                    "reason": "; ".join(field_errors)
                })
            else:
                validated_rows.append(clean_row)

        # ── Write valid rows to the database ──────────────────────────────
        created_count = 0
        updated_count = 0

        with transaction.atomic():  # type: ignore
            for clean_row in validated_rows:
                employee_id = clean_row['employee_id']
                name = clean_row['name']
                email = clean_row['email'].lower()
                mobile = clean_row['mobile']
                department = clean_row['department']
                password = clean_row['password']

                try:
                    employee, created = Employee.objects.get_or_create(
                        employee_id=employee_id,
                        defaults={
                            'name': name,
                            'email': email,
                            'mobile': mobile,
                            'department': department,
                            'is_active': True,
                            'is_admin': False,
                        }
                    )

                    if created:
                        # New employee — set hashed password
                        employee.set_password(password)
                        employee.save()
                        created_count += 1
                    else:
                        # Existing employee — update mutable fields only
                        # NOTE: employee_id, email, and is_admin are never changed via import
                        changed = False
                        if employee.name != name:
                            employee.name = name
                            changed = True
                        if employee.mobile != mobile:
                            employee.mobile = mobile
                            changed = True
                        if employee.department != department:
                            employee.department = department
                            changed = True
                        
                        # Securely check and update password if changed
                        if not employee.check_password(password):
                            employee.set_password(password)
                            changed = True

                        if changed:
                            employee.save()
                        updated_count += 1

                except Exception as exc:
                    # If a specific row insert fails (e.g. unique constraint on email),
                    # record it as an error and continue with the remaining rows.
                    # We re-raise if it is a programming error, not a data error.
                    row_errors.append({
                        "row": "unknown",
                        "employee_id": employee_id,
                        "reason": f"Database error: {exc}",
                    })

        return Response(
            {
                "created": created_count,
                "updated": updated_count,
                "skipped_errors": len(row_errors),
                "errors": row_errors,
            },
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Step 8.8 — Analytics Dashboard
# ─────────────────────────────────────────────────────────────────────────────

class AdminAnalyticsView(APIView):
    """
    GET: api/admin/analytics/
    Returns aggregated analytics data for the admin dashboard.
    Results are cached for 10 minutes to avoid heavy DB load on each refresh.

    Data returned:
    - top_10_viewed_listings: Marketplace listings ordered by views desc
    - top_5_campaigns_by_registrations: Campaigns with highest registration counts
    - category_breakdown: Count of listings per marketplace category
    - new_users_per_month: Monthly new employee registration counts (last 6 months)
    - revenue_summary: Total and approved token payment amounts

    Access: IsAdminEmployee only.
    """
    permission_classes = [IsAdminEmployee]

    CACHE_KEY = 'admin_analytics_data'
    CACHE_TTL = 60 * 10  # 10 minutes

    def get(self, request):
        # Return cached response if available
        cached = cache.get(self.CACHE_KEY)
        if cached:
            return Response(cached, status=status.HTTP_200_OK)

        analytics = self._compute_analytics()
        cache.set(self.CACHE_KEY, analytics, self.CACHE_TTL)
        return Response(analytics, status=status.HTTP_200_OK)

    def _compute_analytics(self):
        """
        Runs all aggregation queries. Uses Django ORM only — no raw SQL.
        Each query is independent so partial failure is handled gracefully.
        """
        # 1. Top 10 viewed marketplace listings
        try:
            top_listings = list(
                MarketplaceListing.objects
                .filter(status__in=['available', 'reserved', 'sold'])
                .select_related('seller', 'category')
                .values('id', 'title', 'price', 'status', 'views', 'seller__name', 'category__name')
                .order_by('-views')[:10]
            )
        except Exception as exc:
            logger.error("Analytics: top_listings query failed — %s", exc)
            top_listings = []

        # 2. Top 5 campaigns by registration count (annotated)
        try:
            top_campaigns = list(
                Campaign.objects
                .annotate(registration_count=Count('registrations'))
                .values('id', 'title', 'status', 'registration_count', 'total_quantity')
                .order_by('-registration_count')[:5]
            )
        except Exception as exc:
            logger.error("Analytics: top_campaigns query failed — %s", exc)
            top_campaigns = []

        # 3. Category breakdown — count of listings per category
        try:
            category_breakdown = list(
                Category.objects
                .annotate(listing_count=Count('listings'))
                .values('id', 'name', 'listing_count')
                .order_by('-listing_count')
            )
        except Exception as exc:
            logger.error("Analytics: category_breakdown query failed — %s", exc)
            category_breakdown = []

        # 4. New users per month — last 6 months
        try:
            six_months_ago = timezone.now() - timedelta(days=180)
            new_users_per_month = list(
                Employee.objects
                .filter(date_joined__gte=six_months_ago)
                .annotate(month=TruncMonth('date_joined'))
                .values('month')
                .annotate(count=Count('employee_id'))
                .order_by('month')
            )
        except Exception as exc:
            logger.error("Analytics: new_users_per_month query failed — %s", exc)
            new_users_per_month = []

        # 5. Revenue / token payment summary
        try:
            payment_summary = TokenPayment.objects.aggregate(
                total_submitted=Sum('amount'),
                total_approved=Sum('amount', filter=Q(status='approved'))
            )
        except Exception as exc:
            logger.error("Analytics: payment_summary query failed — %s", exc)
            payment_summary = {'total_submitted': 0, 'total_approved': 0}

        return {
            "top_10_viewed_listings": top_listings,
            "top_5_campaigns_by_registrations": top_campaigns,
            "category_breakdown": category_breakdown,
            "new_users_per_month": new_users_per_month,
            "payment_summary": {
                "total_submitted": str(payment_summary.get('total_submitted') or 0),
                "total_approved": str(payment_summary.get('total_approved') or 0),
            },
            "generated_at": timezone.now().isoformat(),
        }
