from django.urls import path
from .views import (
    AdminDashboardView,
    PendingPaymentsView,
    ApprovePaymentView,
    RejectPaymentView,
    PendingRefundsView,
    ProcessRefundView,
    BulkEmployeeImportView,
    AdminAnalyticsView,
)

urlpatterns = [
    # ── Dashboard & Analytics ─────────────────────────────────────────────
    # Step 8.2: High-level metrics snapshot for admin dashboard
    path('dashboard/', AdminDashboardView.as_view(), name='admin_dashboard'),
    # Step 8.8: Aggregated analytics — cached 10 minutes
    path('analytics/', AdminAnalyticsView.as_view(), name='admin_analytics'),

    # ── Payment Approval Queue ────────────────────────────────────────────
    # Step 8.5: List pending payment proof submissions
    path('payments/pending/', PendingPaymentsView.as_view(), name='admin_pending_payments'),
    # Step 8.5: Approve a specific payment by ID
    path('payments/<int:payment_id>/approve/', ApprovePaymentView.as_view(), name='admin_approve_payment'),
    # Step 8.5: Reject a specific payment with reason
    path('payments/<int:payment_id>/reject/', RejectPaymentView.as_view(), name='admin_reject_payment'),

    # ── Refund Management ─────────────────────────────────────────────────
    # List all registrations where refund is pending
    path('refunds/pending/', PendingRefundsView.as_view(), name='admin_pending_refunds'),
    # Mark a specific refund as processed (manual UPI / bank transfer)
    path('refunds/<int:refund_id>/process/', ProcessRefundView.as_view(), name='admin_process_refund'),

    # ── User Management ───────────────────────────────────────────────────
    # Step 8.7: Bulk employee import via CSV upload
    path('users/bulk-import/', BulkEmployeeImportView.as_view(), name='admin_bulk_employee_import'),
]
