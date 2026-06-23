import logging
import re

logger = logging.getLogger(__name__)

# URL patterns that map to model names for human-readable audit descriptions
URL_MODEL_MAP = [
    (r'/api/smartbuy/campaigns/(\d+)', 'Campaign', 'id'),
    (r'/api/smartbuy/vendors/(\d+)', 'Vendor', 'id'),
    (r'/api/marketplace/listings/(\d+)', 'MarketplaceListing', 'id'),
    (r'/api/admin/payments/(\d+)', 'TokenPayment', 'id'),
    (r'/api/admin/refunds/(\d+)', 'CampaignRegistration', 'id'),
    (r'/api/admin/users/bulk-import', 'Employee', None),
    (r'/api/users/profile', 'Employee', None),
]

# Action descriptions keyed by HTTP method
ACTION_DESCRIPTIONS = {
    'POST': 'Created or triggered action on',
    'PUT': 'Updated',
    'PATCH': 'Partially updated',
    'DELETE': 'Deleted',
}


def _get_client_ip(request):
    """
    Extract the real client IP address, respecting proxy headers.
    X-Forwarded-For can contain multiple IPs; take the first (original client).
    """
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        # Strip whitespace and take only the first IP
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR')
    # Basic sanity check — return None if empty or obviously invalid
    return ip if ip and len(ip) <= 45 else None


def _parse_url_for_model(path):
    """
    Match the request path against known URL patterns to extract:
      - target_model: the Django model class name affected
      - target_id: the primary key of the object (or None for collection endpoints)
    Returns a tuple (target_model, target_id) or ('Unknown', None) if no match.
    """
    for pattern, model_name, id_group in URL_MODEL_MAP:
        match = re.search(pattern, path)
        if match:
            target_id = None
            if id_group and match.lastindex:
                try:
                    target_id = int(match.group(1))
                except (IndexError, ValueError):
                    pass
            return model_name, target_id
    return 'Unknown', None


class AuditLogMiddleware:
    """
    Middleware that automatically logs all write operations (POST, PUT, PATCH, DELETE)
    performed by admin employees to the AuditLog database table.

    Key design decisions:
    - Only logs requests from authenticated admin users (is_admin=True).
    - Runs AFTER the view has returned a successful response (2xx status).
    - Never raises exceptions — log failure must never break the original request.
    - Captures IP address from X-Forwarded-For for reverse-proxy deployments.
    - Excluded paths: Django admin UI, auth login/logout/refresh, and token ops.
    """

    # Methods to audit (read operations are not logged)
    AUDIT_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

    # URL prefixes that should never be audited (internal/auth traffic)
    EXCLUDED_PREFIXES = (
        '/admin/',           # Django built-in admin
        '/api/auth/',        # Auth flow (login, OTP, logout, refresh)
        '/api/users/profile/',  # Profile updates (not admin operations)
        '/ws/',              # WebSocket handshakes
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Process the request and get the response from the view
        response = self.get_response(request)

        # Only audit write operations for successful responses
        if (
            request.method in self.AUDIT_METHODS
            and response.status_code in range(200, 300)
            and not any(request.path.startswith(prefix) for prefix in self.EXCLUDED_PREFIXES)
        ):
            self._create_audit_log(request, response)

        return response

    def _create_audit_log(self, request, response):
        """
        Creates an AuditLog record. Wrapped in a broad try/except so that
        any failure here never disrupts the main application flow.
        """
        try:
            user = getattr(request, 'user', None)

            # Only log actions performed by authenticated admin users
            if not user or not user.is_authenticated or not getattr(user, 'is_admin', False):
                return

            from adminpanel.models import AuditLog  # Local import to avoid circular deps

            target_model, target_id = _parse_url_for_model(request.path)

            # Build a human-readable description of the action
            action_verb = ACTION_DESCRIPTIONS.get(request.method, request.method)
            description = (
                f"{action_verb} {target_model}"
                + (f" (ID: {target_id})" if target_id else "")
                + f" via {request.method} {request.path}"
                + f" — HTTP {response.status_code}"
            )

            AuditLog.objects.create(
                admin_user=user,
                action=request.method,
                target_model=target_model,
                target_id=target_id,
                description=description,
                ip_address=_get_client_ip(request),
            )

        except Exception as exc:
            # Log the failure but never surface it to the user
            logger.error(
                "AuditLogMiddleware: Failed to write audit log for %s %s — %s",
                request.method,
                request.path,
                exc,
            )
