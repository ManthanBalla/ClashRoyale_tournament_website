from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.timezone import localtime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.core.mail import send_mail
from django.conf import settings
from datetime import timedelta, datetime
from decimal import Decimal
import json
import logging
import random
import pytz
import razorpay

from .models import Tournament, Participant, Match, Profile, WithdrawalRequest, RewardCode, CreatorMembership, Transaction, Notification, Payment


logger = logging.getLogger(__name__)


# ─── HELPERS ───────────────────────────────────────────────────────────────

def add_transaction(user, transaction_type, reason, amount, description=''):
    Transaction.objects.create(
        user=user,
        transaction_type=transaction_type,
        reason=reason,
        amount=amount,
        description=description
    )


def credit_wallet(user, amount, reason, description=''):
    user.profile.reward_balance += Decimal(str(amount))
    user.profile.save()
    add_transaction(user, 'credit', reason, amount, description)


def debit_wallet(user, amount, reason, description=''):
    user.profile.reward_balance -= Decimal(str(amount))
    user.profile.save()
    add_transaction(user, 'debit', reason, amount, description)


def send_notification(user, notification_type, title, message, tournament=None):
    Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        message=message,
        tournament=tournament
    )


def notify_all_participants(tournament, notification_type, title, message):
    participants = Participant.objects.filter(tournament=tournament)
    for p in participants:
        send_notification(p.user, notification_type, title, message, tournament)
    send_notification(tournament.creator, notification_type, title, message, tournament)


def parse_and_convert(dt_str, tz_choice):
    if not dt_str:
        return None
    try:
        naive = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M')
        if tz_choice == 'IST':
            ist = pytz.timezone('Asia/Kolkata')
            aware = ist.localize(naive)
        else:
            utc = pytz.utc
            aware = utc.localize(naive)
        return aware
    except Exception:
        return dt_str


def sync_tournament_status(tournament, now=None):
    """
    Keep tournament.status aligned with time:
    - before start: upcoming
    - between start and end: ongoing
    - after end: completed
    Cancelled/completed remain final states.
    """
    if tournament.status in ('cancelled', 'completed'):
        return tournament.status

    now = now or timezone.now()

    if tournament.end_time and now >= tournament.end_time:
        expected_status = 'completed'
    elif tournament.start_time and now >= tournament.start_time:
        expected_status = 'ongoing'
    else:
        expected_status = 'upcoming'

    if tournament.status != expected_status:
        tournament.status = expected_status
        tournament.save(update_fields=['status'])

    return tournament.status


# ─── AUTH ──────────────────────────────────────────────────────────────────

def home(request):
    tournaments = Tournament.objects.exclude(status='cancelled').order_by('-created_at')
    joined_tournaments = []
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(
            user=request.user
        ).values_list('tournament_id', flat=True)

    tournament_data = []
    for t in tournaments:
        sync_tournament_status(t)
        count = Participant.objects.filter(tournament=t).count()
        tournament_data.append({
            'tournament': t,
            'count': count,
            'prize_pool': t.entry_fee * count if t.is_paid else None
        })

    return render(request, 'home.html', {
        'tournament_data': tournament_data,
        'joined_tournaments': joined_tournaments,
    })


def login_view(request):
    if request.method == "POST":
        username = request.POST['username']
        password = request.POST['password']
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect('/')
        else:
            return render(request, 'auth/login.html', {'error': 'Invalid credentials'})
    return render(request, 'auth/login.html')


def register_view(request):
    if request.method == "POST":
        username = request.POST['username']
        email = request.POST['email']
        password = request.POST['password']

        if User.objects.filter(username=username).exists():
            return render(request, 'auth/register.html', {
                'error': 'Username already taken. Please choose a different username.'
            })
        if User.objects.filter(email=email).exists():
            return render(request, 'auth/register.html', {
                'error': 'An account with this email already exists.'
            })
        if not email:
            return render(request, 'auth/register.html', {'error': 'Email is required.'})

        User.objects.create_user(username=username, email=email, password=password)
        return redirect('/login/?registered=1')

    return render(request, 'auth/register.html')


def logout_view(request):
    logout(request)
    return redirect('/')


# ─── NOTIFICATIONS ─────────────────────────────────────────────────────────

@login_required
def notifications_view(request):
    notifications = Notification.objects.filter(
        user=request.user
    ).order_by('-created_at')[:50]

    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)

    return render(request, 'notifications.html', {
        'notifications': notifications
    })


@login_required
def mark_notification_read(request, notification_id):
    n = get_object_or_404(Notification, id=notification_id, user=request.user)
    n.is_read = True
    n.save()
    return redirect('/notifications/')


# ─── PROFILE ───────────────────────────────────────────────────────────────

@login_required
def profile_view(request):
    profile = request.user.profile
    withdrawal_requests = WithdrawalRequest.objects.filter(
        user=request.user
    ).order_by('-requested_at')
    memberships = CreatorMembership.objects.filter(
        user=request.user
    ).order_by('-started_at')
    transactions = Transaction.objects.filter(
        user=request.user
    ).order_by('-created_at')[:30]

    recent_topup = Transaction.objects.filter(
        user=request.user,
        reason='admin_topup'
    ).order_by('-created_at').first()

    if request.method == "POST":
        first_name = request.POST.get('first_name', '').strip()
        email = request.POST.get('email', '').strip()
        upi_id = request.POST.get('upi_id', '').strip()

        if email and email != request.user.email:
            if User.objects.filter(email=email).exclude(id=request.user.id).exists():
                return render(request, 'profile.html', {
                    'profile': profile,
                    'withdrawal_requests': withdrawal_requests,
                    'memberships': memberships,
                    'transactions': transactions,
                    'recent_topup': recent_topup,
                    'razorpay_key_id': settings.RAZORPAY_KEY_ID,
                    'error': 'This email is already used by another account.'
                })

        request.user.first_name = first_name
        request.user.email = email
        request.user.save()
        profile.upi_id = upi_id
        profile.save()

        return render(request, 'profile.html', {
            'profile': profile,
            'withdrawal_requests': withdrawal_requests,
            'memberships': memberships,
            'transactions': transactions,
            'recent_topup': recent_topup,
            'razorpay_key_id': settings.RAZORPAY_KEY_ID,
            'success': 'Profile updated successfully!'
        })

    return render(request, 'profile.html', {
        'profile': profile,
        'withdrawal_requests': withdrawal_requests,
        'memberships': memberships,
        'transactions': transactions,
        'recent_topup': recent_topup,
        'razorpay_key_id': settings.RAZORPAY_KEY_ID,
    })


def _get_razorpay_client():
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return None
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


@login_required
@require_POST
def create_razorpay_order(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    amount = payload.get('amount')
    try:
        amount_decimal = Decimal(str(amount))
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid amount.'}, status=400)

    if amount_decimal <= 0:
        return JsonResponse({'ok': False, 'error': 'Amount must be greater than 0.'}, status=400)
    if amount_decimal < Decimal('10.00') or amount_decimal > Decimal('50000.00'):
        return JsonResponse({'ok': False, 'error': 'Amount must be between ₹10 and ₹50,000.'}, status=400)

    client = _get_razorpay_client()
    if not client:
        logger.error("Razorpay keys missing while creating order for user_id=%s", request.user.id)
        return JsonResponse({'ok': False, 'error': 'Payment gateway not configured.'}, status=500)

    amount_paise = int(amount_decimal * 100)
    receipt = f"wallet_{request.user.id}_{int(timezone.now().timestamp())}"

    try:
        order = client.order.create({
            'amount': amount_paise,
            'currency': 'INR',
            'receipt': receipt,
            'payment_capture': 1,
            'notes': {
                'user_id': str(request.user.id),
                'purpose': 'wallet_topup',
            }
        })
    except Exception:
        logger.exception("Razorpay order creation failed for user_id=%s", request.user.id)
        return JsonResponse({'ok': False, 'error': 'Could not create payment order.'}, status=502)

    payment = Payment.objects.create(
        user=request.user,
        amount=amount_decimal,
        razorpay_order_id=order.get('id'),
        status='created',
        raw_payload={'create_order_response': order}
    )
    logger.info(
        "Payment order created payment_id=%s user_id=%s order_id=%s amount=%s",
        payment.id, request.user.id, payment.razorpay_order_id, payment.amount
    )

    return JsonResponse({
        'ok': True,
        'payment_id': payment.id,
        'order_id': order.get('id'),
        'amount_paise': amount_paise,
        'currency': 'INR',
        'key_id': settings.RAZORPAY_KEY_ID,
    })


@login_required
@require_POST
def verify_razorpay_payment(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    order_id = payload.get('razorpay_order_id')
    payment_id = payload.get('razorpay_payment_id')
    signature = payload.get('razorpay_signature')

    if not order_id or not payment_id or not signature:
        return JsonResponse({'ok': False, 'error': 'Missing payment verification fields.'}, status=400)

    client = _get_razorpay_client()
    if not client:
        logger.error("Razorpay keys missing while verifying payment for user_id=%s", request.user.id)
        return JsonResponse({'ok': False, 'error': 'Payment gateway not configured.'}, status=500)

    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature,
        })
    except Exception:
        logger.exception("Signature verification failed user_id=%s order_id=%s", request.user.id, order_id)
        Payment.objects.filter(
            user=request.user, razorpay_order_id=order_id, status='created'
        ).update(status='failed', failure_reason='signature_verification_failed')
        return JsonResponse({'ok': False, 'error': 'Signature verification failed.'}, status=400)

    try:
        gateway_payment = client.payment.fetch(payment_id)
    except Exception:
        logger.exception("Could not fetch payment from Razorpay payment_id=%s", payment_id)
        return JsonResponse({'ok': False, 'error': 'Unable to validate payment details.'}, status=502)

    try:
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(
                user=request.user,
                razorpay_order_id=order_id
            )

            if payment.wallet_credited:
                logger.info("Duplicate verify callback ignored payment_id=%s", payment.id)
                return JsonResponse({
                    'ok': True,
                    'message': 'Payment already processed.',
                    'new_balance': str(request.user.profile.reward_balance),
                })

            expected_amount_paise = int(payment.amount * 100)
            if (
                gateway_payment.get('status') != 'captured'
                or gateway_payment.get('order_id') != order_id
                or int(gateway_payment.get('amount', 0)) != expected_amount_paise
            ):
                payment.status = 'failed'
                payment.failure_reason = 'payment_mismatch_or_not_captured'
                payment.raw_payload = {
                    'verify_payload': payload,
                    'gateway_payment': gateway_payment,
                }
                payment.save(update_fields=['status', 'failure_reason', 'raw_payload', 'updated_at'])
                logger.warning(
                    "Payment mismatch user_id=%s payment_row=%s order_id=%s payment_id=%s",
                    request.user.id, payment.id, order_id, payment_id
                )
                return JsonResponse({'ok': False, 'error': 'Payment validation failed.'}, status=400)

            profile = Profile.objects.select_for_update().get(user=request.user)
            profile.reward_balance += payment.amount
            profile.save(update_fields=['reward_balance'])

            payment.razorpay_payment_id = payment_id
            payment.razorpay_signature = signature
            payment.status = 'success'
            payment.wallet_credited = True
            payment.raw_payload = {
                'verify_payload': payload,
                'gateway_payment': gateway_payment,
            }
            payment.failure_reason = None
            payment.save(update_fields=[
                'razorpay_payment_id',
                'razorpay_signature',
                'status',
                'wallet_credited',
                'raw_payload',
                'failure_reason',
                'updated_at',
            ])

            add_transaction(
                request.user,
                'credit',
                'admin_topup',
                payment.amount,
                f'💳 Razorpay wallet top-up successful — ₹{payment.amount}'
            )
    except Payment.DoesNotExist:
        logger.warning(
            "Payment row missing for verify user_id=%s order_id=%s payment_id=%s",
            request.user.id, order_id, payment_id
        )
        return JsonResponse({'ok': False, 'error': 'Order not found.'}, status=404)

    logger.info(
        "Wallet credited user_id=%s order_id=%s payment_id=%s amount=%s",
        request.user.id, order_id, payment_id, payment.amount
    )
    return JsonResponse({
        'ok': True,
        'message': 'Wallet top-up successful.',
        'new_balance': str(profile.reward_balance),
    })


@login_required
@require_POST
def mark_razorpay_payment_failed(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON payload.'}, status=400)

    order_id = payload.get('razorpay_order_id')
    reason = (payload.get('reason') or 'payment_failed')[:255]
    raw_error = payload.get('raw_error')
    if not order_id:
        return JsonResponse({'ok': False, 'error': 'Order ID is required.'}, status=400)

    updated = Payment.objects.filter(
        user=request.user,
        razorpay_order_id=order_id,
        wallet_credited=False
    ).update(
        status='failed',
        failure_reason=reason,
        raw_payload={'frontend_failure': raw_error} if raw_error else {'frontend_failure': reason}
    )
    logger.warning(
        "Payment failed marked user_id=%s order_id=%s updated=%s reason=%s",
        request.user.id, order_id, updated, reason
    )
    return JsonResponse({'ok': True})


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    webhook_secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not webhook_secret:
        return JsonResponse({'ok': False, 'error': 'Webhook is not configured.'}, status=400)

    signature = request.headers.get('X-Razorpay-Signature')
    body = request.body

    client = _get_razorpay_client()
    if not client:
        return JsonResponse({'ok': False, 'error': 'Payment gateway not configured.'}, status=500)

    try:
        client.utility.verify_webhook_signature(body, signature, webhook_secret)
    except Exception:
        logger.exception("Invalid Razorpay webhook signature.")
        return JsonResponse({'ok': False, 'error': 'Invalid webhook signature.'}, status=400)

    try:
        event = json.loads(body.decode('utf-8'))
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid webhook JSON.'}, status=400)

    logger.info("Razorpay webhook received event=%s", event.get('event'))
    return JsonResponse({'ok': True})


# ─── WITHDRAW ──────────────────────────────────────────────────────────────

@login_required
def withdraw_view(request):
    profile = request.user.profile

    if not profile.is_complete():
        return redirect('/profile/?incomplete=1')

    if request.method == "POST":
        amount = request.POST.get('amount')
        try:
            amount = Decimal(str(amount))
        except Exception:
            return render(request, 'withdraw.html', {
                'error': 'Invalid amount.', 'profile': profile
            })

        if amount <= 0:
            return render(request, 'withdraw.html', {
                'error': 'Amount must be greater than 0.', 'profile': profile
            })

        if amount > profile.reward_balance:
            return render(request, 'withdraw.html', {
                'error': f'Insufficient balance. Your balance is ₹{profile.reward_balance}.',
                'profile': profile
            })

        WithdrawalRequest.objects.create(
            user=request.user,
            amount=amount,
            upi_id=profile.upi_id
        )
        debit_wallet(
            request.user, amount, 'withdrawal',
            f'💸 Withdrawal request of ₹{amount} to {profile.upi_id}'
        )
        send_notification(
            request.user, 'wallet_credit',
            f'💸 Withdrawal Requested — ₹{amount}',
            f'Your withdrawal of ₹{amount} to {profile.upi_id} has been submitted. Admin will process within 24hrs.'
        )
        return redirect('/profile/?withdrawn=1')

    return render(request, 'withdraw.html', {'profile': profile})


# ─── SUBSCRIPTION ──────────────────────────────────────────────────────────

@login_required
def subscription_view(request):
    profile = request.user.profile
    memberships = CreatorMembership.objects.filter(
        user=request.user
    ).order_by('-started_at')

    if request.method == "POST":
        plan = request.POST.get('plan')
        upi_ref = request.POST.get('upi_ref', '').strip()

        if not upi_ref:
            return render(request, 'subscription.html', {
                'profile': profile,
                'memberships': memberships,
                'error': 'Please enter your UPI transaction reference number.'
            })

        try:
            send_mail(
                subject=f'🆕 Subscription Request - {request.user.username}',
                message=f'User: {request.user.username}\nEmail: {request.user.email}\nPlan: {plan}\nUPI Ref: {upi_ref}\n\nPlease verify and grant membership.',
                from_email=None,
                recipient_list=['manthanballa08@gmail.com'],
            )
        except Exception:
            pass

        return render(request, 'subscription.html', {
            'profile': profile,
            'memberships': memberships,
            'success': f'Your {plan} subscription request has been submitted! We will activate it within 24 hours after payment verification.'
        })

    return render(request, 'subscription.html', {
        'profile': profile,
        'memberships': memberships,
    })


# ─── TOURNAMENT ────────────────────────────────────────────────────────────

@login_required
def tournament_rules(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    count = Participant.objects.filter(tournament=tournament).count()
    prize_pool = tournament.entry_fee * count if tournament.is_paid else None

    if Participant.objects.filter(user=request.user, tournament=tournament).exists():
        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'tournament_rules.html', {
        'tournament': tournament,
        'count': count,
        'prize_pool': prize_pool
    })


@login_required
def join_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    now = localtime()

    # LOCK: already joined
    if Participant.objects.filter(user=request.user, tournament=tournament).exists():
        return redirect(f'/tournament/{tournament.id}/')

    # LOCK: not upcoming
    if tournament.status != 'upcoming':
        return redirect('/')

    # LOCK: deadline passed
    if tournament.join_deadline and now > localtime(tournament.join_deadline):
        return redirect('/?deadline=1')

    # LOCK: max players reached
    current_count = Participant.objects.filter(tournament=tournament).count()
    if current_count >= tournament.max_players:
        return redirect('/?full=1')

    # PAID TOURNAMENT
    if tournament.is_paid:
        if request.method == "POST":
            agreed = request.POST.get('agreed')
            if not agreed:
                return redirect(f'/rules/{tournament.id}/')

            profile = request.user.profile
            if profile.reward_balance < tournament.entry_fee:
                return render(request, 'tournament_rules.html', {
                    'tournament': tournament,
                    'count': current_count,
                    'prize_pool': tournament.entry_fee * current_count,
                    'error': f'Insufficient balance. You need ₹{tournament.entry_fee} to join. Your balance is ₹{profile.reward_balance}.'
                })

            # deduct fee
            debit_wallet(
                request.user,
                tournament.entry_fee,
                'tournament_join',
                f'🎮 Entry fee for {tournament.name}'
            )

            # notify deduction
            send_notification(
                request.user,
                'wallet_credit',
                f'💸 ₹{tournament.entry_fee} Deducted — {tournament.name}',
                f'₹{tournament.entry_fee} entry fee deducted for joining {tournament.name}. New balance: ₹{request.user.profile.reward_balance}',
                tournament
            )

            tournament.prize_pool += tournament.entry_fee
            tournament.save()

            Participant.objects.create(
                user=request.user,
                tournament=tournament,
                fee_paid=True
            )
            return redirect(f'/tournament/{tournament.id}/')

        return redirect(f'/rules/{tournament.id}/')

    # FREE TOURNAMENT
    # FREE TOURNAMENT — password reveal
    show_password = False
    if tournament.start_time:
        now_utc = timezone.now()
        if now_utc >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    if tournament.password:
        if request.method == "POST":
            entered_password = request.POST.get('password')
            if entered_password == tournament.password:
                Participant.objects.create(user=request.user, tournament=tournament)
                return redirect(f'/tournament/{tournament.id}/')
            else:
                return render(request, 'enter_password.html', {
                    'tournament': tournament,
                    'error': 'Wrong password ❌',
                    'show_password': show_password
                })
        return render(request, 'enter_password.html', {
            'tournament': tournament,
            'show_password': show_password
        })

    Participant.objects.create(user=request.user, tournament=tournament)
    return redirect(f'/tournament/{tournament.id}/')


@login_required
def create_tournament(request):
    profile = request.user.profile

    # ADMIN: unlimited access, no restrictions
    if not profile.is_admin and not profile.is_creator:
        return redirect('/')

    # CREATOR: check plan limits only
    if not profile.is_admin and not profile.can_create_tournament():
        return render(request, 'create_tournament.html', {
            'error': 'You have reached your tournament limit or your plan has expired. Please upgrade your plan.'
        })

    if request.method == "POST":
        name = request.POST['name']
        description = request.POST['description']
        rules = request.POST.get('rules', '')
        password = request.POST.get('password') or None
        reward = request.POST.get('reward', '')
        reward_type = request.POST.get('reward_type', 'other')
        start_time_raw = request.POST.get('start_time')
        end_time_raw = request.POST.get('end_time') or None
        join_deadline_raw = request.POST.get('join_deadline') or None
        proof_image = request.FILES.get('proof_image')
        is_paid = request.POST.get('is_paid') == 'paid'
        entry_fee = request.POST.get('entry_fee', 0) or 0
        min_players = request.POST.get('min_players', 2) or 2
        max_players = request.POST.get('max_players', 100) or 100
        show_participants = request.POST.get('show_participants') == 'on'
        timezone_choice = request.POST.get('timezone_choice', 'IST')

        start_time = parse_and_convert(start_time_raw, timezone_choice)
        end_time = parse_and_convert(end_time_raw, timezone_choice)
        join_deadline = parse_and_convert(join_deadline_raw, timezone_choice)

        Tournament.objects.create(
            name=name,
            description=description,
            rules=rules,
            password=password,
            reward=reward,
            reward_type=reward_type,
            start_time=start_time,
            end_time=end_time,
            join_deadline=join_deadline,
            proof_image=proof_image,
            creator=request.user,
            is_paid=is_paid,
            entry_fee=entry_fee,
            min_players=min_players,
            max_players=max_players,
            show_participants=show_participants,
        )

        # only increment for non-admin creators
        if not profile.is_admin:
            profile.tournaments_created_this_month += 1
            profile.save()

        return redirect('/')

    return render(request, 'create_tournament.html')


@login_required
def delete_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        if tournament.is_paid:
            participants = Participant.objects.filter(tournament=tournament, fee_paid=True)
            for p in participants:
                credit_wallet(
                    p.user,
                    tournament.entry_fee,
                    'tournament_refund',
                    f'♻️ Refund — {tournament.name} deleted by organizer'
                )
                send_notification(
                    p.user, 'tournament_cancel',
                    f'♻️ Refund — {tournament.name} Deleted',
                    f'Tournament {tournament.name} was deleted. ₹{tournament.entry_fee} refunded to your wallet.',
                    tournament
                )
        tournament.delete()
        return redirect('/')

    return redirect('/')


@login_required
def edit_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        timezone_choice = request.POST.get('timezone_choice', 'IST')

        tournament.name = request.POST['name']
        tournament.description = request.POST['description']
        tournament.rules = request.POST.get('rules', '')
        tournament.password = request.POST.get('password') or None
        tournament.reward = request.POST.get('reward', '')
        tournament.reward_type = request.POST.get('reward_type', 'other')
        tournament.start_time = parse_and_convert(request.POST.get('start_time'), timezone_choice)
        tournament.end_time = parse_and_convert(request.POST.get('end_time') or None, timezone_choice)
        tournament.join_deadline = parse_and_convert(request.POST.get('join_deadline') or None, timezone_choice)
        tournament.min_players = request.POST.get('min_players', 2) or 2
        tournament.max_players = request.POST.get('max_players', 100) or 100
        tournament.show_participants = request.POST.get('show_participants') == 'on'

        if request.FILES.get('proof_image'):
            tournament.proof_image = request.FILES.get('proof_image')

        tournament.save()
        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'edit_tournament.html', {'tournament': tournament})


def tournament_detail(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)
    sync_tournament_status(tournament)
    participants = Participant.objects.filter(tournament=tournament)
    matches = Match.objects.filter(tournament=tournament)
    now = localtime()
    count = participants.count()

    show_password = False
    if tournament.start_time:
        now_utc = timezone.now()
        if now_utc >= tournament.start_time - timedelta(minutes=10):
            show_password = True

    joined_tournaments = []
    is_participant = False
    if request.user.is_authenticated:
        joined_tournaments = Participant.objects.filter(
            user=request.user
        ).values_list('tournament_id', flat=True)
        is_participant = Participant.objects.filter(
            user=request.user, tournament=tournament
        ).exists()

    prize_pool = tournament.entry_fee * count if tournament.is_paid else None

    return render(request, 'tournament_detail.html', {
        'tournament': tournament,
        'participants': participants,
        'matches': matches,
        'show_password': show_password,
        'joined_tournaments': joined_tournaments,
        'count': count,
        'prize_pool': prize_pool,
        'is_participant': is_participant,
    })


@login_required
def upload_results(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        if request.FILES.get('result_screenshot'):
            tournament.result_screenshot = request.FILES.get('result_screenshot')
        if request.FILES.get('reward_screenshot'):
            tournament.reward_screenshot = request.FILES.get('reward_screenshot')
        tournament.status = 'completed'
        tournament.save()

        notify_all_participants(
            tournament,
            'result_uploaded',
            f'📸 Results Uploaded — {tournament.name}',
            f'The organizer has uploaded results for {tournament.name}! Check the leaderboard and reward proof now.'
        )

        return redirect(f'/tournament/{tournament.id}/')

    return render(request, 'upload_results.html', {'tournament': tournament})


@login_required
def cancel_tournament(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        reason = request.POST.get('cancel_reason', 'Cancelled by organizer')

        if tournament.is_paid:
            participants = Participant.objects.filter(tournament=tournament, fee_paid=True)
            for p in participants:
                credit_wallet(
                    p.user,
                    tournament.entry_fee,
                    'tournament_refund',
                    f'♻️ Refund — {tournament.name} cancelled: {reason}'
                )

        notify_all_participants(
            tournament,
            'tournament_cancel',
            f'❌ Tournament Cancelled — {tournament.name}',
            f'Reason: {reason}' +
            (f'\n♻️ ₹{tournament.entry_fee} has been refunded to your wallet.' if tournament.is_paid else '')
        )

        tournament.status = 'cancelled'
        tournament.cancel_reason = reason
        tournament.save()
        return redirect('/')

    return redirect('/')


@login_required
def generate_matches(request, tournament_id):
    tournament = get_object_or_404(Tournament, id=tournament_id)

    if request.user != tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    participants = list(
        Participant.objects.filter(tournament=tournament).values_list('user', flat=True)
    )

    # check minimum players
    if len(participants) < tournament.min_players:
        if tournament.is_paid:
            for uid in participants:
                u = User.objects.get(id=uid)
                credit_wallet(
                    u,
                    tournament.entry_fee,
                    'tournament_refund',
                    f'♻️ Refund — {tournament.name} cancelled (min players not reached)'
                )

        tournament.status = 'cancelled'
        tournament.cancel_reason = f'Minimum {tournament.min_players} players required but only {len(participants)} joined.'
        tournament.save()

        notify_all_participants(
            tournament,
            'tournament_cancel',
            f'❌ Tournament Cancelled — {tournament.name}',
            f'Not enough players joined. Minimum {tournament.min_players} required but only {len(participants)} joined.' +
            (f'\n♻️ ₹{tournament.entry_fee} has been refunded to your wallet.' if tournament.is_paid else '')
        )

        return redirect(f'/tournament/{tournament.id}/?cancelled=1')

    random.shuffle(participants)
    Match.objects.filter(tournament=tournament).delete()

    for i in range(0, len(participants), 2):
        if i + 1 < len(participants):
            Match.objects.create(
                tournament=tournament,
                player1_id=participants[i],
                player2_id=participants[i + 1],
                round_number=1
            )

    tournament.status = 'ongoing'
    tournament.save()

    # notify start + end time if set
    end_msg = ''
    if tournament.end_time:
        end_msg = f' Tournament ends at {tournament.end_time.strftime("%d %b %Y, %I:%M %p")} UTC.'

    notify_all_participants(
        tournament,
        'tournament_start',
        f'🔴 {tournament.name} is Now Live!',
        f'Your tournament has started! Check your match and play now.{end_msg}'
    )

    return redirect(f'/tournament/{tournament.id}/')


@login_required
def submit_result(request, match_id):
    match = get_object_or_404(Match, id=match_id)

    if request.user != match.tournament.creator and not request.user.profile.is_admin:
        return redirect('/')

    if request.method == "POST":
        winner_id = request.POST.get('winner')
        winner = User.objects.get(id=winner_id)
        match.winner = winner
        match.save()

        tournament = match.tournament
        all_matches = Match.objects.filter(tournament=tournament)

        if all_matches.count() > 0 and all(m.winner for m in all_matches):
            if tournament.is_paid and tournament.status == 'ongoing':
                from collections import Counter
                wins = Counter(m.winner_id for m in all_matches if m.winner)
                top_winner_id = wins.most_common(1)[0][0]
                top_winner = User.objects.get(id=top_winner_id)

                total_players = Participant.objects.filter(tournament=tournament).count()
                total_collection = tournament.entry_fee * total_players
                prize_pool = (total_collection * Decimal('0.70')).quantize(Decimal('0.01'))
                remaining = total_collection - prize_pool
                creator_share = (remaining * Decimal('0.60')).quantize(Decimal('0.01'))
                admin_share = remaining - creator_share

                # pay winner
                credit_wallet(
                    top_winner, prize_pool,
                    'tournament_win',
                    f'🏆 Prize — Won {tournament.name}'
                )
                send_notification(
                    top_winner, 'wallet_credit',
                    f'🏆 You Won! ₹{prize_pool} Added',
                    f'Congratulations! You won {tournament.name}. ₹{prize_pool} has been added to your wallet.',
                    tournament
                )

                # pay creator
                credit_wallet(
                    tournament.creator, creator_share,
                    'creator_share',
                    f'🎮 Creator earnings — {tournament.name}'
                )
                send_notification(
                    tournament.creator, 'wallet_credit',
                    f'💰 Creator Earnings — ₹{creator_share}',
                    f'₹{creator_share} creator share added from {tournament.name}.',
                    tournament
                )

                # pay admin
                admin = User.objects.filter(profile__is_admin=True).first()
                if admin:
                    credit_wallet(
                        admin, admin_share,
                        'admin_share',
                        f'⚙️ Platform fee — {tournament.name}'
                    )

                tournament.status = 'completed'
                tournament.save()

                notify_all_participants(
                    tournament,
                    'tournament_end',
                    f'🏆 {tournament.name} Has Ended!',
                    f'{tournament.name} has ended! Winner: {top_winner.username}. Check the results now.'
                )

    return redirect(f'/tournament/{match.tournament.id}/')


# ─── ADMIN ─────────────────────────────────────────────────────────────────

@login_required
def creator_admin(request):
    if not request.user.profile.is_admin:
        return redirect('/')

    tournaments = Tournament.objects.all().order_by('-created_at')
    users = User.objects.all().order_by('-date_joined')
    withdrawal_requests = WithdrawalRequest.objects.all().order_by('-requested_at')
    reward_codes = RewardCode.objects.all().order_by('-created_at')
    memberships = CreatorMembership.objects.all().order_by('-started_at')
    all_transactions = Transaction.objects.all().order_by('-created_at')[:50]

    tournament_data = []
    for t in tournaments:
        sync_tournament_status(t)
        count = Participant.objects.filter(tournament=t).count()
        tournament_data.append({'tournament': t, 'count': count})

    return render(request, 'admin_panel.html', {
        'tournament_data': tournament_data,
        'users': users,
        'withdrawal_requests': withdrawal_requests,
        'reward_codes': reward_codes,
        'memberships': memberships,
        'all_transactions': all_transactions,
    })


@login_required
def topup_wallet(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        u = get_object_or_404(User, id=user_id)
        amount = request.POST.get('amount')
        try:
            amount = Decimal(str(amount))
            if amount > 0:
                credit_wallet(
                    u, amount,
                    'admin_topup',
                    f'💰 ₹{amount} added to your wallet by Admin'
                )
                send_notification(
                    u, 'wallet_credit',
                    f'💰 Wallet Top Up — ₹{amount}',
                    f'₹{amount} has been added to your wallet by Admin. Check your balance!'
                )
        except Exception:
            pass
    return redirect('/creator-admin/')


@login_required
def approve_withdrawal(request, withdrawal_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    w = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
    w.status = 'approved'
    w.save()
    send_notification(
        w.user, 'wallet_credit',
        f'✅ Withdrawal Approved — ₹{w.amount}',
        f'Your withdrawal of ₹{w.amount} has been approved and sent to {w.upi_id}.'
    )
    return redirect('/creator-admin/')


@login_required
def reject_withdrawal(request, withdrawal_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    w = get_object_or_404(WithdrawalRequest, id=withdrawal_id)
    credit_wallet(
        w.user, w.amount,
        'withdrawal_refund',
        f'♻️ Withdrawal rejected — ₹{w.amount} refunded to wallet'
    )
    send_notification(
        w.user, 'wallet_credit',
        f'♻️ Withdrawal Rejected — ₹{w.amount} Refunded',
        f'Your withdrawal of ₹{w.amount} was rejected. Amount has been refunded to your wallet.'
    )
    w.status = 'rejected'
    w.save()
    return redirect('/creator-admin/')


@login_required
def add_reward_code(request):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        code = request.POST.get('code')
        description = request.POST.get('description', '')
        RewardCode.objects.create(code=code, description=description)
    return redirect('/creator-admin/')


@login_required
def send_reward_code(request, code_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        code = get_object_or_404(RewardCode, id=code_id)
        user_id = request.POST.get('user_id')
        user = get_object_or_404(User, id=user_id)
        code.assigned_to = user
        code.sent = True
        code.save()
        try:
            send_mail(
                subject='🎁 Your Reward Code - Clash Arena',
                message=f'Hi {user.username},\n\nYour reward code is: {code.code}\n\nDescription: {code.description}\n\nThank you for playing on Clash Arena!',
                from_email=None,
                recipient_list=[user.email],
            )
        except Exception:
            pass
        send_notification(
            user, 'general',
            '🎁 Reward Code Received!',
            f'You received a reward code: {code.code}. Check your email for details.'
        )
    return redirect('/creator-admin/')


@login_required
def grant_membership(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    if request.method == "POST":
        u = get_object_or_404(User, id=user_id)
        plan = request.POST.get('plan')

        if plan == '1month':
            expiry = timezone.now() + timedelta(days=30)
        elif plan == '3month':
            expiry = timezone.now() + timedelta(days=90)
        elif plan == '1year':
            expiry = timezone.now() + timedelta(days=365)
        else:
            expiry = timezone.now() + timedelta(days=30)

        CreatorMembership.objects.create(user=u, plan=plan, expires_at=expiry)
        u.profile.is_creator = True
        u.profile.creator_plan = plan
        u.profile.plan_expiry = expiry
        u.profile.tournaments_created_this_month = 0
        u.profile.save()

        send_notification(
            u, 'general',
            '👑 Creator Membership Activated!',
            f'Your {plan} creator membership is now active! You can start hosting tournaments.'
        )

    return redirect('/creator-admin/')


@login_required
def promote_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    u.profile.is_admin = True
    u.profile.is_creator = True
    u.profile.save()
    return redirect('/creator-admin/')


@login_required
def demote_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    u.profile.is_admin = False
    u.profile.is_creator = False
    u.profile.save()
    return redirect('/creator-admin/')


@login_required
def ban_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    if u != request.user:
        u.is_active = False
        u.save()
    return redirect('/creator-admin/')


@login_required
def unban_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    u.is_active = True
    u.save()
    return redirect('/creator-admin/')


@login_required
def delete_user(request, user_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    u = get_object_or_404(User, id=user_id)
    if u != request.user:
        u.delete()
    return redirect('/creator-admin/')


@login_required
def deactivate_membership(request, membership_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    membership = get_object_or_404(CreatorMembership, id=membership_id)
    membership.is_active = False
    membership.save()
    membership.user.profile.creator_plan = 'none'
    membership.user.profile.plan_expiry = None
    membership.user.profile.is_creator = False
    membership.user.profile.save()
    send_notification(
        membership.user, 'general',
        '❌ Membership Deactivated',
        'Your creator membership has been deactivated by Admin. Contact support for more info.'
    )
    return redirect('/creator-admin/')


@login_required
def reactivate_membership(request, membership_id):
    if not request.user.profile.is_admin:
        return redirect('/')
    membership = get_object_or_404(CreatorMembership, id=membership_id)
    membership.is_active = True
    membership.save()
    membership.user.profile.creator_plan = membership.plan
    membership.user.profile.plan_expiry = membership.expires_at
    membership.user.profile.is_creator = True
    membership.user.profile.save()
    send_notification(
        membership.user, 'general',
        '✅ Membership Reactivated!',
        f'Your {membership.plan} creator membership has been reactivated. You can host tournaments again!'
    )
    return redirect('/creator-admin/')