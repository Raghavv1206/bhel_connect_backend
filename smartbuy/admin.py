from django.contrib import admin
from django import forms
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.utils.html import format_html
from django.contrib.admin import helpers
from django.shortcuts import render, redirect

from adminpanel.admin import admin_site
from .models import Vendor, Campaign, PricingTier, CampaignRegistration, TokenPayment
from notifications.utils import create_notification
from smartbuy.utils import promote_from_waitlist, invalidate_campaign_price_cache

class PricingTierInline(admin.TabularInline):
    model = PricingTier
    extra = 1


@admin.register(Vendor, site=admin_site)
class VendorAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_person', 'email', 'phone', 'is_active', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('name', 'contact_person', 'email', 'phone', 'products_provided')
    ordering = ('name',)


@admin.register(Campaign, site=admin_site)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ('title', 'vendor', 'total_quantity', 'available_quantity', 'start_date', 'end_date', 'status', 'created_by')
    list_filter = ('status', 'start_date', 'end_date', 'vendor')
    search_fields = ('title', 'description', 'vendor__name')
    ordering = ('-created_at',)
    inlines = [PricingTierInline]
    readonly_fields = ('end_date', 'created_at', 'updated_at')

    def save_model(self, request, obj, form, change):
        if not change:  # On creation
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(CampaignRegistration, site=admin_site)
class CampaignRegistrationAdmin(admin.ModelAdmin):
    list_display = ('campaign', 'employee', 'payment_status', 'is_waitlisted', 'waitlist_position', 'refund_status', 'refund_amount', 'cancellation_date')
    list_filter = ('payment_status', 'is_waitlisted', 'refund_status', 'campaign')
    search_fields = ('employee__employee_id', 'employee__name', 'campaign__title', 'cashfree_order_id')
    ordering = ('-reservation_date',)
    actions = ['process_refunds_action']

    def process_refunds_action(self, request, queryset):
        queryset = queryset.filter(refund_status='pending')
        if not queryset.exists():
            self.message_user(request, "No pending refunds selected.", level=messages.WARNING)
            return

        success_count = 0
        for registration in queryset:
            try:
                with transaction.atomic():
                    reg_db = CampaignRegistration.objects.select_for_update().get(id=registration.id)
                    if reg_db.refund_status != 'pending':
                        continue

                    # Execute Cashfree API refund if paid via gateway
                    payment = getattr(reg_db, 'token_payment', None)
                    if payment and payment.status == 'approved' and payment.cashfree_order_id and not payment.cashfree_order_id.startswith('MANUAL-'):
                        from smartbuy.cashfree_service import CashfreeService
                        cf = CashfreeService()
                        refund_ref_id = f"REF-{reg_db.id}-{int(timezone.now().timestamp())}"
                        cf.create_cashfree_refund(
                            order_id=payment.cashfree_order_id,
                            refund_amount=reg_db.refund_amount,
                            refund_id=refund_ref_id,
                            refund_note=f"Refund for Campaign registration cancel: {reg_db.campaign.title}"
                        )

                    reg_db.refund_status = 'processed'
                    reg_db.save()

                    create_notification(
                        recipient=reg_db.employee,
                        title="Refund Disbursed",
                        message=(
                            f"Your refund of ₹{reg_db.refund_amount:.2f} for campaign "
                            f"'{reg_db.campaign.title}' has been processed and disbursed."
                        ),
                        notification_type="payment",
                        link="/profile",
                    )
                    success_count += 1
            except Exception as e:
                self.message_user(request, f"Error processing refund for {registration}: {str(e)}", level=messages.ERROR)

        if success_count > 0:
            self.message_user(request, f"Successfully processed {success_count} refund(s).", level=messages.SUCCESS)

    process_refunds_action.short_description = "Process selected pending refunds (triggers gateway refund if needed)"

    def save_model(self, request, obj, form, change):
        if change:
            original = CampaignRegistration.objects.get(pk=obj.pk)
            if original.refund_status == 'pending' and obj.refund_status == 'processed':
                with transaction.atomic():
                    reg_db = CampaignRegistration.objects.select_for_update().get(id=obj.id)
                    if reg_db.refund_status == 'pending':
                        # Execute Cashfree API refund if paid via gateway
                        payment = getattr(reg_db, 'token_payment', None)
                        if payment and payment.status == 'approved' and payment.cashfree_order_id and not payment.cashfree_order_id.startswith('MANUAL-'):
                            from smartbuy.cashfree_service import CashfreeService
                            cf = CashfreeService()
                            refund_ref_id = f"REF-{reg_db.id}-{int(timezone.now().timestamp())}"
                            cf.create_cashfree_refund(
                                order_id=payment.cashfree_order_id,
                                refund_amount=reg_db.refund_amount,
                                refund_id=refund_ref_id,
                                refund_note=f"Refund for Campaign registration cancel: {reg_db.campaign.title}"
                            )

                        reg_db.refund_status = 'processed'
                        reg_db.save()

                        create_notification(
                            recipient=reg_db.employee,
                            title="Refund Disbursed",
                            message=(
                                f"Your refund of ₹{reg_db.refund_amount:.2f} for campaign "
                                f"'{reg_db.campaign.title}' has been processed and disbursed."
                            ),
                            notification_type="payment",
                            link="/profile",
                        )
                        obj.refund_status = 'processed'

        super().save_model(request, obj, form, change)


@admin.register(TokenPayment, site=admin_site)
class TokenPaymentAdmin(admin.ModelAdmin):
    list_display = ('registration', 'amount', 'status', 'submitted_at', 'cashfree_order_id', 'cashfree_payment_id')
    list_filter = ('status', 'submitted_at')
    search_fields = ('registration__employee__employee_id', 'registration__employee__name', 'cashfree_order_id', 'cashfree_payment_id')
    ordering = ('-submitted_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_admin

