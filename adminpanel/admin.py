from django.contrib import admin
from django.contrib.admin import AdminSite
from django.utils import timezone
from django.db.models import Sum
from .models import AuditLog

class BHELConnectAdminSite(AdminSite):
    site_header = 'BHEL Connect Administration Portal'
    site_title = 'BHEL Connect Admin'
    index_title = 'BHEL Connect Operational Dashboard'

    def index(self, request, extra_context=None):
        # Lazy imports to avoid circular dependencies during startup
        from smartbuy.models import Campaign, TokenPayment
        from marketplace.models import MarketplaceListing
        from users.models import Employee

        now = timezone.now()
        
        # Safe aggregation of dashboard statistics
        try:
            active_campaigns = Campaign.objects.filter(status='active').count()
        except Exception:
            active_campaigns = 0

        try:
            pending_listings = MarketplaceListing.objects.filter(status='pending').count()
        except Exception:
            pending_listings = 0

        try:
            top_viewed = list(
                MarketplaceListing.objects.filter(status__in=['available', 'reserved', 'sold'])
                .order_by('-views')[:5]
            )
        except Exception:
            top_viewed = []

        try:
            total_rev_agg = TokenPayment.objects.filter(status='approved').aggregate(total=Sum('amount'))
            total_revenue = total_rev_agg['total'] or 0
        except Exception:
            total_revenue = 0

        try:
            new_users = Employee.objects.filter(
                date_joined__year=now.year,
                date_joined__month=now.month,
            ).count()
        except Exception:
            new_users = 0

        dashboard_stats = {
            'active_campaigns': active_campaigns,
            'pending_listings': pending_listings,
            'top_viewed_listings': top_viewed,
            'total_revenue': total_revenue,
            'new_users_this_month': new_users,
        }

        context = extra_context or {}
        context['dashboard_stats'] = dashboard_stats
        
        return super().index(request, extra_context=context)

admin_site = BHELConnectAdminSite(name='bhel_admin')


@admin.register(AuditLog, site=admin_site)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only admin view for audit logs — admins can search but not modify records."""
    list_display = ('admin_user', 'action', 'target_model', 'target_id', 'ip_address', 'timestamp')
    list_filter = ('action', 'target_model', 'timestamp')
    search_fields = ('admin_user__employee_id', 'admin_user__name', 'target_model', 'description')
    ordering = ('-timestamp',)
    readonly_fields = ('admin_user', 'action', 'target_model', 'target_id', 'description', 'ip_address', 'timestamp')

    def has_add_permission(self, request):
        # Audit logs should only be created by the middleware — never manually
        return False

    def has_change_permission(self, request, obj=None):
        # Audit logs are immutable
        return False

    def has_delete_permission(self, request, obj=None):
        # Only admin employees can delete audit logs (for GDPR compliance)
        return request.user.is_admin
