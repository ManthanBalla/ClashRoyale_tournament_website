import logging
from django.utils import timezone
from django.db.models import Count
from django.contrib.auth.models import User
from .models import Profile, WithdrawalRequest, Transaction

logger = logging.getLogger('core.fraud')

def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

def track_user_fraud_metrics(request, user):
    """Update IP and device info for the user."""
    ip = get_client_ip(request)
    device = request.META.get('HTTP_USER_AGENT', '')[:255]
    
    profile = user.profile
    profile.last_ip = ip
    profile.device_fingerprint = device
    profile.save(update_fields=['last_ip', 'device_fingerprint'])
    
    # Simple multi-account check
    accounts_on_ip = Profile.objects.filter(last_ip=ip).exclude(user=user).count()
    if accounts_on_ip >= 3:
        profile.is_flagged = True
        profile.flag_reason = f"Multi-account detected: {accounts_on_ip + 1} accounts on IP {ip}"
        profile.save(update_fields=['is_flagged', 'flag_reason'])
        logger.warning(f"Fraud Alert: Multi-account detected for user {user.username} on IP {ip}")

def check_withdrawal_safety(user, amount):
    """
    Returns (is_safe, error_message)
    """
    profile = user.profile
    if profile.is_flagged:
        return False, "Your account is under review. Please contact support."
    
    # Max withdrawals per day
    day_ago = timezone.now() - timezone.timedelta(days=1)
    daily_count = WithdrawalRequest.objects.filter(user=user, requested_at__gte=day_ago).count()
    if daily_count >= 2:
        return False, "Maximum 2 withdrawal requests allowed per 24 hours."
    
    # Rapid withdrawal attempt (e.g. within 1 hour of last one)
    last_req = WithdrawalRequest.objects.filter(user=user).order_by('-requested_at').first()
    if last_req and timezone.now() < last_req.requested_at + timezone.timedelta(hours=1):
        return False, "Please wait at least 1 hour between withdrawal requests."
        
    return True, None

def check_creator_limits(user):
    """
    Limit tournaments per day for creators.
    """
    day_ago = timezone.now() - timezone.timedelta(days=1)
    daily_count = user.tournament_set.filter(created_at__gte=day_ago).count()
    
    limit = 5 # Default limit
    if user.profile.is_admin:
        return True, None
    if daily_count >= limit:
        return False, f"Daily tournament creation limit reached ({limit})."
    
    return True, None
