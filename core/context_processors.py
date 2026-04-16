from .models import Notification


def unread_notifications(request):
    if request.user.is_authenticated:
        qs = Notification.objects.filter(
            user=request.user, is_read=False
        )
        count = qs.count()
        recent = Notification.objects.filter(user=request.user).order_by('-created_at')[:8]
        return {'unread_count': count, 'recent_notifications': recent}
    return {'unread_count': 0, 'recent_notifications': []}