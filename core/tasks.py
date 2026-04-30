import logging
from celery import shared_task
from datetime import timedelta
from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail

from django.db import transaction
from django.utils import timezone
from .models import CreatorFollow, Notification, Tournament, CupMatch, CupMatchConfirmation, CupActionLog

import threading
logger = logging.getLogger(__name__)


def enqueue_task(task_func, *args):
    if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
        # In eager mode on low-CPU/free hosts, run task in daemon thread
        # so request-response is not blocked by network calls.
        threading.Thread(target=lambda: task_func.delay(*args), daemon=True).start()
    else:
        task_func.delay(*args)


from .utils import check_rate_limit, update_trust_score


def _advance_cup_match_winner(locked_match):
    if not locked_match.next_match:
        cup = locked_match.cup
        cup.status = 'completed'
        cup.save(update_fields=['status'])
        return
    nxt = locked_match.next_match
    if locked_match.next_slot == 1:
        nxt.player1 = locked_match.winner
        nxt.player1_label = locked_match.winner_label
    else:
        nxt.player2 = locked_match.winner
        nxt.player2_label = locked_match.winner_label
    nxt.save(update_fields=['player1', 'player2', 'player1_label', 'player2_label'])


@shared_task
def send_reward_code_email_task(
    user_email,
    username,
    code_text,
    description,
    code_id,
    tournament_name='Clash Arena Tournament',
    rank_label='Winner'
):
    if not user_email:
        return
    smtp_block_key = "smtp_unreachable_block"
    if cache.get(smtp_block_key):
        logger.warning("Skipping reward email due to temporary SMTP block code_id=%s", code_id)
        return
    try:
        subject = f'🎁 Congratulations! Reward from {tournament_name}'
        message = (
            f'Hi {username},\n\n'
            f'🎉 You have won {tournament_name}!\n'
            f'🏅 Your Rank: {rank_label}\n\n'
            f'🎁 Reward Code: {code_text}\n'
            f'📝 Reward Details: {description or "Tournament reward"}\n\n'
            'Please redeem your code as soon as possible.\n'
            'Thank you for competing on Clash Arena! 💥'
        )
        send_mail(
            subject=subject,
            message=message,
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


@shared_task
def process_cup_deadlines_task():
    now = timezone.now()
    due_matches = CupMatch.objects.filter(
        status='awaiting_confirmation',
        is_locked=False,
        deadline__isnull=False,
        deadline__lte=now
    ).select_related('cup', 'player1', 'player2', 'winner', 'next_match')

    for match in due_matches:
        with transaction.atomic():
            locked_match = CupMatch.objects.select_for_update().get(id=match.id)
            if locked_match.status != 'awaiting_confirmation' or locked_match.is_locked:
                continue
            confirmations = list(CupMatchConfirmation.objects.select_for_update().filter(match=locked_match))
            accepts = [c for c in confirmations if c.decision == 'accept']
            disputes = [c for c in confirmations if c.decision == 'dispute']
            if disputes:
                locked_match.status = 'disputed'
                locked_match.is_disputed = True
                locked_match.dispute_reason = disputes[0].dispute_reason or 'Deadline reached with dispute.'
                locked_match.save(update_fields=['status', 'is_disputed', 'dispute_reason'])
                CupActionLog.objects.create(
                    cup=locked_match.cup,
                    actor=None,
                    action_type='player_dispute',
                    match=locked_match,
                    message='Auto-marked disputed after deadline.'
                )
            elif accepts:
                locked_match.status = 'completed'
                locked_match.is_locked = True
                locked_match.result_source = 'dual_confirmation'
                locked_match.save(update_fields=['status', 'is_locked', 'result_source'])
                _advance_cup_match_winner(locked_match)
                CupActionLog.objects.create(
                    cup=locked_match.cup,
                    actor=None,
                    action_type='resolve_dispute',
                    match=locked_match,
                    target_user=locked_match.winner,
                    message='Auto-completed by deadline after one-sided acceptance.'
                )
            else:
                locked_match.status = 'disputed'
                locked_match.is_disputed = True
                locked_match.dispute_reason = 'No player response before deadline.'
                locked_match.save(update_fields=['status', 'is_disputed', 'dispute_reason'])
                
                update_trust_score(locked_match.player1, -10)
                update_trust_score(locked_match.player2, -10)
                
                CupActionLog.objects.create(
                    cup=locked_match.cup,
                    actor=None,
                    action_type='player_dispute',
                    match=locked_match,
                    message='Auto-marked disputed after no player response.'
                )


@shared_task
def send_cup_confirmation_reminders_task():
    now = timezone.now()
    upcoming_deadline = now + timedelta(hours=2)
    pending = CupMatch.objects.filter(
        status='awaiting_confirmation',
        is_locked=False,
        deadline__isnull=False,
        deadline__lte=upcoming_deadline,
        deadline__gt=now
    ).select_related('player1', 'player2')
    for match in pending:
        Notification.objects.create(
            user=match.player1,
            notification_type='general',
            title='Cup Confirmation Reminder',
            message=f'Confirm your match result before deadline for {match.cup.name}.'
        )
        Notification.objects.create(
            user=match.player2,
            notification_type='general',
            title='Cup Confirmation Reminder',
            message=f'Confirm your match result before deadline for {match.cup.name}.'
        )


@shared_task
def notify_unresolved_cup_disputes_task():
    stale_disputes = CupMatch.objects.filter(status='disputed', is_locked=False).select_related('cup')
    creator_ids = set(stale_disputes.values_list('cup__creator_id', flat=True))
    for creator_id in creator_ids:
        Notification.objects.create(
            user_id=creator_id,
            notification_type='general',
            title='Unresolved Cup Disputes',
            message='You have disputed cup matches pending resolution.'
        )
@shared_task(bind=True, max_retries=3, acks_late=True)
def distribute_tournament_prizes_task(self, tournament_id):
    """
    Retry-safe Celery task for prize distribution.
    Delegates all financial logic to the services layer.
    Only retries on transient errors (DB timeouts, connection issues).
    Permanent rejections (already distributed, fraud) are NOT retried.
    """
    from .services import distribute_rewards

    success, message = distribute_rewards(tournament_id)

    if success:
        logger.info("PAYOUT task completed for tournament %s", tournament_id)
        return

    # Permanent rejections — do NOT retry
    permanent_rejections = (
        "Rewards already distributed",
        "Tournament is not paid",
        "Duplicate payout blocked",
        "Financial integrity violation",
    )
    if any(msg in message for msg in permanent_rejections):
        logger.warning(
            "PAYOUT permanently rejected for tournament %s: %s",
            tournament_id, message,
        )
        return

    # Transient error — retry with exponential backoff
    retry_delay = 60 * (2 ** self.request.retries)  # 60s, 120s, 240s
    logger.warning(
        "PAYOUT retry %d/%d for tournament %s: %s (next in %ds)",
        self.request.retries + 1, self.max_retries,
        tournament_id, message, retry_delay,
    )
    raise self.retry(countdown=retry_delay, exc=Exception(message))

@shared_task
def reconcile_payments_task():
    """Daily reconciliation between Cashfree and internal wallet balances."""
    from .models import Payment, Profile, User
    from django.db.models import Sum
    
    total_cashfree = Payment.objects.filter(status='success', purpose='wallet_topup').aggregate(s=Sum('amount'))['s'] or 0
    total_deposits = Profile.objects.aggregate(s=Sum('deposit_balance'))['s'] or 0
    
    # This is a naive check (doesn't account for entry fee deductions from deposit)
    # A better check would be sum(Cashfree SUCCESS) == sum(credit transactions for reason=cashfree_deposit)
    from .models import Transaction
    total_tx_credits = Transaction.objects.filter(reason='cashfree_deposit', status='success').aggregate(s=Sum('amount'))['s'] or 0
    
    if total_cashfree != total_tx_credits:
        logger.error(f"Reconciliation Mismatch! Cashfree SUCCESS: {total_cashfree}, Internal Credits: {total_tx_credits}")
    else:
        logger.info(f"Reconciliation Success. Total: {total_cashfree}")
