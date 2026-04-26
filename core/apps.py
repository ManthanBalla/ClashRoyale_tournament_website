from django.apps import AppConfig
from django.db.models.signals import post_migrate

def ensure_sole_admin(sender, **kwargs):
    from django.contrib.auth.models import User
    from django.apps import apps
    Profile = apps.get_model('core', 'Profile')

    try:
        # Demote everyone who is not ClashArena_Admin
        User.objects.exclude(username='ClashArena_Admin').update(is_superuser=False, is_staff=False)
        Profile.objects.exclude(user__username='ClashArena_Admin').update(is_admin=False)

        # Ensure ClashArena_Admin has full admin privileges
        admin_user = User.objects.filter(username='ClashArena_Admin').first()
        if admin_user:
            if not admin_user.is_superuser or not admin_user.is_staff:
                admin_user.is_superuser = True
                admin_user.is_staff = True
                admin_user.save(update_fields=['is_superuser', 'is_staff'])
            
            if hasattr(admin_user, 'profile') and not admin_user.profile.is_admin:
                admin_user.profile.is_admin = True
                admin_user.profile.save(update_fields=['is_admin'])
    except Exception:
        pass  # Prevent crashing if tables are completely empty or not fully migrated yet

class CoreConfig(AppConfig):
    name = 'core'

    def ready(self):
        post_migrate.connect(ensure_sole_admin, sender=self)
