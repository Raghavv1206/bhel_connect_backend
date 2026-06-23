from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    RequestOTPView, 
    VerifyOTPView, 
    LogoutView, 
    ProfileView,
    MyListingsView,
    MyPurchasesView,
    SavedProductsView,
    SavedProductDetailView,
    TokenHistoryView,
    EmployeeListView,
    EmployeeDetailView
)

urlpatterns = [
    # ── Auth Endpoints ──────────────────────────────────────────
    # Request OTP: ID + Email validation, OTP dispatch
    path('request-otp/', RequestOTPView.as_view(), name='request_otp'),
    # Verify OTP: Code validation, returns JWT tokens
    path('verify-otp/', VerifyOTPView.as_view(), name='verify_otp'),
    # Logout: Blacklists refresh token
    path('logout/', LogoutView.as_view(), name='logout'),
    # Token Refresh: Standard SimpleJWT sliding session endpoint
    path('refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # ── User Profile Endpoints ────────────────────────────────────
    # Profile: Fetch logged-in user profile or update mobile/picture
    path('profile/', ProfileView.as_view(), name='user_profile'),
    # Listings created by logged-in user
    path('my-listings/', MyListingsView.as_view(), name='my_listings'),
    # Campaigns registered by logged-in user
    path('my-purchases/', MyPurchasesView.as_view(), name='my_purchases'),
    # Saved/wishlisted marketplace items
    path('saved-products/', SavedProductsView.as_view(), name='saved_products'),
    # Remove from saved/wishlist
    path('saved-products/<int:listing_id>/', SavedProductDetailView.as_view(), name='saved_product_detail'),
    # Payment tokens history
    path('token-history/', TokenHistoryView.as_view(), name='token_history'),
    # Admin Employee Management
    path('employees/', EmployeeListView.as_view(), name='employee_list'),
    path('employees/<str:employee_id>/', EmployeeDetailView.as_view(), name='employee_detail'),
]
