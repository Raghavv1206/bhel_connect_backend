from django.core.management.base import BaseCommand
from users.models import Employee

class Command(BaseCommand):
    """
    Management command to seed the database with initial test users.
    Creates:
    - 1 Admin User (employee_id: EMP000001, email: admin@bhel.in)
    - 1 Regular Employee (employee_id: EMP000002, email: ramesh.kumar@bhel.in)
    """
    help = 'Seeds database with test employees (Admin and Regular)'

    def handle(self, *args, **options):
        self.stdout.write("Seeding test employees...")

        # 1. Seed Admin User
        admin_id = "EMP000001"
        admin_user, created = Employee.objects.get_or_create(
            employee_id=admin_id,
            defaults={
                'name': 'Admin User',
                'email': 'admin@bhel.in',
                'department': 'Information Technology',
                'mobile': '9876543210',
                'is_admin': True,
            }
        )
        if created:
            # Set a default password so they can access the standard /admin site if needed
            admin_user.set_password('admin123')
            admin_user.save()
            self.stdout.write(self.style.SUCCESS(f"Admin employee created: {admin_id} (Password: admin123)"))
        else:
            self.stdout.write(f"Admin employee {admin_id} already exists.")

        # 2. Seed Regular Employee
        employee_id = "EMP000002"
        regular_user, created = Employee.objects.get_or_create(
            employee_id=employee_id,
            defaults={
                'name': 'Ramesh Kumar',
                'email': 'ramesh.kumar@bhel.in',
                'department': 'Electrical Engineering',
                'mobile': '9876543211',
                'is_admin': False,
            }
        )
        if created:
            regular_user.set_password('user123')
            regular_user.save()
            self.stdout.write(self.style.SUCCESS(f"Regular employee created: {employee_id} (Password: user123)"))
        else:
            self.stdout.write(f"Regular employee {employee_id} already exists.")

        self.stdout.write(self.style.SUCCESS("Database seeding completed!"))
