import logging
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail

from .models import CreatorFollow, Notification, Tournament

logger = logging.getLogger(__name__)


def check_rate_limit(key, limit=20, window_seconds=60):
    current = cache.get(key, 0)
    if current >= limit:
        return False
    if current == 0:
        cache.set(key, 1, timeout=window_seconds)
    else:
        cache.incr(key)
    return True


@shared_task
def send_reward_code_email_task(user_email, username, code_text, description, code_id):
    if not user_email:
        return
    smtp_block_key = "smtp_unreachable_block"
    if cache.get(smtp_block_key):
        logger.warning("Skipping reward email due to temporary SMTP block code_id=%s", code_id)
        return
    try:
        send_mail(
            subject='Your Reward Code - Clash Arena',
            message=(
                f'Hi {username},\n\n'
                f'Your reward code is: {code_text}\n\n'
                f'Description: {description}\n\n'
                'Thank you for playing on Clash Arena!'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user_email],
            fail_silently=False,
        )
    except OSError:
        cache.set(smtp_block_key, True, timeout=600)
        logger.exception("SMTP unreachable. Temporarily blocking email attempts code_id=%s", code_id)
    except Exception:
        logger.exception("Reward email task failed code_id=%s", code_id)


@shared_task
def notify_creator_followers_task(creator_id, tournament_id):
    try:
        tournament = Tournament.objects.select_related('creator').get(id=tournament_id, creator_id=creator_id)
        follows = CreatorFollow.objects.filter(
            creator_id=creator_id,
            notifications_enabled=True,
            follower__profile__notify_new_tournaments=True
        ).select_related('follower')
        for follow in follows:
            if not check_rate_limit(f"creator_notify_follower:{creator_id}:{follow.follower_id}", limit=5, window_seconds=3600):
                continue
            Notification.objects.create(
                user=follow.follower,
                notification_type='general',
                title=f'New tournament by {tournament.creator.username}',
                message=f'{tournament.creator.username} created "{tournament.name}". Join now!',
                tournament=tournament
            )
    except Exception:
        logger.exception("Follower notify task failed creator_id=%s tournament_id=%s", creator_id, tournament_id)
