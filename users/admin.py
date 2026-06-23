import csv
import io
import re
import logging
from django.contrib import admin
from django.contrib import messages
from django.urls import path
from django.shortcuts import render, redirect
from django.db import transaction
from django.utils import timezone

from adminpanel.admin import admin_site
from .models import Employee, OTPVerification

logger = logging.getLogger(__name__)

# CSV Validation configuration
REQUIRED_CSV_COLUMNS = {'employee_id', 'name', 'email', 'mobile', 'department'}
MAX_EMPLOYEE_ID_LEN = 20
MAX_NAME_LEN = 100
MAX_DEPARTMENT_LEN = 100
MOBILE_REGEX = re.compile(r'^\d{10}$')
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def _validate_csv_row(row_num: int, row: dict) -> list[str]:
    """Validates CSV row according to model constraints."""
    errors = []
    employee_id = (row.get('employee_id') or '').strip()
    name = (row.get('name') or '').strip()
    email = (row.get('email') or '').strip().lower()
    mobile = (row.get('mobile') or '').strip()
    department = (row.get('department') or '').strip()

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

    return errors


@admin.register(Employee, site=admin_site)
class EmployeeAdmin(admin.ModelAdmin):
    """
    Admin layout for managing BHEL Employees.
    Provides filtering by status/dept and search by ID/Name/Email.
    Includes custom Bulk CSV Employee Import functionality.
    """
    list_display = ('employee_id', 'name', 'email', 'department', 'is_active', 'is_admin', 'date_joined')
    list_filter = ('is_active', 'is_admin', 'department')
    search_fields = ('employee_id', 'name', 'email')
    ordering = ('employee_id',)

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('bulk-import/', self.admin_site.admin_view(self.bulk_import_view), name='users_employee_bulk_import'),
        ]
        return custom_urls + urls

    def bulk_import_view(self, request):
        """Custom admin view to upload and parse employees CSV file."""
        if not request.user.is_admin:
            messages.error(request, "Permission denied. Only BHEL admins can import users.")
            return redirect("admin:index")

        context = {
            'title': 'Bulk Employee Import',
            'opts': self.model._meta,
            'app_label': self.model._meta.app_label,
            'has_permission': True,
        }

        if request.method == 'POST':
            csv_file = request.FILES.get('file')

            if not csv_file:
                messages.error(request, "No file provided. Please select a CSV file.")
                return render(request, 'admin/users/employee/employee_import.html', context)

            if not csv_file.name.lower().endswith('.csv'):
                messages.error(request, "Invalid file format. Only .csv files are accepted.")
                return render(request, 'admin/users/employee/employee_import.html', context)

            if csv_file.size > MAX_FILE_SIZE_BYTES:
                messages.error(request, f"File size exceeds the 5 MB limit.")
                return render(request, 'admin/users/employee/employee_import.html', context)

            try:
                raw_bytes = csv_file.read()
                try:
                    csv_text = raw_bytes.decode('utf-8')
                except UnicodeDecodeError:
                    csv_text = raw_bytes.decode('latin-1')
            except Exception as e:
                messages.error(request, f"Failed to read the file: {str(e)}")
                return render(request, 'admin/users/employee/employee_import.html', context)

            try:
                reader = csv.DictReader(io.StringIO(csv_text))
                if reader.fieldnames is None:
                    messages.error(request, "The CSV file is empty or has no header row.")
                    return render(request, 'admin/users/employee/employee_import.html', context)

                normalized_headers = {h.strip().lower() for h in reader.fieldnames}
                missing_columns = REQUIRED_CSV_COLUMNS - normalized_headers
                if missing_columns:
                    messages.error(
                        request, 
                        f"CSV is missing required column(s): {', '.join(sorted(missing_columns))}. "
                        f"Expected columns: {', '.join(sorted(REQUIRED_CSV_COLUMNS))}."
                    )
                    return render(request, 'admin/users/employee/employee_import.html', context)

                rows = list(reader)
            except csv.Error as exc:
                messages.error(request, f"CSV parsing error: {exc}")
                return render(request, 'admin/users/employee/employee_import.html', context)

            if not rows:
                messages.error(request, "The CSV file contains headers but no data rows.")
                return render(request, 'admin/users/employee/employee_import.html', context)

            validated_rows = []
            row_errors = []
            seen_employee_ids = set()

            for row_num, row in enumerate(rows, start=2):
                clean_row = {k.strip().lower(): (v or '').strip() for k, v in row.items() if k}
                employee_id = clean_row.get('employee_id', '')

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

            created_count = 0
            updated_count = 0

            # Execute transactional import
            with transaction.atomic():
                for clean_row in validated_rows:
                    employee_id = clean_row['employee_id']
                    name = clean_row['name']
                    email = clean_row['email'].lower()
                    mobile = clean_row['mobile']
                    department = clean_row['department']

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
                            employee.set_unusable_password()
                            employee.save()
                            created_count += 1
                        else:
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
                            if changed:
                                employee.save()
                            updated_count += 1
                    except Exception as exc:
                        # Log database conflicts like email uniqueness constraints
                        row_errors.append({
                            "row": "DB Conflict",
                            "employee_id": employee_id,
                            "reason": f"Database error: {exc}"
                        })

            # Return stats and errors
            context['import_processed'] = True
            context['created_count'] = created_count
            context['updated_count'] = updated_count
            context['skipped_count'] = len(row_errors)
            context['row_errors'] = row_errors

            if created_count > 0 or updated_count > 0:
                messages.success(request, f"Import complete: {created_count} employee(s) created, {updated_count} employee(s) updated.")
            if row_errors:
                messages.warning(request, f"Skipped {len(row_errors)} row(s) due to validation errors.")

        return render(request, 'admin/users/employee/employee_import.html', context)


@admin.register(OTPVerification, site=admin_site)
class OTPVerificationAdmin(admin.ModelAdmin):
    """
    Admin layout for viewing and auditing OTP codes.
    Displays OTP status and attempt tracking.
    """
    list_display = ('employee', 'is_used', 'created_at', 'expires_at', 'attempt_count', 'is_expired', 'is_locked_out')
    list_filter = ('is_used', 'created_at')
    search_fields = ('employee__employee_id', 'employee__name')
    readonly_fields = ('created_at', 'expires_at')
    ordering = ('-created_at',)
