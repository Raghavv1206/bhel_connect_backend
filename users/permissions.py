from rest_framework.permissions import BasePermission

class IsAdminEmployee(BasePermission):
    """
    Custom permission to only allow access to BHEL administrative employees (is_admin = True).
    Used on all admin control panel views.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_admin)


class IsOwnerOrAdmin(BasePermission):
    """
    Custom permission to allow read/write access to the owner of the object or any administrative user.
    Handles multiple models dynamically by checking common owner fields:
    - Employee profile: checks if obj == request.user
    - Marketplace listings: checks if obj.seller == request.user
    - SmartBuy registrations: checks if obj.employee == request.user
    """
    def has_object_permission(self, request, view, obj):
        # Must be authenticated
        if not request.user or not request.user.is_authenticated:
            return False
            
        # Admin gets full access
        if request.user.is_admin:
            return True
            
        # Check ownership patterns
        if obj == request.user:
            return True
        if getattr(obj, 'seller', None) == request.user:
            return True
        if getattr(obj, 'employee', None) == request.user:
            return True
            
        return False


class IsOwnerOnly(BasePermission):
    """
    Custom permission to only allow the owner of the object to access/modify it.
    Admins are not granted access through this permission unless they are the owner.
    """
    def has_object_permission(self, request, view, obj):
        # Must be authenticated
        if not request.user or not request.user.is_authenticated:
            return False
            
        # Check ownership patterns
        if obj == request.user:
            return True
        if getattr(obj, 'seller', None) == request.user:
            return True
        if getattr(obj, 'employee', None) == request.user:
            return True
            
        return False
