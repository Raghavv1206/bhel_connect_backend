from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.utils import timezone
from datetime import datetime, timedelta
from bhel_connect_backend.fields import CloudinaryField

class EmployeeManager(BaseUserManager):
    """
    Custom manager for Employee model where employee_id is the unique identifier.
    """
    def create_user(self, employee_id, name, email, department, password=None, **extra_fields):
        if not employee_id:
            raise ValueError('The Employee ID must be set')
        if not email:
            raise ValueError('The Email must be set')
            
        email = self.normalize_email(email)
        user = self.model(
            employee_id=employee_id,
            name=name,
            email=email,
            department=department,
            **extra_fields
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, employee_id, name, email, department, password=None, **extra_fields):
        extra_fields.setdefault('is_admin', True)
        
        if extra_fields.get('is_admin') is not True:
            raise ValueError('Superuser must have is_admin=True.')
            
        return self.create_user(employee_id, name, email, department, password, **extra_fields)

class Employee(AbstractBaseUser):
    """
    Custom user model representing a BHEL Employee.
    employee_id is the unique username field.
    """
    employee_id = models.CharField(max_length=20, primary_key=True, unique=True, db_index=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(max_length=100, unique=True)
    mobile = models.CharField(max_length=15, blank=True, null=True)
    department = models.CharField(max_length=100)
    
    is_active = models.BooleanField(default=True)
    is_admin = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    
    # Cloudinary storage for profile pictures
    profile_picture = CloudinaryField('image', folder='bhel/profiles', blank=True, null=True)

    objects = EmployeeManager()

    USERNAME_FIELD = 'employee_id'
    REQUIRED_FIELDS = ['name', 'email', 'department']

    class Meta:
        ordering = ['employee_id']
        verbose_name = 'Employee'
        verbose_name_plural = 'Employees'

    def __str__(self):
        return f"{self.name} ({self.employee_id})"

    @property
    def is_staff(self):
        # Admin employees are staff members in Django admin
        return self.is_admin

    @property
    def is_superuser(self):
        # Admin employees act as superusers in Django admin
        return self.is_admin

    def has_perm(self, perm, obj=None):
        return True

    def has_module_perms(self, app_label):
        return True


class OTPVerification(models.Model):
    """
    Stores OTP codes for employee login verification.
    OTPs are hashed before storage (like passwords) to prevent exposure if DB is compromised.
    Each OTP expires after 10 minutes and is single-use to prevent replay attacks.
    """

    # The employee requesting OTP-based login
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='otp_verifications',
        db_index=True,
        help_text='Employee who requested this OTP'
    )

    # SHA-256 hashed OTP code — raw OTP is never stored in the database
    otp_code = models.CharField(
        max_length=64,
        help_text='SHA-256 hashed 6-digit OTP code — never store raw OTPs'
    )

    # Timestamp when the OTP was created
    created_at = models.DateTimeField(auto_now_add=True)

    # Whether this OTP has already been used — set True immediately after successful verification
    is_used = models.BooleanField(
        default=False,
        help_text='Marked True after successful verification to prevent replay attacks'
    )

    # OTP expiration timestamp — auto-set to 10 minutes from creation in the save() method
    expires_at: datetime = models.DateTimeField(
        help_text='OTP expires 10 minutes after creation'
    )

    # Tracks failed verification attempts — reject after 5 failures to prevent brute-force
    attempt_count = models.IntegerField(
        default=0,
        help_text='Failed verification attempts — lockout after 5 to prevent brute-force'
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'OTP Verification'
        verbose_name_plural = 'OTP Verifications'
        # Index for fast lookups during verification: find latest unused OTP for an employee
        indexes = [
            models.Index(fields=['employee', 'is_used', '-created_at'], name='idx_otp_employee_lookup'),
        ]

    def __str__(self):
        status = 'Used' if self.is_used else ('Expired' if self.is_expired else 'Active')
        return f"OTP for {self.employee.name} ({status})"

    def save(self, *args, **kwargs):
        """Auto-set expires_at to 10 minutes from now on first save."""
        if not self.pk and not self.expires_at:
            self.expires_at = timezone.now() + timedelta(minutes=10)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        """Check if this OTP has passed its expiration time."""
        return timezone.now() > self.expires_at

    @property
    def is_locked_out(self):
        """Check if too many failed attempts have been made (max 5)."""
        return self.attempt_count >= 5

    @property
    def is_valid(self):
        """
        An OTP is valid only if it:
        1. Has not been used
        2. Has not expired
        3. Has not exceeded the attempt limit
        """
        return not self.is_used and not self.is_expired and not self.is_locked_out


class SavedProduct(models.Model):
    """
    Represents an employee's wishlisted/saved marketplace listing.
    An employee can save listings they are interested in for quick access later.
    unique_together prevents duplicate saves of the same listing by the same employee.
    """

    # The employee who saved this listing
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='saved_products',
        db_index=True,
        help_text='Employee who saved this listing'
    )

    # Reference to the marketplace listing — uses string reference to avoid circular import
    marketplace_listing = models.ForeignKey(
        'marketplace.MarketplaceListing',
        on_delete=models.CASCADE,
        related_name='saved_by',
        db_index=True,
        help_text='The marketplace listing that was saved/wishlisted'
    )

    # Timestamp when the product was saved
    saved_at = models.DateTimeField(
        auto_now_add=True,
        help_text='When the employee saved this listing'
    )

    class Meta:
        ordering = ['-saved_at']
        verbose_name = 'Saved Product'
        verbose_name_plural = 'Saved Products'
        # Prevent an employee from saving the same listing twice — DB-level enforcement
        unique_together = ('employee', 'marketplace_listing')

    def __str__(self):
        return f"{self.employee.name} saved {self.marketplace_listing.title}"
