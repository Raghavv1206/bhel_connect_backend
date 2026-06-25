from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from django.db import models, transaction
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action

from users.permissions import IsAdminEmployee
from .models import Campaign, Vendor, CampaignRegistration, TokenPayment, PricingTier
from .serializers import (
    CampaignSerializer, 
    VendorSerializer, 
    CampaignRegistrationSerializer, 
    TokenPaymentSerializer
)
from .utils import (
    check_and_close_campaign, 
    promote_from_waitlist, 
    invalidate_campaign_price_cache,
    has_token_payment
)
from notifications.models import Notification
from notifications.utils import create_notification, create_notifications_bulk
from .cashfree_service import CashfreeService
import os


class VendorViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing campaign Vendors.
    Admins can perform CRUD operations. Authenticated employees can list/retrieve active vendors.
    """
    queryset = Vendor.objects.all()
    serializer_class = VendorSerializer

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated()]
        return [IsAdminEmployee()]

    def get_queryset(self):
        if not self.request.user.is_admin:
            return Vendor.objects.filter(is_active=True)
        return super().get_queryset()


class CampaignViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing SmartBuy Campaigns.
    Read actions are accessible by all authenticated users.
    Write actions and administrative routines are restricted to admin employees.
    """
    queryset = Campaign.objects.select_related('vendor').prefetch_related('pricing_tiers').all()
    serializer_class = CampaignSerializer

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'create_order']:
            return [IsAuthenticated()]
        return [IsAdminEmployee()]

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def retrieve(self, request, *args, **kwargs):
        """
        Check if the campaign needs to be closed before returning detail data.
        """
        instance = self.get_object()
        check_and_close_campaign(instance)
        # Fetch updated campaign status
        instance.refresh_from_db()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='close')
    def close_campaign(self, request, pk=None):
        """
        POST: api/smartbuy/campaigns/:id/close/
        Allows admins to close a campaign early.
        """
        campaign = self.get_object()
        if campaign.status != 'active':
            return Response(
                {"detail": "Only active campaigns can be closed."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():  # type: ignore
            campaign.status = 'closed'
            campaign.save()

            # Notify all approved registrations
            final_price = campaign.get_current_price()
            registrations = CampaignRegistration.objects.filter(campaign=campaign, payment_status='approved')
            
            notifications = []
            for reg in registrations:
                notifications.append(
                    Notification(
                        recipient=reg.employee,
                        title="Campaign Closed Early",
                        message=f"Campaign '{campaign.title}' was closed early by admin. Final price is ₹{final_price:.2f}.",
                        notification_type="campaign",
                        link=f"/smartbuy/{campaign.id}"
                    )
                )
            if notifications:
                create_notifications_bulk(notifications)

            # Evaluate registrations query to a list for the commit hook
            reg_list = list(registrations)
            from .emails import send_campaign_closed_email, send_campaign_report_to_admin
            transaction.on_commit(lambda: send_campaign_closed_email(campaign, reg_list))
            transaction.on_commit(lambda: send_campaign_report_to_admin(campaign))

        return Response({"detail": "Campaign closed successfully."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel_campaign(self, request, pk=None):
        """
        POST: api/smartbuy/campaigns/:id/cancel/
        Allows admins to cancel a campaign. Marks all registrations for refund.
        """
        campaign = self.get_object()
        if campaign.status == 'cancelled':
            return Response(
                {"detail": "Campaign is already cancelled."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():  # type: ignore
            campaign.status = 'cancelled'
            campaign.save()

            registrations = CampaignRegistration.objects.filter(campaign=campaign)
            notifications = []

            # Capture copy of registrations with original status before we modify them
            original_regs = [
                CampaignRegistration(
                    employee=r.employee,
                    token_amount=r.token_amount,
                    payment_status=r.payment_status
                )
                for r in registrations
            ]

            for reg in registrations:
                if reg.payment_status == 'approved':
                    reg.refund_status = 'pending'
                    reg.refund_amount = reg.token_amount
                    reg.payment_status = 'cancelled'
                    reg.save()
                    
                    notifications.append(
                        Notification(
                            recipient=reg.employee,
                            title="Campaign Cancelled",
                            message=f"Campaign '{campaign.title}' was cancelled by admin. A full token refund of ₹{reg.token_amount:.2f} has been initiated.",
                            notification_type="campaign",
                            link=f"/smartbuy/{campaign.id}"
                        )
                    )
                elif reg.payment_status == 'pending':
                    reg.payment_status = 'cancelled'
                    reg.save()
                    
                    notifications.append(
                        Notification(
                            recipient=reg.employee,
                            title="Campaign Cancelled",
                            message=f"Campaign '{campaign.title}' has been cancelled. Your pending registration is voided.",
                            notification_type="campaign",
                            link=f"/smartbuy/{campaign.id}"
                        )
                    )

            if notifications:
                create_notifications_bulk(notifications)

            from .emails import send_campaign_cancelled_email
            transaction.on_commit(lambda: send_campaign_cancelled_email(campaign, original_regs))

        return Response({"detail": "Campaign cancelled successfully. Refunds initiated."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='extend')
    def extend_campaign(self, request, pk=None):
        """
        POST: api/smartbuy/campaigns/:id/extend/
        Allows admins to extend the campaign by N days.
        """
        campaign = self.get_object()
        if campaign.status != 'active':
            return Response(
                {"detail": "Only active campaigns can be extended."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        days = request.data.get('days')
        try:
            days_int = int(days)
        except (ValueError, TypeError):
            return Response(
                {"detail": "A valid positive integer 'days' parameter is required."},
                status=status.HTTP_400_BAD_REQUEST
            )
        if days_int <= 0:
            return Response(
                {"detail": "A positive integer 'days' parameter is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        campaign.duration_days += days_int
        campaign.end_date += timedelta(days=days_int)
        campaign.save()

        return Response(
            {"detail": f"Campaign extended successfully by {days_int} days."}, 
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['post'], url_path='clone')
    def clone_campaign(self, request, pk=None):
        """
        POST: api/smartbuy/campaigns/:id/clone/
        Allows admins to clone a campaign.
        """
        campaign = self.get_object()
        
        start_date = request.data.get('start_date')
        duration_days = request.data.get('duration_days', campaign.duration_days)

        if not start_date:
            return Response(
                {"detail": "start_date is required to clone campaign."}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():  # type: ignore
            new_campaign = Campaign.objects.create(
                title=f"{campaign.title} (Clone)",
                description=campaign.description,
                vendor=campaign.vendor,
                product_image=campaign.product_image,
                total_quantity=campaign.total_quantity,
                available_quantity=campaign.total_quantity,
                duration_days=int(duration_days),
                start_date=start_date,
                status='active',
                upi_qr_image=campaign.upi_qr_image,
                created_by=request.user,
                token_deposit=campaign.token_deposit,
                cancellation_refund_amount=campaign.cancellation_refund_amount
            )

            # Copy pricing tiers
            for tier in campaign.pricing_tiers.all():
                PricingTier.objects.create(
                    campaign=new_campaign,
                    min_buyers=tier.min_buyers,
                    max_buyers=tier.max_buyers,
                    price=tier.price
                )

        serializer = CampaignSerializer(new_campaign)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='create-order')
    def create_order(self, request, pk=None):
        """
        POST: api/smartbuy/campaigns/:id/create-order/
        Creates a Cashfree payment order for the 10% deposit.
        Locks the campaign row via select_for_update() to prevent concurrent stock race conditions.
        Creates a pending CampaignRegistration and TokenPayment if they do not exist.
        """
        campaign_id = pk
        registration = None
        with transaction.atomic():  # type: ignore
            campaign = get_object_or_404(Campaign.objects.select_for_update(), id=campaign_id)

            if campaign.status != 'active' or campaign.end_date <= timezone.now():
                return Response(
                    {"detail": "This campaign is no longer active."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            existing = CampaignRegistration.objects.filter(campaign=campaign, employee=request.user).first()
            if existing:
                if existing.payment_status == 'approved':
                    return Response(
                        {"detail": "You are already registered and confirmed for this campaign."},
                        status=status.HTTP_409_CONFLICT
                    )
                elif existing.is_waitlisted:
                    return Response(
                        {"detail": "You are currently on the waitlist. Payments are only accepted after promotion."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                elif existing.payment_status in ['cancelled', 'rejected']:
                    # Delete the old cancelled/rejected registration to release the slot properly
                    existing.delete()
                    existing = None
                else:
                    registration = existing

            if not existing:
                if campaign.available_quantity <= 0:
                    return Response(
                        {"detail": "Campaign is sold out. Please join the waitlist instead."},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                token_amount = campaign.token_deposit

                campaign.available_quantity -= 1
                campaign.save()

                registration = CampaignRegistration.objects.create(
                    campaign=campaign,
                    employee=request.user,
                    token_amount=token_amount,
                    payment_status='pending',
                    is_waitlisted=False
                )
                check_and_close_campaign(campaign)

            if not registration:
                return Response(
                    {"detail": "Failed to resolve campaign registration details."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            timestamp = int(timezone.now().timestamp())
            order_id = f"BHEL_{campaign.id}_{request.user.employee_id}_{timestamp}"
            token_amount = registration.token_amount

            if token_amount == 0:
                # Bypassing payment gateway for 0-deposit campaigns
                if registration.payment_status != 'approved':
                    registration.payment_status = 'approved'
                    registration.payment_approved_at = timezone.now()
                    registration.save()

                    # Notify and email user immediately
                    create_notification(
                        recipient=request.user,
                        title="Booking Confirmed",
                        message=f"Your booking slot in Campaign '{campaign.title}' is confirmed.",
                        notification_type="campaign",
                        link=f"/smartbuy/{campaign.id}"
                    )
                    from .emails import send_payment_confirmed_email
                    transaction.on_commit(lambda: send_payment_confirmed_email(registration))

                return Response({
                    "payment_required": False,
                    "payment_status": "approved",
                    "detail": "Registration confirmed. No payment deposit required."
                }, status=status.HTTP_201_CREATED)

            cf = CashfreeService()
            frontend_url = os.environ.get('FRONTEND_URL', 'http://localhost:5173')
            return_url = f"{frontend_url}/payments/cashfree-return"

            backend_url = os.environ.get('BACKEND_URL', 'http://localhost:8000')
            notify_url = f"{backend_url}/api/payments/cashfree-webhook/"

            try:
                cf_order = cf.create_cashfree_order(
                    order_id=order_id,
                    amount=token_amount,
                    customer_id=request.user.employee_id,
                    customer_phone=request.user.mobile,
                    customer_email=request.user.email,
                    customer_name=request.user.name,
                    return_url=return_url,
                    notify_url=notify_url
                )

                payment_session_id = cf_order.payment_session_id

                TokenPayment.objects.update_or_create(
                    registration=registration,
                    defaults={
                        'amount': token_amount,
                        'cashfree_order_id': order_id,
                        'status': 'pending',
                        'cashfree_payment_id': None
                    }
                )

                # Send confirmation email for checkout initiation
                from .emails import send_order_initiated_email
                send_order_initiated_email(registration)

                return Response({
                    "payment_session_id": payment_session_id,
                    "order_id": order_id,
                    "amount": float(token_amount)
                }, status=status.HTTP_201_CREATED)

            except Exception as e:
                # Cashfree order creation failed — reclaim the campaign slot
                if not existing:
                    # Only reclaim slot if we created a new registration (not reusing existing)
                    campaign.available_quantity += 1
                    campaign.save()
                    registration.delete()
                import logging
                logger = logging.getLogger(__name__)
                logger.error(f"Failed to create Cashfree order for registration: {e}", exc_info=True)
                return Response(
                    {"detail": "Failed to initiate payment transaction with gateway. Please try again."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )


class RegisterCampaignView(APIView):
    """
    POST: api/smartbuy/campaigns/:id/register/
    DEPRECATED/DISABLED: Direct manual registrations are disabled.
    All users must go through Cashfree Payment Gateway (via create-order endpoint).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        return Response(
            {"detail": "Direct manual registration without payment gateway is disabled. Please initiate payment via checkout instead."},
            status=status.HTTP_400_BAD_REQUEST
        )


class JoinWaitlistView(APIView):
    """
    POST: api/smartbuy/campaigns/:id/waitlist/
    Allows the user to join the waitlist for a sold-out campaign.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        with transaction.atomic():  # type: ignore
            campaign = get_object_or_404(Campaign.objects.select_for_update(), id=id)

            if campaign.status != 'active' or campaign.end_date <= timezone.now():
                return Response(
                    {"detail": "This campaign is no longer active."}, 
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if already registered/waitlisted
            existing = CampaignRegistration.objects.filter(campaign=campaign, employee=request.user).first()
            if existing:
                if existing.payment_status == 'cancelled':
                    existing.delete()
                else:
                    return Response(
                        {"detail": "You are already registered or waitlisted for this campaign."}, 
                        status=status.HTTP_409_CONFLICT
                    )

            if campaign.available_quantity > 0:
                return Response(
                    {"detail": "Slots are still available. Please register instead."}, 
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get current max waitlist position
            max_pos = CampaignRegistration.objects.filter(
                campaign=campaign, 
                is_waitlisted=True
            ).aggregate(models.Max('waitlist_position'))['waitlist_position__max'] or 0

            registration = CampaignRegistration.objects.create(
                campaign=campaign,
                employee=request.user,
                token_amount=0,  # Waitlist is free until promoted
                payment_status='pending',
                is_waitlisted=True,
                waitlist_position=max_pos + 1
            )

            create_notification(
                recipient=request.user,
                title="Added to Waitlist",
                message=f"You have been added to the waitlist for '{campaign.title}' at position {max_pos + 1}.",
                notification_type="campaign",
                link=f"/smartbuy/{campaign.id}"
            )

            from .emails import send_waitlist_joined_email
            transaction.on_commit(lambda: send_waitlist_joined_email(registration))

        serializer = CampaignRegistrationSerializer(registration, context={'request': request})
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class SubmitPaymentView(APIView):
    """
    POST: api/smartbuy/campaigns/:id/submit-payment/
    DEPRECATED/DISABLED: Manual UPI screenshot payment uploads are disabled.
    All token payments are handled automatically by Cashfree Payment Gateway.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        return Response(
            {"detail": "Manual payment screenshot submission is disabled. All token payments are processed securely via the Cashfree Payment Gateway."},
            status=status.HTTP_400_BAD_REQUEST
        )


class CancelRegistrationView(APIView):
    """
    POST: api/smartbuy/campaigns/:id/cancel-registration/
    Cancels the user's campaign registration and calculates penalty refund.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        with transaction.atomic():  # type: ignore
            # 1. Lock Campaign first to prevent deadlock
            campaign = get_object_or_404(Campaign.objects.select_for_update(), id=id)

            # Security check: Prevent cancellation if the campaign has ended (time has expired)
            if campaign.end_date <= timezone.now():
                return Response(
                    {"detail": "Cannot cancel registration after the campaign has ended."}, 
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 2. Lock CampaignRegistration second
            registration = get_object_or_404(
                CampaignRegistration.objects.select_for_update(), 
                campaign=campaign, 
                employee=request.user
            )

            if registration.payment_status == 'cancelled':
                return Response(
                    {"detail": "Registration is already cancelled."}, 
                    status=status.HTTP_400_BAD_REQUEST
                )

            token_amount = registration.token_amount
            refund_amount = 0
            refund_status = 'not_applicable'

            # If user had a slot (was not waitlisted)
            if not registration.is_waitlisted:
                # Increment campaign inventory
                campaign.available_quantity += 1
                campaign.save()

                # Calculate refund based on campaign status/timing
                if campaign.status == 'active' and campaign.end_date > timezone.now():
                    # Refund the custom cancellation amount, capped at the paid token amount
                    refund_amount = min(campaign.cancellation_refund_amount, token_amount)
                    if has_token_payment(registration) and registration.token_payment.status == 'approved':
                        refund_status = 'pending'
                else:
                    # After closure: 0% refund
                    refund_amount = Decimal('0.00')

                # Promote next user from waitlist
                promote_from_waitlist(campaign.id)
            else:
                # Waitlisted users get a 100% refund if they made any payment
                refund_amount = token_amount
                if has_token_payment(registration) and registration.token_payment.status == 'approved':
                    refund_status = 'pending'

            # Update registration record
            registration.payment_status = 'cancelled'
            registration.cancellation_date = timezone.now()
            registration.refund_amount = refund_amount
            registration.refund_status = refund_status
            registration.save()

            # If there was a token payment, update it to cancelled/refunded
            if has_token_payment(registration):
                registration.token_payment.status = 'rejected'
                registration.token_payment.rejection_reason = "Cancelled by user"
                registration.token_payment.save()

            invalidate_campaign_price_cache(campaign.id)

            create_notification(
                recipient=request.user,
                title="Registration Cancelled",
                message=(
                    f"Your registration for '{campaign.title}' has been cancelled. "
                    f"Refund details: ₹{refund_amount:.2f} ({refund_status})."
                ),
                notification_type="campaign",
                link=f"/smartbuy/{campaign.id}"
            )

            from .emails import send_registration_cancelled_email
            transaction.on_commit(lambda: send_registration_cancelled_email(registration))

        serializer = CampaignRegistrationSerializer(registration, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class CampaignRegistrationsListView(APIView):
    """
    GET: api/smartbuy/campaigns/:id/registrations/
    Lists all registrations (admin only).
    """
    permission_classes = [IsAdminEmployee]

    def get(self, request, id):
        registrations = (
            CampaignRegistration.objects
            .filter(campaign_id=id)
            .select_related('employee', 'campaign')
            .order_by('reservation_date')
        )
        serializer = CampaignRegistrationSerializer(registrations, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


class CampaignWaitlistListView(APIView):
    """
    GET: api/smartbuy/campaigns/:id/waitlist-list/
    Lists all waitlisted users ordered by queue position (admin only).
    """
    permission_classes = [IsAdminEmployee]

    def get(self, request, id):
        waitlist = (
            CampaignRegistration.objects
            .filter(campaign_id=id, is_waitlisted=True)
            .select_related('employee', 'campaign')
            .order_by('waitlist_position')
        )
        serializer = CampaignRegistrationSerializer(waitlist, many=True, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)


def format_inr(val):
    if val is None:
        return "N/A"
    return f"INR {float(val):,.2f}"


class DownloadReceiptView(APIView):
    """
    GET: api/smartbuy/campaigns/:id/receipt/
    Generates a secure, branded PDF purchase receipt for a confirmed campaign reservation.
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        renderers = self.get_renderers()
        return (renderers[0], renderers[0].media_type)

    def get(self, request, id):
        import io
        from django.http import HttpResponse
        from django.utils import timezone
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        
        # 1. Fetch Campaign and Registration
        campaign = get_object_or_404(Campaign, id=id)
        registration = get_object_or_404(CampaignRegistration, campaign=campaign, employee=request.user)

        # 2. Security Check: Receipt is only available if payment is approved
        if registration.payment_status != 'approved':
            return Response(
                {"detail": "Receipt is only available for confirmed and paid reservations."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 3. Fetch details
        final_price = campaign.get_current_price()
        token_paid = registration.token_amount or Decimal("0.00")
        
        if final_price is not None:
            remaining_balance = max(Decimal("0.00"), final_price - token_paid)
            final_price_str = format_inr(final_price)
            remaining_balance_str = format_inr(remaining_balance)
        else:
            final_price_str = "N/A"
            remaining_balance_str = "N/A"

        token_paid_str = format_inr(token_paid)

        # 4. Generate PDF using ReportLab
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=2.0 * cm,
            leftMargin=2.0 * cm,
            topMargin=2.0 * cm,
            bottomMargin=2.0 * cm
        )

        styles = getSampleStyleSheet()
        
        # Define styles
        primary_color = colors.HexColor("#003366") # BHEL Blue
        
        title_style = ParagraphStyle(
            'ReceiptTitle',
            parent=styles['Heading1'],
            fontName='Helvetica-Bold',
            fontSize=18,
            leading=22,
            textColor=primary_color,
            alignment=1, # Center
            spaceAfter=15
        )

        subtitle_style = ParagraphStyle(
            'ReceiptSubtitle',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=10,
            leading=12,
            textColor=colors.HexColor("#555555"),
            alignment=1, # Center
            spaceAfter=25
        )

        section_heading = ParagraphStyle(
            'SectionHeading',
            parent=styles['Heading2'],
            fontName='Helvetica-Bold',
            fontSize=12,
            leading=15,
            textColor=primary_color,
            spaceBefore=12,
            spaceAfter=6,
            keepWithNext=True
        )

        body_style = ParagraphStyle(
            'BodyText',
            parent=styles['Normal'],
            fontName='Helvetica',
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#333333")
        )

        body_bold = ParagraphStyle(
            'BodyBold',
            parent=body_style,
            fontName='Helvetica-Bold'
        )

        price_label_style = ParagraphStyle(
            'PriceLabel',
            parent=body_style,
            fontSize=10,
            leading=14
        )

        price_val_style = ParagraphStyle(
            'PriceVal',
            parent=body_bold,
            fontSize=10,
            leading=14,
            alignment=2 # Right
        )

        total_label_style = ParagraphStyle(
            'TotalLabel',
            parent=body_bold,
            fontSize=11,
            leading=15,
            textColor=primary_color
        )

        total_val_style = ParagraphStyle(
            'TotalVal',
            parent=body_bold,
            fontSize=11,
            leading=15,
            textColor=primary_color,
            alignment=2 # Right
        )

        instruction_style = ParagraphStyle(
            'Instructions',
            parent=body_style,
            fontSize=8.5,
            leading=12,
            textColor=colors.HexColor("#444444")
        )

        elements = []

        # Header Elements
        elements.append(Paragraph("BHEL CONNECT - SMARTBUY", title_style))
        elements.append(Paragraph("OFFICIAL SLOT RESERVATION RECEIPT", subtitle_style))

        # Divider (represented securely by a Table with a bottom border)
        divider_table = Table([[""]], colWidths=[17.0 * cm])
        divider_table.setStyle(TableStyle([
            ('LINEBELOW', (0, 0), (-1, -1), 1.5, primary_color),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ]))
        elements.append(divider_table)
        elements.append(Spacer(1, 15))

        # Section: Reservation Metadata
        meta_data = [
            [Paragraph("Receipt Number:", body_bold), Paragraph(f"REC-SB-{registration.id:06d}", body_style)],
            [Paragraph("Issue Date:", body_bold), Paragraph(timezone.localtime(timezone.now()).strftime("%d-%b-%Y %I:%M %p IST"), body_style)],
            [Paragraph("Transaction ID:", body_bold), Paragraph(registration.cashfree_payment_id or "N/A", body_style)],
            [Paragraph("Payment Status:", body_bold), Paragraph("PAID / APPROVED", body_bold)]
        ]
        meta_table = Table(meta_data, colWidths=[4.0 * cm, 13.0 * cm])
        meta_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(meta_table)
        elements.append(Spacer(1, 15))

        # Section: Employee Details
        elements.append(Paragraph("Employee Information", section_heading))
        emp = registration.employee
        emp_data = [
            [Paragraph("Name:", body_bold), Paragraph(emp.name, body_style), Paragraph("Employee ID:", body_bold), Paragraph(emp.employee_id, body_style)],
            [Paragraph("Department:", body_bold), Paragraph(emp.department, body_style), Paragraph("Mobile:", body_bold), Paragraph(emp.mobile or "N/A", body_style)],
            [Paragraph("Official Email:", body_bold), Paragraph(emp.email, body_style), "", ""]
        ]
        emp_table = Table(emp_data, colWidths=[3.0 * cm, 5.5 * cm, 3.0 * cm, 5.5 * cm])
        emp_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('SPAN', (1, 2), (3, 2)),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(emp_table)
        elements.append(Spacer(1, 15))

        # Section: Campaign & Product Details
        elements.append(Paragraph("Campaign & Product Details", section_heading))
        vendor = campaign.vendor
        vendor_name = vendor.name if vendor else "N/A"
        vendor_contact = f"{vendor.contact_person} ({vendor.phone} / {vendor.email})" if vendor else "N/A"
        
        prod_data = [
            [Paragraph("Product Name:", body_bold), Paragraph(campaign.title, body_style)],
            [Paragraph("Supplier Vendor:", body_bold), Paragraph(vendor_name, body_style)],
            [Paragraph("Vendor Contact:", body_bold), Paragraph(vendor_contact, body_style)]
        ]
        prod_table = Table(prod_data, colWidths=[4.0 * cm, 13.0 * cm])
        prod_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(prod_table)
        elements.append(Spacer(1, 20))

        # Section: Price Calculation Table (Invoice style)
        price_headers = [Paragraph("Item / Description", body_bold), Paragraph("Amount", body_bold)]
        price_rows = [
            [
                Paragraph(f"<b>Dynamic Product Price</b> (Based on {campaign.confirmed_buyers_count} confirmed buyers)", price_label_style),
                Paragraph(final_price_str, price_val_style)
            ],
            [
                Paragraph("<b>Token Deposit Paid</b> (Advance booking amount)", price_label_style),
                Paragraph(f"- {token_paid_str}", price_val_style)
            ],
            [
                Paragraph("<b>Remaining Balance Due to Vendor</b> (To be paid directly to vendor)", total_label_style),
                Paragraph(remaining_balance_str, total_val_style)
            ]
        ]
        
        # Combine headers and rows
        all_price_data = [price_headers] + price_rows
        price_table = Table(all_price_data, colWidths=[12.0 * cm, 5.0 * cm])
        price_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
            ('LINEBELOW', (0, 0), (-1, 0), 1, primary_color),
            ('LINEBELOW', (0, 1), (-1, 1), 0.5, colors.HexColor("#E0E0E0")),
            ('LINEBELOW', (0, 2), (-1, 2), 1, primary_color),
            ('BACKGROUND', (0, 3), (-1, 3), colors.HexColor("#F9F9F9")),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(price_table)
        elements.append(Spacer(1, 25))

        # Section: Instructions Box
        inst_data = [[
            Paragraph(
                "<b>Important Instructions:</b><br/>"
                "1. Please present a printout or a digital copy of this receipt, along with your official BHEL Employee Identity Card, to the supplier vendor to claim your reserved item.<br/>"
                "2. You are required to pay the remaining balance due directly to the vendor at the time of delivery/claim.<br/>"
                "3. This is a computer-generated receipt, verified and secured by BHEL Connect, and does not require a physical signature.",
                instruction_style
            )
        ]]
        inst_table = Table(inst_data, colWidths=[17.0 * cm])
        inst_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F2F6FA")),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor("#B0C4DE")),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
            ('LEFTPADDING', (0, 0), (-1, -1), 12),
            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ]))
        elements.append(inst_table)

        # Build Document
        doc.build(elements)
        
        filename = f"smartbuy_receipt_campaign_{id}.pdf"
        response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
