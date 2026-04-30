"""
Fintech-Grade Financial Services Layer for Clash Arena
=======================================================
All monetary logic lives here. Views and tasks MUST NOT contain
financial calculations, balance mutations, or ledger writes.

Architecture:
  execute_ledger_transaction()  — atomic, row-locked wallet update + immutable ledger entry
  distribute_rewards()          — idempotent tournament payout orchestrator
  monitor_suspicious_activity() — heuristic fraud detection

Design decisions:
  • Service layer (not signals) — explicit, testable, no hidden side-effects.
  • select_for_update() on Profile row — serialises concurrent wallet writes.
  • Unique reference_id per ledger entry — database-enforced idempotency.
  • Decimal with ROUND_HALF_UP everywhere — no floating-point drift.
  • balance_after snapshot in every Transaction — enables offline reconciliation.
"""

import logging
import uuid
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction, IntegrityError
from django.db.models import F

from .models import (
    Tournament, Match, Participant,
    User, Profile, Transaction, Notification,
)
from .utils import (
    send_notification, notify_all_participants,
    generate_winner_certificate,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------

class FraudDetectionError(Exception):
    """Raised when fraud heuristics block a payout."""
    pass


class LedgerError(Exception):
    """Raised on any financial integrity violation (insufficient funds, etc.)."""
    pass


# ---------------------------------------------------------------------------
# 1 · Core Ledger Transaction Executor
# ---------------------------------------------------------------------------

def execute_ledger_transaction(
    user, amount, transaction_type, reason, category,
    description='', tournament=None, reference_id=None,
):
    """
    Atomic, concurrency-safe wallet mutation with immutable ledger entry.

    Guarantees:
      • Row-level lock via select_for_update prevents race conditions.
      • balance_after snapshot enables offline reconciliation.
      • Unique reference_id prevents duplicate entries even on Celery retry.
      • Flagged accounts are blocked before any balance change.

    Args:
        user:             Django User instance (the wallet owner).
        amount:           Positive number (Decimal-safe).
        transaction_type: 'credit' | 'debit'.
        reason:           REASON_CHOICES key from Transaction model.
        category:         CATEGORY_CHOICES key from Transaction model.
        description:      Human-readable note stored in ledger.
        tournament:       Optional FK for audit trail.
        reference_id:     Idempotency key — MUST be unique per ledger entry.

    Returns:
        The created Transaction object.

    Raises:
        LedgerError:           On insufficient funds or invalid amount.
        FraudDetectionError:   If the account is flagged.
        IntegrityError:        If reference_id already exists (duplicate guard).
    """
    amount = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise LedgerError("Amount must be strictly positive.")

    if not reference_id:
        reference_id = str(uuid.uuid4())

    with transaction.atomic():
        # ── Lock the user's profile row ──────────────────────────────────
        profile = Profile.objects.select_for_update().get(user=user)

        # ── Fraud gate ───────────────────────────────────────────────────
        if profile.is_flagged:
            raise FraudDetectionError(
                f"Account {user.username} is flagged ({profile.flag_reason}). "
                f"Cannot process payout."
            )

        # ── Determine affected balance ───────────────────────────────────
        # Tournament winnings, creator commission, refunds → winnings_balance
        # Direct Cashfree top-ups, admin top-ups → deposit_balance
        is_deposit = reason in ('admin_topup', 'cashfree_deposit')

        # ── Calculate new balance ────────────────────────────────────────
        if transaction_type == 'credit':
            if is_deposit:
                new_balance = profile.deposit_balance + amount
                profile.deposit_balance = new_balance
            else:
                new_balance = profile.winnings_balance + amount
                profile.winnings_balance = new_balance
        elif transaction_type == 'debit':
            if is_deposit:
                if profile.deposit_balance < amount:
                    raise LedgerError(
                        f"Insufficient deposit balance for {user.username}: "
                        f"has ₹{profile.deposit_balance}, needs ₹{amount}"
                    )
                new_balance = profile.deposit_balance - amount
                profile.deposit_balance = new_balance
            else:
                if profile.winnings_balance < amount:
                    raise LedgerError(
                        f"Insufficient winnings balance for {user.username}: "
                        f"has ₹{profile.winnings_balance}, needs ₹{amount}"
                    )
                new_balance = profile.winnings_balance - amount
                profile.winnings_balance = new_balance
        else:
            raise LedgerError(f"Invalid transaction_type: {transaction_type}")

        profile.save(update_fields=['deposit_balance', 'winnings_balance'])

        # ── Create immutable ledger entry ────────────────────────────────
        tx = Transaction.objects.create(
            user=user,
            transaction_type=transaction_type,
            category=category,
            reason=reason,
            amount=amount,
            balance_after=new_balance,
            status='success',
            tournament=tournament,
            reference_id=reference_id,
            description=description,
        )

        logger.info(
            "LEDGER %s ₹%s to %s | reason=%s | ref=%s | balance_after=₹%s",
            transaction_type.upper(), amount, user.username,
            reason, reference_id, new_balance,
        )
        return tx


# ---------------------------------------------------------------------------
# 2 · Fraud Detection & Monitoring
# ---------------------------------------------------------------------------

def monitor_suspicious_activity(tournament, winner):
    """
    Heuristic fraud checks run BEFORE any money moves.
    Logs warnings; in the future this can auto-flag accounts or block payouts.
    """
    profile = winner.profile

    # Low trust score
    if profile.trust_score < 30:
        logger.warning(
            "FRAUD ALERT: Low trust score %d for user %s (tournament %s)",
            profile.trust_score, winner.username, tournament.id,
        )

    # Creator winning their own tournament
    if tournament.creator_id == winner.id:
        logger.warning(
            "FRAUD ALERT: Creator %s won their own tournament %s",
            winner.username, tournament.id,
        )

    # Same device fingerprint as creator
    creator_profile = tournament.creator.profile
    if (
        profile.device_fingerprint
        and creator_profile.device_fingerprint
        and profile.device_fingerprint == creator_profile.device_fingerprint
        and tournament.creator_id != winner.id
    ):
        logger.warning(
            "FRAUD ALERT: Winner %s shares device fingerprint with creator %s "
            "(tournament %s)",
            winner.username, tournament.creator.username, tournament.id,
        )

    # Same IP as creator
    if (
        profile.last_ip
        and creator_profile.last_ip
        and profile.last_ip == creator_profile.last_ip
        and tournament.creator_id != winner.id
    ):
        logger.warning(
            "FRAUD ALERT: Winner %s shares IP %s with creator %s "
            "(tournament %s)",
            winner.username, profile.last_ip,
            tournament.creator.username, tournament.id,
        )


# ---------------------------------------------------------------------------
# 3 · Idempotent Reward Distribution
# ---------------------------------------------------------------------------

def distribute_rewards(tournament_id):
    """
    Fintech-grade, idempotent reward distribution for PAID tournaments.

    Split: 70% winner · 18% creator · 12% platform.

    Safety guarantees:
      • tournament row locked with select_for_update — no concurrent payouts.
      • prize_distributed flag checked inside the lock — true idempotency.
      • Unique reference_ids per payout leg — DB rejects duplicates.
      • Entire block wrapped in transaction.atomic — partial payouts impossible.
      • Fraud heuristics evaluated before any money moves.

    Returns:
        (success: bool, message: str)
    """
    try:
        with transaction.atomic():
            # ── 1. Lock tournament row ───────────────────────────────────
            tournament = Tournament.objects.select_for_update().get(id=tournament_id)

            # ── 2. Idempotency guard ────────────────────────────────────
            if tournament.prize_distributed:
                logger.info(
                    "Idempotency guard: rewards already distributed for tournament %s",
                    tournament_id,
                )
                return False, "Rewards already distributed"

            # ── 3. Eligibility checks ───────────────────────────────────
            if not tournament.is_paid:
                return False, "Tournament is not paid"

            if tournament.status != 'ongoing':
                return False, "Tournament is not in 'ongoing' state"

            all_matches = list(Match.objects.filter(tournament=tournament))
            if not all_matches:
                return False, "No matches found"

            incomplete = [m for m in all_matches if not m.winner]
            if incomplete:
                return False, f"{len(incomplete)} match(es) still pending"

            # ── 4. Determine winner ─────────────────────────────────────
            wins = Counter(m.winner_id for m in all_matches)
            top_winner_id = wins.most_common(1)[0][0]
            top_winner = User.objects.select_for_update().get(id=top_winner_id)

            # ── 5. Validate participant count ───────────────────────────
            total_players = Participant.objects.filter(tournament=tournament).count()
            if total_players < tournament.min_players:
                return False, (
                    f"Insufficient participants ({total_players}/{tournament.min_players})"
                )

            # ── 6. Prize pool calculation (Decimal precision) ───────────
            total_collection = (
                Decimal(str(tournament.entry_fee))
                * Decimal(str(total_players))
            )

            prize_pool = (
                total_collection * Decimal('0.70')
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            remaining = total_collection - prize_pool

            creator_share = (
                remaining * Decimal('0.60')          # 60% of 30% = 18% total
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            admin_share = remaining - creator_share   # 12% total (remainder)

            # Sanity: total debits == total credits
            assert prize_pool + creator_share + admin_share == total_collection, (
                f"Integrity violation: {prize_pool}+{creator_share}+{admin_share}"
                f" != {total_collection}"
            )

            # ── 7. Fraud detection ──────────────────────────────────────
            monitor_suspicious_activity(tournament, top_winner)

            # ── 8. Execute ledger payouts ───────────────────────────────
            ref_id = f"PAYOUT_TRN_{tournament.id}"

            # Winner — 70%
            execute_ledger_transaction(
                user=top_winner,
                amount=prize_pool,
                transaction_type='credit',
                reason='tournament_win',
                category='winning',
                description=f'🏆 Prize — Won {tournament.name}',
                tournament=tournament,
                reference_id=f"{ref_id}_WIN",
            )

            # Creator — 18%
            execute_ledger_transaction(
                user=tournament.creator,
                amount=creator_share,
                transaction_type='credit',
                reason='creator_share',
                category='credit',
                description=f'🎮 Creator commission — {tournament.name}',
                tournament=tournament,
                reference_id=f"{ref_id}_CRT",
            )

            # Platform — 12%
            admin = User.objects.filter(profile__is_admin=True).first()
            if admin:
                execute_ledger_transaction(
                    user=admin,
                    amount=admin_share,
                    transaction_type='credit',
                    reason='admin_share',
                    category='credit',
                    description=f'⚙️ Platform fee — {tournament.name}',
                    tournament=tournament,
                    reference_id=f"{ref_id}_PF",
                )

            # ── 9. Finalise tournament state ────────────────────────────
            tournament.status = 'completed'
            tournament.prize_distributed = True
            tournament.save(update_fields=['status', 'prize_distributed'])

            # ── 10. Certificate & notifications ─────────────────────────
            generate_winner_certificate(top_winner, tournament=tournament)

            send_notification(
                top_winner, 'reward',
                f'🏆 You Won {tournament.name}!',
                f'₹{prize_pool} has been securely added to your winnings wallet.',
            )
            notify_all_participants(
                tournament, 'tournament_end',
                f'🏆 {tournament.name} Has Ended!',
                f'{top_winner.profile.display_name} has won the tournament!',
            )

            logger.info(
                "PAYOUT COMPLETE tournament=%s | winner=%s ₹%s | "
                "creator=%s ₹%s | platform ₹%s",
                tournament_id, top_winner.username, prize_pool,
                tournament.creator.username, creator_share, admin_share,
            )
            return True, "Success"

    except FraudDetectionError as fde:
        logger.error("Fraud blocked payout for tournament %s: %s", tournament_id, fde)
        return False, str(fde)

    except IntegrityError as ie:
        # Duplicate reference_id → payout already partially committed in a
        # previous attempt that was lost. This is the DB-level idempotency net.
        logger.error(
            "IntegrityError (likely duplicate payout) for tournament %s: %s",
            tournament_id, ie,
        )
        return False, "Duplicate payout blocked by database constraint"

    except AssertionError as ae:
        logger.critical("Financial integrity check failed: %s", ae)
        return False, "Financial integrity violation"

    except Exception as e:
        logger.exception(
            "Critical error distributing rewards for tournament %s", tournament_id
        )
        return False, "Internal Ledger Error"
