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
@shared_task
def distribute_tournament_prizes_task(tournament_id):
    from .models import Tournament, Match, Participant, User
    from decimal import Decimal
    from collections import Counter
    from .utils import credit_wallet, send_notification, notify_all_participants

    try:
        with transaction.atomic():
            tournament = Tournament.objects.select_for_update().get(id=tournament_id)
            if tournament.prize_distributed:
                return

            all_matches = Match.objects.filter(tournament=tournament)
            total_players = Participant.objects.filter(tournament=tournament).count()
            
            if total_players < tournament.min_players:
                logger.warning(f"Tournament {tournament_id} has insufficient participants ({total_players}/{tournament.min_players}). Skipping prize distribution.")
                return

            if not all_matches.exists() or not all(m.winner for m in all_matches):
                return

            if tournament.is_paid and tournament.status == 'ongoing':
                wins = Counter(m.winner_id for m in all_matches if m.winner)
                top_winner_id = wins.most_common(1)[0][0]
                top_winner = User.objects.get(id=top_winner_id)

                total_players = Participant.objects.filter(tournament=tournament).count()
                total_collection = tournament.entry_fee * total_players
                
                # 70% to winner, 30% split (60% creator, 40% admin)
                prize_pool = (total_collection * Decimal('0.70')).quantize(Decimal('0.01'))
                remaining = total_collection - prize_pool
                creator_share = (remaining * Decimal('0.60')).quantize(Decimal('0.01'))
                admin_share = remaining - creator_share

                credit_wallet(top_winner, prize_pool, 'tournament_win', balance_type='winnings',
                            description=f'🏆 Prize — Won {tournament.name}', tournament=tournament, reference_id=str(tournament.id))
                
                credit_wallet(tournament.creator, creator_share, 'creator_share', balance_type='winnings',
                            description=f'🎮 Creator earnings — {tournament.name}', tournament=tournament, reference_id=str(tournament.id))
                
                admin = User.objects.filter(profile__is_admin=True).first()
                if admin:
                    credit_wallet(admin, admin_share, 'admin_share', balance_type='winnings',
                                description=f'⚙️ Platform fee — {tournament.name}', tournament=tournament, reference_id=str(tournament.id))

                tournament.status = 'completed'
                tournament.prize_distributed = True
                tournament.save(update_fields=['status', 'prize_distributed'])

                # Generate certificate
                from .utils import generate_winner_certificate
                generate_winner_certificate(top_winner, tournament=tournament)

                notify_all_participants(
                    tournament, 'tournament_end', f'🏆 {tournament.name} Has Ended!',
                    f'{tournament.name} has ended! Winner: {top_winner.username}.'
                )
    except Exception:
        logger.exception("Prize distribution task failed tournament_id=%s", tournament_id)

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
