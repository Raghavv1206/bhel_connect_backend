import django_filters
from django.db.models import Q
from .models import Category, MarketplaceListing

class ListingFilter(django_filters.FilterSet):
    """
    Custom filter set for Marketplace listings.
    Supports search (title + description), category (and subcategories), 
    price range, condition, status, and ordering.
    """
    search = django_filters.CharFilter(method='filter_search')
    category = django_filters.CharFilter(method='filter_category')
    min_price = django_filters.NumberFilter(field_name='price', lookup_expr='gte')
    max_price = django_filters.NumberFilter(field_name='price', lookup_expr='lte')
    condition = django_filters.ChoiceFilter(choices=MarketplaceListing.CONDITION_CHOICES)
    status = django_filters.CharFilter(method='filter_status')

    class Meta:
        model = MarketplaceListing
        fields = ['search', 'category', 'min_price', 'max_price', 'condition', 'status']

    def filter_search(self, queryset, name, value):
        """
        Case-insensitive partial search on title and description using Q objects.
        """
        if not value:
            return queryset
        return queryset.filter(Q(title__icontains=value) | Q(description__icontains=value))

    def filter_category(self, queryset, name, value):
        """
        Filters listings by category slug, including all descendants.
        """
        try:
            category = Category.objects.get(slug=value)
            category_ids = [category.id] + list(category.children.values_list('id', flat=True))
            return queryset.filter(category_id__in=category_ids)
        except Category.DoesNotExist:
            return queryset.none()

    def filter_status(self, queryset, name, value):
        """
        Filters by status. Prevents regular users from querying pending, rejected, or sold listings 
        unless they are the owner/seller.
        """
        request = self.request
        user = request.user if request else None
        
        if not value:
            return queryset

        # If user is admin, allow full status querying
        if user and user.is_authenticated and user.is_admin:
            return queryset.filter(status=value)
            
        # Regular users: if they query pending/rejected/sold, only show theirs, else restrict to value
        if value in ['pending', 'rejected', 'sold']:
            if user and user.is_authenticated:
                return queryset.filter(status=value, seller=user)
            return queryset.none()
            
        return queryset.filter(status=value)

    @property
    def qs(self):
        """
        Custom qs property to apply default status='available' filtering if no status parameter is provided,
        and to ensure regular users do not see pending/rejected/sold listings of other users.
        """
        parent_qs = super().qs
        
        request = self.request
        user = request.user if request else None
        
        # Determine if status was explicitly requested
        params = request.query_params if request else {}
        status_param = params.get('status')
        
        # Apply default status='available' if not explicitly provided
        if not status_param:
            if user and user.is_authenticated:
                # Everyone (including admins) sees available by default + their own items of any status (excluding sold)
                parent_qs = parent_qs.filter(Q(status='available') | (Q(seller=user) & ~Q(status='sold')))
            else:
                parent_qs = parent_qs.filter(status='available')
                
        return parent_qs
