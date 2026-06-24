from django.db import migrations

def hash_plaintext_passwords(apps, schema_editor):
    Employee = apps.get_model('users', 'Employee')
    # Loop over all employees and securely hash any plain-text passwords
    for employee in Employee.objects.all():
        password = employee.password
        # If password is set, not empty, and doesn't look like a standard hash prefix
        if password and not password.startswith('pbkdf2_sha256$') and not password.startswith('bcrypt$') and not password.startswith('argon2$'):
            from django.contrib.auth.hashers import make_password
            employee.password = make_password(password)
            employee.save()

def reverse_hash_passwords(apps, schema_editor):
    # Passwords cannot be unhashed, so this is a no-op when reversing
    pass

class Migration(migrations.Migration):
    dependencies = [
        ('users', '0003_alter_employee_profile_picture'),
    ]

    operations = [
        migrations.RunPython(hash_plaintext_passwords, reverse_code=reverse_hash_passwords),
    ]
